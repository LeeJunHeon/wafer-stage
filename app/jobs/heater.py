"""heater.py - 가열 장치: 릴레이 둘(송풍 heater_fan_relay · 열선 heater_element_relay)에 달린 히트건과 가열 속도 제어의 판정.

측정(app/jobs/run.py)은 이 클래스의 on · off · element · is_on · element_on · fan_on 만 쓴다 - 가열 하드웨어가 바뀌면 여기만 고친다.
켜고 끄는 순서(열선 → 송풍 · 열선 끔 → 송풍은 뒤에)와 안전 규칙은 Controller 의 히트건 함수(heater_on · heater_element · heater_off)가
한 곳에서 지킨다. 여기서는 "측정" 주체로 부르기만 한다(온도를 읽을 수 있어야 켠다 · 못 읽으면 꺼진다 · 차단 온도 · 열선 연속 최대 시간).

속도 제어(가열 run_heat_mode "ramp" · 냉각 run_cool_mode "ramp"): 송풍을 켜 둔 채 열선을 켜고 꺼서 목표선을 따른다.
  가열 목표선 = 가열 시작 온도 + 가열 속도(°C/분) × 경과(분), 상한 = 가열 끝 온도.
  냉각 목표선 = 가열 끝 순간 온도 − 냉각 속도(°C/분) × 경과(분), 하한 = 냉각 끝 온도.
  온도 < SP − 허용 폭 → 열선 켬 · 온도 > SP → 끔 · 그 사이 → 그대로. 최소 켜짐 · 최소 꺼짐 시간을 지킨다(element_decision - 가열 · 냉각 같은 함수).
  목표선 기다림(heater_lead_c): 가열 때 온도가 목표선보다 이만큼 넘게 아래 · 냉각 때 이만큼 넘게 위면 목표선이 그 자리에서 기다린다(LineControl).
자동 측정(app/jobs/run.py)과 온도 시험(app/jobs/temptest.py)이 LineControl · Stall 을 같이 쓴다(두 곳에 따로 만들지 않는다).
"""

from app.safety import BY_RUN


def ramp_setpoint(t_start_c, rate_c_min, elapsed_s, top_c):
    """가열 속도 제어의 목표 온도(°C): 시작 온도 + 속도 × 경과, 가열 끝 온도를 넘지 않는다."""
    return min(float(top_c), float(t_start_c) + float(rate_c_min) * max(0.0, float(elapsed_s)) / 60.0)


def element_decision(temp_c, sp_c, band_c, on, since_change_s, min_on_s, min_off_s):
    """열선을 켤지(True) 끌지(False). on = 지금 상태 · since_change_s = 마지막으로 바꾼 뒤 지난 시간.
    온도 < 목표 − 허용 폭 → 켬 · 온도 > 목표 → 끔 · 그 사이 → 그대로(자주 바뀌지 않게). 켠 지 min_on_s · 끈 지 min_off_s 가 안 됐으면 그대로."""
    want = on
    if temp_c < sp_c - band_c:
        want = True
    elif temp_c > sp_c:
        want = False
    if want != on and since_change_s < (min_on_s if on else min_off_s):
        return on
    return want


class LineControl:
    """목표선 + 열선 판정(가열 또는 냉각 한 단계). sign = +1 가열(상한 end_c) · −1 냉각(하한 end_c).
    목표선은 경과 시간이 아니라 회차마다 앞으로 간다: 온도가 목표선보다 lead 넘게 뒤처져 있으면(가열 - 아래 · 냉각 - 위) 그 회차는 기다린다.
    다시 따라오면 그 지점부터 같은 속도로 이어 간다. step() 이 목표를 정하고 열선을 바꾼다(heater.element - 거절은 Rejected 그대로 올라간다)."""

    def __init__(self, heater, cd, T_from, end_c, sign, now, rate_key, el_t=None, on_change=None):
        self.h = heater
        self.sign = 1 if sign > 0 else -1
        self.rate = float(cd[rate_key])
        self.end = float(end_c)
        self.band, self.lead = float(cd["heater_band_c"]), float(cd["heater_lead_c"])
        self.min_on, self.min_off = float(cd["heater_min_on_s"]), float(cd["heater_min_off_s"])
        self.sp = self._clamp(float(T_from))
        self.t = now
        self.el_t = now if el_t is None else el_t                  # 열선을 마지막으로 바꾼 시각
        self.waiting = False                                       # 이번 회차에 목표선이 기다렸다
        self.waits = 0                                             # 기다린 회차 수(기록용)
        self.on_change = on_change                                 # on_change(now, on) - 열선을 바꿀 때(기록)

    def _clamp(self, v):
        return min(self.end, v) if self.sign > 0 else max(self.end, v)

    def target(self, T, now):
        """이번 회차의 목표 온도. 온도가 lead 넘게 뒤처져 있으면 목표선을 그 자리에 둔다."""
        dt = max(0.0, now - self.t)
        self.t = now
        behind = (T < self.sp - self.lead) if self.sign > 0 else (T > self.sp + self.lead)
        self.waiting = bool(behind)
        if behind:
            self.waits += 1
        else:
            self.sp = self._clamp(self.sp + self.sign * self.rate * dt / 60.0)
        return self.sp

    def step(self, T, now):
        """목표를 정하고 열선을 켜고 끈다. 반환 = 목표 온도."""
        sp = self.target(T, now)
        self.decide(T, sp, now)
        return sp

    def decide(self, T, sp, now):
        """정한 목표로 열선을 켜고 끈다(element_decision). 바꿨으면 True."""
        el = self.h.element_on
        want = element_decision(T, sp, self.band, el, now - self.el_t, self.min_on, self.min_off)
        if want != el:
            self.h.element(want)
            self.el_t = now
            if self.on_change is not None:
                self.on_change(now, want)
            return True
        return False


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
    def fan_pending(self):
        """열선은 꺼졌고 송풍을 늦게 끄려고 기다리는 중."""
        return self.c.heater_fan_pending()

    def on(self):
        """송풍이 꺼져 있으면 켜기 순서대로(열선 → 대기 → 송풍), 켜져 있으면 열선만."""
        self.c.heater_on(BY_RUN)

    def element(self, on):
        """송풍이 켜진 동안 열선만 켜고 끈다(가열 속도 제어)."""
        self.c.heater_element(bool(on), BY_RUN)

    def off(self):
        """열선 끔 → 송풍은 열선을 끈 때부터 heater_fan_after_s 뒤에 끈다."""
        self.c.heater_off(BY_RUN)
