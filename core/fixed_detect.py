"""fixed_detect.py - 고정 환경(천장 고정 카메라 · 알루미늄 판 위 샘플 · 3x5 배치) 전용 검출기.

core/detect.py(웨이퍼·철판 일반 검출)는 markers 모드 전용으로 그대로 두고, fixed 모드는 이
모듈이 맡는다(설정 fixed.detector = "fixed"|"legacy"). 서버·화면을 모른다.

파이프라인(2026-11-05 실장 사진 분석 · data/truth 회귀로 튜닝):
  1) Lab 채널별 잔차 = 가우시안(σ) 블러 - medianBlur(칩 긴 변 x bg_k_chips) → 채널 강건 z(중앙값·MAD x1.4826,
     MAD 하한 1.0 = 8비트 Lab 양자화 한 단계) → d = √Σz²  (회색·반투명 칩이 판과 색·밝기가 거의 같아도 채널
     잔차를 합치면 드러난다. 판 긁힘은 얇고 길어 모양 필터가 거른다.)
  2) d > seed → open 3x3 → close 5x5 → 연결요소 → 모양 필터(면적 · 볼록도 · 종횡비 · 최소외접사각형 채움률)
  3) 칩 크기: 강한 후보(볼록도 > 0.85) 긴 변의 중앙값으로 추정해 그 크기로 다시 맞춰 재검출
  4) 배치 사전정보(layout=grid): 후보 중심으로 rows x cols 격자(아핀 o + i·u + j·v)를 RANSAC 으로 맞추고
     최소제곱으로 정련 → 칸 위치 예측. 칸에 후보가 없으면 칸 창(칩 긴 변 ±0.9배) 안에서 낮은 기준
     (local_seeds)으로 재탐색 → 그래도 없으면 같은 창에서 Lab Canny 닫기·채우기 → 그래도 없으면 예측 위치로
     샘플을 낸다(weak, source = "predicted"). 격자를 못 맞추면 free 로 떨어진다.
  출력 샘플 dict 는 detect 와 호환(x_px, y_px, vertices_px, shape, area_mm2, weak, strength, _poly, _cnt …)
  + source("detected"|"local"|"predicted").
"""

import math
import time
from dataclasses import dataclass, field

import cv2
import numpy as np

from core import detect as _legacy          # DetectResult / Wafer 껍데기와 annotate 를 재사용한다(수정 없음)

DEFAULTS = {
    "detector": "fixed",
    "seed": 4.5,
    "local_seeds": [3.5, 2.8, 2.2],
    "bg_k_chips": 3.0,
    "blur_sigma": 1.5,
    "min_solidity": 0.75,
    "max_aspect": 4.5,
    "min_fill": 0.45,
    "area_ratio": [0.25, 3.0],
    "work_long_px": 60,
    "chip_mm": 11.0,
    "layout": "auto",
    "grid_min_ratio": 0.8,          # auto: 격자 인라이어 / 후보 이 비율 이상(그리고 인라이어 >= rows*cols*0.6)일 때만 격자 사용
    "grid_rows": 3,
    "grid_cols": 5,
    "emit_predicted": True,
    "use_ref": True,
    # 빈 판 기준 차이 검출(core/refdiff.py)
    "ref_seed": 5.0,                # 차이 d 이진화 임계
    "ref_faint": 2.5,               # 약한 칩 재탐색(σ2 로 흐린 d) 임계
    "ref_open": 5,
    "ref_close": 9,
    "ref_area_ratio": [0.35, 3.5],
    "ref_min_solidity": 0.7,
    "ref_max_aspect": 5.0,
    "ref_thin_k": 0.10,             # 가는 선(긁힘) 떼어 내기: 원판 열기 반지름 = 작업 해상도 칩 긴 변 x k(0 = 끔)
    "ref_edge_k": 0.4,              # 감지 영역 경계에서 칩 길이의 이 배 안에 닿은 후보는 버린다
    "ref_check_blobs": 6,           # 기준 점검: 칩 크기 범위 밖 큰 차이 덩어리가 이보다 많으면 다시 촬영 권장
    "ref_check_resid": 0.6,         # 기준 점검: 정렬 잔차(바른 정렬 0.23~0.47, 틀린 정렬 ≥ 0.87)가 이보다 크면 다시 촬영 권장
    "cam_shift_warn_mm": 2.0,       # 카메라 이동 경고
    "cam_shift_max_mm": 10.0,       # 이보다 크면 좌표를 만들지 않는다
}
MAD_FLOOR = 1.0            # 채널 잡음 σ 하한(8비트 Lab 양자화 한 단계)
STRONG_SOLIDITY = 0.85     # 칩 크기 추정에 쓰는 후보의 볼록도
GRID_TOL = 0.25            # 격자 인라이어: 잔차 < 이 x 최소 간격
TIGHTEN_K = (1.4, 1.8, 2.4, 3.2)   # 칸 재탐색의 마지막 단계: 후광으로 덩어리가 부푼 칩은 임계를 올려 핵만 잡는다
WIN_K = 0.9                # 칸 창 반폭 = 칩 긴 변 x 이 값
MIN_GRID_INLIERS = 6


def Lw_real_tmp(L_orig, s_real):
    """칩 긴 변(원본 px)을 작업 해상도 px 로."""
    return L_orig * s_real


def decide_grid(centers, cfg, area_poly=None):
    """배치 판단. centers = 후보 중심(작업 좌표). 반환 (grid | None, 안내 문구).

    layout auto: 격자 맞춤의 인라이어 비율(후보 대비)이 grid_min_ratio 이상이고 인라이어가 rows*cols*0.6 이상일 때만 격자를 쓴다.
    grid 를 명시해도 이 조건을 못 넘으면 격자 예측(predicted)은 내지 않고 자유 배치로 돌아간다(경고)."""
    layout = str(cfg["layout"])
    if layout not in ("grid", "auto"):
        return None, ""
    rows, cols = int(cfg["grid_rows"]), int(cfg["grid_cols"])
    g = fit_grid(centers, rows, cols, area_poly) if len(centers) >= 4 else None
    need = rows * cols * 0.6
    if g is not None and g["n_inl"] >= float(cfg["grid_min_ratio"]) * len(centers) and g["n_inl"] >= need:
        return g, ""
    why = "인라이어 %s/%d · 기준 비율 %.0f%% · 최소 %.0f" % ("-" if g is None else g["n_inl"], len(centers),
                                                      float(cfg["grid_min_ratio"]) * 100, need)
    return None, ("격자 맞춤 못 넘음(%s) · 자유 배치로 검출" % why)


def cfg_of(params):
    c = dict(DEFAULTS)
    c.update((params or {}).get("fixed") or {})
    return c


@dataclass
class FixedResult(_legacy.DetectResult):
    """detect.DetectResult 와 같은 모양 + 진단."""
    diag: dict = field(default_factory=dict)


def _odd(n):
    n = int(round(n))
    return max(3, n | 1)


def _lab8(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2LAB)


def _phase_shift(a_gray, b_gray):
    """b 를 a 에 맞추는 (dx, dy). 위상 상관(작은 이동용)."""
    (dx, dy), resp = cv2.phaseCorrelate(np.float32(a_gray), np.float32(b_gray))
    return dx, dy, resp


def residual_d(work, valid, k_bg, sigma, ref=None):
    """Lab 채널 잔차를 강건 z 로 나눠 합친 d(√Σz²). 반환: d(float32), 채널 σ 목록.

    ref 가 있으면(빈 판 기준 사진, 같은 크기·정렬됨) 배경을 medianBlur 대신 그 사진으로 쓴다.
    """
    lab = _lab8(work)
    labr = _lab8(ref) if ref is not None else None
    h, w = work.shape[:2]
    d2 = np.zeros((h, w), np.float32)
    sig = []
    vm = valid > 0
    for c in range(3):
        ch = lab[:, :, c]
        blur = cv2.GaussianBlur(ch.astype(np.float32), (0, 0), sigma)
        if labr is not None:
            bg = cv2.GaussianBlur(labr[:, :, c].astype(np.float32), (0, 0), sigma)
        else:
            bg = cv2.medianBlur(np.clip(np.rint(blur), 0, 255).astype(np.uint8), k_bg).astype(np.float32)
        r = blur - bg
        sel = r[vm] if vm.any() else r.ravel()
        med = float(np.median(sel))
        mad = float(np.median(np.abs(sel - med))) * 1.4826
        s = max(MAD_FLOOR, mad)
        z = (r - med) / s
        d2 += z * z
        sig.append(round(s, 2))
    d = np.sqrt(d2)
    d[~vm] = 0.0
    return d, sig


def _shape_of(cnt_pts, area):
    """윤곽 → 모양 이름 · 꼭짓점. detect 와 같은 기준(tri_fill 우선)을 단순화."""
    hull = cv2.convexHull(cnt_pts)
    peri = cv2.arcLength(hull, True)
    approx = cv2.approxPolyDP(hull, 0.04 * peri, True).reshape(-1, 2)
    nv = len(approx)
    shape = "triangle" if nv == 3 else ("quad" if nv == 4 else "polygon%d" % nv)
    return shape, approx


def components(mask, d, cfg, exp_area, region=None):
    """마스크(0/255) → 모양 필터를 통과한 후보 목록(작업 해상도 좌표).

    region = (x0, y0, x1, y1) 이면 그 창으로 자른 뒤 찾고 좌표는 전체 기준으로 돌려준다.
    """
    x0 = y0 = 0
    m = mask
    if region is not None:
        x0, y0, x1, y1 = region
        m = mask[y0:y1, x0:x1]
    m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, lab, stats, cent = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    lo, hi = [float(v) for v in cfg["area_ratio"]]
    out = []
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area < lo * exp_area or area > hi * exp_area:
            continue
        bx, by, bw, bh = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP,
                                                    cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
        sub = (lab[by:by + bh, bx:bx + bw] == i).astype(np.uint8)
        cs, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cs:
            continue
        cnt = max(cs, key=cv2.contourArea)
        pts = cnt.reshape(-1, 2) + [bx + x0, by + y0]
        hull = cv2.convexHull(pts.astype(np.int32))
        ha = cv2.contourArea(hull)
        if ha < 1:
            continue
        sol = area / ha
        (rcx, rcy), (rw, rh), ang = cv2.minAreaRect(hull)
        long_, short_ = max(rw, rh), max(1e-6, min(rw, rh))
        fill = area / max(1e-6, rw * rh)
        asp = long_ / short_
        ok = (sol >= cfg["min_solidity"] and asp <= cfg["max_aspect"] and fill >= cfg["min_fill"])
        ys, xs = np.nonzero(sub)
        strength = float(np.median(d[ys + by + y0, xs + bx + x0]))
        out.append({"cx": float(cent[i][0] + x0), "cy": float(cent[i][1] + y0), "area": area,
                    "long": long_, "short": short_, "sol": sol, "fill": fill, "asp": asp,
                    "ok": ok, "strength": strength, "hull": hull.reshape(-1, 2), "ang": float(ang)})
    return out


def _good(cands):
    return [c for c in cands if c["ok"]]


# --------------------------------------------------------------------------
# 격자 맞춤
# --------------------------------------------------------------------------
def fit_grid(pts, rows, cols, area_poly=None, tol=GRID_TOL):
    """후보 중심 pts(N,2) → 아핀 격자 o + i·u + j·v. 반환 dict | None.

    dict: o, u, v (np.array 2), slots[(i, j)] = 예측 위치, n_inl, rms.
    i 는 cols 방향(또는 rows 방향 - 격자 방향은 화면 가로·세로 어느 쪽이든 된다), 칸 번호는 0 부터.
    """
    P = np.asarray(pts, np.float64)
    n = len(P)
    if n < 4:
        return None
    D = np.linalg.norm(P[:, None] - P[None], axis=2)
    np.fill_diagonal(D, np.inf)
    med = float(np.median(D.min(axis=1)))
    ok_pair = (D > 0.6 * med) & (D < 1.8 * med)
    big, small = max(rows, cols), min(rows, cols)
    best = None
    for a in range(n):
        nb = np.nonzero(ok_pair[a])[0]
        for bi in nb:
            u = P[bi] - P[a]
            for ci in nb:
                if ci <= bi:
                    continue
                v = P[ci] - P[a]
                cr = abs(u[0] * v[1] - u[1] * v[0]) / (np.linalg.norm(u) * np.linalg.norm(v))
                if cr < 0.5:
                    continue
                M = np.array([u, v]).T
                ij = np.linalg.solve(M, (P - P[a]).T).T
                rij = np.rint(ij)
                res = np.linalg.norm((P - P[a]) - rij @ M.T, axis=1)
                inl = res < tol * min(np.linalg.norm(u), np.linalg.norm(v))
                if inl.sum() < 4:
                    continue
                sp_i = rij[inl, 0].max() - rij[inl, 0].min()
                sp_j = rij[inl, 1].max() - rij[inl, 1].min()
                if not ((sp_i <= cols - 1 and sp_j <= rows - 1) or (sp_i <= rows - 1 and sp_j <= cols - 1)):
                    continue
                # 같은 칸에 둘 이상이면 인라이어 수에서 뺀다
                keys = {(int(r[0]), int(r[1])) for r in rij[inl]}
                score = len(keys) - 0.01 * float(res[inl].mean()) / med
                if best is None or score > best[0]:
                    best = (score, a, u.copy(), v.copy())
    if best is None:
        return None
    _s, a, u, v = best
    o = P[a].copy()
    for _ in range(3):                       # 최소제곱 정련
        M = np.array([u, v]).T
        rij = np.rint(np.linalg.solve(M, (P - o).T).T)
        res = np.linalg.norm((P - o) - rij @ M.T, axis=1)
        inl = res < tol * min(np.linalg.norm(u), np.linalg.norm(v))
        if inl.sum() < 4:
            return None
        A = np.column_stack([np.ones(inl.sum()), rij[inl, 0], rij[inl, 1]])
        sol, *_ = np.linalg.lstsq(A, P[inl], rcond=None)
        o, u, v = sol[0], sol[1], sol[2]
    M = np.array([u, v]).T
    rij = np.rint(np.linalg.solve(M, (P - o).T).T)
    res = np.linalg.norm((P - o) - rij @ M.T, axis=1)
    inl = res < tol * min(np.linalg.norm(u), np.linalg.norm(v))
    if inl.sum() < MIN_GRID_INLIERS:
        return None
    i_min, i_max = rij[inl, 0].min(), rij[inl, 0].max()
    j_min, j_max = rij[inl, 1].min(), rij[inl, 1].max()
    # 칸 번호 배치: 방향 두 가지 x 오프셋 중 칸이 영역 안에 가장 많이 들어가는 것.
    best2 = None
    for ni, nj in ((cols, rows), (rows, cols)):
        if i_max - i_min > ni - 1 or j_max - j_min > nj - 1:
            continue
        for i0 in range(int(i_max) - ni + 1, int(i_min) + 1):
            for j0 in range(int(j_max) - nj + 1, int(j_min) + 1):
                cent = o + u * (i0 + (ni - 1) / 2.0) + v * (j0 + (nj - 1) / 2.0)
                inside = 0
                if area_poly is not None:
                    for ii in range(ni):
                        for jj in range(nj):
                            p = o + u * (i0 + ii) + v * (j0 + jj)
                            if cv2.pointPolygonTest(area_poly, (float(p[0]), float(p[1])), False) >= 0:
                                inside += 1
                    ac = area_poly.reshape(-1, 2).mean(axis=0)
                    dist = float(np.linalg.norm(cent - ac))
                else:
                    inside, dist = 0, 0.0
                key = (inside, -dist)
                if best2 is None or key > best2[0]:
                    best2 = (key, ni, nj, i0, j0)
    if best2 is None:
        return None
    _k, ni, nj, i0, j0 = best2
    slots = {}
    for ii in range(ni):
        for jj in range(nj):
            slots[(ii, jj)] = o + u * (i0 + ii) + v * (j0 + jj)
    return {"o": o, "u": u, "v": v, "ni": ni, "nj": nj, "slots": slots,
            "n_inl": int(inl.sum()), "rms": float(res[inl].mean()), "spacing": float(min(np.linalg.norm(u), np.linalg.norm(v)))}


# --------------------------------------------------------------------------
# 칸 재탐색
# --------------------------------------------------------------------------
def _canny_mask(work, region, exp_area=0.0, cfg=None):
    """칸 창 안 Lab Canny(L 10/30, a·b 4/12) → 닫기 → 창 가장자리에 닿지 않는 닫힌 윤곽만 채운다."""
    x0, y0, x1, y1 = region
    sub = work[y0:y1, x0:x1]
    sub = cv2.GaussianBlur(sub, (0, 0), 1.0)
    lab = _lab8(sub)
    e = np.zeros(lab.shape[:2], np.uint8)
    for c, (lo, hi) in enumerate(((10, 30), (4, 12), (4, 12))):
        e |= cv2.Canny(lab[:, :, c], lo, hi)
    e = cv2.morphologyEx(e, cv2.MORPH_CLOSE, np.ones((3, 3), np.uint8), iterations=2)
    cs, _ = cv2.findContours(e, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    m = np.zeros(e.shape, np.uint8)
    hh, ww = e.shape
    lo_a, hi_a = (0.0, 1e18) if cfg is None else (cfg["area_ratio"][0] * exp_area, cfg["area_ratio"][1] * exp_area)
    for c in cs:
        x, y, w_, h_ = cv2.boundingRect(c)
        if x <= 1 or y <= 1 or x + w_ >= ww - 1 or y + h_ >= hh - 1:
            continue
        if not (lo_a <= cv2.contourArea(c) <= hi_a):
            continue
        cv2.drawContours(m, [c], -1, 255, cv2.FILLED)
    full = np.zeros(work.shape[:2], np.uint8)
    full[y0:y1, x0:x1] = m
    return full


def _pick_near(cands, p, maxd):
    best = None
    for c in cands:
        if not c["ok"]:
            continue
        dd = math.hypot(c["cx"] - p[0], c["cy"] - p[1])
        if dd <= maxd and (best is None or dd < best[0]):
            best = (dd, c)
    return best[1] if best else None


# --------------------------------------------------------------------------
def search_near(w, d, valid, cfg, exp_area, p, Lw_real, half, maxd, first):
    """p(작업 좌표) 둘레 창(반폭 half)에서 칩 하나를 다시 찾는다: 임계를 first + local_seeds + 올리는 단계로
    차례로 → Lab Canny. 빈 칸 재탐색과 수동 지정 스냅이 같이 쓴다. 후보 dict | None."""
    H1, W1 = d.shape[:2]
    x0, y0 = int(max(0, p[0] - half)), int(max(0, p[1] - half))
    x1, y1 = int(min(W1, p[0] + half)), int(min(H1, p[1] + half))
    if x1 - x0 <= 8 or y1 - y0 <= 8:
        return None
    region = (x0, y0, x1, y1)
    seed = float(cfg["seed"])
    for th in list(first) + list(cfg["local_seeds"]) + [seed * k for k in TIGHTEN_K]:
        m = (d > float(th)).astype(np.uint8) * 255
        c = _pick_near(components(m, d, cfg, exp_area, region), p, maxd)
        if c is not None:
            return c
    cm = _canny_mask(w, region, exp_area, cfg)
    cm[valid[0:H1, 0:W1] == 0] = 0
    return _pick_near(components(cm, d, cfg, exp_area, region), p, maxd)


def snap(bgr, u, v, params, mm_px, chip_long_px=None):
    """수동 지정 스냅: 클릭(u, v 원본 px) 주변(칩 길이 정도 반경)에서 칩을 찾아 샘플 dict(원본 px)로.
    못 찾으면 None. mm_px = 원본 px 당 mm, chip_long_px = 칩 긴 변(px, 없으면 chip_mm 로 환산)."""
    cfg = cfg_of(params)
    L = float(chip_long_px) if chip_long_px else float(cfg["chip_mm"]) / max(mm_px, 1e-9)
    Lw = float(cfg["work_long_px"])
    s = min(1.0, Lw / L)
    r = 2.5 * L
    H0, W0 = bgr.shape[:2]
    x0, y0 = int(max(0, u - r)), int(max(0, v - r))
    x1, y1 = int(min(W0, u + r)), int(min(H0, v + r))
    crop = bgr[y0:y1, x0:x1]
    if crop.shape[0] < 16 or crop.shape[1] < 16:
        return None
    w = cv2.resize(crop, None, fx=s, fy=s, interpolation=cv2.INTER_AREA) if s < 0.999 else crop
    sw = w.shape[1] / float(crop.shape[1])
    valid = np.full(w.shape[:2], 255, np.uint8)
    k_bg = min(_odd(cfg["bg_k_chips"] * Lw), _odd(min(w.shape[:2]) - 2))
    d, _sig = residual_d(w, valid, k_bg, float(cfg["blur_sigma"]))
    exp_area = 0.35 * Lw * Lw
    p = ((u - x0) * sw, (v - y0) * sw)
    c = search_near(w, d, valid, cfg, exp_area, p, Lw, 0.9 * Lw, 0.9 * Lw, [float(cfg["seed"])])
    if c is None:
        return None
    sm = _make_sample(c, sw, mm_px, "manual", False, crop.shape[1], crop.shape[0])
    sm["x_px"] = round(sm["x_px"] + x0, 1)
    sm["y_px"] = round(sm["y_px"] + y0, 1)
    sm["vertices_px"] = [[a + x0, b + y0] for a, b in sm["vertices_px"]]
    return sm


def _make_sample(c, s, mm_px, source, weak, W, H, ox=0.0, oy=0.0):
    """작업 해상도 후보 → 원본 crop 해상도 샘플 dict(detect 호환)."""
    inv = 1.0 / s
    hull = (c["hull"].astype(np.float64) * inv).astype(np.float64)
    cx, cy = c["cx"] * inv, c["cy"] * inv
    hi = np.rint(hull).astype(np.int32)
    shape, approx = _shape_of(hi.reshape(-1, 1, 2), c["area"] * inv * inv)
    area_mm2 = c["area"] * inv * inv * mm_px * mm_px
    mm = mm_px
    return {
        "shape": shape,
        "x_mm": round((cx - W / 2.0) * mm, 2), "y_mm": round(-(cy - H / 2.0) * mm, 2),
        "x_px": round(cx, 1), "y_px": round(cy, 1),
        "w_mm": round(c["short"] * inv * mm, 2), "h_mm": round(c["long"] * inv * mm, 2),
        "angle_deg": round(c["ang"], 1),
        "area_mm2": round(area_mm2, 2), "solidity": round(c["sol"], 2),
        "strength": round(c["strength"], 1),
        "vertices_px": [[int(x), int(y)] for x, y in approx],
        "_poly": approx.reshape(-1, 1, 2).astype(np.int32), "_cnt": hi.reshape(-1, 1, 2),
        "source": source, **({"weak": True} if weak else {}),
    }


def _predicted_sample(p, u_hat, L, exp_area_w, s, mm_px, W, H):
    """예측 위치 샘플: 격자 방향의 칩 크기 사각형(긴 변 L)."""
    inv = 1.0 / s
    cx, cy = p[0] * inv, p[1] * inv
    # 긴 변이 세로(행 간격 방향)일 가능성이 높지만 방향을 모르므로 축 정렬 정사각 느낌의 작은 사각형
    hw, hh = 0.5 * L * 0.55 * inv, 0.5 * L * inv
    poly = np.array([[cx - hw, cy - hh], [cx + hw, cy - hh], [cx + hw, cy + hh], [cx - hw, cy + hh]])
    pi = np.rint(poly).astype(np.int32)
    area = (2 * hw) * (2 * hh) * mm_px * mm_px
    return {
        "shape": "quad",
        "x_mm": round((cx - W / 2.0) * mm_px, 2), "y_mm": round(-(cy - H / 2.0) * mm_px, 2),
        "x_px": round(cx, 1), "y_px": round(cy, 1),
        "w_mm": round(2 * hw * mm_px, 2), "h_mm": round(2 * hh * mm_px, 2), "angle_deg": 0.0,
        "area_mm2": round(area, 2), "solidity": 1.0, "strength": 0.0,
        "vertices_px": [[int(x), int(y)] for x, y in pi],
        "_poly": pi.reshape(-1, 1, 2), "_cnt": pi.reshape(-1, 1, 2),
        "source": "predicted", "weak": True,
    }


def detect(crop, params, mm_px, valid=None, ref=None, area_poly=None, log=print):
    """crop(BGR, 감지 영역 바깥은 판 색으로 채움) → FixedResult.

    mm_px = crop 원본 픽셀 당 mm(칩 크기 환산·면적 mm² 용). valid = crop 크기 uint8 마스크(255 = 검출 대상,
    감지 영역 안 + 제외 영역 밖). 좌표는 crop 원본 해상도로 돌려준다.
    area_poly = crop 좌표 감지 영역 꼭짓점(격자 오프셋 선택용).
    """
    t_all = time.perf_counter()
    cfg = cfg_of(params)
    H0, W0 = crop.shape[:2]
    if valid is None:
        valid = np.full((H0, W0), 255, np.uint8)
    res = FixedResult()
    res.wafer = _legacy.Wafer(found=False, surface="plate", mm_per_px=float(mm_px), scale_source="settings")
    tm = {}

    def scaled(s):
        if s >= 0.999:
            return crop, valid, ref, 1.0
        sz = (max(32, int(round(W0 * s))), max(32, int(round(H0 * s))))
        w = cv2.resize(crop, sz, interpolation=cv2.INTER_AREA)
        v = cv2.resize(valid, sz, interpolation=cv2.INTER_NEAREST)
        r = None if ref is None else cv2.resize(ref, sz, interpolation=cv2.INTER_AREA)
        return w, v, r, sz[0] / float(W0)

    chip_mm = float(cfg["chip_mm"])
    L_orig = chip_mm / max(mm_px, 1e-9)               # 예상 칩 긴 변(원본 px) - 자동 추정 전 값
    Lw = float(cfg["work_long_px"])

    def run_pass(s, L_work, exp_area):
        w, v, r, s_real = scaled(s)
        t0 = time.perf_counter()
        k_bg = _odd(cfg["bg_k_chips"] * L_work)
        k_bg = min(k_bg, _odd(min(w.shape[:2]) - 2))
        d, sig = residual_d(w, v, k_bg, float(cfg["blur_sigma"]), r)
        tm["resid"] = tm.get("resid", 0.0) + time.perf_counter() - t0
        mask = (d > float(cfg["seed"])).astype(np.uint8) * 255
        cands = components(mask, d, cfg, exp_area)
        return w, v, r, s_real, d, sig, cands

    # ---- 1차: 예상 칩 크기로 ----
    s = min(1.0, Lw / L_orig)
    exp_area = 0.35 * Lw * Lw
    w, v, r, s_real, d, sig, cands = run_pass(s, Lw, exp_area)
    strong = [c for c in cands if c["ok"] and c["sol"] > STRONG_SOLIDITY]
    L_est_orig = L_orig
    if len(strong) >= 3:
        L_est_orig = float(np.median([c["long"] for c in strong])) / s_real
        exp_area_orig = float(np.median([c["area"] for c in strong])) / (s_real * s_real)
        s2 = min(1.0, Lw / L_est_orig)
        if abs(s2 / s_real - 1.0) > 0.15:
            exp_area = exp_area_orig * s2 * s2
            w, v, r, s_real, d, sig, cands = run_pass(s2, Lw, exp_area)
        else:
            exp_area = exp_area_orig * s_real * s_real
        log("칩 크기    : 추정 긴 변 %.1f px (%.1f mm) · 작업 배율 x%.3f · 채널 σ %s"
            % (L_est_orig, L_est_orig * mm_px, s_real, sig))
    else:
        log("칩 크기    : 추정 실패(강한 후보 %d) → 설정 chip_mm %.1f mm 로 환산 %.1f px · 작업 배율 x%.3f"
            % (len(strong), chip_mm, L_orig, s_real))
    Lw_real = L_est_orig * s_real                       # 작업 해상도에서의 칩 긴 변
    good = _good(cands)
    # 감지 영역 경계(새겨진 선 · 레일)에서 칩 길이 x ref_edge_k 안에 닿은 후보는 칩이 아니다(격자 밖 제외 대신 쓰는 위치 기준)
    if valid is not None and good:
        dist = cv2.distanceTransform((v > 0).astype(np.uint8), cv2.DIST_L2, 3)
        band = float(cfg["ref_edge_k"]) * Lw_real_tmp(L_est_orig, s_real)
        keep = []
        for c in good:
            hh = c["hull"]
            dmin = min(dist[min(max(int(y), 0), dist.shape[0] - 1), min(max(int(x), 0), dist.shape[1] - 1)] for x, y in hh)
            if dmin >= band:
                keep.append(c)
        if len(keep) != len(good):
            log("경계 근처  : 감지 영역 경계에서 칩 길이 x %.1f 안에 닿은 후보 %d개 뺌" % (float(cfg["ref_edge_k"]), len(good) - len(keep)))
        good = keep
    log("1차 후보  : 연결요소 %d · 모양 통과 %d" % (len(cands), len(good)))

    # ---- 배치 ----
    layout = str(cfg["layout"])
    grid = None
    warnings = []
    ap = None
    if area_poly is not None:
        ap = (np.asarray(area_poly, np.float64) * s_real).astype(np.float32).reshape(-1, 1, 2)
    t0 = time.perf_counter()
    grid, why_grid = decide_grid([(c["cx"], c["cy"]) for c in good], cfg, ap)
    tm["grid"] = time.perf_counter() - t0
    if layout == "grid" and grid is None:
        warnings.append(why_grid)
    if grid is None and why_grid:
        log("격자      : %s" % why_grid)
    elif layout == "auto" and grid is None:
        log("배치      : 자동 판단 → 자유 배치")
    elif grid is not None:
        log("격자      : %dx%d · 인라이어 %d/%d · 간격 %.1f px(원본) · 평균 잔차 %.2f px(작업)"
            % (grid["ni"], grid["nj"], grid["n_inl"], len(good), grid["spacing"] / s_real, grid["rms"]))

    samples = []
    H1, W1 = w.shape[:2]
    if grid is None:
        for c in good:
            samples.append(_make_sample(c, s_real, mm_px, "detected", False, W0, H0))
        order_key = lambda sm: (round(sm["y_px"] / max(L_est_orig, 1.0)), sm["x_px"])      # noqa: E731
        samples.sort(key=order_key)
    else:
        slots = grid["slots"]
        used = set()
        n_local = n_pred = 0
        t0 = time.perf_counter()
        tol_match = 0.5 * grid["spacing"]
        det_by_slot = {}
        for c in good:
            best = None
            for key, p in slots.items():
                dd = math.hypot(c["cx"] - p[0], c["cy"] - p[1])
                if dd < tol_match and (best is None or dd < best[0]):
                    best = (dd, key)
            if best is not None:
                k = best[1]
                if k not in det_by_slot or c["strength"] > det_by_slot[k]["strength"]:
                    det_by_slot[k] = c
        chosen = {id(c) for c in det_by_slot.values()}
        extra = [c for c in good if id(c) not in chosen]
        half = WIN_K * Lw_real
        for key in sorted(slots, key=lambda kk: (kk[1], kk[0])):
            p = slots[key]
            c = det_by_slot.get(key)
            src, weak = "detected", False
            if c is None:
                c = search_near(w, d, v, cfg, exp_area, p, Lw_real, half, 0.6 * Lw_real, [])
                if c is not None:
                    src, weak = "local", True
            if c is not None:
                if src == "local":
                    n_local += 1
                sm_ = _make_sample(c, s_real, mm_px, src, weak, W0, H0)
                sm_["_slot"] = key
                samples.append(sm_)
            elif bool(cfg["emit_predicted"]):
                n_pred += 1
                sm_ = _predicted_sample(p, grid["u"], Lw_real, exp_area, s_real, mm_px, W0, H0)
                sm_["_slot"] = key
                samples.append(sm_)
        for c in extra:                      # 격자에 안 맞아도 모양 필터를 통과했으면 샘플로 낸다(경고)
            samples.append(_make_sample(c, s_real, mm_px, "detected", False, W0, H0))
        if extra:
            warnings.append("격자 밖 후보 %d개 유지 · 확인 필요" % len(extra))
            log("격자 밖   : 칸에 들지 않은 후보 %d개를 샘플로 유지" % len(extra))
        tm["local"] = time.perf_counter() - t0
        # 행 우선 정렬: 화면에서 더 세로인 격자축이 행
        u, vv = grid["u"], grid["v"]
        row_axis_is_v = abs(vv[1]) >= abs(u[1])
        samples.sort(key=lambda sm: (round(sm["y_px"] / max(0.6 * grid["spacing"] / s_real, 1.0)), sm["x_px"]))
        if n_local or n_pred:
            warnings.append("보완 %d · 예측 %d (확인 필요)" % (n_local, n_pred)) if n_pred else None
        _ = row_axis_is_v

    # 정렬 안정화 후 번호
    for i, sm in enumerate(samples, 1):
        sm["no"] = i
    res.samples = samples
    res.warnings = warnings
    cnt = {k: sum(1 for x in samples if x["source"] == k) for k in ("detected", "local", "predicted")}
    res.detect_ms = (time.perf_counter() - t_all) * 1000.0
    res.diag = {"chip_long_px": round(L_est_orig, 1), "scale": round(s_real, 4), "sources": cnt,
                "grid": None if grid is None else {"inliers": grid["n_inl"], "n_good": len(good),
                                                    "spacing_px": round(grid["spacing"] / s_real, 1),
                                                    "uv": [(grid["u"] / s_real).tolist(), (grid["v"] / s_real).tolist()]},
                "time_ms": {k: round(v_ * 1000.0, 1) for k, v_ in tm.items()}, "sigma": sig}
    log("출처      : 검출 %d · 보완 %d · 예측 %d · 시간 %.0f ms (잔차 %.0f · 격자 %.0f · 재탐색 %.0f)"
        % (cnt["detected"], cnt["local"], cnt["predicted"], res.detect_ms,
           tm.get("resid", 0) * 1000, tm.get("grid", 0) * 1000, tm.get("local", 0) * 1000))
    return res
