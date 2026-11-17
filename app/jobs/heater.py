"""heater.py - 가열 장치: 릴레이 둘(송풍 heater_fan_relay · 열선 heater_element_relay)에 달린 히트건과 가열 속도 제어의 판정.

측정(app/jobs/run.py)은 이 클래스의 on · off · element · is_on · element_on · fan_on 만 쓴다 - 가열 하드웨어가 바뀌면 여기만 고친다.
켜고 끄는 순서(열선 → 송풍 · 열선 끔 → 송풍은 뒤에)와 안전 규칙은 Controller 의 히트건 함수(heater_on · heater_element · heater_off)가
한 곳에서 지킨다. 여기서는 "측정" 주체로 부르기만 한다(온도를 읽을 수 있어야 켠다 · 못 읽으면 꺼진다 · 차단 온도 · 열선 연속 최대 시간).

가열 속도 제어(run_heat_mode "ramp"): 송풍을 켜 둔 채 열선을 켜고 꺼서 목표선을 따른다.
  목표선 SP(t) = 가열 시작 온도 + 가열 속도(°C/분) × 경과(분), 상한 = 가열 끝 온도.
  온도 < SP − 허용 폭 → 열선 켬 · 온도 > SP → 끔 · 그 사이 → 그대로. 최소 켜짐 · 최소 꺼짐 시간을 지킨다(element_decision).
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
