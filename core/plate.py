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

from core import calib, detect, fixed_detect, labels, paths

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
            "exclude": [[[float(a), float(b)] for a, b in poly] for poly in d.get("exclude") or []],
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


def prep_crop(bgr, quad, exclude=None):
    """감지 영역 crop 준비. (crop, valid, (u0, v0), quad_local).

    바깥(사각형 밖 · 제외 영역 안)은 안쪽 판의 중앙 색으로 채워 평평하게 만든다 - 검출이 볼 것이 없다.
    valid 는 검출 대상(사각형 안 · 제외 영역 밖) 255 마스크. exclude = [[[u,v]..], ..] 원본 px 다각형.
    """
    Q = np.asarray(quad, np.float64)
    u0, v0, u1, v1 = crop_rect(Q, bgr.shape)
    crop = bgr[v0:v1, u0:u1].copy()
    ch, cw = crop.shape[:2]
    valid = np.zeros((ch, cw), np.uint8)
    cv2.fillConvexPoly(valid, np.round(Q - [u0, v0]).astype(np.int32), 255)
    for poly in (exclude or []):
        if len(poly) >= 3:
            cv2.fillPoly(valid, [np.round(np.asarray(poly, np.float64) - [u0, v0]).astype(np.int32)], 0)
    inside = valid > 0
    if inside.any():
        fill = np.median(crop[inside].reshape(-1, 3), axis=0)
        crop[~inside] = fill.astype(np.uint8)
    return crop, valid, (u0, v0), Q - [u0, v0]


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


def area_side_px(quad):
    """감지 영역 네 변의 픽셀 길이: (가로 평균, 세로 평균) = (위 · 아래 변, 왼 · 오른 변). 꼭짓점은 시계방향 정렬."""
    q = order_quad(quad)
    d = lambda a, b: float(np.hypot(*(q[a] - q[b])))            # noqa: E731
    top, right, bottom, left = d(0, 1), d(1, 2), d(2, 3), d(3, 0)
    return (top + bottom) / 2.0, (left + right) / 2.0


def area_scale(quad, size_mm):
    """감지 영역 실제 크기(가로 · 세로 mm)로 잰 배율. (mm/px, 가로 배율, 세로 배율) | None.
    사각형 변의 가로 · 세로가 실제 가로 · 세로에 대응한다고 본다(정사각 판이면 어느 쪽이든 같다)."""
    try:
        wmm, hmm = float(size_mm[0]), float(size_mm[1])
    except (TypeError, ValueError, IndexError):
        return None
    if quad is None or len(quad) == 0 or wmm <= 0 or hmm <= 0:
        return None
    wpx, hpx = area_side_px(quad)
    if wpx < 1 or hpx < 1:
        return None
    sx, sy = wmm / wpx, hmm / hpx
    return math.sqrt(sx * sy), sx, sy


def area_size_warning(d, size_mm):
    """판 보정으로 잰 감지 영역 변 길이가 area_size_mm 와 2% 넘게 다르면 경고 한 줄, 아니면 ""."""
    am = _area_mm(d)
    if am is None:
        return ""
    q = np.asarray(order_quad(am))
    ln = lambda a, b: float(np.hypot(*(q[a] - q[b])))           # noqa: E731
    wmm, hmm = (ln(0, 1) + ln(2, 3)) / 2.0, (ln(1, 2) + ln(3, 0)) / 2.0
    try:
        w0, h0 = float(size_mm[0]), float(size_mm[1])
    except (TypeError, ValueError, IndexError):
        return ""
    ew, eh = abs(wmm - w0) / w0, abs(hmm - h0) / h0
    if max(ew, eh) <= 0.02:
        return ""
    return "감지 영역 크기 %.1f × %.1f mm · 설정 %.0f × %.0f mm 와 %.1f%% 다름 · 판 보정점 · 감지 영역 확인" % (
        wmm, hmm, w0, h0, max(ew, eh) * 100.0)


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
        for k in ("area", "points", "H", "rms_mm", "max_mm", "res_limit_mm", "exclude"):
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
    crop, valid, (u0, v0), quad_l = prep_crop(bgr, quad, d.get("exclude"))
    ch, cw = crop.shape[:2]
    u1, v1 = u0 + cw, v0 + ch

    H = None if d.get("H") is None else np.asarray(d["H"], np.float64)
    cen = quad.mean(axis=0)
    if H is not None:
        mm_px = local_mm_per_px(H, cen)           # 원본 px 당 mm
        log("배율      : 판 보정값 %.5f mm/px" % mm_px)
    else:                                         # 검출만 모드: 감지 영역 실제 크기로 배율(없으면 예비값)
        asc = area_scale(quad, (params.get("calib") or {}).get("area_size_mm"))
        if asc is not None:
            mm_px = asc[0]
            log("배율      : 감지 영역 크기로 배율 %.4f mm/px (가로 %.4f · 세로 %.4f · 판 보정 전)" % asc)
        else:
            mm_px = float((params.get("calib") or {}).get("est_mm_per_px", 0.143))
            log("배율      : 예비값 %.5f mm/px (설정 calib.est_mm_per_px · 감지 영역 크기 없음)" % mm_px)

    use_fixed = str((params.get("fixed") or {}).get("detector", "fixed")) != "legacy"
    if use_fixed:
        # 고정 환경 전용 검출기(core/fixed_detect.py). 작업 해상도 축소는 내부에서 한다.
        s = 1.0
        small = crop
        ref_crop, ref_note = None, ""
        if bool((params.get("fixed") or {}).get("use_ref")):
            ref_crop, ref_note = load_ref_crop(params, d, (u0, v0, u1, v1), crop)
            log("빈 판 기준: %s" % (ref_note or "사용"))
        log("감지영역  : 사각형 (%d,%d)-(%d,%d)  %dx%d px  mm/px %.5f · 제외 영역 %d개"
            % (u0, v0, u1, v1, cw, ch, mm_px, len(d.get("exclude") or [])))
        det = fixed_detect.detect(crop, params, mm_px, valid, ref=ref_crop, area_poly=quad_l, log=log)
        det.wafer.mm_per_px = mm_px
        # 감지 영역 경계 밖이나 제외 영역에 중심이 있는 검출은 결과에서 뺀다(표시도 하지 않는다).
        inside, out = [], 0
        for x in det.samples:
            cx, cy = int(round(x["x_px"])), int(round(x["y_px"]))
            if 0 <= cx < cw and 0 <= cy < ch and valid[cy, cx] > 0:      # 크롭 밖(예측 칸 포함)도 영역 밖
                inside.append(x)
            else:
                out += 1
        if out:
            log("경계 · 제외 : 감지 영역 밖이나 제외 영역에 중심이 있는 검출 %d개 뺌" % out)
        det.samples = order_samples(inside, H, (u0, v0), mm_px, det.diag, log)
        kept = det.samples
    else:
        maxpx = int(params.get("detect_max_px", 2000) or 0)
        s = 1.0
        small = crop
        if maxpx > 0 and max(ch, cw) > maxpx:
            s = maxpx / float(max(ch, cw))
            small = cv2.resize(crop, (max(8, int(round(cw * s))), max(8, int(round(ch * s)))),
                               interpolation=cv2.INTER_AREA)
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

    if use_fixed:
        vis = annotate_fixed(crop, det.samples)         # 라벨 한 벌 · 얇은 윤곽 · 겹침 회피(화면과 같은 규칙)
    else:
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


# --------------------------------------------------------------------------
# 번호 · 저장용 그림 (고정 환경)
# --------------------------------------------------------------------------
def order_samples(samples, H, off, mm_px, diag, log=print):
    """새 촬영의 첫 번호 매김: 위치 순서(행 우선). 판 보정이 있으면 스테이지 좌표, 없으면 사진 좌표.
    격자 배치(칸 인덱스가 있으면)는 격자 인덱스 순서. 행 묶음 허용 오차 = 칩 긴 변의 절반. 편집 때는 다시 매기지 않는다."""
    if not samples:
        return samples
    u0, v0 = off

    def xy(x, y):                                     # 사진 crop 좌표 → 번호 기준 좌표
        return px_to_mm(H, x + u0, y + v0) if H is not None else (x, y)

    pts = [xy(s["x_px"], s["y_px"]) for s in samples]
    grid = (diag or {}).get("grid")
    if grid and grid.get("uv") and all("_slot" in s for s in samples):
        cx, cy = np.mean([s["x_px"] for s in samples]), np.mean([s["y_px"] for s in samples])
        c0 = np.array(xy(cx, cy))
        vec = []
        for d_ in grid["uv"]:
            p1 = np.array(xy(cx + d_[0], cy + d_[1]))
            vec.append(tuple(p1 - c0))
        order = labels.grid_order([s["_slot"] for s in samples], (vec[0], vec[1]))
        how = "격자 인덱스"
    else:
        chip = float((diag or {}).get("chip_long_px") or 0.0)
        if chip <= 0:
            chip = 11.0 / max(mm_px, 1e-9)
        tol = 0.5 * chip * (mm_px if H is not None else 1.0)
        order = labels.row_major_order(pts, tol)
        how = "행 묶음 %.1f %s" % (tol, "mm" if H is not None else "px")
    out = [samples[i] for i in order]
    for n, s in enumerate(out, 1):
        s["no"] = n
    log("번호      : 위치 순서(행 우선) · %s 기준 · %s" % ("스테이지 좌표" if H is not None else "사진 좌표", how))
    return out


_STATE_BGR = {"detected": (90, 200, 60), "local": (255, 190, 60), "predicted": (60, 160, 255)}


def annotate_fixed(crop, samples):
    """저장용 annotated.jpg 의 샘플 표시(고정 환경): 얇은 윤곽(색은 상태에만: 검출 · 보완 · 예측 점선 원 + ?) +
    번호 하나. 라벨은 칩 · 윤곽 · 다른 라벨과 겹치지 않는 바깥 빈 자리에 두고 짧은 지시선으로 잇는다.
    좌표 숫자는 쓰지 않는다(목록 · 툴팁에만)."""
    img = crop.copy()
    H_, W_ = img.shape[:2]
    k = max(1.0, max(W_, H_) / 1280.0)
    th = max(1, int(round(1.0 * k)))
    font = cv2.FONT_HERSHEY_SIMPLEX
    fs = 0.5 * k
    items, texts = [], []
    for s in samples:
        col = _STATE_BGR.get(s.get("source") or "detected", _STATE_BGR["detected"])
        if s.get("source") == "predicted":
            c = (int(round(s["x_px"])), int(round(s["y_px"])))
            r = int(round(22 * k))
            for a in range(0, 360, 30):                       # 점선 원
                cv2.ellipse(img, c, (r, r), 0, a, a + 16, col, th, cv2.LINE_AA)
            bb = (c[0] - r, c[1] - r, c[0] + r, c[1] + r)
        else:
            poly = np.array(s.get("vertices_px") or [], np.int32).reshape(-1, 1, 2)
            if len(poly) >= 3:
                cv2.polylines(img, [poly], True, col, th, cv2.LINE_AA)
                q = poly.reshape(-1, 2)
                bb = (int(q[:, 0].min()), int(q[:, 1].min()), int(q[:, 0].max()), int(q[:, 1].max()))
            else:
                c = (int(round(s["x_px"])), int(round(s["y_px"])))
                bb = (c[0] - 8, c[1] - 8, c[0] + 8, c[1] + 8)
        t = str(s["no"]) + ("+" if s.get("source") == "local" else "") + ("?" if (s.get("weak") or s.get("source") == "predicted") else "")
        (tw, thh), base = cv2.getTextSize(t, font, fs, th)
        items.append({"bbox": bb})
        texts.append((t, tw + 4, thh + base + 4, col))
    if not items:
        return img
    pl = labels.place(items, W_, H_, [t[1] for t in texts], [t[2] for t in texts], gap=5.0 * k)
    for p, (t, w, h, col) in zip(pl, texts):
        x0, y0, x1, y1 = p["box"]
        cv2.line(img, (int(p["from"][0]), int(p["from"][1])), (int(p["to"][0]), int(p["to"][1])), (230, 230, 230), th, cv2.LINE_AA)
        cv2.rectangle(img, (int(x0), int(y0)), (int(x1), int(y1)), (20, 20, 20), -1)
        cv2.putText(img, t, (int(x0) + 2, int(y1) - 4), font, fs, col, th, cv2.LINE_AA)
    return img


# --------------------------------------------------------------------------
# 빈 판 기준 사진 (선택 · 미검증) - 배경을 medianBlur 대신 이 사진으로 쓴다
# --------------------------------------------------------------------------
def ref_paths():
    base = os.path.dirname(_path())
    return os.path.join(base, "plate_ref.png"), os.path.join(base, "plate_ref.json")


REF_HISTORY_KEEP = 10                       # 보관하는 이전 기준 수(넘으면 오래된 것부터 지운다)


def ref_hist_dir():
    return os.path.join(os.path.dirname(_path()), "plate_ref_history")


def _stamp(meta):
    """메타의 찍은 시각 → 파일 이름(yyyymmdd_HHMMSS)."""
    t = str((meta or {}).get("time") or "")
    digits = "".join(ch for ch in t if ch.isdigit())
    return (digits[:8] + "_" + digits[8:14]) if len(digits) >= 14 else time.strftime("%Y%m%d_%H%M%S")


def ref_history():
    """보관된 이전 기준 목록(최근 것 먼저): [{name, meta}]."""
    d = ref_hist_dir()
    out = []
    try:
        names = sorted((f[:-5] for f in os.listdir(d) if f.endswith(".json")), reverse=True)
    except OSError:
        return out
    for n in names:
        try:
            with open(os.path.join(d, n + ".json"), encoding="utf-8") as f:
                m = json.load(f)
        except (OSError, ValueError):
            continue
        if os.path.exists(os.path.join(d, n + ".png")):
            out.append({"name": n, "meta": m})
    return out


def _prune_history():
    for h in ref_history()[REF_HISTORY_KEEP:]:
        for ext in (".png", ".json"):
            try:
                os.remove(os.path.join(ref_hist_dir(), h["name"] + ext))
            except OSError:
                pass


def archive_current_ref():
    """지금 기준(plate_ref.png + 메타)을 plate_ref_history/<시각>.png(+.json) 로 보관한다(이미 보관돼 있으면 건너뜀).
    반환: 보관한 이름 | None."""
    png, mp = ref_paths()
    meta = load_ref_meta()
    if not meta or not os.path.exists(png):
        return None
    name = _stamp(meta)
    d = ref_hist_dir()
    k = 1
    base = name
    while os.path.exists(os.path.join(d, name + ".json")):
        try:
            with open(os.path.join(d, name + ".json"), encoding="utf-8") as f:
                if json.load(f) == meta:
                    return None                     # 이미 보관된 같은 기준
        except (OSError, ValueError):
            pass
        k += 1
        name = "%s_%d" % (base, k)                  # 같은 초에 찍힌 다른 기준
    import shutil
    os.makedirs(d, exist_ok=True)
    shutil.copyfile(png, os.path.join(d, name + ".png"))
    with open(os.path.join(d, name + ".json"), "w", encoding="utf-8") as f:
        json.dump(meta, f, ensure_ascii=False, indent=1)
    _prune_history()
    return name


def use_ref(name):
    """보관된 기준을 현재 기준으로 되돌린다(지금 기준은 먼저 보관). 반환: (ok, 사유)."""
    import shutil
    if not name or not all(ch.isdigit() or ch == "_" for ch in str(name)):
        return False, "이름 오류"
    d = ref_hist_dir()
    src_png, src_json = os.path.join(d, name + ".png"), os.path.join(d, name + ".json")
    if not (os.path.exists(src_png) and os.path.exists(src_json)):
        return False, "보관된 기준 없음"
    archive_current_ref()
    png, mp = ref_paths()
    os.makedirs(os.path.dirname(png), exist_ok=True)
    shutil.copyfile(src_png, png)
    shutil.copyfile(src_json, mp)
    return True, ""


def ref_thumb_jpeg(name, width=160):
    """작은 미리보기 JPEG 바이트(name = 'current' | 보관 이름) 또는 None."""
    from core import imgio
    if name == "current":
        path = ref_paths()[0]
    elif name and all(ch.isdigit() or ch == "_" for ch in str(name)):
        path = os.path.join(ref_hist_dir(), name + ".png")
    else:
        return None
    img = imgio.imread_u(path) if os.path.exists(path) else None
    if img is None:
        return None
    h, w = img.shape[:2]
    img = cv2.resize(img, (width, max(1, int(round(h * width / float(w))))), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", img, [int(cv2.IMWRITE_JPEG_QUALITY), 70])
    return buf.tobytes() if ok else None


def save_ref(bgr, params, camera_index, area):
    """빈 판 사진 + 메타(카메라 번호 · 해상도 · 노출 · 화이트밸런스 · 감지 영역 · 찍은 시각).
    새로 찍으면 이전 기준은 plate_ref_history 에 보관한다(최근 REF_HISTORY_KEEP 개)."""
    from core import imgio
    archive_current_ref()
    png, meta = ref_paths()
    os.makedirs(os.path.dirname(png), exist_ok=True)
    imgio.imwrite_u(png, bgr)
    m = {"camera_index": int(camera_index), "size": [int(bgr.shape[1]), int(bgr.shape[0])],
         "exposure": float(params.get("exposure") or 0), "wb_temperature": int(params.get("wb_temperature") or 0),
         "area": None if not area else [[float(a), float(b)] for a, b in area],
         "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    with open(meta, "w", encoding="utf-8") as f:
        json.dump(m, f, ensure_ascii=False, indent=1)
    return m


def load_ref_meta():
    try:
        with open(ref_paths()[1], encoding="utf-8") as f:
            m = json.load(f)
        return m if isinstance(m, dict) else None
    except (OSError, ValueError):
        return None


def ref_validity(meta, params, camera_index, size, area):
    """(유효한가, 사유). 카메라 번호 · 해상도 · 노출 · 화이트밸런스 · 감지 영역이 촬영 때와 같아야 한다."""
    if not meta:
        return False, "빈 판 기준 없음"
    if int(meta.get("camera_index", -1)) != int(camera_index):
        return False, "카메라 번호가 다름"
    if [int(x) for x in meta.get("size") or []] != [int(size[0]), int(size[1])]:
        return False, "해상도가 다름"
    if abs(float(meta.get("exposure") or 0) - float(params.get("exposure") or 0)) > 1e-6:
        return False, "노출이 다름"
    if int(meta.get("wb_temperature") or 0) != int(params.get("wb_temperature") or 0):
        return False, "화이트밸런스가 다름"
    ma = meta.get("area")
    if not ma or not area or np.abs(np.asarray(ma, np.float64) - np.asarray(area, np.float64)).max() > 1.0:
        return False, "감지 영역이 다름"
    return True, ""


def load_ref_crop(params, d, rect, crop):
    """유효한 빈 판 기준이면 감지 영역 crop 에 맞춘 기준 crop(위상 상관으로 작은 이동 보정). (이미지|None, 메모)."""
    from core import imgio
    meta = load_ref_meta()
    size = (int(d.get("size", [0, 0])[0]), int(d.get("size", [0, 0])[1]))
    ok, why = ref_validity(meta, params, d.get("camera_index", -1), size, d.get("area"))
    if not ok:
        return None, "무효 · %s · medianBlur 로" % why
    ref = imgio.imread_u(ref_paths()[0])
    if ref is None or [ref.shape[1], ref.shape[0]] != list(size):
        return None, "무효 · 기준 사진을 읽지 못함 · medianBlur 로"
    u0, v0, u1, v1 = rect
    rc = ref[v0:v1, u0:u1].copy()
    if rc.shape != crop.shape:
        return None, "무효 · 크기 불일치 · medianBlur 로"
    g1 = cv2.cvtColor(crop, cv2.COLOR_BGR2GRAY)
    g2 = cv2.cvtColor(rc, cv2.COLOR_BGR2GRAY)
    k = 0.25
    a = cv2.resize(g1, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    b = cv2.resize(g2, None, fx=k, fy=k, interpolation=cv2.INTER_AREA)
    (dx, dy), _resp = cv2.phaseCorrelate(np.float32(b), np.float32(a))
    dx, dy = dx / k, dy / k
    note = "사용 · 이동 보정 %.1f, %.1f px" % (dx, dy)
    if abs(dx) > 60 or abs(dy) > 60:
        return None, "무효 · 이동이 너무 큼(%.0f, %.0f px) · medianBlur 로" % (dx, dy)
    if abs(dx) > 0.5 or abs(dy) > 0.5:
        M = np.float32([[1, 0, dx], [0, 1, dy]])
        rc = cv2.warpAffine(rc, M, (rc.shape[1], rc.shape[0]), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
    return rc, note
