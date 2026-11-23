"""heater.py - 가열 장치(송풍 heater_fan_relay · 열선 heater_element_relay 히트건)와 열선 출력 % 제어.

측정(app/jobs/run.py)과 온도 시험(app/jobs/temptest.py)은 RelayHeater 의 on · off · power · element · is_on · element_on · fan_on 만 쓴다
- 가열 하드웨어가 바뀌면 여기만 고친다. 켜고 끄는 순서(열선 → 송풍 · 열선 끔 → 송풍은 뒤에)와 안전 규칙은 Controller 의 히트건 함수
(heater_on · heater_power · heater_element · heater_off)가 한 곳에서 지킨다. 여기서는 "측정" 주체로 부르기만 한다.

열선 출력 %(펌웨어 V17 hp - 0.1 % 단위 · 몰아 켜기 / 고르게 나눠 켜기 · 칸 = 전원 주기의 정수배):
  열선 줄(SSR + 스테이션)은 명령한 % 를 그대로 통과시키지 않는다 - 같은 % 라도 고르게 켜기 칸에 따라 들어가는 열이 2~3배 다르고 출력과 온도가 직선이 아니다.
  그래서 출력 % 를 계산하는 제어기 하나(PowerControl - 배율 × 앞먹임 + PI + 적분 포화 방지 + 0 ~ heater_max_pct 제한)를 가열 · 냉각이 같이 쓰고
  (냉각은 목표선이 내려갈 뿐), 앞먹임과 이득은 실제로 잰 출력별 온도 표(heat_curve - [히트건 맞추기]가 고른 칸으로 잰 정지 온도)에서 읽는다:
  앞먹임 = hold(목표)(그 온도를 유지하는 출력 - 표를 온도 → 출력으로 보간) + τ × 기울기 ÷ gain(목표)(그 온도 구간의 °C/%).
  계수는 손으로 맞추지 않는다: 열 모델(1차 + 지연 τ · θ - ThermalModel)에서 SIMC 규칙(지연 = θ + 평균 창의 절반 · τc = max(지연, heater_tc_s)) · 이득은 회차마다 gain(목표).
  표가 없으면 예전 식(앞먹임 = (목표 − 시작 주위 온도) ÷ K + τ × 기울기 ÷ K · 이득 K · 배율 1). 열 모델도 없으면 보수적인 기본값(DEFAULT_MODEL).
  배율 q(실시간 맞춤): 같은 출력 · 같은 방식인데 히트건이 내는 열이 스스로 1.8~2.3 배 바뀐다(10~17 분에 한 번쯤 · 15 초 안에) → 고정된 표로는 2 배 어긋난다.
    출력 = q × 앞먹임 + Kc e + J(적분 보정 %) · Kc = q × 표의 이득. 앞먹임이 FF_ADAPT 이상이면 J 를 Q_MOVE_S 시정수로 q 에 옮긴다(Q_MIN ~ Q_MAX).
    배운 q 는 RelayHeater.q(= Controller.heat_q · 프로그램을 켜면 1)에 두어 다음 단계 · 샘플 · 작업이 이어받는다.
  PI 는 온도가 아니라 오차(목표 − 온도)의 heater_pi_filter_s 평균으로 계산한다 - 늘 있는 ± 1 °C · 7~10 s 흔들림 한 주기(3 s 평균 온도 PI 는 흔들림을 키웠다).
  온도 급변: 걸러진 온도(heater_lead_filter_s 평균)가 목표선에서 heater_jump_c 넘게 벗어났는데 출력에 여유가 있었으면 q 를 바로 한 단(× JUMP_DOWN · × JUMP_UP) 바꾸고 J 를 지운다.
    같은 쪽으로 JUMP_AGAIN_S 뒤에도 안 나아지면 한 단 더 · 절반 아래로 JUMP_CALM_S 이어지면 끝 · 단계 시작 JUMP_SKIP_S 는 보지 않는다. 급변은 LineControl.jumps 에 남는다
    (온도 시험 · 측정이 "온도 급변" 메모 · 로그로 보인다 - 멈추지 않는다).
목표선(LineControl): 가열 = 시작 온도 + 속도 × 경과(상한 가열 끝 온도) · 냉각 = 가열 끝 순간 온도 − 속도 × 경과(하한 냉각 끝 온도).
  방향 전환(heater_turn_s): 단계를 시작할 때 목표선 기울기를 직전 기울기(slope0 - 가열 직후 냉각이면 가열 속도 · 아니면 0)에서 이 시간에 걸쳐 바꾼다
  (출력을 한 번에 43 → 25 % 로 내려 6 초에 10 °C 떨어지고 되튀었고, 그 출렁임이 온도 급변으로 잡혀 배율이 깎였다). 급변은 전환 시간까지 더 건너뛴다.
  기다림(heater_lead_c): 순간값이 아니라 걸러진 온도(heater_lead_filter_s 평균)가 lead 넘게 뒤처진 채 heater_lead_hold_s 이상 이어질 때만 목표선을 세운다
  (냉각을 늦추려 켠 열선이 순간 온도를 목표선 위로 튀게 해 목표선이 섰던 일 - 냉각이 11 °C/분에 그쳤다).
속도(점 수 기준): 가열 · 냉각 속도 = 목표 점 간격 ÷ 읽기 간격 × 60(config.heat_rate_c_min · cool_rate_c_min).
최대 속도(speed_table): 온도 시험 [최대 속도 재기]의 기록에서 5 °C 구간마다 가장 빠른 가열 · 냉각 속도. 설정 속도가 더 빠르면 시작 때 주의(speed_warnings).
"""

import math
from collections import deque

import numpy as np

from app import config as config_mod
from app.safety import BY_RUN

DEFAULT_MODEL = (1.2, 40.0, 10.0)                   # 열 모델이 없을 때(K °C/% · τ s · θ s): 이득을 크게 · 지연을 길게 - PI 계수가 작아 보수적이다
SPEED_BAND_C = 5.0                                  # 최대 속도 표의 온도 구간
SPEED_WIN_S = 4.0                                   # 최대 속도: 이 시간 창의 직선 기울기를 그 순간 속도로 본다(한 점 잡음을 줄인다)


def ramp_setpoint(t_start_c, rate_c_min, elapsed_s, top_c):
    """가열 목표 온도(°C): 시작 온도 + 속도 × 경과, 가열 끝 온도를 넘지 않는다."""
    return min(float(top_c), float(t_start_c) + float(rate_c_min) * max(0.0, float(elapsed_s)) / 60.0)


GAIN_MIN = 0.2                                      # 출력별 온도 표의 기울기 하한(°C/%) - 평평한 구간에서 계수가 터지지 않게
Q_MIN, Q_MAX = 0.3, 3.5                             # 배율(표에서 읽은 출력에 곱하는 값)의 범위
FF_ADAPT = 8.0                                      # 표에서 읽은 출력이 이 % 이상일 때만 적분 보정을 배율로 옮긴다(낮은 출력에서는 기준 온도 오차가 더 크다)
Q_MOVE_S = 10.0                                     # 적분 보정을 배율로 옮기는 시정수(s)
JUMP_DOWN, JUMP_UP = 0.75, 1.33                     # 온도 급변: 너무 뜨거우면 배율 × 0.75 · 너무 차가우면 × 1.33
JUMP_AGAIN_S = 6.0                                  # 급변이 이어지면 이 시간마다 한 번 더
JUMP_CALM_S = 5.0                                   # 벗어난 폭이 기준의 절반 아래로 이 시간 이어지면 급변 끝
JUMP_SKIP_S = 12.0                                  # 단계를 시작하고 이 시간은 급변으로 보지 않는다(가열 → 냉각으로 꺾일 때 출력이 한 번에 바뀐다)


class ThermalModel:
    """열 모델(1차 + 지연): τ dT/dt = −(T − T_주위) + K · u(t − θ). cd 의 heat_model_k · tau · theta(없으면 DEFAULT_MODEL)
    + 출력별 온도 표 heat_curve(있으면 유지 출력 hold · 이득 gain 을 표에서) + 제어 응답 시간 heater_tc_s."""

    def __init__(self, cd):
        k, tau, th = cd.get("heat_model_k"), cd.get("heat_model_tau_s"), cd.get("heat_model_theta_s")
        self.measured = k is not None and tau is not None and th is not None
        d = DEFAULT_MODEL
        self.K = float(k) if k is not None else d[0]
        self.tau = float(tau) if tau is not None else d[1]
        self.theta = float(th) if th is not None else d[2]
        self.curve = config_mod.heat_curve(cd.get("heat_curve"))
        self.has_curve = len(self.curve) >= 2
        self.tc = float(cd.get("heater_tc_s", 5.0))
        self.fil = float(cd.get("heater_pi_filter_s", 8.0))                 # PI 가 쓰는 오차의 평균 창(흔들림 한 주기) - 지연 = 창의 절반

    def hold(self, T):
        """그 온도를 유지하는 출력 %: 표를 온도 → 출력으로 직선 보간. 첫 점 온도 이하 0 · 마지막 점 위는 마지막 구간 기울기로 연장."""
        c, T = self.curve, float(T)
        if T <= c[0][1]:
            return 0.0
        for (pa, ta), (pb, tb) in zip(c, c[1:]):
            if T <= tb:
                return pa + (pb - pa) * (T - ta) / (tb - ta)
        (pa, ta), (pb, tb) = c[-2], c[-1]
        return pb + (pb - pa) * (T - tb) / (tb - ta)

    def gain(self, T):
        """그 온도가 든 구간의 기울기(°C/%) · 밖이면 가장 가까운 구간 · GAIN_MIN 미만이면 GAIN_MIN. 표가 없으면 K."""
        if not self.has_curve:
            return self.K
        c, T = self.curve, float(T)
        seg = (c[0], c[1]) if T <= c[0][1] else (c[-2], c[-1])
        for a, b in zip(c, c[1:]):
            if a[1] <= T <= b[1]:
                seg = (a, b)
                break
        (pa, ta), (pb, tb) = seg
        return max(GAIN_MIN, (tb - ta) / (pb - pa))

    def gains(self, K=None):
        """SIMC PI: 지연 = θ + 평균 창의 절반 · τc = max(지연, heater_tc_s) · Kc = τ ÷ (K (τc + 지연)) [%/°C] · Ti = max(1, min(τ, 4 (τc + 지연))) [s].
        K 를 주면 그 이득으로."""
        k = self.K if K is None else float(K)
        th = self.theta + self.fil / 2.0                            # 평균 창의 지연까지 넣은 지연
        tc = max(th, self.tc)
        kc = self.tau / (k * (tc + th))
        ti = max(1.0, min(self.tau, 4.0 * (tc + th)))
        return kc, ti

    def text(self):
        kc, ti = self.gains()
        return "K %.3f °C/%% · τ %.1f s · θ %.1f s · Kc %.2f %%/°C · Ti %.0f s%s%s" % (
            self.K, self.tau, self.theta, kc, ti, "" if self.measured else " · 기본값", " · 표 %d점" % len(self.curve) if self.has_curve else "")


class PowerControl:
    """열선 출력 % = 배율 q × 앞먹임(표) + Kc e + J, 0 ~ u_max 로 자른다. J = 적분 보정(%) - 출력이 한계에 걸린 채 오차가 그쪽으로 커지면 쌓지 않는다.
    출력별 온도 표가 있으면: 앞먹임 = hold(목표) + τ × 기울기 ÷ gain(목표) · Kc = q × gains(K=gain(목표)) · 앞먹임이 FF_ADAPT 이상이면 J 를 조금씩 q 로 옮긴다
    (히트건이 내는 열이 표와 몇 배 다르든 q 가 따라가고, 목표가 올라가도 그 비율이 유지된다). 표가 없으면 q = 1 인 예전 PI."""

    def __init__(self, cd, T_amb, q=1.0):
        self.m = ThermalModel(cd)
        self.kc, self.ti = self.m.gains()
        self.umax = float(cd.get("heater_max_pct", 100))
        self.T_amb = float(T_amb)
        self.q = min(Q_MAX, max(Q_MIN, float(q))) if self.m.has_curve else 1.0
        self.J = 0.0
        self.t = None
        self.u = 0.0

    def ff(self, sp, slope_c_s):
        """앞먹임(배율 전): 목표 온도를 유지하는 출력 + 목표선 기울기만큼의 출력."""
        if self.m.has_curve:
            return self.m.hold(sp) + self.m.tau * float(slope_c_s) / self.m.gain(sp)
        return ((float(sp) - self.T_amb) + self.m.tau * float(slope_c_s)) / self.m.K

    def scale(self, f):
        """배율을 f 배로(온도 급변 - LineControl 이 부른다). 표가 없으면 그대로."""
        if self.m.has_curve:
            self.q = min(Q_MAX, max(Q_MIN, self.q * float(f)))

    def out(self, T, sp, slope_c_s, now):
        dt = 0.0 if self.t is None else max(0.0, now - self.t)
        self.t = now
        if self.m.has_curve:
            kc0, self.ti = self.m.gains(K=self.m.gain(sp))
            self.kc = kc0 * self.q
        e = float(sp) - float(T)
        ff = self.ff(sp, slope_c_s)
        raw = self.q * ff + self.kc * e + self.J
        sat_hi, sat_lo = raw > self.umax, raw < 0.0
        if not ((sat_hi and e > 0) or (sat_lo and e < 0)):
            self.J += self.kc * e * dt / self.ti
            if self.m.has_curve and ff >= FF_ADAPT and dt > 0:      # 적분 보정을 배율로 옮긴다(그 순간 출력은 그대로)
                q2 = min(Q_MAX, max(Q_MIN, self.q + self.J * min(1.0, dt / Q_MOVE_S) / ff))
                self.J -= (q2 - self.q) * ff
                self.q = q2
            raw = self.q * ff + self.kc * e + self.J
        self.u = max(0.0, min(self.umax, raw))
        return self.u


class LineControl:
    """목표선 + 열선 출력(가열 또는 냉각 한 단계). sign = +1 가열(상한 end_c) · −1 냉각(하한 end_c).
    목표선은 경과 시간이 아니라 회차마다 앞으로 간다: 걸러진 온도가 lead 넘게 뒤처진 채 hold 초 이상이면 그 회차는 기다린다.
    step() 이 목표를 정하고 출력 % 를 계산해 heater.power 로 보낸다(0.1 % 단위 · 0.05 넘게 바뀔 때만 - 거절은 Rejected 그대로 올라간다).
    PI 는 온도가 아니라 오차(목표 − 온도)의 heater_pi_filter_s 평균으로 계산한다(온도 흔들림 한 주기 - 흔들림에 출력이 따라 출렁이지 않고,
    목표선이 움직여도 평균 때문에 뒤처지지 않는다). 온도 급변(_jump_check)은 heater_lead_filter_s 평균으로 본다.
    배운 배율은 heater.q 에 두어 다음 단계 · 다음 샘플이 이어받는다. 목표 도달 판정 등은 부르는 쪽이 순간 온도로 한다."""

    def __init__(self, heater, cd, T_from, end_c, sign, now, rate_c_min, T_amb=None, on_change=None, slope0=0.0):
        self.h = heater
        self.sign = 1 if sign > 0 else -1
        self.rate = float(rate_c_min)
        self.end = float(end_c)
        self.lead = float(cd["heater_lead_c"])
        self.win = float(cd.get("heater_lead_filter_s", 0.0))
        self.hold = float(cd.get("heater_lead_hold_s", 0.0))
        self.sp = self._clamp(float(T_from))
        self.t = now
        self.t_start = now
        self.waiting = False                                       # 이번 회차에 목표선이 기다렸다
        self.waits = 0                                             # 기다린 회차 수(기록용)
        self.slope = 0.0                                           # 이번 회차 목표선 기울기(°C/s - 앞먹임)
        self._buf = deque()
        self.Tf = None                                             # 마지막 걸러진 온도(heater_lead_filter_s 평균 - 기다림 판정)
        self._behind_t = None
        self.pc = PowerControl(cd, float(T_from) if T_amb is None else T_amb, q=getattr(heater, "q", 1.0))
        self.jump_c = float(cd.get("heater_jump_c", 4.0))          # 온도 급변 기준(걸러진 온도가 목표선에서 이만큼)
        self.jumps = []                                            # [{"t", "sp", "T", "d"}] - 급변마다 하나(d = 가장 크게 벗어난 폭 · 부호 있음 · 이어지는 동안 갱신)
        self._jump = None                                          # 이어지는 급변
        self._jump_step_t = None
        self._jump_d = 0.0
        self._jump_dir = 0
        self._calm_t = None
        self._pbuf = deque()                                       # (시각, 목표 − 온도) - PI 용 평균(heater_pi_filter_s · 흔들림 한 주기)
        self._dbuf = deque()                                       # (시각, 온도 − 목표) - 급변 판정용 평균(heater_lead_filter_s)
        self.pwin = float(cd.get("heater_pi_filter_s", 8.0))
        self.dev = 0.0                                             # 걸러진 (온도 − 목표)
        self.pct = None                                            # 마지막으로 보낸 출력 %
        self._held = False                                         # 스테이션 켠 뒤 100 % 유지 중이었다(끝나면 지금 온도에서 다시 시작)
        self.on_change = on_change                                 # on_change(now, pct) - 출력을 바꿀 때(기록)
        self.turn = max(0.0, float(cd.get("heater_turn_s", 15.0)))     # 방향 전환 시간
        self.slope0 = float(slope0 or 0.0)                            # 이 단계 직전의 목표선 기울기(°C/s) - 가열 직후 냉각이면 +, 멈춘 데서 시작하면 0
        self._turn_left = self.turn                                   # 남은 전환 시간(목표선이 간 시간만 센다)

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
        self.Tf = Tf
        if self._held and self.h.boot_hold <= 0:                   # 스테이션 켜기 유지가 끝났다: 목표선을 지금 온도에서 다시 시작(앞서 가 있으면) · 적분 0
            self._held = False
            self.sp = self._clamp(max(self.sp, float(T)) if self.sign > 0 else min(self.sp, float(T)))
            self.pc.J, self.pc.t = 0.0, now
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
        else:                                                      # 기다리는 동안은 전환 시간이 줄지 않는다
            s1 = self.sign * self.rate / 60.0
            if self._turn_left > 0 and self.turn > 0:              # 기울기를 slope0 → s1 로 고르게 바꾼다(이번 회차는 가운데 값)
                mid = max(0.0, self._turn_left - dt / 2.0) / self.turn
                sl = s1 + (self.slope0 - s1) * mid
                self._turn_left = max(0.0, self._turn_left - dt)
            else:
                sl = s1
            self.sp = self._clamp(self.sp + sl * dt)
            self.slope = 0.0 if self.sp == self.end else sl        # 끝 온도에 닿으면 앞먹임은 유지 출력만
        return self.sp

    def decide(self, T, sp, now):
        """정한 목표로 출력 % 를 계산해 보낸다. 바꿨으면 True.
        스테이션 켠 뒤 100 % 유지 중에는 적분하지 않고(배율 × 앞먹임 값만 요청 - Controller 가 기억했다가 유지가 끝나면 적용) 끝나면 target 이 지금 온도에서 다시 시작한다."""
        if self.h.boot_hold > 0:
            self._held = True
            self.pc.t = now
            self.pc.J = 0.0
            pct = round(max(0.0, min(self.pc.umax, self.pc.q * self.pc.ff(sp, self.slope))), 1)
            if self.pct is None or abs(pct - self.pct) > 0.05:
                self.h.power(pct)
                self.pct = pct
            return False
        err = float(sp) - float(T)                                 # 온도가 아니라 오차를 평균한다(목표선이 움직여도 평균 때문에 뒤처지지 않게)
        e_p = self._mean(self._pbuf, now, err, self.pwin)
        self.dev = self._mean(self._dbuf, now, -err, self.win)
        self._jump_check(T, sp, now)
        e_use = -self.dev if self._jump is not None else e_p       # 급변 중에는 짧은 평균(빨리 따라가야 한다) · 평소에는 흔들림 한 주기 평균
        pct = round(self.pc.out(float(sp) - e_use, sp, self.slope, now), 1)     # 0.1 % 단위(V17)
        if hasattr(self.h, "q"):
            try:
                self.h.q = self.pc.q                               # 다음 단계 · 다음 샘플이 이 배율에서 시작한다
            except AttributeError:
                pass
        if self.pct is not None and abs(pct - self.pct) <= 0.05 and abs(float(self.h.pct) - pct) <= 0.05:     # 실제 출력과도 같을 때만 건너뛴다 - [정지] '!' 가 0 으로 만들었으면 다시 보낸다
            return False
        self.h.power(pct)
        self.pct = pct
        if self.on_change is not None:
            self.on_change(now, pct)
        return True

    @staticmethod
    def _mean(buf, now, v, win):
        buf.append((now, float(v)))
        while buf and now - buf[0][0] > win:
            buf.popleft()
        return sum(x for _t, x in buf) / len(buf)

    def _jump_check(self, T, sp, now):
        """온도 급변: 걸러진 (온도 − 목표)가 jump_c 넘게 벗어났는데 출력에 여유가 있었다(더 줄이거나 더 올릴 수 있었다).
        배율을 바로 한 단 바꾸고 적분 보정을 지운다. 같은 쪽으로 JUMP_AGAIN_S 뒤에도 나아지지 않았으면(1 °C 도 안 줄었으면) 한 단 더.
        반대쪽으로 넘어간 것은 PI 에 맡긴다(같은 급변 안에서 배율을 되돌리지 않는다). 벗어난 폭이 절반 아래로 JUMP_CALM_S 이어지면 끝."""
        d = self.dev
        if now - self.t_start < JUMP_SKIP_S + self.turn:          # 방향 전환 직후의 출렁임은 급변이 아니다
            return
        last = 0.0 if self.pct is None else float(self.pct)
        room = (d > 0 and last > 0.05) or (d < 0 and last < self.pc.umax - 0.05)
        j = self._jump
        if j is None:
            if abs(d) >= self.jump_c and room:
                self._jump = j = {"t": now, "sp": float(sp), "T": float(T), "d": d}
                self.jumps.append(j)
                self._jump_dir = 1 if d > 0 else -1
                self._jump_step(d, now)
            return
        if abs(d) > abs(j["d"]) and (d > 0) == (self._jump_dir > 0):
            j["d"] = d
        if abs(d) < self.jump_c / 2.0:
            if self._calm_t is None:
                self._calm_t = now
            elif now - self._calm_t >= JUMP_CALM_S:
                self._jump = None
                self._pbuf.clear()                                 # 급변 동안의 오차가 평균에 남지 않게
            return
        self._calm_t = None
        if (d > 0) == (self._jump_dir > 0) and abs(d) >= self.jump_c and room and now - self._jump_step_t >= JUMP_AGAIN_S \
                and abs(d) > abs(self._jump_d) - 1.0:
            self._jump_step(d, now)

    def _jump_step(self, d, now):
        self.pc.scale(JUMP_DOWN if d > 0 else JUMP_UP)
        self.pc.J = 0.0
        self._jump_step_t, self._jump_d = now, d
        self._calm_t = None

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
def fit_fopdt(rows, du_pct, T0=None):
    """[히트건 맞추기]의 출력 표 한 단 기록 rows = [(경과 s, 온도)] (출력을 du_pct 만큼 바꾼 뒤 유지한 구간) → (K °C/%, τ s, θ s, 맞춤 오차 °C) 또는 None.
    T(t) = T0 + K du (1 − exp(−(t − θ)/τ)) (t > θ). du 가 음수(내린 계단)면 ys 와 du 의 부호를 같이 뒤집어 같은 식으로 맞춘다.
    T0 = 기준 온도(계단 직전 평균 - 없으면 첫 점). θ · τ 는 격자에서 고르고 K 는 그때마다 최소제곱으로 바로 푼다."""
    if len(rows) < 8 or not du_pct:
        return None
    t0 = rows[0][0]
    base = rows[0][1] if T0 is None else float(T0)
    sgn = 1.0 if du_pct > 0 else -1.0
    ts = [t - t0 for t, _ in rows]
    ys = [sgn * (T - base) for _, T in rows]
    u_pct = abs(float(du_pct))
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
    return A / u_pct, tau, th, math.sqrt(sse / len(ys))         # K = A ÷ |du|(부호를 같이 뒤집었으므로 K 는 양수)


SWEEP_NODES = 11                                    # [환경 맞추기] 정지 곡선의 마디 수(0 ~ top 을 10 등분)
SWEEP_MIN_N, SWEEP_MIN_TOP = 60, 5.0                # 점이 이보다 적거나 최고 출력이 이보다 낮으면 맞추지 않는다
SWEEP_THETAS = tuple(x * 0.5 for x in range(0, 11))           # θ 0 ~ 5 s · 0.5 간격
SWEEP_TAUS = tuple(float(x) for x in range(2, 61))            # τ 2 ~ 60 s · 1 간격


def fit_sweep(t, u, T, top):
    """[환경 맞추기] 출력 훑기 기록 → 열 모델 + 정지 곡선. t(경과 s) · u(그때 출력 %) · T(온도)는 같은 길이(기록 순서) · top = 훑기의 최고 출력 %.
    모델: τ dT/dt = Tss(u(t − θ)) − T · Tss = 마디 u_k = k × top ÷ 10(k = 0..10)의 온도 c_k 를 직선으로 이은 것.
    θ · τ 는 격자(SWEEP_THETAS × SWEEP_TAUS)에서 고르고 c_k 는 조합마다 최소제곱으로 바로 푼다:
    Φ[i][k] = 마디 k 의 모자 함수 값(u(t_i − θ) - 직선 보간 · 첫 점 앞은 첫 값 · 0 ~ top 으로 자름) ·
    F[0] = Φ[0] · a_i = exp(−Δt_i ÷ τ) · F[i] = a_i F[i−1] + (1 − a_i)(Φ[i] + Φ[i−1]) ÷ 2 · T ≈ F c.
    반환 {"tau", "theta", "rms", "pts": [(출력 %, 정지 온도 °C) × 11]} 또는 None(점 SWEEP_MIN_N 미만 · top SWEEP_MIN_TOP 미만).
    점마다의 되풀이는 τ 를 모두 한 번에(배열) 계산한다 - θ 11개 × 점 수 번만 돈다."""
    t = np.asarray(t, float)
    u = np.asarray(u, float)
    T = np.asarray(T, float)
    top = float(top)
    n = len(t)
    if n < SWEEP_MIN_N or len(u) != n or len(T) != n or top < SWEEP_MIN_TOP:
        return None
    nk = SWEEP_NODES
    du = top / (nk - 1)
    taus = np.array(SWEEP_TAUS)
    dt = np.diff(t)
    A = np.exp(-np.maximum(dt, 0.0)[:, None] / taus[None, :])          # (n − 1, τ 수)
    rows = np.arange(n)
    best = None
    for th in SWEEP_THETAS:
        x = np.clip(np.interp(t - th, t, u), 0.0, top) / du               # np.interp 는 첫 점 앞을 첫 값으로 둔다
        k = np.minimum(np.floor(x).astype(int), nk - 2)
        fr = x - k
        phi = np.zeros((n, nk))
        phi[rows, k] = 1.0 - fr
        phi[rows, k + 1] += fr
        B = (phi[1:] + phi[:-1]) * 0.5                                    # (n − 1, 마디)
        F = np.empty((n, len(taus), nk))
        F[0] = phi[0][None, :]
        for i in range(1, n):
            a = A[i - 1][:, None]
            F[i] = a * F[i - 1] + (1.0 - a) * B[i - 1][None, :]
        for j, tau in enumerate(taus):
            M = F[:, j, :]
            c, *_ = np.linalg.lstsq(M, T, rcond=None)
            rms = float(np.sqrt(np.mean((T - M @ c) ** 2)))
            if best is None or rms < best[0]:
                best = (rms, float(tau), float(th), c)
    rms, tau, th, c = best
    return {"tau": tau, "theta": th, "rms": rms, "pts": [(round(i * du, 3), float(c[i])) for i in range(nk)]}


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
    def q(self):
        """목표선 제어가 배운 배율(표에서 읽은 출력에 곱하는 값) - 다음 단계 · 다음 샘플 · 다음 작업이 여기서 시작한다. 프로그램을 켜면 1."""
        return float(getattr(self.c, "heat_q", 1.0))

    @q.setter
    def q(self, v):
        self.c.heat_q = float(v)

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
