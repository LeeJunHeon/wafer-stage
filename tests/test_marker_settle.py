"""헤드 마커 읽기(prep._marker_pair): 가라앉을 때까지 읽고 최근 3장의 가운데 값 · 끝까지 흔들리면 marker_settle_s 뒤 거절 · 안 보이면 None.
시험용 카메라의 마커를 world.marker_shake 로 흔든다(프레임마다 더 옮길 양 mm)."""

import itertools
import math
import time

import pytest

from app.safety import Rejected


def _tol_px(ctrl):
    return ctrl.prep._mm_px(ctrl.cfg["marker_pair_mm"])


def test_marker_settles_after_shaking(ready_ctrl):
    ctrl = ready_ctrl(marker_settle_s=5.0)
    head = ctrl.cfg["photo_head_xy"]
    ctrl.goto(head[0], head[1])
    base = ctrl.prep._marker_pair()
    assert base is not None
    t_end = time.monotonic() + 0.4                                    # 처음 0.4 s 동안 ±2 mm 로 흔들리다 가라앉는다
    flip = itertools.cycle((2.0, -2.0))
    ctrl.world.marker_shake = lambda: (next(flip), 0.0) if time.monotonic() < t_end else (0.0, 0.0)
    m = ctrl.prep._marker_pair()
    assert m is not None
    assert math.hypot(m[0] - base[0], m[1] - base[1]) <= _tol_px(ctrl), "가라앉은 값(흔들리기 전 자리 ± 허용)"


def test_marker_keeps_shaking_is_rejected_after_limit(ready_ctrl):
    ctrl = ready_ctrl(marker_settle_s=0.6)
    head = ctrl.cfg["photo_head_xy"]
    ctrl.goto(head[0], head[1])
    flip = itertools.cycle((2.0, -2.0))
    ctrl.world.marker_shake = lambda: (next(flip), 0.0)
    t0 = time.monotonic()
    with pytest.raises(Rejected, match="마커 위치 흔들림"):
        ctrl.prep._marker_pair()
    assert time.monotonic() - t0 >= 0.6, "한도까지 기다린 뒤 거절"
    with pytest.raises(Rejected, match="마커 위치 흔들림"):
        ctrl.run_command("marker_learn")                             # 등록도 같은 방법 - 흔들리면 저장하지 않는다


def test_marker_not_visible_returns_none(ready_ctrl):
    ctrl = ready_ctrl(marker_settle_s=0.5)
    ctrl.goto(125.0, 240.0)                                          # 마커가 사진 밖으로 나가는 자리
    assert ctrl.prep._marker_pair() is None
