"""temptest.py - 온도 시험(수동 제어 탭 [온도 시험]): 샘플 · 저항계 · 모터 없이 히트건과 쿨러의 온도 거동만 잰다.

목적: "몇 °C 까지 몇 초에 도달하는지" · 최대 가열 속도 · 넘침(열선을 끈 뒤 더 오르는 정도) · 냉각 시간을 보고 히트건 · 가열 제어 값을 정한다.
조건은 자동 측정과 같은 설정(run_start_c · run_heater_off_c · run_cool_end_c · run_heat_mode · run_heat_rate_c_min · heater_band_c ·
heater_min_on_s · heater_min_off_s · run_heat_hold_s · run_fan_during_cool · run_stall_s · run_stall_c · run_phase_limit_s)에
온도 시험 설정(temp_test_coolers · temp_test_interval_s · temp_test_mark_c · temp_test_precool)을 더해 쓴다 - 여기서 맞춘 값이 측정에 그대로 쓰인다.

순서
  1. 예비 냉각 : 쿨러(temp_test_coolers)를 켠다. temp_test_precool 이면 시작 온도 이하까지(정체 - run_stall_s 동안 run_stall_c 도 안 내려감 - 면
                그 온도에서 가열 시작 + 주의 한 줄). 꺼져 있으면 쿨러만 켜고 바로 가열. 쿨러는 시험 끝까지 켜 둔다.
  2. 가열     : 히트건 켜기(Controller 의 히트건 함수 - 열선 → 송풍, 측정 주체 규칙). ramp 이면 자동 측정과 같은 판정 함수
                (heater.ramp_setpoint · element_decision)로 목표선을 따르고 가열 끝 온도에 닿으면 유지 시간 동안 유지. full 이면 닿을 때까지 열선 켬.
  3. 냉각     : 열선 끔(송풍은 run_fan_during_cool 에 따름) → 냉각 끝 온도 이하 또는 정체까지 기록.
  4. 끝       : 히트건 끄는 순서(열선 끔 → 송풍은 heater_fan_after_s 뒤) · 쿨러 끔 → 요약 · 파일.
[정지] · 비상정지 · 안전 규칙의 차단(차단 온도 · 열선 연속 최대 시간 · 온도 못 읽음) · 한 단계 시간 한도 · 통신 끊김 · 온도계 오류에서도
같은 정리를 하고 그때까지의 기록 · 요약을 저장한다(결과 "정지" · "비상정지" · "중단 · 이유").
저장: <데이터 폴더>/temp_test/YYYYMMDD_HHMMSS/ temp.csv(UTF-8 BOM) · summary.txt · graph.png. 점 데이터는 state 에 넣지 않는다(points()).
"""

import math
import os
import threading
import time

import cv2
import numpy as np

from app import config as config_mod
from app import imgio
from app.devices import meters
from app.jobs.heater import RelayHeater, element_decision, ramp_setpoint
from app.safety import Rejected
from app.state import ALARM, NOTICE

PRECOOL, HEAT, HOLD, COOL = 0, 1, 2, 3
PHASES = ("예비 냉각", "가열", "유지", "냉각")
DONE, STOPPED, ESTOPPED, ABORTED = "완료", "정지", "비상정지", "중단"
CSV_HEADER = ("시각", "경과(s)", "단계", "온도(°C)", "목표(°C)", "열선", "송풍", "쿨러")
COND_KEYS = ("run_start_c", "run_heater_off_c", "run_cool_end_c", "run_heat_mode", "run_heat_rate_c_min", "heater_band_c", "heater_min_on_s",
             "heater_min_off_s", "run_heat_hold_s", "run_fan_during_cool", "run_stall_s", "run_stall_c", "run_phase_limit_s",
             "temp_test_coolers", "temp_test_interval_s", "temp_test_mark_c", "temp_test_precool",
             "heater_max_c", "heater_max_on_s", "temp_stale_s", "heater_start_gap_s", "heater_fan_after_s", "heater_fan_relay", "heater_element_relay")
T_MISS_N = 3                                      # 온도를 이만큼 연속으로 못 읽으면 중단(열전대 끊김) - 자동 측정과 같다
MAX_POINTS = 200000


class _End(Exception):
    """시험을 끝낸다: kind = stop([정지]) · estop(비상정지) · abort(안전 규칙 · 장비 오류 - text 가 이유)."""

    def __init__(self, kind, text=""):
        super().__init__(text or kind)
        self.kind, self.text = kind, text


def mode_text(cd):
    if cd.get("run_heat_mode") == "ramp":
        return "가열 속도 제어 %g °C/분" % float(cd["run_heat_rate_c_min"])
    return "계속 가열"


# ----------------------------------------------------------------------
# 요약(순수 함수 - 진행 중에도 같은 함수로 채운다)
# ----------------------------------------------------------------------
def _cross(seq, m):
    """(t, T) 열에서 온도가 처음 m 이상이 되는 시각(앞 점과 직선 보간). 못 닿으면 None."""
    for (ta, Ta), (tb, Tb) in zip(seq, seq[1:]):
        if Tb >= m:
            if Ta < m < Tb:
                return ta + (tb - ta) * (m - Ta) / (Tb - Ta)
            return tb
    return None


def marks(T0, top, step):
    """도달 표의 온도들: 가열 시작 온도보다 큰 step 의 배수(가열 끝 온도 미만) + 가열 끝 온도."""
    out = []
    m = (math.floor(T0 / step + 1e-9) + 1) * step
    while m < top - 1e-9:
        out.append(round(m, 6))
        m += step
    out.append(float(top))
    return out


def summarize(pts, elog, ev, cd):
    """점 [경과 s, 온도, 목표 | None, 열선 0|1, 송풍 0|1, 단계] · 열선 기록 [경과 s, 0|1] · 사건 ev → 결과.
    시간은 모두 가열 시작(히트건을 켠 때)부터 잰다(예비 냉각은 시험 시작부터)."""
    off_c, end_c, step = float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"]), float(cd["temp_test_mark_c"])
    out = {"pre": None, "T0": None, "marks": [], "heat": None, "cool": None}
    if ev.get("pre") is not None:
        out["pre"] = dict(ev["pre"])
    t_h, T0 = ev.get("t_heat"), ev.get("T0")
    if t_h is None or T0 is None:
        return out
    out["T0"] = T0
    t_end = ev.get("t_heat_end")
    hp = [p for p in pts if p[5] in (HEAT, HOLD)]
    seq = [(t_h, T0)] + [(p[0], p[1]) for p in hp]
    for m in marks(T0, off_c, step):
        t = _cross(seq, m)
        out["marks"].append({"c": m, "s": None if t is None else round(t - t_h, 2), "top": m == off_c})
    reach = out["marks"][-1]["s"]
    last_t = t_end if t_end is not None else (hp[-1][0] if hp else t_h)
    on_n, on_s, cur = 0, 0.0, None                                  # 열선 켠 횟수 · 켜진 시간 합(가열 시작 ~ 가열 끝)
    for t, on in elog:
        if on and cur is None:
            on_n += 1
            cur = t
        elif not on and cur is not None:
            on_s += max(0.0, min(t, last_t) - cur)
            cur = None
    if cur is not None:
        on_s += max(0.0, last_t - cur)
    dur = max(0.0, last_t - t_h)
    heat = {"reach_s": reach, "rate_c_min": None if not reach else round((off_c - T0) / reach * 60.0, 2), "on_n": on_n, "on_s": round(on_s, 1),
            "on_ratio": None if dur <= 0 else round(on_s / dur, 3), "dur_s": round(dur, 1), "peak_c": None, "over_c": None, "rms_c": None, "max_c": None}
    if t_end is not None:
        after = [p[1] for p in pts if p[0] >= t_end]
        if after:
            heat["peak_c"] = round(max(after), 2)
            heat["over_c"] = round(max(after) - off_c, 2)
    if cd.get("run_heat_mode") == "ramp":
        err = [p[1] - p[2] for p in hp if p[2] is not None]
        if err:
            heat["rms_c"] = round(math.sqrt(sum(e * e for e in err) / len(err)), 2)
            heat["max_c"] = round(max(err, key=abs), 2)
    out["heat"] = heat
    cp = [p for p in pts if p[5] == COOL]
    if t_end is not None and cp:
        T_from = cp[0][1]
        hit = next((p for p in cp if p[1] <= end_c), None)
        last = hit or cp[-1]
        s = last[0] - t_end
        out["cool"] = {"from_c": round(T_from, 2), "to_c": round(last[1], 2), "s": round(s, 1), "reached": hit is not None,
                       "rate_c_min": None if s <= 0 else round((T_from - last[1]) / s * 60.0, 2)}
    return out


def _f(v, fmt="%.1f", none="—"):
    return none if v is None else fmt % v


def summary_lines(cd, res, result, started, notes):
    """summary.txt 의 줄(조건 + 결과)."""
    L = ["온도 시험 · %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)), "결과 · %s" % result, ""]
    L.append("[조건]")
    L.append("가열 방식 · %s" % mode_text(cd))
    L.append("시작 온도 %g °C · 가열 끝 온도 %g °C · 냉각 끝 온도 %g °C" % (cd["run_start_c"], cd["run_heater_off_c"], cd["run_cool_end_c"]))
    L.append("목표 아래 허용 폭 %g °C · 열선 최소 켜짐 %g s · 최소 꺼짐 %g s · 유지 %g s" % (cd["heater_band_c"], cd["heater_min_on_s"], cd["heater_min_off_s"], cd["run_heat_hold_s"]))
    L.append("냉각 중 송풍 %s · 예비 냉각 %s · 쿨러 %s" % ("켬" if cd["run_fan_during_cool"] else "끔", "켬" if cd["temp_test_precool"] else "끔",
                                                  " · ".join("R%d" % n for n in cd["temp_test_coolers"]) or "없음"))
    L.append("냉각 정체 %g s · %g °C · 한 단계 한도 %g s · 읽기 간격 %g s" % (cd["run_stall_s"], cd["run_stall_c"], cd["run_phase_limit_s"], cd["temp_test_interval_s"]))
    L.append("차단 온도 %g °C · 열선 연속 최대 %g s · 열선 후 송풍 %g s · 열선 끈 뒤 송풍 %g s" % (cd["heater_max_c"], cd["heater_max_on_s"], cd["heater_start_gap_s"], cd["heater_fan_after_s"]))
    L += ["", "[예비 냉각]"]
    pre = res.get("pre")
    if pre is None:
        L.append("안 함")
    else:
        L.append("%s · %s s%s" % ("%.1f °C 정체" % pre["T"] if pre["stalled"] else "%.1f °C 도달" % pre["T"], _f(pre["s"]), ""))
    L += ["", "[도달 표] 가열 시작 %s °C 부터" % _f(res.get("T0"))]
    for m in res.get("marks", []):
        L.append("%g °C · %s s%s" % (m["c"], _f(m["s"]), " (목표)" if m["top"] else ""))
    L += ["", "[가열]"]
    h = res.get("heat")
    if h:
        L.append("목표 도달 %s s · 평균 %s °C/분" % (_f(h["reach_s"]), _f(h["rate_c_min"], "%.2f")))
        L.append("열선 켠 횟수 %d · 켜진 시간 %s s (%s %%)" % (h["on_n"], _f(h["on_s"]), _f(None if h["on_ratio"] is None else h["on_ratio"] * 100, "%.0f")))
        L.append("열선 끈 뒤 최고 %s °C · 넘침 %s °C" % (_f(h["peak_c"], "%.2f"), _f(h["over_c"], "%+.2f")))
        if h["rms_c"] is not None:
            L.append("목표선 차이 평균 제곱근 %.2f °C · 최대 %+.2f °C" % (h["rms_c"], h["max_c"]))
    else:
        L.append("없음")
    L += ["", "[냉각]"]
    cl = res.get("cool")
    if cl:
        L.append("%s °C → %s °C · %s s · 평균 %s °C/분%s" % (_f(cl["from_c"]), _f(cl["to_c"]), _f(cl["s"]), _f(cl["rate_c_min"], "%.2f"),
                                                       "" if cl["reached"] else " · 냉각 끝 온도 미도달"))
    else:
        L.append("없음")
    if notes:
        L += ["", "[메모]"] + list(notes)
    return L


# ----------------------------------------------------------------------
# 그림(graph.png) - 서버가 그린다(창을 닫아도 남는다). OpenCV 글꼴은 영문만 쓴다
# ----------------------------------------------------------------------
INK, BLUE, PINK, GRID, LEAD = (41, 35, 31), (214, 120, 42), (125, 58, 192), (228, 225, 222), (160, 160, 160)       # BGR


def draw_png(pts, elog, ev, cd, title):
    W, H = 1200, 720
    L, R, T, B = 80, 190, 56, 120                                   # 오른쪽 띠 = 라벨(지시선으로 잇는다) · 아래 띠 = 열선 켜진 구간
    img = np.full((H, W, 3), 255, np.uint8)
    font = cv2.FONT_HERSHEY_SIMPLEX
    cv2.putText(img, title, (L, 32), font, 0.6, INK, 1, cv2.LINE_AA)
    x0, x1, y0, y1 = L, W - R, T, H - B
    t_max = max([p[0] for p in pts] + [1.0])
    temps = [p[1] for p in pts] + [p[2] for p in pts if p[2] is not None] + [float(cd["run_heater_off_c"]), float(cd["run_start_c"])]
    lo, hi = math.floor(min(temps) / 5.0) * 5.0, math.ceil(max(temps) / 5.0) * 5.0 + 5.0
    X = lambda t: int(round(x0 + (x1 - x0) * t / t_max))            # noqa: E731
    Y = lambda v: int(round(y1 - (y1 - y0) * (v - lo) / (hi - lo)))  # noqa: E731
    step_t = _nice(t_max / 8.0)
    k = 0.0
    while k <= t_max + 1e-9:
        cv2.line(img, (X(k), y0), (X(k), y1), GRID, 1)
        cv2.putText(img, "%g" % k, (X(k) - 12, y1 + 18), font, 0.45, INK, 1, cv2.LINE_AA)
        k += step_t
    step_c = _nice((hi - lo) / 8.0)
    v = math.ceil(lo / step_c) * step_c
    while v <= hi + 1e-9:
        cv2.line(img, (x0, Y(v)), (x1, Y(v)), GRID, 1)
        cv2.putText(img, "%g" % v, (x0 - 44, Y(v) + 5), font, 0.45, INK, 1, cv2.LINE_AA)
        v += step_c
    cv2.rectangle(img, (x0, y0), (x1, y1), INK, 1)
    cv2.putText(img, "time (s)", ((x0 + x1) // 2 - 30, y1 + 40), font, 0.5, INK, 1, cv2.LINE_AA)
    cv2.putText(img, "T (C)", (8, y0 - 10), font, 0.5, INK, 1, cv2.LINE_AA)
    # 열선 켜진 구간: 아래 띠
    by0, by1 = y1 + 56, y1 + 76
    cv2.rectangle(img, (x0, by0), (x1, by1), GRID, 1)
    cur = None
    for t, on in list(elog) + [[t_max, 0]]:
        if on and cur is None:
            cur = t
        elif not on and cur is not None:
            cv2.rectangle(img, (X(cur), by0 + 1), (max(X(cur) + 1, X(t)), by1 - 1), PINK, -1)
            cur = None
    cv2.putText(img, "heater element on", (x1 + 12, by1 - 4), font, 0.45, PINK, 1, cv2.LINE_AA)
    # 목표선(점선) · 온도
    sp = [(X(p[0]), Y(p[2])) for p in pts if p[2] is not None and p[5] in (HEAT, HOLD)]
    for i in range(0, len(sp) - 1, 2):
        cv2.line(img, sp[i], sp[i + 1], BLUE, 1, cv2.LINE_AA)
    line = np.array([(X(p[0]), Y(p[1])) for p in pts], np.int32)
    if len(line) > 1:
        cv2.polylines(img, [line], False, INK, 2, cv2.LINE_AA)
    # 라벨: 축 밖 오른쪽 띠, 그 선의 마지막 점에서 회색 지시선
    labels = []
    if len(line):
        labels.append(("temperature", tuple(line[-1]), INK))
    if sp:
        labels.append(("target (ramp)" if cd.get("run_heat_mode") == "ramp" else "target", sp[-1], BLUE))
    ys = []
    for _name, (px, py), _col in sorted(labels, key=lambda a: a[1][1]):
        y = max(py, (ys[-1] + 18) if ys else y0 + 8)
        ys.append(min(y, y1))
    for (name, (px, py), col), y in zip(sorted(labels, key=lambda a: a[1][1]), ys):
        cv2.line(img, (px, py), (x1 + 8, y), LEAD, 1, cv2.LINE_AA)
        cv2.line(img, (x1 + 10, y), (x1 + 22, y), col, 2)
        cv2.putText(img, name, (x1 + 26, y + 5), font, 0.45, INK, 1, cv2.LINE_AA)
    return imgio.encode(img, ".png")


def _nice(x):
    if x <= 0:
        return 1.0
    e = 10 ** math.floor(math.log10(x))
    for m in (1, 2, 5, 10):
        if x <= m * e:
            return m * e
    return 10 * e


# ----------------------------------------------------------------------
class TempTest:
    def __init__(self, ctrl):
        self.c = ctrl
        self.heater = RelayHeater(ctrl)
        self._lock = threading.Lock()
        self._busy = False
        self._th = None
        self._stop = self._estop = False
        self.feeding = False                        # 시험이 온도를 읽어 안전 감시에 넘기는 중(감시 스레드가 따로 읽지 않는다)
        self.cond = {}
        self.folder = None
        self.pts = []                               # [경과 s, 온도, 목표 | None, 열선, 송풍, 단계]
        self.elog = []                              # [경과 s, 0|1] 열선을 바꾼 때
        self.ev = {}                                # pre · t_heat · T0 · t_reach · t_heat_end
        self.phase = None
        self.result = ""
        self.notes = []
        self.started = None                         # 시작 시각(epoch) - 화면이 새 시험인지 안다
        self.t0 = None                              # 시작 시각(clock)
        self.coolers = []
        self.last_T = None
        self.sp = None
        self._miss = 0
        self._f = None

    @property
    def active(self):
        return self._busy

    # ---- 명령 ----
    def block_reason(self):
        """지금 온도 시험을 시작할 수 없는 이유(없으면 "")."""
        c = self.c
        if c.runner.active:
            return "자동 측정 중 실행 불가"
        if c.contact_busy():
            return "접촉 탐색 중 실행 불가"
        if not c.stage.connected:
            return "스테이지 연결 필요"
        if c.k2110 is None or not c.k2110.is_open:
            return "온도계 연결 필요"
        why = c.safety.reject_relay_on()
        if why:
            return why
        if c._heater_on() or c._heater_starting or c.heater_fan_pending():
            return "히트건 끈 뒤 시작"
        if any(r is True for r in c.stage.relays):
            return "쿨러 끈 뒤 시작"
        return ""

    def start(self):
        c = self.c
        with c.runner._lock:                                        # 자동 측정 시작과 엇갈리지 않게(측정 시작도 이 잠금 안에서 온도 시험을 본다)
            with self._lock:
                if self._busy:
                    raise Rejected("이미 온도 시험 중")
                why = self.block_reason()
                if why:
                    raise Rejected(why)
                bad = config_mod.heat_problems(c.cfg.values) + config_mod.temptest_problems(c.cfg.values)
                if bad:
                    raise Rejected(bad[0])
                self.cond = {k: c.cfg[k] for k in COND_KEYS}
                self.started = time.time()
                self._open_folder()
                self.pts, self.elog, self.ev, self.notes = [], [], {}, []
                self.phase, self.result, self.last_T, self.sp, self._miss = PRECOOL, "", None, None, 0
                self.coolers = [int(n) for n in self.cond["temp_test_coolers"]]
                self._stop = self._estop = False
                self.t0 = c.clock()
                self._busy = True
                c.state.clear_alarm("temptest", "temptest_stall")
                self._th = threading.Thread(target=self._main, daemon=True, name="temptest")
                self._th.start()
        self.publish()
        return {"folder": str(self.folder)}

    def stop(self):
        if not self._busy:
            raise Rejected("온도 시험 중 아님")
        self._stop = True
        return {}

    def on_estop(self):
        self._estop = True

    def join(self, timeout=None):
        t = self._th
        if t is not None:
            t.join(timeout)

    def points(self, frm=0):
        frm = max(0, int(frm))
        return {"id": self.started, "n": len(self.pts), "points": self.pts[frm:]}

    def open_folder(self):
        if self.folder is None:
            raise Rejected("온도 시험 기록 없음")
        opener = getattr(os, "startfile", None)
        if opener is None:
            raise Rejected("이 PC 에서는 폴더 열기 불가 · %s" % self.folder)
        try:
            opener(str(self.folder))
        except OSError as e:
            raise Rejected("폴더 열기 실패 · %s" % str(e)[:50])
        return {"folder": str(self.folder)}

    # ---- 파일 ----
    def _open_folder(self):
        base = self.c.cfg.data_path() / "temp_test"
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started))
        try:
            base.mkdir(parents=True, exist_ok=True)
            for i in range(100):
                p = base / (name if i == 0 else "%s_%d" % (name, i))
                try:
                    p.mkdir()
                    break
                except FileExistsError:
                    continue
            else:
                raise OSError("폴더 이름 중복")
            self._f = open(p / "temp.csv", "w", encoding="utf-8-sig", newline="")
            self._f.write(",".join(CSV_HEADER) + "\r\n")
            self._f.flush()
        except OSError as e:
            raise Rejected("저장 폴더 생성 실패 · %s" % str(e)[:50])
        self.folder = p

    def _record(self, T, sp):
        c = self.c
        now = c.clock()
        t = now - self.t0
        el, fan = 1 if c._element_on() else 0, 1 if c._fan_on() else 0
        on = [n for n in self.coolers if c._relay_is_on(n)]
        ph = self.phase
        lt = time.localtime()
        row = "%s.%d,%.2f,%s,%.3f,%s,%d,%d,%s\r\n" % (time.strftime("%H:%M:%S", lt), int((time.time() % 1) * 10), t, PHASES[ph], T,
                                                      "" if sp is None else "%.3f" % sp, el, fan, " ".join("R%d" % n for n in on))
        try:
            self._f.write(row)
            self._f.flush()
        except OSError as e:
            raise _End("abort", "파일 쓰기 실패 · %s" % str(e)[:50])
        if len(self.pts) < MAX_POINTS:
            self.pts.append([round(t, 3), round(float(T), 3), None if sp is None else round(float(sp), 3), el, fan, ph])

    def _finish_files(self, res):
        f, self._f = self._f, None
        if f is not None:
            try:
                f.close()
            except OSError as e:
                self.c.logs.app("온도 시험 temp.csv 닫기 실패 · %s" % e, "warn")
        try:
            (self.folder / "summary.txt").write_text("\r\n".join(summary_lines(self.cond, res, self.result, self.started, self.notes)) + "\r\n",
                                                     encoding="utf-8-sig")
        except OSError as e:
            self.c.logs.app("온도 시험 summary.txt 저장 실패 · %s" % e, "error")
        try:
            title = "temperature test %s | %s | target %g C" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)),
                                                            "ramp %g C/min" % float(self.cond["run_heat_rate_c_min"]) if self.cond["run_heat_mode"] == "ramp" else "full",
                                                            float(self.cond["run_heater_off_c"]))
            imgio.write_bytes(self.folder / "graph.png", draw_png(self.pts, self.elog, self.ev, self.cond, title))
        except (imgio.ImageIOError, cv2.error) as e:
            self.c.logs.app("온도 시험 graph.png 저장 실패 · %s" % e, "error")

    # ---- 시험 스레드 ----
    def _main(self):
        c, cd = self.c, self.cond
        g = c.safety.generation
        c.logs.app("온도 시험 시작 · %s · %g → %g → %g °C · %s" % (mode_text(cd), cd["run_start_c"], cd["run_heater_off_c"], cd["run_cool_end_c"], self.folder))
        kind, text = "done", ""
        try:
            self._run(g)
        except _End as e:
            kind, text = e.kind, e.text
        except Exception as e:                                      # noqa: BLE001 - 예상 못 한 오류도 정리 · 저장을 거친다
            kind, text = "abort", "오류 · %s: %s" % (type(e).__name__, str(e)[:60])
            c.logs.app("온도 시험 오류 · %s: %s" % (type(e).__name__, e), "error")
        finally:
            self.feeding = False
            self._cleanup()
            self.result = {"done": DONE, "stop": STOPPED, "estop": ESTOPPED}.get(kind) or "%s · %s" % (ABORTED, text)
            res = summarize(self.pts, self.elog, self.ev, cd)
            self._finish_files(res)
            h = res.get("heat") or {}
            cl = res.get("cool") or {}
            c.logs.app("온도 시험 %s · 목표 도달 %s s · 평균 %s °C/분 · 넘침 %s °C · 냉각 %s s · %s" % (
                self.result, _f(h.get("reach_s")), _f(h.get("rate_c_min"), "%.2f"), _f(h.get("over_c"), "%+.2f"), _f(cl.get("s")), self.folder),
                "warn" if kind == "abort" else "info")
            if kind == "abort":
                c.state.alarm("temptest", "온도 시험 중단 · %s" % text, NOTICE, dismiss=True)
            self.phase = None
            self._busy = False
            self.publish()

    def _run(self, g):
        c, cd = self.c, self.cond
        start_c, off_c, end_c = float(cd["run_start_c"]), float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"])
        for n in self.coolers:
            self._check(g)
            try:
                c.relay_on(n)
            except Rejected as e:
                self._check(g)
                raise _End("abort", "쿨러 R%d 켜기 실패 · %s" % (n, e))
        self.feeding = True
        # ---- 1 예비 냉각 ----
        self.phase = PRECOOL
        t_pre = c.clock()
        T = self._read()
        if cd["temp_test_precool"]:
            stalled = False
            if T is None or T > start_c:
                T, stalled = self._cool_loop(g, start_c)
            self.ev["pre"] = {"s": round(c.clock() - t_pre, 1), "T": None if T is None else round(T, 2), "stalled": stalled}
            if stalled:
                note = "예비 냉각 정체 · %.1f °C 에서 가열 시작" % T
                self.notes.append(note)
                c.state.alarm("temptest_stall", note, NOTICE, dismiss=True)
        # ---- 2 가열 ----
        self._check(g)
        self.phase = HEAT
        T0 = self._read()
        if T0 is None:
            T0 = self.last_T
        if T0 is None:
            raise _End("abort", "온도 측정 실패 · 열전대 확인 필요")
        trips0 = c.heater_trips
        self._check(g)
        try:
            self.heater.on()                                        # 열선 → heater_start_gap_s → 송풍(측정 주체 규칙)
        except Rejected as e:
            self._check(g)
            raise _End("abort", "히트건 켜기 실패 · %s" % e)
        t_heat = c.clock()
        self.ev.update(t_heat=round(t_heat - self.t0, 3), T0=round(T0, 3))
        self.elog.append([round(t_heat - self.t0, 3), 1 if self.heater.element_on else 0])
        self._heat(g, t_heat, T0, off_c, trips0)
        if self.heater.element_on:
            self.heater.element(False)                              # 가열 끝: 열선 끔
        self.elog.append([round(c.clock() - self.t0, 3), 0])
        self.ev["t_heat_end"] = round(c.clock() - self.t0, 3)
        self.sp = None
        if not cd["run_fan_during_cool"]:
            self.heater.off()                                       # 송풍은 열선 끈 뒤 heater_fan_after_s 에 꺼진다
        # ---- 3 냉각 ----
        self.phase = COOL
        self._cool_loop(g, end_c)

    def _check(self, g):
        c = self.c
        if self._estop or not c.safety.gen_ok(g) or c.safety.locked:
            raise _End("estop", "비상정지")
        if self._stop:
            raise _End("stop")
        if not c.stage.connected:
            raise _End("abort", "스테이지 연결 끊김")

    def _read(self):
        c = self.c
        dev = c.k2110
        if dev is None or not dev.is_open:
            raise _End("abort", "온도계 연결 끊김")
        try:
            T = dev.read_temp()
        except meters.VisaError as e:
            c.state.set_device("k2110", "오류", str(e), dev.addr)
            raise _End("abort", "온도계 오류 · %s" % str(e)[:50])
        c.feed_temperature(T)                                       # 이 값을 안전 감시에 그대로 넘긴다
        if T is None:
            self._miss += 1
            if self._miss >= T_MISS_N:
                raise _End("abort", "온도 측정 실패 · 열전대 확인 필요")
        else:
            self._miss = 0
            self.last_T = T
        return T

    def _tick(self, t_it):
        self.publish()
        c = self.c
        c.sleep(max(0.0, float(self.cond["temp_test_interval_s"]) - (c.clock() - t_it)))

    def _heat(self, g, t_heat, T0, off_c, trips0):
        """가열(ramp · full) → 가열 끝 온도 도달(ramp 는 유지 시간까지). 안전 규칙이 열선을 끄면 중단."""
        c, cd = self.c, self.cond
        ramp = cd["run_heat_mode"] == "ramp"
        rate, band = float(cd["run_heat_rate_c_min"]), float(cd["heater_band_c"])
        min_on, min_off, hold_s = float(cd["heater_min_on_s"]), float(cd["heater_min_off_s"]), float(cd["run_heat_hold_s"])
        limit = float(cd["run_phase_limit_s"])
        el_t = t_heat
        hold_t0 = None
        while True:
            self._check(g)
            if c.heater_trips != trips0:
                raise _End("abort", c.heater_trip or "히트건 차단")
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            el_on = self.heater.element_on
            sp = ramp_setpoint(T0, rate, now - t_heat, off_c) if ramp else off_c
            self.sp = sp
            if T is not None:
                self._record(T, sp)
            if c.heater_trips != trips0:                            # 안전 규칙이 열선을 껐다(차단 온도 · 열선 연속 최대 시간 · 온도 못 읽음)
                raise _End("abort", c.heater_trip or "히트건 차단")
            if T is not None:
                if hold_t0 is None and T >= off_c:
                    self.ev["t_reach"] = round(now - self.t0, 3)
                    if not ramp or hold_s <= 0:
                        return
                    hold_t0 = now
                    self.phase = HOLD
                if hold_t0 is not None and now - hold_t0 >= hold_s:
                    return
                if ramp:
                    want = element_decision(T, sp, band, el_on, now - el_t, min_on, min_off)
                    if want != el_on:
                        try:
                            self.heater.element(want)
                        except Rejected as e:
                            self._check(g)
                            raise _End("abort", "열선 켜기 거절 · %s" % e)
                        el_t = now
                        self.elog.append([round(now - self.t0, 3), 1 if want else 0])
            if now - t_heat > limit:
                raise _End("abort", "가열 %g초 초과" % limit)
            self._tick(t_it)

    def _cool_loop(self, g, target):
        """예비 냉각 · 냉각: 온도 ≤ target 이거나 정체(run_stall_s 동안 run_stall_c 도 안 내려감)까지. (마지막 온도, 정체였나)."""
        c, cd = self.c, self.cond
        span, step, limit = float(cd["run_stall_s"]), float(cd["run_stall_c"]), float(cd["run_phase_limit_s"])
        t_start = c.clock()
        best = best_t = None
        T = None
        while True:
            self._check(g)
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            if T is not None:
                self._record(T, None)
                if T <= target:
                    return T, False
                if best is None or T < best - step:
                    best, best_t = T, now
                elif now - best_t >= span:
                    return T, True
            if now - t_start > limit:
                raise _End("abort", "%s %g초 초과" % (PHASES[self.phase], limit))
            self._tick(t_it)

    def _cleanup(self):
        """어떤 끝에서도: 히트건 끄는 순서(열선 끔 → 송풍은 heater_fan_after_s 뒤 - 감시가 끈다) · 쿨러 끔. 비상정지면 r all 0 이 이미 껐다."""
        c = self.c
        self.sp = None
        try:
            if c.stage.connected:
                self.heater.off()
        except Rejected as e:
            c.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
            c.logs.app("온도 시험 뒤 히트건 끄기 실패 · %s" % e, "error")
        if self.ev.get("t_heat") is not None and self.ev.get("t_heat_end") is None:
            self.ev["t_heat_end"] = round(c.clock() - self.t0, 3)       # 가열 중에 끝났다 - 넘침 · 켜진 시간은 여기까지
            if self.elog and self.elog[-1][1]:
                self.elog.append([self.ev["t_heat_end"], 0])
        for n in self.coolers:
            try:
                if c.stage.connected and c.stage.relays[n - 1] is not False:
                    c.relay_off(n)
            except Rejected as e:
                c.state.alarm("cooler_off_fail", "쿨러 끄기 실패 · 전원 직접 차단 필요", ALARM)
                c.logs.app("온도 시험 뒤 쿨러 끄기 실패 · %s" % e, "error")

    # ---- 화면 ----
    def view(self):
        c = self.c
        ht = c.state.get("heater") or {}
        return {"active": self._busy, "id": self.started, "phase": None if self.phase is None else PHASES[self.phase],
                "t": None if self.t0 is None or not self._busy else round(c.clock() - self.t0, 1),
                "T": None if self.last_T is None else round(self.last_T, 2), "sp": None if self.sp is None else round(self.sp, 2),
                "element": ht.get("element"), "fan": ht.get("fan"), "coolers": [n for n in self.coolers if c._relay_is_on(n)] if self._busy else [],
                "result": self.result, "notes": list(self.notes), "folder": None if self.folder is None else str(self.folder),
                "cond": dict(self.cond), "n": len(self.pts),
                "summary": summarize(self.pts, self.elog, self.ev, self.cond) if self.cond else None}

    def publish(self):
        self.c.state.set("temptest", self.view())
