"""rt.py - R–T 측정(저항–온도): 예비 냉각 → 출력 켬 → (가열 → 냉각) × 반복. 히트건 켜기/끄기만으로 목표 온도선을 따라간다.

장비: K2110(K형 열전대 온도) · K2450(전압 걸고 전류 측정). 저항 = Set voltage ÷ 전류.
가열은 히트건(R10), 냉각은 판 아래 쿨러(R1~R9: 측정 내내 켜 둔다). 열전대는 핀에 붙어 있어 핀이 접촉 높이일 때만 판 온도를 잰다.

안전(전부 설정): 비상 차단 온도 · 히트건 연속 켜짐 상한(heater.max_on_s) · 온도 값 없음/끊김 3 s · 단계 시간 한도 ·
비상정지 · 중지 · 오류 · 종료 · 통신 끊김 → 히트건 끔 + K2450 출력 끔. 히트건은 power.py 의 규칙(가열 전용 · 끄기 RS 확인)을 그대로 쓴다.
NAS 쓰기는 별도 스레드 - 느린 네트워크가 히트건 제어를 붙잡지 않는다.

파일(기존 LabVIEW 프로그램과 같다 - K2110 and K2450 for VO2_20250703.vi 2쪽 · 3쪽):
  [File name][Set voltage 소수 2자리]V_[회차].dat 와 [File name][Set voltage]V_merged.dat   예) 1-1_0.10V_0.dat (회차 번호는 0 부터)
  회차 파일 첫 줄 "Temp   Resistance   "(공백 3 · 탭 없음) + CRLF, merged 는 머리글 없음, 값은 LabVIEW %.7E(E+1 · E-1), 탭 구분 · CRLF.
"""

import copy
import json
import ntpath
import os
import queue
import shutil
import threading
import time

import conditions
import logger
from core import instruments as inst
from core import paths
from core.motion import MotionError
from core.power import HEAT, PowerError
from core.stage import StageError

OK, ERROR, STOPPED = "완료", "오류", "중지됨"
RT_KEYS = ["k2110_addr", "k2450_addr", "precool_c", "hold_s", "max_temp", "break_temp", "repeat", "heat_rate", "cool_rate", "band_c",
           "min_on_s", "min_off_s", "emergency_c", "phase_limit_s", "min_interval_s", "stale_s", "volt", "ilim_a", "four_wire", "terminal", "open_a",
           "filter_n", "nplc", "range_mode", "start_range", "tc_type", "cooler_target", "cooler_custom", "file_path", "date_folder", "batch_folder", "file_name",
           "cooler_off_end", "z_top_end"]
Z_TOL_MM = 0.5                         # 핀이 접촉 높이라고 보는 허용 오차
MAX_POINTS = 200000
OPEN_N = 5                              # 전류가 하한 아래인 읽기가 이만큼 연속이면 경고(측정은 계속)
NO_RISE_S = 45.0                      # 히트건을 이만큼 연속으로 켰는데
NO_RISE_C = 0.5                       # 온도가 이만큼도 안 오르면 오류(열전대가 판에서 떨어졌거나 핀이 들림)


class RTStop(Exception):
    pass


class RTFail(Exception):
    pass


def params(cfg):
    """설정(rt.*)을 평평한 dict 로."""
    p = {k: cfg.get("rt." + k) for k in RT_KEYS}
    if p["emergency_c"] is None:
        p["emergency_c"] = float(p["max_temp"]) + 10.0
    return p


def file_stem(file_name, volt):
    """[File name][Set voltage 소수 2자리]V_  (예: 1-1_1.00V_)"""
    return "%s%.2fV_" % (file_name, float(volt))


def cycle_name(file_name, volt, cycle):
    """회차 파일 이름. 파일 번호는 0 부터(기존 LabVIEW 파일과 같게) - 화면의 "n회" 는 1 부터이므로 cycle(1 부터) - 1."""
    return "%s%d.dat" % (file_stem(file_name, volt), int(cycle) - 1)


def merged_name(file_name, volt):
    return "%smerged.dat" % file_stem(file_name, volt)


HEADER = "Temp   Resistance   "          # 회차 파일 첫 줄(Temp 뒤 공백 3 · Resistance 뒤 공백 3 · 탭 없음). merged 파일에는 머리글이 없다.


def fmt_e(x):
    """LabVIEW %.7E 형식: 가수 소수 7자리, 지수는 부호를 붙이고 0 을 채우지 않는다(E+1 · E-1 · E+0)."""
    m, e = ("%.7E" % float(x)).split("E")
    return "%sE%s%d" % (m, "-" if int(e) < 0 else "+", abs(int(e)))


def data_line(t, r):
    return "%s\t%s" % (fmt_e(t), fmt_e(r))


def nas_target(base, auto_date, batch, now=None):
    """NAS 저장 폴더 = 기본 폴더 / [연도 / YYYYMMDD(측정 시작 날짜)] / [묶음 번호]. 기본 폴더가 비면 "" (PC 에만 저장).
    날짜 자동을 끄면 기본 폴더 그대로(묶음 번호가 있으면 그 아래). 화면 미리보기(UI.nasPath)와 같은 규칙."""
    base = (base or "").strip()
    if not base:
        return ""
    t = time.localtime(time.time() if now is None else now)
    parts = []
    if auto_date:
        parts += [time.strftime("%Y", t), time.strftime("%Y%m%d", t)]
    if (batch or "").strip():
        parts.append(batch.strip())
    return ntpath.join(base, *parts)


def unique_path(path):
    """같은 이름이 있으면 덮어쓰지 않고 _1, _2 … 를 붙인다. (경로, 바뀌었는가)"""
    if not os.path.exists(path):
        return path, False
    base, ext = os.path.splitext(path)
    n = 1
    while os.path.exists("%s_%d%s" % (base, n, ext)):
        n += 1
    return "%s_%d%s" % (base, n, ext), True


# --------------------------------------------------------------------------
class HeatCtl:
    """히트건 켜기/끄기 판단. 순수 로직(시간은 인자로). 최소 켜짐/꺼짐 · 연속 상한을 지킨다.

    want(T, target, band, on): 선보다 band 이상 낮으면 켜고 높으면 끈다. 띠 안에서는 지금 상태 유지.
    """

    def __init__(self, min_on, min_off, max_on):
        self.min_on, self.min_off, self.max_on = float(min_on), float(min_off), float(max_on)
        self.on = False
        self.t_on = None
        self.t_off = -1e9
        self.forced = 0                 # 연속 상한으로 강제로 끈 횟수

    @staticmethod
    def want(T, target, band, on):
        if target is None:
            return on
        if T < target - band:
            return True
        if T > target + band:
            return False
        return on

    def step(self, now, want):
        """반환: (켜야 하는가, 사건 | None). 사건 = "max_on" 강제 끔."""
        ev = None
        if self.on:
            if now - self.t_on >= self.max_on:
                self.on, self.t_off, self.t_on, ev = False, now, None, "max_on"
                self.forced += 1
            elif not want and now - self.t_on >= self.min_on:
                self.on, self.t_off, self.t_on = False, now, None
        else:
            if want and now - self.t_off >= self.min_off:
                self.on, self.t_on = True, now
        return self.on, ev

    def force_off(self, now):
        if self.on:
            self.on, self.t_off, self.t_on = False, now, None


def target_line(start_c, rate_c_min, t_since):
    """목표 온도선. rate 가 None(최대)이면 None."""
    if not rate_c_min:
        return None
    return start_c + float(rate_c_min) / 60.0 * t_since


# --------------------------------------------------------------------------
class NasCopier(threading.Thread):
    """NAS 로 복사하는 별도 스레드. 못 쓰면 대기 목록에 두고 경고만 낸다(측정은 계속)."""

    def __init__(self, nas_dir, warn):
        super().__init__(name="rt-nas", daemon=True)
        self.nas_dir, self.warn = nas_dir, warn
        self.q = queue.Queue()
        self.names = {}                   # 로컬 경로 -> NAS 에 쓴 이름
        self.pending = []                 # 못 쓴 로컬 경로
        self.done = []
        self.ok = None                    # None 아직 · True · False

    def put(self, local):
        self.q.put(local)

    def run(self):
        while True:
            local = self.q.get()
            if local is None:
                return
            self._copy(local)

    def finish(self):
        self.q.put(None)

    def _copy(self, local):
        if not self.nas_dir:
            return
        try:
            if not os.path.isdir(self.nas_dir):
                os.makedirs(self.nas_dir, exist_ok=True)
            dst = self.names.get(local)
            if dst is None:
                dst, changed = unique_path(os.path.join(self.nas_dir, os.path.basename(local)))
                if changed:
                    self.warn("NAS 에 같은 이름이 있어 %s 로 저장" % os.path.basename(dst))
                self.names[local] = dst
            shutil.copyfile(local, dst)
            if local in self.pending:
                self.pending.remove(local)
            if local not in self.done:
                self.done.append(local)
            self.ok = True if not self.pending else self.ok
        except OSError as e:
            if local not in self.pending:
                self.pending.append(local)
            if self.ok is not False:
                self.warn("NAS 에 쓰지 못함 · 측정은 계속 · 끝난 뒤 다시 복사")
                logger.log("NAS 쓰기 오류 · %s" % e, "detail")
            self.ok = False


# --------------------------------------------------------------------------
class RTRun:
    """R–T 측정 한 번. run() 은 호출한 스레드에서 끝까지 돈다(앱은 별도 스레드에서 부른다).

    app: backend.App (k2110 · k2450 · power · motion · hub · cfg 를 쓴다)
    cancel_fn: 중지/비상정지/통신 끊김 여부 callable() -> bool
    """

    def __init__(self, app, p, name_stem, run_dir=None, cancel_fn=None, relays=None, mode="rt", speed_s=20.0, z_top_end=None, gen=None):
        self.app, self.p = app, dict(p)
        self.gen_arg = gen
        self.mode, self.speed_s = mode, float(speed_s)
        self.clock = app.hub.clock
        self.cancel_fn = cancel_fn or (lambda: False)
        self.cancel = threading.Event()
        self.name = name_stem                       # File name 으로 쓰는 문자열(샘플 순회: File name + 번호 + "_")
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_dir = run_dir or os.path.join(paths.DATA_DIR, "measure", "%s_%s" % (self.stamp, _safe(name_stem)))
        self.relays = list(relays) if relays is not None else None
        self.z_top_end = self.p["z_top_end"] if z_top_end is None else bool(z_top_end)
        self.state = "ready"                        # ready | running | stopping | done | stopped | error
        self.status = ""                            # 완료 | 오류 | 중지됨
        self.message = ""
        self.phase = ""
        self.cycle = 0
        self.warns = []
        self.points = []
        self.last = {"T": None, "R": None, "I": None, "target": None, "trip": False, "range": None}
        self.t0 = None
        self.nas_dir = ""                                # 이번 측정의 NAS 저장 폴더(시작 때 한 번 정함)
        self.t_end = None
        self.files = []
        self.heat = HeatCtl(self.p["min_on_s"], self.p["min_off_s"], conditions.heater_max_s(app.cfg.values))
        self.cooler_on = []
        self.trips = 0
        self._low = 0                                   # 전류가 하한 아래인 연속 읽기 수(열린 회로 감지)
        self.open_warned = False
        self.speed = None
        self._range = inst.clamp_range(float(self.p["start_range"]), float(self.p["ilim_a"]))
        self._last_valid = None
        self._prev_valid = None
        self.out_on = False
        self._progress = time.monotonic()
        self._guard_stop = threading.Event()
        self._on_t0 = self._on_T0 = None
        self._last_rec = -1e9
        self._ctl = None
        self._cyc_f = None
        self._mrg_f = None
        self._cyc_path_open = None
        self.nas = None
        self.cooler_names = []
        self.thread = None
        self.finished = threading.Event()

    # ---- 제어 ---------------------------------------------------------
    @property
    def active(self):
        return self.state in ("running", "stopping")

    def stop(self):
        self.cancel.set()
        if self.state == "running":
            self.state = "stopping"

    def _warn(self, text):
        if text not in self.warns:
            self.warns.append(text)
            logger.log(text, "warn")

    def _check(self):
        if self.cancel.is_set() or self.cancel_fn() or self.app.motion.gen != self.gen:
            raise RTStop()
        if self.app.link_lost_count != self._link0:
            raise RTFail("통신 끊김 · 릴레이 꺼짐")

    # ---- 시작 조건 ----------------------------------------------------
    @staticmethod
    def start_block(app, mode="rt", check_pin=True, fresh=False):
        """시작할 수 없는 사유 한 줄 | "". fresh = 위치를 장비에서 지금 읽는다(시작 순간 · 순회 단계). 화면 상태용은 주기 갱신값."""
        if app.motion is None:
            return "스테이지 연결 안 됨"
        if not app.k2110 or not app.k2110.is_open:
            return "K2110 연결 필요 · 장비 연결"
        if mode == "rt" and (not app.k2450 or not app.k2450.is_open):
            return "K2450 연결 필요 · 장비 연결"
        if app.motion.estop_locked or not app.motion.z_ok:
            return "원점 확인 필요"
        if check_pin:
            cz = app.cfg.get("contact_z_mm")
            if cz is None:
                return "접촉 높이 필요 · 장비 설정 7"
            z = None if fresh else app.pos.get("z")
            try:
                z = app.motion.status().get("z_mm") if z is None else z
            except Exception:                                   # noqa: BLE001
                z = None
            if z is None or abs(float(z) - float(cz)) > Z_TOL_MM:
                return "핀이 접촉 높이가 아님 · 접촉 높이로 내리기"
        return ""

    # ---- 실행 ---------------------------------------------------------
    def run(self):
        app = self.app
        self._link0 = app.link_lost_count
        self.state = "running"
        self.t0 = time.time()
        gen = app.motion.gen if self.gen_arg is None else self.gen_arg
        self.gen = gen
        status, msg = OK, ""
        self._progress = time.monotonic()
        self._guard_stop = threading.Event()
        threading.Thread(target=self._guard, name="rt-guard", daemon=True).start()
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            self.nas_dir = nas_target(self.p.get("file_path"), self.p.get("date_folder"), self.p.get("batch_folder"), self.t0) if self.mode == "rt" else ""
            self.nas = NasCopier(self.nas_dir, self._warn) if self.nas_dir else None
            if self.nas:
                self.nas.start()
            self._open_ctl()
            self._write_meta()
            with app.motion.op(gen):
                self._setup()
                if self.mode == "speed":
                    self._speed_test()
                else:
                    self._prechill()
                    self.k2450.output(True)
                    self.out_on = True
                    for cyc in range(1, int(self.p["repeat"]) + 1):
                        self._open_cycle(cyc)
                        self._heating(cyc)
                        self._cooling(cyc)
                        self._close_cycle(cyc)
        except RTStop:
            status, msg = STOPPED, "중지됨"
        except RTFail as e:
            status, msg = ERROR, str(e)
        except inst.VisaError as e:
            status, msg = ERROR, str(e)
        except (PowerError, StageError, MotionError) as e:
            status, msg = ERROR, logger.short(e)
        except Exception as e:                                   # noqa: BLE001
            logger.log("R–T 측정 예외 · " + logger.short(e), "err", raw=repr(e))
            status, msg = ERROR, "오류 · " + logger.short(e, 40)
        finally:
            if status == ERROR and app.motion.gen != self.gen:       # 비상정지로 쓰기가 취소된 것은 오류가 아니라 중단이다
                status, msg = STOPPED, "비상정지로 중단"
            self._finish(status, msg)

    def _guard(self):
        """독립 감시(데드맨): 측정 루프가 한 바퀴도 못 돌고 오래 멈추면(VISA 가 막힘 · 스레드 예외) 히트건을 끈다. 실제 시간 기준."""
        scale = max(0.01, float(getattr(self.clock, "scale", 1.0)))
        limit = float(self.p["stale_s"]) / scale + 2.0
        while not self._guard_stop.wait(0.5):
            if self.app.power is not None and self.app.power.owners.get(self.app.power.heater_n) and time.monotonic() - self._progress > limit:
                logger.log("R–T 측정 루프 멈춤 · 히트건 강제로 끔", "err")
                try:
                    self.app.power.heater_off()
                except Exception:                                    # noqa: BLE001
                    pass

    @property
    def k2110(self):
        return self.app.k2110

    @property
    def k2450(self):
        return self.app.k2450

    def _setup(self):
        p, app = self.p, self.app
        self.phase = "장비 설정"
        self.k2110.configure(p["tc_type"], "REAL")
        if self.mode == "rt":
            ok, why = self.k2450.check_scpi()
            if not ok:
                raise RTFail(why)
            self.k2450.configure({"terminal": p["terminal"], "four_wire": p["four_wire"], "volt": p["volt"], "ilim_a": p["ilim_a"],
                                  "range_mode": p["range_mode"], "start_range": p["start_range"], "nplc": p["nplc"], "filter_n": p["filter_n"]})
        # 쿨러 켬 · 히트건 끔
        for n in self._cooler_relays():
            app.power.set(n, True, "cooler")
            self.cooler_on.append(n)
        app.power.heater_off()
        self._check()

    def _cooler_relays(self):
        if self.relays is not None:
            return self.relays
        r = self.app.cooler_relays_for(self.p["cooler_target"], self.p["cooler_custom"])
        if not r:
            raise RTFail("샘플 구역에 쿨러 없음")
        self.relays = r
        return r

    # ---- 읽기 · 제어 ------------------------------------------------
    def _read(self):
        """온도 · 전류 한 번. 열전대 끊김이면 T None. 장비 통신 오류는 예외."""
        T = self.k2110.read_temp()
        I, trip = (None, False)
        if self.mode == "rt" and self.out_on:
            I, trip = self.k2450.read()
        now = self.clock.now()
        R = inst.resistance(self.p["volt"], I) if self.mode == "rt" else None
        self._prev_valid = self._last_valid               # 이번 읽기 전의 마지막 유효 온도 시각(갱신 한도 검사용)
        if T is not None:
            self._last_valid = now
        return T, I, R, trip, now

    def _range_step(self, I, trip):
        if self.mode != "rt" or self.p["range_mode"] != "step" or not self.out_on:
            return
        if I is None:                                            # 범위 넘음 - 한도 전류를 덮는 범위까지 한 번에 올린다(전이 구간 점을 잃지 않게)
            need = float(self.p["ilim_a"])
            new = next((r for r in inst.RANGES if r >= need * 0.999 and r > self._range), inst.RANGES[-1])
        else:
            new = inst.next_range(self._range, I)
        new = inst.clamp_range(new, float(self.p["ilim_a"]))      # 고정 범위에서 한도 ≥ 범위의 10.6 %
        if abs(new - self._range) > 1e-15:
            self.k2450.set_range(new)
            self._range = new

    def _drive_heater(self, now, want, bypass=False):
        """HeatCtl 의 판단대로 히트건을 켜고 끈다(켜기/끄기는 power 계층을 거친다)."""
        app = self.app
        if bypass:
            self.heat.force_off(now)
            on, ev = False, None
        else:
            on, ev = self.heat.step(now, want)
        if ev == "max_on":
            self._warn("히트건 연속 켜짐 상한 %g s · 강제로 끔 · 최소 꺼짐 시간 뒤 이어 감" % self.heat.max_on)
        cur = bool(app.power.owners.get(app.power.heater_n))
        if on and not cur:
            self._check()                                        # 읽는 동안 중지 · 비상정지가 왔으면 켜지 않는다
            app.power.set(app.power.heater_n, True, HEAT)
        elif not on and (cur or ev or bypass or not app.power.heater_ok):
            app.power.heater_off()
        return on

    def _loop(self, phase, cyc, done_fn, target_fn, want_fn, record, limit_s):
        """읽기 → 비상 차단/값 없음 검사 → 히트건 판단 → 기록. done_fn(T, now) 이 참이면 끝."""
        p = self.p
        t_phase = self.clock.now()
        self._last_valid = self._prev_valid = None             # 단계 사이의 릴레이 쓰기 시간은 갱신 한도에 세지 않는다
        self.phase, self.cycle = phase, cyc
        while True:
            self._check()
            T, I, R, trip, now = self._read()
            self._progress = time.monotonic()
            if T is None:                                         # 열전대 끊김 · 범위 넘음: 히트건부터 끈다
                self._drive_heater(now, False, bypass=True)
                if self._last_valid is None or now - self._last_valid > float(p["stale_s"]):
                    raise RTFail("온도 값 없음 · 열전대 확인")
                self._log_point(now, None, None, None, I, trip, phase, cyc)
                continue
            if self._prev_valid is not None and now - self._prev_valid > float(p["stale_s"]) + 1e-9:
                self._drive_heater(now, False, bypass=True)
                raise RTFail("온도 갱신 없음 %g s" % float(p["stale_s"]))
            if T >= float(p["emergency_c"]):
                self._drive_heater(now, False, bypass=True)
                raise RTFail("비상 차단 온도 %.0f °C 이상 · 히트건 끔" % float(p["emergency_c"]))
            if now - t_phase > limit_s:
                self._drive_heater(now, False, bypass=True)
                raise RTFail("시간 한도 초과 · %s" % phase)
            self._range_step(I, trip)
            tgt = target_fn(T, now) if target_fn else None
            fin = done_fn(T, now)
            on = bool(self.heat.on) if fin else self._drive_heater(now, want_fn(T, tgt))
            if on and not fin:                                   # 켜 놓았는데 온도가 안 오르면(열전대가 판에서 떨어짐 · 핀 들림) 오류
                if self._on_t0 is None:
                    self._on_t0, self._on_T0 = now, T
                elif now - self._on_t0 >= NO_RISE_S and T - self._on_T0 < NO_RISE_C:
                    self._drive_heater(now, False, bypass=True)
                    raise RTFail("히트건을 켰는데 온도가 안 오름 · 열전대 · 핀 확인")
                elif T - self._on_T0 >= NO_RISE_C:
                    self._on_t0, self._on_T0 = now, T
            else:
                self._on_t0 = self._on_T0 = None
            self._last_valid = self.clock.now()                  # 히트건 · 릴레이 쓰기에 걸린 시간은 온도 갱신 한도에 세지 않는다(장비가 늦는 것만 센다)
            if self.mode == "rt" and self.out_on and I is not None:
                self._low = self._low + 1 if abs(I) < float(p["open_a"]) else 0
                if self._low >= OPEN_N and not self.open_warned:
                    self.open_warned = True
                    self._warn("연결 없음 의심 · 전류가 하한 아래 %d 회 연속 · 핀 접촉 · 앞/뒤 단자 설정 확인" % OPEN_N)
            if trip:
                self.trips += 1
                if self.trips == 1:
                    self._warn("전류 한도 걸림 · 저항이 낮아 전류가 한도에 닿음")
            self._log_point(now, T, tgt, R, I, trip, phase, cyc, on)
            if record and R is not None and not trip and now - self._last_rec >= float(p["min_interval_s"]):
                self._last_rec = now
                self._write_row(T, R)
            if fin:
                return T

    def _log_point(self, now, T, tgt, R, I, trip, phase, cyc, heater=None):
        if heater is None:
            heater = bool(self.app.power.owners.get(self.app.power.heater_n))
        el = now - (self._clk0 if getattr(self, "_clk0", None) is not None else now)
        self.last.update(T=T, R=R, I=I, target=tgt, trip=trip, range=self._range if self.mode == "rt" else None)
        pt = {"i": len(self.points), "t": round(el, 2), "T": None if T is None else round(T, 3), "tgt": None if tgt is None else round(tgt, 3),
              "R": R, "I": I, "ph": phase, "cy": cyc, "h": 1 if heater else 0, "trip": 1 if trip else 0}
        if len(self.points) < MAX_POINTS:
            self.points.append(pt)
        if self._ctl:
            try:
                self._ctl.write("%s,%.2f,%s,%s,%s,%s,%d,%s,%d,%d,%s,%d\n" % (
                    time.strftime("%H:%M:%S"), el, "" if T is None else "%.3f" % T, "" if tgt is None else "%.3f" % tgt,
                    "" if R is None else "%.7E" % R, "" if I is None else "%.7E" % I, 1 if trip else 0,
                    "" if self.mode != "rt" else "%g" % self._range, 1 if heater else 0, len(self.cooler_on), phase, 1 if self._low >= OPEN_N else 0))
                self._ctl.flush()
            except OSError:
                pass

    # ---- 단계 ---------------------------------------------------------
    def _prechill(self):
        p = self.p
        self._clk0 = self.clock.now()
        below_since = [None]

        def done(T, now):
            if T < float(p["precool_c"]):
                if below_since[0] is None:
                    below_since[0] = now
                return now - below_since[0] >= float(p["hold_s"])
            below_since[0] = None
            return False
        self._loop("예비 냉각", 0, done, None, lambda T, tg: False, False, float(p["phase_limit_s"]))

    def _heating(self, cyc):
        p = self.p
        st = {"t0": None, "T0": None}
        rate = float(p["heat_rate"] or 0)

        def target(T, now):
            if st["t0"] is None:
                st["t0"], st["T0"] = now, T
            return target_line(st["T0"], rate, now - st["t0"])

        def done(T, now):
            return T > float(p["max_temp"])
        want = (lambda T, tg: True) if not rate else (lambda T, tg: HeatCtl.want(T, tg, float(p["band_c"]), self.heat.on))
        self._loop("가열", cyc, done, target, want, True, float(p["phase_limit_s"]))
        self._drive_heater(self.clock.now(), False, bypass=True)          # 최고 온도 도달 - 바로 끈다

    def _cooling(self, cyc):
        p = self.p
        st = {"t0": None, "T0": None}
        rate = float(p["cool_rate"] or 0)

        def target(T, now):
            if st["t0"] is None:
                st["t0"], st["T0"] = now, T
            return target_line(st["T0"], -rate, now - st["t0"])

        def done(T, now):
            return T < float(p["break_temp"])
        want = (lambda T, tg: False) if not rate else (lambda T, tg: HeatCtl.want(T, tg, float(p["band_c"]), self.heat.on))
        self._loop("냉각", cyc, done, target, want, True, float(p["phase_limit_s"]))

    def _speed_test(self):
        """최대 속도 재기: 히트건만 n s(쿨러 끔) → °C/분, 쿨러만 n s(히트건 끔) → °C/분. 상한은 max temp · 시간 한도."""
        p, n = self.p, self.speed_s
        self._clk0 = self.clock.now()
        res = {}
        for name, heater, label in (("heat", True, "속도 측정 · 히트건"), ("cool", False, "속도 측정 · 쿨러")):
            for r in list(self.cooler_on):
                self.app.power.set(r, bool(not heater), "cooler")            # 히트건만 잴 때는 쿨러를 끈다
            st = {"t0": None}
            pts = []

            def done(T, now, st=st, pts=pts):
                if st["t0"] is None:
                    st["t0"] = now
                pts.append((now - st["t0"], T))
                return now - st["t0"] >= n or (heater and T >= float(p["max_temp"]))
            self._loop(label, 0, done, None, lambda T, tg, h=heater: h, False, min(float(p["phase_limit_s"]), n * 3 + 60.0))
            self._drive_heater(self.clock.now(), False, bypass=True)
            res[name] = _slope(pts) * 60.0
        self.speed = {"heat": round(res["heat"], 2), "cool": round(res["cool"], 2), "s": n}

    # ---- 파일 ---------------------------------------------------------
    def _open_cycle(self, cyc):
        d, v = self.run_dir, self.p["volt"]
        if cyc == 1:
            self._mrg_path = os.path.join(d, merged_name(self.name, v))
            self._mrg_f = open(self._mrg_path, "w", encoding="utf-8", newline="")     # merged 는 머리글 없음
        path = os.path.join(d, cycle_name(self.name, v, cyc))
        self._cyc_path = self._cyc_path_open = path
        self._cyc_f = open(path, "w", encoding="utf-8", newline="")
        self._cyc_f.write(HEADER + "\r\n")
        self.files.append(os.path.basename(path))

    def _write_row(self, T, R):
        line = data_line(T, R) + "\r\n"
        try:
            self._cyc_f.write(line)
            self._cyc_f.flush()
            self._mrg_f.write(line)
            self._mrg_f.flush()
        except (OSError, AttributeError) as e:
            raise RTFail("결과 파일 쓰기 실패 · " + logger.short(e))

    def _close_cycle(self, cyc):
        if self._cyc_f:
            self._cyc_f.close()
            self._cyc_f = None
            self._cyc_path_open = None
            if self.nas:
                self.nas.put(self._cyc_path)

    def _open_ctl(self):
        try:
            self._ctl = open(os.path.join(self.run_dir, "control.csv"), "w", encoding="utf-8-sig", newline="")
            self._ctl.write("시각,경과 s,온도,목표 온도,저항,전류,한도 걸림,범위,히트건,쿨러,단계,열린 회로\n")
        except OSError:
            self._ctl = None

    # ---- 끝 -----------------------------------------------------------
    def _finish(self, status, msg):
        """끝 처리 바깥 틀: 안쪽이 예외를 던져도 상태 · 끝 신호 · 앱 통지는 반드시 한다."""
        try:
            self._finish_inner(status, msg)
        except Exception as e:                                   # noqa: BLE001
            logger.log("R–T 끝 처리 예외 · " + logger.short(e), "err", raw=repr(e))
            try:
                self.app.power.heater_off()
            except Exception:                                    # noqa: BLE001
                pass
            if not self.status:
                self.status, self.message = ERROR, "끝 처리 오류 · " + logger.short(e, 30)
                self.state = "error"
        finally:
            self._guard_stop.set()
            if self.state in ("running", "stopping", "ready"):
                self.state = {OK: "done", STOPPED: "stopped"}.get(self.status, "error")
            self.finished.set()
            try:
                self.app.on_rt_end(self)
            except Exception:                                    # noqa: BLE001
                pass
            if self.nas is not None and self.mode == "rt":       # NAS 복사 대기는 끝 신호 뒤에(느린 네트워크가 화면 · 순회를 붙잡지 않게)
                try:
                    self.nas.join(30.0)
                    self._write_meta()
                except Exception:                                # noqa: BLE001
                    pass

    def _finish_inner(self, status, msg):
        """어떤 경우에도: 히트건 끔 + K2450 출력 끔 → 쿨러 끔(설정) → Z 맨 위(설정)."""
        app = self.app
        self.state = "stopping" if status != OK else "running"
        errs = []
        try:
            self.heat.force_off(self.clock.now())
            app.power.heater_off()
        except Exception as e:                                   # noqa: BLE001
            errs.append("히트건 끄기 실패 · " + logger.short(e))
        self.out_off_failed = False
        for k in range(2):
            try:
                if self.k2450 is not None and self.k2450.is_open:
                    self.k2450.output(False)
                break
            except Exception as e:                                   # noqa: BLE001
                if k == 1:
                    self.out_off_failed = True                   # 통신이 돌아오면 앱이 다시 끈다(App.on_rt_end)
                    errs.append("K2450 출력 끄기 실패 · 통신 복구 시 다시 끔")
        for f in (self._cyc_f, self._mrg_f):
            try:
                if f:
                    f.close()
            except OSError:
                pass
        if self._cyc_path_open and self.nas:
            self.nas.put(self._cyc_path_open)                   # 끝나지 못한 회차 파일도 NAS 에 남긴다
        self._cyc_path_open = None
        self._cyc_f = None
        bad = status != OK
        if (self.p["cooler_off_end"] or bad):
            for n in list(self.cooler_on):
                try:
                    app.power.set(n, False, "cooler")
                except Exception:                                # noqa: BLE001
                    pass
            self.cooler_on = []
        if self.z_top_end and app.motion.gen == self.gen and app.motion.z_ok and not app.motion.estop_locked:
            try:
                app.motion.move_z(0.0)
            except Exception as e:                               # noqa: BLE001
                errs.append("Z 맨 위 실패 · " + logger.short(e))
        if errs:
            msg = (msg + " · " if msg else "") + " · ".join(errs)
            if status == OK:
                status = ERROR
        self.status, self.message = status, msg
        self.t_end = time.time()
        if self._ctl:
            try:
                self._ctl.close()
            except OSError:
                pass
            self._ctl = None
        if self.nas and self.mode == "rt":
            if self._mrg_f is not None or getattr(self, "_mrg_path", None):
                self.nas.put(self._mrg_path)
            self.nas.finish()
        self.state = {OK: "done", STOPPED: "stopped"}.get(status, "error")
        self._write_meta()

    def retry_nas(self):
        """[NAS 로 다시 복사]: 못 쓴 파일 + 합친 파일 전부를 다시 시도(별도 스레드 · 호출자는 기다린다)."""
        if not getattr(self, "nas_dir", ""):
            raise RTFail("NAS 경로 없음")
        nas = NasCopier(self.nas_dir, self._warn)
        if self.nas:
            nas.names = dict(self.nas.names)
        nas.start()
        for f in self.files + [os.path.basename(getattr(self, "_mrg_path", ""))]:
            if f:
                nas.put(os.path.join(self.run_dir, f))
        nas.finish()
        nas.join(30.0)
        self.nas = nas
        self._write_meta()
        return not nas.pending

    def _write_meta(self):
        v = self.view()
        v["params"] = {k: self.p[k] for k in self.p}
        v["files"] = list(self.files) + ([os.path.basename(self._mrg_path)] if getattr(self, "_mrg_path", None) else [])
        try:
            with open(os.path.join(self.run_dir, "rt.json"), "w", encoding="utf-8") as f:
                json.dump(v, f, ensure_ascii=False, indent=1, default=str)
        except OSError:
            pass

    def view(self):
        el = (self.t_end or time.time()) - self.t0 if self.t0 else 0.0
        nas = None
        if self.nas is not None:
            nas = {"ok": self.nas.ok, "pending": [os.path.basename(x) for x in self.nas.pending], "dir": self.nas.nas_dir}
        return {"mode": self.mode, "state": self.state, "status": self.status, "message": self.message, "phase": self.phase, "cycle": self.cycle,
                "repeat": int(self.p["repeat"]), "name": self.name, "stamp": self.stamp, "dir": self.run_dir, "elapsed": round(el, 1),
                "T": self.last["T"], "R": self.last["R"], "I": self.last["I"], "target": self.last["target"], "trip": self.last["trip"],
                "range": self.last["range"], "heater": bool(self.app.power and self.app.power.owners.get(self.app.power.heater_n)),
                "cooler": list(self.cooler_on), "warns": list(self.warns), "n_points": len(self.points), "nas": nas, "speed": self.speed,
                "volt": self.p["volt"], "start": self.t0, "end": self.t_end, "heater_forced": self.heat.forced}


def _slope(pts):
    """(t, T) 점들의 최소제곱 기울기(°C/s)."""
    n = len(pts)
    if n < 2:
        return 0.0
    mt = sum(t for t, _ in pts) / n
    mT = sum(T for _, T in pts) / n
    den = sum((t - mt) ** 2 for t, _ in pts)
    return 0.0 if den <= 0 else sum((t - mt) * (T - mT) for t, T in pts) / den


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_ ." else "_" for c in str(name)).strip().replace(" ", "_") or "rt"


def list_results():
    """측정 폴더(data_v2\\measure\\*) 목록: rt.json 요약. 최근 것이 먼저."""
    root = os.path.join(paths.DATA_DIR, "measure")
    out = []
    try:
        names = sorted(os.listdir(root), reverse=True)
    except OSError:
        return out
    for n in names:
        fp = os.path.join(root, n, "rt.json")
        try:
            with open(fp, encoding="utf-8") as f:
                j = json.load(f)
        except (OSError, ValueError):
            continue
        out.append({"dir": n, "time": time.strftime("%m-%d %H:%M:%S", time.localtime(j.get("start") or 0)), "name": j.get("name"),
                    "cycle": "%s / %s" % (j.get("cycle"), j.get("repeat")), "status": j.get("status") or j.get("state"), "message": j.get("message"),
                    "mode": j.get("mode"), "files": j.get("files") or [], "nas": j.get("nas")})
    return out


def read_dat(path):
    """.dat → [(T, R)] (그래프 보기용)."""
    pts = []
    try:
        with open(path, encoding="utf-8") as f:
            for ln in f.read().splitlines():                  # 머리글 있는 회차 파일 · 머리글 없는 merged 파일 모두
                a = ln.split()
                if len(a) != 2:
                    continue
                try:
                    pts.append((float(a[0]), float(a[1])))
                except ValueError:
                    continue                                  # 머리글 줄
    except OSError:
        pass
    return pts


__all__ = ["RTRun", "HeatCtl", "params", "file_stem", "cycle_name", "merged_name", "target_line", "list_results", "read_dat", "unique_path", "fmt_e", "nas_target"]
