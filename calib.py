"""calib.py - 카메라 픽셀 <-> 갠트리 기계좌표(mm) 변환 도구.

CLI 전용이다. cv2.imshow 도 Tkinter 도 쓰지 않는다 (헤드리스/원격에서 돌아가야
하고, 결과는 전부 콘솔 텍스트와 파일로 남긴다).

사용법
  python calib.py fit     [--image photo.png]   # 마커로 변환행렬을 구해 저장
  python calib.py px2mm   700 500               # 픽셀 -> 기계좌표
  python calib.py mm2px   100 50                # 기계좌표 -> 픽셀
  python calib.py samples [--image photo.png]   # 검출된 샘플을 기계좌표로

촬영은 camera.py 의 방식(인덱스 1, 단발, 연속 프레임 중 가장 선명한 장)을 그대로
쓰고, 마커 검출은 detect.py 의 find_markers 와 같은 딕셔너리(DICT_4X4_50)와
기본 DetectorParameters 를 쓴다. 샘플 검출은 detect.detect() 를 그대로 호출한다
(검출 로직에는 손대지 않는다).
"""

import argparse
import json
import os
import sys
import time

import cv2
import numpy as np

import camera
import detect
import paths

HERE = paths.PROJECT_ROOT
SETTINGS = paths.SETTINGS_PATH      # 경로는 전부 paths.py 를 거친다
MATRIX = paths.CALIB_PATH           # data 폴더에 둔다 (git 밖)

# 베이스에 붙은 마커의 기계좌표(mm). 마커 배치가 바뀌면 여기만 고치면 된다.
MARKER_MM = {3: (15, 15), 2: (15, 156), 1: (201, 15), 0: (201, 156)}   # id: (X, Y)

# 갠트리 가동범위. 이 밖의 좌표는 경고만 하고 값은 그대로 보여준다.
AXIS_MIN, AXIS_MAX = 0.0, 247.0


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
        bgr = cv2.imread(path, cv2.IMREAD_COLOR)
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
    corners, ids, _rej = aruco.ArucoDetector(
        dic, aruco.DetectorParameters()).detectMarkers(bgr)
    found = {}
    if ids is None:
        return found
    for c, i in zip(corners, ids.reshape(-1)):
        q = np.asarray(c).reshape(4, 2).astype(np.float64)
        found[int(i)] = {"center_px": (float(q[:, 0].mean()), float(q[:, 1].mean())),
                         "corners": q}
    return found


def load_matrix():
    if not os.path.exists(MATRIX):
        print("calib_matrix.json 이 없습니다. 먼저 'python calib.py fit' 을 실행하세요.")
        return None
    with open(MATRIX, "r", encoding="utf-8") as f:
        d = json.load(f)
    d["affine"] = np.asarray(d["affine"], np.float64)
    if d.get("homography"):
        d["homography"] = np.asarray(d["homography"], np.float64)
    return d


def px_to_mm(A, u, v):
    p = A @ np.array([float(u), float(v), 1.0])
    return float(p[0]), float(p[1])


def mm_to_px(A, x, y):
    """어파인의 역변환. 2x3 을 3x3 으로 채워 역행렬을 쓴다."""
    M = np.vstack([A, [0.0, 0.0, 1.0]])
    q = np.linalg.inv(M) @ np.array([float(x), float(y), 1.0])
    return float(q[0]), float(q[1])


def in_range(x, y):
    return AXIS_MIN <= x <= AXIS_MAX and AXIS_MIN <= y <= AXIS_MAX


def warn_range(x, y):
    if not in_range(x, y):
        print("경고      : 가동범위(%.0f~%.0f mm) 밖입니다." % (AXIS_MIN, AXIS_MAX))


# --------------------------------------------------------------------------
# fit
# --------------------------------------------------------------------------
def cmd_fit(args):
    params = load_params()
    bgr = get_image(args.image, params)
    if bgr is None:
        return 2

    found = detect_markers(bgr, params)
    used = sorted(i for i in found if i in MARKER_MM)
    missing = sorted(i for i in MARKER_MM if i not in found)
    extra = sorted(i for i in found if i not in MARKER_MM)
    print("마커      : 검출 %d개 중 기준 마커 %d개 사용 %s"
          % (len(found), len(used), used))
    if missing:
        print("            못 찾은 기준 마커 id: %s" % missing)
    if extra:
        print("            기준표에 없는 마커 id(무시): %s" % extra)
    if len(used) < 3:
        print("기준 마커가 3개 이상 잡혀야 합니다. 조명/초점/가림을 확인하세요.")
        return 1

    src = np.array([found[i]["center_px"] for i in used], np.float64)
    dst = np.array([MARKER_MM[i] for i in used], np.float64)

    # 점이 3~4개뿐이라 RANSAC 은 의미가 없다. 전체 점을 최소제곱으로 맞춘다.
    A, _inl = cv2.estimateAffine2D(src.reshape(-1, 1, 2), dst.reshape(-1, 1, 2),
                                   method=cv2.LMEDS)
    if A is None:
        print("어파인 계산에 실패했습니다.")
        return 1
    A = np.asarray(A, np.float64)

    Hm = None
    if len(used) >= 4:
        Hm, _m = cv2.findHomography(src, dst, 0)
        if Hm is not None:
            Hm = np.asarray(Hm, np.float64)

    # ---- 잔차표 ----------------------------------------------------------
    print("")
    print("id   pixel(u,v)          real(X,Y)mm      pred(X,Y)mm      err mm")
    errs = []
    resid = {}
    for i in used:
        u, v = found[i]["center_px"]
        rx, ry = MARKER_MM[i]
        px, py = px_to_mm(A, u, v)
        e = float(np.hypot(px - rx, py - ry))
        errs.append(e)
        resid[str(i)] = round(e, 4)
        print("%-4d (%7.1f,%7.1f)   (%6.1f,%6.1f)   (%7.2f,%7.2f)   %6.3f"
              % (i, u, v, rx, ry, px, py, e))
    rms = float(np.sqrt(np.mean(np.square(errs))))
    print("RMS       : %.3f mm    최대: %.3f mm" % (rms, max(errs)))

    # ---- 행렬 해석 -------------------------------------------------------
    # A = [[a,b,tx],[c,d,ty]]. 열 벡터의 길이가 축 스케일, 첫 열의 각도가 회전,
    # 두 열 사이 각이 90도에서 벗어난 만큼이 스큐다.
    a, b, tx = A[0]
    c, d, ty = A[1]
    sx = float(np.hypot(a, c))
    sy = float(np.hypot(b, d))
    rot = float(np.degrees(np.arctan2(c, a)))
    cosang = float((a * b + c * d) / max(sx * sy, 1e-12))
    skew = 90.0 - float(np.degrees(np.arccos(max(-1.0, min(1.0, cosang)))))
    print("스케일    : X %.5f mm/px   Y %.5f mm/px" % (sx, sy))
    print("회전      : %.2f deg      스큐: %.2f deg" % (rot, skew))
    print("원점      : 픽셀(0,0) -> (%.2f, %.2f) mm" % (tx, ty))

    # ---- 저장 ------------------------------------------------------------
    out = {
        "created": time.strftime("%Y-%m-%d %H:%M:%S"),
        "image": args.image or "camera",
        "image_size": [int(bgr.shape[1]), int(bgr.shape[0])],
        "marker_mm": {str(k): list(v) for k, v in MARKER_MM.items()},
        "used_ids": used,
        "pixels": {str(i): [round(found[i]["center_px"][0], 2),
                            round(found[i]["center_px"][1], 2)] for i in used},
        "affine": [[float(x) for x in row] for row in A],
        "homography": ([[float(x) for x in row] for row in Hm]
                       if Hm is not None else None),
        "residual_mm": resid,
        "rms_mm": round(rms, 4),
        "max_mm": round(float(max(errs)), 4),
        "scale_mm_per_px": [round(sx, 6), round(sy, 6)],
        "rotation_deg": round(rot, 3),
        "skew_deg": round(skew, 3),
    }
    with open(MATRIX, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("저장      : %s" % MATRIX)

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
    outdir = paths.resolve_out(params.get("out_dir", "out"))
    os.makedirs(outdir, exist_ok=True)
    vp = os.path.join(outdir, "calib_fit.jpg")
    cv2.imwrite(vp, vis, [int(cv2.IMWRITE_JPEG_QUALITY), 90])
    print("확인이미지: %s" % vp)
    return 0


# --------------------------------------------------------------------------
# px2mm / mm2px
# --------------------------------------------------------------------------
def cmd_px2mm(args):
    d = load_matrix()
    if d is None:
        return 2
    x, y = px_to_mm(d["affine"], args.u, args.v)
    print("픽셀 (%.1f, %.1f)  ->  기계 X %.2f mm  Y %.2f mm" % (args.u, args.v, x, y))
    warn_range(x, y)
    return 0


def cmd_mm2px(args):
    d = load_matrix()
    if d is None:
        return 2
    warn_range(args.x, args.y)
    u, v = mm_to_px(d["affine"], args.x, args.y)
    print("기계 (%.2f, %.2f) mm  ->  픽셀 u %.2f  v %.2f" % (args.x, args.y, u, v))
    w, h = d.get("image_size", [0, 0])
    if w and h and not (0 <= u < w and 0 <= v < h):
        print("경고      : 보정 당시 화면(%dx%d) 밖의 픽셀입니다." % (w, h))
    return 0


# --------------------------------------------------------------------------
# samples
# --------------------------------------------------------------------------
def cmd_samples(args):
    d = load_matrix()
    if d is None:
        return 2
    params = load_params()
    bgr = get_image(args.image, params)
    if bgr is None:
        return 2

    det = detect.detect(bgr, params)          # 검출 로직은 그대로 호출만 한다
    w = det.wafer
    print("웨이퍼    : surface=%s found=%s mm/px=%.5f" % (w.surface, w.found, w.mm_per_px))
    print("샘플      : %d 개 (검출 %.0f ms)" % (len(det.samples), det.detect_ms))
    for x in det.warnings:
        print("경고      : %s" % x)
    if not det.samples:
        return 0

    A = d["affine"]
    print("")
    print("no   pixel u      v        machine X mm   Y mm      range")
    rows = []
    for s in det.samples:
        u, v = float(s["x_px"]), float(s["y_px"])
        mx, my = px_to_mm(A, u, v)
        ok = in_range(mx, my)
        rows.append((s["no"], mx, my))
        print("%-4d %8.1f %8.1f   %10.2f %8.2f      %s"
              % (s["no"], u, v, mx, my, "in" if ok else "OUT"))

    print("")
    print("--- gcode / serial ---")
    for no, mx, my in rows:
        print("; sample #%d" % no)
        print("mx %.1f" % mx)
        print("my %.1f" % my)
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

    a = ap.parse_args(argv)
    if a.cmd == "fit":
        return cmd_fit(a)
    if a.cmd == "px2mm":
        return cmd_px2mm(a)
    if a.cmd == "mm2px":
        return cmd_mm2px(a)
    if a.cmd == "samples":
        return cmd_samples(a)
    ap.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
