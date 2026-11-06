"""framesrc.py - 카메라 계층: 프레임마다 단조 시각을 붙이고 "이 시각 이후 프레임" 요청을 받는다.

드라이버 큐에 이동 전 프레임이 남아 있으면 이동 뒤 촬영이 옛 장면을 쓴다(헤드 마커가 옛 자리에 보인다). 그래서
- 받은 프레임마다 grab 시각(단조)을 붙이고,
- frame_after(t) 는 t 이후에 grab 한 프레임이 min_fresh 장 이상 쌓일 때까지 버린 뒤 그다음 프레임을 돌려준다
  (큐 깊이보다 min_fresh 가 커야 한다 - 기본 5, 카메라 큐 깊이는 설정으로 맞춘다).
촬영 · 위치 확인 · 이동 후 재확인 · 헤드 위치 보정 · 축 방향 측정이 모두 이 함수를 쓴다.
"""

import time
from dataclasses import dataclass


class StaleFrameError(Exception):
    pass


@dataclass
class Stamped:
    frame: object
    t: float                      # grab 단조 시각
    seq: int                      # 받은 순번


class FrameSource:
    """cam: grab() -> bool, retrieve() -> ndarray | None 를 가진 객체(core.camera.Camera 또는 합성 카메라)."""

    def __init__(self, cam, clock=time.monotonic, min_fresh=5, period_s=0.0):
        self.cam = cam
        self.clock = clock
        self.min_fresh = int(min_fresh)
        self.period_s = float(period_s)
        self.seq = 0

    def latest(self):
        """지금 큐에서 꺼낸 프레임(시각 표시 포함) - 미리보기용."""
        t = self.clock()
        if not self.cam.grab():
            return None
        f = self.cam.retrieve()
        if f is None:
            return None
        self.seq += 1
        return Stamped(f, t, self.seq)

    def frame_after(self, t_after, settle_s=0.0, timeout_s=10.0, min_fresh=None):
        """t_after + settle_s 이후에 grab 한 프레임이 min_fresh 장 쌓인 뒤의 프레임. 못 받으면 StaleFrameError."""
        need = self.min_fresh if min_fresh is None else int(min_fresh)
        t_ok = t_after + settle_s
        t_end = self.clock() + timeout_s
        fresh = 0
        last = None
        while self.clock() < t_end:
            t = self.clock()
            if not self.cam.grab():
                time.sleep(0.01)
                continue
            if t < t_ok:
                continue                                   # 아직 이동 직후 - 버린다
            fresh += 1
            if fresh >= need:
                f = self.cam.retrieve()
                if f is not None:
                    self.seq += 1
                    last = Stamped(f, t, self.seq)
                    return last
            if self.period_s:
                time.sleep(self.period_s)
        raise StaleFrameError("새 프레임을 못 받음 · 이동 뒤 %.1f s 안에 %d장 필요" % (timeout_s, need))
