"""control.py - 명령을 받아 안전 규칙을 지나 장비에 보낸다. 서버와 시험이 같은 입구(run_command)를 쓴다.

거절은 Rejected(쉬운 한국어 한 문장). 이동은 한 번에 하나만(이동 잠금) - 여러 단계 이동(Z 올리기 → X · Y)이 다른 이동과 섞이지 않게 한다.
"""

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
from app.prep import Prep
from app.safety import BY_MANUAL, BY_RUN, TRIP_MAX_C, TRIP_MAX_ON, TRIP_NO_TEMP, Rejected, Safety
from app.state import ALARM, NOTICE, State

AXES = "xyz"
MOTION_WORDS = ("g", "j", "m", "fz", "z", "sp", "home", "x", "y", "rx", "ry", "fx", "fy")   # 직접 명령에서 막는 이동 · 원점 명령


def serial_ports():
    """시리얼 포트 이름만(설명 · 장비 종류는 보여 주지 않는다). 여는 것은 사람이 고른 하나뿐이다."""
    try:
        from serial.tools import list_ports
        return sorted(p.device for p in list_ports.comports())
    except Exception:                                               # noqa: BLE001
        return []


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
        self.heater_trip = ""                                       # 안전 규칙이 히트건을 마지막으로 끈 이유
        self._heater_by = None                                      # 지금 보내는 "히트건 켜기"의 주체(수동 · 측정) - 릴레이 상태가 읽히는 순간에 쓴다
        self._heater_why = None                                     # 지금 보내는 "히트건 끄기"의 까닭(로그용)
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
        self.publish_cfg()
        self.prep.publish()
        self.runner.publish()
        self._start_threads()

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------
    def list_devices(self):
        visa, note = meters.list_resources()
        return {"serial_ports": serial_ports(), "visa": visa, "note": note, "cameras": [0, 1, 2, 3]}

    def connect_stage(self, port: Optional[str] = None):
        port = (port or self.cfg["stage_port"]).strip()
        if not port:
            raise Rejected("스테이지 포트 선택 필요")
        if self.stage.connected:
            raise Rejected("스테이지 이미 연결됨")
        self.state.set_device("stage", "연결 중", "", port)
        try:
            speed = (self.cfg["move_speed_pps"], self.cfg["move_accel_pps2"], self.cfg["move_start_pps"])
            self.stage.connect(port, speed, self.cfg["watchdog_ms"])
        except StageError as e:
            self.state.set_device("stage", "오류", str(e), port)
            self.logs.app("스테이지 연결 실패 · %s" % e, "warn")
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
        self._heater_bookkeeping(False, "스테이지 연결 끊기")
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
        h = s.relays[int(self.cfg["heater_relay"]) - 1]
        if h is not None:
            self._heater_bookkeeping(bool(h))

    def _heater_bookkeeping(self, on: bool, why: Optional[str] = None):
        """히트건 릴레이가 켜지거나 꺼진 것을 안전 감시 · 화면 상태에 반영하고, 프로그램 로그에 한 줄 남긴다.
        켠 주체(수동 · 측정)와 끈 까닭(수동 · 측정 · 차단 온도 · 최대 켜짐 시간 · 온도 못 읽음 · 비상정지 …)을 같이 적는다."""
        if on and self.safety.heater_on_t is None:
            by = self._heater_by
            self.safety.heater_turned_on(by or BY_RUN)              # 누가 켰는지 모르면 측정 쪽 규칙(더 엄한 쪽)으로 본다
            self.state.set("heater_on_since", time.time())
            self.logs.app("히트건 켬 · %s" % (by or "릴레이 켜짐 감지"))
        elif not on and self.safety.heater_on_t is not None:
            self.safety.heater_turned_off()
            self.state.set("heater_on_since", None)
            why = why or self._heater_why or "릴레이 꺼짐 감지"
            self.logs.app("히트건 끔 · %s" % why, "error" if why.startswith((TRIP_MAX_C, TRIP_NO_TEMP, TRIP_MAX_ON)) else "info")

    def _heater_on(self) -> bool:
        return self.safety.heater_on_t is not None

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
            self._heater_bookkeeping(False, "통신 끊김")
            self.logs.app("스테이지 통신 끊김 · %s" % text, "error")

    def _board_reboot(self, text):
        """보드가 다시 시작됐다: 비상정지와 같이 잠그고, wd 를 다시 보내고, 위치 확인 경보를 낸다."""
        self.safety.begin_estop()
        self.state.alarm("reboot", "보드 재시작 · 위치 확인 필요", ALARM)
        self.logs.app("보드 재시작 감지 · %s" % text, "error")
        self._heater_bookkeeping(False, "보드 재시작")
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
        self.state.set("heater_on_since", None)
        if heater_was_on:
            self.logs.app("히트건 끔 · 비상정지")
        self.runner.on_estop()                                      # 측정도 멈춘다(정리는 측정 스레드가: 히트건 · 출력 · 쿨러 끔, 핀은 그대로)
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
        self._heater_bookkeeping(False)
        dev = self.k2450
        if dev is not None:
            try:
                dev.output_off_urgent(1.0)
            except Exception as e:                                  # noqa: BLE001
                self.state.alarm("estop_k2450", "K2450 출력 끄기 실패 · 장비에서 직접 차단 필요", ALARM)
                self.logs.app("비상정지 뒤 K2450 출력 끄기 실패 · %s" % e, "error")
        return {"locked": True}

    def estop_release(self):
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
            raise Rejected("%s 원점 필요 (준비 2)" % " · ".join(miss))
        for ax, v in (("x", x), ("y", y)) + ((("z", z),) if z is not None else ()):
            why = self.safety.reject_target(ax, v, True)
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
            raise Rejected("Z 원점 필요 (준비 2)")
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
        self.calib.set_origin(axis, mode)                           # 등록한 때 · 방법(준비 2단계 목록에 보인다)
        self.prep.clear_pos_check()                                 # 원점을 다시 맞추면 위치 확인 경보가 풀린다
        self.prep.publish()
        return {"homed": dict(self.state.get("homed"))}

    # ------------------------------------------------------------------
    # 릴레이
    # ------------------------------------------------------------------
    def relay_on(self, n: int):
        """릴레이 켜기 버튼(수동 탭 · 준비 탭). 히트건이면 "수동" 규칙: 온도계 조건을 보지 않는다."""
        return self._relay_on(n, BY_MANUAL)

    def relay_off(self, n: int):
        return self._relay_off(n, BY_MANUAL)

    def _relay_on(self, n: int, by: str):
        """by = 켜는 주체(BY_MANUAL 수동 · BY_RUN 측정). 히트건(R1)일 때만 뜻이 있다."""
        n = self._relay_no(n)
        self._need_stage()
        heater = n == int(self.cfg["heater_relay"])
        if heater:
            why = self.safety.reject_heater_on(by, self.k2110 is not None and self.k2110.is_open)
        else:
            why = self.safety.reject_relay_on()
        if why:
            raise Rejected(why)
        g = self.safety.generation
        if heater:
            self._heater_by = by                                    # 릴레이 응답이 읽히는 순간(_stage_status)에 주체를 알 수 있게 먼저 적는다
        try:
            self.stage.relay(n, True, g)
        except stage_mod.Discarded:
            raise Rejected(self._cancel_text())
        except StageError as e:
            raise Rejected("릴레이 켜기 실패 · %s" % e)
        finally:
            if heater:
                self._heater_by = None
        return {"relays": list(self.state.get("relays"))}

    def _relay_off(self, n: int, by: str):
        n = self._relay_no(n)
        self._need_stage()
        heater = n == int(self.cfg["heater_relay"])
        if heater:
            self._heater_why = by                                   # 로그: "히트건 끔 · 수동" / "히트건 끔 · 측정"
        try:
            self.stage.relay(n, False, None)                        # 끄기는 비상정지 뒤에도 된다
        except StageError as e:
            raise Rejected("릴레이 끄기 실패 · %s" % e)
        finally:
            if heater:
                self._heater_why = None
        return {"relays": list(self.state.get("relays"))}

    def relays_off(self):
        self._need_stage()
        self._heater_why = BY_MANUAL
        try:
            self.stage.relays_off(None, prio=1)
        except StageError as e:
            raise Rejected("릴레이 끄기 실패 · %s" % e)
        finally:
            self._heater_why = None
        return {"relays": list(self.state.get("relays"))}

    @staticmethod
    def _relay_no(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise Rejected("릴레이 번호는 1~10")
        if not 1 <= n <= 10:
            raise Rejected("릴레이 번호는 1~10")
        return n

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
        trip = self.safety.heater_trip(now)
        if trip:
            self._heater_off(*trip)

    def _heater_off(self, cause: str, text: str):
        """안전 규칙이 히트건을 끈다. cause = 차단 온도 · 온도 못 읽음 · 최대 켜짐 시간(로그), text = 경보 문구."""
        self.heater_trip = text
        self.state.alarm("heater", text, ALARM, dismiss=True)
        heater = int(self.cfg["heater_relay"])
        why = "%s (켠 주체 %s)" % (cause, self.safety.heater_by or "?")
        self._heater_why = why
        try:
            if self.stage.connected:
                self.stage.relay(heater, False, None, prio=0)
        except StageError as e:
            self.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
            self.logs.app("히트건 끄기 실패 · %s" % e, "error")
        finally:
            self._heater_why = None
        self._heater_bookkeeping(False, why)

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
        dev = self.k2450
        if dev is None or not dev.is_open:
            raise Rejected("저항계 연결 안 됨")
        self._no_run("저항 측정")                                   # 측정이 K2450 을 쓰는 중이다(설정을 바꾸면 안 된다)
        c = self.cfg
        try:
            r = dev.measure_once(c["meter_volt"], c["meter_ilim_a"], c["meter_four_wire"], c["meter_nplc"], c["meter_avg_count"])
        except meters.VisaError as e:
            self.state.set_device("k2450", "오류", str(e), dev.addr)
            raise Rejected(str(e))
        self.state.update("resistance", ohm=r["ohm"], current=r["current"], volt=r["volt"], four_wire=r["four_wire"], time=time.time())
        return r

    # ------------------------------------------------------------------
    # 측정 중 막기 · 경보 지우기
    # ------------------------------------------------------------------
    def run_active(self) -> bool:
        r = self.runner
        return bool(r is not None and r.active)

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
    # 전기로 접촉 찾기 (app/contact.py - v2 방식 그대로)
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
        """전기로 접촉을 확인하며 핀을 내린다. mode "height"(z_ref = 저장한 접촉 높이) · "search"(z_ref = 지금 Z).
        반환 {first_z, final_z, r2, r4}. 실패는 ContactFail(핀을 맨 위로 올리고 출력을 끈 뒤). 비상정지면 Rejected(핀은 그대로, 출력만 끔)."""
        dev = self._need_k2450()
        self._need_stage()
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점 필요 (준비 2)")
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
                z_end = self._probe_down(g, z_start, floor, sense)
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

    def _probe_down(self, g, z_start, floor, sense):
        """z_start 까지 내린 뒤(마지막 approach_mm 는 느리게) 멈출 때마다 sense(z) 가 정한 만큼 움직인다. 한도(floor) 아래로는 내리지 않는다.
        이동은 펄스 단위 절대 이동(gz)이라 0.025 mm(4 펄스) 간격이 흔들리지 않는다. 반환: 마지막 Z(mm)."""
        floor_p = int(float(floor) * PPMM + 1e-6)
        start_p = stage_mod.mm_to_pulse(z_start)
        if not 0 < start_p <= floor_p:
            raise Rejected("접촉 탐색 범위 오류 · Z %.2f ~ %.2f mm" % (z_start, floor))
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
            zp = start_p
            while True:
                dz = sense(zp / PPMM)
                if not self.safety.gen_ok(g):                       # 재는 동안 비상정지가 왔으면 다음 걸음을 보내지 않는다
                    raise stage_mod.Discarded("정지로 취소됨")
                if dz is None:
                    return zp / PPMM
                dp = stage_mod.mm_to_pulse(dz)
                if dp == 0:
                    continue
                np_ = max(0, zp + dp)
                if np_ > floor_p:                                   # sense 가 이미 막지만 한 번 더(한도 아래로는 절대 내리지 않는다)
                    raise contact_mod.ContactFail("미접촉 · Z %.2f mm" % (floor_p / PPMM), "none")
                self.stage.goto("z", np_, g)
                zp = np_
        finally:
            if self.stage.connected:
                try:
                    self.stage.set_v(normal, None)                  # 속도를 되돌린다(비상정지 뒤에도 - 이동 명령이 아니다)
                except StageError as e:
                    self.logs.app("이동 속도 되돌리기 실패 · %s" % e, "warn")

    def contact_find(self):
        """준비 6 · 수동 탭 [접촉 탐색]: 지금 높이부터 0.05 mm 씩 내리며 찾는다(샘플 위 5 mm 안쪽까지 조그한 뒤)."""
        self._no_run("접촉 탐색")
        g = self.safety.generation
        try:
            res = self.contact_descend("search", self._mm("z") or 0.0, g)
        except contact_mod.ContactFail as e:
            raise Rejected("%s · 핀 상승 완료" % e)
        return {k: res[k] for k in ("first_z", "final_z", "r2", "r4")}

    def contact_save(self):
        """[이 높이로 저장]: 마지막으로 찾은 첫 접촉 높이를 접촉 높이(contact_z_mm)로 저장한다."""
        self._no_run("접촉 높이 저장")
        res = self.prep.contact.get("result")
        if not res:
            raise Rejected("[접촉 탐색] 필요")
        self._remember(contact_z_mm=round(float(res["first_z"]), 3))
        self.calib.set_contact({"r2": res["r2"], "r4": res["r4"], "how": "접촉 탐색"})
        self.prep.publish()
        return {"contact_z_mm": self.cfg["contact_z_mm"]}

    def raw(self, target: str, line: str):
        line = str(line).strip()
        if not line:
            raise Rejected("명령 입력 필요")
        if target == "stage":
            self._need_stage()
            tok = line.split()
            head = tok[0].lower()
            if head == "!":
                self.estop("직접 명령")
                return {"reply": "비상정지"}
            if self.safety.locked and head not in ("st", "p", "rs", "w"):
                raise Rejected("비상정지 상태")
            if head == "r" and len(tok) >= 3 and tok[2] == "1":
                raise Rejected("릴레이는 버튼 사용")
            if head in MOTION_WORDS or head[:1] in ("g", "j", "m") and len(head) == 2:
                raise Rejected("이동은 버튼 사용")
            try:
                lines = self.stage.raw(line, self.safety.generation)
            except StageError as e:
                raise Rejected(str(e))
            return {"reply": "\n".join(lines)}
        if target in ("k2110", "k2450"):
            dev = getattr(self, target)
            if dev is None or not dev.is_open:
                raise Rejected("장비 연결 안 됨")
            try:
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

    def _remember(self, **kw):
        """연결에 성공한 포트 · 주소 · 번호를 settings 에 저장한다(자동 연결이 쓴다). 저장 실패는 연결에 영향 없다."""
        try:
            self.cfg.update(kw)
            self.cfg.save()
        except (config_mod.ConfigError, OSError) as e:
            self.logs.app("설정 저장 실패 · %s" % e, "warn")
        self.publish_cfg()

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
            if self.k2450 is None and self.k2110 is None:
                raise Rejected("온도계 또는 저항계 연결 필요")
        self._monitor_on = on
        self.state.set("monitor", on)
        return {"monitor": on}

    def _monitor_loop(self):
        while self._alive:
            t0 = time.monotonic()
            try:
                self.prep.publish()                                 # 장비 연결 · 원점이 바뀌면 준비 단계 한 줄도 바뀐다(같으면 아무 일도 없다)
                self.runner.publish()
            except Exception as e:                                  # noqa: BLE001 - 화면용 요약이 감시 루프를 죽이지 않는다
                self.logs.app("준비 상태 요약 오류 · %s: %s" % (type(e).__name__, e), "error")
            if self._monitor_on:
                if self.safety.locked:
                    self._monitor_on = False
                    self.state.set("monitor", False)
                elif self.k2450 is not None and self.k2450.is_open:
                    try:
                        self.read_resistance()
                    except Rejected as e:
                        self._monitor_on = False
                        self.state.set("monitor", False)
                        self.state.alarm("monitor", "연속 측정 중지 · %s" % e, NOTICE)
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
            except Exception as e:                                  # noqa: BLE001 - 감시는 죽지 않는다. 기록하고 계속
                self.logs.app("히트건 감시 오류 · %s" % e, "error")
            time.sleep(0.25)

    def _temp_loop(self):
        while self._alive:
            dev = self.k2110
            if dev is not None and dev.is_open and not self.runner.feeding:      # 측정이 읽는 동안에는 따로 읽지 않는다(측정이 그 값을 넘겨 준다)
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
    "connect_meter": "connect_meter", "disconnect_meter": "disconnect_meter", "devices": "list_devices",
    "connect_camera": "connect_camera", "disconnect_camera": "disconnect_camera", "monitor": "monitor",
    "jog": "jog", "goto": "goto", "z_top": "z_top", "set_origin": "set_origin", "motion_stop": "motion_stop",
    "relay_on": "relay_on", "relay_off": "relay_off", "relays_off": "relays_off",
    "read_temp": "read_temp", "read_resistance": "read_resistance", "raw": "raw",
    "estop_release": "estop_release", "alarm_clear": "alarm_clear",
    "contact_find": "contact_find", "contact_save": "contact_save",
    # 준비 단계 · 사진 · 좌표 · 샘플(app/prep.py)
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
}


# 측정 중에도 되는 명령(읽기 · 측정 조작 · 경보 지우기). 그 밖의 명령은 측정 중에 거절한다. 비상정지는 명령 줄을 거치지 않는다.
RUN_ALLOWED = {"devices", "estop_release", "alarm_clear", "motion_stop", "run_start", "run_stop", "run_skip", "run_pause_after", "run_resume", "run_requeue",
               "sample_rename", "batch_set", "result_open_folder", "result_nas_copy"}


def _ohm(v):
    if v is None:
        return "—"
    a = abs(float(v))
    return "%.3g MΩ" % (v / 1e6) if a >= 1e6 else "%.3g kΩ" % (v / 1e3) if a >= 1e3 else "%.3g Ω" % v

__all__ = ["Controller", "COMMANDS", "Rejected", "config_mod"]
