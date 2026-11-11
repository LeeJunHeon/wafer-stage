"""geometry.py - 좌표 변환과 헤드 마커 찾기.

좌표계 세 가지
  사진     : 원본 해상도 픽셀
  판       : mm. 꼭짓점 A = (0, 0), A→B 가 +x, A→D 가 +y (사진에서 왼쪽 위부터 시계 방향으로 A · B · C · D)
  스테이지 : 핀 가운데의 X · Y mm
변환은 호모그래피(3×3). 점 4개면 정확히, 더 많으면 전체 점의 최소제곱으로 맞춘다.
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
