"""power.py - 전원 계층: 릴레이 · 히트건. 켠 주체(owner)를 기록한다.

규칙 (CLAUDE.md "안전 불변식")
- 히트건 릴레이(기본 R10)는 가열 단계 전용: 릴레이 단계 · 쿨러 n · 수동 켜기에서는 거절한다(owner 가 "heat" 가 아니면).
- 공구 가열(오프셋 방법 2)도 이 계층의 heater_for(초) 를 쓴다 - 시간 한도 안에서만 켜고 어떤 경우에도 끈다.
- 히트건 끄기는 캐시와 상관없이 항상 보낸다(r N 0). 끄기가 실패하면 heater_ok = False 가 되고 X · Y 이동을 거절한다(Motion.pre_xy).
- 비상정지 · 중지 · 오류 · 종료 때는 엔진과 작업이 켠 릴레이를 전부 끈다(all_off).
- 응답(RS)은 그것을 요청한 명령에 맞춘다: Stage 가 보내기 전에 입력을 비운다(Stage._drain).
"""

import threading
import time

from core.motion import MotionError
from core.stage import StageError

HEAT = "heat"                       # 가열 단계
HEAT_OWNERS = ("heat", "tool_heat")   # 히트건을 켤 수 있는 주체: 가열 단계 · 공구 가열(오프셋 방법 2)
MAX_HEAT_S = 3600.0


class PowerError(Exception):
    pass


class Power:
    def __init__(self, stage, heater_n=10, log=None, max_heat_s=MAX_HEAT_S):
        self.stage = stage
        self.heater_n = int(heater_n)
        self.log = log or (lambda *_: None)
        self.max_heat_s = float(max_heat_s)
        self.owners = {}                   # 릴레이 번호 -> 켠 주체
        self.heater_ok = True              # 마지막 끄기가 확인됨
        self.lock = threading.RLock()
        self.heat_since = None
        self.disabled = False             # 종료 중 켜기 금지

    # ---- 일반 릴레이 ------------------------------------------------
    def set(self, n, on, owner):
        """릴레이 하나. 히트건 릴레이는 owner == HEAT 일 때만 켠다."""
        n = int(n)
        with self.lock:
            if on and self.disabled:
                raise PowerError("종료 중 · 켜기 거절")
            if n == self.heater_n:
                if on and owner not in HEAT_OWNERS:
                    raise PowerError("R%d 는 히트건 전용 · 가열 단계만" % n)
                return self._heater(on, owner)
            rs = self.stage.relay_set(n, bool(on))
            if on:
                self.owners[n] = owner
            else:
                self.owners.pop(n, None)
            return rs

    # ---- 히트건 -----------------------------------------------------
    def _heater(self, on, owner):
        try:
            rs = self.stage.relay_set(self.heater_n, bool(on))
        except StageError as e:
            if not on:
                self.heater_ok = False
            raise PowerError("히트건 %s 실패 · %s" % ("켜기" if on else "끄기", str(e)[:40]))
        if on:
            self.owners[self.heater_n] = owner
            self.heat_since = time.monotonic()
            self.heater_ok = True
        else:
            # 응답이 꺼짐을 말할 때만 기록을 지운다(못 끈 채 기록만 지우면 과열 보호가 재시도하지 않는다)
            self.heater_ok = not (isinstance(rs, (list, tuple)) and len(rs) >= self.heater_n and rs[self.heater_n - 1])
            if self.heater_ok:
                self.owners.pop(self.heater_n, None)
                self.heat_since = None
            else:
                raise PowerError("히트건 끄기 미확인 · 이동 거절")
        return rs

    def heater_on(self, owner=HEAT):
        return self.set(self.heater_n, True, owner)

    def heater_off(self):
        """항상 보낸다(캐시 무시). 실패하면 PowerError + heater_ok False."""
        with self.lock:
            return self._heater(False, "off")

    def heater_for(self, seconds, sleep=time.sleep, cancel=None):
        """공구 가열(방법 2) - 한도 안에서 켜고 끝나면(또는 예외 · 취소) 반드시 끈다."""
        s = float(seconds)
        if not 0 < s <= self.max_heat_s:
            raise PowerError("가열 시간 0 초과 %g s 이하" % self.max_heat_s)
        self.heater_on("tool_heat")
        t0 = time.monotonic()
        try:
            while time.monotonic() - t0 < s:
                if cancel and cancel():
                    break
                sleep(min(0.1, s))
        finally:
            self.heater_off()

    def pre_xy(self):
        """Motion.pre_xy 훅: X · Y 이동 전 히트건이 꺼졌음을 확인한다. 못 끄면 이동 거절."""
        try:
            self.heater_off()                  # 캐시와 무관하게 항상 보내고 RS 로 꺼짐을 확인한다
        except PowerError as e:
            raise MotionError(str(e), "heater") from e
        return True

    # ---- 일괄 -------------------------------------------------------
    def all_off(self, owners=None):
        """엔진 · 작업이 켠 릴레이를 전부 끈다(owners 를 주면 그 주체 것만). 히트건은 항상 끈다. 마지막 오류를 돌려준다."""
        err = None
        with self.lock:
            try:
                self.heater_off()
            except PowerError as e:
                err = e
            for n, ow in list(self.owners.items()):
                if owners is not None and ow not in owners:
                    continue
                try:
                    self.stage.relay_set(n, False)
                    self.owners.pop(n, None)
                except StageError as e:
                    err = PowerError(str(e)[:50])
        return err
