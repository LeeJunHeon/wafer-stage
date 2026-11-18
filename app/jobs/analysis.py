"""analysis.py - 온도–저항 곡선의 요약: 시작 온도 저항 · 최고 온도 저항 · 저항 비 · 가열/냉각 전이 온도.

전이 온도 = log10(저항)을 온도로 미분한 값의 크기가 가장 큰 온도(가열 · 냉각 따로, 5점 이동 평균 뒤).
점은 (온도 °C, 저항 Ω, 단계) - 단계 0 = 가열, 1 = 냉각.
"""

import math

SMOOTH_N = 5


def _smooth(v, n=SMOOTH_N):
    if len(v) < n:
        return list(v)
    h = n // 2
    out = []
    for i in range(len(v)):
        a, b = max(0, i - h), min(len(v), i + h + 1)
        out.append(sum(v[a:b]) / (b - a))
    return out


def transition_temp(points):
    """한 방향(가열 또는 냉각)의 점들 [(온도, 저항), …] → 전이 온도(°C). 점이 모자라거나 온도가 안 변하면 None."""
    pts = [(float(t), math.log10(r)) for t, r in points if r is not None and r > 0 and t is not None]
    if len(pts) < SMOOTH_N + 2:
        return None
    ts, ls = _smooth([p[0] for p in pts]), _smooth([p[1] for p in pts])
    best, best_t = 0.0, None
    for i in range(1, len(ts)):
        dt = ts[i] - ts[i - 1]
        if abs(dt) < 0.02:                            # 온도가 거의 안 변한 구간(끝에서 머무는 점)은 미분하지 않는다
            continue
        d = abs((ls[i] - ls[i - 1]) / dt)
        if d > best:
            best, best_t = d, 0.5 * (ts[i] + ts[i - 1])
    return None if best_t is None else round(best_t, 2)


def spacing(temps):
    """한 방향(가열 또는 냉각) 점들의 온도 → {"n" 점 수, "dc" 평균 몇 °C 마다 1점}. dc = (최고 − 최저) ÷ (점 수 − 1). 점이 2개 미만이면 dc None.
    자동 측정(run.json 요약 · 측정 화면 · 측정 결과 탭)과 온도 시험(summary.txt · 창)이 같이 쓴다."""
    ts = [float(t) for t in temps if t is not None]
    n = len(ts)
    return {"n": n, "dc": None if n < 2 else round((max(ts) - min(ts)) / (n - 1), 3)}


def point_spacing(points):
    """[(온도, 저항, 단계)] → {"heat": spacing, "cool": spacing}."""
    return {"heat": spacing([p[0] for p in points if (int(p[2]) if len(p) > 2 else 0) == 0]),
            "cool": spacing([p[0] for p in points if len(p) > 2 and int(p[2]) == 1])}


def analyze(points):
    """[(온도, 저항, 단계)] → {"r_start", "r_peak", "ratio", "t_heat", "t_cool", "hyst", "t_max", "n"}. 없는 값은 None."""
    pts = [(p[0], p[1], int(p[2]) if len(p) > 2 else 0) for p in points if p[0] is not None and p[1] is not None and p[1] > 0]
    out = {"r_start": None, "r_peak": None, "ratio": None, "t_heat": None, "t_cool": None, "hyst": None, "t_max": None, "n": len(pts)}
    out["spacing"] = point_spacing(pts)                # 점 간격(가열 · 냉각) - 몇 °C 마다 1점
    if not pts:
        return out
    k = max(range(len(pts)), key=lambda i: pts[i][0])
    out["r_start"] = pts[0][1]                         # 시작 온도에서의 저항(첫 점)
    out["r_peak"] = pts[k][1]                          # 최고 온도에서의 저항
    out["t_max"] = round(pts[k][0], 2)
    if out["r_peak"]:
        out["ratio"] = out["r_start"] / out["r_peak"]
    heat = [(t, r) for t, r, ph in pts if ph == 0]
    cool = [(t, r) for t, r, ph in pts if ph == 1]
    out["t_heat"] = transition_temp(heat)
    out["t_cool"] = transition_temp(cool)
    if out["t_heat"] is not None and out["t_cool"] is not None:
        out["hyst"] = round(out["t_heat"] - out["t_cool"], 2)
    return out


def curve_stats(rows, lo, hi, sign, reversal_c):
    """가열(sign +1) 또는 냉각(sign −1) 한 단계의 결과: rows = [(경과 s, 온도, 목표 | None)] (기록 순서).
    점 수 = lo ≤ 온도 ≤ hi 인 점(시작~끝 온도 구간) · 실제 평균 속도(°C/분 - 그 구간 첫 점 → 끝 점) · 점 간격 평균 · 흔들림(표준편차) ·
    역전(가열 중 내려감 · 냉각 중 올라감이 reversal_c 넘는 일)의 횟수 · 최대 폭 · 자리(그 점의 경과 · 온도) · 목표선과의 차이(평균 · 최대 - 절댓값)."""
    pts = [r for r in rows if lo - 1e-9 <= r[1] <= hi + 1e-9]
    out = {"n": len(pts), "rate_c_min": None, "step_mean": None, "step_sd": None, "rev_n": 0, "rev_max": 0.0, "rev_at": [],
           "sp_err_mean": None, "sp_err_max": None}
    if len(pts) >= 2:
        dt = pts[-1][0] - pts[0][0]
        if dt > 0:
            out["rate_c_min"] = round(sign * (pts[-1][1] - pts[0][1]) / dt * 60.0, 3)
        steps = [sign * (b[1] - a[1]) for a, b in zip(pts, pts[1:])]
        m = sum(steps) / len(steps)
        out["step_mean"] = round(m, 4)
        out["step_sd"] = round(math.sqrt(sum((s - m) ** 2 for s in steps) / len(steps)), 4)
    best, in_rev = None, False
    for r in rows:
        v = sign * r[1]
        if best is None or v > best:
            best, in_rev = v, False
            continue
        drop = best - v
        if drop > reversal_c:
            if not in_rev:
                out["rev_n"] += 1
                out["rev_at"].append([round(r[0], 2), round(r[1], 3)])
                in_rev = True
            out["rev_max"] = round(max(out["rev_max"], drop), 3)
    err = [abs(r[1] - r[2]) for r in pts if r[2] is not None]
    if err:
        out["sp_err_mean"] = round(sum(err) / len(err), 3)
        out["sp_err_max"] = round(max(err), 3)
    return out
