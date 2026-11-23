"""핀 놓을 자리(find.pin_place): 윤곽 안에서 핀 사각형이 가장 넉넉히 들어가는 자리. 핀 10 × 2 mm · 여유 0.3 mm."""

import pytest

from app.vision.find import pin_place

PIN = (10.0, 2.0, 0.3)


def _rect(cx, cy, w, h):
    return [[cx - w / 2, cy - h / 2], [cx + w / 2, cy - h / 2], [cx + w / 2, cy + h / 2], [cx - w / 2, cy + h / 2]]


def _close(a, b, tol):
    return abs(a[0] - b[0]) <= tol and abs(a[1] - b[1]) <= tol


def test_rect_fits_at_center():
    r = pin_place(_rect(50, 50, 14, 8), (50, 50), (1, 0), *PIN)
    assert r["fits"] and _close(r["at"], (50, 50), 0.06), r
    assert r["clear"] == pytest.approx(2.05, abs=0.06)


def test_rect_too_small_stays_at_center():
    r = pin_place(_rect(50, 50, 9, 6), (50, 50), (1, 0), *PIN)
    assert not r["fits"] and _close(r["at"], (50, 50), 0.06), r
    assert r["inside"] == pytest.approx(0.90, abs=0.02) and r["clear"] == 0.0


def test_right_triangle_moves_to_wide_side():
    out = [[40, 40], [54, 40], [54, 48]]
    r = pin_place(out, (49.33, 42.67), (1, 0), *PIN)
    assert _close(r["at"], (48.95, 41.20), 0.1), r
    assert r["inside"] == pytest.approx(1.0, abs=0.002)
    assert r["clear"] == pytest.approx(0.14, abs=0.06) and not r["fits"], "다 들어가지만 여유 부족"


def test_trapezoid_moves_to_wide_side_and_fits():
    out = [[43, 40], [51, 40], [54, 46], [40, 46]]
    r = pin_place(out, (47.0, 43.3), (1, 0), *PIN)
    assert _close(r["at"], (47.0, 44.3), 0.1) and r["fits"], r
    assert r["clear"] == pytest.approx(0.65, abs=0.06)


def test_vertical_pin():
    r = pin_place(_rect(50, 50, 8, 14), (50, 50), (0, 1), *PIN)
    assert r["fits"] and _close(r["at"], (50, 50), 0.06), r
    r = pin_place(_rect(50, 50, 14, 8), (50, 50), (0, 1), *PIN)
    assert not r["fits"] and r["inside"] == pytest.approx(0.80, abs=0.02), r


def test_too_few_points():
    r = pin_place([[0, 0], [1, 1]], (3, 4), (1, 0), *PIN)
    assert r == {"at": [3.0, 4.0], "fits": False, "inside": 0.0, "clear": 0.0}
