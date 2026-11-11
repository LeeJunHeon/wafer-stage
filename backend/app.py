"""app.py - 서버 상태의 주인. 연결 · 카메라 · 좌표 보정 · 샘플 · 측정 · 회복 순서를 한곳에서 다룬다.

명령은 c_<이름>(args, gen) 이고, **gen 은 명령이 들어온 순간의 비상정지 세대**다(서버가 메시지 수신 때 잡는다).
이동 · 촬영 · 위치 확인 · 원점 등록은 모두 이 gen 을 core/motion.py 에 넘겨 잠금 뒤에 비교한다.
화면은 요청만 보내고 state() 가 돌려주는 값으로만 바뀐다.
"""

import collections
import concurrent.futures
import contextlib
import copy
import datetime
import os
import re
import shutil
import threading
import time

import cv2
import numpy as np

import calibstore
import conditions
import contact as contact_mod
import logger
import measure
import store
import vision as vision_mod
from core import align as align_mod
from core import fixed_detect, frames, geom, imgio, paths, sense, tip, zones
from core import motion as motion_mod
from core import power as power_mod
from core import stage as stage_mod
from core.motion import MotionError
from core.power import PowerError
from core.stage import StageError

STATUS_TAGS = ("done", "need", "invalid")


AUTO_LINK_S = float(os.environ.get("WAFER2_AUTO_LINK_S", "3"))    # 자동 확인 간격(s): 스테이지는 포트 목록만 본다
STAGE_RETRY_S = float(os.environ.get("WAFER2_STAGE_RETRY_S", "30"))   # 포트가 있는데 열기가 실패하면 이만큼에 한 번만 다시
CAM_BASE_S = float(os.environ.get("WAFER2_AUTO_CAM_S", "10"))     # 카메라 자동 시도 간격: 10 s → 20 s → 30 s


def _contact_view(r):
    """접촉 결과의 화면 · 상태용 요약(측정 점 목록은 빼고 짧게)."""
    if not r:
        return None
    return {k: r.get(k) for k in ("sense", "mode", "first_z", "final_z", "r2", "r4", "start_z", "floor_z", "s", "no", "x", "y", "t")}


class AppError(Exception):
    """화면에 가는 한 줄 사유."""


def _stamp():
    return time.strftime("%Y%m%d_%H%M%S")


class App:
    def __init__(self, dry=False, image=None, settings_path=None, fake=False):
        self.dry = dry
        self.fake = fake                             # --fake-serial: tools/fake_v11.py 를 진짜 시리얼 자리에 꽂는다(Stage 코드 경로는 그대로)
        self.sim = dry or fake                       # 더미 드라이버 · 합성 카메라 · 시간 배율은 시뮬레이션에서만
        self.link_lost_count = 0
        self.image = image
        self.cfg = store.Settings(settings_path)
        for n in getattr(self.cfg, "migrate_notes", []):         # 설정 파일 판 이전(한 번): 바꾼 값을 시작 로그에 남긴다
            logger.log("설정 이전 · " + n, "warn")
        for d in (paths.DATA_DIR, paths.SHOTS_DIR, paths.RUNS_DIR, paths.LOGS_DIR, paths.REF_HISTORY_DIR):
            os.makedirs(d, exist_ok=True)
        self.stage = self.motion = self.power = self.hub = None
        # 가동범위는 한 곳(core.stage 의 지금 값)만 본다: 설정 값을 넣고, 연결 때 · 저장 때 다시 넣은 뒤 펌웨어 한계로 자른다
        stage_mod.set_limits(self.cfg.get("limits.x_max_mm"), self.cfg.get("limits.y_max_mm"), self.cfg.get("limits.z_max_mm"))
        self.calib = calibstore.Calib()
        self.head = calibstore.HeadCalib()
        self.vision = vision_mod.Vision(self.cfg, self._true_xy, self.sim, image)
        self.lock = threading.RLock()
        self._estop_lk = threading.Lock()
        self._estop_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="estop")
        self._estop_fut = None
        self._estop_t = -1e9
        self._estop_ex.submit(lambda: None).result()                 # 스레드를 미리 만든다(첫 비상정지가 스레드 생성 때문에 늦지 않게)
        self._stop_lk = threading.Lock()                             # 정지는 비상정지와 실행 스레드를 나눈다(비상정지가 정지 뒤에 줄 서지 않게)
        self._stop_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="stop")
        self._stop_fut = None
        self._stop_t = -1e9
        self._stop_ex.submit(lambda: None).result()
        self.stop_note = None                                        # 정지 결과 알림 한 줄 {n, text} - 화면이 새 n 을 알림으로 띄운다
        self._stop_n = 0
        self.stop_band = None                                        # X · Y 절대 이동이 정지로 끊겼을 때의 주황 띠
        self._pending_estop = False                                  # 연결 중에 눌린 비상정지 - 연결이 끝나면 잠금으로 시작한다
        self._conn_stage = None
        self.version = 0
        self.busy = ""                               # 촬영 | 위치 확인 | 헤드 위치 보정 | 공구 가열 | 빈 판 사진 …
        self.alarm = None
        self.ref_meta = store.read_json(paths.REF_META_PATH)
        self._ref_img = None
        if self.ref_meta and not self.ref_meta.get("size"):
            try:
                self.ref_img()                                    # 예전 파일: 크기를 사진에서 채운다
            except Exception:
                pass
        self.snap = None                             # 단발 촬영: {img, A, id, t}
        self.shot = None                             # 촬영 결과
        self.samples = []
        self.edits = []
        self.sel = None
        self.next_no = 1
        self.run = None
        self.last_run = None
        self.sim_missing = set(x for x in os.environ.get("WAFER2_MISSING", "").split(",") if x)     # 시험: 장치를 뺀 상태(stage · camera · visa)
        self.dev_err = {}                            # 장치별 마지막 연결 실패 사유(화면 띠에 간다)
        self.attempted = {"stage": False}            # 연결을 한 번이라도 해 보았는가(아니면 "확인 안 됨")
        self.connecting = {"stage": False, "camera": False}   # 연결 시도가 지금 진행 중인가(자동 · 수동 모두)
        self.manual = {"stage": False, "camera": False}       # 사용자가 [다시 연결]을 눌러 진행 중인가 - 화면에 "연결 중" 이 보이는 것은 이때만
        self.link_lock = threading.RLock()                    # 스테이지 연결 · 다시 연결 · 자동 시도를 하나로 묶는다
        self.cam_lock = threading.Lock()
        self._link_log = {}
        self._stage_try_t = 0.0
        self._port_prev = False
        self._port_gone = False
        self._cam_next = 0.0
        self._cam_step = 0
        self._fake_next_eeprom = None
        self.opens = {"stage": 0, "camera": 0}                # 시험: 실제로 연 횟수
        self.open_delay = {"stage": 0.0}                      # 시험: 열기를 느리게
        self._auto_th = None
        self.cam_thumbs = {}                         # [카메라 찾기] 번호별 작은 사진(JPEG)
        self.cam_scan = None                         # [카메라 찾기] 결과: {running, items: [{index, ok, size, busy}]}
        self._apply_missing()
        self.k2110 = self.k2450 = None               # 측정 장비(VISA) - [장비 연결] 을 누를 때만 연다
        self.lab = None                              # 시뮬레이션의 가짜 장비 세계(tools/fake_visa.py)
        self.inst_info = {"k2110": None, "k2450": None, "msg": "", "test": None}
        self.rt = None                               # R–T 측정(지금 또는 마지막 - 자동 · 순회 · 수동 하나). 도는 동안 모든 이동 · 릴레이 · 촬영을 막는다
        self.rt_last = {"auto": None, "manual": None}    # 화면별 마지막 측정(측정 탭 · 수동 측정 탭)
        self.free_move = False                        # '원점 없이 이동 허용'(저장 안 함 · 시작 · 다시 연결 · 세 축 원점 정상이면 꺼짐)
        self.manual_last = {}                         # 수동 측정 [온도 1번] · [저항 1번] 마지막 값
        self._rt_cache = {}
        self.pos = {"x": None, "y": None, "z": None}
        self.fw_homed = {"x": False, "y": False, "z": False}   # 펌웨어 ST 의 원점 플래그(남아 있으면 끝단 앞까지 자동 접근)
        self.poll_fail = 0
        self.serial_log = collections.deque(maxlen=400)       # 기록 보기 "시리얼" 탭: (시각, 방향, 원문)
        self.eeprom = ""                                      # 연결 시 EEPROM 상태 restored | unsaved | none
        self.reconnecting = False
        self._head_ref_quad = (None, None)                    # (ref_id, 빈 판 사진 안 헤드 마커 사각형)
        self.pending_verify = None
        self.pending_calib = None
        self.tool_tmp = {}
        self.stop_poll = threading.Event()
        self.cap_text = ""
        self.cap_error = None
        self.notes = []
        self._ka = None
        self._dirty = threading.Event()
        self.calib_msgs = []
        self.shutting = False
        self._load_last_shot()

    # ------------------------------------------------------------------
    # 공통
    # ------------------------------------------------------------------
    def touch(self):
        self.version += 1
        self._dirty.set()

    def _true_xy(self):
        if self.stage is None:
            return (0.0, 0.0)
        if self.stage.dry:
            return self.stage.dry_true_xy()
        return self.stage.ser.true_xy() if hasattr(self.stage.ser, "true_xy") else (self.pos["x"] or 0.0, self.pos["y"] or 0.0)

    def _true_xyz(self):
        """시뮬레이션의 실제(물리) X · Y · Z mm - 가짜 K2450 의 접촉 흉내용. 스테이지가 없으면 None."""
        st = self.stage
        if st is None:
            return None
        x, y = self._true_xy()
        if st.dry:
            z = st._dry_xy["z"]
        else:
            ser = st.ser
            z = ser.real["z"] / stage_mod.Z_PPMM if hasattr(ser, "real") else (self.pos["z"] or 0.0)
        return (x, y, z)

    def ids(self):
        return {"ref": (self.ref_meta or {}).get("id"), "calib": self.calib.d.get("created"), "calib_ref": self.calib.d.get("ref_id"),
                "head": self.head.d.get("ref_id")}

    def shot_dir_name(self):
        return None if not self.shot else self.shot["id"]

    bang_fail = None                                 # "비상정지" | "정지": '!' 가 전해지지 않아 다시 보내는 중 - 그동안 다른 경보가 이 경보를 덮지 못한다
    _held_alarm = None

    def set_alarm(self, text, nxt="", force=False):
        if self.bang_fail and not force:
            if text:
                self._held_alarm = (text, nxt)              # 전송 실패 경보가 우선 - 성공하면 되살린다
            return
        self.alarm = {"text": text, "next": nxt, "t": time.time()} if text else None
        self.touch()

    def _size(self):
        return self.vision.size

    def ref_id(self):
        return (self.ref_meta or {}).get("id")

    def ref_img(self):
        if self._ref_img is None and self.ref_meta and os.path.exists(paths.REF_PATH):
            self._ref_img = imgio.imread_u(paths.REF_PATH)
        if self._ref_img is not None and self.ref_meta and not self.ref_meta.get("size"):
            self.ref_meta["size"] = [int(self._ref_img.shape[1]), int(self._ref_img.shape[0])]     # 크기 없는 예전 파일: 사진을 읽어 채운다
        return self._ref_img

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------
    def _apply_missing(self):
        """시험용 "장치 없음" 스위치를 각 계층에 건다(실제 포트 · 카메라 · VISA 는 열지 않는다)."""
        from core import instruments as inst
        self.vision.missing = self.sim and "camera" in self.sim_missing
        inst.DISABLED = self.sim and "visa" in self.sim_missing

    def connect(self):
        if self.stage is not None:
            return
        if self.sim and "stage" in self.sim_missing:
            self.attempted["stage"] = True
            raise StageError("포트 없음 · USB 연결 확인")
        port = self.cfg.get("serial_port")
        if self.fake:
            import sys as _sys
            _sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
            import fake_v11
            def _mk(*_a):
                ee = self._fake_next_eeprom or os.environ.get("WAFER2_FAKE_EEPROM") or None
                self._fake_next_eeprom = None
                f = fake_v11.FakeV11(time_scale=float(os.environ.get("WAFER2_FAKE_SCALE", "0.05")), eeprom=ee)
                self._last_fake = f                             # 시험: 연결이 실패해 공개되지 않은 보드도 닫혔는지 본다
                return f
            stage_mod.SERIAL_FACTORY = _mk
        st = stage_mod.Stage(port=port, dry=self.dry)
        st.on_line = lambda arrow, text: self.serial_log.append((time.strftime("%H:%M:%S"), arrow, text))
        self.opens["stage"] += 1
        if self.sim and self.open_delay["stage"] > 0:
            time.sleep(self.open_delay["stage"])
        # 지역 변수로 조립하고, 가속 · 시작 속도 · 통신 감시 설정 + ST 동기화 + estop.lock 확인이 모두 끝난 뒤에만 한꺼번에 공개한다.
        # 하나라도 실패하면 포트를 닫고 "연결 안 됨" 으로 되돌린다(반쯤 연결된 이동 계층이 "원점 정상" 으로 보이는 일이 없게).
        self._conn_stage = st
        ok = False
        hub = None
        try:
            st.open()
            stage_mod.set_limits(self.cfg.get("limits.x_max_mm"), self.cfg.get("limits.y_max_mm"), self.cfg.get("limits.z_max_mm"))
            stage_mod.configure(speed_pps=self.cfg.get("motion.speed_pps"), jog_pps=self.cfg.get("motion.jog_pps"),
                                jog_slow_pps=self.cfg.get("motion.jog_slow_pps"), line_timeout_s=self.cfg.get("serial.line_timeout_s"),
                                move_timeout_s=self.cfg.get("serial.move_timeout_s"))
            clamp = self._clamp_to_firmware(st)
            mo = motion_mod.Motion(st, log=lambda m: logger.log(m, "warn"))
            pw = power_mod.Power(st, heater_n=self.cfg.get("heater.relay"), log=logger.log)
            mo.pre_xy = pw.pre_xy
            mo.on_reboot = self._on_reboot
            _prev = st.on_event

            def _ev(kind, line):
                if _prev:
                    _prev(kind, line)
                if kind == "wd_trip":
                    self._on_link_lost(line)
                elif kind == "write_timeout":
                    if not getattr(self, "_link_alarmed", False):          # 막힌 동안 쓰기마다 사건이 나므로 한 번만
                        self._link_alarmed = True
                        self._on_link_lost("응답 없음 · 쓰기 시간 초과")
            st.on_event = _ev
            self._apply_motion_cfg(mo)
            hub = measure.Hub(self.cfg, pw, mo, self.sim)
            hub.configure()
            self._init_firmware(st, mo, strict=True)
            eeprom = st.eeprom_state()
            mo.sync_from_status()
            locked = os.path.exists(os.path.join(paths.DATA_DIR, "estop.lock")) or self._pending_estop      # 비상정지 잠금이 풀리지 않은 채 꺼졌었다 · 연결 중에 눌렸다
            if locked:
                mo.estop_locked = True
                mo.z_ok = False
                mo._xy_lost()
                st._aborted = True
            self.stage, self.motion, self.power, self.hub = st, mo, pw, hub          # 여기서 한꺼번에 공개
            if self.free_move:
                logger.log("[원점 무시] 스테이지 다시 연결 · 원점 없이 이동 허용 끔", "warn")
            self.free_move = False                                               # 새 연결은 늘 꺼진 채로 시작
            self.bang_fail = None                                                # 새로 연결했다(보드가 재시작돼 모터가 멈춘 상태) - 전달 못 한 '!' 는 끝
            self._port_gone = False                                              # 사람이 다시 연결했어도 "포트 사라짐" 이 남으면 자동 연결이 한 번 더 연다(보드 리셋 한 번 더)
            self.eeprom = eeprom
            ok = True
        finally:
            self._conn_stage = None
            if not ok:
                with contextlib.suppress(Exception):
                    st.close()
        hub.start()
        self._link_alarmed = False
        if self._pending_estop or self.motion.estop_locked:
            self._pending_estop = False
            self._lock_file(True)
            self.set_alarm("비상정지 잠금 이어짐 · 원점 다시 잡기", "회복: " + " → ".join(self.motion.recovery_steps()))
        elif clamp:
            self.set_alarm(clamp, "설정에서 가동범위를 확인")
        self.vision.true_xy = self._true_xy
        self.touch()
        if self._ka is None or not self._ka.is_alive():
            self._ka = threading.Thread(target=self._keepalive, name="keepalive", daemon=True)
            self._ka.start()
        logger.log("스테이지 연결 · %s" % (st.fw_version or "?"))
        if self.cfg.get("photohome.on_connect") and self.motion.z_ok and self.motion.xy_ok:
            threading.Thread(target=self._connect_verify, name="connect-verify", daemon=True).start()

    def _clamp_to_firmware(self, st):
        """앱 가동범위가 펌웨어 한계(배너)보다 크면 펌웨어 한계로 자르고 경고 문구를 돌려준다."""
        fw = stage_mod.fw_limits_pulse(st.banner)
        lm = stage_mod.limits_mm()
        over = []
        patch = {}
        for ax in ("x", "y", "z"):
            if ax in fw:
                fw_mm = fw[ax] / (stage_mod.Z_PPMM if ax == "z" else stage_mod.PPMM)
                if lm[ax + "_max_mm"] > fw_mm + 1e-6:
                    patch[ax + "_max_mm"] = fw_mm
                    if lm[ax + "_max_mm"] > fw_mm + 0.5:              # 0.5 mm 이하 차이(기본 Y 247.8 · 펌웨어 247.75)는 조용히 자른다
                        over.append(ax.upper())
        if not patch:
            return ""
        stage_mod.set_limits(**patch)
        if not over:
            return ""
        logger.log("가동범위 · 펌웨어 한계로 자름 %s" % ",".join(over), "warn")
        return "가동범위 설정이 펌웨어 한계보다 큼 · %s 를 펌웨어 한계로 자름" % " · ".join(over)

    def _connect_verify(self):
        """연결 직후 원점이 복원됐고 헤드 위치 보정이 있으면, 움직이지 않고 사진으로 위치를 한 번 확인한다. 차이가 한도를 넘으면 경고만 한다(자동 적용 안 함)."""
        try:
            time.sleep(1.0)
            if self.head.status(self.ref_id(), self._size())[0] != "done" or self.vision.cam is None and not self.sim:
                return
            if self.busy or self.run or self.shutting or self.motion is None or not (self.motion.z_ok and self.motion.xy_ok):
                return
            gen = self.motion.gen
            r = self.c_verify_position({}, gen)
            if r and r.get("verdict") == "확인 필요":
                self.notes.append("위치 확인 · 차이 %.1f mm" % r["d"])
                logger.log("연결 직후 위치 차이 %.1f mm · 위치 확인 필요" % r["d"], "warn")
                self.set_alarm("위치 차이 %.1f mm · 위치 확인 필요" % r["d"], "위치 확인 뒤 적용")
            self.pending_verify = None if r and r.get("verdict") != "확인 필요" else self.pending_verify
        except Exception as e:                              # noqa: BLE001
            logger.log("연결 직후 위치 확인 못 함 · " + logger.short(e), "detail")
        finally:
            self.touch()

    def save_position(self):
        """대기 위치 도착 · 측정 끝 · 종료 때 EEPROM 에 저장(다음 부팅에 원점이 복원된다). 비상정지 잠금 중이면 하지 않는다."""
        try:
            if self.stage is not None and self.motion is not None and not self.motion.estop_locked and self.motion.z_ok and self.motion.xy_ok:
                self.stage.save()
        except Exception as e:                              # noqa: BLE001
            logger.log("위치 저장 못 함 · " + logger.short(e), "warn")

    def _reconnect(self):
        """스테이지를 다시 연결한다(설정 가져오기 · 포트 변경 · 통신 끊김 뒤). 이동 · 작업 중에는 하지 않는다(비상정지가 '!' 를 못 보내는 틈이 생긴다).
        새 이동 계층의 비상정지 세대는 옛 것보다 크게 시작한다(다시 연결 전에 잡힌 명령이 통과하지 못하게)."""
        mo = self.motion
        if mo is not None:
            if self.busy or mo.moving or not mo.lock.acquire(blocking=False):
                raise AppError("이동 중 · 끝난 뒤 다시 연결")
        old_gen = mo.gen if mo is not None else 0
        self.reconnecting = True
        self.touch()
        try:
            st = self.stage
            if st is not None:
                try:
                    if not mo.estop_locked and mo.z_ok and mo.xy_ok:
                        st.save()
                except Exception:                           # noqa: BLE001
                    pass
            if self.hub:
                self.hub.stop()
            if st is not None:
                st.close()                                  # 닫은 뒤에야 옛 계층을 버린다(그 사이 비상정지는 옛 계층으로 간다)
            self.stage = self.motion = self.power = self.hub = None
            self.connect()
            if self.motion is not None:
                self.motion.gen = max(self.motion.gen, old_gen + 1)
        finally:
            if mo is not None:
                mo.lock.release()
            self.reconnecting = False
            self.touch()

    def _apply_motion_cfg(self, m=None):
        m = m or self.motion
        m.speed_pps = int(self.cfg.get("motion.speed_pps"))
        m.accel = int(self.cfg.get("motion.accel"))
        m.slow_pps = int(self.cfg.get("motion.jog_slow_pps"))
        m.jog_guard = self._jog_guard

    def _init_firmware(self, st=None, mo=None, strict=False):
        """연결 직후 · 재부팅 뒤: 속도 · 가속 · 통신 감시를 다시 보낸다. strict(연결 중)면 하나라도 실패하면 예외 - 통신 감시 없이 계속 쓰지 않는다."""
        st = st or self.stage
        mo = mo or self.motion
        with mo.lock:
            try:
                st.set_profile(self.cfg.get("motion.accel"), self.cfg.get("motion.start_pps"))
                st.watchdog(int(self.cfg.get("relay.watchdog_ms")))
            except StageError as e:
                logger.log("펌웨어 설정 실패 · " + logger.short(e), "warn")
                if strict:
                    raise

    def _on_reboot(self, line):
        """A-10: 보드 재부팅 감지 - 측정 중지, 재설정, ST · RS 다시 읽기, 원점 확인 요구."""
        logger.log("보드 재부팅 감지", "err")
        if self.run:
            self.run.stop()
        self.set_alarm("보드 재부팅 감지 · 원점 확인 필요", "회복: " + " → ".join(self.motion.recovery_steps()))

        def work():
            try:
                self._init_firmware()
                st = self.stage.status()
                self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
                self.stage.relay_status()
            except Exception as e:                      # noqa: BLE001
                logger.log("재부팅 뒤 재설정 실패 · " + logger.short(e), "err")
            self.touch()
        threading.Thread(target=work, daemon=True).start()

    def _on_link_lost(self, line):
        """펌웨어가 통신 끊김으로 릴레이를 전부 껐다: 경보 + 측정 중이면 그 샘플을 오류로(Run._check 가 본다).
        경보를 먼저 세운 뒤 카운터를 올린다 - 측정이 오류로 끝나는 순간 화면에 경보가 이미 있게."""
        no_reply = "응답" in str(line)
        if self.power and not no_reply:
            self.power.owners.clear()                       # 펌웨어가 껐다(통신 감시)
        logger.log("통신 끊김 · " + ("응답 없음" if no_reply else "릴레이 꺼짐"), "err", raw=line)
        self.set_alarm("통신 끊김 · " + ("응답 없음" if no_reply else "릴레이 꺼짐"), "연결 확인 뒤 다시 시작")
        self.link_lost_count += 1
        if no_reply and self.power and not getattr(self, "_alloff_running", False):
            def _off():
                self._alloff_running = True
                try:
                    self.power.all_off()                    # 닿는 대로 히트건 끄기를 다시 보낸다(한 번에 하나만)
                finally:
                    self._alloff_running = False
            threading.Thread(target=_off, daemon=True, name="link-off").start()

    def _confirm_heater_off(self):
        """비상정지 뒤 RS 로 히트건 꺼짐을 확인한다. 못 하면 재시도하고, 끝내 못 하면 경보."""
        if not self.power:
            return
        for _ in range(6):
            try:
                self.power.heater_off()
                return
            except PowerError:
                time.sleep(0.5)
        self.set_alarm("히트건 끄기 미확인 · 전원을 직접 끊을 것", "비상정지 뒤 RS 확인 실패")

    def _comm_ok(self):
        self.poll_fail = 0
        self._link_alarmed = False

    def _comm_fail(self):
        self.poll_fail += 1
        if self.poll_fail >= 3 and not getattr(self, "_link_alarmed", False):
            self._link_alarmed = True
            self._on_link_lost("응답 없음 · 연속 실패")

    def _keepalive(self):
        """통신 감시 유지 신호 + 상태 읽기. 바쁨 · 일시정지 · 확인 대기와 상관없이 wd/3 이하 간격으로 st 를 거래 잠금을 거쳐 보낸다.
        블로킹 이동 중이면(잠금을 못 얻으면) 그 이동이 끝날 때까지 건너뛴다(펌웨어도 그때는 판정하지 않는다)."""
        while not self.stop_poll.is_set():
            wd = int(self.cfg.get("relay.watchdog_ms") or 0)
            period = 1.0 if wd <= 0 else max(0.15, min(1.0, wd / 3000.0))
            self.stop_poll.wait(period)
            st = self.stage
            if st is None or self.shutting:
                continue
            try:
                if st.txn_try():
                    try:
                        d = st.status(timeout=1.5)
                    finally:
                        st.txn_release()
                    self.pos.update(x=d["x_mm"], y=d["y_mm"], z=d.get("z_mm"))
                    self.fw_homed = {"x": bool(d.get("homed_x")), "y": bool(d.get("homed_y")), "z": bool(d.get("homed_z"))}
                    self._comm_ok()
            except Exception as e:                      # noqa: BLE001
                self._comm_fail()                        # 한 번 실패로 연결을 끊지 않는다 · 연속 3회면 통신 끊김(측정 중이면 오류, 공구 가열이면 실패)
            self.touch()

    # ------------------------------------------------------------------
    # 비상정지
    # ------------------------------------------------------------------
    def estop_async(self, t_req=None):
        """비상정지 요청(WS · HTTP 둘 다 여기로). 전용 스레드 하나에서만 실행한다 - 이벤트 루프 · 명령 풀에서 쓰기 잠금을 기다리지 않는다.
        300 ms 안의 같은 요청은 앞 요청의 결과를 그대로 돌려준다(한 번만 처리). Future 를 돌려준다."""
        now = time.monotonic()
        t_req = t_req or time.time()
        with self._estop_lk:
            f = self._estop_fut
            if f is not None and now - self._estop_t < 0.3:
                return f
            self._estop_t = now
            if f is not None and not f.done():
                # 앞 비상정지가 아직 끝나지 않았다(쓰기가 막혀 있다) - 새로 누른 것은 그 뒤에 줄 서지 않고 따로 시도한다
                f = concurrent.futures.Future()
                threading.Thread(target=self._run_into, args=(f, self.estop, t_req), daemon=True, name="estop-extra").start()
                self._estop_fut = f
                return f
            self._estop_fut = f = self._estop_ex.submit(self.estop, t_req)
            return f

    @staticmethod
    def _run_into(fut, fn, *a):
        try:
            fut.set_result(fn(*a))
        except BaseException as e:                       # noqa: BLE001
            fut.set_exception(e)

    def stop_async(self, t_req=None):
        """정지 요청(WS · HTTP). 비상정지와 같은 수준의 빠른 길이지만 실행 스레드는 따로다. 300 ms 안의 같은 요청은 한 번만."""
        now = time.monotonic()
        t_req = t_req or time.time()
        with self._stop_lk:
            f = self._stop_fut
            if f is not None and now - self._stop_t < 0.3:
                return f
            self._stop_t = now
            if f is not None and not f.done():                # 앞 정지가 막혀 있으면 따로 시도(비상정지와 같은 규칙)
                f = concurrent.futures.Future()
                threading.Thread(target=self._run_into, args=(f, self.stop, t_req), daemon=True, name="stop-extra").start()
                self._stop_fut = f
                return f
            self._stop_fut = f = self._stop_ex.submit(self.stop, t_req)
            return f

    def estop(self, t_req=None):
        """즉시. 잠금을 기다리지 않는다. '!' 를 가장 먼저 쓰고, 잠금 · 측정 중지 · 출력 끄기 · 경보는 '!' 쓰기가 돌아오기를 기다리지 않는다
        (쓰기가 막혀 있으면 0.15 s 뒤 따로 진행한다)."""
        t_req = t_req or time.time()
        m = self.motion
        if m is None:
            # 연결 중: 이동 계층이 아직 없다. 잠금 파일을 남겨 연결이 끝난 뒤 잠금으로 시작한다(열려 있는 포트가 있으면 '!' 도 보낸다).
            self._pending_estop = True
            self._lock_file(True)
            cs = self._conn_stage
            if cs is not None:
                with contextlib.suppress(Exception):
                    cs.abort()
            self.set_alarm("비상정지 · 연결이 끝나면 잠김", "")
            logger.log("비상정지 · 연결 중", "err")
            return {"ok": True}
        heater = int(self.cfg.get("heater.relay"))
        policy_off = self.cfg.get("relay.on_estop") == "off"
        was_moving = bool(m.moving)
        state = {"done": False, "err": None}
        rest_lk = threading.Lock()
        rest_ran = [False]

        def rest():
            with rest_lk:
                if rest_ran[0]:
                    return
                rest_ran[0] = True
            self._estop_rest(policy_off, heater, state)
        tm = threading.Timer(0.15, rest)
        tm.daemon = True
        tm.start()
        try:
            m.estop(relays_off=policy_off, off_relays=(heater,))                # 세대를 올리고 '!' 를 쓴다
        except Exception as e:                              # noqa: BLE001 - 포트가 뽑힌 상태(SerialException 등)여도 잠금 · 경보는 반드시 건다
            state["err"] = e
            logger.log("비상정지 전송 실패 · " + logger.short(e), "err")
        state["done"] = True
        tm.cancel()
        rest()
        if state["err"] is not None:
            # '!' 가 전해지지 않았다: 사람이 멈춘 줄 알면 안 된다 - 다른 경보(통신 끊김 등)가 이 경보를 덮지 못하게 붙들고 다시 보낸다
            self.bang_fail = "비상정지"
            self.set_alarm("비상정지 전송 실패 · 모터 전원을 끄십시오", "다시 보내는 중", force=True)
            self._retry_bang(policy_off, heater, True)
        else:
            self.bang_fail = None                           # '!' 가 이번에는 전해졌다(앞서 실패한 전송이 있었어도)
            self._held_alarm = None
            self.set_alarm("비상정지 · 이동 잠김", "회복: " + " → ".join(m.recovery_steps()), force=True)   # 쓰기가 늦어 "전송 중" 이 먼저 떴으면 바꾼다
        self._report_cut("비상정지", t_req, m, was_moving)
        return {"ok": True}

    def _estop_rest(self, policy_off, heater, state):
        if self.power:
            self.power.owners.clear() if policy_off else self.power.owners.pop(heater, None)
        if self.run:
            self.run.stop()
        if self.rt is not None and self.rt.active:
            self.rt.stop()
        threading.Thread(target=self._inst_off, daemon=True, name="estop-inst").start()      # K2450 출력 끄기(VISA 는 느릴 수 있어 별도 스레드)
        threading.Thread(target=self._confirm_heater_off, daemon=True, name="estop-heater").start()
        self.pending_verify = None
        self.stop_band = None
        self._lock_file(True)
        if not state["done"]:                               # '!' 쓰기가 아직 돌아오지 않았다(막힘) - 끝나면 estop() 이 성공 · 실패 경보로 바꾼다
            self.set_alarm("비상정지 전송 중 · 모터 전원을 끄십시오", "보내는 중", force=True)
        logger.log("비상정지", "err")

    def _retry_bang(self, policy_off, heater, is_estop):
        """'!' 쓰기가 실패했다: 0.1 s 간격으로 성공할 때까지(최대 10 s) 다시 보낸다. 성공하면 평소 경보로 바꾼다."""
        def work():
            end = time.monotonic() + 10.0
            while time.monotonic() < end:
                st = self.stage
                if st is None:
                    return
                try:
                    if is_estop:
                        st.abort(relays_off=policy_off, off_relays=(heater,))
                    else:
                        if self.motion is not None and self.motion.estop_locked:
                            if self.bang_fail == "정지":
                                self.bang_fail = None           # 그 사이 비상정지가 눌렸다 - 그쪽이 책임진다
                            return
                        st.stop()
                    held, self._held_alarm = self._held_alarm, None
                    if self.bang_fail == ("비상정지" if is_estop else "정지"):
                        self.bang_fail = None
                    if is_estop:
                        self.set_alarm("비상정지 · 이동 잠김", "회복: " + " → ".join(self.motion.recovery_steps()), force=True)
                    elif held and not held[0].startswith("통신 끊김"):
                        self.set_alarm(held[0], held[1], force=True)   # 붙들어 둔 다른 경보(히트건 끄기 미확인 등)를 되살린다
                    else:
                        self.set_alarm(None, force=True)
                    logger.log("%s 전송 재시도 성공" % ("비상정지" if is_estop else "정지"), "warn")
                    return
                except Exception:                           # noqa: BLE001
                    time.sleep(0.1)
            logger.log("%s 전송 10 s 재시도 실패 · 경보 유지" % ("비상정지" if is_estop else "정지"), "err")
        threading.Thread(target=work, daemon=True, name="bang-retry").start()

    def stop(self, t_req=None):
        """정지: 움직이는 모터를 즉시 멈추고 이어서 하려던 이동을 전부 취소한다. 잠그지 않고 릴레이 · K2450 · 원점은 건드리지 않는다."""
        t_req = t_req or time.time()
        m = self.motion
        if m is None:
            return {"ok": True, "cut": False}
        run = self.active_run()
        solo = self.rt is not None and self.rt.active and run is None          # 단독 R–T · 속도 측정: 세대를 올리지 않는다(히트건 켜기 쓰기가 막힌다)
        was_moving = bool(m.moving)
        cut_xy = was_moving and getattr(m, "cur_op", "") == "xy"
        err = None
        try:
            m.stop(bump=not solo)                           # 세대를 올리고 '!' 만 쓴다
        except Exception as e:                              # noqa: BLE001
            err = e
            logger.log("정지 전송 실패 · " + logger.short(e), "err")
        if err is not None:
            if self.bang_fail != "비상정지":
                self.bang_fail = "정지"
                self.set_alarm("정지 전송 실패 · 비상정지를 누르십시오", "다시 보내는 중", force=True)
            self._retry_bang(False, 0, False)
        if not solo:
            if run is not None:
                run.stop()                                  # 측정은 "중지됨 · 정지로 중단" 으로 끝낸다(gen 이 바뀌어 Z 올림 · 대기 위치 이동은 하지 않는다)
            self.pending_verify = None
        self._report_cut("정지", t_req, m, was_moving, cut_xy)
        return {"ok": True, "cut": was_moving}

    def _report_cut(self, kind, t_req, m, was_moving=True, cut_xy=False):
        """누름→'!' · '!'→멈춤 시간과 끊긴 자리를 로그 · 알림으로 남긴다(실장비에서 읽을 숫자). 끝난 것을 기다리지 않도록 별도 스레드."""
        st = self.stage
        wall = (st.stop_wall if kind == "정지" else st.bang_wall) if st is not None else []
        t_bang = wall[-1] if wall else None

        def work():
            try:
                t_end = time.time()
                end = t_end + 3.0
                while m.moving and time.time() < end:
                    time.sleep(0.01)
                t_end = time.time()
                info = dict(getattr(st, "cut_info", {}) or {})
                pos = None
                if kind == "정지" and st is not None and self.motion is m:
                    try:
                        d = m.status()                      # 정지 뒤에도 위치는 펌웨어 ST 로 다시 읽는다
                        pos = d
                        self.pos.update(x=d["x_mm"], y=d["y_mm"], z=d.get("z_mm"))
                    except Exception:                       # noqa: BLE001
                        pos = None
                bits = [kind]
                mt = re.search(r"([XYZ]\S*)\s+[+-]\s+cmd=(-?\d+)\s+out=(-?\d+)", info.get("line", ""))
                if mt and was_moving:
                    bits.append("%s 이동 끊김 · %s 펄스 중 %s" % (mt.group(1), mt.group(2).lstrip("-"), mt.group(3).lstrip("-")))
                elif was_moving and "frac" in info:
                    bits.append("이동 끊김 · %d%%" % round(info["frac"] * 100))
                if pos is not None:
                    bits.append("X %.1f Y %.1f Z %.2f" % (pos["x_mm"], pos["y_mm"], pos.get("z_mm") or 0.0))
                if t_bang is not None:
                    bits.append("누름→'!' %d ms" % round((t_bang - t_req) * 1000))
                    if was_moving:
                        bits.append("'!'→멈춤 %d ms" % round((t_end - t_bang) * 1000))
                logger.log(" · ".join(bits), "info")
                if kind == "정지":
                    self._stop_n += 1
                    if was_moving and pos is not None:
                        self.stop_note = {"n": self._stop_n, "text": "정지 · X %.1f · Y %.1f · Z %.2f" % (pos["x_mm"], pos["y_mm"], pos.get("z_mm") or 0.0)}
                    else:
                        self.stop_note = {"n": self._stop_n, "text": "정지 · 움직이는 것 없음"}
                    if cut_xy and pos is not None and not m.estop_locked and self.motion is m:
                        self.stop_band = {"text": "정지 · 이동이 끊김 · X %.1f · Y %.1f · Z %.2f" % (pos["x_mm"], pos["y_mm"], pos.get("z_mm") or 0.0),
                                          "gen": m.gen}
                self.touch()
            except Exception as e:                          # noqa: BLE001
                logger.log("끊김 보고 못 함 · " + logger.short(e), "detail")
        threading.Thread(target=work, daemon=True, name="cut-report").start()

    def active_run(self):
        r = self.run
        return r if (r is not None and r.state in ("ready", "running", "paused", "stopping")) else None

    def cancel_text(self, gen):
        """세대가 바뀌어 취소된 이유 문구."""
        m = self.motion
        return "비상정지로 중단" if (m is not None and m.last_estop_gen > gen) else "정지로 중단"

    def move_block(self, what="이동", jog=False, ignore_moving=False, ignore_recent=False):
        """지금 사람이 보낸 이동 명령을 받을 수 있는가: 못 받는 사유 한 줄(받을 수 있으면 ""). 모든 이동 명령과 화면의 비활성 사유가 이 하나를 쓴다."""
        m = self.motion
        if m is None:
            return "스테이지 연결 안 됨"
        if self.reconnecting:
            return "다시 연결 중"
        r = self.active_run()
        if r is not None:
            if jog and r.state == "running" and r.paused:
                if not getattr(r, "in_pause", False):
                    return "일시정지 · 단계가 끝난 뒤"            # 히트건 단계가 아직 도는 중 - X · Y 조그의 히트건 끄기가 그 단계를 깨뜨린다
            elif jog and r.state == "running":
                return "측정 중 · 일시정지 뒤 가능"
            else:
                return "측정 중 · %s 못 함" % what
        if self.rt is not None and self.rt.active:
            return "%s 중 · %s 못 함" % (self.rt_label(), what)
        if self.busy:
            return "%s 중 · 끝난 뒤" % self.busy
        now = time.monotonic()
        if ignore_recent:                                   # 화면의 비활성 사유(state): 0.5 · 1 s 짧은 거절은 명령이 맡는다(패드가 깜빡이지 않게)
            pass
        elif now - m.estop_t < motion_mod.ESTOP_QUIET_S:
            return "비상정지 직후 · 잠시 뒤"
        elif now - m.stop_t < motion_mod.STOP_QUIET_S:
            return "정지 직후 · 잠시 뒤"                 # 끊긴 이동의 보고가 아직 안 와 moving 이 남아 있어도 사유는 이것이다
        if m.moving and not ignore_moving:
            return "이동 중 · 끝난 뒤"
        return ""

    def _need_move_opt(self, what):
        """스테이지 없이도 되는 작업(촬영 · 빈 판 사진): 스테이지가 있으면 이동 명령 판단(move_block)을 그대로 쓴다."""
        if self.motion is None:
            self._no_run(what)
            if self.busy:
                raise AppError("%s 중 · 끝난 뒤" % self.busy)
            return
        self._need_move(what)

    def _need_move(self, what="이동", jog=False):
        why = self.move_block(what, jog)
        if why:
            raise AppError(why)

    # ------------------------------------------------------------------
    # 명령 진입
    # ------------------------------------------------------------------
    def handle(self, cmd, args, gen):
        fn = getattr(self, "c_" + cmd, None)
        if fn is None:
            raise AppError("알 수 없는 명령")
        try:
            r = fn(args or {}, gen)
        except MotionError as e:
            if e.code == "estop":
                raise AppError("비상정지로 중단")
            if e.code == "stop":
                raise AppError("정지로 중단")
            raise AppError(str(e))
        except (PowerError, StageError) as e:
            raise AppError(logger.short(e))
        finally:
            self.touch()
        return r if isinstance(r, dict) else {}

    def _need_stage(self):
        if self.motion is None:
            raise AppError("스테이지 연결 안 됨")

    def _need_camera(self):
        """사진이 필요한 명령은 카메라가 실제로 프레임을 줄 때만(열기가 진행 중이면 잠깐 기다린다). 이동하기 전에 먼저 거절한다."""
        if self.connecting["camera"]:
            raise AppError("카메라 연결 중 · 잠시 뒤")           # 기다리지 않는다 - 몇 초 뒤 갑자기 움직이기 시작하는 일이 없게
        if not (self.vision.cam is not None and self.vision.frame_ok):
            raise AppError("카메라 연결 안 됨 · USB 확인")

    def _no_run(self, what="이 동작"):
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            raise AppError("측정 중 · %s 못 함" % what)
        if self.rt is not None and self.rt.active:
            raise AppError("%s 중 · %s 못 함" % (self.rt_label(), what))

    def rt_label(self):
        """도는 R–T 측정의 이름(사유 문구용): 수동 측정 · R–T 측정 · 속도 측정."""
        r = self.rt
        if r is None:
            return "R–T 측정"
        return "수동 측정" if getattr(r, "manual", False) else ("속도 측정" if r.mode == "speed" else "R–T 측정")

    def manual_active(self):
        r = self.rt
        return bool(r is not None and r.active and getattr(r, "manual", False))

    def _begin(self, name):
        with self.lock:
            if self.busy:
                raise AppError("%s 중 · 끝난 뒤" % self.busy)
            self.busy = name
        self.touch()

    def _end(self):
        self.busy = ""
        self.cap_text = ""
        self.touch()

    # ------------------------------------------------------------------
    # 설정
    # ------------------------------------------------------------------
    LIVE_KEYS = ("motion.", "limits.", "relay.watchdog_ms", "serial.line_timeout_s", "serial.move_timeout_s", "heater.relay")

    def c_settings_save(self, a, gen):
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            raise AppError("측정 중 · 설정 변경 잠김")
        patch = dict(a["patch"])
        if "heater.relay" in patch and int(patch["heater.relay"]) != int(self.cfg.get("heater.relay")):
            if self.power is not None and self.power.owners.get(self.power.heater_n):
                raise AppError("히트건 켜짐 · 끈 뒤 변경")
            if (self.rt is not None and self.rt.active) or self.manual_active():
                raise AppError("R–T 측정 중 · 변경 잠김")
            if a.get("swap_heater") and "cooler.zones" not in patch:      # 고급 설정에서 히트건만 바꿈: 그 릴레이를 쓰던 구역과 맞바꾼다(확인 창 뒤)
                import settings_schema as sch
                z, rl, cell = sch.swap_heater_zone(self.cfg.get("cooler.zones"), patch.get("relay.items") or self.cfg.get("relay.items"),
                                                   int(self.cfg.get("heater.relay")), int(patch["heater.relay"]))
                if cell is not None:
                    patch["cooler.zones"] = z
                    patch["relay.items"] = rl
                    logger.log("히트건 R%d → R%d · 쿨러 구역 칸 %d 를 R%d 로 맞바꿈" % (int(self.cfg.get("heater.relay")), int(patch["heater.relay"]), cell, int(self.cfg.get("heater.relay"))), "warn")
        live = any(k == p or k.startswith(p) for k in patch for p in self.LIVE_KEYS)
        mo = self.motion
        got = False
        if mo is not None and live:
            # 이동 중이면 속도 · 가속 · 가동범위 저장을 거절한다("이동 중 · 끝난 뒤"). 잠금을 잡은 채 저장 · 반영한다(그 사이 이동이 시작되지 않게).
            if self.busy or mo.moving or (self.rt is not None and self.rt.active) or not mo.lock.acquire(blocking=False):
                raise AppError("이동 중 · 끝난 뒤 저장")
            got = True
        try:
            ok, errs, warns = self.cfg.save(patch)
            if not ok:
                k = next(iter(errs))
                raise AppError("%s · %s" % (k, errs[k]))
            if mo is not None and live:                     # 장치에 닿는 값(속도 · 가속 · 가동범위 · 시간 한도 · 통신 감시)이 바뀐 저장만 펌웨어로 보낸다
                try:
                    self._apply_live_cfg(mo)
                except Exception as e:                      # noqa: BLE001 - 반영하지 못했으면 실패로 답한다
                    logger.log("설정 반영 실패 · " + logger.short(e), "err", raw=repr(e))
                    raise AppError("저장했지만 장치에 반영 못 함 · 다시 연결 필요 · " + logger.short(e, 30))
            elif live:                                      # 스테이지 없음: 가동범위 한 값 · 속도는 바로 바꿔 둔다(연결 때 펌웨어 한계로 다시 자른다)
                c = self.cfg
                stage_mod.set_limits(c.get("limits.x_max_mm"), c.get("limits.y_max_mm"), c.get("limits.z_max_mm"))
                stage_mod.configure(speed_pps=c.get("motion.speed_pps"), jog_pps=c.get("motion.jog_pps"), jog_slow_pps=c.get("motion.jog_slow_pps"),
                                    line_timeout_s=c.get("serial.line_timeout_s"), move_timeout_s=c.get("serial.move_timeout_s"))
            if self.power:
                self.power.heater_n = int(self.cfg.get("heater.relay"))
            if self.hub:
                self.hub.configure()
        finally:
            if got:
                mo.lock.release()
        return {"warnings": warns}

    def _apply_live_cfg(self, mo):
        """저장한 속도 · 가속 · 가동범위 · 시간 한도를 드라이버 · 이동 계층에 바로 반영하고 가속 · 시작 속도 · 통신 감시를 펌웨어로 보낸다(다음 이동부터). 이동 잠금을 잡은 채 부른다."""
        c = self.cfg
        stage_mod.set_limits(c.get("limits.x_max_mm"), c.get("limits.y_max_mm"), c.get("limits.z_max_mm"))
        stage_mod.configure(speed_pps=c.get("motion.speed_pps"), jog_pps=c.get("motion.jog_pps"), jog_slow_pps=c.get("motion.jog_slow_pps"),
                            line_timeout_s=c.get("serial.line_timeout_s"), move_timeout_s=c.get("serial.move_timeout_s"))
        self._apply_motion_cfg(mo)
        st = self.stage
        st.speed_pps = None                                 # 다음 이동에서 v 를 다시 보낸다
        st.set_profile(c.get("motion.accel"), c.get("motion.start_pps"))
        st.watchdog(int(c.get("relay.watchdog_ms")))
        clamp = self._clamp_to_firmware(st)
        if clamp:
            self.set_alarm(clamp, "설정에서 가동범위를 확인")

    def c_settings_check(self, a, gen):
        """설정 창의 칸 옆 오류 표시용: 저장하지 않고 검증만."""
        errs, warns = self.cfg.check(a.get("patch") or {})
        return {"errs": errs, "warns": warns}

    def c_settings_import_old(self, a, gen):
        self._no_run()
        if self.motion is not None and (self.motion.moving or self.busy):
            raise AppError("이동 중 · 끝난 뒤")
        path = a.get("path") or os.path.join(paths.OLD_DATA_DIR, "..", "wafer_stage", "settings.json")
        patch, why = store.import_old(os.path.normpath(path))
        if patch is None:
            raise AppError(why)
        ok, errs, _w = self.cfg.save(patch)
        if not ok:
            k = next(iter(errs))
            raise AppError("가져오기 실패 · %s %s" % (k, errs[k]))
        if self.stage is not None:
            with self.link_lock:
                self._reconnect()                            # 가져온 포트 · 가동범위 · 속도로 스테이지를 다시 연결
        return {"imported": sorted(patch)}

    # ------------------------------------------------------------------
    # 촬영 · 검출
    # ------------------------------------------------------------------
    def _mm_px(self):
        c = self.calib
        if c.H is not None:
            return c.local_mm_per_px()
        sz = geom.area_scale(c.area, self.cfg.get("calib.area_size_mm"))
        return sz[0] if sz else 0.143

    def _use_diff(self):
        m = self.ref_meta or {}
        return (m.get("exposure") == self.cfg.get("exposure") and m.get("wb_temperature") == self.cfg.get("wb_temperature"))

    def _goto_capture_xy(self, gen):
        """촬영 위치로(Z 맨 위). 원점이 없으면 이동 없이 그 자리에서."""
        m = self.motion
        if m is None or not self.cfg.get("flow.capture_goto"):
            return
        cx, cy = self.cfg.get("capture_xy")
        if m.z_ok and m.xy_ok and not m.estop_locked:
            self.cap_text = "촬영 위치로 이동"
            self.touch()
            m.move_xy(cx, cy, gen=gen)
        else:
            logger.log("원점 없음 · 이동 없이 이 자리에서 촬영", "warn")

    def _grab(self, gen, n_best=3, poly=None):
        t_after = self.motion.last_move_end if self.motion else 0.0
        self.cap_text = "프레임 받는 중"
        self.touch()
        img, tm = self.vision.frame_after(t_after, n_best=n_best, poly=poly)
        if self.motion and gen != self.motion.gen:
            raise AppError("촬영 중 %s · 결과 버림" % ("비상정지" if self.motion.last_estop_gen > gen else "정지"))
        return img, tm

    def c_capture(self, a, gen):
        self._need_move_opt("촬영")
        self._need_camera()
        self._begin("촬영")
        try:
            if not self.calib.area:
                raise AppError("판 영역 필요 · 장비 설정 3")
            self.cap_error = None
            with (self.motion._locked(True) if self.motion else contextlib.nullcontext()):
                if self.motion and gen != self.motion.gen:
                    raise AppError(self.cancel_text(gen))
                self._goto_capture_xy(gen)
                img, tm = self._grab(gen, n_best=int(self.cfg.get("capture_frames")), poly=self.calib.area)
            self.cap_text = "검출"
            self.touch()
            self._detect_and_store(img)
            return {"n": len(self.samples)}
        except (AppError, MotionError) as e:
            self.cap_error = {"id": time.time(), "text": logger.short(e)}
            raise
        except Exception as e:                             # noqa: BLE001
            self.cap_error = {"id": time.time(), "text": logger.short(e)}
            logger.log("촬영 실패 · " + logger.short(e), "err", raw=repr(e))
            raise AppError("촬영 실패 · " + logger.short(e, 40))
        finally:
            self._end()

    def _detect_and_store(self, img):
        c = self.calib
        sid = _stamp()
        ref = self.ref_img()
        mm_px = self._mm_px()
        params = copy.deepcopy(self.cfg.values)
        r = sense.detect_shot(img, c.area, c.d.get("exclude"), params, mm_px, ref=ref, ref_id=self.ref_id() or "",
                              use_diff=self._use_diff(), log=lambda *m: logger.log(" ".join(str(x) for x in m), "detail"))
        det = r["det"]
        coords_ok = bool(r["coords_ok"]) and c.ready(self.ref_id(), self._size()) and (ref is None or r["A"] is not None)
        if ref is not None and r["A"] is None:
            coords_ok = False
        warns = list(r["warnings"]) + list(det.warnings)
        if not c.ready(self.ref_id(), self._size()):
            warns.append("좌표 보정 필요 · 검출만")
        # 번호: 기준 mm 위치 순서(행 우선)
        tmp = [dict(s, x_px=s.get("ref_u", s["x_px"]), y_px=s.get("ref_v", s["y_px"])) for s in det.samples]
        order_in = geom.order_samples(tmp, c.H if coords_ok else None, (0, 0), mm_px, det.diag, lambda *_: None)
        by_pos = {(round(s["x_px"], 1), round(s["y_px"], 1)): s for s in det.samples}
        samples = []
        for s in order_in:
            src = by_pos.get((round(s["x_px"], 1), round(s["y_px"], 1))) or s
            samples.append(self._mk_sample(s["no"], src, coords_ok and r["A"] is not None or (ref is None and coords_ok), r["A"]))
        self.shot = {"id": sid, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "img": img, "size": (img.shape[1], img.shape[0]),
                     "A": r["A"], "cam": r["cam"], "coords_ok": coords_ok, "warnings": warns, "path": r["path"], "fail": r["fail"],
                     "mm_px": mm_px, "area_cur": r["area_cur"], "ref_id": self.ref_id()}
        self.samples = samples
        self.edits = []
        self.sel = None
        self.next_no = len(samples) + 1
        self._assign_cooler()
        self._save_shot()
        if not coords_ok:
            why = "; ".join(w for w in r["warnings"] if "카메라" in w) or ("좌표 보정 필요" if not c.ready(self.ref_id(), self._size()) else "좌표 못 만듦")
            self.set_alarm(why, "장비 설정에서 확인") if r["cam"] and not r["cam"].get("ok", True) or r["fail"] else None
        elif self.alarm and "카메라 위치" in self.alarm["text"]:
            self.set_alarm(None)
        logger.log("촬영 · 인식 %d · 확인 필요 %d" % (len(samples), sum(1 for s in samples if s["weak"])))

    def _mk_sample(self, no, s, with_xy, A):
        ru, rv = s.get("ref_u"), s.get("ref_v")
        d = {"no": no, "u": s["x_px"], "v": s["y_px"], "ru": ru, "rv": rv, "verts": s.get("vertices_px") or [],
             "source": s.get("source", "detected"), "weak": bool(s.get("weak") or s.get("source") == "predicted"),
             "excluded": False, "manual": False, "X": None, "Y": None, "cooler": None, "cooler_relay": None,
             "w_mm": s.get("w_mm"), "h_mm": s.get("h_mm"), "angle": s.get("angle_deg")}
        if with_xy and ru is not None:
            x, y = self.calib.px_to_mm(ru, rv)
            d["X"], d["Y"] = round(x, 2), round(y, 2)
        return d

    def _recalc_samples(self):
        """좌표 보정이 바뀌었을 때: 기준 픽셀에서 X · Y 와 쿨러 구역을 다시 계산한다."""
        ok = self.calib.ready(self.ref_id(), self._size()) and self.shot is not None and \
            (self.ref_img() is None or self.shot.get("A") is not None)
        for s in self.samples:
            if ok and s.get("ru") is not None and not s.get("from_xy"):
                x, y = self.calib.px_to_mm(s["ru"], s["rv"])
                s["X"], s["Y"] = round(x, 2), round(y, 2)
            elif s.get("from_xy") and ok:
                pass
            else:
                s["X"] = s["Y"] = None
        if self.shot:
            self.shot["coords_ok"] = bool(ok)
        self._assign_cooler()

    def _assign_cooler(self):
        c = self.calib
        if c.H is None or not c.area:
            for s in self.samples:
                s["cooler"] = s["cooler_relay"] = None
            return
        size = c.measured_size_mm() or self.cfg.get("calib.area_size_mm")
        z = self.cfg.get("cooler.zones")
        Hp = geom.plate_homography(c.area, size)
        for s in self.samples:
            if s.get("ru") is None:
                continue
            lx, ly = geom.px_to_mm(Hp, s["ru"], s["rv"])
            cell = zones.cell_at(z, size, lx, ly)
            if cell is not None:
                row, col = cell if isinstance(cell, tuple) else (cell // z["cols"], cell % z["cols"])
                cc = z["cells"][row * z["cols"] + col]
                s["cooler"], s["cooler_relay"] = cc.get("cooler"), cc.get("relay")

    def _save_shot(self):
        sh = self.shot
        d = os.path.join(paths.SHOTS_DIR, sh["id"])
        try:
            os.makedirs(d, exist_ok=True)
            imgio.imwrite_u(os.path.join(d, "raw.png"), sh["img"])
            store.write_json(os.path.join(d, "samples.json"), {"id": sh["id"], "samples": self.samples, "cam": sh["cam"],
                                                             "warnings": sh["warnings"], "ref_id": sh["ref_id"]})
            ann = geom.annotate_fixed(sh["img"], [dict(s, x_px=s["u"], y_px=s["v"], vertices_px=s["verts"]) for s in self.samples])
            imgio.imwrite_u(os.path.join(d, "annotated.jpg"), ann)
        except OSError as e:
            logger.log("촬영 저장 실패 · " + logger.short(e), "warn")

    def _load_last_shot(self):
        try:
            ids = sorted(os.listdir(paths.SHOTS_DIR))
        except OSError:
            return
        for sid in reversed(ids):
            j = store.read_json(os.path.join(paths.SHOTS_DIR, sid, "samples.json"))
            img = imgio.imread_u(os.path.join(paths.SHOTS_DIR, sid, "raw.png"))
            if j and img is not None:
                self.shot = {"id": sid, "time": "%s-%s-%s %s:%s:%s" % (sid[:4], sid[4:6], sid[6:8], sid[9:11], sid[11:13], sid[13:15]),
                             "img": img, "size": (img.shape[1], img.shape[0]), "A": None, "cam": j.get("cam"), "coords_ok": False,
                             "warnings": ["이전 촬영 · 다시 촬영하면 좌표가 맞음"], "path": "?", "fail": None, "mm_px": 0.15,
                             "area_cur": None, "ref_id": j.get("ref_id")}
                self.samples = j["samples"]
                self.next_no = max([s["no"] for s in self.samples] + [0]) + 1
                if self.shot["cam"] and self.shot["cam"].get("M"):
                    self.shot["A"] = frames.Align.from_dict({"M": self.shot["cam"]["M"], "model": self.shot["cam"].get("model", "euclid"),
                                                             "tag": frames.SHOT})
                return
            break

    # ------------------------------------------------------------------
    # 샘플 편집
    # ------------------------------------------------------------------
    def _push_edit(self):
        self.edits.append((copy.deepcopy(self.samples), self.next_no, self.sel))
        del self.edits[:-50]

    def _need_shot(self):
        if not self.shot:
            raise AppError("촬영 결과 없음")
        self._no_run("샘플 편집")

    def c_sample_select(self, a, gen):
        self.sel = a.get("no")

    def _shot_to_ref(self, uv):
        A = self.shot.get("A")
        if A is not None:
            return tuple(A.to_ref([list(uv)])[0])
        if self.ref_img() is None:
            return tuple(uv)
        raise AppError("정렬 없음 · 좌표 변환 불가")

    def c_sample_add(self, a, gen):
        self._need_shot()
        u, v = float(a["u"]), float(a["v"])
        if a.get("snap", True):
            try:
                crop_img = self.shot["img"]
                res = fixed_detect.snap(crop_img, u, v, copy.deepcopy(self.cfg.values), self.shot["mm_px"])
                if res:
                    u, v = float(res["x_px"]), float(res["y_px"])
            except Exception:                              # noqa: BLE001
                pass
        ru, rv = self._shot_to_ref((u, v))
        self._push_edit()
        s = {"no": self.next_no, "u": round(u, 1), "v": round(v, 1), "ru": round(ru, 1), "rv": round(rv, 1), "verts": [], "source": "manual",
             "weak": False, "excluded": False, "manual": True, "X": None, "Y": None, "cooler": None, "cooler_relay": None}
        self.next_no += 1
        self.samples.append(s)
        self.sel = s["no"]
        self._recalc_samples()
        self._save_shot()

    def c_sample_here(self, a, gen):
        """현재 위치를 샘플로: X · Y 가 기준. 픽셀은 X · Y 에서 다시 만든다(기준 -> 촬영 프레임)."""
        self._need_shot()
        self._need_stage()
        c = self.calib
        if not c.ready(self.ref_id(), self._size()):
            raise AppError("좌표 보정 필요 · 장비 설정 5")
        x, y = self.pos["x"], self.pos["y"]
        tl = conditions.active_tool(self.cfg.values)
        x, y = x + float(tl.get("dx_mm") or 0.0), y + float(tl.get("dy_mm") or 0.0)
        ru, rv = c.mm_to_px(x, y)
        A = self.shot.get("A")
        u, v = (A.to_cur([[ru, rv]])[0] if A is not None else (ru, rv))
        self._push_edit()
        s = {"no": self.next_no, "u": round(float(u), 1), "v": round(float(v), 1), "ru": round(ru, 1), "rv": round(rv, 1), "verts": [],
             "source": "manual", "weak": False, "excluded": False, "manual": True, "X": round(x, 2), "Y": round(y, 2), "cooler": None,
             "cooler_relay": None, "from_xy": True}
        self.next_no += 1
        self.samples.append(s)
        self.sel = s["no"]
        self._assign_cooler()
        self._save_shot()

    def _find(self, no):
        for s in self.samples:
            if s["no"] == no:
                return s
        raise AppError("샘플 %s 없음" % no)

    def c_sample_move(self, a, gen):
        """위치 수정."""
        self._need_shot()
        s = self._find(int(a["no"]))
        ru, rv = self._shot_to_ref((float(a["u"]), float(a["v"])))
        self._push_edit()
        s.update(u=round(float(a["u"]), 1), v=round(float(a["v"]), 1), ru=round(ru, 1), rv=round(rv, 1), manual=True, weak=False,
                 verts=[], from_xy=False)
        self._recalc_samples()
        self._save_shot()

    def c_sample_delete(self, a, gen):
        self._need_shot()
        self._find(int(a["no"]))
        self._push_edit()
        self.samples = [s for s in self.samples if s["no"] != int(a["no"])]
        if self.sel == int(a["no"]):
            self.sel = None
        self._save_shot()

    def c_sample_exclude(self, a, gen):
        self._need_shot()
        s = self._find(int(a["no"]))
        self._push_edit()
        s["excluded"] = bool(a.get("on", not s["excluded"]))

    def c_undo_edit(self, a, gen):
        self._need_shot()
        if not self.edits:
            raise AppError("되돌릴 편집 없음")
        self.samples, self.next_no, self.sel = self.edits.pop()
        self._recalc_samples()
        self._save_shot()

    def c_sample_confirm(self, a, gen):
        """확인 필요 해제."""
        s = self._find(int(a["no"]))
        self._push_edit()
        s["weak"] = False

    def c_goto_sample(self, a, gen):
        self._need_move("이동")
        s = self._find(int(a["no"]))
        if s["X"] is None:
            raise AppError("좌표 없음 · 좌표 보정 필요")
        tl = conditions.active_tool(self.cfg.values)
        px, py = s["X"] - float(tl.get("dx_mm") or 0.0), s["Y"] - float(tl.get("dy_mm") or 0.0)
        lm = stage_mod.limits_mm()                          # 가동범위는 한 곳(드라이버의 지금 값 - 설정 + 펌웨어 한계 자르기)
        lx, ly = lm["x_max_mm"], lm["y_max_mm"]
        if not (0 <= px <= lx and 0 <= py <= ly):
            raise AppError("가동범위 밖 · X %.1f Y %.1f" % (px, py))
        self._begin("이동")
        try:
            self.motion.move_xy(px, py, gen=gen, nowait=True)
            st = self.motion.status()
            self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
            self.stop_band = None
        finally:
            self._end()

    # ------------------------------------------------------------------
    # 이동 · 조그 · 회복
    # ------------------------------------------------------------------
    def c_jog(self, a, gen):
        """조그 한 번. 이동 · 작업 중이면 줄 세우지 않고 즉시 거절한다(기존 프로그램과 같다). rep = 누르고 있는 연속 · free = 접촉 높이 화면(Z 아래 한도 없음)."""
        self._need_move("이동", jog=True)
        ax = str(a["axis"]).lower()
        if ax not in ("x", "y", "z"):
            raise AppError("축은 X · Y · Z")
        try:
            mm = float(a["mm"])
        except (TypeError, ValueError):
            raise AppError("거리 값 오류")
        if mm != mm or mm == 0 or abs(mm) == float("inf"):
            raise AppError("거리 값 오류")
        if abs(mm) > float(self.cfg.get("jog.max_mm")):
            raise AppError("한 번 최대 %g mm" % self.cfg.get("jog.max_mm"))
        nohome = self.free_active()
        if nohome:
            logger.log("[원점 무시] 조그 %s %+g mm" % (ax.upper(), mm), "warn")
        self.motion.jog(ax, mm, gen=gen, nowait=True, opts={"rep": bool(a.get("rep")), "free": bool(a.get("free")), "nohome": nohome}, nohome=nohome)
        st = self.motion.status()
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))

    def _jog_guard(self, ax, mm, st, opts, z_ok):
        """조그 규칙(Motion.jog 이 이동 잠금 안에서 부른다). 거절하면 MotionError."""
        c = self.cfg
        cz = c.get("contact_z_mm")
        z = None if st is None else st.get("z_mm")
        nohome = bool(opts.get("nohome"))                   # 원점 없이 이동 허용: Z 높이를 모른다 - 접촉 높이 · 핀 내려감 판정은 하지 않는다(확인 창에서 사람이 받아들임)
        if ax == "z":
            zmax = float(c.get("jog.z_max_mm"))
            if abs(mm) > zmax + 1e-9:
                raise MotionError("Z 는 한 번 최대 %g mm" % zmax, "z_step")
            if opts.get("rep") and mm > 0:
                raise MotionError("Z↓ 는 한 번에 한 스텝", "z_repeat")
            if mm > 0 and cz is not None and z_ok and not opts.get("free") and not nohome:
                lim = float(cz) + float(c.get("jog.z_over_contact_mm"))
                if z is None or z + mm > lim + 1e-9:
                    raise MotionError("접촉 높이 %.2f + %.1f mm 아래로 못 내림" % (float(cz), float(c.get("jog.z_over_contact_mm"))), "z_deep")
        else:
            if opts.get("rep") and abs(mm) > float(c.get("jog.repeat_max_mm")) + 1e-9:
                raise MotionError("연속 조그는 스텝 %g mm 이하만" % float(c.get("jog.repeat_max_mm")), "repeat")
            if cz is not None and z_ok and not nohome:
                if z is None or z > float(cz) - float(c.get("jog.xy_clear_mm")) + 1e-9:
                    raise MotionError("핀이 내려가 있음 · Z 를 올린 뒤", "pin_down")

    def free_active(self):
        """'원점 없이 이동 허용' 이 켜져 있는가. 세 축 원점이 정상이 되면(또는 스테이지가 없으면) 저절로 끈다."""
        if not self.free_move:
            return False
        m = self.motion
        if m is None or (m.z_ok and m.xy_ok and not m.estop_locked):
            self.free_move = False
            logger.log("[원점 무시] %s · 원점 없이 이동 허용 끔" % ("스테이지 연결 없음" if m is None else "세 축 원점 정상"), "warn")
            return False
        return True

    def c_free_move(self, a, gen):
        """'원점 없이 이동 허용' 체크(저장 안 함). 켜면 사람의 수동 이동(조그 · 키보드 · 스텝 · 직접 거리)만 원점 · 위치 신뢰 ·
        비상정지 잠금(회복 순서) · 회복 중 5 mm 한도 · 핀 내려감 판정을 건너뛴다(Motion.jog nohome). 절대 좌표 이동은 그대로 원점을 요구한다."""
        on = bool(a.get("on"))
        if on:
            self._need_stage()
            m = self.motion
            if m.z_ok and m.xy_ok and not m.estop_locked:
                raise AppError("세 축 원점 정상 · 필요 없음")
        if on != self.free_move:
            self.free_move = on
            logger.log("[원점 무시] 원점 없이 이동 허용 %s" % ("켬 · Z 높이 모름 · 비상정지는 그대로" if on else "끔"), "warn")

    def c_move_xy(self, a, gen):
        self._need_move("이동")
        st = self.motion.move_xy(float(a["x"]), float(a["y"]), gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        self.stop_band = None                               # 끊겼던 이동 안내는 새 이동이 끝까지 가면 닫는다

    def c_goto_named(self, a, gen):
        self._need_move("이동")
        key = {"park": "park_xy", "capture": "capture_xy"}[a["name"]]
        x, y = self.cfg.get(key)
        st = self.motion.move_xy(x, y, gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        self.stop_band = None
        if a["name"] == "park":
            self.save_position()                            # 대기 위치에 도착 - 다음 부팅에 원점이 복원되게

    def c_save_here(self, a, gen):
        """지금 위치를 대기 · 촬영 위치로 저장한다. 화면이 좌표를 보내지 않는다 - 서버가 그 순간 ST 로 읽은 위치를 저장한다."""
        self._need_stage()
        key = {"park": "park_xy", "capture": "capture_xy"}.get(a.get("name"))
        if key is None:
            raise AppError("저장할 위치 이름 오류")
        if self.motion.moving or self.busy:
            raise AppError("이동 중 · 끝난 뒤")
        d = self.motion.status()
        self.pos.update(x=d["x_mm"], y=d["y_mm"], z=d.get("z_mm"))
        xy = [round(d["x_mm"], 1), round(d["y_mm"], 1)]
        self.c_settings_save({"patch": {key: xy}}, gen)
        return {"xy": xy}

    def c_z_top(self, a, gen):
        self._need_move("이동")
        st = self.motion.move_z(0.0, gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))

    def c_origin(self, a, gen):
        """원점 잡기(펌웨어 fz): 축 x | y | z. X · Y 는 Z 원점이 있고 Z 가 맨 위일 때만. 펌웨어 위치가 남아 있으면 끝단 5 mm 앞까지 먼저 다가간다."""
        self._need_move("원점 잡기")
        ax = str(a.get("axis", "")).lower()
        if ax not in ("x", "y", "z"):
            raise AppError("축은 X · Y · Z")
        self._begin("원점 잡기")
        try:
            info = self.motion.find_origin(ax, gen=gen, nowait=True)
            logger.log("%s 원점 잡기 완료%s" % (ax.upper(), " · 끝단 앞까지 자동 접근" if info.get("approach") else ""))
        finally:
            self._end()
        self._after_recovery()
        m = self.motion
        if m.z_ok and m.xy_ok and not m.estop_locked and ax in ("x", "y") and self.head.status(self.ref_id(), self._size())[0] == "done" and not self.sim_skip_verify:
            threading.Thread(target=self._verify_after_origin, name="origin-verify", daemon=True).start()
        return {"approach": info.get("approach")}

    sim_skip_verify = False

    def _verify_after_origin(self):
        """X · Y 원점을 fz 로 잡은 뒤 헤드 위치 보정이 있으면 사진으로 위치를 한 번 확인한다(틀린 원점 점검). 차이가 경고 한도를 넘으면 경보 + [적용] · [위치 확인]."""
        try:
            time.sleep(0.5)
            if self.busy or self.run or self.shutting:
                return
            r = self.c_verify_position({}, self.motion.gen)
            if r and r.get("d") is not None and r["d"] > float(self.cfg.get("photohome.warn_mm")):
                self.set_alarm("원점 확인 · 사진 위치와 %.1f mm 차이" % r["d"], "적용 또는 원점 다시 잡기")
        except Exception as e:                              # noqa: BLE001
            logger.log("원점 뒤 위치 확인 못 함 · " + logger.short(e), "detail")

    def _lock_file(self, on):
        """비상정지 잠금을 디스크에 남긴다 - 회복 전에 앱을 껐다 켜도 잠금이 이어진다(펌웨어는 fz · sp 때 다른 축 위치까지 저장해 버린다)."""
        fp = os.path.join(paths.DATA_DIR, "estop.lock")
        try:
            if on:
                with open(fp, "w", encoding="utf-8") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S"))
            elif os.path.exists(fp):
                os.remove(fp)
        except OSError:
            pass

    def _after_recovery(self):
        st = self.motion.status()
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        if not self.motion.recovery_steps():
            self._lock_file(False)
            self.set_alarm(None)
        else:
            self.set_alarm("원점 확인 중", "다음: " + " → ".join(self.motion.recovery_steps()))

    def recovery_actions(self):
        """경보 띠에 넣을 다음 회복 동작(누를 수 있는 버튼). 안내는 전부 버튼이어야 한다."""
        m = self.motion
        if m is None:
            return []
        out = []
        if not m.z_ok:
            out.append({"act": "origin", "args": {"axis": "z"}, "label": "Z 원점 잡기", "tip": self._origin_tip("z"), "confirm": True})
        elif not m.xy_ok or m.estop_locked:
            head = self.head.status(self.ref_id(), self._size())[0] == "done"
            if head and not (m.xy_done["x"] or m.xy_done["y"]):
                vr = getattr(self, "verify_result", None)
                if self.pending_verify or (vr and vr.get("x") is not None and vr.get("gen") == m.gen):
                    out.append({"act": "apply_position", "args": {}, "label": "적용", "tip": "위치 확인으로 잰 X · Y 를 스테이지에 적용합니다."})
                else:
                    out.append({"act": "verify_position", "args": {}, "label": "위치 확인", "tip": "헤드 마커를 사진으로 찍어 실제 X · Y 를 잽니다."})
            for ax in ("x", "y"):
                if not m.xy_done[ax]:
                    out.append({"act": "origin", "args": {"axis": ax}, "label": "%s 원점 잡기" % ax.upper(), "tip": self._origin_tip(ax), "confirm": True})
        return out

    def _origin_tip(self, ax):
        base = "%s 를 끝단 쪽으로 최대 10 mm 밀어 2 mm 물러난 자리를 원점으로 잡고 저장합니다." % ax.upper()
        if self.fw_homed.get(ax):
            return base + " 펌웨어 위치가 남아 있어 끝단 3 mm 앞까지 자동으로 다가간 뒤 잡습니다. 실제 위치가 그보다 7 mm 넘게 멀면 틀린 원점이 잡히니, 끝단 가까이인지 눈으로 확인하십시오."
        return base + " 먼저 조그로 끝단 10 mm 안까지 옮겨 두어야 합니다."

    def band(self):
        """머리줄 아래 띠: {text, next, err, actions}. 경보가 있으면 그것, 없어도 원점 회복이 남았으면 그 버튼을 보인다."""
        m = self.motion
        al = self.alarm
        if al:
            return {"text": al["text"], "next": al.get("next", ""), "err": bool(__import__("re").search("비상정지|오류|재부팅|통신 끊김", al["text"])),
                    "actions": self.alarm_actions()}
        if m is not None and (m.estop_locked or not m.z_ok or not m.xy_ok):
            if m.estop_locked:
                return {"text": "비상정지 잠금", "next": "다음: " + " → ".join(m.recovery_steps()), "err": True, "actions": self.recovery_actions()}
            first = self.eeprom != "restored"
            return {"text": "원점 없음" if first else "원점 다시 잡기", "next": "다음: " + " → ".join(m.recovery_steps()),
                    "err": False, "actions": self.recovery_actions()}
        sb = self.stop_band
        if sb is not None and m is not None and not (self.run and self.run.state in ("running", "paused")):
            acts = []
            if self.head.status(self.ref_id(), self._size())[0] == "done":
                acts.append({"act": "stop_verify", "args": {}, "label": "위치 확인", "tip": "헤드 마커를 사진으로 찍어 실제 X · Y 를 재고 펌웨어 위치와 비교합니다."})
            vr = sb.get("verify")
            if vr:
                acts.insert(0, {"act": "apply_position", "args": {}, "label": "적용", "tip": "위치 확인으로 잰 X · Y 를 스테이지에 적용합니다."})
            acts.append({"act": "stop_band_close", "args": {}, "label": "닫기", "tip": "이 안내를 닫습니다(위치는 그대로)."})
            return {"text": sb["text"], "next": "", "err": False, "actions": acts, "stop": True}
        return None

    def alarm_actions(self):
        """모든 경보에 다음 동작 버튼을 붙인다 - 막다른 상태 없음."""
        al = self.alarm
        if not al:
            return self.recovery_actions()
        acts = self.recovery_actions()
        if acts:
            return acts
        t = al.get("text", "")
        pv = self.pending_verify
        if pv and pv.get("x") is not None:
            return [{"act": "apply_position", "args": {}, "label": "적용", "tip": "위치 확인으로 잰 X · Y 를 스테이지에 적용합니다."},
                    {"act": "verify_position", "args": {}, "label": "위치 확인", "tip": "헤드 마커를 다시 찍어 실제 X · Y 를 잽니다."},
                    {"act": "alarm_clear", "args": {}, "label": "확인", "tip": "경보를 닫습니다(위치는 그대로)."}]
        if t.startswith("정지 전송") or t.startswith("비상정지 전송"):
            return [{"act": "estop", "args": {}, "label": "비상정지", "tip": "비상정지를 보냅니다(정지가 전해지지 않았습니다)."},
                    {"act": "reconnect", "args": {}, "label": "다시 연결", "tip": "스테이지를 다시 연결합니다(보드가 다시 시작해 모터가 멈춥니다)."}]
        if t.startswith("통신 끊김"):
            return [{"act": "link_check", "args": {}, "label": "연결 확인", "tip": "스테이지에 상태를 물어 응답이 오면 경보를 닫습니다."},
                    {"act": "reconnect", "args": {}, "label": "다시 연결", "tip": "스테이지를 다시 연결합니다."}]
        if t.startswith("측정 오류"):
            return [{"act": "alarm_clear", "args": {}, "label": "확인", "tip": "경보를 닫습니다. 결과 탭에서 사유를 볼 수 있습니다."}]
        if "히트건 끄기 미확인" in t:
            return [{"act": "heater_off_retry", "args": {}, "label": "히트건 끄기 다시", "tip": "히트건 릴레이를 끄고 꺼졌는지 확인합니다."}]
        if "카메라 위치" in t or "좌표" in t:
            return [{"act": "capture", "args": {}, "label": "다시 촬영", "tip": "지금 자리에서 다시 촬영하고 인식합니다."}]
        if "스테이지 연결" in t:
            return [{"act": "reconnect", "args": {}, "label": "다시 연결", "tip": "스테이지를 다시 연결합니다."}]
        return [{"act": "alarm_clear", "args": {}, "label": "확인", "tip": "경보를 닫습니다."}]

    def c_stop_band_close(self, a, gen):
        self.stop_band = None

    def c_stop_verify(self, a, gen):
        """정지로 끊긴 이동 뒤 위치 확인: 사진 위치와 펌웨어 위치의 차이가 허용 안이면 띠를 닫고 로그, 넘으면 띠에 차이와 [적용]."""
        self._need_stage()
        sb = self.stop_band
        r = self.c_verify_position({}, gen)
        if not r or r.get("d") is None:
            return {}
        d = float(r["d"])
        tol = float(self.cfg.get("photohome.tol_mm") if self.cfg.get("photohome.tol_mm") is not None else 0.3)
        if d <= tol:
            self.stop_band = None
            logger.log("위치 확인 · 차이 %.1f mm · 정상" % d)
            self.notes.append("위치 확인 · 차이 %.1f mm · 정상" % d)
        elif sb is not None:
            sb["text"] = "사진 위치와 %.1f mm 차이" % d
            sb["verify"] = True
        return {"d": d}

    def c_alarm_clear(self, a, gen):
        if self.bang_fail:
            raise AppError("%s 전송 중 · 모터 전원을 끄십시오" % self.bang_fail)
        if self.motion and self.recovery_actions():
            raise AppError("회복이 남음 · " + " → ".join(self.motion.recovery_steps()))
        self.set_alarm(None)

    def c_link_check(self, a, gen):
        self._need_stage()
        d = self.stage.status(timeout=2.0)
        self.pos.update(x=d["x_mm"], y=d["y_mm"], z=d.get("z_mm"))
        self._comm_ok()
        self.set_alarm(None)

    def c_heater_off_retry(self, a, gen):
        self._need_stage()
        self.power.heater_off()
        self.set_alarm(None)

    def c_reconnect(self, a, gen):
        self._no_run("다시 연결")
        r = self.link_stage("manual")                       # 진행 중이면 두 번 열지 않고 "연결 중" 으로 답한다
        if r is None:
            return {"connecting": True}
        self.set_alarm(None)

    def c_ports(self, a, gen):
        """시리얼 포트 목록(COM 번호 · 설명 · Arduino 여부). 목록만 읽는다 - 포트를 열지 않는다. 시뮬레이션은 가짜 목록."""
        cur = self.cfg.get("serial_port")
        if self.sim:
            items = [] if "stage" in self.sim_missing else [{"device": "COM3", "desc": "Arduino Mega 2560 (COM3)", "arduino": True}, {"device": "COM5", "desc": "표준 Bluetooth 직렬 링크 (COM5)", "arduino": False}]
        else:
            items = []
            for p in stage_mod.list_ports():
                txt = ((p.description or "") + " " + (p.manufacturer or "")).lower()
                items.append({"device": str(p.device), "desc": str(p.description or p.device), "arduino": "arduino" in txt or "ch340" in txt or "usb serial" in txt})
        return {"ports": items, "selected": cur, "connected": bool(self.stage is not None)}

    def c_port_connect(self, a, gen):
        """고른 포트로 저장하고 연결한다(설정 키 serial_port 는 그대로). 이동 · 측정 중에는 거절."""
        self._no_run("포트 바꾸기")
        port = str(a.get("port") or "").strip()
        if not port:
            raise AppError("포트를 고르십시오")
        if self.busy:
            raise AppError("%s 중 · 끝난 뒤" % self.busy)
        if self.motion is not None and self.motion.moving:
            raise AppError("이동 중 · 끝난 뒤")
        if port != self.cfg.get("serial_port"):
            ok, errs, _w = self.cfg.save({"serial_port": port})
            if not ok:
                raise AppError("포트 저장 실패 · " + next(iter(errs.values())))
            if self.stage is not None:
                self.c_reconnect({}, gen)                       # 이미 연결돼 있으면 새 포트로 다시 연다
                return {"port": port}
        return self.c_reconnect({}, gen) or {"port": port}

    def c_cam_scan(self, a, gen):
        """[카메라 찾기]: 번호 0–5 를 차례로 열어 본다(백그라운드). 시뮬레이션은 가짜 목록 - 실제 카메라를 열지 않는다."""
        if self.busy:
            raise AppError("%s 중 · 끝난 뒤" % self.busy)
        if self.cam_scan and self.cam_scan.get("running"):
            return {}
        self._cam_step, self._cam_next = 0, 0.0
        self.cam_scan = {"running": True, "items": []}
        self.cam_thumbs = {}
        threading.Thread(target=self._cam_scan, name="cam-scan", daemon=True).start()
        self.touch()
        return {}

    def _cam_scan(self):
        items = []
        try:
            cur = int(self.cfg.get("camera_index"))
            for idx in range(6):
                if self.shutting:
                    break
                if idx == cur and self.vision.cam is not None and self.vision.frame_ok:
                    it = {"index": idx, "ok": True, "size": list(self.vision.size or []), "busy": True}
                    if self.vision.preview_jpg:
                        self.cam_thumbs[idx] = self._thumb_from_jpg(self.vision.preview_jpg)
                elif self.sim:
                    ok = idx == 1 and not self.vision.missing
                    it = {"index": idx, "ok": ok, "size": [int(os.environ.get("WAFER2_SYNTH_W", "1280")), int(os.environ.get("WAFER2_SYNTH_H", "960"))] if ok else None, "busy": False}
                    if ok:
                        self.cam_thumbs[idx] = self._thumb_from_jpg(self.vision.preview_jpg) if self.vision.preview_jpg else self._thumb_blank()
                else:
                    it = self._probe_cam(idx)
                it["thumb"] = idx in self.cam_thumbs
                items.append(it)
                self.cam_scan = {"running": True, "items": list(items)}
                self.touch()
        except Exception as e:                              # noqa: BLE001
            logger.log("카메라 찾기 오류 · " + logger.short(e), "detail", raw=repr(e))
        finally:
            self.cam_scan = {"running": False, "items": items}
            self.touch()

    @staticmethod
    def _thumb_from_jpg(jpg):
        """미리보기 JPEG 를 작은 사진(가로 200)으로."""
        try:
            img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
            return vision_mod.jpeg(img, 200, 70)
        except Exception:                                   # noqa: BLE001
            return None

    @staticmethod
    def _thumb_blank():
        img = np.full((150, 200, 3), 90, np.uint8)
        return vision_mod.jpeg(img, 200, 70)

    def _probe_cam(self, idx):
        """번호 하나를 잠깐 열어 프레임이 오는지 본다(실장비 전용)."""
        cap = None
        try:
            cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW)
            if not cap.isOpened():
                return {"index": idx, "ok": False, "size": None, "busy": False}
            ok, fr = cap.read()
            if not ok or fr is None:
                return {"index": idx, "ok": False, "size": None, "busy": False}
            self.cam_thumbs[idx] = vision_mod.jpeg(fr, 200, 70)
            return {"index": idx, "ok": True, "size": [int(fr.shape[1]), int(fr.shape[0])], "busy": False}
        except Exception:                                   # noqa: BLE001
            return {"index": idx, "ok": False, "size": None, "busy": False}
        finally:
            if cap is not None:
                try:
                    cap.release()
                except Exception:                           # noqa: BLE001
                    pass

    def c_cam_reconnect(self, a, gen):
        """[다시 연결](카메라): 지금 열어 본다. 실패하면 사유를 한 줄로."""
        if self.busy in ("촬영", "빈 판 사진", "위치 확인"):
            raise AppError("%s 중 · 끝난 뒤" % self.busy)
        self._cam_step, self._cam_next = 0, 0.0              # 사용자가 눌렀으니 자동 간격은 다시 10 s 부터
        try:
            r = self.link_camera("manual")
        except Exception:                                   # noqa: BLE001
            raise AppError("카메라 연결 안 됨 · " + self.dev_err.get("camera", "USB 확인"))
        if r is None:
            return {"connecting": True}

    # ------------------------------------------------------------------
    # 장치 상태 · 자동 다시 연결
    # ------------------------------------------------------------------
    def _log_link(self, key, msg, lv="info"):
        """연결 로그는 자동 시도마다 쓰지 않고 내용이 바뀔 때만 한 줄."""
        if self._link_log.get(key) != msg:
            self._link_log[key] = msg
            logger.log(msg, lv)

    def link_stage(self, mode="auto"):
        """스테이지 연결 · 다시 연결의 유일한 입구(잠금 하나). mode = start | auto | manual.
        반환: True 연결 / False 실패(auto) / None 다른 연결이 진행 중이라 건너뜀. 수동 실패는 예외. 포트를 두 번 열지 않는다."""
        if not self.link_lock.acquire(blocking=False):
            if mode == "manual":
                self.manual["stage"] = True
                self.touch()
            return None
        manual = mode == "manual"
        self.connecting["stage"] = True
        self.manual["stage"] = manual
        self._stage_try_t = time.monotonic()
        self.touch()
        try:
            if self.stage is None:
                self.connect()
            else:
                self._reconnect()
            self.dev_err.pop("stage", None)
            self._link_log.pop("stage_err", None)
            self._log_link("stage", "스테이지 연결됨" if mode == "start" else "스테이지 다시 연결됨")
            self._link_log.pop("stage", None) if mode != "start" else None            # 다음에 또 끊겼다 붙으면 다시 소식
            return True
        except Exception as e:                              # noqa: BLE001
            self.dev_err["stage"] = logger.short(e, 30)
            self._log_link("stage_err", "스테이지 연결 못 함 · " + logger.short(e), "warn")
            if manual:
                raise
            return False
        finally:
            self.attempted["stage"] = True
            self.connecting["stage"] = False
            self.manual["stage"] = False
            self.link_lock.release()
            self.touch()

    def try_connect_stage(self, raise_err=False):
        return self.link_stage("start" if not raise_err else "manual")

    def link_camera(self, mode="auto"):
        """카메라 열기(자동 시도와 사용자 동작이 겹치지 않게 잠금 하나). 반환 True / False / None(진행 중)."""
        v = self.vision
        if not self.cam_lock.acquire(blocking=False):
            if mode == "manual":
                self.manual["camera"] = True
                self.touch()
            return None
        manual = mode == "manual"
        self.connecting["camera"] = True
        self.manual["camera"] = manual
        self.touch()
        try:
            v.open()
            v.start_preview()
            self.dev_err.pop("camera", None)
            self._link_log.pop("camera_err", None)
            self._cam_step = 0
            self._log_link("camera", "카메라 연결됨" if mode == "start" else "카메라 다시 연결됨")
            self._link_log.pop("camera", None) if mode != "start" else None
            return True
        except Exception as e:                              # noqa: BLE001
            self.dev_err["camera"] = logger.short(e, 30)
            self._log_link("camera_err", "카메라 열기 못 함 · " + logger.short(e), "warn")
            v._release()
            if manual:
                raise
            return False
        finally:
            v.attempted = True
            self.connecting["camera"] = False
            self.manual["camera"] = False
            self.cam_lock.release()
            self.touch()

    def try_open_camera(self):
        return bool(self.link_camera("start"))

    def start_auto_link(self):
        if self._auto_th is None or not self._auto_th.is_alive():
            self._auto_th = threading.Thread(target=self._auto_link, name="auto-link", daemon=True)
            self._auto_th.start()

    def _idle_for_link(self):
        r = self.run
        if self.shutting or self.busy or self.reconnecting or (r and r.state in ("ready", "running", "paused", "stopping")):
            return False
        if self.rt is not None and self.rt.active:                 # 자동 · 수동 R–T: 릴레이를 쓰는 중에는 다시 연결하지 않는다
            return False
        return not (self.motion is not None and self.motion.moving)

    def _port_present(self):
        """설정 포트가 지금 포트 목록에 있는가(목록만 본다 - 열지 않는다). 시뮬레이션은 장치 없음 스위치."""
        if self.sim:
            return "stage" not in self.sim_missing
        try:
            devs = {str(p.device).upper() for p in stage_mod.list_ports()}
        except Exception:                                   # noqa: BLE001
            return True                                     # 목록을 못 읽으면 "있다" 로 본다(거짓 뽑힘 경보를 만들지 않는다)
        want = str(self.cfg.get("serial_port") or "").upper()
        return (want in devs) if want else bool(devs)

    @staticmethod
    def _is_io_error(e):
        n = " ".join(c.__name__ for c in type(e).__mro__) + " " + str(e)
        return isinstance(e, OSError) or "SerialException" in n or "쓰기 실패" in n or "ClearCommError" in n

    def _auto_link(self):
        """백그라운드: 스테이지 포트가 다시 생기거나 카메라가 다시 열리면 자동으로 연결한다. 측정 · 이동 중에는 하지 않는다.
        화면 상태는 바꾸지 않는다(연결 안 됨 그대로) - 깜빡임 없음. 열기가 오래 걸려도 이 스레드만 기다린다."""
        while not self.shutting:
            time.sleep(AUTO_LINK_S)
            try:
                self._auto_once()
            except Exception as e:                          # noqa: BLE001
                logger.log("자동 다시 연결 오류 · " + logger.short(e), "detail", raw=repr(e))

    def _auto_once(self):
        now = time.monotonic()
        # ---- 스테이지
        if self.stage is None:
            present = self._port_present()
            due = present and ((not self._port_prev) or now - self._stage_try_t >= STAGE_RETRY_S)
            self._port_prev = present
            if due and self._idle_for_link():
                self.link_stage("auto")
        elif self.motion is not None:
            present = self._port_present()
            lost = bool(self.alarm and self.alarm["text"].startswith("통신 끊김"))
            if not present and not self._port_gone:                  # 쓰는 중 포트가 목록에서 사라짐 = 통신 끊김(빨강 경보)
                self._port_gone = True
                if not lost:
                    self._link_alarmed = True
                    self._on_link_lost("응답 없음 · 포트 사라짐")
            elif present and self._port_gone and self._idle_for_link():       # 다시 꽂힘 → 새로 연다(옛 핸들은 죽었다 · 아두이노는 재부팅된 것으로 본다)
                if self.link_stage("auto"):
                    self._port_gone = False
                    self._after_relink()
            elif lost and present and self._idle_for_link() and not self.motion.moving:
                try:                                        # 잠깐 멈춘 것: 응답이 돌아오면 경보만 닫는다
                    d = self.stage.status(timeout=2.0)
                    self.pos.update(x=d["x_mm"], y=d["y_mm"], z=d.get("z_mm"))
                    self._comm_ok()
                    self.set_alarm(None)
                    self._log_link("stage_back", "스테이지 통신 돌아옴")
                    self._link_log.pop("stage_back", None)
                except Exception as e:                      # noqa: BLE001
                    if self._is_io_error(e) and self.link_stage("auto"):          # 입출력 오류 = 핸들이 죽었다 → 새로 연다
                        self._after_relink()
        # ---- 카메라: 10 s → 20 s → 30 s 로 늘려 가며
        if self.vision.cam is None and now >= self._cam_next and self._idle_for_link():
            ok = self.link_camera("auto")
            if ok is False:
                self._cam_step = min(self._cam_step + 1, 2)
                self._cam_next = time.monotonic() + CAM_BASE_S * (1 + self._cam_step)
            elif ok:
                self._cam_next = 0.0

    def _after_relink(self):
        """다시 연결한 뒤: 경보를 닫는다(재부팅 · 원점 규칙은 connect() 가 이미 적용). 움직이지 않는다."""
        if self.alarm and self.alarm["text"].startswith("통신 끊김"):
            self.set_alarm(None)

    def devices(self):
        """장치 상태 넷: ok(연결됨) · down(연결 안 됨) · retry(연결 중 - 사용자가 [다시 연결]을 눌렀을 때만) · unknown(확인 안 됨).
        실제 장치 상태에서만 만든다. 뒤에서 하는 자동 시도는 상태를 바꾸지 않는다(깜빡임 없음)."""
        m, v = self.motion, self.vision
        if m is None:
            if self.reconnecting or self.manual["stage"]:
                st = {"state": "retry", "text": "연결 중"}
            elif not self.attempted["stage"]:
                st = {"state": "unknown", "text": "확인 안 됨"}
            else:
                st = {"state": "down", "text": "연결 안 됨", "why": self.dev_err.get("stage", "")}
        elif self.alarm and self.alarm["text"].startswith("통신 끊김"):
            st = {"state": "down", "text": "통신 끊김", "why": ""}
        else:
            st = {"state": "ok", "text": "연결"}
        if v.cam is not None and v.frame_ok:
            cam = {"state": "ok", "text": "연결"}
        elif self.manual["camera"]:
            cam = {"state": "retry", "text": "연결 중"}
        elif not v.attempted:
            cam = {"state": "unknown", "text": "확인 안 됨"}
        else:
            cam = {"state": "down", "text": "연결 안 됨", "why": self.dev_err.get("camera") or v.last_error}
        out = {"stage": st, "camera": cam}
        for nm in ("k2110", "k2450"):
            d, info = getattr(self, nm), self.inst_info.get(nm)
            if info and info.get("ok") is False:
                out[nm] = {"state": "down", "text": "연결 안 됨", "why": info.get("err", "")}
            elif d is not None and d.is_open:
                out[nm] = {"state": "ok", "text": "연결"}
            elif info and not info.get("ok"):
                out[nm] = {"state": "down", "text": "연결 안 됨", "why": info.get("err", "")}
            else:
                out[nm] = {"state": "unknown", "text": "확인 안 됨"}
        return out

    def bands(self, dev=None):
        """머리줄 아래 띠 목록. 맨 위 = 비상정지 · 통신 끊김 · 원점 회복(경보, 빨강), 그 아래 장치마다 한 줄(스테이지 · 카메라 · K2110 · K2450, 주황)."""
        out = []
        if self.free_active():                                # 원점 없이 이동 허용: 머리줄 바로 아래 붉은 띠(맨 위)
            out.append({"id": "free", "text": "원점 없이 이동 중 · Z 높이 모름", "next": "핀이 내려가 있으면 X · Y 이동이 샘플을 긁을 수 있음", "err": True,
                        "actions": [{"act": "free_move", "args": {"on": False}, "label": "끄기", "tip": "원점 없이 이동 허용을 끕니다(원점이 있어야 조그 · 이동)."}]})
        b = self.band()
        if b:
            out.append(dict(b, id="alarm"))
        dev = dev or self.devices()

        def go(step):
            return {"act": "ui_setup", "args": {"step": step}, "label": {1: "스테이지 설정", 2: "카메라 설정", 8: "측정 장비 설정"}.get(step, "장비 설정 %d" % step), "tip": "장비 설정의 해당 단계를 엽니다."}

        def re_btn(act, busy, tip):
            return {"act": act, "args": {}, "label": "연결 중…" if busy else "다시 연결", "busy": bool(busy), "tip": tip}
        s = dev["stage"]
        if s["state"] in ("down", "retry") and s["text"] != "통신 끊김":
            busy = s["state"] == "retry"
            out.append({"id": "stage", "text": "스테이지 " + ("연결 중" if busy else "연결 안 됨"), "next": "" if busy else "포트 번호 확인", "err": False,
                        "actions": [re_btn("reconnect", busy, "스테이지 포트를 다시 열어 봅니다."), go(1)]})
        c = dev["camera"]
        if c["state"] in ("down", "retry"):
            busy = c["state"] == "retry"
            out.append({"id": "camera", "text": "카메라 " + ("연결 중" if busy else "연결 안 됨"), "next": "" if busy else "카메라 번호 확인", "err": False,
                        "actions": [re_btn("cam_reconnect", busy, "카메라를 다시 열어 봅니다."), go(2)]})
        downs = [(nm, label) for nm, label in (("k2110", "K2110"), ("k2450", "K2450")) if dev[nm]["state"] == "down"]
        inst_act = [{"act": "inst_connect", "args": {}, "label": "측정 장비 연결", "tip": "두 장비에 다시 연결합니다. 출력은 켜지 않습니다."}, go(8)]
        if len(downs) == 2:                                   # 둘 다 안 되면 한 줄
            drv = any("드라이버" in (dev[nm].get("why") or "") for nm, _l in downs)
            out.append({"id": "inst", "text": "측정 장비 연결 안 됨 · K2110 · K2450", "next": "NI-VISA 설치 확인" if drv else "주소 · 케이블 확인", "err": False, "actions": inst_act})
        for nm, label in downs if len(downs) == 1 else []:
            drv = "드라이버" in (dev[nm].get("why") or "")
            out.append({"id": nm, "text": label + " 연결 안 됨", "next": "NI-VISA 설치 확인" if drv else "주소 · 케이블 확인", "err": False, "actions": inst_act})
        return out

    # ------------------------------------------------------------------
    # 위치 확인 (헤드 마커)
    # ------------------------------------------------------------------
    def _head_quad(self, img):
        """사진 안의 헤드 마커를 넉넉히(1.5배 + 여유) 키운 사각형 4점. 없으면 None."""
        h = geom.detect_head(img, {"marker_id": self.cfg.get("photohome.marker_id"), "dict": self.cfg.get("photohome.dict")})
        return None if h is None else align_mod.head_box(h[2])

    def _ref_head_quad(self):
        """빈 판 사진 안의 헤드 마커 상자(빈 판 사진마다 한 번만 찾는다)."""
        rid = self.ref_id()
        if self._head_ref_quad[0] != rid:
            ref = self.ref_img()
            self._head_ref_quad = (rid, None if ref is None else self._head_quad(ref))
        return self._head_ref_quad[1]

    def _covers_plate(self, quad):
        """헤드 마커 상자가 판 영역과 겹치는가(겹치면 헤드가 판을 가린다)."""
        if quad is None or not self.calib.area:
            return False
        a = np.asarray(self.calib.area, np.float32).reshape(-1, 2)
        ok, inter = cv2.intersectConvexConvex(cv2.convexHull(a), cv2.convexHull(np.asarray(quad, np.float32).reshape(-1, 2)))
        return bool(ok and ok > 1.0)

    def _align_snap(self, img, head_quad="auto"):
        """촬영 사진과 빈 판 사진 정렬 -> (Align | None, 문구). 헤드 마커 상자(양쪽 사진)는 판 마스크에서 뺀다."""
        ref = self.ref_img()
        if ref is None or not self.calib.area:
            return None, "빈 판 사진 없음"
        q = self._head_quad(img) if isinstance(head_quad, str) else head_quad
        ar = align_mod.align(ref, img, self.calib.area, ref_id=self.ref_id() or "", tag=frames.SNAP,
                             mask_cur=[q] if q is not None else None, mask_ref=[self._ref_head_quad()] if self._ref_head_quad() is not None else None)
        if not ar["ok"]:
            if q is not None and self._covers_plate(q):
                return None, "헤드가 판을 가림 · 판 밖으로 조그 뒤 다시"
            return None, "정렬 실패 · " + ar["msg"]
        return ar["A"], ""

    def _head_px_ref(self, img):
        ph = {"marker_id": self.cfg.get("photohome.marker_id"), "dict": self.cfg.get("photohome.dict")}
        h = geom.detect_head(img, ph)
        if h is None:
            return None, None, "헤드 마커 안 보임"
        size = (img.shape[1], img.shape[0])
        if geom.touches_edge(h[2], size, geom.edge_margin_px(self.cfg.get("photohome"), size)):
            return None, None, "헤드 마커가 화면 가장자리에 걸침"
        A, why = self._align_snap(img, align_mod.head_box(h[2]))
        if A is None:
            return (h[0], h[1]), None, why
        ru, rv = A.to_ref([[h[0], h[1]]])[0]
        return (h[0], h[1]), (float(ru), float(rv)), ""

    def c_verify_position(self, a, gen):
        """위치 확인: 지금 자리에서 한 장 -> 헤드 마커 -> 기준 프레임 -> 헤드 위치 보정 -> 실제 X · Y."""
        self._need_move("위치 확인")
        self._need_camera()
        self._begin("위치 확인")
        try:
            with self.motion.lock:
                self.motion._enter(gen)
                with self.motion.op(gen):
                    pre = self.motion.stage.status()
                pre_xy = (pre["x_mm"], pre["y_mm"])
                img, tm = self._grab(gen, n_best=1)
            hq = geom.detect_head(img, {"marker_id": self.cfg.get("photohome.marker_id"), "dict": self.cfg.get("photohome.dict")})
            self.snap = {"img": img, "A": None, "id": _stamp(), "t": tm, "why": "",
                         "head_box": None if hq is None else [[round(float(x), 1), round(float(y), 1)] for x, y in align_mod.head_box(hq[2])]}
            _px, rp, why = self._head_px_ref(img)
            if self.head.status(self.ref_id(), self._size())[0] != "done":
                raise AppError("헤드 위치 보정 필요 · 장비 설정 6")
            if rp is None:
                self.pending_verify = None
                self.verify_result = {"verdict": "못 함", "why": why, "box": self.snap.get("head_box"), "snap": self.snap["id"], "d": None, "gen": gen}
                raise AppError("위치 확인 못 함 · " + why + (" · 자동 적용 안 함" if why.startswith("정렬") or "빈 판" in why else ""))
            X, Y = self.head.predict(*rp)
            d = float(np.hypot(X - pre_xy[0], Y - pre_xy[1]))
            tol, warn = float(self.cfg.get("photohome.tol_mm")), float(self.cfg.get("photohome.warn_mm"))
            res = {"x": round(X, 2), "y": round(Y, 2), "fw_x": round(pre_xy[0], 2), "fw_y": round(pre_xy[1], 2), "d": round(d, 2),
                   "gen": gen, "pre_xy": pre_xy, "t": time.time(), "box": self.snap.get("head_box"), "snap": self.snap["id"]}
            if d <= tol:
                res["verdict"] = "정상"
                self.pending_verify = None
            else:
                res["verdict"] = "자동 적용" if d <= warn else "확인 필요"
                self.pending_verify = res
                if d <= warn and not self.motion.estop_locked and self.motion.z_ok:
                    pass
            self.verify_result = res
            logger.log("위치 확인 · 차이 %.2f mm · %s" % (d, res["verdict"]))
            return res
        finally:
            self._end()

    def c_apply_position(self, a, gen):
        """위치 확인 결과 적용(sp) - 회복의 마지막 단계. 촬영 사이 비상정지 · 위치 변경이면 거절."""
        self._need_stage()
        pv = self.pending_verify or getattr(self, "verify_result", None)
        if not pv or pv.get("x") is None:
            raise AppError("적용할 위치 확인 결과 없음")
        self.motion.apply_position(pv["x"], pv["y"], expect_gen=pv["gen"], expect_xy=pv["pre_xy"], gen=gen)
        self.pending_verify = None
        self.verify_result = None
        self._after_recovery()
        return {"x": pv["x"], "y": pv["y"]}

    # ------------------------------------------------------------------
    # 빈 판 사진 · 판 영역
    # ------------------------------------------------------------------
    def c_ref_capture(self, a, gen):
        """빈 판 사진: 항상 촬영 위치(Z 맨 위)로 자동 이동한 뒤 찍는다. 교체면 옛 -> 새 정렬 뒤 기하를 옮긴다."""
        self._need_move_opt("빈 판 사진")
        self._need_camera()
        self._begin("빈 판 사진")
        try:
            with (self.motion._locked(True) if self.motion else contextlib.nullcontext()):
                if self.motion:
                    self.motion._enter(gen)
                    cx, cy = self.cfg.get("capture_xy")
                    if not (self.motion.z_ok and self.motion.xy_ok and not self.motion.estop_locked):
                        raise AppError("원점 필요 · 촬영 위치로 못 감")
                    self.cap_text = "촬영 위치로 이동"
                    self.touch()
                    self.motion.move_xy(cx, cy, gen=gen, nowait=True)
                img, tm = self._grab(gen, n_best=int(self.cfg.get("capture_frames")), poly=self.calib.area)
            return self._install_ref(img)
        finally:
            self._end()

    def _install_ref(self, new_img, new_id=None, from_history=False):
        old = self.ref_img()
        new_id = new_id or _stamp()
        size = (new_img.shape[1], new_img.shape[0])
        A = None
        if self.calib.area and (old is not None or (self.snap and self.snap.get("img") is not None)):
            base = old if old is not None else self.snap["img"]
            qb, qn = self._head_quad(base), self._head_quad(new_img)
            ar = align_mod.align(base, new_img, self.calib.area, ref_id=new_id,
                                 mask_cur=[qn] if qn is not None else None, mask_ref=[qb] if qb is not None else None)
            if not ar["ok"]:
                raise AppError("교체 거절 · 정렬 실패 (%s)" % ar["msg"][:28])
            A = ar["A"]
            moved = {"shift_px": round(A.shift_px(np.mean(self.calib.area, axis=0)), 1), "resid": round(ar["resid"], 2), "model": A.model}
        else:
            moved = None
        self._archive_current_ref()
        imgio.imwrite_u(paths.REF_PATH, new_img)
        self.ref_meta = {"id": new_id, "time": time.strftime("%Y-%m-%d %H:%M:%S"), "size": list(size), "camera_index": self.cfg.get("camera_index"),
                         "exposure": self.cfg.get("exposure"), "wb_temperature": self.cfg.get("wb_temperature")}
        store.write_json(paths.REF_META_PATH, self.ref_meta)
        self._ref_img = new_img
        if A is not None:
            why = self.calib.replace_ref(A, new_id, size)
            self.head.replace_ref(A, new_id)
            if why:
                self.calib_msgs.append("좌표 보정 다시 풂 실패 · " + why)
        elif self.calib.area:
            self.calib.d["ref_id"] = new_id
            self.calib.d["size"] = list(size)
            self.calib.save()
        self.shot = self.shot if self.shot and self.shot.get("ref_id") == new_id else self.shot
        self._recalc_samples()
        self._prune_history()
        res = {"id": new_id, "moved": moved}
        logger.log("빈 판 사진 교체" + ("" if not moved else " · 이동 %(shift_px)s px · 잔차 %(resid)s · %(model)s" % moved))
        return res

    def _archive_current_ref(self):
        if not (self.ref_meta and os.path.exists(paths.REF_PATH)):
            return
        os.makedirs(paths.REF_HISTORY_DIR, exist_ok=True)
        base = os.path.join(paths.REF_HISTORY_DIR, self.ref_meta["id"])
        try:
            shutil.copy2(paths.REF_PATH, base + ".png")
            store.write_json(base + ".json", self.ref_meta)
        except OSError:
            pass

    def _prune_history(self):
        try:
            names = sorted({f.split(".")[0] for f in os.listdir(paths.REF_HISTORY_DIR)})
            for n in names[:-10]:
                for ext in (".png", ".json"):
                    try:
                        os.remove(os.path.join(paths.REF_HISTORY_DIR, n + ext))
                    except OSError:
                        pass
        except OSError:
            pass

    def ref_history(self):
        out = []
        try:
            for n in sorted({f.split(".")[0] for f in os.listdir(paths.REF_HISTORY_DIR)}, reverse=True):
                m = store.read_json(os.path.join(paths.REF_HISTORY_DIR, n + ".json")) or {}
                out.append({"id": n, "time": m.get("time", n)})
        except OSError:
            pass
        return out

    def c_ref_restore(self, a, gen):
        self._no_run("빈 판 사진")
        n = a["id"]
        img = imgio.imread_u(os.path.join(paths.REF_HISTORY_DIR, n + ".png"))
        if img is None:
            raise AppError("보관 사진 없음")
        meta = store.read_json(os.path.join(paths.REF_HISTORY_DIR, n + ".json")) or {}
        r = self._install_ref(img, new_id=meta.get("id", n), from_history=True)
        if meta:
            self.ref_meta.update({k: meta[k] for k in ("exposure", "wb_temperature", "time") if k in meta})
            store.write_json(paths.REF_META_PATH, self.ref_meta)
        return r

    def c_snap(self, a, gen):
        """단발 촬영(보정 작업용): 지금 자리에서 한 장. 정렬이 있으면 기준 프레임으로 옮길 수 있다."""
        self._need_camera()
        self._begin("촬영")
        try:
            with (self.motion._locked(True) if self.motion else contextlib.nullcontext()):
                img, tm = self._grab(gen, n_best=1)
            A, why = (None, "") if not self.calib.area else self._align_snap(img)
            self.snap = {"img": img, "A": A, "id": _stamp(), "t": tm, "why": why}
            return {"aligned": A is not None, "why": why}
        finally:
            self._end()

    def _frame_to_ref(self, pts, frame):
        """클릭 좌표(프레임 태그 frame) -> 기준 프레임. 프레임 변환은 frames.Align 한곳."""
        if frame == "ref":
            return [tuple(p) for p in pts]
        A = None
        if frame == "snap":
            A = self.snap and self.snap.get("A")
        elif frame == "shot":
            A = self.shot and self.shot.get("A")
        if A is None:
            if self.ref_img() is None:
                return [tuple(p) for p in pts]               # 빈 판 사진이 없으면 지금 프레임이 곧 기준이 된다
            raise AppError("정렬 없음 · 클릭 좌표 변환 불가")
        return [tuple(t) for t in A.to_ref(pts)]

    def c_area_set(self, a, gen):
        self._no_run("판 영역")
        pts = self._frame_to_ref(a["points"], a.get("frame", "ref"))
        size = self._size() or (a.get("size") or (1280, 960))
        ok, why = self.calib.set_area(pts, self.ref_id(), size)
        if not ok:
            raise AppError("판 영역 거절 · " + why)
        self._recalc_samples()
        return {"area": self.calib.area}

    def c_exclude_set(self, a, gen):
        polys = [self._frame_to_ref(p, a.get("frame", "ref")) for p in a["polys"]]
        self.calib.set_exclude(polys)

    # ------------------------------------------------------------------
    # 좌표 보정
    # ------------------------------------------------------------------
    def c_calib_record(self, a, gen):
        """위치 기록: 지금 스테이지 X · Y 를 잡아 두고 사진에서 같은 점을 클릭받는다."""
        self._need_stage()
        st = self.motion.status()
        tl = conditions.active_tool(self.cfg.values)
        self.pending_calib = {"x": round(st["x_mm"], 3), "y": round(st["y_mm"], 3)}
        return dict(self.pending_calib)

    def c_calib_click(self, a, gen):
        if not self.pending_calib:
            raise AppError("위치 기록 먼저")
        (ru, rv), = self._frame_to_ref([[a["u"], a["v"]]], a.get("frame", "snap"))
        p = self.calib.point_near(a.get("shot"), self.pending_calib["x"], self.pending_calib["y"], tol_mm=0.2) if a.get("shot") else None
        if p:
            raise AppError("이미 기록한 점 · %d번" % p["no"])
        no = self.calib.add_point(ru, rv, self.pending_calib["x"], self.pending_calib["y"], a.get("src", "click"), a.get("shot"))
        self.pending_calib = None
        self._recalc_samples()
        return {"no": no, "warnings": self.calib.warnings()}

    def c_calib_delete(self, a, gen):
        if not self.calib.delete_point(a["no"]):
            raise AppError("점 없음")
        self._recalc_samples()

    def c_calib_apply(self, a, gen):
        ok, why = self.calib.apply()
        if not ok:
            raise AppError(why)
        self._recalc_samples()
        return {"rms": self.calib.d["rms_mm"], "size_mm": self.calib.d.get("size_mm")}

    # ------------------------------------------------------------------
    # 헤드 위치 보정
    # ------------------------------------------------------------------
    def c_head_calib(self, a, gen):
        self._need_move("헤드 위치 보정")
        self._need_camera()
        c = self.calib
        if not c.ready(self.ref_id(), self._size()):
            raise AppError("좌표 보정 필요 · 장비 설정 5")
        n, mg = int(self.cfg.get("photohome.grid_n")), float(self.cfg.get("photohome.grid_margin_mm"))
        q = np.asarray([c.px_to_mm(u, v) for u, v in c.area])
        x0, x1, y0, y1 = q[:, 0].min() + mg, q[:, 0].max() - mg, q[:, 1].min() + mg, q[:, 1].max() - mg
        xs, ys = np.linspace(max(0, x0), x1, n), np.linspace(max(0, y0), y1, n)
        self._begin("헤드 위치 보정")
        try:
            new = calibstore.HeadCalib(path=self.head.path)          # 이전 보정은 그대로 둔다 - 새 점은 따로 모아 성공했을 때만 바꾼다(정지 · 비상정지 · 오류로 끝나면 이전 보정이 남는다)
            new.clear()
            skipped = 0
            with self.motion._locked(True):
                self.motion._enter(gen)
                for i, y in enumerate(ys):
                    for x in (xs if i % 2 == 0 else xs[::-1]):
                        self.cap_text = "격자 %d / %d" % (len(new.d["pairs"]) + skipped + 1, n * n)
                        self.touch()
                        self.motion.move_xy(float(x), float(y), gen=gen, nowait=True)
                        st = self.motion.status()
                        img, tm = self._grab(gen, n_best=1)
                        _px, rp, why = self._head_px_ref(img)
                        if rp is None:
                            skipped += 1
                            logger.log("헤드 보정 점 건너뜀 · " + why, "warn")
                            continue
                        new.add(rp[0], rp[1], st["x_mm"], st["y_mm"], self.ref_id(), self._size())
            ok, why = new.fit([c.px_to_mm(u, v) for u, v in c.area])
            if not ok:
                raise AppError("헤드 위치 보정 실패 · " + why)
            new.save()
            self.head = new
            return {"n": len(new.d["pairs"]), "rms": new.d["rms_mm"], "skipped": skipped}
        finally:
            self._end()

    def c_head_apply(self, a, gen):
        ok, why = self.head.apply(float(self.cfg.get("photohome.rms_limit_mm")))
        if not ok:
            raise AppError(why)

    # ------------------------------------------------------------------
    # 접촉 높이 (고정 절대값: Z 원점에서 핀이 샘플에 닿는 높이)
    # ------------------------------------------------------------------
    def _over_sample_check(self, no):
        s = self._find(int(no))
        if s["X"] is None:
            raise AppError("좌표 없음 · 좌표 보정 필요")
        tl = conditions.active_tool(self.cfg.values)
        px, py = s["X"] - float(tl.get("dx_mm") or 0.0), s["Y"] - float(tl.get("dy_mm") or 0.0)

        def chk(st):
            if abs(st["x_mm"] - px) > measure.XY_TOL_MM or abs(st["y_mm"] - py) > measure.XY_TOL_MM:
                raise MotionError("샘플 %d 위가 아님 · [샘플 위로] 먼저" % s["no"], "not_over")
        return chk

    def c_contact_save(self, a, gen):
        """지금 Z 높이를 접촉 높이로 저장한다(샘플 위에서 핀이 닿는 자리까지 Z 조그로 내린 뒤).
        z 를 주면 그 값([전기로 접촉 찾기]의 첫 접촉 Z - 마지막 찾기 결과와 같아야 한다)."""
        self._need_stage()
        self._no_run("접촉 높이 저장")
        if not self.motion.z_ok or self.motion.estop_locked:
            raise AppError("원점 필요 · 장비 설정 1")
        st = self.motion.status()
        z = st.get("z_mm")
        if a.get("z") is not None:
            last = self.contact_last or {}
            if last.get("first_z") is None or abs(float(a["z"]) - float(last["first_z"])) > 1e-6:
                raise AppError("전기로 찾은 높이가 아님 · 다시 찾기")
            z = float(a["z"])
        zmax = float(stage_mod.limits_mm()["z_max_mm"])
        if z is None or not (0.1 <= z <= zmax):
            raise AppError("Z %s mm · 저장 못 함 (0.1~%g)" % ("?" if z is None else "%.2f" % z, zmax))
        if a.get("no") is not None:
            self._over_sample_check(a["no"])(st)            # 샘플을 골랐으면 그 위에서만 저장
        zr = round(float(z), 4) if a.get("z") is not None else round(float(z), 2)      # 전기로 찾은 높이는 펄스 자리 그대로(0.025 mm 간격이 반올림으로 흔들리지 않게)
        ok, errs, _w = self.cfg.save({"contact_z_mm": zr, "contact_tool": conditions.active_tool(self.cfg.values).get("name")})
        if not ok:
            raise AppError("접촉 높이 저장 실패 · " + next(iter(errs.values())))
        logger.log("접촉 높이 저장 · Z %s mm%s" % (zr, " (전기로 찾음)" if a.get("z") is not None else ""))
        return {"z": zr}

    contact_last = None                              # 마지막 접촉 내리기 · 찾기 결과(첫 접촉 Z · 최종 Z · 2 · 4단자 저항)

    def _contact_tool_ok(self):
        ct = self.cfg.get("contact_tool")
        if ct and ct != conditions.active_tool(self.cfg.values).get("name"):
            raise AppError("접촉 높이는 '%s' 로 잰 값 · 지금 공구와 다름" % ct)

    def c_contact_clear(self, a, gen):
        self._no_run("접촉 높이 지우기")
        self.cfg.save({"contact_z_mm": None, "contact_tool": None})

    def contact_descend(self, gen, check_xy=None, nowait=False):
        """접촉 높이로 내리기(측정 단계 · 측정 탭 · 장비 설정 7 공용). 접촉 확인이 켜져 있으면 전기로 확인하며 내리고(contact.find),
        꺼져 있으면 저장한 높이까지만(descend_contact). 반환 {sense, first_z, final_z, r2, r4 …}. 실패는 contact.ContactFail · MotionError."""
        z = self.cfg.get("contact_z_mm")
        if z is None:
            raise AppError("접촉 높이 필요 · 장비 설정 7")
        if not contact_mod.sense_on(self.cfg):
            st = self.motion.descend_contact(float(z), float(self.cfg.get("approach_mm")), gen=gen, check_xy=check_xy, nowait=nowait)
            res = {"sense": False, "first_z": None, "final_z": st.get("z_mm"), "r2": None, "r4": None, "x": st.get("x_mm"), "y": st.get("y_mm")}
        else:
            why = contact_mod.block(self)
            if why:
                raise AppError(why)
            res = dict(contact_mod.find(self, gen, mode="height", z_ref=float(z), check_xy=check_xy, nowait=nowait), sense=True)
        self.contact_last = dict(res, t=time.time())
        return res

    def _contact_cmd(self, fn, what):
        """사람이 누른 접촉 명령의 공통 틀: 바쁨 표시 · 위치 갱신 · 실패 사유 한 줄."""
        self._begin(what)
        try:
            try:
                res = fn()
            except contact_mod.ContactFail as e:
                raise AppError(str(e))
            finally:
                try:
                    st = self.motion.status()
                    self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
                except Exception:                          # noqa: BLE001
                    pass
            return res
        finally:
            self._end()

    def c_contact_try(self, a, gen):
        """저장한 접촉 높이로 내리기 - X · Y 가 고른 샘플 위일 때만(측정 탭 '지금 위치' 는 free). 접촉 확인이 켜져 있으면 전기로 확인하며."""
        self._need_move("접촉 높이 내려 보기")
        if self.cfg.get("contact_z_mm") is None:
            raise AppError("접촉 높이 필요 · 먼저 저장")
        self._contact_tool_ok()
        chk = self._over_sample_check(a["no"]) if a.get("no") is not None else None
        if chk is None and not a.get("free"):
            raise AppError("샘플을 고른 뒤")                  # free: 측정 탭 '이 위치 측정' - 사람이 조그로 핀을 맞춘 자리에서 내림
        res = self._contact_cmd(lambda: self.contact_descend(gen, check_xy=chk, nowait=True), "접촉 높이로 내리기")
        return _contact_view(res)

    def c_contact_find(self, a, gen):
        """장비 설정 7 [전기로 접촉 찾기]: 고른 샘플 위에서. 접촉 높이가 있으면 측정과 같은 범위, 없으면 지금 Z 부터 0.05 mm 간격으로
        contact.search_mm 까지 → 닿으면 0.3 mm 올렸다가 step_mm 간격으로 다시. 저장은 하지 않는다(화면이 확인 창 뒤 contact_save {z})."""
        self._need_move("접촉 찾기")
        if self.k2450 is None or not self.k2450.is_open:
            raise AppError(contact_mod.NO_SENSE)
        if a.get("no") is None:
            raise AppError("샘플을 고른 뒤 · [샘플 위로]")
        self._contact_tool_ok()
        chk = self._over_sample_check(a["no"])
        z = self.cfg.get("contact_z_mm")

        def go():
            if z is not None:
                r = contact_mod.find(self, gen, mode="height", z_ref=float(z), check_xy=chk, nowait=True)
            else:
                zc = float(self.motion.status().get("z_mm") or 0.0)
                r = contact_mod.find(self, gen, mode="search", z_ref=zc, check_xy=chk, nowait=True)
            self.contact_last = dict(r, sense=True, t=time.time(), no=int(a["no"]))
            return self.contact_last
        res = self._contact_cmd(go, "접촉 찾기")
        return _contact_view(res)

    # ------------------------------------------------------------------
    # 릴레이 (수동)
    # ------------------------------------------------------------------
    def _relay_on_guard(self, gen):
        """사람이 보낸 릴레이 켜기 · 시험의 공통 검사: 이동 중이면 줄 서지 않고 즉시 거절 · 명령이 들어온 순간의 세대 확인 · 비상정지 잠금."""
        m = self.motion
        if m.moving:
            raise AppError("이동 중 · 끝난 뒤")
        if gen != m.gen:
            raise AppError(self.cancel_text(gen))
        if m.estop_locked:
            raise AppError("비상정지 잠금 · 켜기 거절")

    def c_relay(self, a, gen):
        self._need_stage()
        n, on = int(a["n"]), bool(a["on"])
        if self.run and self.run.state in ("ready", "running", "paused", "stopping") and on:
            raise AppError("측정 중 · 수동 릴레이 못 씀")
        if self.rt is not None and self.rt.active and on:           # 끄기는 언제나 받는다(안전한 쪽) - 측정은 RS 비교로 알아채고 멈춘다
            raise AppError("%s 중 · 릴레이 켜기 못 함" % self.rt_label())
        if not on:
            self.power.set(n, False, "manual")
            return
        if not self.cfg.get("relay.items")[n - 1].get("enabled"):
            raise AppError("R%d 사용 안 함 · 장비 설정 8" % n)
        if self.busy:
            raise AppError("%s 중 · 끝난 뒤" % self.busy)
        m = self.motion
        # 이동 잠금을 기다리지 않고 잡는다: 이동 중이면 즉시 거절, 잡은 동안 이동이 시작되지 못한다(끼어드는 켜기가 없다)
        if not m.lock.acquire(blocking=False):
            raise AppError("이동 중 · 끝난 뒤")
        try:
            self._relay_on_guard(gen)
            with m.op(gen):                                 # 쓰기 직전에 세대를 다시 본다
                self.power.set(n, True, "manual")
        except StageError as e:
            raise AppError(self.cancel_text(gen) if m.gen != gen else logger.short(e))
        finally:
            m.lock.release()

    def c_relay_test(self, a, gen):
        """릴레이 하나를 2 s 켜 본다(끝나면 반드시 끈다). 히트건 릴레이도 시간 한도 안에서만 켠다. 시험이 도는 동안 이동은 거절된다(busy)."""
        self._need_stage()
        self._no_run("릴레이 시험")
        n = int(a["n"])
        if not 1 <= n <= 10:
            raise AppError("릴레이 번호 1~10")
        if not self.cfg.get("relay.items")[n - 1].get("enabled"):
            raise AppError("R%d 사용 안 함 · 장비 설정 8" % n)
        m = self.motion
        self._relay_on_guard(gen)
        self._begin("릴레이 시험")
        try:
            if not m.lock.acquire(blocking=False):
                raise AppError("이동 중 · 끝난 뒤")
            m.lock.release()
            n0 = self.link_lost_count
            canc = lambda: m.gen != gen or self.link_lost_count != n0          # noqa: E731
            with m.op(gen):
                if n == int(self.cfg.get("heater.relay")):
                    self.power.heater_for(2.0, cancel=canc)
                else:
                    self.power.set(n, True, "manual")
                    try:
                        end = time.time() + 2.0
                        while time.time() < end and not canc():
                            time.sleep(0.05)
                    finally:
                        self.power.set(n, False, "manual")
        except StageError as e:
            raise AppError(self.cancel_text(gen) if m.gen != gen else logger.short(e))
        finally:
            self._end()

    # ------------------------------------------------------------------
    # 측정
    # ------------------------------------------------------------------
    def _condition(self, name=None):
        return copy.deepcopy(conditions.find(self.cfg.values, name or self.cfg.get("condition.active")))

    def preflight(self, cond, nos):
        """(차단 사유 | "", 안내 목록)."""
        if self.motion is None:
            return "스테이지 연결 안 됨", []
        if self.busy:
            return "%s 중 · 끝난 뒤" % self.busy, []
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            return "측정 중", []
        if self.rt is not None and self.rt.active:
            return "%s 중" % self.rt_label(), []
        if conditions.uses_rt(cond):
            import rt as rt_mod
            why = rt_mod.RTRun.start_block(self, "rt", check_pin=False)
            if why:
                return why, []
        if not self.shot or not self.samples:
            return "촬영 필요", []
        if not self.calib.ready(self.ref_id(), self._size()):
            return "좌표 보정 필요 · 장비 설정 5", []
        if not self.shot.get("coords_ok"):
            return "카메라 위치 확인 필요 · 다시 촬영", []
        m = self.motion
        if m.estop_locked or not m.z_ok or not m.xy_ok:
            return "원점 확인 필요 · " + " → ".join(m.recovery_steps()), []
        notes = []
        if conditions.uses_contact(cond) and self.cfg.get("contact_z_mm") is None:
            return "접촉 높이 필요 · 장비 설정 7", []
        if conditions.uses_contact(cond):
            try:
                self._contact_tool_ok()
            except AppError as e:
                return str(e), []
            why = contact_mod.block(self)                     # 접촉 확인이 켜져 있으면 K2450 이 있어야 내린다
            if why:
                return why, []
            if not contact_mod.sense_on(self.cfg):
                notes.append("접촉 확인 꺼짐 · 저장한 높이까지만 내림")
        if conditions.uses_heat(cond) and not self.cfg.get("relay.items")[int(self.cfg.get("heater.relay")) - 1].get("enabled"):
            return "히트건 릴레이 사용 안 함 · 장비 설정 8", []
        errs, _w = conditions.validate_condition(cond, self.cfg.values, stage_mod.limits_mm()["z_max_mm"], prefix="c")
        if errs:
            return "측정 단계 오류 · " + next(iter(errs.values())), []
        for n in conditions.relays_used(cond, self.cfg.values):
            if not self.cfg.get("relay.items")[n - 1].get("enabled"):
                return "R%d 사용 안 함 · 장비 설정 8" % n, []
        sel = [s for s in self.samples if (nos is None or s["no"] in nos) and not s["excluded"]]
        if not sel:
            return "측정 대상 없음", []
        if any(s["X"] is None for s in sel):
            return "좌표 없는 샘플 있음", []
        if any(st.get("type") == "cooler" and st.get("target") == "sample" for st in cond["steps"]) and any(not s.get("cooler_relay") for s in sel):
            notes.append("쿨러 구역 밖 샘플 있음 · 쿨러 단계에서 오류")
        return "", notes

    def c_measure_start(self, a, gen):
        nos = a.get("nos")
        cond = self._condition(a.get("condition"))
        why, notes = self.preflight(cond, nos)
        if why:
            raise AppError(why)
        if any(s["weak"] and not s["excluded"] for s in self.samples) and not a.get("ok_weak"):
            raise AppError("확인 필요 샘플 남음")
        sel = [s for s in self.samples if (nos is None or s["no"] in nos) and not s["excluded"]]
        self.hub.configure()
        self.hub.start()
        self._apply_motion_cfg()
        if self.motion and gen != self.motion.gen:
            raise AppError(self.cancel_text(gen))
        with self.lock:
            if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
                raise AppError("측정 중")
            run = measure.Run(self, cond, copy.deepcopy(sel), "continuous")
            self.run = run
        self.last_run = None
        run.warns.extend(notes)
        self._save_run_photo(run)
        run.start()
        logger.log("측정 시작 · %s · %d개" % (cond["name"], len(sel)))
        return {"dir": run.run_dir}

    def _save_run_photo(self, run):
        if not self.shot:
            return
        try:
            c = self.calib
            ann = geom.annotate_fixed(self.shot["img"], [dict(s, x_px=s["u"], y_px=s["v"], vertices_px=s["verts"]) for s in self.samples])
            area = self.shot.get("area_cur") or c.area
            u0, v0, u1, v1 = geom.crop_rect(area, ann.shape)
            os.makedirs(run.run_dir, exist_ok=True)
            imgio.imwrite_u(os.path.join(run.run_dir, "plate_fit.jpg"), ann[v0:v1, u0:u1])
        except Exception:                                    # noqa: BLE001
            pass

    def on_run_end(self, run):
        self.last_run = run
        if run.state in ("done", "stopped") and not run.app.motion.estop_locked:
            self.save_position()
        if run.state == "error":
            self.set_alarm("측정 오류 · " + run.message, "결과 탭에서 확인")
        self.touch()

    def _run_cmd(self, fn):
        if not self.run or self.run.state in ("done", "stopped", "error"):
            raise AppError("측정 중 아님")
        fn(self.run)

    def c_measure_pause(self, a, gen):
        self._run_cmd(lambda r: r.pause())

    def c_measure_resume(self, a, gen):
        self._run_cmd(lambda r: r.resume())

    def c_measure_stop(self, a, gen):
        self._run_cmd(lambda r: r.stop())

    def c_measure_answer(self, a, gen):
        self._run_cmd(lambda r: r.answer(a["value"]))

    def c_measure_redo_sample(self, a, gen):
        """이 샘플만 다시 측정: 같은 측정 폴더에 재측정 기록을 추가한다(이전 기록 보존)."""
        base = self.last_run
        if base is None or base.state not in ("done", "stopped", "error"):
            raise AppError("끝난 측정 없음")
        no = int(a["no"])
        s = next((x for x in self.samples if x["no"] == no), None)
        if s is None:
            raise AppError("샘플 %d 없음" % no)
        cond = base.cond
        why, _n = self.preflight(cond, [no])
        if why:
            raise AppError(why)
        run = measure.Run(self, cond, [copy.deepcopy(s)], "continuous", run_dir=base.run_dir, prior=base.results)
        run.t0 = base.t0
        run.warns = base.warns
        self.run = run
        run.start()

    # ------------------------------------------------------------------
    # 결과
    # ------------------------------------------------------------------
    # ------------------------------------------------------------------
    # 측정 장비(VISA) · R–T 측정
    # ------------------------------------------------------------------
    def _lab_install(self):
        """시뮬레이션: 가짜 장비 세계를 만들어 VISA 자리에 꽂는다(실장비에서는 호출하지 않는다)."""
        if self.lab is not None:
            return
        import sys as _sys
        _sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
        import fake_visa
        if self.hub is not None:
            clk = self.hub.clock
        else:                                              # 스테이지가 없어도 측정 장비 목록 · 연결은 된다(시뮬레이션)
            import devices as devices_mod
            clk = devices_mod.Clock(float(self.cfg.get("sim.time_scale")))

        def heater():
            return bool(self.power and self.power.owners.get(self.power.heater_n))

        def cooler():
            return bool(self.power and any(n != self.power.heater_n for n in self.power.owners))

        def pin():
            if getattr(self.lab, "tc_on_plate", False):          # 수동 측정 흉내: 사람이 열전대를 시료에 둔다(핀 높이와 무관)
                return True
            cz = self.cfg.get("contact_z_mm")
            z = self.pos.get("z")
            return cz is not None and z is not None and abs(float(z) - float(cz)) <= 0.5
        self.lab = fake_visa.Lab(clock=clk.now, sleep=lambda s: time.sleep(s / clk.scale), heater_fn=heater, cooler_fn=cooler, pin_down_fn=pin)
        self.lab.pos_fn = self._true_xyz                      # 접촉 찾기 흉내: 가짜 K2450 이 스테이지의 실제 X · Y · Z 를 본다
        fake_visa.install(self.lab)

    def _inst_close(self, off=True):
        for nm in ("k2450", "k2110"):
            d = getattr(self, nm)
            if d is None:
                continue
            try:
                if off and nm == "k2450" and d.is_open:
                    d.output(False)
            except Exception:                              # noqa: BLE001
                pass
            d.close()
            setattr(self, nm, None)

    def _inst_off(self):
        """비상정지 · 종료: K2450 출력 끄기(실패해도 계속)."""
        d = self.k2450
        if d is None or not d.is_open:
            return
        for _ in range(2):
            try:
                d.output(False)
                return
            except Exception:                              # noqa: BLE001
                time.sleep(0.2)

    def c_inst_list(self, a, gen):
        from core import instruments as inst
        if self.sim:
            self._lab_install()
        items, msg = inst.list_resources()
        return {"items": items, "msg": msg}

    def _inst_open(self, nm, cls, addr, items):
        """장비 하나를 연다. 설정 주소가 일련번호 없는 USB 형식이거나 열기 · 모델 확인이 실패하면 목록에서 찾은 주소로 한 번 더(로그).
        반환 (장치 | None, 정보 dict). 찾은 주소로 열렸으면 정보에 "found" = 그 주소."""
        from core import instruments as inst
        tries = [addr] if addr else []
        alt = inst.find_addr(nm, items)
        if alt and alt not in tries:
            if not addr or inst.usb_no_serial(addr):
                tries.insert(0, alt)                           # 일련번호 없는 주소보다 목록의 정확한 주소를 먼저
            else:
                tries.append(alt)
        err = "주소 없음 · 장비 설정 8 에서 선택"
        for i, ad in enumerate(tries):
            d = cls(ad)
            try:
                idn = d.open()
            except inst.VisaUnavailable:
                raise
            except inst.VisaError as e:
                err = logger.short(e)
                logger.log("%s 열기 실패 · %s · %s" % (cls.NAME, ad, err), "warn")
                continue
            if not d.model_ok():
                d.close()
                err = "%s 아님 · %s" % (cls.NAME, logger.short(idn, 30))
                logger.log("%s 주소 %s 의 장비가 다름 · %s" % (cls.NAME, ad, idn), "warn")
                continue
            if nm == "k2450":
                ok, why = d.check_scpi()
                if not ok:
                    d.close()
                    return None, {"ok": False, "addr": ad, "err": why}
            info = {"ok": True, "addr": ad, "idn": idn, "kind": inst.visa_kind(ad)}
            if ad != addr:
                info["found"] = ad
                logger.log("%s 주소 바꿈 · %s → %s (목록에서 찾음)" % (cls.NAME, addr or "없음", ad), "warn")
            return d, info
        return None, {"ok": False, "addr": addr, "err": err}

    def c_inst_connect(self, a, gen):
        """K2110 · K2450 을 연다(읽기만 - *IDN? · 명령 세트). 출력은 켜지 않는다. 시리얼(ASRL) 주소는 열지 않는다."""
        from core import instruments as inst
        self._no_run("장비 연결")
        if self.sim:
            self._lab_install()
        self._inst_close()
        info = {"k2110": None, "k2450": None, "msg": "", "test": self.inst_info.get("test")}
        items, lmsg = inst.list_resources()
        found = {}
        for nm, cls, key in (("k2110", inst.K2110, "rt.k2110_addr"), ("k2450", inst.K2450, "rt.k2450_addr")):
            addr = str(self.cfg.get(key) or "").strip()
            if inst.visa_kind(addr) == "시리얼":
                logger.log("%s 주소가 시리얼 포트(%s) · 열지 않음" % (cls.NAME, addr), "warn")
                addr = ""
            try:
                d, info[nm] = self._inst_open(nm, cls, addr, items)
            except inst.VisaUnavailable as e:
                info[nm] = {"ok": False, "addr": addr, "err": str(e)}
                info["msg"] = str(e)
                continue
            if d is not None:
                setattr(self, nm, d)
                if info[nm].get("found"):
                    found[key] = info[nm]["found"]
        if found:                                              # 찾은 주소를 저장한다(다음 연결부터 바로)
            ok, errs, _w = self.cfg.save(found)
            if not ok:
                logger.log("찾은 장비 주소 저장 실패 · %s" % next(iter(errs.values())), "warn")
        self.inst_info = info
        logger.log("측정 장비 연결 · K2110 %s · K2450 %s" % ("연결" if info["k2110"] and info["k2110"]["ok"] else "안 됨", "연결" if info["k2450"] and info["k2450"]["ok"] else "안 됨"))
        if not (info["k2110"] and info["k2110"]["ok"] and info["k2450"] and info["k2450"]["ok"]):
            bad = [v["err"] for v in (info["k2110"], info["k2450"]) if v and not v["ok"]]
            raise AppError(bad[0] if bad else (lmsg or "장비 연결 실패"))
        return {}

    def c_inst_disconnect(self, a, gen):
        self._no_run("장비 연결 끊기")
        self._inst_close()
        self.inst_info = {"k2110": None, "k2450": None, "msg": "", "test": None}

    def c_inst_test(self, a, gen):
        """연결 시험: 장비 이름 · 온도 1회 · 저항 1회. 저항 1회를 잴 때만 출력을 짧게 켜고 바로 끈다."""
        import rt as rt_mod
        from core import instruments as inst
        self._no_run("연결 시험")
        if not (self.k2110 and self.k2110.is_open and self.k2450 and self.k2450.is_open):
            self.c_inst_connect({}, gen)
        p = rt_mod.params(self.cfg)
        res = {"k2110": self.k2110.idn_text, "k2450": self.k2450.idn_text, "k2110_kind": inst.visa_kind(self.k2110.addr), "k2450_kind": inst.visa_kind(self.k2450.addr), "temp": None, "R": None, "I": None, "err": ""}
        self._begin("연결 시험")
        try:
            try:
                self.k2110.configure(p["tc_type"], "REAL")
                res["temp"] = self.k2110.read_temp()
                self.k2450.configure({"terminal": p["terminal"], "four_wire": p["four_wire"], "volt": p["volt"], "ilim_a": p["ilim_a"],
                                      "range_mode": p["range_mode"], "start_range": p["start_range"], "nplc": p["nplc"], "filter_n": p["filter_n"]})
                res["terminal"] = self.k2450.terminal()
                self.k2450.output(True)
                try:
                    i, trip = self.k2450.read()
                    if i is None and p["range_mode"] == "step":                       # 범위 넘음 - 한 단계 올려 한 번 더
                        self.k2450.set_range(min(1.0, float(p["start_range"]) * 10.0))
                        i, trip = self.k2450.read()
                    res["I"] = i
                    res["R"] = inst.resistance(p["volt"], i)
                    res["open"] = inst.is_open_circuit(i, p["open_a"])
                finally:
                    self.k2450.output(False)
            except inst.VisaError as e:
                res["err"] = logger.short(e)
        finally:
            self._end()
        self.inst_info["test"] = dict(res, t=time.strftime("%H:%M:%S"))
        if res["err"]:
            raise AppError(res["err"])
        if res.get("open"):
            raise AppError("연결 없음 · 핀 접촉 · 앞/뒤 단자 설정 확인")
        if res["temp"] is None:
            raise AppError("온도 값 없음 · 열전대 확인")
        return res

    def cooler_relays_for(self, target, custom=""):
        """쿨러 릴레이 목록. 샘플 구역은 지금 도구 위치(공구 오프셋 적용)가 속한 쿨러 구역."""
        z = self.cfg.get("cooler.zones")
        cells = z.get("cells") or []
        if target == "all":
            return sorted({int(c["relay"]) for c in cells if c.get("relay")})
        if target == "custom":
            return [int(x) for x in str(custom or "").split(",") if x.strip()]
        c = self.calib
        if c.H is None or not c.area:
            return []
        x, y = self.pos.get("x"), self.pos.get("y")
        if x is None or y is None:
            return []
        tl = conditions.active_tool(self.cfg.values)
        sx, sy = float(x) + float(tl.get("dx_mm") or 0.0), float(y) + float(tl.get("dy_mm") or 0.0)
        u, v = c.mm_to_px(sx, sy)
        size = c.measured_size_mm() or self.cfg.get("calib.area_size_mm")
        Hp = geom.plate_homography(c.area, size)
        lx, ly = geom.px_to_mm(Hp, u, v)
        cell = zones.cell_at(z, size, lx, ly)
        if cell is None:
            return []
        row, col = cell if isinstance(cell, tuple) else (cell // z["cols"], cell % z["cols"])
        cc = z["cells"][row * z["cols"] + col]
        return [int(cc["relay"])] if cc.get("relay") else []

    def rt_block(self, mode="rt", fresh=False):
        import rt as rt_mod
        if self.busy:
            return "%s 중 · 끝난 뒤" % self.busy
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            return "측정 중"
        if self.rt is not None and self.rt.active:
            return "%s 중" % self.rt_label()
        why = rt_mod.RTRun.start_block(self, mode, check_pin=True, fresh=fresh) or rt_mod.params_block(rt_mod.params(self.cfg))
        if why:
            return why
        hn = int(self.cfg.get("heater.relay"))
        items = self.cfg.get("relay.items")
        if not items[hn - 1].get("enabled"):
            return "히트건 릴레이 사용 안 함 · 장비 설정 8"
        try:
            rl = self.cooler_relays_for(self.cfg.get("rt.cooler_target"), self.cfg.get("rt.cooler_custom"))
        except Exception:                                    # noqa: BLE001
            rl = []
        if not rl:
            return "쿨러 구역 없음 · 좌표 보정 또는 쿨러 선택"
        off = [n for n in rl if not items[n - 1].get("enabled")]
        if off:
            return "R%d 사용 안 함 · 장비 설정 8" % off[0]
        return ""

    def _rt_launch(self, mode, gen, speed_s=20.0):
        import rt as rt_mod
        why = self.rt_block(mode, fresh=True)
        if why:
            raise AppError(why)
        p = rt_mod.params(self.cfg)
        from core import instruments as inst
        try:                                                  # 시작 조건 '온도 값 정상': 열전대를 한 번 읽어 본다
            self.k2110.configure(p["tc_type"], "REAL")
            t0 = self.k2110.read_temp()
        except inst.VisaError as e:
            raise AppError(logger.short(e))
        if t0 is None:
            raise AppError("온도 값 없음 · 열전대 확인")
        self.hub.configure()
        self.hub.start()
        self._apply_motion_cfg()
        if gen != self.motion.gen:
            raise AppError(self.cancel_text(gen))
        relays = self.cooler_relays_for(p["cooler_target"], p["cooler_custom"])
        n0 = self.link_lost_count
        self._begin("R–T 측정" if mode == "rt" else "속도 측정")
        name = p["file_name"] if mode == "rt" else "speed_"
        rr = rt_mod.RTRun(self, p, name, cancel_fn=lambda: self.link_lost_count != n0, relays=relays, mode=mode, speed_s=speed_s, gen=gen)
        try:
            zn = self.motion.status().get("z_mm")
        except Exception:                                   # noqa: BLE001
            zn = None
        if zn is not None and rt_mod.RTRun._contact_here(self, zn):
            rr.contact = dict(_contact_view(self.contact_last), points=self.contact_last.get("points"))   # rt.json 에 이 자리의 접촉 찾기 결과
        self.rt = rr
        self.rt_last["auto"] = rr
        rr.thread = threading.Thread(target=rr.run, name="rt-run", daemon=True)
        rr.thread.start()
        logger.log("%s 시작 · %s" % ("R–T 측정" if mode == "rt" else "속도 측정", name))
        return {"dir": rr.run_dir}

    def c_rt_start(self, a, gen):
        return self._rt_launch("rt", gen)

    def c_rt_speed(self, a, gen):
        """최대 속도 재기: 히트건만 n s · 쿨러만 n s 의 °C/분."""
        return self._rt_launch("speed", gen, speed_s=float(a.get("s", 20.0)))

    def c_rt_stop(self, a, gen):
        if self.rt is None or not self.rt.active or self.rt.manual:
            raise AppError("R–T 측정 중 아님")
        self.rt.stop()

    def c_rt_defaults(self, a, gen):
        """측정 조건을 기본값으로(장비 주소는 그대로)."""
        import rt as rt_mod
        import settings_schema as sch
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · 변경 잠김")
        d = sch.defaults()["rt"]
        patch = {"rt." + k: d[k] for k in rt_mod.RT_KEYS if k not in ("k2110_addr", "k2450_addr")}
        ok, errs, _w = self.cfg.save(patch)
        if not ok:
            raise AppError("기본값 저장 실패 · " + next(iter(errs.values())))

    def c_rt_nas_retry(self, a, gen):
        """[NAS 로 다시 복사]: 마지막(또는 고른) R–T 측정의 파일을 NAS 로 다시 복사한다."""
        r = self.rt_last["manual" if a.get("manual") else "auto"]
        if r is None or r.active:
            raise AppError("끝난 R–T 측정 없음")
        try:
            ok = r.retry_nas()
        except Exception as e:                               # noqa: BLE001
            raise AppError(logger.short(e))
        if not ok:
            raise AppError("NAS 에 쓰지 못함 · 경로 확인")
        return {}

    # ------------------------------------------------------------------
    # 수동 측정 탭: 모터를 부르지 않는다(이동 계층 · 원점 · 접촉 높이 · 샘플 선택 · 끝의 Z 올림 없음). 스테이지는 릴레이에만 쓴다.
    # 도는 동안은 self.rt 자리를 차지해 자동 측정 · 촬영 · 헤드 보정 · 모든 이동(조그 포함) · 장비 연결 · 릴레이 켜기를 막는다(반대도 같다).
    # ------------------------------------------------------------------
    OUTP_ON = __import__("re").compile(r"^:?OUTP(UT)?1?(:STAT(E)?)?\s*(ON|1)\s*$", __import__("re").I)

    def manual_relays(self):
        """수동 측정 쿨러 릴레이(manual.cooler_relays "2" · "2,3"). 형식이 틀리면 []."""
        try:
            return sorted({int(x) for x in str(self.cfg.get("manual.cooler_relays") or "").replace(" ", "").split(",") if x})
        except ValueError:
            return []

    def manual_block(self, mode):
        """mode: quick(온도 · 저항 1번 · 명령 직접) · cont(연속) · rt(R–T). 못 하는 사유 한 줄 | ""."""
        import rt as rt_mod
        if self.shutting:
            return "종료 중"
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            return "측정 중 · 끝난 뒤"
        if self.rt is not None and self.rt.active:
            return "%s 중" % self.rt_label()
        if self.busy:
            return "%s 중 · 끝난 뒤" % self.busy
        if self.reconnecting:
            return "다시 연결 중"
        if mode != "rt":
            return ""
        m = self.motion
        if m is None or self.power is None:
            return "스테이지 연결 필요 · 쿨러 · 히트건 릴레이"
        if m.moving:
            return "이동 중 · 끝난 뒤"
        if m.estop_locked:
            return "비상정지 잠금 · 켜기 거절"
        if time.monotonic() - m.estop_t < motion_mod.ESTOP_QUIET_S:
            return "비상정지 직후 · 잠시 뒤"
        hn = int(self.cfg.get("heater.relay"))
        items = self.cfg.get("relay.items")
        rl = self.manual_relays()
        if not rl:
            return "쿨러 릴레이 번호 필요"
        if hn in rl:
            return "히트건 R%d 이 쿨러 목록에 있음 · 쿨러 다시 고르기" % hn
        if not items[hn - 1].get("enabled"):
            return "히트건 릴레이 사용 안 함 · 장비 설정 8"
        off = [n for n in rl if not items[n - 1].get("enabled")]
        if off:
            return "R%d 사용 안 함 · 장비 설정 8" % off[0]
        return rt_mod.params_block(rt_mod.params(self.cfg))

    def _man_inst(self, need_2450):
        """장비가 안 열려 있으면 연다([장비 연결]과 같은 길 - 주소 찾기 · 시리얼 주소 열지 않음)."""
        ok1 = self.k2110 is not None and self.k2110.is_open
        ok2 = self.k2450 is not None and self.k2450.is_open
        if ok1 and (ok2 or not need_2450):
            return
        try:
            self.c_inst_connect({}, None)
        except AppError as e:
            if not (self.k2110 is not None and self.k2110.is_open) or (need_2450 and not (self.k2450 is not None and self.k2450.is_open)):
                raise AppError(str(e))

    def _man_need(self, mode):
        why = self.manual_block(mode)
        if why:
            raise AppError(why)

    def c_man_temp(self, a, gen):
        """[온도 1번]: K2110 설정 후 한 번 읽는다(히트건 · 쿨러 · 출력을 건드리지 않는다)."""
        import rt as rt_mod
        from core import instruments as inst
        self._man_need("quick")
        self._begin("수동 측정")
        try:
            self._man_inst(False)
            p = rt_mod.params(self.cfg)
            self.k2110.configure(p["tc_type"], "REAL")
            t = self.k2110.read_temp()
        except inst.VisaError as e:
            raise AppError(logger.short(e))
        finally:
            self._end()
        self.manual_last = dict(self.manual_last, T=t, t_T=time.strftime("%H:%M:%S"))
        if t is None:
            raise AppError("온도 값 없음 · 열전대 확인")
        logger.log("수동 측정 · 온도 %.2f °C" % t)
        return {"T": t}

    def c_man_res(self, a, gen):
        """[저항 1번]: K2450 설정 → 출력 켬 → 한 번 읽음 → 출력 끔(어떤 경우에도)."""
        import rt as rt_mod
        from core import instruments as inst
        self._man_need("quick")
        p = rt_mod.params(self.cfg)
        self._begin("수동 측정")
        res = {}
        try:
            self._man_inst(True)
            ok, why = self.k2450.check_scpi()
            if not ok:
                raise AppError(why)
            self.k2450.configure({"terminal": p["terminal"], "four_wire": p["four_wire"], "volt": p["volt"], "ilim_a": p["ilim_a"],
                                  "range_mode": p["range_mode"], "start_range": p["start_range"], "nplc": p["nplc"], "filter_n": p["filter_n"]})
            self.k2450.output(True)
            try:
                i, trip = self.k2450.read()
                rng = inst.clamp_range(float(p["start_range"]), float(p["ilim_a"]))
                while i is None and p["range_mode"] == "step" and rng < inst.RANGES[-1]:      # 범위 넘음 - 한 단계씩 올린다
                    rng = next(r for r in inst.RANGES if r > rng * 1.0001)
                    self.k2450.set_range(rng)
                    i, trip = self.k2450.read()
            finally:
                self.k2450.output(False)
            res = {"I": i, "R": inst.resistance(p["volt"], i), "trip": bool(trip), "volt": p["volt"], "open": inst.is_open_circuit(i, p["open_a"])}
        except inst.VisaError as e:
            raise AppError(logger.short(e))
        finally:
            self._end()
        self.manual_last = dict(self.manual_last, R=res.get("R"), I=res.get("I"), t_R=time.strftime("%H:%M:%S"))
        if res.get("open"):
            raise AppError("연결 없음 · 핀 접촉 · 앞/뒤 단자 설정 확인")
        logger.log("수동 측정 · 저항 %s Ω · 전류 %s A%s" % ("—" if res["R"] is None else "%.4E" % res["R"], "—" if res["I"] is None else "%.4E" % res["I"],
                                                        " · 한도 걸림" if res["trip"] else ""))
        return res

    def _man_launch(self, mode, gen):
        import rt as rt_mod
        from core import instruments as inst
        self._man_need("rt" if mode == "manual_rt" else "cont")
        p = rt_mod.params(self.cfg)
        self._begin("수동 측정")
        try:
            self._man_inst(True)
            try:
                self.k2110.configure(p["tc_type"], "REAL")
                t0 = self.k2110.read_temp()
            except inst.VisaError as e:
                raise AppError(logger.short(e))
            if t0 is None:
                raise AppError("온도 값 없음 · 열전대 확인")
            m = self.motion
            relays, cancel, g = None, None, None
            if m is not None:
                g = gen if gen is not None else m.gen
                if g != m.gen:
                    raise AppError(self.cancel_text(g))
            if mode == "manual_rt":
                relays = self.manual_relays()
                n0 = self.link_lost_count
                cancel = lambda: self.link_lost_count != n0                    # noqa: E731
                if self.hub:
                    self.hub.configure()
                    self.hub.start()
            rr = rt_mod.RTRun(self, p, p["file_name"], cancel_fn=cancel, relays=relays, mode=mode, gen=g)
        except Exception:
            self._end()
            raise
        self.rt = rr
        self.rt_last["manual"] = rr
        rr.thread = threading.Thread(target=rr.run, name="rt-manual", daemon=True)
        rr.thread.start()
        logger.log("수동 측정 시작 · %s · %s%s" % ("R–T" if mode == "manual_rt" else "연속", p["file_name"],
                                               " · 쿨러 " + ",".join("R%d" % n for n in relays) if relays else ""))
        return {"dir": rr.run_dir}

    def c_man_cont(self, a, gen):
        """[연속 측정]: 가열 없이 온도 · 저항을 정지할 때까지(control.csv 만)."""
        return self._man_launch("manual_cont", gen)

    def c_man_rt(self, a, gen):
        """[R–T 시작]: 쿨러 켬 → 히트건 켜 최고 온도까지 → 끔 → 냉각 끝 온도 아래 또는 냉각 멈춤까지 × 반복."""
        return self._man_launch("manual_rt", gen)

    def c_man_stop(self, a, gen):
        r = self.rt
        if r is None or not r.active or not r.manual:
            raise AppError("수동 측정 중 아님")
        r.stop()

    def c_man_scpi(self, a, gen):
        """명령 직접 보내기: K2110 · K2450 에 SCPI 한 줄. '?' 로 끝나면 응답을 읽는다. 보낸 뒤 오류 큐를 읽어 돌려준다(전부 visa.log).
        K2450 출력 켜기는 보내지 않는다(저항 1번 · R–T 만 켠다). 측정 중에는 못 한다."""
        from core import instruments as inst
        self._man_need("quick")
        dev = a.get("dev")
        cmd = str(a.get("line") or "").strip()
        if dev not in ("k2110", "k2450"):
            raise AppError("장비 선택 · K2110 · K2450")
        if not cmd or len(cmd) > 200 or "\n" in cmd or "\r" in cmd:
            raise AppError("명령 한 줄 · 200자 이내")
        if dev == "k2450":
            for part in cmd.split(";"):
                q = part.strip()
                if self.OUTP_ON.match(q) or q.upper().startswith("*RCL") or ("SMU.SOURCE.OUTPUT" in q.upper().replace(" ", "") and "ON" in q.upper()):
                    raise AppError("K2450 출력 켜기는 보낼 수 없음 · [저항 1번]")
        self._begin("수동 측정")
        try:
            self._man_inst(dev == "k2450")
            d = getattr(self, dev)
            if d is None or not d.is_open:
                raise AppError("%s 연결 안 됨" % dev.upper())
            resp = None
            if cmd.endswith("?"):
                resp = d.query(cmd)
            else:
                d.write(cmd)
            errs = inst._drain_errors(d)
        except inst.VisaError as e:
            raise AppError(logger.short(e))
        finally:
            self._end()
        logger.log("명령 직접 · %s · %s%s%s" % (dev.upper(), cmd, " → " + resp if resp is not None else "", " · 오류 " + errs[0] if errs else ""))
        return {"resp": resp, "errs": errs}

    def manual_state(self):
        r = self.rt_last["manual"]
        pw = self.power
        return {"run": None if r is None else r.view(), "block_rt": self.manual_block("rt"), "block_cont": self.manual_block("cont"),
                "block_quick": self.manual_block("quick"), "coolers": self.manual_relays(), "heater": int(self.cfg.get("heater.relay")),
                "last": dict(self.manual_last), "relays_on": None if pw is None else sorted(pw.owners)}

    def _inst_off_retry(self, secs=120.0):
        """K2450 출력을 못 껐으면(통신 끊김) 통신이 돌아올 때까지 1 s 마다 다시 끈다."""
        end = time.time() + secs
        while time.time() < end and not self.shutting:
            d = self.k2450
            if d is None or not d.is_open:
                return
            try:
                d.output(False)
                logger.log("K2450 출력 끔 · 통신 복구", "warn")
                return
            except Exception:                              # noqa: BLE001
                time.sleep(1.0)

    def on_rt_end(self, rr):
        import rt as rt_mod
        if getattr(rr, "out_off_failed", False):
            threading.Thread(target=self._inst_off_retry, name="inst-off-retry", daemon=True).start()
        if getattr(rr, "relay_off_failed", False):              # 쿨러 · 히트건 끄기를 못 확인함(통신 끊김): 돌아올 때까지 다시 끈다
            threading.Thread(target=rt_mod.relays_off_retry, args=(self,), kwargs={"stop_fn": lambda: self.shutting}, name="relay-off-retry", daemon=True).start()
        if self.busy in ("R–T 측정", "속도 측정", "수동 측정"):
            self._end()
        if rr.status == "오류":
            if "장비 통신" in rr.message:                       # 측정 장비가 끊겼다 - 장치 상태 "연결 안 됨" + 띠
                for nm in ("k2110", "k2450"):                    # 핸들은 그대로 둔다(출력 끄기 재시도가 통신이 돌아오면 쓴다)
                    self.inst_info[nm] = {"ok": False, "err": "통신 끊김"}
            self.set_alarm("%s 오류 · " % ("수동 측정" if rr.manual else "R–T 측정") + rr.message[:50], "결과 탭에서 확인")
        if rr.mode == "rt" and rr.status != "중지됨":
            self._rt_cache.clear()
        self._rt_cache.clear()
        self.touch()

    def rt_results(self):
        import rt as rt_mod
        mt = 0.0
        for rn in rt_mod.ROOTS:                                 # 자동(measure) · 수동(manual)
            root = os.path.join(paths.DATA_DIR, rn)
            try:
                mt = max(mt, os.path.getmtime(root))
                for n in os.listdir(root):                      # 기록 파일(rt.json)이 새로 생기거나 바뀌면 목록을 다시 만든다
                    try:
                        mt = max(mt, os.path.getmtime(os.path.join(root, n, "rt.json")))
                    except OSError:
                        pass
            except OSError:
                pass
        if not mt:
            return []
        c = self._rt_cache.get("list")
        if c is None or c[0] != mt:
            c = (mt, rt_mod.list_results()[:40])
            self._rt_cache["list"] = c
        return c[1]

    def rt_state(self):
        import rt as rt_mod
        r = self.rt_last["auto"]
        info = self.inst_info
        return {"inst": {"k2110": info.get("k2110"), "k2450": info.get("k2450"), "msg": info.get("msg"), "test": info.get("test")},
                "run": None if r is None else r.view(), "block": self.rt_block("rt"), "block_speed": self.rt_block("speed"),
                "results": self.rt_results(), "keys": rt_mod.RT_KEYS}

    def results_list(self):
        """측정 폴더 목록(run.json 은 수정 시각이 바뀐 것만 다시 읽는다 - 폴더가 많아도 상태 전송이 느려지지 않게)."""
        out = []
        cache = self.__dict__.setdefault("_rl_cache", {})
        try:
            for n in sorted(os.listdir(paths.RUNS_DIR), reverse=True):
                fp = os.path.join(paths.RUNS_DIR, n, "run.json")
                try:
                    mt = os.path.getmtime(fp)
                except OSError:
                    continue
                c = cache.get(n)
                if c is None or c[0] != mt:
                    j = store.read_json(fp)
                    c = (mt, None if not j else {"name": n, "state": j.get("state"), "n": len(j.get("results") or {}), "summary": j.get("summary")})
                    cache[n] = c
                if c[1]:
                    out.append(c[1])
        except OSError:
            pass
        return out

    def c_results_open(self, a, gen):
        j = store.read_json(os.path.join(paths.RUNS_DIR, a["name"], "run.json"))
        if not j:
            raise AppError("측정 기록 없음")
        self.viewing = j
        self.viewing["dir"] = os.path.join(paths.RUNS_DIR, a["name"])
        return {}

    def c_open_folder(self, a, gen):
        if a.get("measure"):
            import rt as rt_mod
            a = {"path": rt_mod.result_dir(a["measure"], a.get("root") or "measure")}
        d = a.get("path") or (os.path.join(paths.RUNS_DIR, os.path.basename(a["name"])) if a.get("name") else (self.run.run_dir if self.run else paths.RUNS_DIR))
        if os.name == "nt" and not os.environ.get("WAFER2_NO_SHELL"):
            os.startfile(d)                               # noqa: S606
        return {"path": d}

    # ------------------------------------------------------------------
    # 종료
    # ------------------------------------------------------------------
    def c_focus_refind(self, a, gen):
        """초점 다시 맞추기: 판 영역 안의 선명도로 점수를 매긴다(실제 카메라 전용). 합성 카메라에는 초점이 없다."""
        if self.vision.synth is not None or self.image:
            return {"msg": "합성 카메라 · 초점 없음"}
        cam = self.vision.cam
        if cam is None or not hasattr(cam, "sweep_focus"):
            raise AppError("카메라 연결 안 됨")
        with self.vision.lock:
            cam.sweep_focus()
        img, _t = self._grab(gen, n_best=1, poly=self.calib.area)
        return {"msg": "초점 점수 %.0f" % vision_mod.sharpness(img, self.calib.area)}

    def c_dry(self, a, gen):
        """검증 전용(dry 에서만): 합성 카메라 세계 · 멈춤 · 재부팅 · 탈조 …"""
        if not self.sim:
            raise AppError("시뮬레이션에서만")
        op = a["op"]
        fk = self.stage.ser if (self.stage is not None and not self.stage.dry) else None
        if op == "world":
            kw = {k: (tuple(v) if isinstance(v, list) and k in ("shift_px", "tilt") else v) for k, v in a["kw"].items()}
            self.vision.set_world(**kw)
            if self.vision.synth and set(kw) - {"flash"}:              # flash(실시간 지연 시험)는 그림 캐시를 버리지 않는다(다시 그리는 시간이 지연에 끼지 않게)
                self.vision.synth.cache.clear()
        elif op == "hang":
            if fk is not None:
                fk.hang_move = bool(a.get("on", True))
            else:
                self.stage.dry_hang = bool(a.get("on", True))
        elif op == "cpu":
            end = time.time() + float(a["s"])

            def burn():
                while time.time() < end:
                    sum(range(2000))
            for _ in range(int(a.get("n", 4))):
                threading.Thread(target=burn, daemon=True).start()
        elif op == "cut" and fk is not None:
            fk.cut_link(float(a["s"]))
        elif op == "bang" and fk is not None:
            return {"t": fk.bang_times[-1] if fk.bang_times else None, "wall": list(self.stage.bang_wall)}
        elif op == "bang":
            return {"t": None, "wall": list(self.stage.bang_wall)}
        elif op == "fw" and self.stage is None:             # 공개된 스테이지가 없다(연결 실패 · 연결 중): 마지막 가짜 보드가 닫혔는지만
            lf = getattr(self, "_last_fake", None)
            return {"stage": None, "last_alive": None if lf is None else bool(lf.alive), "stop_wall": [], "bang_wall": []}
        elif op == "hold_txn":                              # 검증 전용: 시리얼 거래 잠금을 s 초 잡는다(늦은 이동 거절 시험)
            st = self.stage

            def hold(sec=float(a.get("s", 2.0))):
                with st._txn:
                    time.sleep(sec)
            threading.Thread(target=hold, daemon=True, name="hold-txn").start()
            time.sleep(0.05)
        elif op == "split_st" and fk is not None:           # 검증 전용: 다음 ST n 개를 줄 중간에서 둘로 나눠 보낸다(앞 조각은 줄바꿈 없음) · trunc=True 면 잘린 줄 + 뒤에 바른 줄
            fk.split_st = int(a.get("n", 1))
            fk.trunc_st = bool(a.get("trunc", False))
        elif op == "fw":                                    # 검증 전용: 펌웨어(가짜)가 받은 줄 · 끊긴 이동 · 실제 위치 · 버퍼 넘침
            if fk is not None:
                return {"log": list(fk.log), "cuts": list(fk.cuts), "bang": list(fk.bang_times), "stop_wall": list(self.stage.stop_wall),
                        "bang_wall": list(self.stage.bang_wall), "real": {k: v / 160.0 for k, v in fk.real.items()},
                        "counter": {"x": fk.px, "y": fk.py, "z": fk.pz}, "hom": [fk.hx, fk.hy, fk.hz], "overflow": fk.overflow, "relay": list(fk.relay),
                        "mono": time.monotonic(), "wall": time.time(), "alive": bool(fk.alive)}
            return {"stop_wall": list(self.stage.stop_wall), "bang_wall": list(self.stage.bang_wall), "cut": dict(self.stage.cut_info),
                    "real": {k: self.stage._dry_xy[k] for k in "xyz"}, "relay": list(self.stage._dry_relay), "hom": [self.stage._dry_homed[k] for k in "xyz"]}
        elif op == "set_real" and fk is not None:           # 실제 위치를 카운터와 따로 정한다(펌웨어는 모른다)
            fk.set_real_mm(a["axis"], float(a["mm"]))
        elif op == "forget_z" and fk is not None:           # 펌웨어 Z 원점을 없앤다(save 없이 전원이 꺼진 상태)
            fk.hz = False
            fk.pz = 0
        elif op == "freeze" and fk is not None:
            fk.freeze(float(a["s"]))
        elif op == "drop" and fk is not None:
            fk.drop_next(int(a.get("n", 1)))
        elif op == "latency" and fk is not None:
            fk.latency = float(a["s"])
        elif op == "reboot":
            if fk is not None:
                fk.reboot()
            else:
                self.stage._event("reboot", "=== 3-AXIS V11 ===")
        elif op == "slip":
            if fk is not None:
                fk.slip(a["dx"], a["dy"])
            else:
                self.stage.dry_slip(a["dx"], a["dy"])
        elif op == "z":
            if fk is not None:
                fk.set_z_mm(float(a["mm"]))
            else:
                self.stage._dry_xy["z"] = float(a["mm"])
        elif op == "block_write" and fk is not None:
            fk.block_write(float(a["s"]))
        elif op == "eeprom":
            if fk is not None:
                fk.reboot(eeprom=a.get("state", "restored"))
        elif op == "truth":
            import sys as _s
            _s.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tools"))
            import synth_cam
            sy = self.vision.synth
            return {"H": synth_cam.plate_homography(sy.w, sy.h).tolist(), "chips": [list(c) for c in sy.world["chips"]],
                    "size": [sy.w, sy.h], "quad_mm": [list(q) for q in synth_cam.PLATE_QUAD_MM]}
        elif op == "slow":
            if fk is not None:
                fk.time_scale = float(a["s"]) / 1.333 if float(a["s"]) > 0 else 0.05          # 8000 펄스 조그 한 조각이 s 초
            else:
                stage_mod.DRY_MOVE_S = float(a["s"])
        elif op == "fault":
            measure.FAULT[a["name"]] = a.get("on", True)
        elif op == "cam_delay":
            self.vision.synth.delay_s = float(a["s"])
        elif op in ("unplug", "replug"):                   # 쓰는 중 USB 가 뽑힘 / 다시 꽂음(stage · camera)
            dev = a.get("device", "stage")
            if dev == "stage":
                if op == "unplug":
                    self.sim_missing.add("stage")
                    if self.stage is not None:
                        if fk is not None:
                            self._fake_next_eeprom = "restored" if (fk.eeprom == "restored" and not getattr(fk, "dirty", False)) else "unsaved"
                            fk.unplug()
                        else:
                            self.stage.dry_unplugged = True
                else:
                    self.sim_missing.discard("stage")      # 포트 목록에 다시 나타난다 - 옛 객체는 계속 오류
            else:
                if op == "unplug":
                    self.sim_missing.add("camera")
                    self._apply_missing()
                    self.vision.unplug()
                else:
                    self.sim_missing.discard("camera")
                    self._apply_missing()
            return {"missing": sorted(self.sim_missing)}
        elif op == "open_delay":                           # 열기를 느리게(초): stage= · camera=
            if "stage" in a:
                self.open_delay["stage"] = float(a["stage"])
            if "camera" in a:
                self.vision.open_delay = float(a["camera"])
            return {}
        elif op == "counts":
            return {"stage_opens": self.opens["stage"], "camera_opens": self.vision.opens}
        elif op == "missing":                              # 장치를 빼거나 꽂는다(stage · camera · visa). 카메라를 빼면 열린 것도 놓는다.
            for k, v in a.items():
                if k not in ("stage", "camera", "visa"):
                    continue
                (self.sim_missing.add if v else self.sim_missing.discard)(k)
            self._apply_missing()
            if self.vision.missing:
                self.vision.unplug()
            return {"missing": sorted(self.sim_missing)}
        elif op == "lab":                                  # 가짜 측정 장비 조작: fault(tc_open · disconnect · ilim · slow) · 모델 값
            self._lab_install()
            for k, v in (a.get("fault") or {}).items():
                self.lab.fault[k] = v
            for k, v in (a.get("set") or {}).items():
                setattr(self.lab, k, v)
            if a.get("addrs") is not None:                  # 가짜 VISA 목록을 바꾼다(주소 찾기 시험)
                self.lab.addrs = list(a["addrs"])
            return {"T": self.lab.T, "Ts": self.lab.Ts, "R": self.lab.resistance(), "out": self.lab.out_on, "cmds": list((self.k2450.cmds if self.k2450 else [])[-60:]),
                    "cmds2110": list((self.k2110.cmds if self.k2110 else [])[-20:]), "err": [d.err for d in self.lab.dev.values()],
                    "opened": list(getattr(self.lab, "opened", [])[-20:])}
        return {}

    def c_shutdown_check(self, a, gen):
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            raise AppError("측정 중 · 중지한 뒤 종료")
        if self.rt is not None and self.rt.active:
            raise AppError("%s 중 · 정지한 뒤 종료" % self.rt_label())
        if self.busy:
            raise AppError("%s 중 · 끝난 뒤 종료" % self.busy)
        if self.motion and (self.motion.moving or not self.motion.lock.acquire(blocking=False)):
            raise AppError("이동 중 · 끝난 뒤 종료")
        if self.motion:
            self.motion.lock.release()
        return {}

    def shutdown_cleanup(self):
        """종료 순서 2~5: 히트건 · 릴레이 정책 -> save -> 시리얼 닫기 -> 카메라 놓기."""
        self.shutting = True
        self.stop_poll.set()
        if self.power:
            self.power.disabled = True                       # 이후 켜기 금지
        if self.run and self.run.thread and self.run.thread.is_alive():
            self.run.stop()                                  # 측정 스레드를 먼저 멈추고 기다린다(릴레이를 끈 뒤 다시 켜지 못하게)
            self.run.thread.join(10.0)
        if self.rt is not None and self.rt.active:
            self.rt.stop()
            th = getattr(self.rt, "thread", None)
            if th is not None:
                th.join(10.0)
        try:
            self._inst_close()                               # K2450 출력 끄고 닫기
        except Exception:                                    # noqa: BLE001
            pass
        try:
            if self.power:
                if self.cfg.get("relay.on_exit") == "off":
                    self.power.all_off()
                else:
                    self.power.heater_off()
        except Exception as e:                              # noqa: BLE001
            logger.log("종료 · 릴레이 끄기 실패 " + logger.short(e), "err")
        try:
            if self.hub:
                self.hub.stop()
            if self.stage and not (self.motion and self.motion.estop_locked):
                self.stage.save()
        except Exception as e:                              # noqa: BLE001
            logger.log("종료 · save 실패 " + logger.short(e), "warn")
        try:
            if self.stage:
                self.stage.close()
        except Exception:                                   # noqa: BLE001
            pass
        try:
            self.vision.close()
        except Exception:                                   # noqa: BLE001
            pass

    # ------------------------------------------------------------------
    # 상태 (화면으로 가는 전부)
    # ------------------------------------------------------------------
    def phase(self):
        m = self.motion
        r = self.run
        if m is None:                                    # 연결 문제는 상태 칸과 띠가 맡는다 - 상태 필은 동작 상태만
            return "대기"
        if m.estop_locked:
            return "비상정지"
        if r and r.state in ("running", "paused", "stopping"):
            if r.state == "stopping":
                return "중지 중"
            if r.waiting:
                return "확인 대기"
            if r.state == "paused" or r.paused:
                return "일시정지"
            return "측정 중 %d / %d" % (min(len(r.results) + 1, len(r.samples)), len(r.samples))
        if self.rt is not None and self.rt.active:
            return "%s 중" % self.rt_label()
        if self.busy == "촬영" or self.busy == "빈 판 사진" or self.busy == "위치 확인":
            return "촬영 중"
        if self.busy or m.moving:
            return "이동 중"
        if self.alarm and self.alarm["text"].startswith("측정 오류"):
            return "오류"
        return "대기"

    def calib_view(self):
        c = self.calib
        code, text = c.status(self.ref_id(), self._size())
        hc, ht = self.head.status(self.ref_id(), self._size())
        return {"status": code, "text": text, "points": c.points, "area": c.area, "exclude": c.d.get("exclude") or [],
                "size_mm": c.d.get("size_mm"), "rms": c.d.get("rms_mm"), "area_mm": self._area_mm(), "applied": c.d.get("applied"), "warnings": c.warnings(),
                "pending": self.pending_calib, "min_apply": calibstore.MIN_APPLY, "ref_id": c.d.get("ref_id"),
                "head": {"status": hc, "text": ht, "pairs": self.head.d["pairs"], "rms": self.head.d.get("rms_mm")}, "msgs": self.calib_msgs[-3:]}

    def _area_mm(self):
        c = self.calib
        if c.H is None or not c.area:
            return None
        return [[round(v, 2) for v in c.px_to_mm(u, v_)] for u, v_ in c.area]

    def state(self):
        m, cfg = self.motion, self.cfg
        shot = self.shot
        dev = self.devices()
        cam_ok = dev["camera"]["state"] == "ok"
        st = {
            "v": self.version, "dry": self.sim, "phase": self.phase(), "alarm": self.alarm, "dev": dev, "bands": self.bands(dev), "cam_scan": self.cam_scan,
            "stage": None if m is None else {
                "connected": True, "x": self.pos["x"], "y": self.pos["y"], "z": self.pos["z"], "moving": m.moving, "locked": m.estop_locked,
                "z_ok": m.z_ok, "xy_ok": m.xy_ok, "recovery": m.recovery_steps(), "gen": m.gen,
                "jog_block": self.move_block("이동", jog=True, ignore_moving=True, ignore_recent=True), "move_block": self.move_block("이동", ignore_recent=True),
                "done": dict(m.xy_done), "limits": stage_mod.limits_mm(), "fw_homed": dict(self.fw_homed),
                "origin_text": "원점 정상" if (m.z_ok and m.xy_ok and not m.estop_locked) else "원점 없음", "free_move": self.free_active()},
            "camera": {"open": cam_ok, "state": dev["camera"]["state"], "size": self.vision.size if cam_ok else None,
                "text": ("%s×%s · 노출 %s · %s K" % (*(self.vision.size or (cfg.get("width"), cfg.get("height"))), cfg.get("exposure"), cfg.get("wb_temperature"))) if cam_ok else "—",
                "cap_text": self.cap_text, "cap_error": self.cap_error, "busy": self.busy, "preview_n": self.vision.preview_n},
            "calib": self.calib_view(),
            "ref": None if not self.ref_meta else dict(self.ref_meta, history=self.ref_history()),
            "snap_size": None if not self.snap else [int(self.snap["img"].shape[1]), int(self.snap["img"].shape[0])],
            "shot": None if not shot else {"id": shot["id"], "time": shot["time"], "size": shot["size"], "coords_ok": shot["coords_ok"],
                                           "warnings": shot["warnings"], "path": shot["path"], "cam": shot["cam"], "area": shot.get("area_cur"),
                                           "ref_id": shot.get("ref_id"), "fail": shot.get("fail")},
            "samples": [self._pub(s) for s in self.samples], "sel": self.sel, "can_undo": bool(self.edits),
            "n_detect": len(self.samples), "n_weak": sum(1 for s in self.samples if s["weak"]),
            "eeprom": self.eeprom,
            "fw": None if self.stage is None else self.stage.fw_version,
            "port": None if self.stage is None else (self.stage.port_name or ("가상" if self.sim else None)),
            "contact": {"z": cfg.get("contact_z_mm"), "approach": cfg.get("approach_mm"), "sense": bool(cfg.get("contact.sense")),
                        "block": contact_mod.block(self), "last": None if not self.contact_last else _contact_view(self.contact_last)},
            "band": self.band(), "stop_note": self.stop_note,
            "relays": None if self.power is None else {"owners": dict(self.power.owners), "heater": cfg.get("heater.relay"),
                                                       "heater_on": bool(self.power.owners.get(int(cfg.get("heater.relay"))))},
            "settings": cfg.values, "conditions": [self._cond_view(c) for c in cfg.get("condition.items")],
            "active_condition": cfg.get("condition.active"),
            "run": None if self.run is None else self.run.view(),
            "verify": getattr(self, "verify_result", None),
            "tool": conditions.active_tool(cfg.values),
            "results": self.results_list()[:30] if not str(self.phase()).startswith("측정 중") else [],
            "start_block": self._start_block(),
            "rt": self.rt_state(),
            "manual": self.manual_state(),
        }
        if self.last_run is not None and (self.run is self.last_run):
            st["run"] = self.last_run.view()
        return st

    def _start_block(self):
        try:
            why, notes = self.preflight(self._condition(), None)
            return {"why": why, "notes": notes}
        except Exception as e:                              # noqa: BLE001
            return {"why": logger.short(e), "notes": []}

    def _cond_view(self, c):
        est = conditions.estimate_sample_s(c, self.cfg.values, 6.0, float(self.cfg.get("motion.speed_pps")) / 160.0)
        return {"name": c["name"], "est_s": round(est), "steps": [dict(s, i=i, text=conditions.describe(s, self.cfg.values)) for i, s in enumerate(c["steps"])]}

    @staticmethod
    def _pub(s):
        return {k: s.get(k) for k in ("no", "u", "v", "ru", "rv", "verts", "X", "Y", "cooler", "weak", "excluded", "manual", "source")}

    def log_tail(self):
        """화면 아래 한 줄 소식: 쉬운 말 로그만(내부 용어가 든 detail 은 로그 전체 보기에만)."""
        return [e for e in logger.recent(40) if e["lv"] in ("info", "warn", "err")][-1:]


__all__ = ["App", "AppError", "datetime", "tip"]
