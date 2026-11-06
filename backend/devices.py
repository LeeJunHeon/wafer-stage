"""devices.py - 측정 장비 드라이버 인터페이스(온도 센서 · 접촉 · 계측기)와 더미, 시간 배율 시계.

실제 장비는 아직 미정이다. 같은 인터페이스를 구현해 settings 의 temp.driver / contact.driver / meter.driver 로 갈아끼운다.
더미는 dry 전용이다(실제 장비에서 더미이면 그 드라이버를 쓰는 단계의 측정 조건은 시작을 거절한다 - engine).

  TempSource.read() -> °C | None(센서 오류)          connect() / close() / is_dummy
  ContactSource.is_contact() -> bool | None          connect() / close() / is_dummy
  MeterSource.read() -> {채널: 값}                    connect() / close() / is_dummy / channels(설정 meter.channels)

시간 배율(sim.time_scale)은 dry 에서만 적용한다(Clock). 실장비에서는 항상 1.
"""

import math
import random
import threading
import time


class DeviceError(Exception):
    pass


class Clock:
    """가상 시계. 실제 경과 x scale (dry 에서만 scale > 1)."""

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


# --------------------------------------------------------------------------
class TempSource:
    is_dummy = False

    def connect(self):
        pass

    def close(self):
        pass

    def read(self):                                  # pragma: no cover - 인터페이스
        raise NotImplementedError


class DummyTemp(TempSource):
    """열 모델: 히트건 켜짐 -> heater_c, 쿨러 켜짐 -> cooler_c, 아니면 주변. 지수 응답(시간 상수는 방향별)."""
    is_dummy = True

    def __init__(self, clock, sim, heater_on_fn, cooler_on_fn):
        self.clock = clock
        self.sim = sim
        self.heater_on = heater_on_fn
        self.cooler_on = cooler_on_fn
        self.T = float(sim.get("ambient_c", 25.0))
        self._t = clock.now()
        self.fail = False                            # 시험: 센서 오류 흉내
        self.lock = threading.Lock()

    def read(self):
        with self.lock:
            now = self.clock.now()
            dt = max(0.0, now - self._t)
            self._t = now
            s = self.sim
            if self.heater_on():
                tgt, tau = float(s["heater_c"]), float(s["tau_heat_s"])
            elif self.cooler_on():
                tgt, tau = float(s["cooler_c"]), float(s["tau_cool_s"])
            else:
                tgt, tau = float(s["ambient_c"]), float(s["tau_amb_s"])
            self.T = tgt + (self.T - tgt) * math.exp(-dt / max(tau, 1e-6))
            if self.fail:
                return None
            return self.T + random.gauss(0.0, float(s.get("noise_c", 0.0)))


class NoTemp(TempSource):
    """센서 없음 - 읽으면 None."""

    def read(self):
        return None


class ContactSource:
    is_dummy = False

    def connect(self):
        pass

    def close(self):
        pass

    def is_contact(self):                            # pragma: no cover
        raise NotImplementedError


class DummyContact(ContactSource):
    """Z 가 contact_z 이상이면 접촉."""
    is_dummy = True

    def __init__(self, z_fn, contact_z):
        self.z_fn = z_fn
        self.contact_z = float(contact_z)

    def is_contact(self):
        z = self.z_fn()
        return None if z is None else bool(z >= self.contact_z)


class MeterSource:
    is_dummy = False

    def __init__(self, channels=None):
        self.channels = channels or []

    def connect(self):
        pass

    def close(self):
        pass

    def read(self):                                  # pragma: no cover
        raise NotImplementedError


class DummyMeter(MeterSource):
    """dry 전용 더미 계측기: 온도에 비례하는 값 + 잡음."""
    is_dummy = True

    def __init__(self, channels, temp_fn):
        super().__init__(channels)
        self.temp_fn = temp_fn

    def read(self):
        t = self.temp_fn()
        t = 25.0 if t is None else t
        out = {}
        for i, ch in enumerate(self.channels):
            out[ch["name"]] = round(100.0 + (i + 1) * 0.5 * (t - 25.0) + random.gauss(0, 0.05), int(ch.get("digits", 3)))
        return out


class NoMeter(MeterSource):
    """계측기 미연결 - 읽으면 빈 값."""

    def read(self):
        return {}
