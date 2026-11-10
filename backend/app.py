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
import shutil
import threading
import time

import cv2
import numpy as np

import calibstore
import conditions
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
        for d in (paths.DATA_DIR, paths.SHOTS_DIR, paths.RUNS_DIR, paths.LOGS_DIR, paths.REF_HISTORY_DIR):
            os.makedirs(d, exist_ok=True)
        self.stage = self.motion = self.power = self.hub = None
        self.calib = calibstore.Calib()
        self.head = calibstore.HeadCalib()
        self.vision = vision_mod.Vision(self.cfg, self._true_xy, self.sim, image)
        self.lock = threading.RLock()
        self._estop_lk = threading.Lock()
        self._estop_ex = concurrent.futures.ThreadPoolExecutor(max_workers=1, thread_name_prefix="estop")
        self._estop_fut = None
        self._estop_t = -1e9
        self._estop_ex.submit(lambda: None).result()                 # 스레드를 미리 만든다(첫 비상정지가 스레드 생성 때문에 늦지 않게)
        self.version = 0
        self.busy = ""                               # 촬영 | 위치 확인 | 헤드 위치 보정 | 공구 가열 | 빈 판 사진 …
        self.alarm = None
        self.ref_meta = store.read_json(paths.REF_META_PATH)
        self._ref_img = None
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
        self.rt = None                               # R–T 측정(지금 또는 마지막)
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

    def ids(self):
        return {"ref": (self.ref_meta or {}).get("id"), "calib": self.calib.d.get("created"), "calib_ref": self.calib.d.get("ref_id"),
                "head": self.head.d.get("ref_id")}

    def shot_dir_name(self):
        return None if not self.shot else self.shot["id"]

    def set_alarm(self, text, nxt=""):
        self.alarm = {"text": text, "next": nxt, "t": time.time()} if text else None
        self.touch()

    def _size(self):
        return self.vision.size

    def ref_id(self):
        return (self.ref_meta or {}).get("id")

    def ref_img(self):
        if self._ref_img is None and self.ref_meta and os.path.exists(paths.REF_PATH):
            self._ref_img = imgio.imread_u(paths.REF_PATH)
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
                return fake_v11.FakeV11(time_scale=float(os.environ.get("WAFER2_FAKE_SCALE", "0.05")), eeprom=ee)
            stage_mod.SERIAL_FACTORY = _mk
        st = stage_mod.Stage(port=port, dry=self.dry)
        st.on_line = lambda arrow, text: self.serial_log.append((time.strftime("%H:%M:%S"), arrow, text))
        self.opens["stage"] += 1
        if self.sim and self.open_delay["stage"] > 0:
            time.sleep(self.open_delay["stage"])
        st.open()
        self.stage = st
        stage_mod.set_limits(self.cfg.get("limits.x_max_mm"), self.cfg.get("limits.y_max_mm"), self.cfg.get("limits.z_max_mm"))
        stage_mod.configure(speed_pps=self.cfg.get("motion.speed_pps"), jog_pps=self.cfg.get("motion.jog_pps"),
                            jog_slow_pps=self.cfg.get("motion.jog_slow_pps"), line_timeout_s=self.cfg.get("serial.line_timeout_s"),
                            move_timeout_s=self.cfg.get("serial.move_timeout_s"))
        self.motion = motion_mod.Motion(st, log=lambda m: logger.log(m, "warn"))
        self.power = power_mod.Power(st, heater_n=self.cfg.get("heater.relay"), log=logger.log)
        self.motion.pre_xy = self.power.pre_xy
        self.motion.on_reboot = self._on_reboot
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
        self._apply_motion_cfg()
        self.hub = measure.Hub(self.cfg, self.power, self.motion, self.sim)
        self.hub.configure()
        self.hub.start()
        self._init_firmware()
        self.eeprom = st.eeprom_state()
        self.motion.sync_from_status()
        self._link_alarmed = False
        if os.path.exists(os.path.join(paths.DATA_DIR, "estop.lock")):                    # 비상정지 잠금이 풀리지 않은 채 꺼졌었다
            self.motion.estop_locked = True
            self.motion.z_ok = False
            self.motion._xy_lost()
            st._aborted = True
            self.set_alarm("비상정지 잠금 이어짐 · 원점 다시 잡기", "회복: " + " → ".join(self.motion.recovery_steps()))
        self.vision.true_xy = self._true_xy
        self.touch()
        if self._ka is None or not self._ka.is_alive():
            self._ka = threading.Thread(target=self._keepalive, name="keepalive", daemon=True)
            self._ka.start()
        logger.log("스테이지 연결 · %s" % (st.fw_version or "?"))
        if self.cfg.get("photohome.on_connect") and self.motion.z_ok and self.motion.xy_ok:
            threading.Thread(target=self._connect_verify, name="connect-verify", daemon=True).start()

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

    def _apply_motion_cfg(self):
        m = self.motion
        m.speed_pps = int(self.cfg.get("motion.speed_pps"))
        m.accel = int(self.cfg.get("motion.accel"))
        m.slow_pps = int(self.cfg.get("motion.jog_slow_pps"))
        m.jog_guard = self._jog_guard

    def _init_firmware(self):
        """연결 직후 · 재부팅 뒤: 속도 · 가속 · 통신 감시를 다시 보낸다."""
        st = self.stage
        with self.motion.lock:
            try:
                st.set_profile(self.cfg.get("motion.accel"), self.cfg.get("motion.start_pps"))
                st.watchdog(int(self.cfg.get("relay.watchdog_ms")))
            except StageError as e:
                logger.log("펌웨어 설정 실패 · " + logger.short(e), "warn")

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
    def estop_async(self):
        """비상정지 요청(WS · HTTP 둘 다 여기로). 전용 스레드 하나에서만 실행한다 - 이벤트 루프 · 명령 풀에서 쓰기 잠금을 기다리지 않는다.
        300 ms 안의 같은 요청은 앞 요청의 결과를 그대로 돌려준다(한 번만 처리). Future 를 돌려준다."""
        now = time.monotonic()
        with self._estop_lk:
            f = self._estop_fut
            if f is not None and now - self._estop_t < 0.3:
                return f
            self._estop_t = now
            self._estop_fut = f = self._estop_ex.submit(self.estop)
            return f

    def estop(self):
        """즉시. 잠금을 기다리지 않는다."""
        if self.motion is None:
            return {"ok": True}
        heater = int(self.cfg.get("heater.relay"))
        policy_off = self.cfg.get("relay.on_estop") == "off"
        try:
            self.motion.estop(relays_off=policy_off, off_relays=(heater,))
        except Exception as e:                              # noqa: BLE001 - 포트가 뽑힌 상태(SerialException 등)여도 잠금 · 경보는 반드시 건다
            logger.log("비상정지 전송 실패 · " + logger.short(e), "err")
        if self.power:
            self.power.owners.clear() if policy_off else self.power.owners.pop(heater, None)
        if self.run:
            self.run.stop()
        if self.rt is not None and self.rt.active:
            self.rt.stop()
        threading.Thread(target=self._inst_off, daemon=True, name="estop-inst").start()      # K2450 출력 끄기(VISA 는 느릴 수 있어 별도 스레드)
        threading.Thread(target=self._confirm_heater_off, daemon=True, name="estop-heater").start()
        self.pending_verify = None
        self._lock_file(True)
        self.set_alarm("비상정지 · 이동 잠김", "회복: " + " → ".join(self.motion.recovery_steps()))
        logger.log("비상정지", "err")
        return {"ok": True}

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
        t0 = time.time()
        while self.connecting["camera"] and time.time() - t0 < 8.0:
            time.sleep(0.05)
        if not (self.vision.cam is not None and self.vision.frame_ok):
            raise AppError("카메라 연결 안 됨 · USB 확인")

    def _no_run(self, what="이 동작"):
        if self.run and self.run.state in ("running", "paused", "stopping"):
            raise AppError("측정 중 · %s 못 함" % what)
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · %s 못 함" % what)

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
    def c_settings_save(self, a, gen):
        if self.run and self.run.state in ("running", "paused", "stopping"):
            raise AppError("측정 중 · 설정 변경 잠김")
        ok, errs, warns = self.cfg.save(a["patch"])
        if not ok:
            k = next(iter(errs))
            raise AppError("%s · %s" % (k, errs[k]))
        if self.motion:
            self._apply_motion_cfg()
            if "relay.watchdog_ms" in a["patch"] and self.motion.lock.acquire(blocking=False):
                try:
                    self.stage.watchdog(int(self.cfg.get("relay.watchdog_ms")))
                except Exception:                           # noqa: BLE001
                    pass
                finally:
                    self.motion.lock.release()
            if self.power:
                self.power.heater_n = int(self.cfg.get("heater.relay"))
            self.hub.configure()
        return {"warnings": warns}

    def c_settings_check(self, a, gen):
        """설정 창의 칸 옆 오류 표시용: 저장하지 않고 검증만."""
        errs, warns = self.cfg.check(a.get("patch") or {})
        return {"errs": errs, "warns": warns}

    def c_settings_import_old(self, a, gen):
        self._no_run()
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
            raise AppError("촬영 중 비상정지 · 결과 버림")
        return img, tm

    def c_capture(self, a, gen):
        self._no_run("촬영")
        self._need_camera()
        self._begin("촬영")
        try:
            if not self.calib.area:
                raise AppError("판 영역 필요 · 장비 설정 3")
            self.cap_error = None
            with (self.motion._locked(True) if self.motion else contextlib.nullcontext()):
                if self.motion and gen != self.motion.gen:
                    raise AppError("비상정지로 중단")
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
        self._need_stage()
        self._no_run("이동")
        s = self._find(int(a["no"]))
        if s["X"] is None:
            raise AppError("좌표 없음 · 좌표 보정 필요")
        tl = conditions.active_tool(self.cfg.values)
        px, py = s["X"] - float(tl.get("dx_mm") or 0.0), s["Y"] - float(tl.get("dy_mm") or 0.0)
        lx, ly = self.cfg.get("limits.x_max_mm"), self.cfg.get("limits.y_max_mm")
        if not (0 <= px <= lx and 0 <= py <= ly):
            raise AppError("가동범위 밖 · X %.1f Y %.1f" % (px, py))
        self._begin("이동")
        try:
            self.motion.move_xy(px, py, gen=gen, nowait=True)
            st = self.motion.status()
            self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        finally:
            self._end()

    # ------------------------------------------------------------------
    # 이동 · 조그 · 회복
    # ------------------------------------------------------------------
    def c_jog(self, a, gen):
        """조그 한 번. 이동 · 작업 중이면 줄 세우지 않고 즉시 거절한다(기존 프로그램과 같다). rep = 누르고 있는 연속 · free = 접촉 높이 화면(Z 아래 한도 없음)."""
        self._need_stage()
        if self.run and self.run.state == "running" and not self.run.paused:
            raise AppError("측정 중 · 일시정지 뒤 가능")
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · 이동 못 함")
        if self.busy:
            raise AppError("%s 중 · 끝난 뒤" % self.busy)
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
        self.motion.jog(ax, mm, gen=gen, nowait=True, opts={"rep": bool(a.get("rep")), "free": bool(a.get("free"))})
        st = self.motion.status()
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))

    def _jog_guard(self, ax, mm, st, opts, z_ok):
        """조그 규칙(Motion.jog 이 이동 잠금 안에서 부른다). 거절하면 MotionError."""
        c = self.cfg
        cz = c.get("contact_z_mm")
        z = None if st is None else st.get("z_mm")
        if ax == "z":
            zmax = float(c.get("jog.z_max_mm"))
            if abs(mm) > zmax + 1e-9:
                raise MotionError("Z 는 한 번 최대 %g mm" % zmax, "z_step")
            if opts.get("rep") and mm > 0:
                raise MotionError("Z↓ 는 한 번에 한 스텝", "z_repeat")
            if mm > 0 and cz is not None and z_ok and not opts.get("free"):
                lim = float(cz) + float(c.get("jog.z_over_contact_mm"))
                if z is None or z + mm > lim + 1e-9:
                    raise MotionError("접촉 높이 %.2f + %.1f mm 아래로 못 내림" % (float(cz), float(c.get("jog.z_over_contact_mm"))), "z_deep")
        else:
            if opts.get("rep") and abs(mm) > float(c.get("jog.repeat_max_mm")) + 1e-9:
                raise MotionError("연속 조그는 스텝 %g mm 이하만" % float(c.get("jog.repeat_max_mm")), "repeat")
            if cz is not None and z_ok:
                if z is None or z > float(cz) - float(c.get("jog.xy_clear_mm")) + 1e-9:
                    raise MotionError("핀이 내려가 있음 · Z 를 올린 뒤", "pin_down")

    def c_move_xy(self, a, gen):
        self._need_stage()
        self._no_run("이동")
        if self.motion.moving:
            raise AppError("이동 중 · 끝난 뒤")
        st = self.motion.move_xy(float(a["x"]), float(a["y"]), gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))

    def c_goto_named(self, a, gen):
        self._need_stage()
        self._no_run("이동")
        key = {"park": "park_xy", "capture": "capture_xy"}[a["name"]]
        x, y = self.cfg.get(key)
        st = self.motion.move_xy(x, y, gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        if a["name"] == "park":
            self.save_position()                            # 대기 위치에 도착 - 다음 부팅에 원점이 복원되게

    def c_z_top(self, a, gen):
        self._need_stage()
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · 이동 못 함")
        st = self.motion.move_z(0.0, gen=gen, nowait=True)
        self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))

    def c_origin(self, a, gen):
        """원점 잡기(펌웨어 fz): 축 x | y | z. X · Y 는 Z 원점이 있고 Z 가 맨 위일 때만. 펌웨어 위치가 남아 있으면 끝단 5 mm 앞까지 먼저 다가간다."""
        self._need_stage()
        self._no_run("원점 잡기")
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
            first = self.eeprom != "restored" and not m.estop_locked
            return {"text": ("원점 없음" if first else "비상정지 잠금"), "next": "다음: " + " → ".join(m.recovery_steps()),
                    "err": not first, "actions": self.recovery_actions()}
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

    def c_alarm_clear(self, a, gen):
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
        if self.rt is not None and self.rt.active:
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
        self._need_stage()
        self._no_run("위치 확인")
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
        self._no_run("빈 판 사진")
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
        self._need_stage()
        self._no_run("헤드 위치 보정")
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
            self.head.clear()
            skipped = 0
            with self.motion._locked(True):
                self.motion._enter(gen)
                for i, y in enumerate(ys):
                    for x in (xs if i % 2 == 0 else xs[::-1]):
                        self.cap_text = "격자 %d / %d" % (len(self.head.d["pairs"]) + skipped + 1, n * n)
                        self.touch()
                        self.motion.move_xy(float(x), float(y), gen=gen, nowait=True)
                        st = self.motion.status()
                        img, tm = self._grab(gen, n_best=1)
                        _px, rp, why = self._head_px_ref(img)
                        if rp is None:
                            skipped += 1
                            logger.log("헤드 보정 점 건너뜀 · " + why, "warn")
                            continue
                        self.head.add(rp[0], rp[1], st["x_mm"], st["y_mm"], self.ref_id(), self._size())
            ok, why = self.head.fit([c.px_to_mm(u, v) for u, v in c.area])
            if not ok:
                self.head.save()
                raise AppError("헤드 위치 보정 실패 · " + why)
            self.head.save()
            return {"n": len(self.head.d["pairs"]), "rms": self.head.d["rms_mm"], "skipped": skipped}
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
        """지금 Z 높이를 접촉 높이로 저장한다(샘플 위에서 핀이 닿는 자리까지 Z 조그로 내린 뒤)."""
        self._need_stage()
        self._no_run("접촉 높이 저장")
        if not self.motion.z_ok or self.motion.estop_locked:
            raise AppError("원점 필요 · 장비 설정 1")
        st = self.motion.status()
        z = st.get("z_mm")
        zmax = float(self.cfg.get("limits.z_max_mm"))
        if z is None or not (0.1 <= z <= zmax):
            raise AppError("Z %s mm · 저장 못 함 (0.1~%g)" % ("?" if z is None else "%.2f" % z, zmax))
        if a.get("no") is not None:
            self._over_sample_check(a["no"])(st)            # 샘플을 골랐으면 그 위에서만 저장
        ok, errs, _w = self.cfg.save({"contact_z_mm": round(float(z), 2), "contact_tool": conditions.active_tool(self.cfg.values).get("name")})
        if not ok:
            raise AppError("접촉 높이 저장 실패 · " + next(iter(errs.values())))
        logger.log("접촉 높이 저장 · Z %.2f mm" % z)
        return {"z": round(float(z), 2)}

    def _contact_tool_ok(self):
        ct = self.cfg.get("contact_tool")
        if ct and ct != conditions.active_tool(self.cfg.values).get("name"):
            raise AppError("접촉 높이는 '%s' 로 잰 값 · 지금 공구와 다름" % ct)

    def c_contact_clear(self, a, gen):
        self._no_run("접촉 높이 지우기")
        self.cfg.save({"contact_z_mm": None, "contact_tool": None})

    def c_contact_try(self, a, gen):
        """저장한 접촉 높이로 내려 보기 - X · Y 가 고른 샘플 위일 때만."""
        self._need_stage()
        self._no_run("접촉 높이 내려 보기")
        z = self.cfg.get("contact_z_mm")
        if z is None:
            raise AppError("접촉 높이 필요 · 먼저 저장")
        self._contact_tool_ok()
        chk = self._over_sample_check(a["no"]) if a.get("no") is not None else None
        if chk is None and not a.get("free"):
            raise AppError("샘플을 고른 뒤")                  # free: 측정 탭 '이 위치 측정' - 사람이 조그로 핀을 맞춘 자리에서 내림
        self._begin("접촉 높이로 내리기")
        try:
            st = self.motion.descend_contact(float(z), float(self.cfg.get("approach_mm")), gen=gen, check_xy=chk, nowait=True)
            self.pos.update(x=st["x_mm"], y=st["y_mm"], z=st.get("z_mm"))
        finally:
            self._end()

    # ------------------------------------------------------------------
    # 릴레이 (수동)
    # ------------------------------------------------------------------
    def c_relay(self, a, gen):
        self._need_stage()
        n, on = int(a["n"]), bool(a["on"])
        if self.run and self.run.state == "running":
            raise AppError("측정 중 · 수동 릴레이 못 씀")
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · 수동 릴레이 못 씀")
        if on and not self.cfg.get("relay.items")[n - 1].get("enabled"):
            raise AppError("R%d 사용 안 함 · 장비 설정 8" % n)
        if self.motion.estop_locked and on:
            raise AppError("비상정지 잠금 · 켜기 거절")
        self.power.set(n, on, "manual")

    def c_relay_test(self, a, gen):
        """릴레이 하나를 2 s 켜 본다(끝나면 반드시 끈다). 히트건 릴레이도 시간 한도 안에서만 켠다."""
        self._need_stage()
        self._no_run("릴레이 시험")
        n = int(a["n"])
        if not 1 <= n <= 10:
            raise AppError("릴레이 번호 1~10")
        if not self.cfg.get("relay.items")[n - 1].get("enabled"):
            raise AppError("R%d 사용 안 함 · 장비 설정 8" % n)
        if self.motion.estop_locked:
            raise AppError("비상정지 잠금 · 켜기 거절")
        self._begin("릴레이 시험")
        try:
            n0 = self.link_lost_count
            canc = lambda: self.motion.gen != gen or self.link_lost_count != n0          # noqa: E731
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
            return "R–T 측정 중", []
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
        if conditions.uses_heat(cond) and not self.cfg.get("relay.items")[int(self.cfg.get("heater.relay")) - 1].get("enabled"):
            return "히트건 릴레이 사용 안 함 · 장비 설정 8", []
        errs, _w = conditions.validate_condition(cond, self.cfg.values, self.cfg.get("limits.z_max_mm"), prefix="c")
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
            raise AppError("비상정지로 중단")
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
            cz = self.cfg.get("contact_z_mm")
            z = self.pos.get("z")
            return cz is not None and z is not None and abs(float(z) - float(cz)) <= 0.5
        self.lab = fake_visa.Lab(clock=clk.now, sleep=lambda s: time.sleep(s / clk.scale), heater_fn=heater, cooler_fn=cooler, pin_down_fn=pin)
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

    def c_inst_connect(self, a, gen):
        """K2110 · K2450 을 연다(읽기만 - *IDN? · 명령 세트). 출력은 켜지 않는다."""
        from core import instruments as inst
        self._no_run("장비 연결")
        if self.sim:
            self._lab_install()
        self._inst_close()
        info = {"k2110": None, "k2450": None, "msg": "", "test": self.inst_info.get("test")}
        for nm, cls, addr in (("k2110", inst.K2110, self.cfg.get("rt.k2110_addr")), ("k2450", inst.K2450, self.cfg.get("rt.k2450_addr"))):
            try:
                d = cls(addr)
                idn = d.open()
                if nm == "k2450":
                    ok, why = d.check_scpi()
                    if not ok:
                        d.close()
                        info[nm] = {"ok": False, "addr": addr, "err": why}
                        continue
                setattr(self, nm, d)
                info[nm] = {"ok": True, "addr": addr, "idn": idn, "kind": inst.visa_kind(addr)}
            except inst.VisaUnavailable as e:
                info[nm] = {"ok": False, "addr": addr, "err": str(e)}
                info["msg"] = str(e)
            except inst.VisaError as e:
                info[nm] = {"ok": False, "addr": addr, "err": logger.short(e)}
        self.inst_info = info
        logger.log("측정 장비 연결 · K2110 %s · K2450 %s" % ("연결" if info["k2110"] and info["k2110"]["ok"] else "안 됨", "연결" if info["k2450"] and info["k2450"]["ok"] else "안 됨"))
        if not (info["k2110"] and info["k2110"]["ok"] and info["k2450"] and info["k2450"]["ok"]):
            bad = [v["err"] for v in (info["k2110"], info["k2450"]) if v and not v["ok"]]
            raise AppError(bad[0] if bad else "장비 연결 실패")
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
            return "R–T 측정 중"
        why = rt_mod.RTRun.start_block(self, mode, check_pin=True, fresh=fresh)
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
            raise AppError("비상정지로 중단")
        relays = self.cooler_relays_for(p["cooler_target"], p["cooler_custom"])
        n0 = self.link_lost_count
        self._begin("R–T 측정" if mode == "rt" else "속도 측정")
        name = p["file_name"] if mode == "rt" else "speed_"
        rr = rt_mod.RTRun(self, p, name, cancel_fn=lambda: self.link_lost_count != n0, relays=relays, mode=mode, speed_s=speed_s, gen=gen)
        self.rt = rr
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
        if self.rt is None or not self.rt.active:
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
        r = self.rt
        if r is None or r.active:
            raise AppError("끝난 R–T 측정 없음")
        try:
            ok = r.retry_nas()
        except Exception as e:                               # noqa: BLE001
            raise AppError(logger.short(e))
        if not ok:
            raise AppError("NAS 에 쓰지 못함 · 경로 확인")
        return {}

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
        if getattr(rr, "out_off_failed", False):
            threading.Thread(target=self._inst_off_retry, name="inst-off-retry", daemon=True).start()
        if self.busy in ("R–T 측정", "속도 측정"):
            self._end()
        if rr.status == "오류":
            if "장비 통신" in rr.message:                       # 측정 장비가 끊겼다 - 장치 상태 "연결 안 됨" + 띠
                for nm in ("k2110", "k2450"):                    # 핸들은 그대로 둔다(출력 끄기 재시도가 통신이 돌아오면 쓴다)
                    self.inst_info[nm] = {"ok": False, "err": "통신 끊김"}
            self.set_alarm("R–T 측정 오류 · " + rr.message[:50], "결과 탭에서 확인")
        if rr.mode == "rt" and rr.status != "중지됨":
            self._rt_cache.clear()
        self._rt_cache.clear()
        self.touch()

    def rt_results(self):
        import rt as rt_mod
        root = os.path.join(paths.DATA_DIR, "measure")
        try:
            mt = os.path.getmtime(root)
            for n in os.listdir(root):                          # 기록 파일(rt.json)이 새로 생기거나 바뀌면 목록을 다시 만든다
                try:
                    mt = max(mt, os.path.getmtime(os.path.join(root, n, "rt.json")))
                except OSError:
                    pass
        except OSError:
            return []
        c = self._rt_cache.get("list")
        if c is None or c[0] != mt:
            c = (mt, rt_mod.list_results()[:40])
            self._rt_cache["list"] = c
        return c[1]

    def rt_state(self):
        import rt as rt_mod
        r = self.rt
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
            a = {"path": os.path.join(paths.DATA_DIR, "measure", os.path.basename(a["measure"]))}
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
            if self.vision.synth:
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
            return {"T": self.lab.T, "Ts": self.lab.Ts, "R": self.lab.resistance(), "out": self.lab.out_on, "cmds": list((self.k2450.cmds if self.k2450 else [])[-60:]),
                    "cmds2110": list((self.k2110.cmds if self.k2110 else [])[-20:]), "err": [d.err for d in self.lab.dev.values()]}
        return {}

    def c_shutdown_check(self, a, gen):
        if self.run and self.run.state in ("ready", "running", "paused", "stopping"):
            raise AppError("측정 중 · 중지한 뒤 종료")
        if self.rt is not None and self.rt.active:
            raise AppError("R–T 측정 중 · 중지한 뒤 종료")
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
            return "R–T 측정 중" if self.rt.mode == "rt" else "속도 측정 중"
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
                "done": dict(m.xy_done), "limits": stage_mod.limits_mm(), "fw_homed": dict(self.fw_homed),
                "origin_text": "원점 정상" if (m.z_ok and m.xy_ok and not m.estop_locked) else "원점 없음"},
            "camera": {"open": cam_ok, "state": dev["camera"]["state"], "size": self.vision.size if cam_ok else None,
                "text": ("%s×%s · 노출 %s · %s K" % (*(self.vision.size or (cfg.get("width"), cfg.get("height"))), cfg.get("exposure"), cfg.get("wb_temperature"))) if cam_ok else "—",
                "cap_text": self.cap_text, "cap_error": self.cap_error, "busy": self.busy, "preview_n": self.vision.preview_n},
            "calib": self.calib_view(),
            "ref": None if not self.ref_meta else dict(self.ref_meta, history=self.ref_history()),
            "shot": None if not shot else {"id": shot["id"], "time": shot["time"], "size": shot["size"], "coords_ok": shot["coords_ok"],
                                           "warnings": shot["warnings"], "path": shot["path"], "cam": shot["cam"], "area": shot.get("area_cur"),
                                           "ref_id": shot.get("ref_id"), "fail": shot.get("fail")},
            "samples": [self._pub(s) for s in self.samples], "sel": self.sel, "can_undo": bool(self.edits),
            "n_detect": len(self.samples), "n_weak": sum(1 for s in self.samples if s["weak"]),
            "eeprom": self.eeprom,
            "fw": None if self.stage is None else self.stage.fw_version,
            "port": None if self.stage is None else (self.stage.port_name or ("가상" if self.sim else None)),
            "contact": {"z": cfg.get("contact_z_mm"), "approach": cfg.get("approach_mm")},
            "band": self.band(),
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
