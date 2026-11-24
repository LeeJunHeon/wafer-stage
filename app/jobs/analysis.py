"""analysis.py - 온도–저항 곡선의 요약: 시작 온도 저항 · 최고 온도 저항 · 저항 비 · 가열/냉각 전이 온도.

전이 온도 = log10(저항)을 온도로 미분한 값의 크기가 가장 큰 온도(가열 · 냉각 따로, 5점 이동 평균 뒤).
점은 (온도 °C, 저항 Ω, 단계) - 단계 0 = 가열, 1 = 냉각.
"""

import math

import numpy as np

SMOOTH_N = 5
RIPPLE_WIN_S = 30.0                 # 흔들림 기준선: 점마다 앞뒤 이 시간의 절반 안 점들로 맞춘 직선
RIPPLE_MIN_N, RIPPLE_MIN_S = 10, 20.0   # 이보다 점이 적거나 구간이 짧으면 흔들림을 내지 않는다
RIPPLE_P_MIN, RIPPLE_P_MAX = 2.0, 30.0  # 가장 센 주기를 훑는 범위(아래는 읽기 간격 중앙값 × 3 이상 · 위는 구간 길이 이하)


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


def period_scan(t, r, p_min, p_max, step=0.2):
    """가장 센 주기: p_min 부터 step 간격으로 p_max 까지 주기마다 r ≈ a cos(2πt/P) + b sin(2πt/P) 의 최소제곱 진폭 √(a² + b²) 을 구해
    가장 큰 것 → (주기 s, 진폭 °C). 훑을 주기가 없거나 점이 2개 미만이면 (None, None). [박자 시험] · 흔들림(ripple)이 같이 쓴다."""
    t = np.asarray(t, float)
    r = np.asarray(r, float)
    if len(t) < 2:
        return None, None
    best = None
    k = 0
    while True:
        P = p_min + k * step
        if P > p_max + 1e-9:
            break
        k += 1
        wv = 2.0 * math.pi / P
        X = np.column_stack([np.cos(wv * t), np.sin(wv * t)])
        ab = np.linalg.lstsq(X, r, rcond=None)[0]
        amp = float(math.hypot(ab[0], ab[1]))
        if best is None or amp > best[1]:
            best = (P, amp)
    return best if best is not None else (None, None)


def ripple(rows, win_s=RIPPLE_WIN_S):
    """한 단계의 온도 흔들림: rows = [(경과 s, 온도)] (기록 순서). 기준선 = 점마다 앞뒤 win_s ÷ 2 안 점들로 맞춘 직선의 그 점 값
    (끝에서는 있는 쪽 점만 · 창 안 점이 3개 미만이면 그 점은 뺀다). 나머지 = 온도 − 기준선 →
    {"sd" 평균 제곱근, "pp" 최고 − 최저, "period_s" · "amp" 가장 센 주기 · 진폭(period_scan - max(2 s, 읽기 간격 중앙값 × 3) ~ min(30 s, 구간 길이))}.
    점이 RIPPLE_MIN_N 개 미만이거나 구간이 RIPPLE_MIN_S 미만이면 네 값 모두 None."""
    out = {"sd": None, "pp": None, "period_s": None, "amp": None}
    if len(rows) < RIPPLE_MIN_N:
        return out
    t = np.array([float(a[0]) for a in rows])
    v = np.array([float(a[1]) for a in rows])
    span = float(t[-1] - t[0])
    if span < RIPPLE_MIN_S:
        return out
    tz = t - t[0]                                                   # 합이 커지지 않게 첫 점 기준
    half = win_s / 2.0
    lo = np.searchsorted(tz, tz - half, side="left")
    hi = np.searchsorted(tz, tz + half, side="right")
    cs = lambda x: np.concatenate([[0.0], np.cumsum(x)])           # noqa: E731
    S1, St, Sv, Stt, Stv = cs(np.ones_like(tz)), cs(tz), cs(v), cs(tz * tz), cs(tz * v)
    n = S1[hi] - S1[lo]
    st, sv, stt, stv = St[hi] - St[lo], Sv[hi] - Sv[lo], Stt[hi] - Stt[lo], Stv[hi] - Stv[lo]
    keep = n >= 3
    den = n * stt - st * st
    with np.errstate(divide="ignore", invalid="ignore"):
        b = np.where(np.abs(den) > 1e-12, (n * stv - st * sv) / den, 0.0)
        a = (sv - b * st) / n
    r = (v - (a + b * tz))[keep]
    tk = t[keep]
    if len(r) == 0:
        return out
    out["sd"] = float(np.sqrt(np.mean(r * r)))
    out["pp"] = float(r.max() - r.min())
    dts = np.diff(t)
    dt_med = float(np.median(dts)) if len(dts) else 0.0
    out["period_s"], out["amp"] = period_scan(tk, r, max(RIPPLE_P_MIN, dt_med * 3.0), min(RIPPLE_P_MAX, span))
    return out


def curve_stats(rows, lo, hi, sign, reversal_c):
    """가열(sign +1) 또는 냉각(sign −1) 한 단계의 결과: rows = [(경과 s, 온도, 목표 | None)] (기록 순서).
    점 수 = lo ≤ 온도 ≤ hi 인 점(시작~끝 온도 구간) · 실제 평균 속도(°C/분 - 그 구간 첫 점 → 끝 점) · 점 간격 평균 · 흔들림(표준편차) ·
    역전(가열 중 내려감 · 냉각 중 올라감이 reversal_c 넘는 일)의 횟수 · 최대 폭 · 자리(그 점의 경과 · 온도) · 목표선과의 차이(평균 · 최대 - 절댓값) ·
    흔들림(ripple - 같은 구간의 점 · rip_sd · rip_pp · rip_period_s · rip_amp)."""
    pts = [r for r in rows if lo - 1e-9 <= r[1] <= hi + 1e-9]
    out = {"n": len(pts), "rate_c_min": None, "step_mean": None, "step_sd": None, "rev_n": 0, "rev_max": 0.0, "rev_at": [],
           "sp_err_mean": None, "sp_err_max": None}
    rp = ripple([(r[0], r[1]) for r in pts])
    r3 = lambda x, d=3: None if x is None else round(x, d)         # noqa: E731
    out.update(rip_sd=r3(rp["sd"]), rip_pp=r3(rp["pp"]), rip_period_s=r3(rp["period_s"], 1), rip_amp=r3(rp["amp"]))
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


def hold_summary(rows, open_a=None, n_miss=0):
    """기록 모드(히트건 사용 끔 - 일정 시간 그 자리 온도에서 저항 기록)의 요약. rows = [(경과 s, 온도 °C, 저항 Ω, 전류 A | None)] 기록한 점.
    반환 {n, dur_s, r_mean, r_sd_pct(평균 대비 %), r_min, r_max, drift_pct_min(선형 맞춤 기울기 ÷ 평균 · 1 분당 %), t_mean, t_min, t_max,
    n_open(전류 < open_a 인 점), n_miss(못 쓴 읽기 - 전류 한도 걸림 · 값 없음)}. 점이 없으면 n 0 과 None 들."""
    pts = [(float(t), None if T is None else float(T), float(R), cur) for t, T, R, cur in rows if R is not None and R > 0]
    out = {"n": len(pts), "dur_s": None, "r_mean": None, "r_sd_pct": None, "r_min": None, "r_max": None, "drift_pct_min": None,
           "t_mean": None, "t_min": None, "t_max": None, "n_open": 0, "n_miss": int(n_miss)}
    if not pts:
        return out
    t = np.array([p[0] for p in pts])
    r = np.array([p[2] for p in pts])
    m = float(r.mean())
    out.update(dur_s=round(float(t[-1] - t[0]), 2), r_mean=m, r_sd_pct=float(r.std() / m * 100.0) if m else None, r_min=float(r.min()), r_max=float(r.max()))
    if len(pts) >= 2 and float(t[-1] - t[0]) > 0:
        slope = float(np.polyfit(t / 60.0, r, 1)[0])                # Ω / 분
        d = round(slope / m * 100.0, 6) if m else None             # 반올림해 0 이면 0.0(음수 0 "-0" 이 summary.csv · 화면에 나오지 않게)
        out["drift_pct_min"] = (d + 0.0) if d is not None and d != 0 else (0.0 if d is not None else None)
    temps = [p[1] for p in pts if p[1] is not None]
    if temps:
        out.update(t_mean=float(sum(temps) / len(temps)), t_min=float(min(temps)), t_max=float(max(temps)))
    if open_a is not None:
        out["n_open"] = sum(1 for p in pts if p[3] is not None and abs(float(p[3])) < float(open_a))
    return out
