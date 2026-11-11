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

import contextlib
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
           "cooler_off_end", "z_top_end", "cool_stall_s", "cool_stall_c"]
Z_TOL_MM = 0.5                         # 핀이 접촉 높이라고 보는 허용 오차
MAX_POINTS = 200000
OPEN_N = 5                              # 전류가 하한 아래인 읽기가 이만큼 연속이면 경고(측정은 계속)
NO_RISE_S = 45.0                      # 히트건을 이만큼 연속으로 켰는데
NO_RISE_C = 0.5                       # 온도가 이만큼도 안 오르면 오류(열전대가 판에서 떨어졌거나 핀이 들림)
NO_RISE_MANUAL = (60.0, 1.0)          # 수동 측정: 히트건을 켠 뒤 60 s 안에 1 °C 도 안 오르면 중단
T_MISS_N = 3                          # 온도를 이만큼 연속으로 못 읽으면(열전대 끊김 · 범위 넘음) 중단
FAST_RISE_C_S = 5.0                   # 가열 중 이보다 빨리 오르면(°C/s) 한 번 경보: 열전대가 히트건 바람을 직접 맞음
FAST_RISE_TEXT = "온도가 너무 빨리 오름 · 열전대가 히트건 바람을 직접 맞는지 확인"
MODES = ("rt", "speed", "manual_rt", "manual_cont")     # manual_* = 수동 측정 탭(모터를 부르지 않는다)
MANUAL_CTL_HEAD = "시각,경과 s,회차,단계,온도,전류,저항,한도 걸림,히트건,쿨러,전압\n"


class RTStop(Exception):
    pass


class RTFail(Exception):
    pass


def params(cfg):
    """설정(rt.*)을 평평한 dict 로."""
    p = {k: cfg.get("rt." + k) for k in RT_KEYS}
    if p["emergency_c"] is None:                         # 예전 설정(비움 = 최고 온도 + 10) - 지금 기본은 110 °C 고정
        p["emergency_c"] = float(p["max_temp"]) + 10.0
    if p.get("cool_stall_s") is None:
        p["cool_stall_s"] = 180.0
    if p.get("cool_stall_c") is None:
        p["cool_stall_c"] = 0.3
    return p


def params_block(p):
    """설정 조합이 시작할 수 없으면 사유 한 줄."""
    if float(p["max_temp"]) >= float(p["emergency_c"]):
        return "최고 온도 %g °C ≥ 비상 차단 %g °C · 설정 확인" % (float(p["max_temp"]), float(p["emergency_c"]))
    return ""


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
    # Windows 형식 기본 폴더(\\NAS\... · D:\...)는 Windows 규칙으로, 그 밖(시험의 임시 폴더 등)은 운영체제 규칙으로 잇는다.
    # Windows 에서는 둘 다 ntpath.join 이라 결과가 같다 - 다른 운영체제에서 '\' 가 이름에 붙어 폴더가 하나로 뭉치지 않게.
    win = "\\" in base or (len(base) >= 2 and base[1] == ":" and base[0].isalpha())
    return (ntpath.join if win else os.path.join)(base, *parts)


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
        self.manual = mode.startswith("manual")             # 수동 측정: 이동 계층을 부르지 않는다(Z 올림 없음 · 세대는 있으면 본다)
        self.measures_r = mode in ("rt", "manual_rt", "manual_cont")
        self.uses_relays = mode in ("rt", "speed", "manual_rt")
        self.clock = app.hub.clock
        self.cancel_fn = cancel_fn or (lambda: False)
        self.cancel = threading.Event()
        self.name = name_stem                       # File name 으로 쓰는 문자열(샘플 순회: File name + 번호 + "_")
        self.stamp = time.strftime("%Y%m%d_%H%M%S")
        self.run_dir = run_dir or os.path.join(paths.DATA_DIR, "manual" if self.manual else "measure", "%s_%s" % (self.stamp, _safe(name_stem)))
        self.relays = list(relays) if relays is not None else None
        self.z_top_end = False if self.manual else (self.p["z_top_end"] if z_top_end is None else bool(z_top_end))
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
        self._t_miss = 0                                # 온도를 연속으로 못 읽은 횟수
        self._fast_warned = False
        self._prevT = None                              # (시각, 온도) - 빠른 상승 판정
        self.overshoot = None                           # 히트건 끈 뒤 최고 온도 - 최고 온도 설정(회차마다 가장 큰 것)
        self.peak_after_off = None
        self.stalls = []                                # 냉각 멈춤으로 끝난 단계("예비 냉각" · "냉각 n")
        self.relay_off_failed = False
        self.gen = None

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
        m = self.app.motion
        if self.cancel.is_set() or self.cancel_fn() or (m is not None and self.gen is not None and m.gen != self.gen):
            raise RTStop()
        if self.uses_relays and self.app.link_lost_count != self._link0:
            raise RTFail("통신 끊김 · 릴레이 꺼짐")

    def _relay_check(self):
        """B4: 펌웨어 RS 를 읽어 '있어야 할 상태'(전원 계층이 켠 것 = 쿨러 · 단계대로의 히트건, 나머지 꺼짐)와 비교한다. 다르면 중단.
        통신 감시가 릴레이를 껐거나(통신 끊김) 보드가 재부팅했거나 다른 곳에서 바뀐 것 - 측정을 이어 가지 않는다."""
        app = self.app
        st, pw = getattr(app, "stage", None), app.power
        if not self.uses_relays or st is None or pw is None:
            return
        try:
            bits = st.relay_status()
        except StageError as e:
            raise RTFail("통신 끊김 · 릴레이 상태 못 읽음 · " + logger.short(e, 30))
        want = [(i + 1) in pw.owners for i in range(len(bits))]
        if bits != want:
            diff = ["R%d %s" % (i + 1, "켜짐" if b else "꺼짐") for i, (b, w) in enumerate(zip(bits, want)) if b != w]
            raise RTFail("릴레이 상태 다름 · " + " · ".join(diff[:3]) + " · 통신 끊김 확인")

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
            if z is None or (abs(float(z) - float(cz)) > Z_TOL_MM and not RTRun._contact_here(app, z)):
                return "핀이 접촉 높이가 아님 · 접촉 높이로 내리기"
        return ""

    @staticmethod
    def _contact_here(app, z):
        """전기로 접촉을 확인한 자리에 그대로 있는가(최종 Z 가 같음 - 저장 높이와 0.5 mm 넘게 달라도 실제로 닿은 자리)."""
        last = getattr(app, "contact_last", None) or {}
        fz = last.get("final_z")
        return bool(last.get("sense")) and fz is not None and abs(float(z) - float(fz)) <= 0.01

    # ---- 실행 ---------------------------------------------------------
    def run(self):
        app = self.app
        self._link0 = app.link_lost_count
        self.state = "running"
        self.t0 = time.time()
        m = app.motion
        gen = (m.gen if m is not None else None) if self.gen_arg is None else self.gen_arg
        self.gen = gen
        status, msg = OK, ""
        self._progress = time.monotonic()
        self._guard_stop = threading.Event()
        threading.Thread(target=self._guard, name="rt-guard", daemon=True).start()
        try:
            os.makedirs(self.run_dir, exist_ok=True)
            self.nas_dir = nas_target(self.p.get("file_path"), self.p.get("date_folder"), self.p.get("batch_folder"), self.t0) if self.mode in ("rt", "manual_rt") else ""
            self.nas = NasCopier(self.nas_dir, self._warn) if self.nas_dir else None
            if self.nas:
                self.nas.start()
            self._open_ctl()
            self._write_meta()
            why = params_block(self.p)
            if why:
                raise RTFail(why)
            # 세대를 잡는다(시리얼 쓰기 직전마다 비교 - 비상정지 뒤 히트건 켜기가 나가지 않게). 수동 측정에서 스테이지가 없으면 릴레이도 없다.
            with (m.op(gen) if m is not None else contextlib.nullcontext()):
                self._setup()
                if self.mode == "speed":
                    self._speed_test()
                elif self.mode == "manual_cont":
                    self.k2450.output(True)
                    self.out_on = True
                    self._continuous()
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
            if status == ERROR and app.motion is not None and self.gen is not None and app.motion.gen != self.gen:   # 비상정지로 쓰기가 취소된 것은 오류가 아니라 중단이다
                status, msg = STOPPED, app.cancel_text(self.gen)
            self._finish(status, msg)

    def _guard(self):
        """독립 감시(데드맨): 측정 루프가 한 바퀴도 못 돌고 오래 멈추면(VISA 가 막힘 · 스레드 예외) 히트건을 끈다. 실제 시간 기준."""
        scale = max(0.01, float(getattr(self.clock, "scale", 1.0)))
        limit = float(self.p["stale_s"]) / scale + 2.0
        while not self._guard_stop.wait(0.5):
            pw = self.app.power
            if pw is not None and pw.owners.get(pw.heater_n) and time.monotonic() - self._progress > limit:
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
        if self.measures_r:
            ok, why = self.k2450.check_scpi()
            if not ok:
                raise RTFail(why)
            self.k2450.configure({"terminal": p["terminal"], "four_wire": p["four_wire"], "volt": p["volt"], "ilim_a": p["ilim_a"],
                                  "range_mode": p["range_mode"], "start_range": p["start_range"], "nplc": p["nplc"], "filter_n": p["filter_n"]})
        if not self.uses_relays:                        # 연속 측정: 릴레이를 쓰지 않는다
            self._check()
            return
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
        if self.measures_r and self.out_on:
            I, trip = self.k2450.read()
        now = self.clock.now()
        R = inst.resistance(self.p["volt"], I) if self.measures_r else None
        self._prev_valid = self._last_valid               # 이번 읽기 전의 마지막 유효 온도 시각(갱신 한도 검사용)
        if T is not None:
            self._last_valid = now
        return T, I, R, trip, now

    def _range_step(self, I, trip):
        if not self.measures_r or self.p["range_mode"] != "step" or not self.out_on:
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
        if not self.uses_relays or app.power is None:            # 연속 측정(릴레이 안 씀): 히트건을 켜지 않는다
            self.heat.force_off(now)
            return False
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
        self._prevT = None
        no_rise_s, no_rise_c = NO_RISE_MANUAL if self.manual else (NO_RISE_S, NO_RISE_C)
        while True:
            self._check()
            self._relay_check()
            T, I, R, trip, now = self._read()
            self._progress = time.monotonic()
            if T is None:                                         # 열전대 끊김 · 범위 넘음: 히트건부터 끈다
                self._drive_heater(now, False, bypass=True)
                self._t_miss += 1
                if self._t_miss >= T_MISS_N or self._last_valid is None or now - self._last_valid > float(p["stale_s"]):
                    raise RTFail("온도 값 없음 · 열전대 확인")
                self._log_point(now, None, None, None, I, trip, phase, cyc)
                continue
            self._t_miss = 0
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
            self._rise_check(phase, T, now)
            if self.peak_after_off is not None and T > self.peak_after_off:
                self.peak_after_off = T
            tgt = target_fn(T, now) if target_fn else None
            fin = done_fn(T, now)
            on = bool(self.heat.on) if fin else self._drive_heater(now, want_fn(T, tgt))
            if on and not fin:                                   # 켜 놓았는데 온도가 안 오르면(열전대가 판에서 떨어짐 · 핀 들림) 오류
                if self._on_t0 is None:
                    self._on_t0, self._on_T0 = now, T
                elif now - self._on_t0 >= no_rise_s and T - self._on_T0 < no_rise_c:
                    self._drive_heater(now, False, bypass=True)
                    raise RTFail("히트건을 켰는데 %g s 동안 %g °C 도 안 오름 · 열전대 · 히트건 확인" % (no_rise_s, no_rise_c))
                elif T - self._on_T0 >= no_rise_c:
                    self._on_t0, self._on_T0 = now, T
            else:
                self._on_t0 = self._on_T0 = None
            self._last_valid = self.clock.now()                  # 히트건 · 릴레이 쓰기에 걸린 시간은 온도 갱신 한도에 세지 않는다(장비가 늦는 것만 센다)
            if self.measures_r and self.out_on and I is not None:
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
            pw = self.app.power
            heater = bool(pw is not None and pw.owners.get(pw.heater_n))
        el = now - (self._clk0 if getattr(self, "_clk0", None) is not None else now)
        self.last.update(T=T, R=R, I=I, target=tgt, trip=trip, range=self._range if self.measures_r else None)
        pt = {"i": len(self.points), "t": round(el, 2), "T": None if T is None else round(T, 3), "tgt": None if tgt is None else round(tgt, 3),
              "R": R, "I": I, "ph": phase, "cy": cyc, "h": 1 if heater else 0, "trip": 1 if trip else 0}
        if len(self.points) < MAX_POINTS:
            self.points.append(pt)
        if self._ctl and self.manual:
            try:
                self._ctl.write("%s,%.2f,%d,%s,%s,%s,%s,%d,%d,%s,%g\n" % (
                    time.strftime("%Y-%m-%d %H:%M:%S"), el, cyc, phase, "" if T is None else "%.3f" % T, "" if I is None else "%.7E" % I,
                    "" if R is None else "%.7E" % R, 1 if trip else 0, 1 if heater else 0, " ".join("R%d" % n for n in self.cooler_on), float(self.p["volt"])))
                self._ctl.flush()
            except OSError:
                pass
        elif self._ctl:
            try:
                self._ctl.write("%s,%.2f,%s,%s,%s,%s,%d,%s,%d,%d,%s,%d\n" % (
                    time.strftime("%H:%M:%S"), el, "" if T is None else "%.3f" % T, "" if tgt is None else "%.3f" % tgt,
                    "" if R is None else "%.7E" % R, "" if I is None else "%.7E" % I, 1 if trip else 0,
                    "" if not self.measures_r else "%g" % self._range, 1 if heater else 0, len(self.cooler_on), phase, 1 if self._low >= OPEN_N else 0))
                self._ctl.flush()
            except OSError:
                pass

    # ---- 단계 ---------------------------------------------------------
    def _stall(self, label):
        """냉각 멈춤 판정기: cool_stall_s 동안 cool_stall_c 만큼도 안 내려가면 참(방 온도에 닿음 - 목표 온도에 못 닿는다)."""
        st = {"best": None, "t": None}
        span, step = float(self.p["cool_stall_s"]), float(self.p["cool_stall_c"])

        def stalled(T, now):
            if st["best"] is None or T < st["best"] - step:
                st["best"], st["t"] = T, now
                return False
            if now - st["t"] >= span:
                if label not in self.stalls:
                    self.stalls.append(label)
                    self._warn("냉각 멈춤 · 방 온도에 닿음 · %s 끝 (%.1f °C)" % (label, T))
                return True
            return False
        return stalled

    def _prechill(self):
        p = self.p
        self._clk0 = self.clock.now()
        if float(p["precool_c"]) == 0.0:                       # 0 = 예비 냉각 건너뜀(쿨러는 켜 둔 채 바로 가열)
            self.phase = "예비 냉각 건너뜀"
            return
        below_since = [None]
        stalled = self._stall("예비 냉각")

        def done(T, now):
            if T < float(p["precool_c"]):
                if below_since[0] is None:
                    below_since[0] = now
                return now - below_since[0] >= float(p["hold_s"])
            below_since[0] = None
            return stalled(T, now)
        self._loop("예비 냉각", 0, done, None, lambda T, tg: False, False, float(p["phase_limit_s"]))

    def _rise_check(self, phase, T, now):
        """A6: 가열 중 FAST_RISE_C_S °C/s 보다 빨리 오르면 한 번 경보(측정은 계속)."""
        pv, self._prevT = self._prevT, (now, T)
        if phase != "가열" or self._fast_warned or pv is None:
            return
        dt = now - pv[0]
        if dt > 0.05 and (T - pv[1]) / dt > FAST_RISE_C_S:
            self._fast_warned = True
            self._warn("%s (%.1f °C/s)" % (FAST_RISE_TEXT, (T - pv[1]) / dt))
            try:
                self.app.set_alarm(FAST_RISE_TEXT, "측정은 계속 · 배치를 고친 뒤 다음 측정")
            except Exception:                                    # noqa: BLE001
                pass

    def _continuous(self):
        """수동 연속 측정: 가열 없이 온도 · 저항을 정지할 때까지 읽는다(control.csv 만)."""
        self._clk0 = self.clock.now()
        self._loop("연속 측정", 0, lambda T, now: False, None, lambda T, tg: False, False, 7 * 24 * 3600.0)

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
        self.peak_after_off = float(self.last["T"] if self.last["T"] is not None else p["max_temp"])     # 끈 뒤 오버슈트를 잰다(냉각 단계에서)

    def _cooling(self, cyc):
        p = self.p
        st = {"t0": None, "T0": None}
        rate = float(p["cool_rate"] or 0)

        def target(T, now):
            if st["t0"] is None:
                st["t0"], st["T0"] = now, T
            return target_line(st["T0"], -rate, now - st["t0"])

        stalled = self._stall("냉각 %d" % cyc)

        def done(T, now):
            return T < float(p["break_temp"]) or stalled(T, now)
        want = (lambda T, tg: False) if not rate else (lambda T, tg: HeatCtl.want(T, tg, float(p["band_c"]), self.heat.on))
        try:
            self._loop("냉각", cyc, done, target, want, True, float(p["phase_limit_s"]))
        finally:
            self._note_overshoot(cyc)

    def _note_overshoot(self, cyc):
        """히트건을 끈 뒤 오른 최고 온도를 기록(로그 · rt.json)."""
        pk, self.peak_after_off = self.peak_after_off, None
        if pk is None:
            return
        ov = pk - float(self.p["max_temp"])
        if self.overshoot is None or ov > self.overshoot:
            self.overshoot = ov
        logger.log("R–T %d회 · 히트건 끈 뒤 최고 %.1f °C (최고 온도 설정보다 %+.1f °C)" % (cyc, pk, ov))

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
            self._ctl.write(MANUAL_CTL_HEAD if self.manual else "시각,경과 s,온도,목표 온도,저항,전류,한도 걸림,범위,히트건,쿨러,단계,열린 회로\n")
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
                if self.app.power is not None:
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
            if self.nas is not None and self.mode in ("rt", "manual_rt"):       # NAS 복사 대기는 끝 신호 뒤에(느린 네트워크가 화면 · 순회를 붙잡지 않게)
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
            if app.power is not None:
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
        if (self.p["cooler_off_end"] or bad or self.manual) and self.uses_relays:
            failed = False
            for n in list(self.cooler_on):
                try:
                    app.power.set(n, False, "cooler")
                except Exception:                                # noqa: BLE001
                    failed = True
            st = getattr(app, "stage", None)
            if (bad or self.manual) and st is not None:          # 수동 측정 · 오류 · 중지 · 비상정지: 전부 끄기(r all 0)로 꺼짐을 확인한다
                try:
                    st.relay_all_off()
                    app.power.owners.clear()
                except Exception as e:                           # noqa: BLE001
                    failed = True
                    logger.log("릴레이 전부 끄기 실패 · " + logger.short(e), "err")
            if failed:
                self.relay_off_failed = True                     # 통신이 돌아오면 앱이 다시 끈다(App.on_rt_end)
                errs.append("쿨러 끄기 미확인 · 통신 복구 시 다시 끔")
            self.cooler_on = []
        m = app.motion
        if self.z_top_end and m is not None and m.gen == self.gen and m.z_ok and not m.estop_locked:
            try:
                app.motion.move_z(0.0)
            except MotionError as e:
                if e.code == "stop":                             # 단독 측정 끝의 Z 올림이 정지로 끊김: 결과는 정상, 메시지만
                    msg = (msg + " · " if msg else "") + "Z 올림 · 정지로 중단"
                else:
                    errs.append("Z 맨 위 실패 · " + logger.short(e))
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
        if self.nas and self.mode in ("rt", "manual_rt"):
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
        if getattr(self, "contact", None):
            v["contact"] = self.contact                              # 이 자리의 접촉 찾기 결과(첫 접촉 Z · 최종 Z · 2 · 4단자 저항 · 잰 점)
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
                "volt": self.p["volt"], "start": self.t0, "end": self.t_end, "heater_forced": self.heat.forced,
                "kind": "수동" if self.manual else "자동", "overshoot": None if self.overshoot is None else round(self.overshoot, 1),
                "stalls": list(self.stalls), "fast_rise": self._fast_warned}


def relays_off_retry(app, secs=120.0, stop_fn=None, every=1.0):
    """정리 경로에서 릴레이 끄기를 확인하지 못했으면(통신 끊김) 통신이 돌아올 때까지 every 초마다 히트건 끄기 + r all 0 을 다시 보낸다.
    꺼짐을 RS 로 확인하면 True."""
    end = time.monotonic() + float(secs)
    while time.monotonic() < end and not (stop_fn and stop_fn()):
        st, pw = getattr(app, "stage", None), app.power
        if st is None or pw is None:
            return False
        try:
            pw.heater_off()
            st.relay_all_off()
            pw.owners.clear()
            logger.log("릴레이 전부 꺼짐 확인 · 통신 복구", "warn")
            return True
        except Exception:                                    # noqa: BLE001
            time.sleep(every)
    return False


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


ROOTS = ("measure", "manual")           # 자동(측정 탭 · 순회) · 수동 측정 탭


def result_dir(name, root="measure"):
    """결과 폴더 경로(이름은 폴더 이름만 - 경로 조각을 받지 않는다)."""
    return os.path.join(paths.DATA_DIR, root if root in ROOTS else "measure", os.path.basename(str(name)))


def list_results():
    """측정 폴더(data_v2\\measure\\* · data_v2\\manual\\*) 목록: rt.json 요약. 최근 것이 먼저."""
    out = []
    for root_name in ROOTS:
        root = os.path.join(paths.DATA_DIR, root_name)
        try:
            names = os.listdir(root)
        except OSError:
            continue
        for n in names:
            fp = os.path.join(root, n, "rt.json")
            try:
                with open(fp, encoding="utf-8") as f:
                    j = json.load(f)
            except (OSError, ValueError):
                continue
            out.append({"dir": n, "root": root_name, "time": time.strftime("%m-%d %H:%M:%S", time.localtime(j.get("start") or 0)), "name": j.get("name"),
                        "cycle": "%s / %s" % (j.get("cycle"), j.get("repeat")), "status": j.get("status") or j.get("state"), "message": j.get("message"),
                        "mode": j.get("mode"), "kind": "수동" if root_name == "manual" else "자동", "files": j.get("files") or [], "nas": j.get("nas"),
                        "start": j.get("start") or 0})
    out.sort(key=lambda x: (x["start"], x["dir"]), reverse=True)
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


__all__ = ["RTRun", "HeatCtl", "params", "params_block", "relays_off_retry", "result_dir", "file_stem", "cycle_name", "merged_name", "target_line", "list_results", "read_dat", "unique_path", "fmt_e", "nas_target"]
