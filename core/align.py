"""align.py - 빈 판 사진(기준) → 그 촬영 사진 정렬. 기준 좌표계의 유일한 변환 M 을 만든다.

흐름: 고역 통과 → 위상 상관(초기 이동) → ECC 유클리드 → (잔차가 크면) ECC 원근 → 잔차가 작은 모델 선택.
판정(잔차)은 **판 영역 마스크 안의 강건 잔차**로 한다 - 전체 화면이 아니라서 헤드 · Z · 케이블 위치가 달라도 통과한다.
  잔차 = 1.4826·MAD(기준 고역 − 현재 고역(M·x)) / 1.4826·MAD(기준 고역)  (마스크 안, 프레임 밖 제외)
문턱 RESID_FAIL 은 tools/align_measure.py 가 truth 사진에서 실측한 값(CLAUDE.md "정렬 문턱")이다.
"""


import cv2
import numpy as np

from core import frames

SCALE = 0.25                  # 정렬은 1/4 해상도로 먼저
HP_SIGMA = 10.0               # 고역 통과(원본 − 가우시안 σ10) 뒤 σ1.5 로 다시 흐림
MAX_SHIFT_FRAC = 0.12         # 위상 상관 이동이 프레임의 이 비율보다 크면 정렬 실패
ERODE_FRAC = 0.03             # 마스크를 판 영역 짧은 변의 이 비율만큼 깎는다(경계 · 가장자리 제외)
HOMO_TRY = 0.60               # 유클리드 잔차가 이 이상일 때만 원근을 시도(통과선 0.65 바로 아래). 원근은 화면 전체 구조물에 끌려 판 안이 어긋날 수 있다 - 164607 에서 칩 1개를 놓침(실측)
HOMO_GAIN = 0.85              # 원근 잔차 < 유클리드 × 이 값 일 때만 원근 채택
RESID_FAIL = 0.65             # 이 이상이면 정렬 실패. 실측(tools/align_measure.py · 8장): 바른 0.40~0.55 · 10 px 틀림 0.75~0.86 · 20 px 1.04~1.16 · 40 px 1.34~1.47
RESID_EARLY = RESID_FAIL      # 통과하면 1/4 해상도 결과로 끝낸다(실패일 때만 1/2 해상도로 다시)


def _hp(gray):
    g = gray.astype(np.float32)
    return cv2.GaussianBlur(g - cv2.GaussianBlur(g, (0, 0), HP_SIGMA), (0, 0), 1.5)


def area_mask(shape, area, scale=1.0, erode_frac=ERODE_FRAC):
    """판 영역(기준 좌표 다각형) 마스크. 영역이 없으면 가장자리 8 px 를 뺀 전체."""
    h, w = shape[:2]
    m = np.zeros((h, w), np.uint8)
    if area is None or len(area) < 3:
        m[8:-8, 8:-8] = 255
        return m
    q = (np.asarray(area, np.float64) * scale)
    cv2.fillConvexPoly(m, np.round(q).astype(np.int32), 255)
    side = float(min(np.hypot(*(q[i] - q[(i + 1) % len(q)])) for i in range(len(q))))
    r = max(2, int(side * erode_frac))
    return cv2.erode(m, cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1)))


def _mad(x):
    return 1.4826 * float(np.median(np.abs(x - np.median(x))))


def residual(ha, hb, Ms, mask):
    """Ms(기준→현재, 3x3, 이 해상도 px) 의 강건 잔차."""
    h, w = ha.shape[:2]
    wb = cv2.warpPerspective(hb, Ms, (w, h), flags=cv2.INTER_LINEAR | cv2.WARP_INVERSE_MAP, borderValue=float("nan"))
    ok = (mask > 0) & np.isfinite(wb)
    if int(ok.sum()) < 500:
        return 9.9
    r = (ha - wb)[ok]
    sd = _mad(ha[ok]) + 1e-6
    return _mad(r) / sd


def _ecc(ha, hb, init3, motion, mask):
    crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-6)
    w0 = init3.astype(np.float32) if motion == cv2.MOTION_HOMOGRAPHY else init3[:2].astype(np.float32)
    # inputMask 는 두 번째 영상(현재)의 유효 화소다 - 기준 프레임 마스크를 초기 변환으로 현재 프레임에 옮겨 쓴다
    mi = None
    if mask is not None:
        mi = cv2.warpPerspective(mask, init3, (mask.shape[1], mask.shape[0]), flags=cv2.INTER_NEAREST)
    _cc, w1 = cv2.findTransformECC(ha, hb, w0, motion, crit, mi, 5)
    return np.vstack([w1, [0, 0, 1.0]]) if w1.shape[0] == 2 else np.asarray(w1, np.float64)


HEAD_BOX_K = 1.5              # 헤드 마커 상자를 이만큼 키우고
HEAD_BOX_PAD = 0.35           # 거기에 마커 한 변의 이만큼을 더 (그림자 · 모터 몸체 여유)


def head_box(quad, k=HEAD_BOX_K, pad=HEAD_BOX_PAD):
    """헤드 마커 모서리 4점 -> 넉넉하게 키운 사각형(4점). 중심에서 k 배 + 한 변의 pad 배 여유."""
    q = np.asarray(quad, np.float64).reshape(4, 2)
    c = q.mean(axis=0)
    side = float(np.mean([np.hypot(*(q[i] - q[(i + 1) % 4])) for i in range(4)]))
    out = c + (q - c) * k
    d = out - c
    n = np.hypot(d[:, 0], d[:, 1])[:, None] + 1e-9
    return out + d / n * (pad * side)


def _punch(mask, quads, shift, scale):
    """mask 에서 quads(원해상도 px)를 지운다. shift = 이 quads 가 있는 프레임 -> 마스크 프레임 이동(원해상도 px)."""
    for q in quads or []:
        P = (np.asarray(q, np.float64).reshape(-1, 2) + np.asarray(shift, np.float64)) * scale
        cv2.fillConvexPoly(mask, np.round(cv2.convexHull(P.astype(np.float32))).astype(np.int32), 0)
    return mask


def _at(ref, cur, area_ref, scale, allow_homography, mask_cur=None, mask_ref=None):
    a = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
    sa = cv2.resize(a, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sb = cv2.resize(b, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ha, hb = _hp(sa), _hp(sb)
    for h_, qs in ((ha, mask_ref), (hb, mask_cur)):
        for q in qs or []:                                   # 헤드 마커 상자는 위상 상관 · ECC 에서도 뺀다(마커가 커서 정렬을 마커 이동 쪽으로 끌고 간다)
            P = np.asarray(q, np.float64).reshape(-1, 2) * scale
            cv2.fillConvexPoly(h_, np.round(cv2.convexHull(P.astype(np.float32))).astype(np.int32), 0)
    mask = area_mask(ha.shape, area_ref, scale)
    win = cv2.createHanningWindow((ha.shape[1], ha.shape[0]), cv2.CV_32F)
    (dx0, dy0), _cc = cv2.phaseCorrelate(ha, hb, win)
    if mask_ref:
        _punch(mask, mask_ref, (0.0, 0.0), scale)                     # 빈 판 사진 안의 헤드 마커 상자(기준 프레임)
    if mask_cur:
        _punch(mask, mask_cur, (-dx0 / scale, -dy0 / scale), scale)   # 이 사진의 헤드 마커 상자: 초기 이동만큼 되돌려 기준 프레임으로
    out = {"ok": False, "model": "euclid", "Ms": np.eye(3), "resid": 9.9, "resid_euclid": 9.9, "resid_homography": None, "msg": ""}
    if abs(dx0) > MAX_SHIFT_FRAC * ha.shape[1] or abs(dy0) > MAX_SHIFT_FRAC * ha.shape[0]:
        out["msg"] = "이동 큼 · 위상 상관 %.0f, %.0f px" % (dx0 / scale, dy0 / scale)
        return out
    M0 = np.array([[1.0, 0, dx0], [0, 1.0, dy0], [0, 0, 1.0]])
    cands = []                                              # (잔차, 모델, Ms)

    def add(model, motion, init, m):
        try:
            Mx = _ecc(ha, hb, init, motion, m)
        except cv2.error:
            return None
        if not np.all(np.isfinite(Mx)):
            return None
        r = residual(ha, hb, Mx, mask)
        cands.append((r, model, Mx))
        return Mx

    Me = add("euclid", cv2.MOTION_EUCLIDEAN, M0, None)       # 화면 전체로 먼저(구조물이 많아 안정적)
    if Me is None:
        out["msg"] = "정밀 정렬 실패 · 이동만 보정"
        Me = M0
        cands.append((residual(ha, hb, M0, mask), "euclid", M0))
    re_ = min(c[0] for c in cands)
    out["resid_euclid"] = re_
    if allow_homography and re_ >= HOMO_TRY:
        # 카메라 기울기: 화면 전체로 맞춘 원근(판 영역만으로는 오히려 어긋남 - 실측), 채택은 판 영역 잔차로
        add("homography", cv2.MOTION_HOMOGRAPHY, Me, None)
        hs = [c for c in cands if c[1] == "homography"]
        if hs:
            out["resid_homography"] = min(c[0] for c in hs)
    best = min((c for c in cands if c[1] == "euclid"), key=lambda c: c[0])
    hs = [c for c in cands if c[1] == "homography"]
    if hs:
        h = min(hs, key=lambda c: c[0])
        if h[0] < best[0] * HOMO_GAIN:
            best = h
    out.update(Ms=best[2], resid=best[0], model=best[1])
    out["ok"] = bool(np.isfinite(out["resid"]) and out["resid"] < RESID_FAIL)
    if not out["ok"] and not out["msg"]:
        out["msg"] = "정렬 잔차 큼 · %.2f" % out["resid"]
    return out


def align(ref, cur, area_ref=None, scales=(SCALE, 0.5), allow_homography=True, ref_id="", tag=frames.SHOT, mask_cur=None, mask_ref=None):
    """기준 → 현재 정렬. mask_cur / mask_ref = 판 마스크에서 뺄 헤드 마커 상자들(각각 이 사진 · 빈 판 사진의 px 사각형). 반환 dict: ok · A(frames.Align) · resid · model · msg · resid_euclid · resid_homography.
    1/4 해상도로 먼저, 실패하거나 잔차가 크면 1/2 해상도로 다시 - 잔차가 더 작은 쪽."""
    best, best_s = None, SCALE
    for sc in scales:
        r = _at(ref, cur, area_ref, sc, allow_homography, mask_cur, mask_ref)
        if best is None or (r["ok"], not r["msg"], -r["resid"]) > (best["ok"], not best["msg"], -best["resid"]):
            best, best_s = r, sc
        if best["ok"] and best["resid"] < RESID_EARLY:
            break
    S = np.diag([best_s, best_s, 1.0])
    M = np.linalg.inv(S) @ best["Ms"] @ S                     # 원해상도 px
    A = frames.Align(M, tag, best["model"], best["resid"], ref_id)
    best.update(A=A, scale=best_s, theta_deg=A.theta_deg())
    return best
