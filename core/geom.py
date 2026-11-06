"""geom.py - 기하: 사각형 · 호모그래피 · 좌표 보정 풀이 · 퇴화 검사 · 헤드 마커 검출. 서버 상태를 모른다(순수).

좌표 규칙(CLAUDE.md "좌표계"): 픽셀은 프레임 태그가 붙은 점으로만 오간다(core/frames.py). 이 모듈은 한 프레임 안의 계산만 한다.
"""

import math

import cv2
import numpy as np

from core import labels

MIN_AREA_FRAC = 0.01               # 판 영역이 프레임의 1% 는 돼야 한다
MIN_POINTS = 4
APPLY_MIN_POINTS = 5               # 좌표 보정 적용은 5점부터(4점은 잔차가 항상 0)
SPREAD_MIN_FRAC = 0.25             # 점들의 볼록껍질이 판 영역의 이 비율 이상이면 '고르게 퍼짐'
PRIOR_W = 0.1                      # 전체 호모그래피 최소제곱에서 근사 사각형(사전 정보)의 가중치
COLLINEAR_MAX_RATIO = 0.08         # 점 집합 주성분의 작은 축 / 큰 축 이 이 이하면 한 직선(조건수 ≥ 12.5)


def fit_affine(src, dst):
    """전체 점 최소제곱 어파인 src(px) -> dst(mm), 2x3 (cv2.estimateAffine2D 는 4점에서 한 점을 이상치로 버린다)."""
    M = np.hstack([np.asarray(src, np.float64), np.ones((len(src), 1))])
    sol, _res, _rank, _sv = np.linalg.lstsq(M, np.asarray(dst, np.float64), rcond=None)
    return sol.T


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


def plate_homography(quad, size_mm):
    """사진 px → 판 평면 mm. 감지 영역 네 꼭짓점(사진 기준 TL · TR · BR · BL) = area_size_mm 사각형의 (0,0) · (W,0) · (W,H) · (0,H)."""
    q = order_quad(quad).astype(np.float32)
    w, h = float(size_mm[0]), float(size_mm[1])
    dst = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    return cv2.getPerspectiveTransform(q, dst).astype(np.float64)


def axes_matrix(p_x, p_y):
    """스테이지 +X · +Y 가 판 평면에서 가리키는 방향 → 직교 정규화한 Q(판 = Q · 스테이지). 반사(거울상)도 허용한다."""
    x = np.asarray(p_x, np.float64)
    x = x / max(np.linalg.norm(x), 1e-12)
    y = np.asarray(p_y, np.float64)
    y = y - float(y @ x) * x
    y = y / max(np.linalg.norm(y), 1e-12)
    return np.column_stack([x, y])


def axes_from_head(H_head, Hp, center_px):
    """헤드 보정(사진 px → 스테이지 mm)에서 축 방향: 스테이지 +X · +Y 단위 이동이 사진에서 움직이는 방향을 판 평면 방향으로 옮긴다."""
    c = np.asarray(center_px, np.float64)
    x0, y0 = px_to_mm(H_head, c[0], c[1])
    xu, yu = px_to_mm(H_head, c[0] + 1.0, c[1])
    xv, yv = px_to_mm(H_head, c[0], c[1] + 1.0)
    J = np.array([[xu - x0, xv - x0], [yu - y0, yv - y0]])           # px → mm
    out = []
    for e in (np.array([1.0, 0.0]), np.array([0.0, 1.0])):
        d_px = np.linalg.solve(J, e)
        p0 = np.array(px_to_mm(Hp, c[0], c[1]))
        p1 = np.array(px_to_mm(Hp, c[0] + d_px[0] * 20.0, c[1] + d_px[1] * 20.0))
        out.append(p1 - p0)
    return axes_matrix(out[0], out[1])


def axes_from_probe(px0, px_x, px_y, Hp):
    """자동 측정: 헤드 마커가 X(+dx) · Y(+dy) 이동 때 사진에서 옮겨 간 자리 → 판 평면 방향(부호는 이동 방향 부호를 호출 쪽이 곱해 넘긴다)."""
    p0 = np.array(px_to_mm(Hp, px0[0], px0[1]))
    return axes_matrix(np.array(px_to_mm(Hp, px_x[0], px_x[1])) - p0, np.array(px_to_mm(Hp, px_y[0], px_y[1])) - p0)


def _fit_h_weighted(src, dst, w):
    """가중 DLT 호모그래피(정규화) src(px) → dst(mm)."""
    def norm(P):
        c = P.mean(axis=0)
        s = math.sqrt(2.0) / max(float(np.mean(np.linalg.norm(P - c, axis=1))), 1e-12)
        return np.array([[s, 0, -s * c[0]], [0, s, -s * c[1]], [0, 0, 1.0]])
    Ts, Td = norm(src), norm(dst)
    S = (Ts @ np.column_stack([src, np.ones(len(src))]).T).T
    D = (Td @ np.column_stack([dst, np.ones(len(dst))]).T).T
    rows = []
    for (x, y, _), (u, v, _), wi in zip(S, D, w):
        rows.append(wi * np.array([-x, -y, -1, 0, 0, 0, u * x, u * y, u]))
        rows.append(wi * np.array([0, 0, 0, -x, -y, -1, v * x, v * y, v]))
    _u, _s, vt = np.linalg.svd(np.array(rows))
    Hn = vt[-1].reshape(3, 3)
    H = np.linalg.inv(Td) @ Hn @ Ts
    return H / H[2, 2]


def points_spread_ok(points, quad):
    """판 위에 고르게 퍼졌는가: 점 4개 이상이고 사진 px 볼록껍질 넓이가 감지 영역의 SPREAD_MIN_FRAC 이상."""
    if len(points) < 4 or not quad:
        return False
    pts = np.array([[p["u"], p["v"]] for p in points], np.float32)
    area = cv2.contourArea(cv2.convexHull(pts))
    qa = cv2.contourArea(order_quad(quad).astype(np.float32))
    return bool(qa > 0 and area >= SPREAD_MIN_FRAC * qa)


def solve_record(d, size_mm):
    """저장 본문 d(area · approx_axes · approx_t · points)에서 변환 H 를 다시 계산해 d 에 쓴다. 반환: 정보 dict | None.

    n = 0 근사(배율 · 원근 · 축 방향, 오프셋은 approx_t) / 1 오프셋만 맞춤 / 2+ 닮음 변환(이동 · 회전 · 배율, 원근은 근사에서) /
    4+ 이고 고르게 퍼짐 = 전체 호모그래피(근사 사각형을 약한 사전 정보로). 점이 모두 클릭 방식이고 4개 이상이면 예전과 같은 최소제곱(fit).
    """
    quad, axes = d.get("area"), d.get("approx_axes")
    pts = d.get("points") or []
    if not quad or not axes or not d.get("approx_t"):
        return None
    Hp = plate_homography(quad, size_mm)
    QT = np.asarray(axes["Q"], np.float64).T
    t0 = np.asarray(d["approx_t"], np.float64)
    n = len(pts)

    def aff(s_rot, t):
        A = np.eye(3)
        A[:2, :2], A[:2, 2] = s_rot, t
        return A

    base = aff(QT, np.zeros(2)) @ Hp                                  # 근사(오프셋 0)
    a = np.array([QT @ np.array(px_to_mm(Hp, p["u"], p["v"])) for p in pts]) if n else np.zeros((0, 2))
    b = np.array([[p["x"], p["y"]] for p in pts]) if n else np.zeros((0, 2))
    scale_rot = np.eye(2)
    if n == 0:
        model, H = "approx", aff(np.eye(2), t0) @ base
    elif n == 1:
        model, H = "offset", aff(np.eye(2), b[0] - a[0]) @ base
    else:
        mu_a, mu_b = a.mean(axis=0), b.mean(axis=0)
        A0, B0 = a - mu_a, b - mu_b
        var_a = float(np.sum(A0 ** 2) / n)
        U, D, Vt = np.linalg.svd(B0.T @ A0 / n)
        S = np.diag([1.0, np.sign(np.linalg.det(U) * np.linalg.det(Vt)) or 1.0])
        R = U @ S @ Vt
        s = float(np.trace(np.diag(D) @ S) / max(var_a, 1e-12))
        scale_rot = s * R
        t = mu_b - scale_rot @ mu_a
        model, H = "similarity", aff(scale_rot, t) @ base
    if n >= 4 and points_spread_ok(pts, quad):
        src = [[p["u"], p["v"]] for p in pts]
        dst = [[p["x"], p["y"]] for p in pts]
        w = [1.0] * n
        for q in order_quad(quad):                                    # 근사 사각형 꼭짓점 = 약한 사전 정보
            src.append([float(q[0]), float(q[1])])
            dst.append(list(px_to_mm(H, q[0], q[1])))
            w.append(PRIOR_W)
        Hh = _fit_h_weighted(np.array(src, np.float64), np.array(dst, np.float64), w)
        if np.all(np.isfinite(Hh)):
            model, H = "homography", Hh
    if not np.all(np.isfinite(H)):
        return None
    res = [math.hypot(px_to_mm(H, p["u"], p["v"])[0] - p["x"], px_to_mm(H, p["u"], p["v"])[1] - p["y"]) for p in pts]
    for p, r in zip(pts, res):
        p["res_mm"] = round(r, 4)
    rms = float(np.sqrt(np.mean(np.square(res)))) if res else 0.0
    d["H"] = np.asarray(H, np.float64)
    d["model"], d["approx"] = model, (n == 0)
    d["rms_mm"], d["max_mm"] = round(rms, 4), round(float(max(res)) if res else 0.0, 4)
    scale = float(math.sqrt(abs(np.linalg.det(scale_rot)))) if n >= 2 else 1.0
    return {"model": model, "n": n, "rms": rms, "max": float(max(res)) if res else 0.0, "res": res, "scale": scale}


def predict_mm(d, u, v):
    """저장 본문의 현재 변환으로 사진 (u, v) 의 스테이지 mm (변환이 없으면 None)."""
    if d is None or d.get("H") is None:
        return None
    return px_to_mm(np.asarray(d["H"], np.float64), u, v)


def point_bins(points, quad, size_mm, n=3):
    """판 위 점 분포(n x n 칸 · 사진 기준 위→아래, 왼쪽→오른쪽): 칸마다 점 개수. 빈 구역 강조용."""
    out = [[0] * n for _ in range(n)]
    if not quad:
        return out
    Hp = plate_homography(quad, size_mm)
    w, h = float(size_mm[0]), float(size_mm[1])
    for p in points:
        x, y = px_to_mm(Hp, p["u"], p["v"])
        c, r = min(n - 1, max(0, int(x / w * n))), min(n - 1, max(0, int(y / h * n)))
        out[r][c] += 1
    return out


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
# 퇴화 변환 거절 (점이 한쪽에 몰렸거나 한 직선 위)
# --------------------------------------------------------------------------
def point_conditioning(pts_uv):
    """(작은 축 / 큰 축, 조건수). 점 집합의 공분산 특이값 비 - 한 직선이면 0 에 가깝다."""
    P = np.asarray(pts_uv, np.float64).reshape(-1, 2)
    if len(P) < 3:
        return 0.0, float("inf")
    sv = np.linalg.svd(P - P.mean(axis=0), compute_uv=False)
    if sv[0] < 1e-9:
        return 0.0, float("inf")
    return float(sv[1] / sv[0]), float(sv[0] / max(sv[1], 1e-12))


def degenerate_reason(points, quad):
    """좌표 보정 점(u, v 키)이 변환을 풀기에 모자라면 사유 문구, 괜찮으면 "". 점이 판 영역에 고르게 퍼져야 하고 한 직선이면 안 된다."""
    if len(points) < MIN_POINTS:
        return "점 %d개 · 4개 이상 필요" % len(points)
    uv = [[p["u"], p["v"]] for p in points]
    ratio, cond = point_conditioning(uv)
    if ratio < COLLINEAR_MAX_RATIO:
        return "점이 한 직선에 가까움 · 조건수 %.0f" % min(cond, 9999)
    if quad is not None and len(quad) >= 3:
        hull = cv2.convexHull(np.asarray(uv, np.float32))
        qa = cv2.contourArea(order_quad(quad).astype(np.float32))
        if qa > 0 and cv2.contourArea(hull) < SPREAD_MIN_FRAC * qa:
            return "점이 판 영역의 %d%% 도 못 채움 · 고르게 퍼뜨림" % round(SPREAD_MIN_FRAC * 100)
    return ""


def fit(points, quad=None):
    """points [{x, y, u, v}] -> {"H", "res", "rms", "max"}. 점 4개 미만 · 퇴화면 None (사유는 degenerate_reason)."""
    if degenerate_reason(points, quad):
        return None
    src = np.array([[p["u"], p["v"]] for p in points], np.float64)
    dst = np.array([[p["x"], p["y"]] for p in points], np.float64)
    H, _m = cv2.findHomography(src, dst, 0)
    if H is None or not np.all(np.isfinite(H)):
        return None
    res = []
    for p in points:
        a, b = px_to_mm(H, p["u"], p["v"])
        res.append(math.hypot(a - p["x"], b - p["y"]))
    return {"H": np.asarray(H, np.float64), "res": res,
            "rms": float(np.sqrt(np.mean(np.square(res)))), "max": float(max(res))}
