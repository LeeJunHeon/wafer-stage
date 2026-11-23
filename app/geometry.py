"""geometry.py - 좌표 변환과 헤드 마커 찾기.

좌표계 세 가지
  사진     : 원본 해상도 픽셀
  판       : mm. 꼭짓점 A = (0, 0), A→B 가 +x, A→D 가 +y (사진에서 왼쪽 위부터 시계 방향으로 A · B · C · D)
  스테이지 : 핀 가운데의 X · Y mm
변환은 호모그래피(3×3). 점 4개면 정확히, 더 많으면 전체 점의 최소제곱으로 맞춘다.
사진 → 스테이지는 꼭짓점 4개 호모그래피(바탕) + 보정 점의 보정량을 거리로 가중한 보정(warp_shift · warp_inverse - calib.to_stage · from_stage):
  q = Hc(px) · xy = q + c(q) · c(q) = Σ w_i r_i ÷ Σ w_i · w_i = 1 ÷ max(|q − q_i|, 1e-6)^WARP_POW (q_i = Hc(보정 점 px) · r_i = 보정 점 xy − q_i).
  보정 점 자리에서는 정확히 그 xy · 1개면 판 전체가 r_1 만큼 평행 이동 · c 는 r_i 의 가중 평균이라 가장 큰 보정량을 넘지 않는다.
"""

import math

import cv2
import numpy as np

MARKER_ID = 10                                     # 헤드 윗면의 ArUco 4X4_50 · 40 mm
RECT_PPMM = 8.0                                    # 보정 사진(판을 위에서 본 정사각형)의 해상도: 1 mm = 8 픽셀
RECT_MARGIN_MM = 6.0                               # 검출용 보정 사진은 새겨진 사각형 바깥을 이만큼 더 담는다(화면에는 사각형 안쪽만 보인다)
_ARUCO = None


def homography(src, dst):
    """src → dst 호모그래피. 점 4개는 정확한 풀이, 5개부터는 최소제곱. 못 만들면 None(점이 한 줄 · 겹침)."""
    s = np.asarray(src, np.float64).reshape(-1, 2)
    d = np.asarray(dst, np.float64).reshape(-1, 2)
    if len(s) < 4 or len(s) != len(d):
        return None
    try:
        if len(s) == 4:
            H = cv2.getPerspectiveTransform(s.astype(np.float32), d.astype(np.float32))
        else:
            H, _ = cv2.findHomography(s, d, 0)
    except cv2.error:
        return None
    if H is None or not np.all(np.isfinite(H)) or abs(np.linalg.det(H)) < 1e-12:
        return None
    return np.asarray(H, np.float64)


def apply(H, pts):
    """점들(N×2 또는 한 점)을 H 로 옮긴다. 반환 N×2."""
    p = np.asarray(pts, np.float64).reshape(-1, 1, 2)
    return cv2.perspectiveTransform(p, np.asarray(H, np.float64)).reshape(-1, 2)


def apply1(H, x, y):
    p = apply(H, [(x, y)])[0]
    return float(p[0]), float(p[1])


WARP_POW = 2.0                                     # 보정 점 가중 = 거리^−WARP_POW
WARP_ITERS = 12                                    # 스테이지 → 바탕 자리 되풀이 횟수


def warp_shift(q, pts, res, pow=WARP_POW):
    """바탕 자리 q(스테이지 mm)의 보정량 c(q) = 보정 점 보정량 res 의 거리 가중 평균(pts = 보정 점 바탕 자리). 보정 점이 없으면 (0, 0)."""
    if not len(pts):
        return 0.0, 0.0
    P = np.asarray(pts, np.float64).reshape(-1, 2)
    R = np.asarray(res, np.float64).reshape(-1, 2)
    d = np.hypot(P[:, 0] - float(q[0]), P[:, 1] - float(q[1]))
    w = 1.0 / np.maximum(d, 1e-6) ** float(pow)
    c = (w[:, None] * R).sum(axis=0) / w.sum()
    return float(c[0]), float(c[1])


def warp_inverse(xy, pts, res, pow=WARP_POW, iters=WARP_ITERS):
    """스테이지 xy → 바탕 자리 q(xy = q + c(q) 를 q = xy − c(q) 로 되풀이 · 시작 q = xy)."""
    x, y = float(xy[0]), float(xy[1])
    qx, qy = x, y
    if not len(pts):
        return qx, qy
    for _ in range(int(iters)):
        cx, cy = warp_shift((qx, qy), pts, res, pow)
        qx, qy = x - cx, y - cy
    return qx, qy


def plate_corners_mm(size_mm):
    s = float(size_mm)
    return [(0.0, 0.0), (s, 0.0), (s, s), (0.0, s)]


def photo_to_plate(corners_px, size_mm):
    """사진의 꼭짓점 4개(A · B · C · D 픽셀) → "사진 → 판 mm" 호모그래피."""
    return homography(corners_px, plate_corners_mm(size_mm))


def rectify(img, H_photo_plate, size_mm, ppmm=RECT_PPMM, margin_mm=0.0):
    """원본 사진에서 판 영역만 잘라 위에서 본 정사각형 사진(보정 사진)으로. 한 변 = (size_mm + 2 × margin_mm) × ppmm 픽셀.
    margin_mm 을 주면 새겨진 사각형 바깥을 그만큼 더 담는다(검출이 사각형 경계 근처 후보를 빼는 데 쓴다)."""
    n = int(round((float(size_mm) + 2.0 * float(margin_mm)) * ppmm))
    S = np.array([[ppmm, 0, margin_mm * ppmm], [0, ppmm, margin_mm * ppmm], [0, 0, 1.0]])
    return cv2.warpPerspective(img, S @ np.asarray(H_photo_plate, np.float64), (n, n), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def quad_quality(stage_pts, size_mm):
    """스테이지에서 잰 꼭짓점 A · B · C · D 의 품질: 네 변 길이 · 판 한 변과의 차이 · 모서리 각의 직각 오차."""
    p = [np.asarray(q, np.float64) for q in stage_pts]
    sides, angles = [], []
    for i in range(4):
        a, b = p[i], p[(i + 1) % 4]
        sides.append(float(np.hypot(*(b - a))))
    for i in range(4):
        u, v = p[(i - 1) % 4] - p[i], p[(i + 1) % 4] - p[i]
        c = float(np.dot(u, v) / max(1e-12, np.hypot(*u) * np.hypot(*v)))
        angles.append(math.degrees(math.acos(max(-1.0, min(1.0, c)))))
    side_err = [s - float(size_mm) for s in sides]
    ang_err = [a - 90.0 for a in angles]
    return {"sides": [round(s, 2) for s in sides], "side_err": [round(e, 2) for e in side_err], "angle_err": [round(e, 2) for e in ang_err],
            "max_side_err": round(max(abs(e) for e in side_err), 2), "max_angle_err": round(max(abs(e) for e in ang_err), 2)}


def find_marker(img, marker_id=MARKER_ID, max_w=2040):
    """헤드 마커(ArUco 4X4_50)의 가운데 픽셀(원본 해상도) (x, y). 안 보이면 None. 큰 사진은 줄여서 찾고 좌표만 되돌린다."""
    global _ARUCO
    if img is None:
        return None
    if _ARUCO is None:
        _ARUCO = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50), cv2.aruco.DetectorParameters())
    g = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY) if img.ndim == 3 else img
    k = 1.0
    if g.shape[1] > max_w:
        k = max_w / float(g.shape[1])
        g = cv2.resize(g, (max_w, int(round(g.shape[0] * k))), interpolation=cv2.INTER_AREA)
    corners, ids, _ = _ARUCO.detectMarkers(g)
    if ids is None:
        return None
    for c, i in zip(corners, ids.ravel()):
        if int(i) == int(marker_id):
            q = c.reshape(4, 2).astype(np.float64)
            # 대각선의 교점 = 사각형 가운데(원근이 있어도 맞다 - 네 점 평균은 원근에서 치우친다)
            ctr = _diag_cross(q)
            return float(ctr[0] / k), float(ctr[1] / k)
    return None


def _diag_cross(q):
    a, b, c, d = q
    m = np.array([c - a, -(d - b)]).T
    try:
        t = np.linalg.solve(m, b - a)
    except np.linalg.LinAlgError:
        return q.mean(axis=0)
    return a + t[0] * (c - a)


def point_in_poly(poly, x, y):
    return cv2.pointPolygonTest(np.asarray(poly, np.float32).reshape(-1, 1, 2), (float(x), float(y)), False) >= 0


def cooler_cell(plate_xy, size_mm):
    """판 mm 자리 → 쿨러 배치 칸 (행, 열). 0 행 = A–B 변 쪽(판 y 작은 쪽) · 0 열 = A–D 변 쪽(판 x 작은 쪽). 판 밖은 가장자리 칸."""
    cell = float(size_mm) / 3.0
    col = min(2, max(0, int(float(plate_xy[0]) // cell)))
    row = min(2, max(0, int(float(plate_xy[1]) // cell)))
    return row, col


def _side_value(side, x, y):
    """스테이지 X · Y 를 그 쪽으로 갈수록 커지는 값으로(먼 쪽 = +좌표 · 모터 쪽 = −좌표)."""
    v = x if side[0] == "x" else y
    return v if side.endswith("far") else -v


def front_layout(stage_corners, front, right):
    """장비 앞에서 본 쿨러 칸 배치(초기 설정 7 의 장비 앞 기준 보기).
    stage_corners: 꼭짓점 A~D 의 스테이지 X · Y(초기 설정 5) · front · right: 장비 앞쪽 · 오른쪽 방향(config.SIDES 값 - 서로 다른 축).
    화면: 아래 = 앞쪽 · 오른쪽 = 오른쪽. 반환 {"corners": {"A": [화면 행, 화면 열], …}(0 또는 2), "cells": 화면 3×3 마다 [cooler_grid 행, 열]}.
    꼭짓점이 모자라거나 방향이 같은 축 · 네 꼭짓점이 화면의 네 구석으로 나뉘지 않으면 None."""
    if front[0] == right[0] or not all(n in stage_corners for n in "ABCD"):
        return None
    pts = {n: (_side_value(right, *stage_corners[n]), _side_value(front, *stage_corners[n])) for n in "ABCD"}   # (화면 오른쪽 값, 화면 아래 값)
    mu = sum(p[0] for p in pts.values()) / 4.0
    mv = sum(p[1] for p in pts.values()) / 4.0
    at = {n: (2 if p[1] > mv else 0, 2 if p[0] > mu else 0) for n, p in pts.items()}
    if len(set(at.values())) != 4:
        return None
    a, b, d = (np.array(at[n]) for n in "ABD")
    eb, ed = (b - a) // 2, (d - a) // 2                 # 판 열(+1) · 판 행(+1)마다 화면에서 움직이는 칸
    if abs(eb).sum() != 1 or abs(ed).sum() != 1 or (eb * ed).any():
        return None
    cells = [[None] * 3 for _ in range(3)]
    for gr in range(3):
        for gc in range(3):
            r, c = a + gc * eb + gr * ed
            cells[int(r)][int(c)] = [gr, gc]
    return {"corners": {n: [int(v) for v in at[n]] for n in "ABCD"}, "cells": cells}


# ----------------------------------------------------------------------
# 화면 방향(장비 앞 = 문 기준): 사진 → 화면 회전(0 · 90 · 180 · 270 시계 방향) + 좌우 뒤집기(먼저). 저장된 값(사진 픽셀 · 판 mm · 스테이지 mm)은
# 그대로 두고, 화면에 그릴 때와 화면 입력을 받을 때만 이 함수들로 바꾼다(변환은 여기 한 곳 - 서버가 그림 · 덧그림 좌표를 화면 방향으로 보낸다).
# 판 mm 도 같은 변환(판 좌표는 사진의 A = 왼쪽 위 기준이라 사진과 같은 방향이다).
# ----------------------------------------------------------------------
VIEW_NAMES = ("A", "B", "C", "D")                   # 화면 꼭짓점 이름: 화면 왼쪽 위부터 시계 방향
_UNIT = {"A": (0.0, 0.0), "B": (1.0, 0.0), "C": (1.0, 1.0), "D": (0.0, 1.0)}     # 사진(저장) 꼭짓점의 사진 안 자리


def view_size(W, H, rot):
    return (H, W) if int(rot) in (90, 270) else (W, H)


def to_view(x, y, W, H, rot, flip):
    """사진(또는 판) 좌표 → 화면 좌표(W × H 는 사진 · 판의 크기)."""
    rot = int(rot)
    if flip:
        x = W - x
    if rot == 90:
        return H - y, x
    if rot == 180:
        return W - x, H - y
    if rot == 270:
        return y, W - x
    return x, y


def from_view(u, v, W, H, rot, flip):
    """화면 좌표 → 사진(또는 판) 좌표."""
    rot = int(rot)
    if rot == 90:
        x, y = v, H - u
    elif rot == 180:
        x, y = W - u, H - v
    elif rot == 270:
        x, y = W - v, u
    else:
        x, y = u, v
    if flip:
        x = W - x
    return x, y


def view_matrix(W, H, rot, flip):
    """to_view 를 3×3 행렬로(다른 변환 뒤에 곱해 화면 좌표를 바로 낸다)."""
    o = np.asarray(to_view(0.0, 0.0, W, H, rot, flip))
    ex = np.asarray(to_view(1.0, 0.0, W, H, rot, flip)) - o
    ey = np.asarray(to_view(0.0, 1.0, W, H, rot, flip)) - o
    return np.array([[ex[0], ey[0], o[0]], [ex[1], ey[1], o[1]], [0.0, 0.0, 1.0]])


def rotate_img(img, rot, flip):
    """사진(그림)을 화면 방향으로."""
    if img is None:
        return None
    if flip:
        img = cv2.flip(img, 1)
    rot = int(rot)
    if rot == 90:
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    if rot == 180:
        return cv2.rotate(img, cv2.ROTATE_180)
    if rot == 270:
        return cv2.rotate(img, cv2.ROTATE_90_COUNTERCLOCKWISE)
    return img


def view_names(rot, flip):
    """저장(사진 기준) 꼭짓점 이름 → 화면 이름(화면 왼쪽 위부터 시계 방향 A · B · C · D)."""
    out = {}
    for n, (x, y) in _UNIT.items():
        u, v = to_view(x, y, 1.0, 1.0, rot, flip)
        out[n] = {(0, 0): "A", (1, 0): "B", (1, 1): "C", (0, 1): "D"}[(int(round(u)), int(round(v)))]
    return out


def store_name(view_name, rot, flip):
    """화면 꼭짓점 이름 → 저장 이름."""
    inv = {v: k for k, v in view_names(rot, flip).items()}
    return inv[str(view_name).upper()]


def view_matches(stage_corners, rot, flip, front, right):
    """회전 설정이 꼭짓점 방향(초기 설정 5 스테이지 좌표 + 장비 앞쪽 · 오른쪽 - front_layout)과 맞는가. 꼭짓점이 모자라면 None."""
    lay = front_layout(stage_corners, front, right)
    if lay is None:
        return None
    for n, (x, y) in _UNIT.items():
        u, v = to_view(x, y, 1.0, 1.0, rot, flip)
        if lay["corners"][n] != [2 if v > 0.5 else 0, 2 if u > 0.5 else 0]:
            return False
    return True
