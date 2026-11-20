"""fake_heatgun.py - 시험용 히트건: 송풍(바람) · 열선 릴레이 둘에 달린 히트건. 실제 히트건의 켜는 순서 인터락을 흉내 낸다.

  - 열선과 송풍이 둘 다 켜졌을 때만 가열한다(heating()). 세기 = 열선 출력 %(펌웨어 V12 hp)의 시간 평균(power() 0~1 - 열 모델이 본다).
  - 송풍이 열선 없이 먼저 켜진 상태에서 열선이 켜지면 "인터락 오류": 둘 다 꺼질 때까지 가열하지 않는다(errors 에 남는다).
  - 열선을 먼저 켜고 송풍을 켠 뒤에는 송풍을 켜 둔 채 열선만 껐다 켜도 괜찮다.
  - 송풍이 열선 없이 켜진 순간은 fan_alone 에 남는다 - 프로그램에는 그런 길이 없어야 한다(conftest 가 시험마다 끝에 확인한다).
가짜 보드(FakeV11)가 릴레이를 바꿀 때마다 update(릴레이 목록) 을 부른다. 시각은 열 모델과 같은 시계(시간 배율 적용)다.
"""

import threading
import time


class FakeHeatGun:
    def __init__(self, fan=1, element=10, clock=None):
        self.fan_n, self.el_n = int(fan), int(element)
        self.clock = clock or time.monotonic
        self.fan = self.element = False
        self.fan_first = False          # 이번에 송풍이 열선 없이 켜졌다(그 뒤 열선을 켜면 인터락 오류)
        self.fault = False              # 인터락 오류 상태(둘 다 꺼질 때까지 가열 안 함)
        self.errors = []                # [(시각, 글)]
        self.fan_alone = []             # 송풍이 열선 없이 켜진 시각들
        self.log = []                   # [(시각, 송풍, 열선)] - 바뀔 때마다
        self.pct = 100                  # 열선 출력 %(V11 보드 · 모르면 100 = 켜짐 그대로)
        self._lock = threading.Lock()

    def update(self, relays, pct=None):
        if pct is not None:
            self.pct = float(pct)                                   # V14 ~ V16 은 0.1 % 단위
        fan, el = bool(relays[self.fan_n - 1]), bool(relays[self.el_n - 1])
        with self._lock:
            if (fan, el) == (self.fan, self.element):
                return
            now = self.clock()
            if fan and not self.fan and not el:
                self.fan_first = True
                self.fan_alone.append(now)
            if not fan:
                self.fan_first = False
            if el and not self.element and fan and self.fan_first:
                self.fault = True
                self.errors.append((now, "인터락 오류 · 송풍 먼저 켜진 뒤 열선 켬"))
            if not fan and not el:
                self.fault = False
            self.fan, self.element = fan, el
            self.log.append((now, fan, el))

    def heating(self) -> bool:
        return self.fan and self.element and not self.fault

    def power(self) -> float:
        """가열 세기 0~1: 열선 출력 % 의 시간 평균(SSR 을 주기마다 % 만큼 켠다)."""
        return self.pct / 100.0 if self.heating() else 0.0

    def problems(self):
        """시험 끝에 확인하는 것: 인터락 오류 · 송풍 단독 켜짐."""
        out = ["%.2f %s" % e for e in self.errors]
        out += ["%.2f 송풍 단독 켜짐" % t for t in self.fan_alone]
        return out
