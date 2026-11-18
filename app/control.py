"""control.py - 명령을 받아 안전 규칙을 지나 장비에 보낸다. 서버와 시험이 같은 입구(run_command)를 쓴다.

거절은 Rejected(쉬운 한국어 한 문장). 이동은 한 번에 하나만(이동 잠금) - 여러 단계 이동(Z 올리기 → X · Y)이 다른 이동과 섞이지 않게 한다.
"""

import ntpath
import os
import re
import threading
import time
from contextlib import contextmanager
from typing import Optional

from app import config as config_mod
from app import contact as contact_mod
from app.calib import Calib
from app.config import Config
from app.devices import meters
from app.devices.camera import Camera, CameraError
from app.devices import stage as stage_mod
from app.devices.stage import PPMM, Stage, StageError
from app.logs import Logs
from app.jobs.run import Runner
from app.jobs.temptest import TempTest
from app.prep import Prep
from app.safety import BY_MANUAL, BY_RUN, Rejected, Safety
from app.state import ALARM, NOTICE, State

N_RELAY = config_mod.N_RELAY

AXES = "xyz"
# K2450 을 쓰는 쪽(사용권 - 한 번에 하나). 거절 글 "저항계 사용 중 · <누가>" 에 그대로 쓴다
USE_CONTACT, USE_ONCE, USE_MONITOR, USE_RUN, USE_RAW = "접촉 탐색", "1회 측정", "연속 측정", "자동 측정", "직접 명령"

# 스테이지 직접 명령은 **허용 목록**이다(막을 명령을 적는 방식은 빠진 명령이 그대로 통과한다).
# 펌웨어 V11(firmware/stage_v11/stage_v11.ino handleLine)의 명령을 하나씩 확인해 "읽기만 하는 것" 만 골랐다. 값이 붙으면 거절한다(한 낱말만).
#   st   printST()                      파싱용 상태 한 줄 - 아무것도 바꾸지 않는다
#   p    printStatus() + printWiring()  사람용 상태 · 배선 표 - 바꾸지 않는다
#   rs   printRS()                      릴레이 상태 한 줄 - 바꾸지 않는다
#   wd   wdCmd() 값 없이                 지금 통신 감시 시간만 찍는다(값을 주면 감시 시간을 바꾼다 - wd 0 은 감시를 끈다)
#   ?    모르는 명령 → 도움말            명령 목록을 찍는다 - 바꾸지 않는다
# 허용하지 않는 것과 까닭:
#   이동      fz home gx gy gz g mx my mz jx jy jz x y x1 x2 rx ry fx fy   (x1 · x2 는 X 모터 하나만 움직여 갠트리를 튼다)
#   원점 · 위치 z zx zy zz sp forget save                                   (원점을 몰래 바꾸거나 EEPROM 을 쓴다)
#   릴레이    r <n> <1|0> · r all 0                                         (안전 규칙 3 을 돌아간다 - 버튼을 쓴다)
#   파라미터  v a b w e                                                     (v · a · b 는 값이 없으면 상태만 찍지만 p 가 같은 것을 보여 준다.
#                                                                           w 는 값이 없으면 0 으로 바꾼다. e 1 은 모터를 풀고 원점을 지운다)
#   '!' 는 지금처럼 비상정지로 처리한다.
STAGE_RAW_OK = ("st", "p", "rs", "wd", "?")
RAW_STAGE_NO = "직접 명령은 읽기만 가능 · 이동 · 원점 · 릴레이 · 설정은 버튼 사용"
# 측정 장비 직접 명령은 조회(…?)만. 조회 모양이어도 장비를 움직이는 것은 뺀다(머리글 앞부분으로 가린다):
#   MEAS…?  기능 · 범위를 다시 설정하고 잰다(K2110 은 열전대 설정이 풀린다)   *TST? · *CAL?  자가 시험 · 교정
#   K2450 의 READ? · INIT 는 소스를 써서 재는 동작이다 - 재는 것은 [1회 측정] 버튼으로 한다
METER_RAW_NO_HEAD = {"k2450": ("MEAS", "READ", "INIT", "*TST", "*CAL"), "k2110": ("MEAS", "INIT", "*TST", "*CAL")}
RAW_METER_NO = "직접 명령은 조회만 가능 · 출력과 설정은 버튼 사용"
# 송풍 · 열선 릴레이는 개별 릴레이 버튼 · 쿨러 경로로 켜고 끄지 않는다(히트건 인터락: 송풍을 먼저 켜고 열선을 켜면 히트건이 오류를 낸다).
RELAY_HEATER_NO = "히트건 릴레이 · 히트건 버튼 사용"


def meter_raw_ok(target, line) -> bool:
    """측정 장비 직접 명령이 조회인가: 한 줄에 명령 하나(';' 로 잇지 않는다) · 머리글(첫 낱말)이 '?' 로 끝난다 · 장비를 움직이는 조회가 아니다."""
    line = str(line).strip()
    if not line or ";" in line:
        return False
    head = line.split()[0].upper()
    if not head.endswith("?"):
        return False
    return not head.lstrip(":").startswith(METER_RAW_NO_HEAD.get(target, ()))


CAMERA_SCAN = (0, 1, 2, 3)                                         # 카메라 목록을 만들 때 열어 보는 번호


def serial_ports():
    """이 컴퓨터의 시리얼 포트 [(이름, 장치 설명)]. 목록만 읽는다 - 포트를 열지 않는다(여는 것은 사람이 고른 하나뿐이다)."""
    try:
        from serial.tools import list_ports
        return sorted((str(p.device), str(p.description or "")) for p in list_ports.comports())
    except Exception:                                               # noqa: BLE001
        return []


def port_label(name, desc):
    """선택 상자에 보일 글: "COM5 · Arduino Mega 2560". 설명 끝의 "(COM5)" 는 떼고, 설명이 없으면 이름만."""
    d = re.sub(r"\s*\(%s\)\s*$" % re.escape(name), "", str(desc or "").strip(), flags=re.I).strip()
    return name if not d or d.lower() in ("n/a", name.lower()) else "%s · %s" % (name, d)


def open_serial(name):
    import serial
    return serial.Serial(name, 115200, timeout=0.2, write_timeout=1.0)


class Controller:
    def __init__(self, cfg: Optional[Config] = None, clock=None, sleep=None, open_port=None):
        """장비를 꽂는 자리: open_port(이름) → 시리얼 포트 객체(기본 open_serial) · clock() · sleep(초)(기본 time.monotonic · time.sleep) ·
        meters.RM_FACTORY(VISA 관리자) · camera.open_capture(카메라 열기). 시험은 이 자리에 시험용 장비와 시계를 꽂는다."""
        self.cfg = cfg or Config().load()
        self.clock = clock or time.monotonic                        # 안전 규칙 · 측정이 쓰는 시계(초)
        self.sleep = sleep or time.sleep                            # 측정이 읽는 간격을 기다릴 때 쓴다
        self.state = State()
        self.logs = Logs(self.cfg.data_path())
        self.safety = Safety(self.cfg, self.state, self.clock)
        self.stage = Stage(open_port or open_serial, gen_ok=self.safety.gen_ok, log=self.logs.serial,
                           on_event=self._stage_event, on_status=self._stage_status)
        self.k2110: Optional[meters.K2110] = None
        self.k2450: Optional[meters.K2450] = None
        self.camera = Camera(log=self.logs.app, on_event=self._camera_event)
        self.calib = Calib(self.cfg.data_path())
        self.prep = Prep(self)
        self.runner = Runner(self)                                  # 측정(app/jobs/run.py)
        self.temptest = TempTest(self)                              # 온도 시험(app/jobs/temptest.py - 수동 제어 탭)
        self.heater_trip = ""                                       # 안전 규칙이 열선을 마지막으로 끈 이유
        self.heater_trips = 0                                       # 안전 규칙이 열선을 끈 횟수(측정이 "안전 규칙이 먼저 껐다" 를 알아챈다)
        self._heater_lock = threading.RLock()                       # 히트건 켜고 끄는 순서는 한 번에 하나(켜기 · 끄기 · 안전 감시 · 송풍 늦게 끄기)
        self._heater_starting = False                               # 켜기 순서(열선 → 대기 → 송풍) 진행 중
        self._heater_by = None                                      # 지금 보내는 "열선 켜기"의 주체(수동 · 측정) - 릴레이 상태가 읽히는 순간에 쓴다
        self._heater_why = None                                     # 지금 보내는 "전부 끄기"의 까닭(로그용)
        self._heater_active = False                                 # 송풍 또는 열선이 켜져 있다(릴레이 상태로 안다)
        self._heater_off_logged = False                             # 이번 "히트건 끔" 을 이미 로그에 적었다(송풍이 늦게 꺼질 때 두 번 적지 않게)
        self._element_off_t = None                                  # 열선을 마지막으로 끈 시각(clock) - 송풍은 여기서 heater_fan_after_s 뒤에 끈다
        self._fan_off_at = None                                     # 송풍을 끌 시각(clock). 없으면 None
        self._fan_off_epoch = None                                  # 같은 시각(화면용 epoch)
        self._folder_picker = None                                  # 폴더 선택 창을 띄우는 함수(창으로 실행했을 때 run.py 가 넘긴다)
        self._cam_lock = threading.Lock()                           # 카메라 열기(연결 · 목록용 열어 보기)는 한 번에 하나
        self._k2450_cv = threading.Condition()                      # K2450 사용권(한 번에 한 기능)
        self._k2450_user = None                                     # 지금 K2450 을 쓰는 쪽(USE_*). 없으면 None
        self._scan_lock = threading.Lock()                          # 장비 목록 읽기는 한 번에 하나
        self._motion = threading.Lock()
        self._plock = threading.Lock()                              # 조그 대기 칸 · 현재 이동 종류
        self._cur_kind = None
        self._jog_pending = None                                    # 대기 칸 하나: {"axis", "mm"}
        self._monitor_on = False
        self._alive = True
        self._shut = False                                          # 종료 처리는 한 번만(창 닫기 · 서버 종료가 둘 다 부른다)
        self._shut_lock = threading.Lock()
        self._threads = []
        meters.LOG = self.logs.visa
        for n in self.cfg.loaded_notes:
            self.logs.app(n, "warn")
            self.state.alarm("config", n, NOTICE)
        self._apply_migration()
        self.state.set("hw", {"find_push_mm": stage_mod.FIND_PULSE / PPMM, "find_gap_mm": stage_mod.FIND_GAP_PULSE / PPMM})
        self.publish_cfg()
        self.prep.publish()
        self.runner.publish()
        self.temptest.publish()
        # 측정 시작 조건(gate)의 입력이 바뀌는 길을 여기 한 곳에 모은다 → gate_inputs_changed:
        #   state 의 devices(장비 연결 · 끊김 · 오류) · homed(세 축 원점) · estop_locked(비상정지) · cfg(접촉 높이 등 설정) /
        #   calib 저장(판 영역 · 좌표 보정 · 접촉 높이 기록) / prep.pos_block(헤드 위치 확인)
        self.state.watch(("devices", "homed", "estop_locked", "cfg"), self.gate_inputs_changed)
        self.calib.on_change = self.gate_inputs_changed
        self._start_threads()

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------
    def list_devices(self):
        """이 컴퓨터의 장비 목록(선택 상자용). 포트 · 측정 장비는 목록만 읽고 열지 않는다.
        카메라는 0~3 번을 잠깐 열어 보고 열리는 것만 넣는다. 연결 중인 카메라 번호는 열어 보지 않고 그대로 넣는다."""
        blocked = self.cfg["blocked_ports"]                         # 사용 안 할 포트: 목록에는 보이지만 고를 수 없다(화면이 꺼 둔다)
        ports = [{"value": n, "label": port_label(n, d) + (" (사용 안 함)" if config_mod.port_blocked(n, blocked) else ""),
                  "blocked": config_mod.port_blocked(n, blocked)} for n, d in serial_ports()]
        visa, note = meters.list_resources()
        cams, closed = [], []
        c = self.cfg
        for i in CAMERA_SCAN:
            with self._cam_lock:                                    # 연결([연결] · 자동 연결)과 겹치지 않게 번호 하나씩
                if self.camera.connected and str(i) == self.state.get("devices", "camera", "target"):
                    size = tuple(self.camera.info.get("actual") or (0, 0))
                else:
                    size = self.camera.probe(i, c["camera_width"], c["camera_height"], c["camera_fourcc"])
            if size is None:
                closed.append(str(i))
            else:
                cams.append({"value": i, "label": "%d · %d×%d" % (i, size[0], size[1]) if size[0] and size[1] else str(i)})
        self.logs.app("장비 목록 · 포트 %d · 측정 장비 %d · 카메라 %s%s" % (
            len(ports), len(visa), " ".join(str(x["value"]) for x in cams) or "없음", " (열기 실패 %s)" % " ".join(closed) if closed else ""))
        return {"ports": ports, "visa": [{"value": a, "label": a} for a in visa], "cameras": cams, "note": note}

    def refresh_devices(self):
        """[새로고침]: 세 목록을 뒤에서 다시 읽는다(카메라를 열어 보느라 몇 초 걸릴 수 있다). 바로 돌아오고 결과는 state.devlist 에 온다."""
        if not self._scan_lock.acquire(blocking=False):
            return {"busy": True}                                   # 이미 읽는 중
        self.state.update("devlist", busy=True)

        def work():
            try:
                self.state.update("devlist", busy=False, t=time.time(), **self.list_devices())
            except Exception as e:                                  # noqa: BLE001 - 목록을 못 읽어도 프로그램은 계속 돈다
                self.logs.app("장비 목록 읽기 오류 · %s: %s" % (type(e).__name__, e), "error")
                self.state.update("devlist", busy=False, note="장비 목록 읽기 실패")
            finally:
                self._scan_lock.release()

        threading.Thread(target=work, daemon=True, name="device-list").start()
        return {"busy": True}

    def connect_stage(self, port: Optional[str] = None):
        port = (port or self.cfg["stage_port"]).strip()
        if not port:
            raise Rejected("스테이지 포트 선택 필요")
        if self.stage.connected:
            raise Rejected("스테이지 이미 연결됨")
        if config_mod.port_blocked(port, self.cfg["blocked_ports"]):   # 다른 장비가 쓰는 포트(열면 그 장비가 재시작한다): [연결] · 자동 연결 모두 열지 않는다
            why = "%s 사용 안 함 · 다른 포트 선택 필요" % port
            self.state.set_device("stage", "오류", why, port)
            self.logs.app("스테이지 연결 거절 · %s 는 사용 안 할 포트" % port, "warn")
            raise Rejected(why)
        self.state.set_device("stage", "연결 중", "", port)
        try:
            speed = (self.cfg["move_speed_pps"], self.cfg["move_accel_pps2"], self.cfg["move_start_pps"])
            self.stage.connect(port, speed, self.cfg["watchdog_ms"])
        except StageError as e:
            self.state.set_device("stage", "오류", str(e), port)
            detail = getattr(e, "detail", "")                       # 포트 열기 실패: 화면은 한글 한 줄, 원래 예외 전체는 로그에
            self.logs.app("스테이지 연결 실패 · %s%s" % (e, " · " + detail if detail else ""), "warn")
            raise Rejected(str(e))
        self.state.set_device("stage", "연결", "", port, "펌웨어 %s · %s" % (self.stage.firmware, port))
        self._remember(stage_port=port)
        self.state.clear_alarm("stage_lost")
        self._stage_status()
        self.logs.app("스테이지 연결 · %s · 펌웨어 %s" % (port, self.stage.firmware))
        return {"port": port, "firmware": self.stage.firmware}

    def disconnect_stage(self):
        if self.stage.connected:
            try:
                self.stage.relays_off(None, prio=0)
            except StageError:
                pass
        self.stage.disconnect()
        self.state.set_device("stage", "끊김", "", "")
        self._heater_sync("스테이지 연결 끊기", force_off=True)
        self._set_move(None)

    def connect_meter(self, kind: str, addr: Optional[str] = None):
        if kind not in ("k2110", "k2450"):
            raise Rejected("알 수 없는 장비")
        cls = meters.K2110 if kind == "k2110" else meters.K2450
        addr = (addr or self.cfg["%s_addr" % kind]).strip()
        if meters.visa_kind(addr) == "시리얼":
            raise Rejected("시리얼 포트는 측정 장비로 사용 불가")
        self._close_meter(kind)
        self.state.set_device(kind, "연결 중", "", addr)
        dev = cls(addr)
        try:
            dev.open()
            if kind == "k2110":
                dev.configure()
            else:
                ok, why = dev.check_scpi()
                if not ok:
                    dev.close()
                    raise meters.VisaError(why)
        except meters.VisaUnavailable as e:
            self.state.set_device(kind, "오류", "드라이버 없음", addr)
            raise Rejected(str(e))
        except meters.VisaError as e:
            self.state.set_device(kind, "오류", str(e), addr)
            self.logs.app("%s 연결 실패 · %s" % (kind, e), "warn")
            raise Rejected(str(e))
        setattr(self, kind, dev)
        self.state.set_device(kind, "연결", "", addr, dev.idn)
        self._remember(**{"%s_addr" % kind: addr})
        self.logs.app("%s 연결 · %s" % (kind, dev.idn))
        return {"addr": addr, "idn": dev.idn}

    def disconnect_meter(self, kind: str):
        self._close_meter(kind)
        self.state.set_device(kind, "끊김", "", "")

    def _close_meter(self, kind):
        dev = getattr(self, kind)
        setattr(self, kind, None)
        if dev is not None:
            if kind == "k2450":
                try:
                    dev.output_off_urgent(1.0)
                except meters.VisaError:
                    pass
            dev.close()

    # ------------------------------------------------------------------
    # 스테이지 → 상태
    # ------------------------------------------------------------------
    def _stage_status(self):
        s = self.stage
        for ax in AXES:
            p = s.pos.get(ax)
            self.state.set("pos", ax, None if p is None else round(p / PPMM, 3))
            self.state.set("homed", ax, bool(s.homed.get(ax)))
        self.state.set("dirty", s.dirty)
        for i, r in enumerate(s.relays):
            self.state.set("relays", i, r)
        self._heater_sync()

    def _heater_sync(self, why: Optional[str] = None, force_off: bool = False):
        """릴레이 상태(st · rs 로 읽은 것)를 안전 감시 · 화면 상태에 반영한다. 스테이지 일꾼 스레드에서도 불린다(잠그지 않는다).
        열선이 켜지고 꺼진 것 → 안전 감시(열선 연속 시간 · 차단 온도 · 온도 못 읽음). 송풍 또는 열선 → "히트건 켜짐"(X · Y 이동 거절).
        force_off = 통신 끊김 · 연결 끊기 · 보드 재시작(릴레이는 보드가 끈다 - 상태를 몰라도 꺼진 것으로 본다).
        로그: 히트건이 켜지는 것은 켠 쪽(heater_on)이 "히트건 켬 · 수동|측정" 으로, 끄는 것은 끈 쪽이 까닭과 함께 적는다.
        여기서는 그 밖의 길(통신 감시 · 보드 재시작 · 직접 끈 것)만 "릴레이 꺼짐 감지" 등으로 적는다."""
        fan, el, _ = self._roles()
        r = self.stage.relays
        fv, ev = (False, False) if force_off else (r[fan - 1], r[el - 1])
        if ev is True and self.safety.element_on_t is None:
            self.safety.element_turned_on(self._heater_by or self.safety.heater_by or BY_RUN)     # 누가 켰는지 모르면 측정 쪽 규칙(더 엄한 쪽)
        elif ev is False and self.safety.element_on_t is not None:
            self.safety.element_turned_off()
            self._element_off_t = self.clock()
        if ev is False and self._element_off_t is None:
            self._element_off_t = self.clock()
        active = fv is True or ev is True
        if active and not self._heater_active:
            self._heater_active = True
            self._heater_off_logged = False
            self.state.set("heater_on_since", time.time())
            if self._heater_by is None:
                self.logs.app("히트건 켬 · 릴레이 켜짐 감지")
        elif not active and self._heater_active and fv is False and ev is False:
            self._heater_active = False
            self.safety.heater_released()
            self._fan_off_at = self._fan_off_epoch = None
            self.state.set("heater_on_since", None)
            if not self._heater_off_logged:
                self.logs.app("히트건 끔 · %s" % (why or self._heater_why or "릴레이 꺼짐 감지"))
            self._heater_off_logged = False
        self._publish_heater(fv, ev)

    def _publish_heater(self, fv=None, ev=None):
        """화면용 히트건 상태: 송풍 · 열선(True · False · None 모름) · 송풍을 끌 시각(epoch - 화면이 남은 초를 센다)."""
        if fv is None and ev is None:
            fan, el, _ = self._roles()
            fv, ev = self.stage.relays[fan - 1], self.stage.relays[el - 1]
        self.state.set("heater", {"fan": fv, "element": ev, "fan_off_at": self._fan_off_epoch})

    def _apply_migration(self):
        """예전 settings.json 을 지금 규칙으로 옮긴 것(config._migrate)을 로그에 한 줄씩 남기고 파일에 저장한다.
        쿨러 배치를 기본값으로 되돌렸으면 초기 설정 7 을 미완료로 한다(다시 켜 보고 정해야 한다)."""
        cfg = self.cfg
        for n in getattr(cfg, "migrated", []):
            self.logs.app("설정 이행 · %s" % n, "warn")
        if getattr(cfg, "cooler_reset", False):
            self.calib.clear_cooler()
        if getattr(cfg, "migrated", []):
            try:
                cfg.save()
            except OSError as e:
                self.logs.app("설정 저장 실패 · %s" % e, "warn")

    def _roles(self):
        """릴레이 역할 (송풍, 열선, [쿨러 …]). 그 밖의 릴레이는 미배정."""
        c = self.cfg
        return int(c["heater_fan_relay"]), int(c["heater_element_relay"]), [int(n) for n in c["cooler_relays"]]

    def _relay_is_on(self, n) -> bool:
        return self.stage.relays[n - 1] is True

    def _heater_on(self) -> bool:
        """히트건 켜짐 = 송풍 또는 열선이 켜짐(X · Y 이동 거절의 기준)."""
        fan, el, _ = self._roles()
        return self.safety.element_on_t is not None or self._relay_is_on(fan) or self._relay_is_on(el)

    def _element_on(self) -> bool:
        return self._relay_is_on(self._roles()[1])

    def _fan_on(self) -> bool:
        return self._relay_is_on(self._roles()[0])

    def _stage_event(self, kind: str, text: str):
        """일꾼 스레드에서 온다 - 오래 걸리는 일은 새 스레드로 넘긴다."""
        if kind == "reboot":
            threading.Thread(target=self._board_reboot, args=(text,), daemon=True, name="reboot").start()
        elif kind == "wd_trip":
            self.state.alarm("wd", "통신 끊김 · 릴레이 차단", NOTICE)
            self.logs.app("통신 감시 작동 · %s" % text, "warn")
        elif kind == "lost":
            self.state.set_device("stage", "오류", "통신 끊김 · " + text[:40], self.stage.port_name)
            self.state.alarm("stage_lost", "스테이지 연결 끊김", ALARM)
            self._heater_sync("통신 끊김", force_off=True)
            self.logs.app("스테이지 통신 끊김 · %s" % text, "error")

    def _board_reboot(self, text):
        """보드가 다시 시작됐다: 비상정지와 같이 잠그고, wd 를 다시 보내고, 위치 확인 경보를 낸다."""
        self.safety.begin_estop()
        self.state.alarm("reboot", "보드 재시작 · 위치 확인 필요", ALARM)
        self.logs.app("보드 재시작 감지 · %s" % text, "error")
        self._heater_sync("보드 재시작", force_off=True)
        try:
            self.stage.call("wd %d" % int(self.cfg["watchdog_ms"]), None, lambda ln: "통신 감시" in ln, 3.0, prio=0)
            self.stage.read_relays()
            self.stage.read_status()
        except StageError as e:
            self.logs.app("재시작 뒤 정리 실패 · %s" % e, "warn")

    # ------------------------------------------------------------------
    # 비상정지
    # ------------------------------------------------------------------
    def estop(self, source: str = "화면"):
        """어떤 일 중이든 기다리지 않는다: 세대 올리기 → '!' → (이동이 멈추면) r all 0 → K2450 출력 끄기."""
        t0 = time.monotonic()
        heater_was_on = self._heater_on()
        self.safety.begin_estop()                                   # 세대 올림 · 잠금 · 히트건 감시는 이 순간부터 "꺼짐"
        self._fan_off_at = self._fan_off_epoch = None               # 송풍도 r all 0 이 같이 끈다(늦게 끄기 없음)
        self.state.set("heater_on_since", None)
        if heater_was_on:
            self._heater_off_logged = True
            self.logs.app("히트건 끔 · 비상정지")
        self.runner.on_estop()                                      # 측정도 멈춘다(정리는 측정 스레드가: 히트건 · 출력 · 쿨러 끔, 핀은 그대로)
        self.temptest.on_estop()                                    # 온도 시험도 바로 끝난다(기록 · 요약은 저장)
        with self._plock:
            self._jog_pending = None                                # 대기 중이던 조그를 지운다
        self._monitor_on = False
        self.state.set("monitor", False)
        banged = False
        try:
            banged = self.stage.bang()
        except StageError as e:
            self.state.alarm("estop_io", "비상정지 신호 전송 실패 · 모터 전원 직접 차단 필요", ALARM)
            self.logs.app("비상정지 신호 실패 · %s" % e, "error")
        self.logs.app("비상정지 (%s) · '!' %s · %.1f ms" % (source, "전송" if banged else "못 보냄", (time.monotonic() - t0) * 1000))
        if banged:
            try:
                self.stage.submit("r all 0", None, stage_mod.is_rs, 3.0, prio=0).result(timeout=10)
            except Exception as e:                                  # noqa: BLE001
                self.state.alarm("estop_relay", "릴레이 끄기 실패 · 히트건과 쿨러 전원 직접 차단 필요", ALARM)
                self.logs.app("비상정지 뒤 릴레이 끄기 실패 · %s" % e, "error")
        self._heater_sync("비상정지", force_off=True)
        dev = self.k2450
        if dev is not None:
            try:
                dev.output_off_urgent(1.0)
            except Exception as e:                                  # noqa: BLE001
                self.state.alarm("estop_k2450", "K2450 출력 끄기 실패 · 장비에서 직접 차단 필요", ALARM)
                self.logs.app("비상정지 뒤 K2450 출력 끄기 실패 · %s" % e, "error")
        return {"locked": True}

    def estop_release(self):
        """비상정지 해제: 위치와 릴레이 상태를 다시 읽기만 한다. 릴레이 · K2450 출력은 꺼진 채, 버려진 이동은 버려진 채, 측정은 중지된 채로 둔다.
        정지 직후(ESTOP_HOLD_S)에는 받지 않는다."""
        why = self.safety.reject_release()
        if why:
            raise Rejected(why)
        if self.stage.connected:
            try:
                self.stage.call("st", None, stage_mod.is_st, 3.0, prio=0)
                self.stage.read_relays()
            except StageError as e:
                raise Rejected("위치 읽기 실패로 해제 불가 · %s" % e)
        self.safety.release()
        self.state.clear_alarm("estop_io", "estop_relay", "estop_k2450")
        self.logs.app("비상정지 풀림 · 위치를 다시 읽음")
        return {"locked": False}

    # ------------------------------------------------------------------
    # 이동
    # ------------------------------------------------------------------
    def _need_stage(self):
        if not self.stage.connected:
            raise Rejected("스테이지 연결 안 됨")

    def _set_move(self, what, target=None, pending=None):
        """이동 중 정보(펌웨어는 이동 중 위치를 알려 주지 않는다). 끝나면 None."""
        self.state.set("move", None if what is None else {"what": what, "target": target, "pending": pending})

    @contextmanager
    def _moving(self, what, target=None, lock=True):
        if lock and not self._motion.acquire(blocking=False):
            raise Rejected("이동 중")
        self.state.set("busy", what)
        self._set_move(what, target)
        try:
            yield
        except Rejected:
            raise
        except stage_mod.Discarded:
            raise Rejected(self._cancel_text())
        except stage_mod.MoveAborted:                               # '!' 로 끊겼다: 비상정지면 위치 확인을 알리고, 이동 정지면 취소만 알린다
            raise Rejected("이동 중단 · 위치 확인 필요" if self.safety.locked else "정지로 취소됨")
        except stage_mod.StageRejected as e:
            raise Rejected(str(e))
        except StageError as e:
            raise Rejected("스테이지 오류 · %s" % e)
        finally:
            self.state.set("busy", "")
            self._set_move(None)
            if lock:
                self._motion.release()

    def _cancel_text(self):
        """세대가 바뀌어 버려진 명령의 거절 글: 비상정지(잠김) / 이동 정지."""
        return "비상정지로 취소됨" if self.safety.locked else "정지로 취소됨"

    def motion_stop(self, source: str = "화면"):
        """이동 정지: 이동만 멈추고 잠그지 않는다(비상정지와 다르다).
        1) 세대를 올려 대기 중인 조그 · 줄 서 있는 이동 · 여러 걸음 이동의 남은 걸음을 버린다 2) '!' 를 줄 서지 않고 바로 쓴다 3) st 로 위치를 다시 읽는다.
        원점 · 릴레이 · 히트건 · K2450 출력은 건드리지 않는다. 접촉 탐색 중이었다면 탐색이 스스로 끝나며 출력을 끈다(핀은 그 자리).
        멈춰 있을 때 불러도 된다. 명령 줄을 거치지 않게 서버는 새 스레드에서 부른다(/api/stop)."""
        if self.run_active():
            raise Rejected("측정 중 · 측정 중지 또는 비상정지 사용")
        self._need_stage()
        t0 = time.monotonic()
        self.safety.cancel_pending()
        with self._plock:
            self._jog_pending = None
        try:
            banged = self.stage.bang()
        except StageError as e:
            self.logs.app("이동 정지 (%s) · '!' 전송 실패 · %s" % (source, e), "error")
            raise Rejected("정지 신호 전송 실패 · 비상정지 사용")
        self.logs.app("이동 정지 (%s) · '!' %s · %.1f ms" % (source, "전송" if banged else "못 보냄", (time.monotonic() - t0) * 1000))
        try:
            self.stage.call("st", None, stage_mod.is_st, 3.0, prio=0)   # 끊긴 이동이 끝난 뒤의 위치를 다시 읽는다
        except StageError as e:
            raise Rejected("위치 읽기 실패 · %s" % e)
        return {"pos": dict(self.state.get("pos")), "locked": self.safety.locked}

    def _mm(self, ax):
        p = self.stage.pos.get(ax)
        return None if p is None else p / PPMM

    def jog(self, axis: str, mm: float):
        """조그. 이동 중에 들어온 조그는 대기 칸 하나에 받아 두었다가 지금 이동이 끝나면 이어서 한다
        (같은 축 · 같은 방향이면 거리를 더하고, 다르면 대기를 새 것으로 바꾼다). 비상정지는 대기를 지운다."""
        axis = str(axis).lower()
        if axis not in AXES:
            raise Rejected("축은 x · y · z 중 하나")
        self._need_stage()
        mm = float(mm)
        if stage_mod.mm_to_pulse(mm) == 0:
            raise Rejected("이동 거리 너무 짧음")
        why = self.safety.reject_move(axis in "xy", self._heater_on())
        if why:
            raise Rejected(why)
        if not self._motion.acquire(blocking=False):
            with self._plock:
                if self._cur_kind != "jog":
                    raise Rejected("이동 중")
                p = self._jog_pending
                if p and p["axis"] == axis and (p["mm"] > 0) == (mm > 0):
                    p["mm"] += mm
                else:
                    self._jog_pending = {"axis": axis, "mm": mm}
                self._publish_pending()
            return {"queued": True}
        with self._plock:
            self._cur_kind = "jog"
        item = {"axis": axis, "mm": mm}
        try:
            while item is not None:
                self._jog_once(item["axis"], item["mm"])
                with self._plock:
                    item, self._jog_pending = self._jog_pending, None
                    if item is None:
                        self._cur_kind = None
                        self._motion.release()                      # 대기가 없을 때만, 대기 칸 검사와 같은 잠금 안에서 놓는다
                        return {"pos": dict(self.state.get("pos"))}
                    self._publish_pending()
        except BaseException:
            with self._plock:
                self._jog_pending = None
                self._cur_kind = None
                self._motion.release()
            self._set_move(None)
            raise

    def _publish_pending(self):
        mv = self.state.get("move")
        if mv is not None:
            self.state.set("move", dict(mv, pending=self._jog_pending and dict(self._jog_pending)))

    def _jog_once(self, axis, mm):
        g = self.safety.generation
        pulses = stage_mod.mm_to_pulse(mm)
        why = self.safety.reject_move(axis in "xy", self._heater_on())
        if why:
            raise Rejected(why)
        cur = self._mm(axis)
        if self.stage.homed.get(axis) and cur is not None:
            why = self.safety.reject_target(axis, cur + pulses / PPMM, True)
            if why:
                raise Rejected(why)
        if axis == "z" and pulses > 0:                              # 수동 Z 하강 한도(켜져 있을 때만): 한도 아래로 가는 조그는 통째로 거절한다
            why = self.safety.reject_z_down(None if cur is None else cur + pulses / PPMM, bool(self.stage.homed.get("z")))
            if why:
                raise Rejected(why)
        tgt = {a: self._mm(a) for a in AXES}
        if tgt[axis] is not None:
            tgt[axis] = round(tgt[axis] + pulses / PPMM, 3)
        with self._moving("조그 %s" % axis.upper(), tgt, lock=False):
            self.stage.jog(axis, pulses, g)

    def goto(self, x: float, y: float, z: Optional[float] = None):
        """좌표로 가기: Z 맨 위 → st 로 Z=0 확인 → X → Y → (z 가 있으면) Z."""
        self._need_stage()
        x, y = float(x), float(y)
        z = None if z is None else float(z)
        homed = self.stage.homed
        miss = [a.upper() for a in AXES if not homed.get(a)] if z is not None else [a.upper() for a in "xyz" if not homed.get(a)]
        if miss:
            raise Rejected("%s 원점 필요 (초기 설정 2)" % " · ".join(miss))
        for ax, v in (("x", x), ("y", y)) + ((("z", z),) if z is not None else ()):
            why = self.safety.reject_target(ax, v, True)
            if why:
                raise Rejected(why)
        if z is not None and z > 0:                                 # 수동 Z 하강 한도: 움직이기 전에 명령 전체를 거절한다(측정은 Z 값을 주지 않는다)
            why = self.safety.reject_z_down(z, True)
            if why:
                raise Rejected(why)
        g = self.safety.generation
        why = self.safety.reject_move(True, self._heater_on())
        if why:
            raise Rejected(why)
        with self._moving("좌표 이동 중", {"x": round(x, 3), "y": round(y, 3), "z": round(z if z else 0.0, 3)}):
            self._z_top_checked(g)
            why = self.safety.reject_move(True, self._heater_on())     # 기다리는 동안 히트건이 켜졌을 수 있다
            if why:
                raise Rejected(why)
            self.stage.goto("x", stage_mod.mm_to_pulse(x), g)
            self.stage.goto("y", stage_mod.mm_to_pulse(y), g)
            if z is not None and z > 0:
                self.stage.goto("z", stage_mod.mm_to_pulse(z), g)
        return {"pos": dict(self.state.get("pos"))}

    def _z_top_checked(self, g):
        """Z 를 맨 위로 올리고 st 로 Z=0 을 확인한다. 확인되지 않으면 X · Y 를 움직이지 않는다."""
        st = self.stage.read_status(g)
        if st["pos"]["z"] != 0:
            self.stage.goto("z", 0, g)
            st = self.stage.read_status(g)
        if not self.safety.z_is_top(st["pos"]["z"]):
            raise Rejected("Z 맨 위 확인 실패")

    def z_top(self):
        self._need_stage()
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요 (초기 설정 2)")
        g = self.safety.generation
        why = self.safety.reject_move(False, False)
        if why:
            raise Rejected(why)
        with self._moving("핀 상승 중", {"x": self._mm("x"), "y": self._mm("y"), "z": 0.0}):
            self.stage.goto("z", 0, g)
        return {"pos": dict(self.state.get("pos"))}

    def set_origin(self, axis: str, mode: str = "here"):
        """mode: here = 지금 자리를 원점으로 · end = 끝단까지 밀어서 잡기. 끝나면 save."""
        axis = str(axis).lower()
        if mode not in ("here", "end"):
            raise Rejected("원점 방식 오류")
        if axis == "all" and mode == "here":                        # 세 축 모두 등록
            self.set_origin("xy", "here")
            return self.set_origin("z", "here")
        if axis not in (tuple(AXES) + (("xy",) if mode == "here" else ())):
            raise Rejected("축은 x · y · z 중 하나")
        self._need_stage()
        self._no_run("원점 설정")
        xy_push = mode == "end" and axis in "xy"
        if xy_push and not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요")           # 핀이 내려간 채 X · Y 를 밀면 판을 긁을 수 있다
        g = self.safety.generation
        why = self.safety.reject_move(xy_push, self._heater_on())
        if why:
            raise Rejected(why)
        with self._moving("원점 설정 중", None):
            if xy_push:
                self._z_top_checked(g)                              # 자동 이동과 같이: Z 맨 위 + st 로 Z=0 확인
                why = self.safety.reject_move(True, self._heater_on())
                if why:
                    raise Rejected(why)
            if mode == "here":
                self.stage.set_here(axis, g)
            else:
                self.stage.find_end(axis, g)
            self.stage.save(g)
            self.stage.read_status(g)
        self.calib.set_origin(axis, mode)                           # 등록한 때 · 방법(초기 설정 2단계 목록에 보인다)
        self.prep.clear_pos_check()                                 # 원점을 다시 맞추면 위치 확인 경보가 풀린다
        self.prep.publish()
        return {"homed": dict(self.state.get("homed"))}

    # ------------------------------------------------------------------
    # 릴레이: 역할(송풍 · 열선 · 쿨러)은 설정이 정한다(heater_fan_relay · heater_element_relay · cooler_relays).
    # 개별 릴레이 버튼은 쿨러만 켠다. 송풍 · 열선은 히트건 함수(heater_*)로만 켜고 끈다. 미배정 릴레이는 켜지 않는다(끄기는 된다).
    # ------------------------------------------------------------------
    def relay_on(self, n: int):
        """쿨러 릴레이 켜기(수동 제어 탭 · 초기 설정 7 · 측정의 쿨러)."""
        n = self._relay_no(n)
        self._need_stage()
        fan, el, cool = self._roles()
        if n in (fan, el):
            raise Rejected(RELAY_HEATER_NO)
        if n not in cool:
            raise Rejected("R%d 미배정 · 쿨러 릴레이 아님" % n)
        why = self.safety.reject_relay_on()
        if why:
            raise Rejected(why)
        self._switch(n, True, self.safety.generation)
        return {"relays": list(self.state.get("relays"))}

    def relay_off(self, n: int):
        """쿨러 · 미배정 릴레이 끄기(끄기는 비상정지 뒤에도 된다). 송풍 · 열선은 히트건 [끄기] 로(끄는 순서가 있다)."""
        n = self._relay_no(n)
        self._need_stage()
        fan, el, _ = self._roles()
        if n in (fan, el):
            raise Rejected(RELAY_HEATER_NO)
        self._switch(n, False, None)
        return {"relays": list(self.state.get("relays"))}

    def relays_off(self):
        """[전체 끄기]: 히트건은 끄는 순서대로(열선 끔 → 송풍은 heater_fan_after_s 뒤), 그 밖의 릴레이는 바로 끈다.
        온도 시험 중이면 시험을 [정지] 하고 그 정리(히트건 끄는 순서 · 쿨러 끔)가 끝나기를 기다린 뒤 나머지를 끈다 - 끄기는 언제나 된다."""
        self._need_stage()
        if self.temptest.active:
            self.temptest.stop()
            self.temptest.join(timeout=10)
        self.heater_off(BY_MANUAL)
        fan, el, _ = self._roles()
        for n in range(1, N_RELAY + 1):
            if n not in (fan, el) and self.stage.relays[n - 1] is not False:
                self._switch(n, False, None, prio=1)
        return {"relays": list(self.state.get("relays"))}

    def _switch(self, n, on, g, prio=5):
        """릴레이 하나를 켜거나 끄고, 응답의 릴레이 상태(rs)로 확인한다. 안 맞으면 Rejected. g = 세대(끄기는 None - 비상정지 뒤에도 된다)."""
        what = "켜기" if on else "끄기"
        try:
            self.stage.relay(n, on, g, prio=prio)
        except stage_mod.Discarded:
            raise Rejected(self._cancel_text())
        except StageError as e:
            raise Rejected("R%d %s 실패 · %s" % (n, what, e))
        if self.stage.relays[n - 1] is not on:
            raise Rejected("R%d %s 확인 실패" % (n, what))

    @staticmethod
    def _relay_no(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise Rejected("릴레이 번호는 1~%d" % N_RELAY)
        if not 1 <= n <= N_RELAY:
            raise Rejected("릴레이 번호는 1~%d" % N_RELAY)
        return n

    # ------------------------------------------------------------------
    # 히트건 = 송풍(바람) + 열선. 켜고 끄는 순서는 여기서만 정한다 - 수동 버튼 · 측정(RelayHeater) · 안전 감시가 모두 이 함수들을 지난다.
    #   켜기: 열선 켬 → heater_start_gap_s → 송풍 켬(각 단계 뒤 rs 로 확인 · 실패하면 둘 다 끄고 거절). 송풍이 이미 켜져 있으면 열선만 켠다.
    #   끄기: 열선 끔 → (열선을 끈 때부터) heater_fan_after_s 뒤 송풍 끔. 그 사이 다시 켜면 송풍이 켜져 있으므로 열선만 켠다.
    #   송풍이 꺼진 채 송풍만 켜는 길은 없다(히트건 인터락: 송풍을 먼저 켜고 열선을 켜면 히트건이 오류를 낸다).
    #   비상정지 · 통신 끊김 · 종료는 r all 0(전부 끔). 안전 판단(차단 온도 · 온도 못 읽음 · 열선 연속 최대 시간)은 열선만 바로 끄고 송풍은 뒤에.
    # ------------------------------------------------------------------
    def manual_heater_on(self):
        """수동 제어 탭 히트건 [켜기]: "수동" 규칙(온도계 조건을 보지 않는다)."""
        return self.heater_on(BY_MANUAL)

    def manual_heater_off(self):
        return self.heater_off(BY_MANUAL)

    def manual_heater_element(self, on):
        """수동 제어 탭 [열선 끄기] · [열선 켜기]: 송풍이 켜진 동안만."""
        return self.heater_element(bool(on), BY_MANUAL)

    def _heater_reply(self):
        return {"heater": dict(self.state.get("heater")), "relays": list(self.state.get("relays"))}

    def _check_element_on(self, by):
        why = self.safety.reject_heater_on(by, self.k2110 is not None and self.k2110.is_open)
        if why:
            raise Rejected(why)

    def _element_switch(self, on, by, g, prio=5):
        """열선 릴레이 하나만 바꾼다(부르는 쪽이 _heater_lock 을 쥐고 송풍 조건을 본다). 켜면 기다리던 "송풍 늦게 끄기" 를 거둔다."""
        el = self._roles()[1]
        if on:
            self._heater_by = by                                    # 릴레이 응답이 읽히는 순간(_heater_sync)에 주체를 알 수 있게 먼저 적는다
        try:
            self._switch(el, on, g, prio)
        finally:
            self._heater_by = None
        if on:
            self._fan_off_at = self._fan_off_epoch = None
            self._heater_off_logged = False
            self._publish_heater()

    def heater_on(self, by: str = BY_MANUAL):
        """히트건 켜기. 송풍이 꺼져 있으면 열선 켬 → heater_start_gap_s → 송풍 켬, 이미 켜져 있으면 열선만 켠다.
        기다리는 동안은 잠금 밖이다 - 그 사이 안전 감시 · 끄기 · 비상정지가 열선을 끌 수 있고, 그러면 송풍을 켜지 않고 둘 다 끈다."""
        self._need_stage()
        fan, el, _ = self._roles()
        with self._heater_lock:
            if self._heater_starting:
                raise Rejected("히트건 켜는 중")
            self._check_element_on(by)
            g = self.safety.generation
            if self._relay_is_on(fan):                              # 송풍이 켜져 있다(끄는 중 · 냉각 중 송풍 유지) → 열선만
                self._element_switch(True, by, g)
                self.logs.app("히트건 켬 · %s · 송풍 켜진 채 열선" % by)
                return self._heater_reply()
            self._heater_starting = True
            try:
                self._element_switch(True, by, g)
            except Rejected:
                self._heater_starting = False
                self._heater_abort("켜기 실패")
                raise
        try:
            self.sleep(float(self.cfg["heater_start_gap_s"]))
            with self._heater_lock:
                if not self.safety.gen_ok(g):
                    raise Rejected(self._cancel_text())
                if not self._relay_is_on(el):                       # 기다리는 동안 열선이 꺼졌다 - 송풍만 켜지 않는다
                    raise Rejected("히트건 켜기 취소됨 · 열선 꺼짐")
                why = self.safety.reject_relay_on()
                if why:
                    raise Rejected(why)
                self._switch(fan, True, g)
                self.logs.app("히트건 켬 · %s" % by)
        except Rejected:
            with self._heater_lock:
                self._heater_abort("켜기 실패")
            raise
        finally:
            self._heater_starting = False
        return self._heater_reply()

    def _heater_abort(self, why):
        """켜기 순서가 중간에 실패했다: 송풍 · 열선을 둘 다 끈다(세대와 무관 - 비상정지 뒤에도)."""
        fan, el = self._roles()[:2]
        if not self.stage.connected:
            return
        for n in (el, fan):
            if self.stage.relays[n - 1] is not False:
                try:
                    self._switch(n, False, None, prio=0)
                except Rejected as e:
                    self.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
                    self.logs.app("히트건 %s 뒤 끄기 실패 · %s" % (why, e), "error")
        self._fan_off_at = self._fan_off_epoch = None
        self._heater_off_logged = True
        self.logs.app("히트건 끔 · %s · 송풍 · 열선 끔" % why, "warn")

    def heater_element(self, on: bool, by: str = BY_MANUAL):
        """송풍이 켜진 동안 열선만 켜고 끈다(측정의 가열 속도 제어 · 수동 [열선 끄기 · 켜기]). 송풍이 꺼져 있으면 열선을 켜지 않는다."""
        self._need_stage()
        fan = self._roles()[0]
        with self._heater_lock:
            if not on:
                self._element_switch(False, by, None, prio=1)
                return self._heater_reply()
            if self._heater_starting:
                raise Rejected("히트건 켜는 중")
            if not self._relay_is_on(fan):
                raise Rejected("송풍 꺼짐 · 히트건 [켜기] 필요")
            self._check_element_on(by)
            self._element_switch(True, by, self.safety.generation)
        return self._heater_reply()

    def heater_off(self, by: str = BY_MANUAL, why: Optional[str] = None):
        """히트건 끄기: 열선 끔 → 열선을 끈 때부터 heater_fan_after_s 뒤 송풍 끔(감시 check_heater 가 끈다).
        그 시간이 이미 지났으면(냉각 중 송풍을 켜 둔 뒤) 송풍도 바로 끈다. 이미 꺼져 있으면 아무 일도 없다."""
        self._need_stage()
        fan, el = self._roles()[:2]
        with self._heater_lock:
            was = self._heater_on()
            if self.stage.relays[el - 1] is not False:
                self._element_switch(False, by, None, prio=1)
            note = ""
            if self.stage.relays[fan - 1] is not False:
                after = float(self.cfg["heater_fan_after_s"])
                t_el = self._element_off_t if self._element_off_t is not None else self.clock()
                due = t_el + after
                if self.clock() >= due:
                    self._switch(fan, False, None, prio=1)
                    self._fan_off_at = self._fan_off_epoch = None
                    note = "송풍 끔"
                elif self._fan_off_at is None:
                    self._set_fan_off(due)
                    note = "송풍 %.0f초 뒤 끔" % (due - self.clock())
            if was and not self._heater_off_logged:
                self._heater_off_logged = True
                self.logs.app("히트건 끔 · %s%s" % (why or by, " · " + note if note else ""))
            self._publish_heater()
        return self._heater_reply()

    def _set_fan_off(self, due):
        self._fan_off_at = due
        self._fan_off_epoch = time.time() + max(0.0, due - self.clock())

    def heater_fan_pending(self) -> bool:
        """송풍을 늦게 끄려고 기다리는 중(열선은 꺼져 있다)."""
        return self._fan_off_at is not None

    # ------------------------------------------------------------------
    # 히트건 감시 · 온도
    # ------------------------------------------------------------------
    def feed_temperature(self, value: Optional[float], now: Optional[float] = None):
        """온도 한 값을 받았다(감시 스레드 · 시험). 값 없음(None)이면 시각을 갱신하지 않는다."""
        self.safety.note_temp(value, now)
        if value is not None:
            self.state.update("temp", value=round(float(value), 2), time=time.time())
        self.check_heater(now)

    def check_heater(self, now: Optional[float] = None):
        """안전 판단(열선) + 송풍 늦게 끄기. 감시 스레드(0.25 초마다) · 측정의 온도 읽기 · 시험이 부른다."""
        trip = self.safety.heater_trip(now)
        if trip:
            self._heater_trip_off(*trip)
        due = self._fan_off_at
        if due is not None and (self.clock() if now is None else now) >= due:
            self._fan_off_due(now)

    def _fan_off_due(self, now=None):
        """열선을 끈 뒤 heater_fan_after_s 가 지났다: 송풍을 끈다(그 사이 열선이 다시 켜졌으면 송풍은 그대로)."""
        fan, el = self._roles()[:2]
        with self._heater_lock:
            due = self._fan_off_at
            if due is None or (self.clock() if now is None else now) < due:
                return
            self._fan_off_at = self._fan_off_epoch = None
            if self._relay_is_on(el) or not self.stage.connected:
                self._publish_heater()
                return
            try:
                self._switch(fan, False, None, prio=0)
                self.logs.app("송풍 끔 · 열선 끈 뒤 %g초" % float(self.cfg["heater_fan_after_s"]))
            except Rejected as e:
                self.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
                self.logs.app("송풍 끄기 실패 · %s" % e, "error")
            self._publish_heater()

    def _heater_trip_off(self, cause: str, text: str):
        """안전 규칙이 열선을 끈다(열선만 바로 · 송풍은 heater_fan_after_s 뒤). cause = 차단 온도 · 온도 못 읽음 · 최대 켜짐 시간(로그), text = 경보 문구."""
        self.heater_trip = text
        self.heater_trips += 1
        self.state.alarm("heater", text, ALARM, dismiss=True)
        why = "%s (켠 주체 %s)" % (cause, self.safety.heater_by or "?")
        with self._heater_lock:
            try:
                if self.stage.connected:
                    self._element_switch(False, None, None, prio=0)
            except Rejected as e:
                self.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
                self.logs.app("열선 끄기 실패 · %s" % e, "error")
            finally:
                self.safety.element_turned_off()                    # 못 껐어도 같은 판정을 0.25 초마다 되풀이하지 않는다(경보가 할 일을 알린다)
            note = ""
            if self.stage.connected and self.stage.relays[self._roles()[0] - 1] is not False and self._fan_off_at is None:
                t_el = self._element_off_t if self._element_off_t is not None else self.clock()
                self._set_fan_off(t_el + float(self.cfg["heater_fan_after_s"]))
                note = " · 송풍 %g초 뒤 끔" % float(self.cfg["heater_fan_after_s"])
            self._heater_off_logged = True
            self.logs.app("히트건 끔 · %s · 열선 끔%s" % (why, note), "error")
            self._publish_heater()

    def read_temp(self):
        dev = self.k2110
        if dev is None or not dev.is_open:
            raise Rejected("온도계 연결 안 됨")
        try:
            v = dev.read_temp()
        except meters.VisaError as e:
            self.state.set_device("k2110", "오류", str(e), dev.addr)
            raise Rejected(str(e))
        self.feed_temperature(v)
        return {"temp": v, "note": "" if v is not None else "값 없음 · 열전대 확인 필요"}

    def read_resistance(self):
        """[1회 측정]. 다른 쪽(접촉 탐색 · 자동 측정 …)이 K2450 을 쓰고 있으면 거절한다."""
        dev = self._need_k2450()
        self._no_run("저항 측정")                                   # 측정이 K2450 을 쓰는 중이다(설정을 바꾸면 안 된다)
        with self._use_k2450(USE_ONCE):
            return self._measure_r(dev)

    def _measure_r(self, dev, meter=None):
        """저항 한 번(설정 → 출력 켜기 → 읽기 → 출력 끄기)을 재서 화면 상태에 올린다. 사용권을 가진 쪽만 부른다. meter = 측정 조건(없으면 지금 설정)."""
        c = meter or self.cfg
        try:
            r = dev.measure_once(c["meter_volt"], c["meter_ilim_a"], c["meter_four_wire"], c["meter_nplc"], c["meter_avg_count"])
        except meters.VisaError as e:
            self.state.set_device("k2450", "오류", str(e), dev.addr)
            raise Rejected(str(e))
        self.state.update("resistance", ohm=r["ohm"], current=r["current"], volt=r["volt"], four_wire=r["four_wire"], time=time.time())
        return r

    # ------------------------------------------------------------------
    # K2450 사용권: 한 번에 한 기능만
    # ------------------------------------------------------------------
    def _k2450_acquire(self, who, stop_monitor=False):
        """K2450 사용권을 얻는다. 쓰는 쪽은 접촉 탐색 · 1회 측정 · 연속 측정 · 자동 측정 · 직접 명령 중 하나(USE_*)이고, 여러 번 읽는 작업 전체를 감싼다
        (장비 객체의 lock 은 명령 한 줄 · 읽기 한 번만 지킨다 - 그 사이에 다른 기능이 끼어 설정을 바꾸는 것은 못 막는다).
        다른 쪽이 쓰고 있으면 "저항계 사용 중 · <누가>" 로 거절한다. 연속 측정만은 한 회차가 짧으므로 끝나기를 기다렸다가 얻는다.
        stop_monitor = 연속 측정을 먼저 끈다(접촉 · 측정 시작)."""
        with self._k2450_cv:
            t_end = time.monotonic() + 2.0 * meters.VISA_TIMEOUT_MS / 1000.0     # 연속 측정의 한 회차(읽기 한 번)가 끝나기를 기다리는 한도
            while self._k2450_user == USE_MONITOR and time.monotonic() < t_end:
                self._k2450_cv.wait(0.05)
            if self._k2450_user is not None:
                raise Rejected("저항계 사용 중 · %s" % self._k2450_user)
            self._k2450_user = who
        if stop_monitor and self._monitor_on:                       # 사용권을 얻은 뒤에 끈다(거절됐는데 연속 측정만 꺼지는 일이 없게). 그 사이 루프는 사용권을 못 얻어 건너뛴다
            self._monitor_on = False
            self.state.set("monitor", False)
            self.logs.app("연속 측정 끔 · %s 시작" % who)

    def _k2450_try(self, who) -> bool:
        """기다리지 않고 사용권을 얻어 본다(연속 측정 루프용). 다른 쪽이 쓰고 있으면 False."""
        with self._k2450_cv:
            if self._k2450_user is not None:
                return False
            self._k2450_user = who
            return True

    def _k2450_release(self, who):
        with self._k2450_cv:
            if self._k2450_user == who:
                self._k2450_user = None
            self._k2450_cv.notify_all()

    def k2450_begin_run(self):
        """측정 시작 · 재개: 연속 측정을 끄고 K2450 사용권("자동 측정")을 얻는다. 다른 기능이 쓰는 중이면 거절한다."""
        self._k2450_acquire(USE_RUN, stop_monitor=True)

    def k2450_end_run(self):
        self._k2450_release(USE_RUN)

    @contextmanager
    def _use_k2450(self, who, stop_monitor=False):
        self._k2450_acquire(who, stop_monitor)
        try:
            yield
        finally:
            self._k2450_release(who)

    # ------------------------------------------------------------------
    # 측정 중 막기 · 경보 지우기
    # ------------------------------------------------------------------
    def run_active(self) -> bool:
        r = self.runner
        return bool(r is not None and r.active)

    def contact_busy(self) -> bool:
        """접촉 탐색이 K2450 을 쓰는 중."""
        with self._k2450_cv:
            return self._k2450_user == USE_CONTACT

    def _no_run(self, what: str):
        """측정 중에는 손으로 하는 이동 · 릴레이 · 측정을 막는다(비상정지와 읽기만 된다)."""
        if self.run_active():
            raise Rejected("측정 중 실행 불가")

    def result_open_folder(self, id: str):
        from app import results
        return results.open_folder(self.cfg.data_path(), id)

    def result_nas_copy(self, id: str):
        from app import results
        return results.nas_copy(self.cfg.data_path(), id)

    def alarm_clear(self, code: str):
        """[지우기] 를 누를 수 있는 알림(dismiss)만 지운다. 비상정지 · 장비 끊김 같은 경보는 원인이 풀려야 없어진다."""
        for a in self.state.get("alarms"):
            if a["code"] == code and a.get("dismiss"):
                self.state.clear_alarm(code)
                return {}
        raise Rejected("원인 해결 후 해제됨")

    # ------------------------------------------------------------------
    # 접촉: 고정 높이(기본 · contact_fixed) / 전기 탐색(선택 · app/contact.py - v2 방식)
    # ------------------------------------------------------------------
    def _need_k2450(self):
        dev = self.k2450
        if dev is None or not dev.is_open:
            raise Rejected("저항계 연결 안 됨")
        return dev

    def _k2450_off(self, dev):
        """K2450 출력 끄기(두 번까지). 못 끄면 경보 - 장비에서 직접 꺼야 한다."""
        for _ in range(2):
            try:
                dev.output_off_urgent(1.0)
                self.state.clear_alarm("k2450_off")
                return True
            except Exception as e:                                  # noqa: BLE001
                err = e
                time.sleep(0.2)
        self.state.alarm("k2450_off", "K2450 출력 끄기 실패 · 장비에서 직접 차단 필요", ALARM)
        self.logs.app("K2450 출력 끄기 실패 · %s" % err, "error")
        return False

    def contact_descend(self, mode: str, z_ref, g, lock: bool = True):
        """전기로 접촉을 확인하며 핀을 내린다("전기 탐색"). mode "height"(z_ref = 저장한 접촉 높이) · "search"(z_ref = 지금 Z).
        반환 {first_z, final_z, r2, r4}. 실패는 ContactFail(핀을 맨 위로 올리고 출력을 끈 뒤). 비상정지면 Rejected(핀은 그대로, 출력만 끔).
        부르는 쪽이 K2450 사용권을 갖고 있어야 한다(contact_find = "접촉 탐색" · 측정 작업 = "자동 측정")."""
        dev = self._need_k2450()
        self._need_stage()
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요 (초기 설정 2)")
        why = self.safety.reject_move(False, self._heater_on())
        if why:
            raise Rejected(why)
        p = contact_mod.params(self.cfg)
        one = 1.0 / PPMM
        z_start, floor = contact_mod.limits(mode, z_ref, p, self.cfg["z_max_mm"], one)
        pc = self.prep.contact

        def on_step(z, kind, ohm):
            names = {"open": "미접촉", "contact": "접촉", "short": "단락", "bad": "값 없음"}
            four = kind.startswith("4w:")
            k2 = kind.split(":")[1] if four else kind
            pc.update(z=round(z, 3), text=("4단자 " if four else "") + names.get(k2, k2) + (" %s" % _ohm(ohm) if ohm else ""))
            self.prep.publish()

        sense, st = contact_mod.make_sense(dev, p, mode, floor, one, on_step)
        pc.update(active=True, z=self._mm("z"), text="하강 중", result=None, fail="")
        self.prep.publish()
        fail = None
        try:
            with self._moving("접촉 탐색 중", {"x": self._mm("x"), "y": self._mm("y"), "z": round(floor, 3)}, lock=lock):
                try:
                    dev.configure_contact(p["volt"], p["ilim_a"], p["nplc"])
                    dev.output(True)
                except meters.VisaError as e:
                    raise contact_mod.ContactFail("저항계 설정 오류 · %s" % str(e)[:40], "inst")
                z_end = self._probe_down(g, z_start, floor, sense, st["max_down"])
            res = {"mode": mode, "first_z": round(st["first_z"], 4), "final_z": round(z_end, 4), "r2": st["r2"], "r4": st["r4"],
                   "x": self._mm("x"), "y": self._mm("y"), "t": time.time()}
            pc.update(result=res, text="샘플에 닿음")
            self.logs.app("접촉 찾기 · 첫 접촉 Z %.3f · 최종 Z %.3f · 2단자 %s · 4단자 %s" % (res["first_z"], res["final_z"], _ohm(res["r2"]), _ohm(res["r4"])))
            return res
        except contact_mod.ContactFail as e:
            fail = e
            pc.update(fail=contact_mod.FAIL_TEXT.get(e.code, str(e)), text=str(e))
            self.logs.app("접촉 찾기 실패 · %s" % e, "warn")
            raise
        finally:
            self._k2450_off(dev)                                    # 어떤 끝에서도 출력을 끈다
            if fail is not None and self.safety.gen_ok(g) and not self.safety.locked and self.stage.connected:
                try:                                                # 실패하면 핀을 맨 위로(비상정지면 움직이지 않는다)
                    with self._moving("핀 올리는 중", {"x": self._mm("x"), "y": self._mm("y"), "z": 0.0}, lock=lock):
                        self.stage.goto("z", 0, g)
                except Rejected as e2:
                    self.logs.app("접촉 찾기 실패 뒤 핀 올리기 실패 · %s" % e2, "error")
            pc["active"] = False
            self.prep.publish()

    @contextmanager
    def _approach(self, g, start_p):
        """핀을 start_p(펄스)까지 내린다: 마지막 approach_mm 는 핀 하강 속도(slow_speed_pps)로. with 블록 안은 그 느린 속도 그대로이고, 나올 때 속도를 되돌린다.
        핀이 이미 start_p 보다 낮으면 움직이지 않고 거절한다(올렸다 내리지 않는다)."""
        cur_p = self.stage.read_status(g)["pos"]["z"]
        if cur_p > start_p:
            raise Rejected("핀이 시작 높이 Z %.2f mm 보다 낮음 · 핀 상승 필요" % (start_p / PPMM))
        pre_p = max(cur_p, start_p - stage_mod.mm_to_pulse(self.cfg["approach_mm"]))
        if pre_p > cur_p:
            self.stage.goto("z", pre_p, g)
        normal = int(self.cfg["move_speed_pps"])
        try:
            self.stage.set_v(min(normal, int(self.cfg["slow_speed_pps"])), g)
            if start_p > pre_p:
                self.stage.goto("z", start_p, g)
            yield
        finally:
            if self.stage.connected:
                try:
                    self.stage.set_v(normal, None)                  # 속도를 되돌린다(비상정지 뒤에도 - 이동 명령이 아니다)
                except StageError as e:
                    self.logs.app("이동 속도 되돌리기 실패 · %s" % e, "warn")

    def _probe_down(self, g, z_start, floor, sense, max_down):
        """z_start 까지 내린 뒤(마지막 approach_mm 는 느리게) 멈출 때마다 sense(z) 가 정한 만큼 움직인다. 반환: 마지막 Z(mm).
        이동은 펄스 단위 절대 이동(gz)이라 간격이 흔들리지 않는다. 걸음마다 두 가지를 직접 확인한다(sense 가 이미 지키지만 한 번 더):
          · 아래로 가는 한 걸음이 지금 단계의 간격 max_down() 을 넘지 않는다 - 넘는 요청은 움직이지 않고 실패(ContactFail "step")
          · 한도(floor) 아래로는 절대 내리지 않는다"""
        floor_p = int(float(floor) * PPMM + 1e-6)
        start_p = stage_mod.mm_to_pulse(z_start)
        if not 0 < start_p <= floor_p:
            raise Rejected("접촉 탐색 범위 오류 · Z %.2f ~ %.2f mm" % (z_start, floor))
        with self._approach(g, start_p):
            zp = start_p
            while True:
                dz = sense(zp / PPMM)
                if not self.safety.gen_ok(g):                       # 재는 동안 비상정지 · 이동 정지가 왔으면 다음 걸음을 보내지 않는다
                    raise stage_mod.Discarded("정지로 취소됨")
                if dz is None:
                    return zp / PPMM
                dp = stage_mod.mm_to_pulse(dz)
                if dp == 0:
                    continue
                if dp > stage_mod.mm_to_pulse(max_down()):
                    raise contact_mod.ContactFail("하강 간격 초과 · %.3f mm 요청" % dz, "step")
                np_ = max(0, zp + dp)
                if np_ > floor_p:
                    raise contact_mod.ContactFail("미접촉 · Z %.2f mm" % (floor_p / PPMM), "none")
                self.stage.goto("z", np_, g)
                zp = np_

    def contact_fixed(self, z_mm, g, read: bool = True, meter=None, lock: bool = True):
        """고정 높이 접촉(접촉 방식의 기본): Z 를 저장한 접촉 높이까지 정확히 내리고 끝낸다. 전기 판단으로 더 내리거나 실패시키지 않는다.
        read = 내린 뒤 K2450 으로 저항을 한 번 재서 기록만 한다(판정 없음 - 못 재도 실패로 만들지 않는다). meter = 그때 쓸 측정 조건.
        반환 {mode "fixed", z, ohm, current, tripped, four_wire, volt}. 부르는 쪽(측정 작업)이 K2450 사용권을 갖고 있어야 한다."""
        self._need_stage()
        if z_mm is None:
            raise Rejected("접촉 높이 필요 (초기 설정 6)")
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요 (초기 설정 2)")
        why = self.safety.reject_move(False, self._heater_on()) or self.safety.reject_target("z", float(z_mm), True)
        if why:
            raise Rejected(why)
        target_p = stage_mod.mm_to_pulse(z_mm)
        with self._moving("접촉 높이로 하강 중", {"x": self._mm("x"), "y": self._mm("y"), "z": round(target_p / PPMM, 3)}, lock=lock):
            with self._approach(g, target_p):
                pass
        res = {"mode": "fixed", "z": round(target_p / PPMM, 4), "ohm": None, "current": None, "tripped": None, "four_wire": None, "volt": None}
        dev = self.k2450
        if read and dev is not None and dev.is_open:
            try:
                r = self._measure_r(dev, meter)
                res.update(ohm=r["ohm"], current=r["current"], tripped=r["tripped"], four_wire=r["four_wire"], volt=r["volt"])
            except Rejected as e:                                   # 기록용 읽기다 - 못 재도 접촉 단계를 실패로 만들지 않는다(장비가 고장이면 가열 단계가 멈춘다)
                self.logs.app("접촉 후 저항 기록 실패 · %s" % e, "warn")
        self.logs.app("접촉 · 고정 높이 Z %.3f%s" % (res["z"], " · %s" % _ohm(res["ohm"]) if res["ohm"] else ""))
        return res

    def contact_find(self):
        """초기 설정 6 · 수동 제어 탭 [접촉 탐색]: 지금 높이부터 탐색 간격씩 내리며 찾는다. 접촉 방식이 전기 탐색일 때만 쓴다.
        연속 측정을 먼저 끄고 K2450 사용권을 얻는다(탐색 도중 다른 기능이 K2450 설정을 바꾸면 "안 닿음" 으로 읽고 한도까지 내려간다)."""
        self._no_run("접촉 탐색")
        if self.cfg["contact_mode"] != "search":
            raise Rejected("접촉 방식 고정 높이 · 전기 탐색 선택 필요")
        self._need_k2450()
        g = self.safety.generation
        with self._use_k2450(USE_CONTACT, stop_monitor=True):
            try:
                res = self.contact_descend("search", self._mm("z") or 0.0, g)
            except contact_mod.ContactFail as e:
                raise Rejected("%s · 핀 상승 완료" % e)
        return {k: res[k] for k in ("first_z", "final_z", "r2", "r4")}

    def contact_save(self):
        """[높이 저장]: 마지막으로 찾은 첫 접촉 높이를 접촉 높이(contact_z_mm)로 저장한다(전기 탐색)."""
        self._no_run("접촉 높이 저장")
        res = self.prep.contact.get("result")
        if not res:
            raise Rejected("[접촉 탐색] 필요")
        self._remember(contact_z_mm=round(float(res["first_z"]), 3))
        self.calib.set_contact({"r2": res["r2"], "r4": res["r4"], "how": "접촉 탐색"})
        self.prep.publish()
        return {"contact_z_mm": self.cfg["contact_z_mm"]}

    def contact_save_here(self):
        """[현재 높이로 저장]: 지금 Z 를 접촉 높이(contact_z_mm)로 저장한다(how = "수동"). 핀을 손으로 조금씩 내려 샘플에 댄 뒤 누른다."""
        self._no_run("접촉 높이 저장")
        self._need_stage()
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요 (초기 설정 2)")
        if self.stage.moving or self.state.get("move") is not None:
            raise Rejected("이동 중")
        try:
            z = self.stage.read_status(None)["pos"]["z"] / PPMM    # 화면 값이 아니라 지금 st 로 읽은 위치
        except StageError as e:
            raise Rejected("위치 읽기 실패 · %s" % e)
        if z <= 0:
            raise Rejected("핀이 맨 위 · 샘플에 닿는 높이로 내린 후 저장")
        self._remember(contact_z_mm=round(z, 3))
        self.calib.set_contact({"r2": None, "r4": None, "how": "수동"})
        self.prep.publish()
        self.logs.app("접촉 높이 저장 · 수동 · Z %.3f" % z)
        return {"contact_z_mm": self.cfg["contact_z_mm"]}

    def raw(self, target: str, line: str):
        """직접 명령. 스테이지는 허용 목록(읽기 전용 - STAGE_RAW_OK)만, 측정 장비는 조회(…?)만 보낸다. '!' 는 비상정지."""
        line = str(line).strip()
        if not line:
            raise Rejected("명령 입력 필요")
        if target == "stage":
            self._need_stage()
            tok = line.split()
            if tok[0] == "!":
                self.estop("직접 명령")
                return {"reply": "비상정지"}
            if len(tok) != 1 or tok[0].lower() not in STAGE_RAW_OK:
                raise Rejected(RAW_STAGE_NO)
            try:
                lines = self.stage.raw(tok[0].lower(), None)        # 읽기 전용이라 세대와 무관하다(비상정지 중에도 된다)
            except StageError as e:
                raise Rejected(str(e))
            return {"reply": "\n".join(lines)}
        if target in ("k2110", "k2450"):
            dev = getattr(self, target)
            if dev is None or not dev.is_open:
                raise Rejected("장비 연결 안 됨")
            if not meter_raw_ok(target, line):
                raise Rejected(RAW_METER_NO)
            try:
                if target == "k2450":                               # 접촉 탐색 · 측정이 쓰는 중에는 조회도 끼어들지 않는다
                    with self._use_k2450(USE_RAW):
                        return {"reply": dev.raw(line)}
                return {"reply": dev.raw(line)}
            except meters.VisaError as e:
                raise Rejected(str(e))
        raise Rejected("대상은 stage · k2110 · k2450 중 하나")

    # ------------------------------------------------------------------
    # 카메라 · 설정 공개 · 자동 연결 · 계속 읽기
    # ------------------------------------------------------------------
    def connect_camera(self, index: Optional[int] = None):
        if self.camera.connected:
            raise Rejected("카메라 이미 연결됨")
        c = self.cfg
        idx = c["camera_index"] if index is None else int(index)
        self.state.set_device("camera", "연결 중", "", str(idx))
        try:
            with self._cam_lock:                                    # 목록용 열어 보기가 같은 번호를 잡고 있으면 끝날 때까지 기다린다
                info = self.camera.open(idx, c["camera_width"], c["camera_height"], c["camera_fourcc"], c["camera_focus_fixed"],
                                        c["camera_focus_value"], c["preview_width"],
                                        exposure=c["camera_exposure"] if c["camera_exposure_fixed"] else None,
                                        wb_k=c["camera_wb_k"] if c["camera_wb_fixed"] else None)
        except CameraError as e:
            self.state.set_device("camera", "오류", str(e)[:80], str(idx))
            self.logs.app("카메라 연결 실패 · %s" % e, "warn")
            raise Rejected(str(e))
        text = "%d×%d %s" % (info["actual"][0], info["actual"][1], info["actual_fourcc"] or "?")
        self.state.set_device("camera", "연결", "", str(idx), text)
        self._remember(camera_index=idx)
        mm = self.camera.mismatch()
        if mm:
            self.state.alarm("camera_format", mm, NOTICE)
            self.logs.app(mm, "warn")
        else:
            self.state.clear_alarm("camera_format")
        self.state.clear_alarm("camera_lost")
        return {"index": idx, "actual": info["actual"], "fourcc": info["actual_fourcc"]}

    def disconnect_camera(self):
        self.camera.close()
        self.state.set_device("camera", "끊김", "", "")
        self.state.clear_alarm("camera_format")

    def _camera_event(self, kind, text):
        if kind == "lost":
            self.state.set_device("camera", "오류", "프레임 수신 실패 · USB 확인 필요", str(self.cfg["camera_index"]))
            self.state.alarm("camera_lost", "카메라 연결 끊김 · USB 확인 필요", NOTICE)
            self.logs.app("카메라 끊김 · %s" % text, "warn")

    def set_folder_picker(self, fn):
        """폴더 선택 창을 띄우는 함수(시작 폴더 → 고른 경로 또는 None)를 받는다. 창으로 실행했을 때만 있다(--no-window 에는 없다)."""
        self._folder_picker = fn
        self.state.set("can_pick_folder", fn is not None)

    def pick_folder(self, start: str = ""):
        """[찾아보기]: 이 컴퓨터의 폴더 선택 창을 띄우고 고른 경로를 돌려준다. 취소하면 path = None. 설정은 바꾸지 않는다(화면이 입력 칸에 넣는다)."""
        fn = self._folder_picker
        if fn is None:
            raise Rejected("폴더 선택 창 사용 불가")
        start = str(start or "").strip()
        if start and not (ntpath.isabs(start) or os.path.isabs(start)):   # 상대 경로(데이터 폴더 기본값)는 프로그램 폴더 기준. D:\… · \\NAS\… 는 그대로
            start = str((config_mod.ROOT / start).resolve())
        try:
            path = fn(start)
        except Exception as e:                                      # noqa: BLE001
            self.logs.app("폴더 선택 창 오류 · %s: %s" % (type(e).__name__, e), "error")
            raise Rejected("폴더 선택 창 열기 실패")
        return {"path": str(path) if path else None}

    def update_settings(self, patch: dict):
        """설정 바꾸기(전체 설정 · 조건 창 - PUT /api/settings). 측정 중에는 거절한다(측정이 쓰는 값이 도중에 바뀌지 않게).
        값 하나하나의 검증(ConfigError) + 조합 검사(config.problems)를 통과해야 저장한다. 조합 검사는 "이번 변경으로 새로 생기는 문제" 만 거절한다 -
        예전 파일에 이미 있던 문제 때문에 다른 값을 못 고치는 일이 없게(그 문제는 측정 시작이 막는다)."""
        if self.run_active():
            raise Rejected("측정 중 실행 불가")
        if self.temptest.active:
            raise Rejected("온도 시험 중 실행 불가")                  # 시험이 쓰는 값 · 릴레이 역할이 도중에 바뀌지 않게
        old = dict(self.cfg.values)
        new = self.cfg.checked(patch)
        roles_changed = [k for k in config_mod.ROLE_KEYS if k in new and new[k] != old[k]]
        if roles_changed and (any(r for r in self.state.get("relays")) or self._heater_starting):
            raise Rejected("릴레이 모두 끈 뒤 변경")                 # 켜진 릴레이의 뜻(송풍 · 열선 ↔ 쿨러)이 도중에 바뀌면 안전 감시가 엉뚱한 릴레이를 본다
        allp = lambda v: config_mod.problems(v) + config_mod.temptest_problems(v)     # noqa: E731
        before = set(allp(old))
        bad = [t for t in allp(dict(old, **new)) if t not in before]
        if bad:
            raise Rejected(bad[0])
        self.cfg.values.update(new)
        self.cfg.save()
        self.publish_cfg()
        self.prep.on_settings()
        if roles_changed:
            self.logs.app("릴레이 역할 변경 · %s" % " · ".join("%s %s → %s" % (config_mod.BY_KEY[k].label, old[k], new[k]) for k in roles_changed))
            if self.stage.connected:
                try:
                    self.stage.read_relays()                        # rs 를 다시 읽어 안전 감시가 새 번호의 릴레이를 기준으로 보게 한다
                except StageError as e:
                    self.logs.app("릴레이 상태 읽기 실패 · %s" % e, "warn")
            self._heater_sync()
        return {}

    def _remember(self, **kw):
        """연결에 성공한 포트 · 주소 · 번호를 settings 에 저장한다(자동 연결이 쓴다). 저장 실패는 연결에 영향 없다."""
        try:
            self.cfg.update(kw)
            self.cfg.save()
        except (config_mod.ConfigError, OSError) as e:
            self.logs.app("설정 저장 실패 · %s" % e, "warn")
        self.publish_cfg()

    def gate_inputs_changed(self):
        """측정 시작 조건의 입력이 바뀌었다: 화면용 gate 를 그 순간 다시 계산한다(1초 주기를 기다리지 않는다).
        바꾼 쪽 스레드(스테이지 일꾼 포함)에서 불리므로 가볍게 - gate 만 계산한다. 화면용 값 계산의 오류가 장비 스레드를 멈추지 않게 로그에 남기고 계속한다."""
        try:
            self.runner.refresh_gate()
        except Exception as e:                                      # noqa: BLE001 - 다음 1초 주기 publish 가 다시 계산한다
            self.logs.app("측정 시작 조건 계산 오류 · %s: %s" % (type(e).__name__, e), "error")

    def publish_cfg(self):
        """설정 값을 state 로 내보낸다(설정이 바뀔 때마다). 화면은 이것만 본다."""
        self.state.set("cfg", dict(self.cfg.values))

    def auto_connect(self):
        """저장된 포트 · 주소 하나씩만 연다(찾아다니지 않는다). 실패해도 그 장비만 오류로 보이고 계속 간다."""
        if not self.cfg["auto_connect"]:
            return
        steps = (("스테이지", self.connect_stage), ("온도계", lambda: self.connect_meter("k2110")),
                 ("저항계", lambda: self.connect_meter("k2450")), ("카메라", self.connect_camera))
        for name, fn in steps:
            try:
                fn()
            except Rejected as e:
                self.logs.app("자동 연결 실패 · %s · %s" % (name, e), "warn")
            except Exception as e:                                  # noqa: BLE001 - 한 장비의 예상 밖 오류가 다른 장비 연결을 막지 않는다
                self.logs.app("자동 연결 오류 · %s · %s: %s" % (name, type(e).__name__, e), "error")
                self.state.set_device({"스테이지": "stage", "온도계": "k2110", "저항계": "k2450", "카메라": "camera"}[name], "오류", str(e)[:60])

    def monitor(self, on: bool):
        """1 초마다 계속 읽기: 온도 · 저항을 state 로 보낸다. 비상정지면 멈춘다."""
        on = bool(on)
        if on:
            if self.safety.locked:
                raise Rejected("비상정지 상태")
            if self.k2450 is None or not self.k2450.is_open:         # 연속 측정은 저항을 1 초마다 잰다(온도는 따로 늘 읽는다)
                raise Rejected("저항계 연결 필요")
            self._no_run("연속 측정")                               # 측정 작업 중에는 켜지 않는다(측정 시작이 꺼 둔다)
        self._monitor_on = on
        self.state.set("monitor", on)
        return {"monitor": on}

    def _monitor_loop(self):
        while self._alive:
            t0 = time.monotonic()
            try:
                self.prep.publish()                                 # 장비 연결 · 원점이 바뀌면 초기 설정 단계 한 줄도 바뀐다(같으면 아무 일도 없다)
                self.runner.publish()
            except Exception as e:                                  # noqa: BLE001 - 화면용 요약이 감시 루프를 죽이지 않는다
                self.logs.app("초기 설정 요약 오류 · %s: %s" % (type(e).__name__, e), "error")
            if self._monitor_on:
                if self.safety.locked:
                    self._monitor_on = False
                    self.state.set("monitor", False)
                elif self.k2450 is not None and self.k2450.is_open and self._k2450_try(USE_MONITOR):
                    try:                                            # 사용권을 못 얻으면(다른 기능이 K2450 을 쓰는 중) 이 회차는 건너뛴다 - 오류로 끄지 않는다
                        self._measure_r(self.k2450)
                    except Rejected as e:
                        self._monitor_on = False
                        self.state.set("monitor", False)
                        self.state.alarm("monitor", "연속 측정 중지 · %s" % e, NOTICE)
                    finally:
                        self._k2450_release(USE_MONITOR)
            time.sleep(max(0.05, 1.0 - (time.monotonic() - t0)))

    # ------------------------------------------------------------------
    # 감시 스레드 · 종료
    # ------------------------------------------------------------------
    def _start_threads(self):
        for fn, name in ((self._watch_loop, "heater-watch"), (self._temp_loop, "temp-poll"), (self._monitor_loop, "monitor")):
            t = threading.Thread(target=fn, daemon=True, name=name)
            t.start()
            self._threads.append(t)

    def _watch_loop(self):
        while self._alive:
            try:
                self.check_heater()
                self.safety.update_hold()                           # 비상정지 직후의 해제 대기가 끝났는지
            except Exception as e:                                  # noqa: BLE001 - 감시는 죽지 않는다. 기록하고 계속
                self.logs.app("히트건 감시 오류 · %s" % e, "error")
            time.sleep(0.25)

    def _temp_loop(self):
        while self._alive:
            dev = self.k2110
            if dev is not None and dev.is_open and not self.runner.feeding and not self.temptest.feeding:      # 측정 · 온도 시험이 읽는 동안에는 따로 읽지 않는다(그 값을 넘겨 준다)
                try:
                    self.feed_temperature(dev.read_temp())
                    if self.state.get("devices", "k2110", "state") == "오류":
                        self.state.set_device("k2110", "연결", "", dev.addr)
                except meters.VisaError as e:
                    self.state.set_device("k2110", "오류", str(e), dev.addr)
            time.sleep(1.0)

    def run_command(self, name: str, args: Optional[dict] = None):
        fn = COMMANDS.get(name)
        if fn is None:
            raise Rejected("알 수 없는 명령: %s" % name)
        if self.run_active() and name not in RUN_ALLOWED:           # 측정 중에는 손으로 하는 이동 · 릴레이 · 측정을 막는다(측정은 명령이 아니라 메서드를 직접 부른다)
            if not (name == "raw" and (args or {}).get("target") == "stage" and str((args or {}).get("line", "")).strip().split(" ")[0].lower() in ("st", "rs", "p")):
                raise Rejected("측정 중 실행 불가")
        if self.temptest.active and name in TEMPTEST_BLOCKED:        # 온도 시험이 히트건 · 쿨러 · 안전 감시를 쓰는 중
            raise Rejected("온도 시험 중 실행 불가")
        obj = self
        if "." in fn:                                               # "prep.photo_take" → self.prep.photo_take
            owner, fn = fn.split(".", 1)
            obj = getattr(self, owner)
            if obj is None:
                raise Rejected("사용할 수 없는 명령: %s" % name)
        method = getattr(obj, fn)
        try:
            import inspect
            inspect.signature(method).bind(**(args or {}))         # 값이 안 맞으면 명령을 실행하기 전에 거절한다
        except TypeError as e:
            raise Rejected("명령 값 오류 · %s" % str(e)[:60])
        return method(**(args or {}))

    def shutdown(self):
        """닫을 때: 히트건 · 쿨러 끄기 → K2450 출력 끄기 → 연결 닫기. 여러 번 불러도 한 번만 한다."""
        with self._shut_lock:
            if self._shut:
                return
            self._shut = True
        if self.runner.active:                                      # 측정 중에 닫으면 측정부터 멈춘다(정리: 히트건 · 출력 · 쿨러 끔 → 핀 맨 위 · 파일 닫기)
            self.runner._stop = True
            self.runner.join(timeout=20)
        if self.temptest.active:                                    # 온도 시험도 멈춘다(정리 · 기록 저장)
            self.temptest._stop = True
            self.temptest.join(timeout=20)
        self._alive = False
        self._heater_why = "종료"
        try:
            if self.stage.connected:
                self.stage.relays_off(None, prio=0)
        except Exception as e:                                      # noqa: BLE001
            self.logs.app("종료 중 릴레이 끄기 실패 · %s" % e, "error")
        finally:
            self._heater_why = None
        for kind in ("k2450", "k2110"):
            self._close_meter(kind)
        self.camera.close()
        self.stage.disconnect()
        self.logs.app("종료")


COMMANDS = {
    "connect_stage": "connect_stage", "disconnect_stage": "disconnect_stage",
    "connect_meter": "connect_meter", "disconnect_meter": "disconnect_meter", "devices_refresh": "refresh_devices", "pick_folder": "pick_folder",
    "connect_camera": "connect_camera", "disconnect_camera": "disconnect_camera", "monitor": "monitor",
    "jog": "jog", "goto": "goto", "z_top": "z_top", "set_origin": "set_origin", "motion_stop": "motion_stop",
    "relay_on": "relay_on", "relay_off": "relay_off", "relays_off": "relays_off",
    "heater_on": "manual_heater_on", "heater_off": "manual_heater_off", "heater_element": "manual_heater_element",
    "read_temp": "read_temp", "read_resistance": "read_resistance", "raw": "raw",
    "estop_release": "estop_release", "alarm_clear": "alarm_clear",
    "contact_find": "contact_find", "contact_save": "contact_save", "contact_save_here": "contact_save_here",
    # 초기 설정 단계 · 사진 · 좌표 · 샘플(app/prep.py)
    "photo_head_set": "prep.photo_head_set", "marker_check": "prep.marker_check", "photo_take": "prep.photo_take", "plate_set": "prep.plate_set",
    "empty_take": "prep.empty_take", "coord_set": "prep.coord_set", "coord_goto_d": "prep.coord_goto_d", "coord_clear": "prep.coord_clear",
    "marker_learn": "prep.marker_learn", "plate_goto": "prep.plate_goto", "coord_add_here": "prep.coord_add_here", "coord_remove": "prep.coord_remove",
    "samples_find": "prep.samples_find", "sample_use": "prep.sample_use",
    "cooler_only": "prep.cooler_only", "cooler_save": "prep.cooler_save",
    "sample_rename": "prep.sample_rename", "batch_set": "prep.batch_set",
    # 측정(app/jobs/run.py)
    "run_start": "runner.start", "run_resume": "runner.resume", "run_stop": "runner.stop", "run_skip": "runner.skip",
    "run_pause_after": "runner.set_pause_after", "run_requeue": "runner.requeue",
    "result_open_folder": "result_open_folder", "result_nas_copy": "result_nas_copy",
    # 온도 시험(app/jobs/temptest.py)
    "temptest_start": "temptest.start", "temptest_stop": "temptest.stop", "temptest_open_folder": "temptest.open_folder",
}

# 온도 시험 중에 거절하는 명령: 자동 측정 · 접촉 탐색 · 쿨러 [켜 보기] · 히트건 수동 버튼 · 개별 릴레이 버튼(시험이 히트건 · 쿨러를 쓴다).
# [전체 끄기] 는 시험을 [정지] 한 뒤 끈다(끄기는 언제나 된다). 비상정지는 명령 줄을 거치지 않는다.
TEMPTEST_BLOCKED = {"run_start", "run_resume", "contact_find", "cooler_only", "heater_on", "heater_off", "heater_element", "relay_on", "relay_off"}


# 측정 중에도 되는 명령(읽기 · 측정 조작 · 경보 지우기). 그 밖의 명령은 측정 중에 거절한다. 비상정지는 명령 줄을 거치지 않는다.
RUN_ALLOWED = {"estop_release", "alarm_clear", "motion_stop", "run_start", "run_stop", "run_skip", "run_pause_after", "run_resume", "run_requeue",
               "sample_rename", "batch_set", "result_open_folder", "result_nas_copy"}


def _ohm(v):
    if v is None:
        return "—"
    a = abs(float(v))
    return "%.3g MΩ" % (v / 1e6) if a >= 1e6 else "%.3g kΩ" % (v / 1e3) if a >= 1e3 else "%.3g Ω" % v

__all__ = ["Controller", "COMMANDS", "Rejected", "config_mod"]
