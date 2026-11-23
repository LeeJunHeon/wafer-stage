"""보정 점 가중 보정(geometry.warp_shift · warp_inverse): c(q) = Σ w_i r_i ÷ Σ w_i · w_i = 1 ÷ |q − q_i|^2.
보정 점 자리에서는 정확히 r_i · 1개면 어디서나 r_1 · 가장 큰 보정량을 넘지 않음 · 스테이지 → 바탕 되풀이 왕복."""

import math

import numpy as np
import pytest

from app import geometry as geo


def test_no_points_no_shift():
    assert geo.warp_shift((10.0, 20.0), [], []) == (0.0, 0.0)
    assert geo.warp_inverse((10.0, 20.0), [], []) == (10.0, 20.0)


def test_one_point_shifts_everywhere():
    pts, res = [[50.0, 60.0]], [[1.2, -0.7]]
    for q in ((0, 0), (50, 60), (150, 10), (80.3, 140.9)):
        c = geo.warp_shift(q, pts, res)
        assert c == pytest.approx((1.2, -0.7), abs=1e-9)


def test_three_points_exact_at_points_and_bounded():
    pts = [[20.0, 30.0], [120.0, 40.0], [70.0, 130.0]]
    res = [[1.0, -2.0], [-0.5, 0.3], [2.5, 1.5]]
    for p, r in zip(pts, res):
        assert geo.warp_shift(p, pts, res) == pytest.approx(tuple(r), abs=1e-6)
    big = max(math.hypot(*r) for r in res)
    rng = np.random.default_rng(3)
    for q in rng.uniform(0, 160, (300, 2)):
        c = geo.warp_shift(q, pts, res)
        assert math.hypot(*c) <= big + 1e-9


def _spaced(rng, n, gap):
    out = []
    while len(out) < n:
        p = rng.uniform(0, 160, 2)
        if all(math.hypot(p[0] - q[0], p[1] - q[1]) >= gap for q in out):
            out.append([float(p[0]), float(p[1])])
    return out


def test_inverse_round_trip():
    """보정량 ≤ 3 mm · 점 간격 ≥ 10 mm 의 임의 점 200개: q → xy = q + c(q) → warp_inverse(xy) 가 q 와 0.01 mm 안."""
    rng = np.random.default_rng(11)
    for trial in range(5):
        pts = _spaced(rng, 6, 10.0)
        res = []
        for _ in pts:
            a, m = rng.uniform(0, 2 * math.pi), rng.uniform(0, 3.0)
            res.append([m * math.cos(a), m * math.sin(a)])
        for q in rng.uniform(0, 160, (40, 2)):
            c = geo.warp_shift(q, pts, res)
            xy = (q[0] + c[0], q[1] + c[1])
            back = geo.warp_inverse(xy, pts, res)
            assert math.hypot(back[0] - q[0], back[1] - q[1]) < 0.01, (trial, q, back)
