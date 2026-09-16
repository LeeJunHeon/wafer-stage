"""flat.py - 종이 기준 조명 평탄화 + 포화 진단.

한쪽에서 강한 빛이 들어오는 촬영 환경에서는 자동 노출이 반사광에 반응해 장마다
밝기가 크게 다르고(웨이퍼 안 평균 36~162), 조명 기울기 때문에 같은 사진 안에서도
한쪽이 어둡다. 검출 임계값을 아무리 맞춰도 사진이 매번 다르면 소용이 없다.

마커 사각형(감지영역) 안·웨이퍼 밖은 흰 종이다. 그 종이를 흰색 기준으로 삼아
채널별로 2차 다항식 조명면을 맞추고 그 면으로 나누면, 조명 기울기와 화이트밸런스가
한 번에 잡혀 종이가 목표 밝기(180)의 무채색이 된다. 웨이퍼·칩·그림자는 반복
최소제곱(잔차가 큰 화소를 버리고 다시 맞추기)으로 자연히 빠진다.

검출 로직(detect.py)은 건드리지 않는다 - 이 모듈은 detect 에 들어가기 전의 사진을
고르게 만들 뿐이다.
"""

import cv2
import numpy as np

PAPER_TARGET = 180.0      # 보정 뒤 종이의 목표 밝기(채널마다 같다 = 무채색)
SAT_LEVEL = 254           # 이 이상이면 포화(정보 없음)로 본다
PAPER_MAX = 250           # 종이 후보는 포화되지 않은 화소만
PAPER_MIN_FRAC = 0.20     # 종이 화소가 감지영역의 이 비율 미만이면 보정하지 않는다
ITERS = 3                 # 반복 최소제곱 횟수
RESID_KEEP = 0.12         # 상대 잔차 |I - fit| / fit 가 이 이하인 화소만 남긴다
FIT_STRIDE = 4            # 맞출 때는 이 간격으로 뽑아 쓴다(속도. 2차 면은 이걸로 충분)
WAFER_SAT_WARN = 10.0     # 웨이퍼 안 포화 비율(%)이 이보다 크면 경고


def _design(xn, yn):
    """2차 다항식 설계행렬 [1, x, y, x², xy, y²]. 좌표는 -1~1 로 정규화된 값."""
    return np.stack([np.ones_like(xn), xn, yn, xn * xn, xn * yn, yn * yn], axis=-1)


def _norm_coords(h, w, rect):
    """감지영역을 -1~1 로 두는 정규화 좌표(전체 프레임 크기로). 마커는 사각형
    경계에 걸쳐 있으므로 사각형 바깥도 같은 면으로 외삽해 보정한다."""
    u0, v0, u1, v1 = rect
    xs = (np.arange(w, dtype=np.float32) - (u0 + u1) / 2.0) / max((u1 - u0) / 2.0, 1.0)
    ys = (np.arange(h, dtype=np.float32) - (v0 + v1) / 2.0) / max((v1 - v0) / 2.0, 1.0)
    return np.meshgrid(xs, ys)


def paper_mask(crop):
    """종이 후보: 포화되지 않았고(<250) 충분히 밝은 화소(밝은 쪽 상위 절반)."""
    mx = crop.max(axis=2)
    mn = crop.min(axis=2)
    ok = mx < PAPER_MAX
    if not ok.any():
        return ok
    # '충분히 밝다' 는 그 사진의 밝은 쪽을 기준으로 정한다 - 절대값으로 자르면
    # 노출이 낮은 장에서 종이가 전부 빠진다.
    thr = 0.6 * float(np.percentile(mn[ok], 97))
    return ok & (mn >= thr)


def fit_surface(crop, rect_in_crop, mask):
    """채널별 2차 조명면을 반복 최소제곱으로 맞춘다.

    돌려주는 것: (coef[3,6], 최종 종이 마스크). 종이가 모자라면 (None, mask).
    """
    h, w = crop.shape[:2]
    X, Y = _norm_coords(h, w, rect_in_crop)
    sub = (slice(None, None, FIT_STRIDE), slice(None, None, FIT_STRIDE))
    A_all = _design(X[sub], Y[sub]).reshape(-1, 6).astype(np.float64)
    I_all = crop[sub].reshape(-1, 3).astype(np.float64)
    keep = mask[sub].reshape(-1).copy()
    coef = None
    for _ in range(ITERS):
        if keep.sum() < 60:
            return None, mask
        A = A_all[keep]
        coef = np.linalg.lstsq(A, I_all[keep], rcond=None)[0].T     # [3, 6]
        fit = A_all @ coef.T                                        # [N, 3]
        fit = np.maximum(fit, 1.0)
        rel = np.abs(I_all - fit) / fit
        keep = keep & (rel.max(axis=1) <= RESID_KEEP)
    return coef, keep.reshape(mask[sub].shape)


def flatten(bgr, rect, params=None):
    """감지영역 rect=(u0,v0,u1,v1) 안의 종이를 기준으로 전체 프레임을 평탄화한다.

    (보정된 이미지, info). 보정하지 못하면 원본을 그대로 돌려주고 info["applied"]
    가 False, info["warning"] 에 이유가 있다. 원본은 건드리지 않는다.
    """
    info = {"applied": False, "warning": "", "paper_frac": 0.0,
            "paper_rgb_before": None, "paper_rgb_after": None, "target": PAPER_TARGET}
    if bgr is None or rect is None:
        info["warning"] = "감지영역 없음 · 평탄화 건너뜀"
        return bgr, info
    u0, v0, u1, v1 = [int(v) for v in rect]
    crop = bgr[v0:v1, u0:u1]
    if crop.size == 0:
        info["warning"] = "감지영역 없음 · 평탄화 건너뜀"
        return bgr, info
    mask = paper_mask(crop)
    coef, kept = fit_surface(crop, (0, 0, u1 - u0, v1 - v0), mask)
    # 종이 비율은 최종 반복에서 남은 화소로 센다(웨이퍼·칩·그림자를 뺀 진짜 종이).
    frac = float(kept.mean()) if kept.size else 0.0
    info["paper_frac"] = round(frac, 3)
    if coef is None or frac < PAPER_MIN_FRAC:
        info["warning"] = ("종이 화소 %.0f%% < %.0f%% · 평탄화 건너뜀"
                           % (frac * 100.0, PAPER_MIN_FRAC * 100.0))
        return bgr, info

    sub = (slice(None, None, FIT_STRIDE), slice(None, None, FIT_STRIDE))
    before = crop[sub][kept].reshape(-1, 3).mean(axis=0)
    info["paper_rgb_before"] = [round(float(v), 1) for v in before[::-1]]   # BGR -> RGB

    h, w = bgr.shape[:2]
    X, Y = _norm_coords(h, w, (u0, v0, u1, v1))
    A = _design(X, Y).reshape(-1, 6)
    surf = (A @ coef.T).reshape(h, w, 3)
    surf = np.maximum(surf, 8.0)                     # 0 나눗셈·극단 증폭 방지
    out = bgr.astype(np.float32) * (PAPER_TARGET / surf.astype(np.float32))
    out = np.clip(out + 0.5, 0, 255).astype(np.uint8)

    after = out[v0:v1, u0:u1][sub][kept].reshape(-1, 3).mean(axis=0)
    info["paper_rgb_after"] = [round(float(v), 1) for v in after[::-1]]
    info["applied"] = True
    return out, info


# --------------------------------------------------------------------------
# 진단
# --------------------------------------------------------------------------
def saturation_frac(bgr, rect=None):
    """어느 채널이든 SAT_LEVEL 이상인 화소 비율(0~1). rect 가 있으면 그 안에서만."""
    if bgr is None:
        return 0.0
    img = bgr
    if rect is not None:
        u0, v0, u1, v1 = [int(v) for v in rect]
        img = bgr[v0:v1, u0:u1]
    if img.size == 0:
        return 0.0
    return float((img.max(axis=2) >= SAT_LEVEL).mean())


def wafer_saturation_frac(bgr, center_px, major_px, minor_px, theta_deg):
    """웨이퍼 타원 안에서 포화된 화소 비율(0~1). 좌표는 전체 프레임 기준."""
    if bgr is None or not major_px:
        return 0.0
    h, w = bgr.shape[:2]
    m = np.zeros((h, w), np.uint8)
    cv2.ellipse(m, (int(round(center_px[0])), int(round(center_px[1]))),
                (max(1, int(major_px / 2)), max(1, int((minor_px or major_px) / 2))),
                float(theta_deg or 0.0), 0, 360, 255, -1)
    inside = m > 0
    if not inside.any():
        return 0.0
    return float((bgr.max(axis=2)[inside] >= SAT_LEVEL).mean())


def mean_brightness(bgr):
    if bgr is None or bgr.size == 0:
        return 0.0
    return float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())
