"""plate.py - 고정 보정(calib.mode = fixed): 감지 영역 + 판 보정.

판 마커 없이 쓰는 방식. 카메라는 천장에 고정이고 샘플은 판에 새겨진 사각형 안에만
놓이므로 감지 영역은 촬영본 픽셀 좌표의 사각형으로 한 번 정해 둔다. 판 보정은
(촬영본 픽셀 <-> 스테이지 XY) 점 4개 이상의 호모그래피 최소제곱이며 한 번 저장한다
(카메라를 옮기거나 해상도를 바꾸면 다시).

저장: data/plate_calib.json
  {"created", "camera_index", "size": [w, h],
   "area": [[u, v] x4] | None,                 감지 영역(볼록 사각형, 촬영본 px)
   "points": [{"x","y","u","v","res_mm"}],     판 보정 점(스테이지 XY mm / 촬영본 px)
   "H": 3x3 | None (px -> mm), "rms_mm", "max_mm", "res_limit_mm"}

이 모듈은 순수하다(서버 상태를 모른다). 검출 로직(detect.py)은 건드리지 않고
plate 모드의 기존 인자(surface, plate_mm_per_px, marker_mm=0, plate_margin_pct)만 쓴다.
"""

import json
import math
import os
import time

import cv2
import numpy as np

from core import calib, detect, paths

MIN_POINTS = 4
MIN_AREA_FRAC = 0.01               # 감지 영역이 프레임의 1% 는 돼야 한다

# plate 모드가 붙이는 안내 - 고정 보정에서는 의도한 동작이라 경고로 올리지 않는다.
_QUIET_WARN = ("plate mode: origin = frame center", "plate scale not calibrated")


def _path():
    return paths.PLATE_CALIB_PATH


# --------------------------------------------------------------------------
# 헤드 마커 (판 마커와 무관 - 사진 호밍 · 촬영본 표시 공용)
# --------------------------------------------------------------------------
def detect_head(bgr, ph):
    """(u, v, 모서리 4x2) 헤드 마커 또는 None. ph = settings["photohome"]."""
    aruco = getattr(cv2, "aruco", None)
    if aruco is None or bgr is None:
        return None
    ph = ph or {}
    name = "DICT_" + str(ph.get("dict", "4X4_50"))
    dic = aruco.getPredefinedDictionary(getattr(aruco, name, aruco.DICT_4X4_50))
    dp = aruco.DetectorParameters()
    dp.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    corners, ids, _rej = aruco.ArucoDetector(dic, dp).detectMarkers(bgr)
    if ids is None:
        return None
    want = int(ph.get("marker_id", 10))
    for c, i in zip(corners, ids.reshape(-1)):
        if int(i) == want:
            q = np.asarray(c).reshape(4, 2).astype(np.float64)
            return float(q[:, 0].mean()), float(q[:, 1].mean()), q
    return None


def edge_margin_px(ph, size):
    """헤드 마커가 걸리면 안 되는 화면 가장자리 폭(px). 0 이면 짧은 변의 3%."""
    m = int((ph or {}).get("edge_margin_px") or 0)
    if m > 0:
        return m
    return int(round(0.03 * min(int(size[0]), int(size[1]))))


def touches_edge(corners, size, margin):
    """마커 모서리 중 하나라도 프레임 가장자리 margin 안쪽에 걸치는가."""
    q = np.asarray(corners, np.float64).reshape(-1, 2)
    w, h = int(size[0]), int(size[1])
    return bool(q[:, 0].min() < margin or q[:, 1].min() < margin
                or q[:, 0].max() > w - 1 - margin or q[:, 1].max() > h - 1 - margin)


# --------------------------------------------------------------------------
# 파일
# --------------------------------------------------------------------------
def load(path=None):
    """저장된 고정 보정 dict (H 는 ndarray) 또는 None."""
    try:
        with open(path or _path(), encoding="utf-8") as f:
            d = json.load(f)
    except (OSError, ValueError):
        return None
    if not isinstance(d, dict):
        return None
    if d.get("H") is not None:
        try:
            d["H"] = np.asarray(d["H"], np.float64).reshape(3, 3)
        except (ValueError, TypeError):
            d["H"] = None
    return d


def to_json(d):
    out = dict(d)
    if out.get("H") is not None:
        out["H"] = [[float(a) for a in row] for row in np.asarray(out["H"]).reshape(3, 3)]
    return out


# storage 는 backend 모듈이라 core 에서 부르지 않는다 - 같은 방식(임시 파일 -> 교체)을 직접.
def save(d, path=None):
    path = path or _path()
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(to_json(d), f, ensure_ascii=False, indent=1)
    os.replace(tmp, path)


def validity(d, camera_index, size, need_h=True):
    """(유효한가, 사유). 카메라 번호·해상도가 저장 때와 같고 감지 영역이 있어야 한다.

    need_h=True(기본)면 판 보정(H)까지 있어야 통과(좌표를 만들 수 있다). need_h=False 는
    '감지 영역 유효' - 판 보정 없이 검출만 돌릴 수 있다(area_validity).
    """
    if d is None:
        return False, "감지 영역 없음"
    if int(d.get("camera_index", -1)) != int(camera_index):
        return False, "카메라 번호가 보정 때와 다름"
    want = [int(size[0]), int(size[1])]
    if [int(x) for x in (d.get("size") or [])] != want:
        return False, "해상도가 보정 때와 다름 (%s → %s)" % (
            "×".join(str(x) for x in d.get("size") or []), "×".join(str(x) for x in want))
    if not d.get("area"):
        return False, "감지 영역 없음"
    if need_h and d.get("H") is None:
        return False, "판 보정 없음"
    return True, ""


def area_validity(d, camera_index, size):
    """감지 영역 유효(카메라 번호 · 해상도 일치 + area 있음). 판 보정은 보지 않는다."""
    return validity(d, camera_index, size, need_h=False)


def summary(d):
    """화면용 요약(state.plate)."""
    if d is None:
        return None
    return {"created": d.get("created"), "camera_index": d.get("camera_index"),
            "size": d.get("size"), "area": d.get("area"),
            "points": [dict(p) for p in d.get("points") or []],
            "has_h": d.get("H") is not None, "area_mm": _area_mm(d), "rms_mm": d.get("rms_mm"),
            "max_mm": d.get("max_mm"), "res_limit_mm": d.get("res_limit_mm"),
            "n": len(d.get("points") or [])}


# --------------------------------------------------------------------------
# 감지 영역
# --------------------------------------------------------------------------
def order_quad(pts):
    """네 점을 중심 둘레 각도순(화면에서 시계방향)으로 정렬한다."""
    P = np.asarray(pts, np.float64).reshape(-1, 2)
    c = P.mean(axis=0)
    idx = np.argsort(np.arctan2(P[:, 1] - c[1], P[:, 0] - c[0]))
    return P[idx]


def check_quad(pts, size):
    """(정렬된 사각형 [[u,v]x4] | None, 사유). 볼록·프레임 안·최소 넓이를 본다."""
    try:
        P = np.asarray(pts, np.float64).reshape(4, 2)
    except (ValueError, TypeError):
        return None, "꼭짓점 4개 필요"
    if not np.all(np.isfinite(P)):
        return None, "좌표 오류"
    w, h = int(size[0]), int(size[1])
    if (P[:, 0].min() < 0 or P[:, 1].min() < 0 or P[:, 0].max() > w - 1
            or P[:, 1].max() > h - 1):
        return None, "꼭짓점이 프레임 밖"
    Q = order_quad(P)
    if not cv2.isContourConvex(Q.astype(np.float32).reshape(-1, 1, 2)):
        return None, "볼록 사각형이 아님"
    if cv2.contourArea(Q.astype(np.float32)) < MIN_AREA_FRAC * w * h:
        return None, "감지 영역이 너무 작음"
    return [[round(float(u), 1), round(float(v), 1)] for u, v in Q], ""


def crop_rect(quad, shape):
    """사각형을 감싸는 영역 (u0, v0, u1, v1) 프레임 안으로 자른다."""
    h, w = shape[:2]
    Q = np.asarray(quad, np.float64)
    u0 = max(0, int(math.floor(Q[:, 0].min())))
    v0 = max(0, int(math.floor(Q[:, 1].min())))
    u1 = min(w, int(math.ceil(Q[:, 0].max())) + 1)
    v1 = min(h, int(math.ceil(Q[:, 1].max())) + 1)
    return u0, v0, u1, v1


# --------------------------------------------------------------------------
# 판 보정
# --------------------------------------------------------------------------
def px_to_mm(H, u, v):
    p = np.asarray(H) @ np.array([float(u), float(v), 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def fit(points):
    """points [{x, y, u, v}] -> {"H", "res", "rms", "max"}. 점 4개 미만·퇴화면 None.

    4점 이상 최소제곱(cv2.findHomography method=0). 점이 한 직선 위이면 풀리지 않으므로
    px 볼록껍질 넓이로 걸러 낸다.
    """
    if len(points) < MIN_POINTS:
        return None
    src = np.array([[p["u"], p["v"]] for p in points], np.float64)
    dst = np.array([[p["x"], p["y"]] for p in points], np.float64)
    hull = cv2.convexHull(src.astype(np.float32))
    if cv2.contourArea(hull) < 1.0 or np.ptp(dst[:, 0]) < 1e-6 and np.ptp(dst[:, 1]) < 1e-6:
        return None
    H, _m = cv2.findHomography(src, dst, 0)
    if H is None or not np.all(np.isfinite(H)):
        return None
    res = [math.hypot(px_to_mm(H, p["u"], p["v"])[0] - p["x"],
                      px_to_mm(H, p["u"], p["v"])[1] - p["y"]) for p in points]
    return {"H": np.asarray(H, np.float64), "res": res,
            "rms": float(np.sqrt(np.mean(np.square(res)))), "max": float(max(res))}


def local_mm_per_px(H, center):
    """center(px) 에서 호모그래피의 국소 mm/px (두 축 보폭의 기하평균)."""
    u, v = float(center[0]), float(center[1])
    x0, y0 = px_to_mm(H, u, v)
    xu, yu = px_to_mm(H, u + 1.0, v)
    xv, yv = px_to_mm(H, u, v + 1.0)
    a = math.hypot(xu - x0, yu - y0)
    b = math.hypot(xv - x0, yv - y0)
    return math.sqrt(max(a * b, 1e-18))


def _area_mm(d):
    """감지 영역 꼭짓점의 스테이지 mm(맵 표시용). 판 보정이 없으면 None."""
    if d.get("H") is None or not d.get("area"):
        return None
    return [[round(v, 2) for v in px_to_mm(d["H"], u, v_)] for u, v_ in d["area"]]


def cal_dict(d):
    """calib.px_to_mm / mm_to_px 가 그대로 쓰는 변환 dict (engine · photohome 공용)."""
    H = np.asarray(d["H"], np.float64)
    src = np.array([[p["u"], p["v"]] for p in d.get("points") or []], np.float64)
    dst = np.array([[p["x"], p["y"]] for p in d.get("points") or []], np.float64)
    A = calib.fit_affine(src, dst) if len(src) >= 3 else np.array([[0.0, 0, 0], [0, 0, 0]])
    return {"homography": H, "affine": A, "transform": "homography", "mode": "fixed",
            "used_ids": [], "pixels": {}, "corners_px": [],
            "corner_rms_mm": float(d.get("rms_mm") or 0.0),
            "corner_max_mm": float(d.get("max_mm") or 0.0), "rotation_deg": 0.0,
            "created": d.get("created"), "n_points": len(src)}


_CACHE = {"key": None, "cal": None}


def current_cal(path=None):
    """저장된 판 보정의 변환 dict (파일이 바뀌면 다시 읽는다). 없으면 None."""
    p = path or _path()
    try:
        key = (p, os.path.getmtime(p), os.path.getsize(p))
    except OSError:
        _CACHE.update(key=None, cal=None)
        return None
    if _CACHE["key"] != key:
        d = load(p)
        _CACHE["cal"] = cal_dict(d) if d is not None and d.get("H") is not None else None
        _CACHE["key"] = key
    return _CACHE["cal"]


def new_record(camera_index, size, old=None):
    """새 파일 본문. old 와 카메라·해상도가 같으면 그 값을 이어받는다."""
    d = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "camera_index": int(camera_index),
         "size": [int(size[0]), int(size[1])], "area": None, "points": [], "H": None,
         "rms_mm": None, "max_mm": None, "res_limit_mm": None}
    if old and int(old.get("camera_index", -1)) == int(camera_index) \
            and [int(x) for x in old.get("size") or []] == d["size"]:
        for k in ("area", "points", "H", "rms_mm", "max_mm", "res_limit_mm"):
            d[k] = old.get(k)
    return d


# --------------------------------------------------------------------------
# 검출 (감지 영역 crop -> 필요하면 축소 -> detect -> 원본 좌표)
# --------------------------------------------------------------------------
def sense_fixed(bgr, params, d, log=print):
    """한 프레임에서 '감지 영역 crop -> 검출 -> 원본 프레임 좌표 -> mm' 까지.

    calib.sense 와 같은 모양의 dict 를 돌려준다(cal, det, rows, rect, refit, vis_crop,
    info) + quad · scale · mm_per_px · timing. 판 마커는 보지 않는다.
    """
    quad = np.asarray(d["area"], np.float64)
    u0, v0, u1, v1 = crop_rect(quad, bgr.shape)
    crop = bgr[v0:v1, u0:u1].copy()
    ch, cw = crop.shape[:2]
    mask = np.zeros((ch, cw), np.uint8)
    cv2.fillConvexPoly(mask, np.round(quad - [u0, v0]).astype(np.int32), 255)
    inside = mask > 0
    if inside.any():
        # 사각형 밖은 안쪽 판의 중앙 색으로 채워 평평하게 만든다 - 검출이 볼 것이 없다.
        fill = np.median(crop[inside].reshape(-1, 3), axis=0)
        crop[~inside] = fill.astype(np.uint8)

    maxpx = int(params.get("detect_max_px", 2000) or 0)
    s = 1.0
    small = crop
    if maxpx > 0 and max(ch, cw) > maxpx:
        s = maxpx / float(max(ch, cw))
        small = cv2.resize(crop, (max(8, int(round(cw * s))), max(8, int(round(ch * s)))),
                           interpolation=cv2.INTER_AREA)

    H = None if d.get("H") is None else np.asarray(d["H"], np.float64)
    cen = quad.mean(axis=0)
    if H is not None:
        mm_px = local_mm_per_px(H, cen)           # 원본 px 당 mm
        log("배율      : 판 보정값 %.5f mm/px" % mm_px)
    else:                                         # 검출만 모드: 대략 배율(면적 필터에만 쓴다)
        mm_px = float((params.get("calib") or {}).get("est_mm_per_px", 0.143))
        log("배율      : 대략값 %.5f mm/px (설정 calib.est_mm_per_px · 판 보정 전)" % mm_px)
    p = dict(params)
    # 검출 로직은 그대로: 철판 모드 + 스케일 폴백만 쓴다. marker_mm 0 = 마커 검출 끔.
    p.update({"surface": "plate", "marker_mm": 0, "plate_mm_per_px": mm_px / s,
              "plate_margin_pct": 0})
    log("감지영역  : 사각형 (%d,%d)-(%d,%d)  %dx%d px  축소 x%.3f → %dx%d px  mm/px %.5f"
        % (u0, v0, u1, v1, cw, ch, s, small.shape[1], small.shape[0], mm_px / s))
    det = detect.detect(small, p)
    det.warnings = [w for w in det.warnings if not any(w.startswith(q) for q in _QUIET_WARN)]
    det.wafer.mm_per_px = mm_px / s

    # 사각형 경계에 닿거나 밖인 검출은 버린다. 안쪽 중앙값으로 채운 바깥과의 이음매나 새겨진
    # 사각형의 선이 칩처럼 잡히기 때문이다. 버린 뒤 번호를 1부터 다시 매긴다(그림도 그 번호로).
    margin_px = float(params.get("calib", {}).get("area_margin_mm", 1.0)) / max(mm_px / s, 1e-9)
    q_small = ((quad - [u0, v0]) * s).astype(np.float32).reshape(-1, 1, 2)
    kept, dropped = [], 0
    for x in det.samples:
        pts = x.get("vertices_px") or [[x["x_px"], x["y_px"]]]
        dmin = min(cv2.pointPolygonTest(q_small, (float(a), float(b)), True) for a, b in pts)
        dc = cv2.pointPolygonTest(q_small, (float(x["x_px"]), float(x["y_px"])), True)
        if dmin < margin_px or dc < margin_px:
            dropped += 1
            continue
        kept.append(x)
    if dropped:
        log("경계    : 감지 영역 경계에 닿거나 밖인 검출 %d개 제외" % dropped)
    for i, x in enumerate(kept, 1):
        x["no"] = i
    det.samples = kept

    vis = detect.annotate(small, det, ())
    if s != 1.0:
        vis = cv2.resize(vis, (cw, ch), interpolation=cv2.INTER_LINEAR)

    log("샘플      : %d 개 (검출 %.0f ms)" % (len(det.samples), det.detect_ms))
    for x in det.warnings:
        log("경고      : %s" % x)
    inv = 1.0 / s
    for x in det.samples:
        x["x_px"] = round(float(x["x_px"]) * inv + u0, 1)
        x["y_px"] = round(float(x["y_px"]) * inv + v0, 1)
        if x.get("vertices_px"):
            x["vertices_px"] = [[int(round(a * inv)) + u0, int(round(b * inv)) + v0]
                                for a, b in x["vertices_px"]]
    rows = []
    for x in det.samples:
        u, v = float(x["x_px"]), float(x["y_px"])
        mx, my = (None, None) if H is None else px_to_mm(H, u, v)
        rows.append((x["no"], u, v, mx, my))

    cal = None if H is None else cal_dict(d)
    info = ["samples: %d" % len(det.samples),
            ("calib: NONE (detect only, est %.4f mm/px)" % mm_px) if cal is None else
            "calib: FIXED plate  %d pts  rms %.2f max %.2f mm"
            % (cal["n_points"], cal["corner_rms_mm"], cal["corner_max_mm"]),
            "sensing: quad %dx%d px  scale x%.3f" % (cw, ch, s)]
    return {"cal": cal, "det": det, "rows": rows, "rect": (u0, v0, u1, v1), "refit": False,
            "vis_crop": vis, "info": info, "quad": [[float(a), float(b)] for a, b in quad],
            "scale": s, "mm_per_px": mm_px, "detect_ms": det.detect_ms, "mode": "fixed",
            "area_only": H is None}
