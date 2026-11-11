"""control.py - 명령을 받아 안전 규칙을 지나 장비에 보낸다. 서버와 시험이 같은 입구(run_command)를 쓴다.

거절은 Rejected(쉬운 한국어 한 문장). 이동은 한 번에 하나만(이동 잠금) - 여러 단계 이동(Z 올리기 → X · Y)이 다른 이동과 섞이지 않게 한다.
"""

import threading
import time
from contextlib import contextmanager
from typing import Optional

from app import config as config_mod
from app.config import Config
from app.devices import meters
from app.devices.camera import Camera, CameraError
from app.devices import stage as stage_mod
from app.devices.stage import PPMM, Stage, StageError
from app.logs import Logs
from app.safety import Rejected, Safety
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
    def __init__(self, cfg: Optional[Config] = None, fake: bool = False, fake_scale: float = 0.2, clock=time.monotonic):
        self.cfg = cfg or Config().load()
        self.fake = fake
        self.fake_scale = fake_scale
        self.state = State()
        self.logs = Logs(self.cfg.data_path())
        self.safety = Safety(self.cfg, self.state, clock)
        self.stage = Stage(self._open_port, gen_ok=self.safety.gen_ok, log=self.logs.serial,
                           on_event=self._stage_event, on_status=self._stage_status)
        self.k2110: Optional[meters.K2110] = None
        self.k2450: Optional[meters.K2450] = None
        self.camera = Camera(log=self.logs.app, on_event=self._camera_event)
        self.fake_fw = None
        self.lab = None
        self._motion = threading.Lock()
        self._plock = threading.Lock()                              # 조그 대기 칸 · 현재 이동 종류
        self._cur_kind = None
        self._jog_pending = None                                    # 대기 칸 하나: {"axis", "mm"}
        self._monitor_on = False
        self._alive = True
        self._threads = []
        meters.LOG = self.logs.visa
        for n in self.cfg.loaded_notes:
            self.logs.app(n, "warn")
            self.state.alarm("config", n, NOTICE)
        if fake:
            self._setup_fake()
        self.publish_cfg()
        self._start_threads()

    # ------------------------------------------------------------------
    # 가짜 장비
    # ------------------------------------------------------------------
    def _setup_fake(self):
        from sim import fake_visa
        heater = int(self.cfg["heater_relay"]) - 1
        self.lab = fake_visa.Lab(heater_fn=lambda: bool(self.fake_fw and self.fake_fw.relay[heater]),
                                 cooler_fn=lambda: bool(self.fake_fw and any(r for i, r in enumerate(self.fake_fw.relay) if i != heater)))
        self.lab.read_dt = 0.02                                     # 가짜 읽기 시간(실장비는 길다)
        self.lab.addrs = ["ASRL5::INSTR", "ASRL4::INSTR", self.cfg["k2110_addr"], self.cfg["k2450_addr"]]
        fake_visa.install(self.lab)
        from sim.fake_camera import open_fake
        self.camera.open_capture = open_fake(lambda: self.fake_fw.true_xy() if self.fake_fw else (0.0, 0.0))

    def _open_port(self, name):
        if self.fake:
            from sim.fake_v11 import FakeV11
            self.fake_fw = FakeV11(time_scale=self.fake_scale, homed=True)
            return self.fake_fw
        return open_serial(name)

    # ------------------------------------------------------------------
    # 연결
    # ------------------------------------------------------------------
    def list_devices(self):
        visa, note = meters.list_resources()
        return {"serial_ports": ["COM5"] if self.fake else serial_ports(), "visa": visa, "note": note, "cameras": [0, 1, 2, 3]}

    def connect_stage(self, port: Optional[str] = None):
        port = (port or self.cfg["stage_port"]).strip()
        if not port:
            raise Rejected("스테이지 포트를 골라 주십시오")
        if self.stage.connected:
            raise Rejected("스테이지가 이미 연결되어 있습니다")
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
        self._heater_bookkeeping(False)
        self._set_move(None)

    def connect_meter(self, kind: str, addr: Optional[str] = None):
        if kind not in ("k2110", "k2450"):
            raise Rejected("알 수 없는 장비입니다")
        cls = meters.K2110 if kind == "k2110" else meters.K2450
        addr = (addr or self.cfg["%s_addr" % kind]).strip()
        if meters.visa_kind(addr) == "시리얼":
            raise Rejected("시리얼 포트는 측정 장비로 열지 않습니다")
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

    def _heater_bookkeeping(self, on: bool):
        if on and self.safety.heater_on_t is None:
            self.safety.heater_turned_on()
            self.state.set("heater_on_since", time.time())
        elif not on and self.safety.heater_on_t is not None:
            self.safety.heater_turned_off()
            self.state.set("heater_on_since", None)

    def _heater_on(self) -> bool:
        return self.safety.heater_on_t is not None

    def _stage_event(self, kind: str, text: str):
        """일꾼 스레드에서 온다 - 오래 걸리는 일은 새 스레드로 넘긴다."""
        if kind == "reboot":
            threading.Thread(target=self._board_reboot, args=(text,), daemon=True, name="reboot").start()
        elif kind == "wd_trip":
            self.state.alarm("wd", "통신 감시가 릴레이를 껐습니다 (통신이 2 초 넘게 끊겼습니다)", NOTICE)
            self.logs.app("통신 감시 작동 · %s" % text, "warn")
        elif kind == "lost":
            self.state.set_device("stage", "오류", "통신 끊김 · " + text[:40], self.stage.port_name)
            self.state.alarm("stage_lost", "스테이지 통신이 끊겼습니다. 케이블을 확인하고 다시 연결하십시오 (릴레이는 펌웨어 통신 감시가 끕니다)", ALARM)
            self._heater_bookkeeping(False)
            self.logs.app("스테이지 통신 끊김 · %s" % text, "error")

    def _board_reboot(self, text):
        """보드가 다시 시작됐다: 비상정지와 같이 잠그고, wd 를 다시 보내고, 위치 확인 경보를 낸다."""
        self.safety.begin_estop()
        self.state.alarm("reboot", "보드가 다시 시작됨 · 위치 확인 필요. 위치를 확인한 뒤 [비상정지 풀기]를 누르십시오", ALARM)
        self.logs.app("보드 재시작 감지 · %s" % text, "error")
        self._heater_bookkeeping(False)
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
        self.safety.begin_estop()
        with self._plock:
            self._jog_pending = None                                # 대기 중이던 조그를 지운다
        self._monitor_on = False
        self.state.set("monitor", False)
        banged = False
        try:
            banged = self.stage.bang()
        except StageError as e:
            self.state.alarm("estop_io", "비상정지 신호를 보내지 못했습니다 (%s). 모터 전원을 직접 끄십시오" % str(e)[:30], ALARM)
        self.logs.app("비상정지 (%s) · '!' %s · %.1f ms" % (source, "전송" if banged else "못 보냄", (time.monotonic() - t0) * 1000))
        if banged:
            try:
                self.stage.submit("r all 0", None, stage_mod.is_rs, 3.0, prio=0).result(timeout=10)
            except Exception as e:                                  # noqa: BLE001
                self.state.alarm("estop_relay", "릴레이를 끄지 못했습니다 (%s). 히트건 · 쿨러 전원을 직접 끄십시오" % str(e)[:30], ALARM)
        self._heater_bookkeeping(False)
        dev = self.k2450
        if dev is not None:
            try:
                dev.output_off_urgent(1.0)
            except Exception as e:                                  # noqa: BLE001
                self.state.alarm("estop_k2450", "K2450 출력을 끄지 못했습니다 (%s). 장비에서 직접 끄십시오" % str(e)[:30], ALARM)
        return {"locked": True}

    def estop_release(self):
        if self.stage.connected:
            try:
                self.stage.call("st", None, stage_mod.is_st, 3.0, prio=0)
                self.stage.read_relays()
            except StageError as e:
                raise Rejected("위치를 읽지 못해 풀지 않았습니다 · %s" % e)
        self.safety.release()
        self.state.clear_alarm("estop_io", "estop_relay", "estop_k2450")
        self.logs.app("비상정지 풀림 · 위치를 다시 읽음")
        return {"locked": False}

    # ------------------------------------------------------------------
    # 이동
    # ------------------------------------------------------------------
    def _need_stage(self):
        if not self.stage.connected:
            raise Rejected("스테이지가 연결되어 있지 않습니다")

    def _set_move(self, what, target=None, pending=None):
        """이동 중 정보(펌웨어는 이동 중 위치를 알려 주지 않는다). 끝나면 None."""
        self.state.set("move", None if what is None else {"what": what, "target": target, "pending": pending})

    @contextmanager
    def _moving(self, what, target=None, lock=True):
        if lock and not self._motion.acquire(blocking=False):
            raise Rejected("다른 이동이 진행 중입니다. 끝난 뒤에 눌러 주십시오")
        self.state.set("busy", what)
        self._set_move(what, target)
        try:
            yield
        except Rejected:
            raise
        except stage_mod.Discarded:
            raise Rejected("비상정지로 취소되었습니다")
        except stage_mod.MoveAborted:
            raise Rejected("이동이 중단되었습니다. 위치를 확인하십시오")
        except stage_mod.StageRejected as e:
            raise Rejected(str(e))
        except StageError as e:
            raise Rejected("스테이지 오류 · %s" % e)
        finally:
            self.state.set("busy", "")
            self._set_move(None)
            if lock:
                self._motion.release()

    def _mm(self, ax):
        p = self.stage.pos.get(ax)
        return None if p is None else p / PPMM

    def jog(self, axis: str, mm: float):
        """조그. 이동 중에 들어온 조그는 대기 칸 하나에 받아 두었다가 지금 이동이 끝나면 이어서 한다
        (같은 축 · 같은 방향이면 거리를 더하고, 다르면 대기를 새 것으로 바꾼다). 비상정지는 대기를 지운다."""
        axis = str(axis).lower()
        if axis not in AXES:
            raise Rejected("축은 x · y · z 중 하나여야 합니다")
        self._need_stage()
        mm = float(mm)
        if stage_mod.mm_to_pulse(mm) == 0:
            raise Rejected("움직일 거리가 너무 짧습니다")
        why = self.safety.reject_move(axis in "xy", self._heater_on())
        if why:
            raise Rejected(why)
        if not self._motion.acquire(blocking=False):
            with self._plock:
                if self._cur_kind != "jog":
                    raise Rejected("다른 이동이 진행 중입니다. 끝난 뒤에 눌러 주십시오")
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
            raise Rejected("%s 축 원점이 없어 좌표로 갈 수 없습니다. 먼저 원점을 잡으십시오" % "·".join(miss))
        for ax, v in (("x", x), ("y", y)) + ((("z", z),) if z is not None else ()):
            why = self.safety.reject_target(ax, v, True)
            if why:
                raise Rejected(why)
        g = self.safety.generation
        why = self.safety.reject_move(True, self._heater_on())
        if why:
            raise Rejected(why)
        with self._moving("좌표로 가는 중", {"x": round(x, 3), "y": round(y, 3), "z": round(z if z else 0.0, 3)}):
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
            raise Rejected("Z 가 맨 위에 있는지 확인되지 않아 움직이지 않았습니다")

    def z_top(self):
        self._need_stage()
        if not self.stage.homed.get("z"):
            raise Rejected("Z 원점이 없어 맨 위로 올릴 수 없습니다. 먼저 원점을 잡으십시오")
        g = self.safety.generation
        why = self.safety.reject_move(False, False)
        if why:
            raise Rejected(why)
        with self._moving("Z 맨 위로", {"x": self._mm("x"), "y": self._mm("y"), "z": 0.0}):
            self.stage.goto("z", 0, g)
        return {"pos": dict(self.state.get("pos"))}

    def set_origin(self, axis: str, mode: str = "here"):
        """mode: here = 지금 자리를 원점으로 · end = 끝단까지 밀어서 잡기. 끝나면 save."""
        axis = str(axis).lower()
        if mode not in ("here", "end"):
            raise Rejected("원점 방식은 지금 자리 / 끝단 밀기 중 하나여야 합니다")
        if axis not in (AXES + ("xy" if mode == "here" else "")):
            raise Rejected("축은 x · y · z 중 하나여야 합니다")
        self._need_stage()
        xy_push = mode == "end" and axis in "xy"
        if xy_push and not self.stage.homed.get("z"):
            raise Rejected("Z 원점을 먼저 잡아 주십시오")           # 핀이 내려간 채 X · Y 를 밀면 판을 긁을 수 있다
        g = self.safety.generation
        why = self.safety.reject_move(xy_push, self._heater_on())
        if why:
            raise Rejected(why)
        with self._moving("원점 잡는 중", None):
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
        return {"homed": dict(self.state.get("homed"))}

    # ------------------------------------------------------------------
    # 릴레이
    # ------------------------------------------------------------------
    def relay_on(self, n: int):
        n = self._relay_no(n)
        self._need_stage()
        heater = n == int(self.cfg["heater_relay"])
        if heater:
            why = self.safety.reject_heater_on(self.k2110 is not None and self.k2110.is_open)
        else:
            why = self.safety.reject_relay_on()
        if why:
            raise Rejected(why)
        g = self.safety.generation
        try:
            self.stage.relay(n, True, g)
        except stage_mod.Discarded:
            raise Rejected("비상정지로 취소되었습니다")
        except StageError as e:
            raise Rejected("릴레이를 켜지 못했습니다 · %s" % e)
        return {"relays": list(self.state.get("relays"))}

    def relay_off(self, n: int):
        n = self._relay_no(n)
        self._need_stage()
        try:
            self.stage.relay(n, False, None)                        # 끄기는 비상정지 뒤에도 된다
        except StageError as e:
            raise Rejected("릴레이를 끄지 못했습니다 · %s" % e)
        return {"relays": list(self.state.get("relays"))}

    def relays_off(self):
        self._need_stage()
        try:
            self.stage.relays_off(None, prio=1)
        except StageError as e:
            raise Rejected("릴레이를 끄지 못했습니다 · %s" % e)
        return {"relays": list(self.state.get("relays"))}

    @staticmethod
    def _relay_no(n):
        try:
            n = int(n)
        except (TypeError, ValueError):
            raise Rejected("릴레이 번호는 1~10 입니다")
        if not 1 <= n <= 10:
            raise Rejected("릴레이 번호는 1~10 입니다")
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
        reason = self.safety.heater_trip_reason(now)
        if reason:
            self._heater_off(reason)

    def _heater_off(self, reason: str):
        self.logs.app("히트건 끔 · %s" % reason, "error")
        self.state.alarm("heater", reason, ALARM)
        heater = int(self.cfg["heater_relay"])
        try:
            if self.stage.connected:
                self.stage.relay(heater, False, None, prio=0)
        except StageError as e:
            self.state.alarm("heater_off_fail", "히트건을 끄지 못했습니다 (%s). 전원을 직접 끄십시오" % str(e)[:30], ALARM)
        self._heater_bookkeeping(False)

    def read_temp(self):
        dev = self.k2110
        if dev is None or not dev.is_open:
            raise Rejected("온도계(K2110)가 연결되어 있지 않습니다")
        try:
            v = dev.read_temp()
        except meters.VisaError as e:
            self.state.set_device("k2110", "오류", str(e), dev.addr)
            raise Rejected(str(e))
        self.feed_temperature(v)
        return {"temp": v, "note": "" if v is not None else "값 없음 (열전대 끊김 또는 범위 넘음)"}

    def read_resistance(self):
        dev = self.k2450
        if dev is None or not dev.is_open:
            raise Rejected("저항계(K2450)가 연결되어 있지 않습니다")
        c = self.cfg
        try:
            r = dev.measure_once(c["meter_volt"], c["meter_ilim_a"], c["meter_four_wire"], c["meter_nplc"], c["meter_avg_count"])
        except meters.VisaError as e:
            self.state.set_device("k2450", "오류", str(e), dev.addr)
            raise Rejected(str(e))
        self.state.update("resistance", ohm=r["ohm"], current=r["current"], volt=r["volt"], four_wire=r["four_wire"], time=time.time())
        return r

    def raw(self, target: str, line: str):
        line = str(line).strip()
        if not line:
            raise Rejected("명령을 입력하십시오")
        if target == "stage":
            self._need_stage()
            tok = line.split()
            head = tok[0].lower()
            if head == "!":
                self.estop("직접 명령")
                return {"reply": "비상정지"}
            if self.safety.locked and head not in ("st", "p", "rs", "w"):
                raise Rejected("비상정지 상태라 보낼 수 없습니다. 비상정지를 풀어 주십시오")
            if head == "r" and len(tok) >= 3 and tok[2] == "1":
                raise Rejected("릴레이 켜기는 릴레이 버튼으로 하십시오 (안전 규칙을 지나야 합니다)")
            if head in MOTION_WORDS or head[:1] in ("g", "j", "m") and len(head) == 2:
                raise Rejected("이동 · 원점 명령은 이동 버튼으로 하십시오 (안전 규칙을 지나야 합니다)")
            try:
                lines = self.stage.raw(line, self.safety.generation)
            except StageError as e:
                raise Rejected(str(e))
            return {"reply": "\n".join(lines)}
        if target in ("k2110", "k2450"):
            dev = getattr(self, target)
            if dev is None or not dev.is_open:
                raise Rejected("장비가 연결되어 있지 않습니다")
            try:
                return {"reply": dev.raw(line)}
            except meters.VisaError as e:
                raise Rejected(str(e))
        raise Rejected("대상은 stage · k2110 · k2450 중 하나입니다")

    # ------------------------------------------------------------------
    # 카메라 · 설정 공개 · 자동 연결 · 계속 읽기
    # ------------------------------------------------------------------
    def connect_camera(self, index: Optional[int] = None):
        if self.camera.connected:
            raise Rejected("카메라가 이미 연결되어 있습니다")
        c = self.cfg
        idx = c["camera_index"] if index is None else int(index)
        self.state.set_device("camera", "연결 중", "", str(idx))
        try:
            info = self.camera.open(idx, c["camera_width"], c["camera_height"], c["camera_fourcc"], c["camera_focus_fixed"],
                                    c["camera_focus_value"], c["preview_width"])
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
            self.state.set_device("camera", "오류", "프레임을 받지 못함 · USB 확인", str(self.cfg["camera_index"]))
            self.state.alarm("camera_lost", "카메라가 끊겼습니다. USB 를 확인하고 다시 연결하십시오", NOTICE)
            self.logs.app("카메라 끊김 · %s" % text, "warn")

    def _remember(self, **kw):
        """연결에 성공한 포트 · 주소 · 번호를 settings 에 저장한다(자동 연결이 쓴다). 저장 실패는 연결에 영향 없다."""
        try:
            self.cfg.update(kw)
            self.cfg.save()
        except (config_mod.ConfigError, OSError) as e:
            self.logs.app("설정 저장 실패 · %s" % e, "warn")
        self.publish_cfg()

    CFG_PUBLIC = ("stage_port", "k2110_addr", "k2450_addr", "camera_index", "auto_connect", "heater_relay", "heater_max_c",
                  "heater_max_on_s", "heater_min_off_s", "watchdog_ms", "cooler_grid", "photo_head_xy", "meter_volt", "meter_four_wire",
                  "meter_avg_count", "meter_nplc", "meter_ilim_a", "x_max_mm", "y_max_mm", "z_max_mm")

    def publish_cfg(self):
        """화면이 쓰는 설정 일부를 state 로 내보낸다(설정이 바뀔 때마다)."""
        self.state.set("cfg", {k: self.cfg[k] for k in self.CFG_PUBLIC})

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
                raise Rejected("비상정지 상태입니다. 비상정지를 풀어 주십시오")
            if self.k2450 is None and self.k2110 is None:
                raise Rejected("온도계나 저항계를 먼저 연결하십시오")
        self._monitor_on = on
        self.state.set("monitor", on)
        return {"monitor": on}

    def _monitor_loop(self):
        while self._alive:
            t0 = time.monotonic()
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
                        self.state.alarm("monitor", "계속 읽기를 껐습니다 · %s" % e, NOTICE)
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
            if dev is not None and dev.is_open:
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
            raise Rejected("알 수 없는 명령입니다: %s" % name)
        try:
            return getattr(self, fn)(**(args or {}))
        except TypeError as e:
            raise Rejected("명령 값이 맞지 않습니다 · %s" % str(e)[:60])

    def shutdown(self):
        """닫을 때: 히트건 · 쿨러 끄기 → K2450 출력 끄기 → 연결 닫기."""
        self._alive = False
        try:
            if self.stage.connected:
                self.stage.relays_off(None, prio=0)
        except Exception as e:                                      # noqa: BLE001
            self.logs.app("종료 중 릴레이 끄기 실패 · %s" % e, "error")
        for kind in ("k2450", "k2110"):
            self._close_meter(kind)
        self.camera.close()
        self.stage.disconnect()
        self.logs.app("종료")


COMMANDS = {
    "connect_stage": "connect_stage", "disconnect_stage": "disconnect_stage",
    "connect_meter": "connect_meter", "disconnect_meter": "disconnect_meter", "devices": "list_devices",
    "connect_camera": "connect_camera", "disconnect_camera": "disconnect_camera", "monitor": "monitor",
    "jog": "jog", "goto": "goto", "z_top": "z_top", "set_origin": "set_origin",
    "relay_on": "relay_on", "relay_off": "relay_off", "relays_off": "relays_off",
    "read_temp": "read_temp", "read_resistance": "read_resistance", "raw": "raw",
    "estop_release": "estop_release",
}

__all__ = ["Controller", "COMMANDS", "Rejected", "config_mod"]
