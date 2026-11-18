"""heater.py - 가열 장치(송풍 heater_fan_relay · 열선 heater_element_relay 히트건)와 열선 출력 % 제어.

측정(app/jobs/run.py)과 온도 시험(app/jobs/temptest.py)은 RelayHeater 의 on · off · power · element · is_on · element_on · fan_on 만 쓴다
- 가열 하드웨어가 바뀌면 여기만 고친다. 켜고 끄는 순서(열선 → 송풍 · 열선 끔 → 송풍은 뒤에)와 안전 규칙은 Controller 의 히트건 함수
(heater_on · heater_power · heater_element · heater_off)가 한 곳에서 지킨다. 여기서는 "측정" 주체로 부르기만 한다.

열선 출력 %(펌웨어 V13 hp - 열선 SSR 을 주기마다 주기 × % 동안만 켠다):
  예전 켜기/끄기(최소 2 s) 제어는 한 번 켤 때마다 3~4 s 만에 +10~40 °C 튀고 40~70 s 걸려 내려와 톱니가 됐다. 그래서 출력 % 를 계산하는 제어기 하나
  (PowerControl - 목표선 기준 PI + 앞먹임 + 적분 포화 방지 + 0 ~ heater_max_pct 제한)를 가열 · 냉각이 같이 쓴다(냉각은 목표선이 내려갈 뿐).
  계수는 손으로 맞추지 않는다: 열 모델(1차 + 지연: 이득 K °C/% · 시간 상수 τ · 지연 θ - ThermalModel)에서 SIMC 규칙으로 계산한다.
  앞먹임 = 그 온도를 유지하는 출력((목표 − 시작 주위 온도) ÷ K) + 목표선 기울기만큼의 출력(τ × 기울기 ÷ K). 목표 온도 · 속도를 바꿔도 같은 값으로 동작한다.
  열 모델은 온도 시험 [반응 재기](fit_fopdt)로 잰다. 없으면 보수적인 기본값(DEFAULT_MODEL - 이득을 크게 · 지연을 길게 봐서 계수가 작다).
목표선(LineControl): 가열 = 시작 온도 + 속도 × 경과(상한 가열 끝 온도) · 냉각 = 가열 끝 순간 온도 − 속도 × 경과(하한 냉각 끝 온도).
  기다림(heater_lead_c): 순간값이 아니라 걸러진 온도(heater_lead_filter_s 평균)가 lead 넘게 뒤처진 채 heater_lead_hold_s 이상 이어질 때만 목표선을 세운다
  (냉각을 늦추려 켠 열선이 순간 온도를 목표선 위로 튀게 해 목표선이 섰던 일 - 냉각이 11 °C/분에 그쳤다).
속도(점 수 기준): 가열 · 냉각 속도 = 목표 점 간격 ÷ 읽기 간격 × 60(config.heat_rate_c_min · cool_rate_c_min).
최대 속도(speed_table): 온도 시험 [최대 속도 재기]의 기록에서 5 °C 구간마다 가장 빠른 가열 · 냉각 속도. 설정 속도가 더 빠르면 시작 때 주의(speed_warnings).
"""

import math
from collections import deque

from app.safety import BY_RUN

DEFAULT_MODEL = (1.2, 40.0, 10.0)                   # 열 모델이 없을 때(K °C/% · τ s · θ s): 이득을 크게 · 지연을 길게 - PI 계수가 작아 보수적이다
SPEED_BAND_C = 5.0                                  # 최대 속도 표의 온도 구간
SPEED_WIN_S = 4.0                                   # 최대 속도: 이 시간 창의 직선 기울기를 그 순간 속도로 본다(한 점 잡음을 줄인다)


def ramp_setpoint(t_start_c, rate_c_min, elapsed_s, top_c):
    """가열 목표 온도(°C): 시작 온도 + 속도 × 경과, 가열 끝 온도를 넘지 않는다."""
    return min(float(top_c), float(t_start_c) + float(rate_c_min) * max(0.0, float(elapsed_s)) / 60.0)


class ThermalModel:
    """열 모델(1차 + 지연): τ dT/dt = −(T − T_주위) + K · u(t − θ). cd 의 heat_model_k · tau · theta(없으면 DEFAULT_MODEL)."""

    def __init__(self, cd):
        k, tau, th = cd.get("heat_model_k"), cd.get("heat_model_tau_s"), cd.get("heat_model_theta_s")
        self.measured = k is not None and tau is not None and th is not None
        d = DEFAULT_MODEL
        self.K = float(k) if k is not None else d[0]
        self.tau = float(tau) if tau is not None else d[1]
        self.theta = float(th) if th is not None else d[2]

    def gains(self):
        """SIMC PI: τc = max(θ, 1 s) · Kc = τ ÷ (K (τc + θ)) [%/°C] · Ti = min(τ, 4 (τc + θ)) [s]."""
        tc = max(self.theta, 1.0)
        kc = self.tau / (self.K * (tc + self.theta))
        ti = min(self.tau, 4.0 * (tc + self.theta))
        return kc, max(1.0, ti)

    def text(self):
        kc, ti = self.gains()
        return "K %.3f °C/%% · τ %.1f s · θ %.1f s · Kc %.2f %%/°C · Ti %.0f s%s" % (self.K, self.tau, self.theta, kc, ti, "" if self.measured else " · 기본값")


class PowerControl:
    """열선 출력 % = 앞먹임 + Kc (e + ∫e dt ÷ Ti), 0 ~ u_max 로 자른다. 출력이 한계에 걸린 채 오차가 그쪽으로 커지면 적분하지 않는다(포화 방지)."""

    def __init__(self, cd, T_amb):
        self.m = ThermalModel(cd)
        self.kc, self.ti = self.m.gains()
        self.umax = float(cd.get("heater_max_pct", 100))
        self.T_amb = float(T_amb)
        self.I = 0.0
        self.t = None
        self.u = 0.0

    def ff(self, sp, slope_c_s):
        """앞먹임: 목표 온도를 유지하는 출력 + 목표선 기울기만큼의 출력."""
        return ((float(sp) - self.T_amb) + self.m.tau * float(slope_c_s)) / self.m.K

    def out(self, T, sp, slope_c_s, now):
        dt = 0.0 if self.t is None else max(0.0, now - self.t)
        self.t = now
        e = float(sp) - float(T)
        ff = self.ff(sp, slope_c_s)
        raw = ff + self.kc * (e + self.I / self.ti)
        sat_hi, sat_lo = raw > self.umax, raw < 0.0
        if not ((sat_hi and e > 0) or (sat_lo and e < 0)):
            self.I += e * dt
            raw = ff + self.kc * (e + self.I / self.ti)
        self.u = max(0.0, min(self.umax, raw))
        return self.u


class LineControl:
    """목표선 + 열선 출력(가열 또는 냉각 한 단계). sign = +1 가열(상한 end_c) · −1 냉각(하한 end_c).
    목표선은 경과 시간이 아니라 회차마다 앞으로 간다: 걸러진 온도가 lead 넘게 뒤처진 채 hold 초 이상이면 그 회차는 기다린다.
    step() 이 목표를 정하고 출력 % 를 계산해 heater.power 로 보낸다(1 % 넘게 바뀔 때만 - 거절은 Rejected 그대로 올라간다)."""

    def __init__(self, heater, cd, T_from, end_c, sign, now, rate_c_min, T_amb=None, on_change=None):
        self.h = heater
        self.sign = 1 if sign > 0 else -1
        self.rate = float(rate_c_min)
        self.end = float(end_c)
        self.lead = float(cd["heater_lead_c"])
        self.win = float(cd.get("heater_lead_filter_s", 0.0))
        self.hold = float(cd.get("heater_lead_hold_s", 0.0))
        self.sp = self._clamp(float(T_from))
        self.t = now
        self.waiting = False                                       # 이번 회차에 목표선이 기다렸다
        self.waits = 0                                             # 기다린 회차 수(기록용)
        self.slope = 0.0                                           # 이번 회차 목표선 기울기(°C/s - 앞먹임)
        self._buf = deque()
        self._behind_t = None
        self.pc = PowerControl(cd, float(T_from) if T_amb is None else T_amb)
        self.pct = None                                            # 마지막으로 보낸 출력 %
        self._held = False                                         # 스테이션 켠 뒤 100 % 유지 중이었다(끝나면 지금 온도에서 다시 시작)
        self.on_change = on_change                                 # on_change(now, pct) - 출력을 바꿀 때(기록)

    def _clamp(self, v):
        return min(self.end, v) if self.sign > 0 else max(self.end, v)

    def filtered(self, T, now):
        """걸러진 온도: 최근 heater_lead_filter_s 초 평균(0 이면 순간값)."""
        self._buf.append((now, float(T)))
        while self._buf and now - self._buf[0][0] > self.win:
            self._buf.popleft()
        return sum(v for _t, v in self._buf) / len(self._buf)

    def target(self, T, now):
        """이번 회차의 목표 온도. 걸러진 온도가 lead 넘게 뒤처진 채 hold 초 넘게 이어졌으면 목표선을 그 자리에 둔다."""
        dt = max(0.0, now - self.t)
        self.t = now
        Tf = self.filtered(T, now)
        if self._held and self.h.boot_hold <= 0:                   # 스테이션 켜기 유지가 끝났다: 목표선을 지금 온도에서 다시 시작(앞서 가 있으면) · 적분 0
            self._held = False
            self.sp = self._clamp(max(self.sp, float(T)) if self.sign > 0 else min(self.sp, float(T)))
            self.pc.I, self.pc.t = 0.0, now
            self._behind_t = None
        behind = (Tf < self.sp - self.lead) if self.sign > 0 else (Tf > self.sp + self.lead)
        if behind:
            if self._behind_t is None:
                self._behind_t = now
        else:
            self._behind_t = None
        self.waiting = behind and now - self._behind_t >= self.hold
        if self.waiting:
            self.waits += 1
            self.slope = 0.0
        else:
            self.sp = self._clamp(self.sp + self.sign * self.rate * dt / 60.0)
            self.slope = 0.0 if self.sp == self.end else self.sign * self.rate / 60.0     # 끝 온도에 닿으면 앞먹임은 유지 출력만
        return self.sp

    def decide(self, T, sp, now):
        """정한 목표로 출력 % 를 계산해 보낸다. 바꿨으면 True.
        스테이션 켠 뒤 100 % 유지 중에는 적분하지 않고(앞먹임 값만 요청 - Controller 가 기억했다가 유지가 끝나면 적용) 끝나면 target 이 지금 온도에서 다시 시작한다."""
        if self.h.boot_hold > 0:
            self._held = True
            self.pc.t = now
            self.pc.I = 0.0
            pct = int(round(max(0.0, min(self.pc.umax, self.pc.ff(sp, self.slope)))))
            if pct != self.pct:
                self.h.power(pct)
                self.pct = pct
            return False
        pct = int(round(self.pc.out(T, sp, self.slope, now)))
        if self.pct is not None and pct == self.pct and self.h.pct == pct:     # 실제 출력(펌웨어가 알려 준 값)과도 같을 때만 건너뛴다 - [정지] '!' 가 0 으로 만들었으면 다시 보낸다
            return False
        self.h.power(pct)
        self.pct = pct
        if self.on_change is not None:
            self.on_change(now, pct)
        return True

    def step(self, T, now):
        sp = self.target(T, now)
        self.decide(T, sp, now)
        return sp


class Stall:
    """냉각 정체 판정(예비 냉각 · 냉각): span 초 동안 step °C 도 안 내려가면 정체. 자동 측정 · 온도 시험이 같이 쓴다."""

    def __init__(self, span_s, step_c):
        self.span, self.step = float(span_s), float(step_c)
        self.best = self.best_t = None

    def update(self, T, now) -> bool:
        if self.best is None or T < self.best - self.step:
            self.best, self.best_t = T, now
            return False
        return now - self.best_t >= self.span


# ----------------------------------------------------------------------
# 열 모델 맞추기 · 최대 속도 표 · 속도 주의
# ----------------------------------------------------------------------
def fit_fopdt(rows, u_pct):
    """[반응 재기]의 출력 단계 기록 rows = [(출력을 올린 뒤 경과 s, 온도)] (출력 u_pct 를 유지한 구간) → (K °C/%, τ s, θ s, 맞춤 오차 °C) 또는 None.
    T(t) = T0 + K u (1 − exp(−(t − θ)/τ)) (t > θ). θ · τ 는 격자에서 고르고 K 는 그때마다 최소제곱으로 바로 푼다."""
    if len(rows) < 8 or u_pct <= 0:
        return None
    t0 = rows[0][0]
    T0 = rows[0][1]
    ts = [t - t0 for t, _ in rows]
    ys = [T - T0 for _, T in rows]
    if max(ys) < 1.0:
        return None
    best = None
    span = ts[-1]
    thetas = [x * 0.5 for x in range(0, int(min(60.0, span / 2) / 0.5) + 1)]
    taus = [5.0 * (1.08 ** i) for i in range(0, 80) if 5.0 * (1.08 ** i) <= 3600.0]
    for th in thetas:
        for tau in taus:
            f = [0.0 if t <= th else 1.0 - math.exp(-(t - th) / tau) for t in ts]
            ff = sum(v * v for v in f)
            if ff <= 1e-9:
                continue
            A = sum(a * b for a, b in zip(f, ys)) / ff
            sse = sum((y - A * v) ** 2 for y, v in zip(ys, f))
            if best is None or sse < best[0]:
                best = (sse, A, tau, th)
    if best is None or best[1] <= 0:
        return None
    sse, A, tau, th = best
    return A / float(u_pct), tau, th, math.sqrt(sse / len(ys))


def _slopes(seq, win=SPEED_WIN_S):
    """(t, T) 열 → [(T, 속도 °C/분)]: 각 점 앞 win 초 창의 직선 기울기."""
    out = []
    j = 0
    for i in range(len(seq)):
        while seq[i][0] - seq[j][0] > win:
            j += 1
        pts = seq[j:i + 1]
        if len(pts) < 3 or pts[-1][0] - pts[0][0] < win * 0.5:
            continue
        mt = sum(p[0] for p in pts) / len(pts)
        mT = sum(p[1] for p in pts) / len(pts)
        den = sum((p[0] - mt) ** 2 for p in pts)
        if den <= 0:
            continue
        out.append((seq[i][1], sum((p[0] - mt) * (p[1] - mT) for p in pts) / den * 60.0))
    return out


def speed_table(heat, cool, band=SPEED_BAND_C):
    """[최대 속도 재기] 기록(가열 · 냉각 각각 (t, T) 열) → [[구간 아래 °C, 최대 가열 °C/분 | None, 최대 냉각 °C/분 | None], …](구간 band °C)."""
    rows = {}
    for seq, col, sign in ((heat, 1, 1), (cool, 2, -1)):
        for T, r in _slopes(seq):
            lo = math.floor(T / band) * band
            v = r * sign
            if v <= 0:
                continue
            cur = rows.setdefault(lo, [lo, None, None])
            if cur[col] is None or v > cur[col]:
                cur[col] = v
    return [[r[0], None if r[1] is None else round(r[1], 2), None if r[2] is None else round(r[2], 2)] for _lo, r in sorted(rows.items())]


def speed_warnings(table, heat_rate, cool_rate, start_c, off_c, end_c, band=SPEED_BAND_C):
    """설정 속도가 최대 속도 표의 그 구간 최대보다 빠른 곳 → 주의 글 목록(막지 않는다)."""
    out = []
    if not table:
        return out
    for lo, hmax, cmax in table:
        hi = lo + band
        if hmax is not None and hi > start_c and lo < off_c and heat_rate > hmax:
            out.append("가열 %.1f °C/분 · %g~%g °C 최대 %.1f °C/분" % (heat_rate, lo, hi, hmax))
        if cmax is not None and hi > end_c and lo < off_c and cool_rate > cmax:
            out.append("냉각 %.1f °C/분 · %g~%g °C 최대 %.1f °C/분" % (cool_rate, lo, hi, cmax))
    return out


class RelayHeater:
    def __init__(self, ctrl):
        self.c = ctrl

    @property
    def is_on(self):
        """송풍 또는 열선이 켜짐(X · Y 이동 거절의 기준)."""
        return self.c._heater_on()

    @property
    def element_on(self):
        return self.c._element_on()

    @property
    def fan_on(self):
        return self.c._fan_on()

    @property
    def pct(self):
        """지금 열선 출력 %(펌웨어가 알려 준 값 · 모르면 열선 상태로 0 · 100)."""
        return self.c.heater_pct()

    @property
    def boot_hold(self):
        """스테이션 켠 뒤 100 % 유지가 남은 초(0 = 유지 아님)."""
        return self.c.boot_left()

    @property
    def fan_pending(self):
        """열선은 꺼졌고 송풍을 늦게 끄려고 기다리는 중."""
        return self.c.heater_fan_pending()

    def on(self):
        """송풍이 꺼져 있으면 켜기 순서대로(열선 → 대기 → 송풍), 켜져 있으면 열선만."""
        self.c.heater_on(BY_RUN)

    def power(self, pct):
        """송풍이 켜진 동안 열선 출력 %(0 = 열선 끔)."""
        self.c.heater_power(pct, BY_RUN)

    def element(self, on):
        """송풍이 켜진 동안 열선만 켜고 끈다(켬 = 100 %)."""
        self.c.heater_element(bool(on), BY_RUN)

    def off(self):
        """열선 끔 → 송풍은 열선을 끈 때부터 heater_fan_after_s 뒤에 끈다."""
        self.c.heater_off(BY_RUN)
