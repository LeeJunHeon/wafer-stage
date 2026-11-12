"""heater.py - 가열 장치. 지금은 릴레이(R1)에 달린 히트건이라 켜기 · 끄기뿐이다.

가열 방식은 나중에 하드웨어가 바뀔 예정이다 - 측정(app/jobs/run.py)은 이 클래스의 on · off · is_on 만 쓰므로 여기만 바꾸면 된다.
켜기는 Controller 의 안전 규칙을 "측정" 주체로 지난다(온도를 읽을 수 있어야 켠다 · 온도를 못 읽으면 꺼진다 · 차단 온도 · 최대 켜짐 시간).
"""

from app.safety import BY_RUN


class RelayHeater:
    def __init__(self, ctrl):
        self.c = ctrl

    @property
    def relay(self):
        return int(self.c.cfg["heater_relay"])

    @property
    def is_on(self):
        return self.c._heater_on()

    def on(self):
        self.c._relay_on(self.relay, BY_RUN)

    def off(self):
        self.c._relay_off(self.relay, BY_RUN)
