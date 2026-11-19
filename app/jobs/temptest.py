"""temptest.py - 온도 시험(수동 제어 탭 [온도 시험]): 샘플 · 저항계 · 모터 없이 히트건과 쿨러의 온도 거동만 잰다.

시험 셋(mode): test = 자동 측정과 같은 가열 · 냉각(열선 출력 % 제어 - heater.LineControl) ·
tune = [히트건 맞추기](_run_tune - 출력 방식(TUNE_METHODS)을 같은 출력에서 비교해 출렁임이 가장 작은 것을 고르고, 내림 · 올림 계단을
heater.fit_fopdt 로 맞춰 열 모델 K · τ · θ - 고른 방식 · 열 모델을 설정에 저장) · max = [최대 속도 재기](출력 최대로 가열 끝 온도까지 → 0 % 로
냉각 끝 온도까지, 5 °C 구간마다 최대 가열 · 냉각 속도를 calib speed_max 에 저장 - heater.speed_table). 펌웨어 V14 필요(열선 출력 0.1 % · 출력 방식).
가열 · 냉각 속도 = 목표 점 간격 ÷ temp_test_interval_s × 60(config.heat_rate_c_min).

목적: "몇 °C 까지 몇 초에 도달하는지" · 최대 가열 속도 · 넘침(열선을 끈 뒤 더 오르는 정도) · 냉각 시간을 보고 히트건 · 가열 제어 값을 정한다.
조건은 자동 측정과 같은 설정(run_start_c · run_heater_off_c · run_cool_end_c · run_heat_mode · run_point_step_c · cool_rate_separate ·
heater_lead_* · heater_max_pct · heat_model_* · run_heat_hold_s · run_fan_during_cool · run_stall_s · run_stall_c · run_phase_limit_s)에
온도 시험 설정(temp_test_coolers · temp_test_interval_s · temp_test_mark_c · temp_test_precool)을 더해 쓴다 - 여기서 맞춘 값이 측정에 그대로 쓰인다.

순서
  1. 예비 냉각 : 쿨러(temp_test_coolers)를 켠다. temp_test_precool 이면 시작 온도 이하까지. 정체(run_stall_s 동안 run_stall_c 도 안 내려감)면
                가열하지 않고 "중단 · 시작 온도 미도달 · 현재 xx.x °C". 꺼져 있으면 쿨러만 켜고 바로 가열. 쿨러는 시험 끝까지 켜 둔다.
  2. 가열     : 히트건 켜기(Controller 의 히트건 함수 - 열선 → 송풍, 측정 주체 규칙). ramp 이면 자동 측정과 같은 목표선 제어
                (heater.LineControl - 열선 출력 % · 목표선 기다림)로 따르고 가열 끝 온도에 닿으면 유지 시간 동안 유지. full 이면 닿을 때까지 열선 켬.
  3. 냉각     : 열선 끔. run_cool_mode ramp 이면 송풍을 켜 둔 채 같은 LineControl(냉각 속도)로 내려가고, free 면 자연 냉각(송풍은 run_fan_during_cool).
                냉각 끝 온도 이하 또는 정체까지 기록(정체면 "냉각 끝 온도 미도달").
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
from app.jobs import analysis
from app.devices.stage import FW_NEED
from app.jobs import heater as heater_mod
from app.jobs.heater import LineControl, RelayHeater, Stall
from app.safety import Rejected
from app.state import ALARM, NOTICE

PRECOOL, HEAT, HOLD, COOL, SETTLE, COMPARE, STEP = 0, 1, 2, 3, 4, 5, 6
PHASES = ("예비 냉각", "가열", "유지", "냉각", "안정", "비교", "계단")      # 안정 · 비교 · 계단 = [히트건 맞추기](가열 · 냉각 요약은 이 단계 점을 쓰지 않는다)
DONE, STOPPED, ESTOPPED, ABORTED = "완료", "정지", "비상정지", "중단"
CSV_HEADER = ("시각", "경과(s)", "단계", "온도(°C)", "목표(°C)", "열선 출력(%)", "출력 방식", "송풍", "쿨러")
COND_KEYS = ("run_start_c", "run_heater_off_c", "run_cool_end_c", "run_heat_mode", "run_point_step_c", "cool_rate_separate",
             "run_heat_hold_s", "run_fan_during_cool", "run_stall_s", "run_stall_c", "run_phase_limit_s",
             "temp_test_coolers", "temp_test_interval_s", "temp_test_mark_c", "temp_test_precool", "temp_step_pct", "temp_step_s",
             "heater_max_c", "heater_max_on_s", "temp_stale_s", "heater_start_gap_s", "heater_fan_after_s", "heater_fan_relay", "heater_element_relay",
             "run_cool_mode", "run_cool_rate_c_min", "heater_lead_c", "heater_lead_filter_s", "heater_lead_hold_s", "heater_max_pct", "heater_full_pct",
             "heat_model_k", "heat_model_tau_s", "heat_model_theta_s", "reversal_c")
MODES = {"test": "온도 시험", "tune": "히트건 맞추기", "max": "최대 속도 재기"}
# [히트건 맞추기]: 비교할 출력 방식(안전한 것 → 잘게 켜는 것 순) · 시간 · 계단 아래쪽 출력 · H-E 의심 기준
TUNE_METHODS = (("window", 5000), ("window", 1000), ("spread", 100), ("spread", 10))
TUNE_SETTLE_S, TUNE_SEG_S, TUNE_SKIP_S, TUNE_LOW_PCT, TUNE_HE_DROP_C = 40, 60, 20, 10, 10


def method_name(mode, ms):
    return ("고르게 %d ms" if mode == "spread" else "몰아 켜기 %d ms") % int(ms)
IV = "temp_test_interval_s"
T_MISS_N = 3                                      # 온도를 이만큼 연속으로 못 읽으면 중단(열전대 끊김) - 자동 측정과 같다
MAX_POINTS = 200000


class _End(Exception):
    """시험을 끝낸다: kind = stop([정지]) · estop(비상정지) · abort(안전 규칙 · 장비 오류 - text 가 이유)."""

    def __init__(self, kind, text=""):
        super().__init__(text or kind)
        self.kind, self.text = kind, text


def mode_text(cd):
    heat = "가열 속도 제어 %.2f °C/분" % config_mod.heat_rate_c_min(cd, IV) if cd.get("run_heat_mode") == "ramp" else "계속 가열"
    cool = "냉각 속도 제어 %.2f °C/분" % config_mod.cool_rate_c_min(cd, IV) if cd.get("run_cool_mode") == "ramp" else "자연 냉각"
    return "%s · %s" % (heat, cool)


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
    """점 [경과 s, 온도, 목표 | None, 열선 출력 %, 송풍 0|1, 단계] · 열선 기록 [경과 s, 출력 %] · 사건 ev → 결과.
    시간은 모두 가열 시작(히트건을 켠 때)부터 잰다(예비 냉각은 시험 시작부터)."""
    off_c, end_c, step = float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"]), float(cd["temp_test_mark_c"])
    out = {"pre": None, "T0": None, "marks": [], "heat": None, "cool": None,
           "spacing": {"heat": analysis.spacing([p[1] for p in pts if p[5] in (HEAT, HOLD)]), "cool": analysis.spacing([p[1] for p in pts if p[5] == COOL])}}
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
    on_n, on_s, cur = 0, 0.0, None                                  # 열선을 0 에서 켠 횟수 · 최대 출력으로 환산한 켜진 시간(가열 시작 ~ 가열 끝)
    for t, pct in elog:
        if cur is not None:
            on_s += cur[1] / 100.0 * max(0.0, min(t, last_t) - cur[0])
        if pct and (cur is None or not cur[1]):
            on_n += 1
        cur = (t, pct)
    if cur is not None and cur[1]:
        on_s += cur[1] / 100.0 * max(0.0, last_t - cur[0])
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
    rc = float(cd.get("reversal_c", 0.3))
    out["curve"] = {"heat": analysis.curve_stats([(p[0], p[1], p[2]) for p in hp], float(cd["run_start_c"]), off_c, 1, rc),
                    "cool": analysis.curve_stats([(p[0], p[1], p[2]) for p in pts if p[5] == COOL], end_c, off_c, -1, rc)}
    cp = [p for p in pts if p[5] == COOL]
    if t_end is not None and cp:
        T_from = cp[0][1]
        hit = next((p for p in cp if p[1] <= end_c), None)
        last = hit or cp[-1]
        s = last[0] - t_end
        out["cool"] = {"from_c": round(T_from, 2), "to_c": round(last[1], 2), "s": round(s, 1), "reached": hit is not None,
                       "rate_c_min": None if s <= 0 else round((T_from - last[1]) / s * 60.0, 2), "rms_c": None, "max_c": None}
        err = [p[1] - p[2] for p in cp if p[2] is not None]
        if err:
            out["cool"]["rms_c"] = round(math.sqrt(sum(e * e for e in err) / len(err)), 2)
            out["cool"]["max_c"] = round(max(err, key=abs), 2)
    return out


def _f(v, fmt="%.1f", none="—"):
    return none if v is None else fmt % v


def tune_lines(tn):
    """[히트건 맞추기] 결과 줄: 방식 표 · 고른 방식 · 계단 둘 · 저장한 값."""
    L = ["", "[히트건 맞추기] 방식 · 평균 °C · 출렁임 °C · 판정"]
    for s in tn.get("segs") or []:
        L.append("%s · %s · %s · %s" % (s["name"], _f(s["mean_c"], "%.2f"), _f(s["ripple_c"], "%.2f"),
                                        "H-E 의심" if s["he"] else "선택" if s["name"] == tn.get("pick") else "-"))
    L.append("고른 방식 · %s" % (tn.get("pick") or "없음"))
    for st in tn.get("steps") or []:
        L.append("계단 %g → %g %% · K %s · τ %s s · θ %s s · 오차 %s °C" % (st["from"], st["to"], _f(st["K"], "%.4f"), _f(st["tau"], "%.1f"),
                                                                     _f(st["theta"], "%.1f"), _f(st["err"], "%.2f")))
    m = tn.get("model")
    if m:
        L.append("열 모델 · K %.4f °C/%% · τ %.1f s · θ %.1f s · 오차 %.2f °C" % (m["K"], m["tau"], m["theta"], m["err"]))
    L.append("저장 · %s" % ("고른 방식%s" % (" · 열 모델" if m else "") if tn.get("saved") else "안 함"))
    return L


def summary_lines(cd, res, result, started, notes):
    """summary.txt 의 줄(조건 + 결과). [히트건 맞추기] 는 조건 뒤에 맞추기 결과만."""
    L = ["온도 시험 · %s" % time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(started)), "결과 · %s" % result, ""]
    L.append("[조건]")
    L.append("가열 방식 · %s" % mode_text(cd))
    L.append("시작 온도 %g °C · 가열 끝 온도 %g °C · 냉각 끝 온도 %g °C" % (cd["run_start_c"], cd["run_heater_off_c"], cd["run_cool_end_c"]))
    L.append("목표 점 간격 %g °C · 읽기 간격 %g s · 열선 최대 출력 %d %% · 유지 %g s" % (cd["run_point_step_c"], cd["temp_test_interval_s"], cd["heater_max_pct"], cd["run_heat_hold_s"]))
    L.append("목표선 기다림 폭 %g °C · 평균 창 %g s · 지속 %g s" % (cd["heater_lead_c"], cd["heater_lead_filter_s"], cd["heater_lead_hold_s"]))
    L.append("열 모델 · %s" % heater_mod.ThermalModel(cd).text())
    L.append("냉각 중 송풍 %s · 예비 냉각 %s · 쿨러 %s" % ("켬" if cd["run_fan_during_cool"] else "끔", "켬" if cd["temp_test_precool"] else "끔",
                                                  " · ".join("R%d" % n for n in cd["temp_test_coolers"]) or "없음"))
    L.append("냉각 정체 %g s · %g °C · 한 단계 한도 %g s · 읽기 간격 %g s" % (cd["run_stall_s"], cd["run_stall_c"], cd["run_phase_limit_s"], cd["temp_test_interval_s"]))
    L.append("차단 온도 %g °C · 열선 연속 최대 %g s · 열선 후 송풍 %g s · 열선 끈 뒤 송풍 %g s" % (cd["heater_max_c"], cd["heater_max_on_s"], cd["heater_start_gap_s"], cd["heater_fan_after_s"]))
    if res.get("tune") is not None:
        L.append("맞추기 출력 %g %% · 계단 유지 %g s · 계단 아래 %d %%" % (cd["temp_step_pct"], cd["temp_step_s"], TUNE_LOW_PCT))
        L += tune_lines(res["tune"])
        if notes:
            L += ["", "[메모]"] + list(notes)
        return L
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
        L.append("열선 켠 횟수 %d · 최대 출력 환산 %s s · 평균 출력 %s %%" % (h["on_n"], _f(h["on_s"]), _f(None if h["on_ratio"] is None else h["on_ratio"] * 100, "%.0f")))
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
        if cl.get("rms_c") is not None:
            L.append("목표선 차이 평균 제곱근 %.2f °C · 최대 %+.2f °C" % (cl["rms_c"], cl["max_c"]))
    else:
        L.append("없음")
    L += ["", "[점 · 속도 · 역전] (가열 = 시작 ~ 가열 끝 온도 · 냉각 = 냉각 끝 ~ 가열 끝 온도)"]
    for key, name in (("heat", "가열"), ("cool", "냉각")):
        cs = (res.get("curve") or {}).get(key)
        if not cs:
            L.append("%s · 없음" % name)
            continue
        L.append("%s · 점 %d개 · 평균 %s °C/분 · 점 간격 %s ± %s °C · 역전 %d회 · 최대 %s °C · 목표선 차이 평균 %s · 최대 %s °C" % (
            name, cs["n"], _f(cs["rate_c_min"], "%.2f"), _f(cs["step_mean"], "%.3f"), _f(cs["step_sd"], "%.3f"), cs["rev_n"], _f(cs["rev_max"], "%.2f"),
            _f(cs["sp_err_mean"], "%.2f"), _f(cs["sp_err_max"], "%.2f")))
    if res.get("speed"):
        L += ["", "[최대 속도] 구간 · 가열 · 냉각 °C/분"]
        for lo, hv, cv in res["speed"]:
            L.append("%g~%g °C · %s · %s" % (lo, lo + heater_mod.SPEED_BAND_C, _f(hv, "%.1f"), _f(cv, "%.1f")))
    if notes:
        L += ["", "[메모]"] + list(notes)
    return L


# ----------------------------------------------------------------------
# 그림(graph.png) - 서버가 그린다(창을 닫아도 남는다). OpenCV 글꼴은 영문만 쓴다
# ----------------------------------------------------------------------
INK, BLUE, PINK, GRID, LEAD = (41, 35, 31), (214, 120, 42), (125, 58, 192), (228, 225, 222), (160, 160, 160)       # BGR


def draw_png(pts, elog, ev, cd, title, rev=()):
    """rev = 역전 자리 [(경과, 온도)] - 선 위에 글자 없이 작은 표시(삼각형)."""
    W, H = 1200, 720
    L, R, T, B = 80, 190, 56, 120                                   # 오른쪽 띠 = 라벨(지시선으로 잇는다) · 아래 띠 = 열선 출력 %
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
    # 열선 출력 %: 아래 띠(높이 = 출력)
    by0, by1 = y1 + 50, y1 + 96
    cv2.rectangle(img, (x0, by0), (x1, by1), GRID, 1)
    seq = list(elog) + [[t_max, 0]]
    for (ta, pa), (tb, _pb) in zip(seq, seq[1:]):
        if pa:
            top = int(round(by1 - 1 - (by1 - by0 - 2) * min(100.0, float(pa)) / 100.0))
            cv2.rectangle(img, (X(ta), top), (max(X(ta) + 1, X(tb)), by1 - 1), PINK, -1)
    cv2.putText(img, "heater %", (x1 + 12, by1 - 4), font, 0.45, PINK, 1, cv2.LINE_AA)
    cv2.putText(img, "100", (x0 - 34, by0 + 10), font, 0.4, INK, 1, cv2.LINE_AA)
    cv2.putText(img, "0", (x0 - 16, by1), font, 0.4, INK, 1, cv2.LINE_AA)
    # 목표선(점선) · 온도
    sp = [(X(p[0]), Y(p[2])) for p in pts if p[2] is not None]
    for i in range(0, len(sp) - 1, 2):                              # 점선(가열 · 유지 · 냉각 목표선)
        cv2.line(img, sp[i], sp[i + 1], BLUE, 1, cv2.LINE_AA)
    line = np.array([(X(p[0]), Y(p[1])) for p in pts], np.int32)
    if len(line) > 1:
        cv2.polylines(img, [line], False, INK, 2, cv2.LINE_AA)
    for t, v in rev:                                                # 역전 자리: 작은 빨간 삼각형(글자 없음)
        px, py = X(t), Y(v)
        cv2.fillConvexPoly(img, np.array([(px, py - 9), (px - 5, py - 17), (px + 5, py - 17)], np.int32), (35, 35, 200), cv2.LINE_AA)
    # 라벨: 축 밖 오른쪽 띠, 그 선의 마지막 점에서 회색 지시선
    labels = []
    if len(line):
        labels.append(("temperature", tuple(line[-1]), INK))
    if sp:
        labels.append(("target", sp[-1], BLUE))
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
        self.mode = "test"                          # test · tune(히트건 맞추기) · max(최대 속도 재기)
        self.tune = None                            # [히트건 맞추기] 결과 {"segs", "pick", "steps", "model", "saved"}
        self.step_text = ""                         # 지금 하는 일 한 줄(맞추기 - "비교 3/4 · 고르게 100 ms")
        self.folder = None
        self.pts = []                               # [경과 s, 온도, 목표 | None, 열선 출력 %, 송풍, 단계]
        self.elog = []                              # [경과 s, 출력 %] 열선 출력을 바꾼 때
        self.ev = {}                                # pre · t_heat · T0 · t_reach · t_heat_end · t_step · t_step_end
        self.phase = None
        self.result = ""
        self.notes = []
        self.started = None                         # 시작 시각(epoch) - 화면이 새 시험인지 안다
        self.t0 = None                              # 시작 시각(clock)
        self.coolers = []
        self.last_T = None
        self.sp = None
        self.model = None                           # [히트건 맞추기] 결과 {"u", "K", "tau", "theta", "err"}
        self.speed = None                           # [최대 속도 재기] 결과 [[구간 아래, 가열, 냉각], …]
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
        if not c.stage.heat_capable:
            return FW_NEED
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

    def start(self, mode="test"):
        """mode: test(온도 시험) · tune([히트건 맞추기]) · max([최대 속도 재기])."""
        c = self.c
        if mode not in MODES:
            raise Rejected("시험 종류 오류")
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
                self.mode = mode
                self.started = time.time()
                self._open_folder()
                self.pts, self.elog, self.ev, self.notes = [], [], {}, []
                self.phase, self.result, self.last_T, self.sp, self._miss = PRECOOL, "", None, None, 0
                self.model, self.speed, self.tune, self.step_text = None, None, None, ""
                if c.stage.heat_capable:
                    c.heater_method_restore()                       # 설정의 출력 방식으로 시작(맞추기는 안에서 바꾼다)
                self.coolers = [int(n) for n in self.cond["temp_test_coolers"]]
                self._stop = self._estop = False
                self.t0 = c.clock()
                self._busy = True
                c.state.clear_alarm("temptest", "temptest_stall")
                if mode == "test":
                    from app.jobs.run import heat_notices
                    heat_notices(c, self.cond, IV, "온도 시험")             # 열 모델 없음 · 최대 속도 초과 주의(막지 않는다)
                self._th = threading.Thread(target=self._main, daemon=True, name="temptest")
                self._th.start()
        self.publish()
        return {"folder": str(self.folder), "mode": mode}

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
        return {"id": self.started, "n": len(self.pts), "points": self.pts[frm:], "elog": self.elog}

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
        name = time.strftime("%Y%m%d_%H%M%S", time.localtime(self.started)) + {"test": "", "tune": "_맞추기", "max": "_최대속도"}[self.mode]
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
        pct, fan = c.heater_pct(), 1 if c._fan_on() else 0
        on = [n for n in self.coolers if c._relay_is_on(n)]
        ph = self.phase
        lt = time.localtime()
        row = "%s.%d,%.2f,%s,%.3f,%s,%.1f,%s,%d,%s\r\n" % (time.strftime("%H:%M:%S", lt), int((time.time() % 1) * 10), t, PHASES[ph], T,
                                                           "" if sp is None else "%.3f" % sp, pct, c.stage.method_text(), fan, " ".join("R%d" % n for n in on))
        try:
            self._f.write(row)
            self._f.flush()
        except OSError as e:
            raise _End("abort", "파일 쓰기 실패 · %s" % str(e)[:50])
        if len(self.pts) < MAX_POINTS:
            self.pts.append([round(t, 3), round(float(T), 3), None if sp is None else round(float(sp), 3), pct, fan, ph])

    def _elog(self, now, pct):
        self.elog.append([round(now - self.t0, 3), round(float(pct), 1)])

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
            kind = {"test": "ramp %.1f C/min" % config_mod.heat_rate_c_min(self.cond, IV) if self.cond["run_heat_mode"] == "ramp" else "full",
                    "tune": "heater tune", "max": "max speed"}[self.mode]
            title = "temperature test %s | %s | target %g C" % (time.strftime("%Y-%m-%d %H:%M", time.localtime(self.started)), kind,
                                                            float(self.cond["run_heater_off_c"]))
            cv = res.get("curve") or {}
            rev = [tuple(x) for k in ("heat", "cool") for x in ((cv.get(k) or {}).get("rev_at") or [])]
            imgio.write_bytes(self.folder / "graph.png", draw_png(self.pts, self.elog, self.ev, self.cond, title, rev))
        except (imgio.ImageIOError, cv2.error) as e:
            self.c.logs.app("온도 시험 graph.png 저장 실패 · %s" % e, "error")

    def _summary(self):
        res = summarize(self.pts, self.elog, self.ev, self.cond) if self.cond else None
        if res is not None:
            res["model"] = self.model
            res["speed"] = self.speed
            res["tune"] = self.tune if self.mode == "tune" else None
        return res

    # ---- 시험 스레드 ----
    def _main(self):
        c, cd = self.c, self.cond
        g = c.safety.generation
        c.logs.app("%s 시작 · %s · %g → %g → %g °C · %s" % (MODES[self.mode], mode_text(cd), cd["run_start_c"], cd["run_heater_off_c"], cd["run_cool_end_c"], self.folder))
        kind, text = "done", ""
        try:
            {"test": self._run, "tune": self._run_tune, "max": self._run_max}[self.mode](g)
        except _End as e:
            kind, text = e.kind, e.text
        except Exception as e:                                      # noqa: BLE001 - 예상 못 한 오류도 정리 · 저장을 거친다
            kind, text = "abort", "오류 · %s: %s" % (type(e).__name__, str(e)[:60])
            c.logs.app("온도 시험 오류 · %s: %s" % (type(e).__name__, e), "error")
        finally:
            self.feeding = False
            self._cleanup()
            self.result = {"done": DONE, "stop": STOPPED, "estop": ESTOPPED}.get(kind) or "%s · %s" % (ABORTED, text)
            try:
                self._results(kind)
            except Exception as e:                                  # noqa: BLE001 - 맞추기 · 표 계산이 실패해도 기록 · 요약은 저장한다
                self.notes.append("결과 계산 실패 · %s" % str(e)[:60])
                c.logs.app("%s 결과 계산 실패 · %s: %s" % (MODES[self.mode], type(e).__name__, e), "error")
            res = self._summary()
            self._finish_files(res)
            h = res.get("heat") or {}
            cl = res.get("cool") or {}
            cv = res.get("curve") or {}
            c.logs.app("%s %s · 목표 도달 %s s · 넘침 %s °C · 냉각 %s s · 가열 점 %s · 냉각 점 %s · 역전 %s / %s · %s" % (
                MODES[self.mode], self.result, _f(h.get("reach_s")), _f(h.get("over_c"), "%+.2f"), _f(cl.get("s")),
                (cv.get("heat") or {}).get("n", "—"), (cv.get("cool") or {}).get("n", "—"),
                (cv.get("heat") or {}).get("rev_n", "—"), (cv.get("cool") or {}).get("rev_n", "—"), self.folder),
                "warn" if kind == "abort" else "info")
            if kind == "abort":
                c.state.alarm("temptest", "온도 시험 중단 · %s" % text, NOTICE, dismiss=True)
            self.phase = None
            self._busy = False
            self.publish()

    def _results(self, kind):
        """[히트건 맞추기] → 고른 출력 방식 · 열 모델 저장(설정) · [최대 속도 재기] → 최대 속도 표 저장(calib speed_max). 끝까지 갔을 때만."""
        c = self.c
        tn = self.tune
        if self.mode == "tune" and kind == "done" and tn and tn.get("pick"):
            seg = next(s for s in tn["segs"] if s["name"] == tn["pick"])
            kw = {"heater_out_mode": seg["mode"]}
            kw["heater_pwm_period_ms" if seg["mode"] == "window" else "heater_slot_ms"] = int(seg["ms"])
            fits = [s for s in tn["steps"] if s["K"] is not None]
            if len(fits) == 2:
                m = {"K": round(sum(s["K"] for s in fits) / 2, 5), "tau": round(sum(s["tau"] for s in fits) / 2, 2),
                     "theta": round(sum(s["theta"] for s in fits) / 2, 2), "err": max(s["err"] for s in fits)}
                tn["model"] = m
                kw.update(heat_model_k=m["K"], heat_model_tau_s=m["tau"], heat_model_theta_s=m["theta"])
                if abs(fits[0]["K"] - fits[1]["K"]) > 0.3 * max(fits[0]["K"], fits[1]["K"]):
                    self.notes.append("내림 · 올림 반응 차이 큼")
            else:
                self.notes.append("열 모델 맞추기 실패 · 출력 방식만 저장")
            c._remember(**kw)                                       # 장비 값 - 코드 기본값에도 옮기면 GitHub 로 관리된다
            tn["saved"] = True
            if tn["model"]:
                c.state.clear_alarm("heat_model")
            if c.stage.heat_capable:
                try:
                    c.heater_method_restore()                       # 저장한 방식을 펌웨어에도
                except Rejected as e:
                    c.logs.app("히트건 맞추기 · 출력 방식 보내기 실패 · %s" % e, "warn")
            c.logs.app("히트건 맞추기 · 고른 방식 %s · 열 모델 %s" % (tn["pick"], heater_mod.ThermalModel(c.cfg.values).text() if tn["model"] else "저장 안 함"))
        if self.mode == "max" and kind == "done":
            heat = [(p[0], p[1]) for p in self.pts if p[5] == HEAT]
            cool = [(p[0], p[1]) for p in self.pts if p[5] == COOL]
            self.speed = heater_mod.speed_table(heat, cool)
            c.calib.set_speed_max(self.speed)
            c.logs.app("최대 속도 재기 · 저장 · %s" % " / ".join("%g°C %s·%s" % (r[0], _f(r[1]), _f(r[2])) for r in self.speed))

    def _coolers_on(self, g):
        c = self.c
        for n in self.coolers:
            self._check(g)
            try:
                c.relay_on(n)
            except Rejected as e:
                self._check(g)
                raise _End("abort", "쿨러 R%d 켜기 실패 · %s" % (n, e))

    def _precool(self, g, start_c):
        c, cd = self.c, self.cond
        self.phase = PRECOOL
        t_pre = c.clock()
        T = self._read()
        if cd["temp_test_precool"]:
            stalled = False
            if T is None or T > start_c:
                T, stalled = self._cool_loop(g, start_c)
            self.ev["pre"] = {"s": round(c.clock() - t_pre, 1), "T": None if T is None else round(T, 2), "stalled": stalled}
            if stalled:                                             # 시작 온도까지 못 식혔다 - 가열하지 않는다(자동 측정과 같은 규칙)
                raise _End("abort", "시작 온도 미도달 · 현재 %.1f °C" % T)

    def _heater_start(self, g):
        """가열 시작: 히트건 켜기(열선 → heater_start_gap_s → 송풍 · 측정 주체 규칙). 반환 (켠 시각, 켜기 직전 온도)."""
        c = self.c
        self._check(g)
        self.phase = HEAT
        T0 = self._read()
        if T0 is None:
            T0 = self.last_T
        if T0 is None:
            raise _End("abort", "온도 측정 실패 · 열전대 확인 필요")
        self._check(g)
        try:
            self.heater.on()
        except Rejected as e:
            self._check(g)
            raise _End("abort", "히트건 켜기 실패 · %s" % e)
        t_heat = c.clock()
        self.ev.update(t_heat=round(t_heat - self.t0, 3), T0=round(T0, 3))
        self._elog(t_heat, self.heater.pct)
        return t_heat, T0

    def _power(self, g, pct):
        try:
            self.heater.power(pct)
        except Rejected as e:
            self._check(g)
            raise _End("abort", "열선 출력 거절 · %s" % e)
        self._elog(self.c.clock(), self.heater.pct)

    def _run(self, g):
        """온도 시험: 예비 냉각 → 가열(목표선 · 출력 % 제어) → 냉각(같은 제어 또는 자연 냉각)."""
        c, cd = self.c, self.cond
        start_c, off_c, end_c = float(cd["run_start_c"]), float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"])
        self._coolers_on(g)
        self.feeding = True
        self._precool(g, start_c)
        trips0 = c.heater_trips
        t_heat, T0 = self._heater_start(g)
        if cd["run_heat_mode"] != "ramp" and int(cd["heater_max_pct"]) < 100:
            self._power(g, cd["heater_max_pct"])
        self._heat(g, t_heat, T0, off_c, trips0)
        if self.heater.element_on:
            self.heater.element(False)                              # 가열 끝: 열선 끔
        now = c.clock()
        self._elog(now, 0)
        self.ev["t_heat_end"] = round(now - self.t0, 3)
        self.sp = None
        ctl = None
        if cd["run_cool_mode"] == "ramp":                           # 냉각 속도 제어: 가열이 끝난 순간 온도에서 냉각 속도로 내려가는 목표선(같은 출력 % 제어)
            ctl = LineControl(self.heater, cd, self.last_T if self.last_T is not None else off_c, end_c, -1, now, config_mod.cool_rate_c_min(cd, IV),
                              T_amb=T0, on_change=self._elog)
        elif not cd["run_fan_during_cool"]:
            self.heater.off()                                       # 송풍은 열선 끈 뒤 heater_fan_after_s 에 꺼진다
        self.phase = COOL
        _T, stalled = self._cool_loop(g, end_c, ctl=ctl, trips0=c.heater_trips)
        if self.heater.element_on:
            self.heater.element(False)                              # 냉각 끝: 열선 끔(송풍은 정리가 heater_fan_after_s 뒤에 끈다)
            self._elog(c.clock(), 0)
        if stalled:
            self.notes.append("냉각 끝 온도 미도달")

    # ---- [히트건 맞추기] ----
    def _method(self, g, mode, ms):
        """펌웨어 출력 방식만 바꾼다(설정은 그대로 - 끝에 _cleanup 이 설정값으로 되돌린다)."""
        try:
            self.c.heater_method(mode, ms)
        except Rejected as e:
            self._check(g)
            raise _End("abort", "출력 방식 바꾸기 실패 · %s" % e)

    def _hold_for(self, g, trips0, secs=None, until=None, he=None):
        """기록만 하며 기다린다(출력은 그대로). secs 초가 지나거나 until() 이 참이면 끝. 반환 [(구간 경과 s, 온도)].
        he = 방식 이름이면 H-E 의심을 본다: 구간 시작 15 s 뒤부터 5 s 이동 평균의 최고값을 따라가다가 지금 이동 평균이 그보다 TUNE_HE_DROP_C 넘게 낮으면 중단."""
        c = self.c
        t1 = c.clock()
        rows = []
        top = None
        limit = float(self.cond["run_phase_limit_s"])
        while True:
            self._check(g)
            if c.heater_trips != trips0:
                raise _End("abort", c.heater_trip or "히트건 차단")
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            if T is not None:
                self._record(T, None)
                rows.append((now - t1, float(T)))
                if he is not None and now - t1 >= 15.0:
                    w = [v for t, v in rows if t >= rows[-1][0] - 5.0]
                    ma = sum(w) / len(w)
                    top = ma if top is None or ma > top else top
                    if top - ma > TUNE_HE_DROP_C:
                        if self.tune and self.tune["segs"]:
                            self.tune["segs"][-1]["he"] = True
                        raise _End("abort", "H-E 의심 · %s · 스테이션 화면 확인" % he)
            if secs is not None and now - t1 >= secs:
                return rows
            if until is not None and until():
                return rows
            if now - t1 > limit:
                raise _End("abort", "%s %g초 초과" % (PHASES[self.phase], limit))
            self._tick(t_it)

    @staticmethod
    def _seg_eval(rows):
        """앞 TUNE_SKIP_S 를 뺀 구간: 평균 온도 · 출렁임(직선 추세를 뺀 나머지의 최고 − 최저)."""
        r = [(t, v) for t, v in rows if t >= TUNE_SKIP_S]
        if len(r) < 3:
            return None, None
        n = len(r)
        mt, mv = sum(t for t, _ in r) / n, sum(v for _, v in r) / n
        den = sum((t - mt) ** 2 for t, _ in r)
        b = sum((t - mt) * (v - mv) for t, v in r) / den if den > 0 else 0.0
        res = [v - (mv + b * (t - mt)) for t, v in r]
        return round(mv, 3), round(max(res) - min(res), 3)

    @staticmethod
    def _last_mean(rows, span=5.0):
        if not rows:
            return None
        w = [v for t, v in rows if t >= rows[-1][0] - span]
        return sum(w) / len(w)

    def _run_tune(self, g):
        """[히트건 맞추기]: 예비 냉각 → (가장 안전한 방식으로) 히트건 켜기 → 출력 temp_step_pct → 켜기 유지 끝 → 안정 TUNE_SETTLE_S →
        방식마다 TUNE_SEG_S 비교(평균 · 출렁임 · H-E 의심) → 출렁임이 가장 작은 방식(0.05 °C 안이면 더 잘게 켜는 쪽) → 안정 20 s →
        계단: TUNE_LOW_PCT 로 내림 temp_step_s → 다시 temp_step_pct 로 올림 temp_step_s → 두 계단을 열 모델로 맞춘다. 저장은 끝까지 갔을 때만(_results)."""
        c, cd = self.c, self.cond
        up = float(cd["temp_step_pct"])
        low = float(TUNE_LOW_PCT)
        self.tune = {"segs": [], "pick": None, "steps": [], "model": None, "saved": False}
        self._coolers_on(g)
        self.feeding = True
        self._precool(g, float(cd["run_start_c"]))
        self._method(g, *TUNE_METHODS[0])                           # 켜는 동안은 가장 안전한 방식(몰아 켜기 5 s)
        trips0 = c.heater_trips
        self._heater_start(g)
        self._power(g, up)                                          # 켠 뒤 100 % 유지 중이면 끝날 때 적용(Controller)
        self.phase = SETTLE
        self.step_text = "스테이션 켜는 중"
        self._hold_for(g, trips0, until=lambda: c.boot_left() <= 0)
        self.step_text = "안정 · %d초" % TUNE_SETTLE_S
        self._hold_for(g, trips0, secs=TUNE_SETTLE_S)
        self.phase = COMPARE
        for i, (mode, ms) in enumerate(TUNE_METHODS):
            name = method_name(mode, ms)
            self._method(g, mode, ms)
            self.step_text = "비교 %d/%d · %s" % (i + 1, len(TUNE_METHODS), name)
            seg = {"mode": mode, "ms": ms, "name": name, "mean_c": None, "ripple_c": None, "he": False}
            self.tune["segs"].append(seg)
            rows = self._hold_for(g, trips0, secs=TUNE_SEG_S, he=name)
            seg["mean_c"], seg["ripple_c"] = self._seg_eval(rows)
        ok = [s for s in self.tune["segs"] if not s["he"] and s["ripple_c"] is not None]
        if not ok:
            raise _End("abort", "비교 기록 부족")
        lo_r = min(s["ripple_c"] for s in ok)
        pick = [s for s in ok if s["ripple_c"] <= lo_r + 0.05][-1]   # 0.05 °C 안이면 목록 뒤쪽(더 잘게 켜는 쪽)
        self.tune["pick"] = pick["name"]
        self._method(g, pick["mode"], pick["ms"])
        self.phase = SETTLE
        self.step_text = "안정 · %s" % pick["name"]
        rows = self._hold_for(g, trips0, secs=20.0)
        self.phase = STEP
        for a, b in ((up, low), (low, up)):
            base = self._last_mean(rows)
            self.step_text = "계단 %g → %g %%" % (a, b)
            self._power(g, b)
            rows = self._hold_for(g, trips0, secs=float(cd["temp_step_s"]))
            fit = heater_mod.fit_fopdt(rows, b - a, T0=base)
            st = {"from": a, "to": b, "K": None, "tau": None, "theta": None, "err": None}
            if fit is not None:
                st.update(K=round(fit[0], 5), tau=round(fit[1], 2), theta=round(fit[2], 2), err=round(fit[3], 3))
            self.tune["steps"].append(st)
        self.step_text = ""

    def _run_max(self, g):
        """[최대 속도 재기]: 예비 냉각 → 출력 최대로 가열 끝 온도까지 → 출력 0 %(쿨러 · 송풍)로 냉각 끝 온도까지(정체면 거기까지)."""
        c, cd = self.c, self.cond
        start_c, off_c, end_c = float(cd["run_start_c"]), float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"])
        limit = float(cd["run_phase_limit_s"])
        self._coolers_on(g)
        self.feeding = True
        self._precool(g, start_c)
        trips0 = c.heater_trips
        t_heat, _T0 = self._heater_start(g)
        if int(cd["heater_max_pct"]) < 100:
            self._power(g, cd["heater_max_pct"])
        while True:
            self._check(g)
            if c.heater_trips != trips0:
                raise _End("abort", c.heater_trip or "히트건 차단")
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            if T is not None:
                self._record(T, None)
                if T >= off_c:
                    self.ev["t_reach"] = round(now - self.t0, 3)
                    break
            if now - t_heat > limit:
                raise _End("abort", "가열 %g초 초과" % limit)
            self._tick(t_it)
        self._power(g, 0)
        self.ev["t_heat_end"] = round(c.clock() - self.t0, 3)
        self.phase = COOL
        _T, stalled = self._cool_loop(g, end_c)
        if stalled:
            self.notes.append("냉각 끝 온도 미도달 · 정체까지 기록")

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
        hold_s = float(cd["run_heat_hold_s"])
        limit = float(cd["run_phase_limit_s"])
        ctl = LineControl(self.heater, cd, T0, off_c, 1, t_heat, config_mod.heat_rate_c_min(cd, IV), T_amb=T0, on_change=self._elog) if ramp else None
        hold_t0 = None
        while True:
            self._check(g)
            if c.heater_trips != trips0:
                raise _End("abort", c.heater_trip or "히트건 차단")
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            sp = off_c if ctl is None else (ctl.target(T, now) if T is not None else ctl.sp)
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
                if ctl is not None:
                    try:
                        ctl.decide(T, sp, now)                      # 자동 측정과 같은 출력 % 제어
                    except Rejected as e:
                        self._check(g)
                        raise _End("abort", "열선 출력 거절 · %s" % e)
            if now - t_heat > limit:
                raise _End("abort", "가열 %g초 초과" % limit)
            self._tick(t_it)

    def _cool_loop(self, g, target, ctl=None, trips0=None):
        """예비 냉각 · 냉각: 온도 ≤ target 이거나 정체(run_stall_s 동안 run_stall_c 도 안 내려감 - heater.Stall)까지. (마지막 온도, 정체였나).
        ctl = 냉각 속도 제어(LineControl). 안전 규칙이 열선을 끄거나 출력을 거절하면 중단(온도 시험 규칙 - 가열과 같다)."""
        c, cd = self.c, self.cond
        limit = float(cd["run_phase_limit_s"])
        stall = Stall(cd["run_stall_s"], cd["run_stall_c"])
        t_start = c.clock()
        T = None
        while True:
            self._check(g)
            if ctl is not None and c.heater_trips != trips0:
                raise _End("abort", c.heater_trip or "히트건 차단")
            t_it = c.clock()
            T = self._read()
            now = c.clock()
            if T is not None:
                sp = ctl.target(T, now) if ctl is not None else None
                self.sp = sp
                self._record(T, sp)
                if ctl is not None and c.heater_trips != trips0:
                    raise _End("abort", c.heater_trip or "히트건 차단")
                if T <= target:
                    return T, False
                if stall.update(T, now):
                    return T, True
                if ctl is not None:
                    try:
                        ctl.decide(T, sp, now)
                    except Rejected as e:
                        self._check(g)
                        raise _End("abort", "열선 출력 거절 · %s" % e)
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
        if c.stage.connected and c.stage.heat_capable:
            try:
                c.heater_method_restore()                           # 맞추기가 바꾼 출력 방식을 설정값으로 되돌린다
            except Rejected as e:
                c.logs.app("온도 시험 뒤 출력 방식 되돌리기 실패 · %s" % e, "warn")
        if self.ev.get("t_heat") is not None and self.ev.get("t_heat_end") is None:
            self.ev["t_heat_end"] = round(c.clock() - self.t0, 3)       # 가열 중에 끝났다 - 넘침 · 켜진 시간은 여기까지
        if self.elog and self.elog[-1][1]:
            self.elog.append([round(c.clock() - self.t0, 3), 0])
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
        cfg = c.cfg.values
        sp_max = c.calib.d.get("speed_max") or {}
        return {"active": self._busy, "id": self.started, "mode": self.mode, "mode_text": MODES.get(self.mode, ""),
                "phase": None if self.phase is None else PHASES[self.phase],
                "t": None if self.t0 is None or not self._busy else round(c.clock() - self.t0, 1),
                "T": None if self.last_T is None else round(self.last_T, 2), "sp": None if self.sp is None else round(self.sp, 2),
                "element": ht.get("element"), "fan": ht.get("fan"), "pct": ht.get("pct"),
                "coolers": [n for n in self.coolers if c._relay_is_on(n)] if self._busy else [],
                "result": self.result, "notes": list(self.notes), "folder": None if self.folder is None else str(self.folder),
                "cond": dict(self.cond), "n": len(self.pts),
                "summary": self._summary(),
                "expect": config_mod.expect(cfg, IV),                                   # 지금 설정의 예상 점 수 · 시간(시작 전)
                "model_text": heater_mod.ThermalModel(cfg).text(), "model_measured": heater_mod.ThermalModel(cfg).measured,
                "speed_max": sp_max.get("rows"), "speed_t": sp_max.get("t"),
                "step_text": self.step_text if self._busy else "", "tune": self.tune if self.mode == "tune" else None}

    def publish(self):
        self.c.state.set("temptest", self.view())
