"""calib.py - 카메라 픽셀 <-> 갠트리 기계좌표(mm) 변환 도구.

CLI 전용이다. cv2.imshow 도 Tkinter 도 쓰지 않는다 (헤드리스/원격에서 돌아가야
하고, 결과는 전부 콘솔 텍스트와 파일로 남긴다).

사용법
  python -m core.calib fit     [--image photo.png]   # 마커로 변환행렬을 구해 저장
  python -m core.calib px2mm   700 500               # 픽셀 -> 기계좌표
  python -m core.calib mm2px   100 50                # 기계좌표 -> 픽셀
  python -m core.calib samples [--image photo.png]   # 검출된 샘플을 기계좌표로
  python -m core.calib check   [--image photo.png] [--save]   # 카메라가 움직였는지 확인

카메라 고정이 흔들려 촬영 사이에 1~2도씩 돌아가는 것이 확인됐다 (화면 중앙에서
130mm 떨어진 점이 1도에 2.3mm 움직인다). 마커 4개는 베이스에 고정돼 있으므로,
변환은 저장된 값이 아니라 '지금 찍은 그 사진' 의 마커로 구하는 것이 원칙이다.
samples 는 매번 다시 보정하고, 마커를 못 찾을 때만 저장값으로 떨어진다.

촬영은 camera.py 의 방식(인덱스 1, 단발, 연속 프레임 중 가장 선명한 장)을 그대로
쓰고, 마커 검출은 detect.py 의 find_markers 와 같은 딕셔너리(DICT_4X4_50)와
기본 DetectorParameters 를 쓴다. 샘플 검출은 detect.detect() 를 그대로 호출한다
(검출 로직에는 손대지 않는다).
"""

import argparse
import json
import math
import os
import sys
import time

import cv2
import numpy as np

from . import camera
from . import detect
from . import imgio
from . import paths

HERE = paths.PROJECT_ROOT
SETTINGS = paths.SETTINGS_PATH      # 경로는 전부 paths.py 를 거친다
MATRIX = paths.CALIB_PATH           # data 폴더에 둔다 (git 밖)

# 베이스에 붙은 마커의 기계좌표(mm). 마커 배치가 바뀌면 여기만 고치면 된다.
# 실제 값은 settings.json 의 "marker_mm_xy" 이고, 아래는 그 키가 없을 때 쓰는
# 기본값이다(드라이버 포인터 실측).
MARKER_MM_DEFAULT = {3: (15, 20), 2: (15, 160), 1: (201, 19), 0: (201, 160)}
MARKER_MM = dict(MARKER_MM_DEFAULT)                                    # id: (X, Y)


def set_marker_mm(table):
    """settings 의 marker_mm_xy({"3":[15,20],...}) 를 MARKER_MM 에 반영한다.

    다른 모듈이 calib.MARKER_MM 을 직접 참조하므로 새 dict 로 갈아끼우지 않고
    내용을 바꾼다 (설정을 저장하면 곧바로 다음 촬영에 반영된다).
    """
    if not isinstance(table, dict) or not table:
        return MARKER_MM
    got = {}
    for k, v in table.items():
        try:
            got[int(k)] = (float(v[0]), float(v[1]))
        except (TypeError, ValueError, IndexError):
            continue
    if got:
        MARKER_MM.clear()
        MARKER_MM.update(got)
    return MARKER_MM


def _load_marker_mm_from_settings():
    try:
        with open(SETTINGS, "r", encoding="utf-8") as f:
            set_marker_mm(json.load(f).get("marker_mm_xy"))
    except Exception:                      # noqa: BLE001
        pass                               # 설정이 없거나 깨져도 기본값으로 돈다


_load_marker_mm_from_settings()

# 갠트리 가동범위. 이 밖의 좌표는 경고만 하고 값은 그대로 보여준다.
AXIS_MIN, AXIS_MAX = 0.0, 247.0

FONT = cv2.FONT_HERSHEY_SIMPLEX


# --------------------------------------------------------------------------
# 공통
# --------------------------------------------------------------------------
def load_params():
    """detect.DEFAULTS + settings.json (main.py 와 같은 값을 쓴다)."""
    p = dict(detect.DEFAULTS)
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as f:
                p.update(json.load(f))
        except Exception as e:
            print("settings.json 을 읽지 못해 기본값을 씁니다: %s" % e)
    return p


def get_image(path, params):
    """--image 가 있으면 그 파일, 없으면 카메라로 단발 촬영."""
    if path:
        bgr = imgio.imread_u(path, cv2.IMREAD_COLOR)
        if bgr is None:
            print("이미지를 열 수 없습니다: %s" % path)
            return None
        print("입력      : %s (%dx%d)" % (path, bgr.shape[1], bgr.shape[0]))
        return bgr
    try:
        cam = camera.Camera(params).open()
    except camera.CameraError as e:
        print(str(e))
        return None
    try:
        print("카메라    : index=%s backend=%s fourcc=%s"
              % (cam.info["index"], cam.info["backend"], cam.info["actual_fourcc"]))
        frame, stats = cam.capture_best()      # 연속 프레임 중 가장 선명한 한 장
        print("촬영      : %d 프레임 중 선명도 %.1f 인 장을 사용"
              % (stats["frames_grabbed"], stats["chosen_focus_score"]))
        return frame
    finally:
        cam.release()


def detect_markers(bgr, params):
    """id -> {"center_px", "corners"}.

    detect.find_markers 와 같은 딕셔너리/파라미터를 쓰되, 그쪽의 '스케일 타당성
    가드' 는 여기서 쓰지 않는다. 그 가드는 mm/px 를 재기 위한 것이고, 여기서는
    마커의 기계좌표를 이미 알고 있으므로 잡힌 것을 그대로 쓰는 편이 맞다.
    """
    aruco = getattr(cv2, "aruco", None)
    if aruco is None or not hasattr(aruco, "ArucoDetector"):
        print("cv2.aruco 를 쓸 수 없습니다. opencv-python 을 4.7 이상으로 올려주세요.")
        return {}
    name = "DICT_" + str(params.get("marker_dict", "4X4_50"))
    dic = aruco.getPredefinedDictionary(getattr(aruco, name, aruco.DICT_4X4_50))
    dp = aruco.DetectorParameters()
    # 보정은 모서리 좌표를 그대로 쓰므로 서브픽셀 정밀화를 켠다. 픽셀 단위로만
    # 잡으면 30mm 마커의 모서리 하나가 0.4mm 씩 튄다.
    dp.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    corners, ids, _rej = aruco.ArucoDetector(dic, dp).detectMarkers(bgr)
    found = {}
    if ids is None:
        return found
    for c, i in zip(corners, ids.reshape(-1)):
        q = np.asarray(c).reshape(4, 2).astype(np.float64)
        found[int(i)] = {"center_px": (float(q[:, 0].mean()), float(q[:, 1].mean())),
                         "corners": q}
    return found


def fit_affine(src, dst):
    """전체 점 최소제곱 어파인. src(px) -> dst(mm), 2x3.

    cv2.estimateAffine2D 는 (method 를 뭘 주든) 로버스트 추정이라 점이 4개면
    3개만 맞추고 남은 하나를 이상치로 버린다. 실제로 잔차가 0/0/0/4.075 로
    나왔다 - 네 점을 고루 맞춘 결과가 아니다. 여기서는 정직하게 최소제곱을 푼다.
    """
    M = np.hstack([np.asarray(src, np.float64), np.ones((len(src), 1))])
    sol, _res, _rank, _sv = np.linalg.lstsq(M, np.asarray(dst, np.float64), rcond=None)
    return sol.T


def fit_homography(src, dst):
    """평면 대 평면 정사영. 카메라가 기울어져 생기는 원근까지 표현한다."""
    H, _m = cv2.findHomography(np.asarray(src, np.float64),
                               np.asarray(dst, np.float64), 0)
    return None if H is None else np.asarray(H, np.float64)


def load_matrix():
    if not os.path.exists(MATRIX):
        print("calib_matrix.json 이 없습니다. 먼저 'python -m core.calib fit' 을 실행하세요.")
        return None
    with open(MATRIX, "r", encoding="utf-8") as f:
        d = json.load(f)
    if "transform" not in d and d.get("pixels") and d.get("marker_mm"):
        # 옛 파일(로버스트 어파인만 저장하던 버전)은 저장된 점으로 다시 푼다.
        # 점이 진짜 데이터이고 행렬은 거기서 유도되는 값이므로 이게 안전하다.
        ids = [str(i) for i in d.get("used_ids", sorted(d["pixels"]))]
        src = np.array([d["pixels"][i] for i in ids], np.float64)
        dst = np.array([d["marker_mm"][i] for i in ids], np.float64)
        d["affine"] = fit_affine(src, dst)
        d["homography"] = fit_homography(src, dst) if len(ids) >= 4 else None
        d["transform"] = "homography" if d.get("homography") is not None else "affine"
        print("안내      : 예전 형식의 calib_matrix.json 이라 저장된 마커 좌표로 "
              "변환행렬을 다시 계산했습니다 (모델: %s)." % d["transform"])
        return d
    d["affine"] = np.asarray(d["affine"], np.float64)
    d["homography"] = (np.asarray(d["homography"], np.float64)
                       if d.get("homography") is not None else None)
    d.setdefault("transform", "affine")
    return d


def affine_px2mm(A, u, v):
    p = np.asarray(A) @ np.array([float(u), float(v), 1.0])
    return float(p[0]), float(p[1])


def affine_mm2px(A, x, y):
    """어파인의 역변환. 2x3 을 3x3 으로 채워 역행렬을 쓴다."""
    M = np.vstack([np.asarray(A), [0.0, 0.0, 1.0]])
    q = np.linalg.inv(M) @ np.array([float(x), float(y), 1.0])
    return float(q[0]), float(q[1])


def homo_px2mm(H, u, v):
    p = np.asarray(H) @ np.array([float(u), float(v), 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


def homo_mm2px(H, x, y):
    q = np.linalg.inv(np.asarray(H)) @ np.array([float(x), float(y), 1.0])
    return float(q[0] / q[2]), float(q[1] / q[2])


def model_of(d, model=None):
    """쓸 변환 이름. 요청한 모델이 없으면 있는 것으로 떨어진다."""
    m = model or d.get("transform", "affine")
    if m == "homography" and d.get("homography") is None:
        m = "affine"
    return m


def px_to_mm(d, u, v, model=None):
    m = model_of(d, model)
    if m == "homography":
        return homo_px2mm(d["homography"], u, v)
    return affine_px2mm(d["affine"], u, v)


def mm_to_px(d, x, y, model=None):
    m = model_of(d, model)
    if m == "homography":
        return homo_mm2px(d["homography"], x, y)
    return affine_mm2px(d["affine"], x, y)


def perspective_gap(A, H, src):
    """마커 사각형 안 5x5 격자에서 두 모델 예측이 얼마나 벌어지는지(mm).

    카메라가 완전히 수직이 아니면 네 점이 평행사변형에서 벗어난다(실측: 오른쪽
    열이 왼쪽 열보다 2.1% 짧다). 어파인은 그 원근을 표현할 수 없고 호모그래피는
    평면에 대해 정확하므로, 이 값이 곧 '어파인을 쓰면 생기는 오차' 다.
    """
    P = np.asarray(src, np.float64)
    if H is None or len(P) < 4:
        return 0.0                      # 사각형 격자를 만들 수 없다 (마커 3개)
    ctr = P.mean(axis=0)
    Q = P[np.argsort(np.arctan2(P[:, 1] - ctr[1], P[:, 0] - ctr[0]))]  # 사각형 순서로
    worst = 0.0
    for u in np.linspace(0.0, 1.0, 5):
        top = Q[0] * (1 - u) + Q[1] * u
        bot = Q[3] * (1 - u) + Q[2] * u
        for v in np.linspace(0.0, 1.0, 5):
            p = top * (1 - v) + bot * v
            ax, ay = affine_px2mm(A, p[0], p[1])
            hx, hy = homo_px2mm(H, p[0], p[1])
            worst = max(worst, float(np.hypot(ax - hx, ay - hy)))
    return worst


def in_range(x, y):
    return AXIS_MIN <= x <= AXIS_MAX and AXIS_MIN <= y <= AXIS_MAX


def warn_range(x, y):
    if not in_range(x, y):
        print("경고      : 가동범위(%.0f~%.0f mm) 밖입니다." % (AXIS_MIN, AXIS_MAX))


# --------------------------------------------------------------------------
# fit
# --------------------------------------------------------------------------
def corner_targets(A, corners_px, center_mm, half_mm):
    """마커 모서리 4개의 기계좌표를 정한다.

    1차 어파인으로 모서리를 mm 로 보낸 뒤, 중심 대비 부호로 네 사분면 중
    하나에 스냅한다 (중심 (cx,cy) 의 마커라면 모서리는 (cx±half, cy±half)).
    ArUco 가 주는 모서리 순서는 마커 회전에 따라 달라지므로 순서를 믿지 않고
    위치로 배정한다. 네 사분면이 겹치면 그 마커는 못 쓴다.
    """
    cx, cy = float(center_mm[0]), float(center_mm[1])
    out, seen = [], set()
    for u, v in corners_px:
        mx, my = affine_px2mm(A, u, v)
        sx = 1.0 if mx >= cx else -1.0
        sy = 1.0 if my >= cy else -1.0
        key = (sx, sy)
        if key in seen:
            return None                     # 두 모서리가 같은 사분면 = 배정 실패
        seen.add(key)
        out.append((cx + sx * half_mm, cy + sy * half_mm))
    return out


def fit_points(src, dst, used, markers=None, half_mm=15.0):
    """점 대응만으로 변환 한 벌을 만든다 (이미지 없이도 검증할 수 있게 분리).

    src: 마커 중심 픽셀 Nx2, dst: 중심 기계좌표 Nx2, used: id 목록.

    호모그래피는 '중심 4점' 이 아니라 '모서리 전부' 로 푼다. 중심만 쓰면 마커가
    3개일 때(웨이퍼가 하나를 덮으면 실제로 일어난다) 점이 모자라 어파인으로
    떨어지고, 그 어파인은 원근을 표현 못 해 칩 위치에서 평균 2.5mm / 최대 3.4mm
    까지 틀어졌다 (실측 150621). 모서리를 쓰면 마커 3개라도 12점이라 호모그래피가
    되고(같은 사진에서 0.8mm), 4개면 16점이라 과결정이 되어 잔차가 곧 검증값이다.
    """
    src = np.asarray(src, np.float64)
    dst = np.asarray(dst, np.float64)
    # 1차: 중심으로 최소제곱 어파인. 모서리를 mm 에 배정하는 기준자로 쓴다.
    # (점이 3~4개뿐이라 로버스트 추정은 해가 되기만 한다 - 하나를 버린다.)
    A = fit_affine(src, dst)

    errs, resid = [], {}
    for k, i in enumerate(used):
        px, py = affine_px2mm(A, src[k][0], src[k][1])
        e = float(np.hypot(px - dst[k][0], py - dst[k][1]))
        errs.append(e)
        resid[str(i)] = round(e, 4)

    # 모서리 대응 모으기
    cpx, cmm, cids, warns = [], [], [], []
    if markers:
        for k, i in enumerate(used):
            q = markers.get(i, {}).get("corners")
            if q is None:
                continue
            q = np.asarray(q, np.float64).reshape(4, 2)
            tgt = corner_targets(A, q, dst[k], half_mm)
            if tgt is None:
                warns.append("marker id%d: 모서리 사분면 배정 실패 - 제외" % i)
                continue
            for (u, v), (mx, my) in zip(q, tgt):
                cpx.append([float(u), float(v)])
                cmm.append([float(mx), float(my)])
                cids.append(i)

    Hm = fit_homography(cpx, cmm) if len(cpx) >= 8 else None
    # 기본 변환: 모서리 호모그래피(원근까지). 모서리를 못 쓰면 어파인.
    transform = "homography" if Hm is not None else "affine"

    cerr, cres = [], {}
    if Hm is not None:
        for (u, v), (mx, my), i in zip(cpx, cmm, cids):
            hx, hy = homo_px2mm(Hm, u, v)
            e = float(np.hypot(hx - mx, hy - my))
            cerr.append(e)
            cres.setdefault(str(i), []).append(round(e, 4))

    a, b, _tx = A[0]
    c, d, _ty = A[1]
    sx = float(np.hypot(a, c))
    sy = float(np.hypot(b, d))
    rot = float(np.degrees(np.arctan2(c, a)))
    cosang = float((a * b + c * d) / max(sx * sy, 1e-12))
    skew = 90.0 - float(np.degrees(np.arccos(max(-1.0, min(1.0, cosang)))))

    return {
        "transform": transform,
        "affine": A,
        "homography": Hm,
        "used_ids": list(used),
        "pixels": {str(i): [round(float(src[k][0]), 2), round(float(src[k][1]), 2)]
                   for k, i in enumerate(used)},
        "corners_px": [[round(u, 2), round(v, 2)] for u, v in cpx],
        "corners_mm": [[round(x, 2), round(y, 2)] for x, y in cmm],
        "corner_ids": cids,
        "corner_rms_mm": float(np.sqrt(np.mean(np.square(cerr)))) if cerr else 0.0,
        "corner_max_mm": float(max(cerr)) if cerr else 0.0,
        "corner_resid_mm": cres,
        "residual_mm": resid,
        "rms_mm": float(np.sqrt(np.mean(np.square(errs)))),
        "max_mm": float(max(errs)),
        "persp_mm": perspective_gap(A, Hm, src),
        "rotation_deg": rot,
        "scale": [sx, sy],
        "skew": skew,
        "markers": markers or {},
        "warnings": warns,
    }


def fit_from_image(bgr, params, verbose=False):
    """사진 한 장에서 변환 한 벌을 구한다. 기준 마커가 3개 미만이면 None.

    카메라가 촬영 사이에 돌아가므로, 좌표를 낼 때는 저장된 행렬이 아니라 이
    함수로 '그 사진' 에서 다시 구한 값을 쓰는 것이 원칙이다.
    """
    found = detect_markers(bgr, params)
    used = sorted(i for i in found if i in MARKER_MM)
    if verbose:
        missing = sorted(i for i in MARKER_MM if i not in found)
        extra = sorted(i for i in found if i not in MARKER_MM)
        print("마커      : 검출 %d개 중 기준 마커 %d개 사용 %s"
              % (len(found), len(used), used))
        if missing:
            print("            못 찾은 기준 마커 id: %s" % missing)
        if extra:
            print("            기준표에 없는 마커 id(무시): %s" % extra)
    if len(used) < 3:
        return None
    src = [found[i]["center_px"] for i in used]
    dst = [MARKER_MM[i] for i in used]
    res = fit_points(src, dst, used, found,
                     half_mm=float(params.get("marker_mm", 30.0)) / 2.0)
    if verbose:
        for w in res.get("warnings", []):
            print("            " + w)
    return res


def save_matrix(res, bgr, image_label):
    """fit 결과를 calib_matrix.json 형식으로 저장한다."""
    A, Hm = res["affine"], res["homography"]
    out = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": image_label,
        "image_size": [int(bgr.shape[1]), int(bgr.shape[0])],
        "marker_mm": {str(k): list(v) for k, v in MARKER_MM.items()},
        "used_ids": res["used_ids"],
        "pixels": res["pixels"],
        "corners_px": res.get("corners_px", []),
        "corners_mm": res.get("corners_mm", []),
        "corner_ids": res.get("corner_ids", []),
        "corner_rms_mm": round(res.get("corner_rms_mm", 0.0), 4),
        "corner_max_mm": round(res.get("corner_max_mm", 0.0), 4),
        "corner_resid_mm": res.get("corner_resid_mm", {}),
        "transform": res["transform"],
        "affine": [[float(x) for x in row] for row in A],
        "homography": ([[float(x) for x in row] for row in Hm]
                       if Hm is not None else None),
        "perspective_gap_mm": round(res["persp_mm"], 4),
        "residual_mm": res["residual_mm"],
        "rms_mm": round(res["rms_mm"], 4),
        "max_mm": round(res["max_mm"], 4),
        "scale_mm_per_px": [round(res["scale"][0], 6), round(res["scale"][1], 6)],
        "rotation_deg": round(res["rotation_deg"], 3),
        "skew_deg": round(res["skew"], 3),
    }
    with open(MATRIX, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    return MATRIX


def grid_diff_mm(new, old, n=5):
    """가동범위를 덮는 n x n 격자에서 두 변환이 얼마나 어긋나는지(mm).

    각 기계좌표를 '옛 변환' 으로 픽셀에 놓고, 그 픽셀을 '새 변환' 으로 다시
    읽는다. 곧 '옛 보정을 그대로 쓰면 생기는 좌표 오차' 다.
    """
    ds = []
    g = np.linspace(AXIS_MIN, AXIS_MAX, n)
    for x in g:
        for y in g:
            u, v = mm_to_px(old, x, y)
            nx, ny = px_to_mm(new, u, v)
            ds.append(float(np.hypot(nx - x, ny - y)))
    return float(max(ds)), float(np.mean(ds))


def cmd_fit(args):
    params = load_params()
    bgr = get_image(args.image, params)
    if bgr is None:
        return 2

    res = fit_from_image(bgr, params, verbose=True)
    if res is None:
        print("기준 마커가 3개 이상 잡혀야 합니다. 조명/초점/가림을 확인하세요.")
        return 1
    found = res["markers"]
    used = res["used_ids"]
    A, Hm = res["affine"], res["homography"]
    errs = [res["residual_mm"][str(i)] for i in used]
    rms = res["rms_mm"]

    # ---- 잔차표 ----------------------------------------------------------
    print("")
    print("id   pixel(u,v)          real(X,Y)mm      pred(X,Y)mm      err mm")
    for i in used:
        u, v = found[i]["center_px"]
        rx, ry = MARKER_MM[i]
        px, py = affine_px2mm(A, u, v)
        print("%-4d (%7.1f,%7.1f)   (%6.1f,%6.1f)   (%7.2f,%7.2f)   %6.3f"
              % (i, u, v, rx, ry, px, py, res["residual_mm"][str(i)]))
    print("(참고)어파인 RMS %.3f mm  최대 %.3f mm" % (rms, max(errs)))

    gap = res["persp_mm"]
    if Hm is not None:
        # 대표 잔차는 '모서리 호모그래피' 다. 과결정(16점)이라 이 값이 실제 검증값.
        print("모서리    : %d점 (마커 %d개) 호모그래피"
              % (len(res["corners_px"]), len(used)))
        for i in used:
            es = res["corner_resid_mm"].get(str(i))
            if es:
                print("            id%-2d 잔차 %s mm"
                      % (i, " ".join("%.2f" % e for e in es)))
        print("잔차      : RMS %.3f mm    최대 %.3f mm  (모서리 호모그래피)"
              % (res["corner_rms_mm"], res["corner_max_mm"]))
        print("(참고)원근: 어파인과 최대 %.3f mm 차이 (마커 사각형 안 5x5 격자)" % gap)
    if len(used) < 4:
        print("주의      : 마커 %d개(모서리 %d점)뿐입니다. 가려진 마커를 치우면 "
              "정확도가 올라갑니다." % (len(used), len(res["corners_px"])))
    print("기본 변환 : %s" % res["transform"])

    # ---- 행렬 해석 -------------------------------------------------------
    # A = [[a,b,tx],[c,d,ty]]. 열 벡터의 길이가 축 스케일, 첫 열의 각도가 회전,
    # 두 열 사이 각이 90도에서 벗어난 만큼이 스큐다.
    sx, sy = res["scale"]
    print("스케일    : X %.5f mm/px   Y %.5f mm/px" % (sx, sy))
    print("회전      : %.2f deg      스큐: %.2f deg" % (res["rotation_deg"], res["skew"]))
    print("원점      : 픽셀(0,0) -> (%.2f, %.2f) mm" % (A[0][2], A[1][2]))

    # ---- 저장 ------------------------------------------------------------
    print("저장      : %s" % save_matrix(res, bgr, args.image or "camera"))

    outdir = paths.resolve_out(params.get("out_dir", "out"))
    os.makedirs(outdir, exist_ok=True)
    if not args.image:
        # 촬영 원본도 남긴다 (--image 로 같은 프레임을 다시 돌려볼 수 있게).
        rp = os.path.join(outdir, "calib_raw.png")
        imgio.imwrite_u(rp, bgr)
        print("원본저장  : %s" % rp)

    # ---- 확인용 이미지 (이미지 위 글자는 전부 영문) ----------------------
    vis = bgr.copy()
    for i, m in found.items():
        col = (255, 128, 0) if i in MARKER_MM else (128, 128, 128)
        cv2.polylines(vis, [np.int32(m["corners"]).reshape(-1, 1, 2)], True, col, 2)
        u, v = m["center_px"]
        cv2.drawMarker(vis, (int(round(u)), int(round(v))), (0, 0, 255),
                       cv2.MARKER_CROSS, 16, 2)
        txt = "id%d" % i
        if i in MARKER_MM:
            txt += " (%g,%g)mm" % MARKER_MM[i]
        cv2.putText(vis, txt, (int(u) + 8, int(v) - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(vis, txt, (int(u) + 8, int(v) - 8), cv2.FONT_HERSHEY_SIMPLEX,
                    0.5, col, 1, cv2.LINE_AA)
    cv2.putText(vis, "calib rms %.3f mm  max %.3f mm" % (rms, max(errs)),
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 0), 4, cv2.LINE_AA)
    cv2.putText(vis, "calib rms %.3f mm  max %.3f mm" % (rms, max(errs)),
                (12, 28), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 1, cv2.LINE_AA)
    vp = os.path.join(outdir, "calib_fit.jpg")
    imgio.imwrite_u(vp, vis, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    print("확인이미지: %s" % vp)
    return 0


# --------------------------------------------------------------------------
# px2mm / mm2px
# --------------------------------------------------------------------------
def cmd_px2mm(args):
    d = load_matrix()
    if d is None:
        return 2
    m = model_of(d)
    x, y = px_to_mm(d, args.u, args.v)
    print("픽셀 (%.1f, %.1f)  ->  기계 X %.2f mm  Y %.2f mm   [%s]"
          % (args.u, args.v, x, y, m))
    other = "affine" if m == "homography" else "homography"
    if model_of(d, other) == other:
        ox, oy = px_to_mm(d, args.u, args.v, other)
        print("            참고: %-11s X %.2f mm  Y %.2f mm  (차이 %.2f mm)"
              % (other, ox, oy, float(np.hypot(ox - x, oy - y))))
    warn_range(x, y)
    return 0


def cmd_mm2px(args):
    d = load_matrix()
    if d is None:
        return 2
    warn_range(args.x, args.y)
    u, v = mm_to_px(d, args.x, args.y)
    print("기계 (%.2f, %.2f) mm  ->  픽셀 u %.2f  v %.2f   [%s]"
          % (args.x, args.y, u, v, model_of(d)))
    w, h = d.get("image_size", [0, 0])
    if w and h and not (0 <= u < w and 0 <= v < h):
        print("경고      : 보정 당시 화면(%dx%d) 밖의 픽셀입니다." % (w, h))
    return 0


# --------------------------------------------------------------------------
# samples
# --------------------------------------------------------------------------
def sensing_rect(marker_pixels, image_shape):
    """기준 마커 중심들의 바운딩 직사각형 (u0, v0, u1, v1). 3개 미만이면 None.

    detect.detect() 는 '웨이퍼가 화면 대부분' 인 근접 촬영을 전제로 만들어졌다.
    고정 카메라는 작업영역 전체(약 52x29cm)를 보므로 웨이퍼가 화면의 5% 뿐이고,
    레일·모터·캐리지·인쇄 글자가 원판 후보를 만들어 우하단 커플링 근처를 가짜
    원판으로 잡았다 (실측: 전체 프레임 -> 샘플 1개 / 14초, 마커 사각형으로
    자르면 -> 장축 243px 의 진짜 웨이퍼 / 샘플 15개 / 0.2초).

    마커는 베이스에 고정돼 작업영역을 둘러싸므로 그 바운딩이 곧 감지 영역이다.
    자르기는 여기(스테이지 연동 경로)에서만 한다 - 옛 사진들은 마커가 웨이퍼
    옆에 있어 같은 규칙으로 자르면 웨이퍼가 잘린다.
    """
    pts = [(float(u), float(v)) for u, v in marker_pixels]
    if len(pts) < 3:
        return None
    if len(pts) == 3:
        # 웨이퍼가 마커 하나를 덮으면 사각형이 그쪽으로 쪼그라들어 칩이 감지영역
        # 밖으로 밀린다. 직사각형이라는 것을 알고 있으므로 네 번째 꼭짓점을
        # 만들어 준다: 가장 먼 두 점이 대각선(A,C), 남은 것이 B, D = A + C - B.
        import itertools
        (ia, ic), _dmax = max(
            (((i, j), (pts[i][0] - pts[j][0]) ** 2 + (pts[i][1] - pts[j][1]) ** 2)
             for i, j in itertools.combinations(range(3), 2)), key=lambda t: t[1])
        ib = ({0, 1, 2} - {ia, ic}).pop()
        pts.append((pts[ia][0] + pts[ic][0] - pts[ib][0],
                    pts[ia][1] + pts[ic][1] - pts[ib][1]))
    h, w = image_shape[:2]
    us = [p[0] for p in pts]
    vs = [p[1] for p in pts]
    u0 = max(0, int(math.floor(min(us))))
    v0 = max(0, int(math.floor(min(vs))))
    u1 = min(w, int(math.ceil(max(us))))
    v1 = min(h, int(math.ceil(max(vs))))
    if u1 - u0 < 8 or v1 - v0 < 8:
        return None
    return (u0, v0, u1, v1)


def sense(bgr, params, allow_fallback=True, save=True):
    """한 프레임에서 '보정 -> 감지영역 -> 검출 -> 전체프레임 좌표 -> mm' 까지.

    돌려주는 dict: cal(변환), det(DetectResult), rows[(no,u,v,X,Y)], rect, refit,
    vis_crop(감지영역에 그린 주석 이미지). 쓸 변환이 없으면 None.
    스테이지 순회(run_sequence)와 samples 명령이 같은 경로를 쓰도록 분리했다.
    """
    # 원칙: 좌표는 '이 사진' 의 마커로 구한 변환으로 낸다. 카메라가 촬영 사이에
    # 1~2도 돌아가면 저장된 보정은 조용히 틀린 좌표를 준다 (화면 중앙에서
    # 130mm 떨어진 점이 1도에 2.3mm 움직인다).
    d = fit_from_image(bgr, params)
    refit = d is not None
    if refit:
        note = ""
        if len(d["used_ids"]) < 4:
            missing = [i for i in sorted(MARKER_MM) if i not in d["used_ids"]]
            note = ("  <- 마커 id %s 가려짐 -> %d점 호모그래피, 오차가 커질 수 "
                    "있음(약 1mm)" % (", ".join(str(i) for i in missing),
                                     len(d.get("corners_px", []))))
        print("보정      : 이 사진의 마커 %d개(모서리 %d점)로 재계산 / 잔차 RMS "
              "%.2f mm 최대 %.2f mm / 회전 %.1f deg [%s]%s"
              % (len(d["used_ids"]), len(d.get("corners_px", [])),
                 d.get("corner_rms_mm", d["rms_mm"]),
                 d.get("corner_max_mm", d["max_mm"]),
                 d["rotation_deg"], d["transform"], note))
        if save:
            save_matrix(d, bgr, "camera")
    elif not allow_fallback:
        return None
    else:
        print("!" * 70)
        print("!! 마커 미검출 -> 저장된 보정 사용. 카메라가 움직였으면 좌표가 틀릴 수 있음")
        print("!" * 70)
        d = load_matrix()
        if d is None:
            return None

    # 감지 영역 = 마커 사각형. 전체 프레임을 그대로 넣으면 웨이퍼를 못 찾는다.
    rect = sensing_rect([d["pixels"][k] for k in d["pixels"]], bgr.shape) if refit else None
    if rect is None:
        print("경고      : 마커 부족 -> 전체 프레임 검출, 웨이퍼 오검출 가능")
        rect = (0, 0, bgr.shape[1], bgr.shape[0])
    u0, v0, u1, v1 = rect
    crop = bgr[v0:v1, u0:u1]
    print("감지영역  : (%d,%d)-(%d,%d)  %dx%d px" % (u0, v0, u1, v1, u1 - u0, v1 - v0))

    det = detect.detect(crop, params)         # 검출 로직은 그대로 호출만 한다
    w = det.wafer
    print("웨이퍼    : surface=%s found=%s mm/px=%.5f" % (w.surface, w.found, w.mm_per_px))
    print("샘플      : %d 개 (검출 %.0f ms)" % (len(det.samples), det.detect_ms))
    for x in det.warnings:
        print("경고      : %s" % x)
    for x in det.info:
        print("안내      : %s" % x)

    # 그림은 crop 좌표계 그대로 그린 뒤 원래 자리에 되붙인다. 정보 패널은
    # crop 이 아니라 전체 프레임 좌상단(감지영역 밖)에 그린다 - crop 안에 그리면
    # 패널이 웨이퍼를 덮어 정작 볼 것을 가린다.
    vis_crop = detect.annotate(crop, det, ())
    info = samples_info(det, d, refit, rect)

    # 좌표는 전체 프레임 기준으로 되돌린다 (mm 변환은 전체 프레임 호모그래피).
    for x in det.samples:
        x["x_px"] = round(float(x["x_px"]) + u0, 1)
        x["y_px"] = round(float(x["y_px"]) + v0, 1)
        if x.get("vertices_px"):
            x["vertices_px"] = [[int(p) + u0, int(q) + v0] for p, q in x["vertices_px"]]

    rows = []
    for x in det.samples:
        u, v = float(x["x_px"]), float(x["y_px"])
        mx, my = px_to_mm(d, u, v)            # 기본 변환만 쓴다
        rows.append((x["no"], u, v, mx, my))
    return {"cal": d, "det": det, "rows": rows, "rect": rect,
            "refit": refit, "vis_crop": vis_crop, "info": info}


def print_rows(res):
    """samples 표 + 아두이노에 붙여넣을 블록 (출력 형식은 바꾸지 않는다)."""
    print("")
    print("no   pixel u      v        machine X mm   Y mm      range   [%s]"
          % model_of(res["cal"]))
    for no, u, v, mx, my in res["rows"]:
        print("%-4d %8.1f %8.1f   %10.2f %8.2f      %s"
              % (no, u, v, mx, my, "in" if in_range(mx, my) else "OUT"))
    print("")
    print("--- gcode / serial ---")
    for no, _u, _v, mx, my in res["rows"]:
        print("; sample #%d" % no)
        print("mx %.1f" % mx)
        print("my %.1f" % my)


def cmd_samples(args):
    params = load_params()
    bgr = get_image(args.image, params)
    if bgr is None:
        return 2
    res = sense(bgr, params)
    if res is None:
        return 2
    if res["rows"]:
        print_rows(res)
    save_samples_image(bgr, res, params)
    return 0


def samples_info(det, cal, refit, rect):
    """확인용 이미지 좌상단 패널. 이미지 위 글자는 전부 영문
    (cv2.putText 는 한글을 네모로 그린다)."""
    tri = sum(1 for x in det.samples if x.get("shape") == "triangle")
    w = det.wafer
    info = ["samples: %d  (triangles: %d)" % (len(det.samples), tri),
            "surface: %s   mm/px: %.5f" % (w.surface, w.mm_per_px)]
    if refit:
        info.append("calib: %d markers / %d corners  rms %.2f max %.2f mm  "
                    "rot %.1f deg  [%s]"
                    % (len(cal["used_ids"]), len(cal.get("corners_px", [])),
                       float(cal.get("corner_rms_mm", cal.get("rms_mm", 0.0))),
                       float(cal.get("corner_max_mm", cal.get("max_mm", 0.0))),
                       float(cal.get("rotation_deg", 0.0)), model_of(cal)))
    else:
        # 이 프레임에서 마커를 못 찾아 저장값을 쓴 경우. 그림에도 남겨야 나중에
        # 사진만 보고 "좌표를 믿어도 되는지" 판단할 수 있다.
        info.append("calib: SAVED matrix - no markers here, coords may be off")
    info.append("sensing: marker rect %dx%d px" % (rect[2] - rect[0], rect[3] - rect[1]))
    return info


def save_samples_image(bgr, res, params, outdir=None,
                       ann_name="samples_annotated.jpg", raw_name="samples_raw.png"):
    """표만 봐서는 어느 칩이 몇 번인지 알 수 없다. 사진과 대조할 그림을 남긴다.

    번호·외곽선은 detect.annotate 가 crop 에 그린 것을 그대로 원래 자리에
    되붙이고(검출과 번호 매기기 로직은 건드리지 않는다), 그 위에 감지 영역
    사각형·마커 id·샘플별 기계좌표를 덧그린다.
    """
    det, cal, rect = res["det"], res["cal"], res["rect"]
    u0, v0, u1, v1 = rect
    img = bgr.copy()
    img[v0:v1, u0:u1] = res["vis_crop"]
    cv2.rectangle(img, (u0, v0), (u1 - 1, v1 - 1), (0, 255, 255), 2)   # 감지 영역
    for i, p in (cal.get("pixels") or {}).items():
        cv2.drawMarker(img, (int(round(p[0])), int(round(p[1]))), (255, 128, 0),
                       cv2.MARKER_CROSS, 18, 2)
        cv2.putText(img, "id%s" % i, (int(p[0]) + 8, int(p[1]) - 8),
                    FONT, 0.5, (0, 0, 0), 3, cv2.LINE_AA)
        cv2.putText(img, "id%s" % i, (int(p[0]) + 8, int(p[1]) - 8),
                    FONT, 0.5, (255, 128, 0), 1, cv2.LINE_AA)

    detect.draw_info_panel(img, res.get("info") or [])   # 전체 프레임 좌상단

    fs = max(0.4, min(img.shape[1], img.shape[0]) / 1600.0)
    th = max(1, int(round(fs * 2)))
    mmxy = {no: (mx, my) for no, _u, _v, mx, my in res["rows"]}
    for x in det.samples:
        mx, my = mmxy.get(x["no"], (0.0, 0.0))
        t = "X%.1f Y%.1f" % (mx, my)
        org = (int(round(x["x_px"])) + 8, int(round(x["y_px"])) + 16)
        cv2.putText(img, t, org, FONT, fs, (0, 0, 0), th + 2, cv2.LINE_AA)
        cv2.putText(img, t, org, FONT, fs, (255, 255, 255), th, cv2.LINE_AA)

    outdir = outdir or paths.resolve_out(params.get("out_dir", "out"))
    os.makedirs(outdir, exist_ok=True)
    ap = os.path.join(outdir, ann_name)
    rp = os.path.join(outdir, raw_name)
    imgio.imwrite_u(ap, img, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    imgio.imwrite_u(rp, bgr)
    print("")
    print("확인이미지: %s" % ap)
    print("원본저장  : %s" % rp)


# --------------------------------------------------------------------------
# check - 카메라가 움직였는지
# --------------------------------------------------------------------------
def cmd_check(args):
    old = load_matrix()
    if old is None:
        return 2
    params = load_params()
    bgr = get_image(args.image, params)
    if bgr is None:
        return 2
    new = fit_from_image(bgr, params, verbose=True)
    if new is None:
        print("기준 마커가 3개 이상 잡혀야 비교할 수 있습니다.")
        return 1

    print("")
    print("저장된 보정: %s (%s, 마커 %s)"
          % (old.get("created", "?"), old.get("transform", "?"), old.get("used_ids")))
    print("이번 사진  : 마커 %s [%s]" % (new["used_ids"], new["transform"]))
    print("마커 잔차  : 저장 최대 %.3f mm  ->  이번 최대 %.3f mm  (모서리 기준)"
          % (float(old.get("corner_max_mm", old.get("max_mm", 0.0))),
             new["corner_max_mm"] or new["max_mm"]))
    print("회전       : 저장 %.2f deg  ->  이번 %.2f deg   (차이 %.2f deg)"
          % (float(old.get("rotation_deg", 0.0)), new["rotation_deg"],
             new["rotation_deg"] - float(old.get("rotation_deg", 0.0))))
    mx, avg = grid_diff_mm(new, old)
    print("좌표 차이  : 가동범위 %g~%g mm 5x5 격자에서 최대 %.2f mm / 평균 %.2f mm"
          % (AXIS_MIN, AXIS_MAX, mx, avg))
    if mx > 1.0:
        print("             -> 카메라가 움직였습니다. samples 는 매번 재보정하므로")
        print("                영향이 없지만, 저장값을 쓰는 px2mm/mm2px 는 --save 후 쓰세요.")
    if args.save:
        print("저장       : %s" % save_matrix(new, bgr, args.image or "camera"))
    else:
        print("(저장하지 않음. 갱신하려면 --save)")
    return 0


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="카메라 픽셀 <-> 갠트리 기계좌표 변환")
    sub = ap.add_subparsers(dest="cmd")

    f = sub.add_parser("fit", help="마커로 변환행렬을 구해 calib_matrix.json 저장")
    f.add_argument("--image", help="저장된 사진으로 실행 (없으면 카메라 촬영)")

    p = sub.add_parser("px2mm", help="픽셀 -> 기계좌표")
    p.add_argument("u", type=float)
    p.add_argument("v", type=float)

    m = sub.add_parser("mm2px", help="기계좌표 -> 픽셀")
    m.add_argument("x", type=float)
    m.add_argument("y", type=float)

    s = sub.add_parser("samples", help="검출된 샘플을 기계좌표로 변환")
    s.add_argument("--image", help="저장된 사진으로 실행 (없으면 카메라 촬영)")

    k = sub.add_parser("check", help="저장된 보정과 지금 사진을 비교 (카메라 이동량)")
    k.add_argument("--image", help="저장된 사진으로 실행 (없으면 카메라 촬영)")
    k.add_argument("--save", action="store_true", help="비교 후 calib_matrix.json 갱신")

    a = ap.parse_args(argv)
    if a.cmd == "fit":
        return cmd_fit(a)
    if a.cmd == "px2mm":
        return cmd_px2mm(a)
    if a.cmd == "mm2px":
        return cmd_mm2px(a)
    if a.cmd == "samples":
        return cmd_samples(a)
    if a.cmd == "check":
        return cmd_check(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
