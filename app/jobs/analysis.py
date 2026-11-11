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


def analyze(points):
    """[(온도, 저항, 단계)] → {"r_start", "r_peak", "ratio", "t_heat", "t_cool", "hyst", "t_max", "n"}. 없는 값은 None."""
    pts = [(p[0], p[1], int(p[2]) if len(p) > 2 else 0) for p in points if p[0] is not None and p[1] is not None and p[1] > 0]
    out = {"r_start": None, "r_peak": None, "ratio": None, "t_heat": None, "t_cool": None, "hyst": None, "t_max": None, "n": len(pts)}
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
