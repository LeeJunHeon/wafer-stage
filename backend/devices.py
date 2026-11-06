"""devices.py - 가상 시계(시뮬레이션 전용 시간 배율).

온도 · 계측기 · 접촉 판정 드라이버는 이 프로그램에서 뺐다(별도 프로그램이 맡는다. 나중에 연동할 때 다시 설계한다).
시간 배율(sim.time_scale)은 dry / fake-serial 에서만 적용한다. 실장비에서는 항상 1.
"""

import time


class Clock:
    """가상 시계. 실제 경과 x scale (시뮬레이션에서만 scale > 1)."""

    def __init__(self, scale=1.0):
        self.scale = max(0.01, float(scale))
        self._t0 = time.monotonic()
        self._v0 = 0.0

    def now(self):
        return self._v0 + (time.monotonic() - self._t0) * self.scale

    def set_scale(self, scale):
        self._v0 = self.now()
        self._t0 = time.monotonic()
        self.scale = max(0.01, float(scale))

    def sleep(self, dt_v, cancel=None, tick=0.05):
        """가상 시간 dt_v 초 대기. cancel() 이 참이면 즉시 False."""
        end = time.monotonic() + max(0.0, dt_v) / self.scale
        while True:
            if cancel and cancel():
                return False
            left = end - time.monotonic()
            if left <= 0:
                return True
            time.sleep(min(tick, left))
