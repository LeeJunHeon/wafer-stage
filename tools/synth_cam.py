"""synth_cam.py - 사진 호밍 검증용 합성 카메라(--dry 전용).

dry 스테이지의 '실제' 위치(보고 위치 + 검증용 탈조 오프셋)를 가짜 호모그래피로
픽셀에 투영해 헤드 마커(ArUco)를 그린 프레임을 만든다. 약간의 회전·원근·잡음·
흐림을 넣어 실제 천장 카메라처럼 서브픽셀 검출이 필요하게 한다.

서버가 WAFER_STAGE_DRY_CAMERA=synth 로 뜨면 backend/vision.single_shot 이 카메라
대신 render() 를 부른다. 프로그램 실행에는 필요 없다.

  python tools/synth_cam.py            # 몇 자리에서 그려 검출·역변환 오차를 찍는다
"""

import math
import os
import sys

import cv2
import numpy as np

# 헤드 마커 중심이 기준 포인터(스테이지 좌표 P)에서 떨어진 거리(mm). 보정이
# 이 상수를 몰라도 되는지(호모그래피가 흡수하는지)까지 보는 셈이다.
HEAD_OFFSET_MM = (18.0, 26.0)


def true_homography(w, h):
    """헤드 마커 평면 mm(X, Y) → px. 카메라 1280×720 기준으로 만들고 크기에 맞춘다.

    맵과 같은 배치(위 X+, 왼쪽 Y+): u 는 Y 가 커질수록 왼쪽, v 는 X 가 커질수록 위.
    1.3° 회전 + 아주 약한 원근을 넣는다.
    """
    s = 2.0                                 # px/mm (1280 기준)
    th = math.radians(1.3)
    c, si = math.cos(th), math.sin(th)
    # [u, v] = R @ [-Y, -X] * s + t
    A = np.array([[0.0, -s], [-s, 0.0]])
    R = np.array([[c, -si], [si, c]])
    M = R @ A
    t = np.array([940.0, 700.0])
    H = np.array([[M[0, 0], M[0, 1], t[0]],
                  [M[1, 0], M[1, 1], t[1]],
                  [2.0e-6, -3.0e-6, 1.0]])
    k = np.diag([w / 1280.0, h / 720.0, 1.0])
    return k @ H


def project(H, x, y):
    p = H @ np.array([x, y, 1.0])
    return p[0] / p[2], p[1] / p[2]


def _marker_img(mid, dict_name, px=200):
    aruco = cv2.aruco
    dic = aruco.getPredefinedDictionary(getattr(aruco, "DICT_" + dict_name, aruco.DICT_4X4_50))
    img = aruco.generateImageMarker(dic, int(mid), px)
    return img


def render(params, true_xy, seed=None):
    """BGR 프레임. true_xy 가 None 이면 헤드 마커 없이 배경만.

    WAFER_STAGE_SYNTH_ROT=1 이면 카메라를 90° 돌린 장면(같은 w x h 로 시계방향 90° 회전한 그림)."""
    if os.environ.get("WAFER_STAGE_SYNTH_ROT"):
        p2 = dict(params)
        p2["width"], p2["height"] = int(params.get("height", 720)), int(params.get("width", 1280))
        os.environ.pop("WAFER_STAGE_SYNTH_ROT")
        try:
            img = render(p2, true_xy, seed)
        finally:
            os.environ["WAFER_STAGE_SYNTH_ROT"] = "1"
        return cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
    w = int(params.get("width", 1280))
    h = int(params.get("height", 720))
    ph = params.get("photohome") or {}
    if os.environ.get("WAFER_STAGE_SYNTH_HEAD_OFF"):
        true_xy = None                      # 검증용: 헤드 마커가 안 보이는 장면
    rng = np.random.default_rng(seed)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 120.0 + 25.0 * (xx / max(w, 1)) - 15.0 * (yy / max(h, 1))
    img = np.repeat(base[:, :, None], 3, axis=2)
    if true_xy is not None:
        H = true_homography(w, h)
        cx, cy = true_xy[0] + HEAD_OFFSET_MM[0], true_xy[1] + HEAD_OFFSET_MM[1]
        half = float(ph.get("marker_mm", 40.0)) / 2.0
        # 흰 테두리(마커 한 칸 = 1/6) 를 포함한 사각형을 그린다.
        quiet = half * 1.35
        mk = _marker_img(ph.get("marker_id", 10), str(ph.get("dict", "4X4_50")))
        n = mk.shape[0]
        pad = int(round(n * (quiet / half - 1.0) / 2.0))
        mk = cv2.copyMakeBorder(mk, pad, pad, pad, pad, cv2.BORDER_CONSTANT, value=255)
        n2 = mk.shape[0]
        # 마커 이미지의 (0,0)=왼쪽 위 → 스테이지 좌표에서 어느 모서리인지 고정
        src = np.float32([[0, 0], [n2 - 1, 0], [n2 - 1, n2 - 1], [0, n2 - 1]])
        mm = [(cx + quiet, cy + quiet), (cx + quiet, cy - quiet),
              (cx - quiet, cy - quiet), (cx - quiet, cy + quiet)]
        dst = np.float32([project(H, x, y) for x, y in mm])
        P = cv2.getPerspectiveTransform(src, dst)
        warped = cv2.warpPerspective(mk, P, (w, h), flags=cv2.INTER_LINEAR,
                                     borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        mask = cv2.warpPerspective(np.full_like(mk, 255), P, (w, h), flags=cv2.INTER_LINEAR,
                                   borderMode=cv2.BORDER_CONSTANT, borderValue=0)
        a = (mask.astype(np.float32) / 255.0)[:, :, None]
        tile = (warped.astype(np.float32) * 0.85 + 20.0)[:, :, None]
        img = img * (1.0 - a) + np.repeat(tile, 3, axis=2) * a
    img = cv2.GaussianBlur(img.astype(np.float32), (0, 0), 0.7)
    img = img + rng.normal(0.0, 2.5, img.shape)
    return np.clip(img, 0, 255).astype(np.uint8)


# --------------------------------------------------------------------------
# 고정 보정용 판 장면(알려진 호모그래피 · 알려진 샘플 위치)
# --------------------------------------------------------------------------
PLATE_QUAD_MM = [(40.0, 40.0), (40.0, 200.0), (200.0, 200.0), (200.0, 40.0)]   # 새겨진 사각형
PLATE_CAL_MM = [(40.0, 40.0), (40.0, 200.0), (200.0, 200.0), (200.0, 40.0), (120.0, 120.0)]
PLATE_SAMPLES_MM = [(70.0, 70.0), (70.0, 120.0), (70.0, 170.0),
                    (120.0, 70.0), (120.0, 120.0), (120.0, 170.0),
                    (170.0, 70.0), (170.0, 120.0), (170.0, 170.0)]
PLATE_OUTSIDE_MM = [(15.0, 100.0), (212.0, 150.0)]       # 사각형 밖 물체(무시돼야 한다)


def plate_homography(w, h):
    """판 평면 스테이지 mm(X, Y) → px. 4080x3060 에서 약 12 px/mm. 1.8° 회전 + 약한 원근.

    u 는 Y 가 커질수록 왼쪽, v 는 X 가 커질수록 위(맵과 같은 배치).
    """
    s = 12.0 * (w / 4080.0)
    th = math.radians(1.8)
    c, si = math.cos(th), math.sin(th)
    A = np.array([[0.0, -s], [-s, 0.0]])
    R = np.array([[c, -si], [si, c]])
    M = R @ A
    t = np.array([w * 0.82, h * 0.88])
    H = np.array([[M[0, 0], M[0, 1], t[0]],
                  [M[1, 0], M[1, 1], t[1]],
                  [1.5e-6 * 4080.0 / w, -2.0e-6 * 4080.0 / w, 1.0]])
    return H


def _chip(img, H, x, y, side_mm, rot_deg, color):
    th = math.radians(rot_deg)
    pts = []
    for dx, dy in [(-1, -1), (1, -1), (1, 1), (-1, 1)]:
        px, py = dx * side_mm / 2.0, dy * side_mm / 2.0
        pts.append((x + px * math.cos(th) - py * math.sin(th),
                    y + px * math.sin(th) + py * math.cos(th)))
    uv = np.array([project(H, a, b) for a, b in pts], np.float32)
    cv2.fillConvexPoly(img, np.round(uv).astype(np.int32), color, lineType=cv2.LINE_AA)


def render_plate(w, h, head_xy=None, params=None, seed=3, shift_mm=(0.0, 0.0)):
    """BGR 판 장면. 회색 판 + 알려진 위치의 칩 9개 + 사각형 밖 물체 2개 (+ 헤드 마커).

    shift_mm: 칩 위치를 이만큼 옮긴다(샘플 위치를 바꿔 다시 찍는 검증용).
    돌려주는 것: (img, truth{"samples": [(X,Y)], "outside": [(X,Y)], "H": 3x3 mm→px}).
    """
    rng = np.random.default_rng(seed)
    H = plate_homography(w, h)
    yy, xx = np.mgrid[0:h, 0:w]
    base = 150.0 + 14.0 * (xx / max(w, 1)) - 10.0 * (yy / max(h, 1))
    img = np.repeat(base[:, :, None], 3, axis=2).astype(np.float32)
    # 판 밖 배경은 더 어둡게(레일 · 바닥), 새겨진 사각형 선은 얇은 짙은 선
    quad = np.array([project(H, a, b) for a, b in PLATE_QUAD_MM], np.float32)
    cv2.polylines(img, [np.round(quad).astype(np.int32)], True, (132, 132, 132), max(2, w // 1500))
    for i, (x, y) in enumerate(PLATE_SAMPLES_MM):
        sx, sy = x + shift_mm[0], y + shift_mm[1]
        col = [(70, 90, 150), (60, 110, 80), (140, 80, 70), (80, 80, 170)][i % 4]   # BGR 로 서로 다른 색
        _chip(img, H, sx, sy, 7.0 + (i % 3), 7.0 * i, col)
    for (x, y) in PLATE_OUTSIDE_MM:
        _chip(img, H, x, y, 9.0, 20.0, (60, 70, 160))
    out = np.clip(img, 0, 255).astype(np.uint8)
    # 판 마커(id0~3)가 화면에 있어도 고정 보정은 무시해야 한다 - 사각형 밖 네 모서리 쪽에 붙인다
    for mid, (mx, my) in enumerate([(10.0, 20.0), (10.0, 225.0), (212.0, 20.0), (212.0, 225.0)]):
        n = 18.0
        mk = _marker_img(mid, "4X4_50", 120)
        mk = cv2.cvtColor(cv2.copyMakeBorder(mk, 12, 12, 12, 12, cv2.BORDER_CONSTANT, value=255),
                          cv2.COLOR_GRAY2BGR)
        q = n * (144 / 120.0) / 2.0
        src = np.float32([[0, 0], [143, 0], [143, 143], [0, 143]])
        dst = np.float32([project(H, mx + q, my + q), project(H, mx + q, my - q),
                          project(H, mx - q, my - q), project(H, mx - q, my + q)])
        P = cv2.getPerspectiveTransform(src, dst)
        cv2.warpPerspective(mk, P, (w, h), dst=out, flags=cv2.INTER_LINEAR,
                            borderMode=cv2.BORDER_TRANSPARENT)
    # 헤드 마커는 별도 평면(판보다 높다) - 기존 합성기를 그대로 쓴다
    if head_xy is not None:
        p = dict(params or {}, width=w, height=h)
        mk = render(p, head_xy, seed=seed)
        # 마커 타일만 겹친다(배경 그라데이션은 버린다): 합성기 배경과 다른 화소만 복사
        bg = render(p, None, seed=seed)
        diff = np.abs(mk.astype(np.int16) - bg.astype(np.int16)).sum(axis=2) > 30
        diff = cv2.dilate(diff.astype(np.uint8), np.ones((5, 5), np.uint8)) > 0
        out[diff] = mk[diff]
    out = cv2.GaussianBlur(out, (0, 0), 0.8 * w / 1280.0 if w > 1280 else 0.8)
    out = np.clip(out.astype(np.float32) + rng.normal(0.0, 2.0, out.shape), 0, 255).astype(np.uint8)
    truth = {"samples": [(x + shift_mm[0], y + shift_mm[1]) for x, y in PLATE_SAMPLES_MM],
             "outside": list(PLATE_OUTSIDE_MM), "H": H, "quad_mm": list(PLATE_QUAD_MM)}
    return out, truth


def _selftest():
    sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                                    "backend"))
    params = {"width": 1280, "height": 720,
              "photohome": {"marker_id": 10, "marker_mm": 40.0, "dict": "4X4_50"}}
    aruco = cv2.aruco
    dic = aruco.getPredefinedDictionary(aruco.DICT_4X4_50)
    dp = aruco.DetectorParameters()
    dp.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    H = true_homography(1280, 720)
    for xy in [(0, 0), (120, 120), (247, 247), (20, 230), (230, 20)]:
        img = render(params, xy, seed=1)
        c, ids, _ = aruco.ArucoDetector(dic, dp).detectMarkers(img)
        if ids is None:
            print(xy, "미검출")
            continue
        q = np.asarray(c[0]).reshape(4, 2)
        u, v = q[:, 0].mean(), q[:, 1].mean()
        Hi = np.linalg.inv(H)
        p = Hi @ np.array([u, v, 1.0])
        x, y = p[0] / p[2] - HEAD_OFFSET_MM[0], p[1] / p[2] - HEAD_OFFSET_MM[1]
        print(xy, "id", int(ids[0][0]), "역변환 오차 %.3f mm" % math.hypot(x - xy[0], y - xy[1]))


if __name__ == "__main__":
    _selftest()
