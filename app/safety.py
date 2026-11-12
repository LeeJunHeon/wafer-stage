"""safety.py - 안전 규칙을 한 곳에 모은다. 이 파일의 규칙만 지킨다(그 밖의 제한은 만들지 않는다).

 1. 비상정지: 기다리지 않고 즉시 '!'. 명령을 받은 순간 세대 번호를 잡고, 쓰기 직전에 다시 비교해서 다르면 쓰지 않는다.
 2. 비상정지 뒤에는 이동 · 릴레이 켜기를 막는다. 사람이 풀면 st 로 위치를 다시 읽고 풀린다(원점을 강제로 다시 잡게 하지 않는다).
 3. 히트건(R1): 켠 주체를 구분한다 - "수동"(릴레이 켜기 버튼) / "측정"(측정 작업).
    공통: 온도 값이 들어오고 있으면 차단 온도 이상에서 못 켜고, 켜진 동안 차단 온도에 닿으면 끈다 · 최대 켜짐 시간이 지나면 끈다 ·
          켜진 동안 X · Y 이동 거절 · 비상정지 잠금.
    측정만: 온도를 읽을 수 있을 때만 켠다 · 켜진 동안 온도를 못 읽으면(temp_stale_s) 끈다.
    수동은 온도계가 없거나 온도를 못 읽어도 켤 수 있고, 온도를 못 읽는다고 끄지 않는다.
 4. 자동 X · Y 이동 전에는 Z 를 맨 위로 올리고 st 로 Z=0 을 확인한다(control 이 순서를 지키고, 여기서는 Z 가 확인됐는지 판정한다).
 5. 원점이 있는 축은 가동 범위 밖으로 가는 명령을 거절한다.
 6. 통신 감시 · 종료 처리는 control 이 한다(여기에는 판정만).
 7. 경보는 state.alarms 한 곳에만 쌓는다.

판정 함수는 시계를 인자로 받아 시험에서 시간을 건너뛸 수 있다. 거절 문구는 쉬운 한국어 한 문장이다.
"""

import threading
import time
from typing import Optional

from app.state import ALARM, NOTICE, State
from app.config import Config


BY_MANUAL, BY_RUN = "수동", "측정"                # 히트건을 켠 주체
TRIP_MAX_C, TRIP_NO_TEMP, TRIP_MAX_ON = "차단 온도", "온도 못 읽음", "최대 켜짐 시간"      # 안전 규칙이 히트건을 끄는 까닭


class Rejected(Exception):
    """명령 거절. 문구를 그대로 사용자에게 돌려준다."""


class Safety:
    def __init__(self, cfg: Config, state: State, clock=time.monotonic):
        self.cfg = cfg
        self.state = state
        self.clock = clock
        self._lock = threading.Lock()
        self.generation = 0
        self.heater_on_t: Optional[float] = None        # 히트건을 켠 시각(clock)
        self.heater_by: Optional[str] = None            # 켠 주체(BY_MANUAL · BY_RUN). 꺼져 있으면 None
        self.last_temp_t: Optional[float] = None        # 마지막으로 온도를 읽은 시각
        self.last_temp: Optional[float] = None

    # ---- 세대 · 비상정지 ----
    def gen_ok(self, g) -> bool:
        """g 가 None 이면 세대와 무관한 명령(연결 · 감시 · 비상정지 뒤 정리)."""
        return g is None or g == self.generation

    def begin_estop(self) -> int:
        """비상정지 시작: 세대를 올려 기다리던 명령 · 진행 중이던 작업을 버리게 하고 잠근다. '!' 를 쓰기 전에 부른다."""
        with self._lock:
            self.generation += 1
            g = self.generation
            if self.heater_on_t is not None:              # 히트건은 비상정지가 끈다(r all 0) - 감시는 이 순간부터 꺼진 것으로 본다.
                self.heater_turned_off()                  # 끄기 응답을 기다리는 동안 "온도를 못 읽어 껐습니다" 경보가 덧붙지 않게
        self.state.set("estop_locked", True)
        self.state.alarm("estop", "비상정지 상태", ALARM)
        return g

    def release(self):
        """비상정지 풀기(위치는 control 이 st 로 다시 읽은 뒤 부른다)."""
        with self._lock:
            self.generation += 1                          # 풀기 전의 낡은 명령이 살아나지 않게
        self.state.set("estop_locked", False)
        self.state.clear_alarm("estop", "reboot")

    @property
    def locked(self) -> bool:
        return bool(self.state.get("estop_locked"))

    # ---- 이동 · 릴레이 거절 ----
    def reject_move(self, xy: bool, heater_on: bool) -> Optional[str]:
        if self.locked:
            return "비상정지 상태"
        if xy and heater_on:
            return "히트건 켜짐 · X · Y 이동 불가"
        return None

    def reject_relay_on(self) -> Optional[str]:
        if self.locked:
            return "비상정지 상태"
        return None

    def reject_target(self, axis: str, target_mm: float, homed: bool) -> Optional[str]:
        """원점이 있는 축은 가동 범위 밖으로 가지 않는다. 원점이 없으면 막지 않는다."""
        if not homed:
            return None
        hi = float(self.cfg["%s_max_mm" % axis])
        if target_mm < -1e-9 or target_mm > hi + 1e-9:
            return "%s 가동 범위 0~%g mm 초과" % (axis.upper(), hi)
        return None

    def z_is_top(self, z_pulse) -> bool:
        """자동 X · Y 이동을 시작해도 되는가: st 로 읽은 Z 가 0(맨 위)."""
        return z_pulse is not None and int(z_pulse) == 0

    # ---- 히트건 ----
    def note_temp(self, value: Optional[float], now: Optional[float] = None):
        now = self.clock() if now is None else now
        if value is not None:
            self.last_temp, self.last_temp_t = float(value), now

    def temp_fresh(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        return self.last_temp_t is not None and (now - self.last_temp_t) <= float(self.cfg["temp_stale_s"])

    def reject_heater_on(self, by: str, thermometer_ok: bool, now: Optional[float] = None) -> Optional[str]:
        """by = 켜려는 주체. 측정(BY_RUN)은 온도를 읽을 수 있어야 켠다. 수동(BY_MANUAL)은 온도계 조건을 보지 않는다.
        둘 다: 온도 값이 들어오고 있고 그 값이 차단 온도 이상이면 켜지 않는다."""
        now = self.clock() if now is None else now
        r = self.reject_relay_on()
        if r:
            return r
        fresh = self.temp_fresh(now)
        if by != BY_MANUAL and not (thermometer_ok and fresh):
            return "온도 측정 실패 · 히트건 켜기 불가"
        if fresh and self.last_temp >= float(self.cfg["heater_max_c"]):
            return "온도 %.0f °C · 차단 온도 %g °C 이상" % (self.last_temp, self.cfg["heater_max_c"])
        return None

    def heater_turned_on(self, by: str = BY_RUN, now: Optional[float] = None):
        """by 를 모르면(릴레이가 켜진 것만 읽혔을 때) 측정 쪽 규칙(더 엄한 쪽)으로 본다."""
        self.heater_on_t = self.clock() if now is None else now
        self.heater_by = by

    def heater_turned_off(self):
        self.heater_on_t = None
        self.heater_by = None

    def heater_trip(self, now: Optional[float] = None):
        """켜진 동안 매번 부른다. 꺼야 하면 (까닭, 경보 문구), 괜찮으면 None.
        까닭 = TRIP_MAX_C(차단 온도) · TRIP_NO_TEMP(온도 못 읽음 - 측정이 켰을 때만) · TRIP_MAX_ON(최대 켜짐 시간)."""
        if self.heater_on_t is None:
            return None
        now = self.clock() if now is None else now
        if self.last_temp is not None and self.last_temp_t is not None and self.last_temp_t >= self.heater_on_t - 1e-9 \
                and self.last_temp >= float(self.cfg["heater_max_c"]):
            return TRIP_MAX_C, "온도 %.0f °C 도달 · 히트건 차단" % self.last_temp
        if self.heater_by != BY_MANUAL:                              # 수동으로 켰을 때는 온도를 못 읽어도 끄지 않는다
            since = max(self.heater_on_t, self.last_temp_t if self.last_temp_t is not None else -1e18)
            if now - since > float(self.cfg["temp_stale_s"]):
                return TRIP_NO_TEMP, "온도 측정 실패 · 히트건 차단"
        if now - self.heater_on_t >= float(self.cfg["heater_max_on_s"]):
            return TRIP_MAX_ON, "히트건 %g초 가동 · 차단" % self.cfg["heater_max_on_s"]
        return None


__all__ = ["Safety", "Rejected", "ALARM", "NOTICE", "BY_MANUAL", "BY_RUN", "TRIP_MAX_C", "TRIP_NO_TEMP", "TRIP_MAX_ON"]
