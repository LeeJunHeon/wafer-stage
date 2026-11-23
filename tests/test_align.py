"""빈 판 사진 정렬(refdiff.align): 두 사진이 실제로 다른 곳(샘플)을 빼고 맞춘다.
합성 사진 - 판 무늬(완만한 밝기 + 가는 긁힘) · 샘플 0 · 1 · 9개 · 사진 밀림. 고치기 전 코드는 샘플 1개부터 ok False · 잔차 0.86~1.00 이었다."""

import cv2
import numpy as np
import pytest

from app.vision import refdiff

N = 800


def _board(seed):
    """판 = 밝기 110 + 10·sin(x/170) + 8·cos(y/210) + 임의 직선 90개(굵기 1 · 길이 30~180 · 밝기 +12~+40)."""
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:N, 0:N].astype(np.float32)
    img = 110.0 + 10.0 * np.sin(xx / 170.0) + 8.0 * np.cos(yy / 210.0)
    layer = np.zeros((N, N), np.uint8)
    for _ in range(90):
        x0, y0 = rng.uniform(0, N, 2)
        ang, ln = rng.uniform(0, np.pi), rng.uniform(30, 180)
        x1, y1 = x0 + ln * np.cos(ang), y0 + ln * np.sin(ang)
        cv2.line(layer, (int(x0), int(y0)), (int(x1), int(y1)), int(rng.uniform(12, 40)), 1, cv2.LINE_AA)
    return img + layer.astype(np.float32)


def _photo(board, shift, n_samples, noise_seed, place_seed=7):
    """판을 shift 만큼 옮기고(BORDER_REFLECT) 샘플(75 × 40 px · 3 × 3 자리 ± 20 · 밝기 165 / 65 번갈아)을 얹은 뒤 잡음 σ 2.0 → BGR."""
    M = np.float32([[1, 0, shift[0]], [0, 1, shift[1]]])
    img = cv2.warpAffine(board, M, (N, N), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    rng = np.random.default_rng(place_seed)
    spots = [(150 + 230 * c, 150 + 230 * r) for r in range(3) for c in range(3)]
    for k, (cx, cy) in enumerate(spots[:n_samples]):
        jx, jy = rng.uniform(-20, 20, 2)
        x0, y0 = int(cx + jx - 37), int(cy + jy - 20)
        img[y0:y0 + 40, x0:x0 + 75] = 165.0 if k % 2 == 0 else 65.0
    img = img + np.random.default_rng(noise_seed).normal(0, 2.0, img.shape).astype(np.float32)
    g = np.clip(img, 0, 255).astype(np.uint8)
    return cv2.cvtColor(g, cv2.COLOR_GRAY2BGR)


@pytest.mark.parametrize("n_samples", [0, 1, 9])
@pytest.mark.parametrize("shift", [(0, 0), (3, -2), (12, 7)])
def test_align_ignores_samples(n_samples, shift):
    board = _board(1)
    ref = _photo(board, (0, 0), 0, noise_seed=100)
    cur = _photo(board, shift, n_samples, noise_seed=200)
    a = refdiff.align(ref, cur, None)
    assert "excl" in a
    assert a["ok"], a
    assert abs(a["dx"] - shift[0]) < 1.0 and abs(a["dy"] - shift[1]) < 1.0, a
    assert a["resid"] < 0.55, a


@pytest.mark.parametrize("n_samples", [0, 9])
def test_align_fails_on_different_plate(n_samples):
    ref = _photo(_board(1), (0, 0), 0, noise_seed=100)
    cur = _photo(_board(2), (0, 0), n_samples, noise_seed=200)
    a = refdiff.align(ref, cur, None)
    assert not a["ok"], a
