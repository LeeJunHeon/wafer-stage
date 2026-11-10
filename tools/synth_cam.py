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
import threading

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


def render_plate(w, h, head_xy=None, params=None, seed=3, shift_mm=(0.0, 0.0), chips=True, texture=False,
                 samples_mm=None, extra_mm=(), blobs_mm=()):
    """BGR 판 장면. 회색 판 + 알려진 위치의 칩 9개 + 사각형 밖 물체 2개 (+ 헤드 마커).

    shift_mm: 칩 위치를 이만큼 옮긴다(샘플 위치를 바꿔 다시 찍는 검증용).
    chips=False 면 빈 판(기준 사진용). texture=True 면 판에 고정된 긁힘 무늬(기준 정렬 · 차이 검출 검증용)를 더한다 - 촬영마다 다른 잡음과 달리 두 사진에 똑같다.
    samples_mm = 칩 위치 목록 덮어쓰기, extra_mm = 더하는 칩, blobs_mm = 큰 얼룩(칩 크기 밖 · 기준 점검 검증용).
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
    if texture:
        trng = np.random.default_rng(11)
        for _k in range(160):
            a = trng.uniform(25.0, 215.0, 2)
            ang = trng.uniform(0, 2 * math.pi)
            ln = trng.uniform(6.0, 30.0)
            b = a + ln * np.array([math.cos(ang), math.sin(ang)])
            p0, p1 = project(H, a[0], a[1]), project(H, b[0], b[1])
            v = float(trng.uniform(-28.0, 28.0))
            cv2.line(img, (int(p0[0]), int(p0[1])), (int(p1[0]), int(p1[1])), (150.0 + v, 150.0 + v, 150.0 + v), 1, cv2.LINE_AA)
    for (bx, by) in blobs_mm:
        pb = project(H, bx, by)
        cv2.circle(img, (int(pb[0]), int(pb[1])), int(60 * w / 1280.0), (95, 100, 105), -1, cv2.LINE_AA)
    pos = list(PLATE_SAMPLES_MM if samples_mm is None else samples_mm) + list(extra_mm)
    for i, (x, y) in enumerate(pos if chips else []):
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
    truth = {"samples": [(x + shift_mm[0], y + shift_mm[1]) for x, y in pos],
             "outside": list(PLATE_OUTSIDE_MM), "H": H, "quad_mm": list(PLATE_QUAD_MM)}
    return out, truth


# --------------------------------------------------------------------------
# 합성 카메라 객체 (grab / retrieve) - 서버의 dry 카메라
# --------------------------------------------------------------------------
class SynthCamera:
    """판 · 칩 · 스테이지 위치에 따른 헤드 마커를 그리는 카메라. core.camera.Camera 와 같은 grab / retrieve.

    실제 USB 카메라 드라이버처럼 움직인다(실시간 · 촬영 시험이 실제와 다른 조건에서 통과하지 않게):
    - 프레임은 카메라 속도(fps, WAFER2_SYNTH_FPS 기본 30)로 생긴다. 드라이버 큐는 depth 장 - 큐가 차 있으면 새 프레임을 버린다
      (그래서 드문드문 grab 하면 옛 장면이 depth 장만큼 남는다 = 실시간이 1~2 s 늦던 증상). grab() 은 가장 오래된 프레임을 꺼내고,
      큐가 비어 있으면 다음 프레임이 생길 때까지 기다린다(쉬지 않고 grab 하면 큐가 비어 늘 최신 장면).
    - 프레임은 생길 때의 장면(헤드 위치 · world)만 기억하고, 그림은 retrieve() 때 그린다(grab 은 가볍다 - 실제 카메라처럼).
    - 빠른 모드(WAFER2_SYNTH_FAST=1, 메모리 · CPU 측정용): 판 그림을 한 번만 그리고 프레임마다 복사본에 헤드 표시만 그린다
      (4080×3060 에서 프레임마다 다시 그리면 4 s 씩 걸려 카메라가 아니라 합성기를 재게 된다).
    - 카메라 평행 이동 · 기울기: world["shift_px"] (dx, dy) · world["rot_deg"] · world["tilt"] (원근 계수).
    - world["empty"] 가 참이면 칩 없는 빈 판(빈 판 사진용), world["chips"] 는 스테이지 mm 위치 목록.
    """

    def __init__(self, true_xy_fn, w=1280, h=960, depth=4, params=None, fps=None, fast=None):
        from collections import deque
        import time as _t
        self._t = _t
        self.true_xy = true_xy_fn
        self.w, self.h = int(w), int(h)
        self.depth = max(1, int(depth))
        self.fps = float(fps if fps is not None else (os.environ.get("WAFER2_SYNTH_FPS") or 30.0))
        self.period = 1.0 / max(1.0, self.fps)
        self.fast = bool(int(os.environ.get("WAFER2_SYNTH_FAST", "0") or 0)) if fast is None else bool(fast)
        self.params = params or {"photohome": {"marker_id": 10, "marker_mm": 40.0, "dict": "4X4_50"}}
        self.world = {"empty": False, "chips": list(PLATE_SAMPLES_MM), "shift_px": (0.0, 0.0), "rot_deg": 0.0,
                      "tilt": (0.0, 0.0), "head": True, "seed": 5, "flash": 0}
        self.q = deque()                                  # 드라이버 큐: (생긴 단조 시각, 장면 키, 번호)
        self.cur = None                                   # 마지막으로 grab 한 프레임의 (시각, 장면 키, 번호)
        self.cache = {}
        self.opened = True
        self.n_made = 0                                   # 카메라가 만든 프레임 수(버린 것 포함)
        self.n_drop = 0                                   # 큐가 차서 버린 프레임 수
        self._cv = threading.Condition()
        self._th = None                                   # 카메라 쪽 스레드(첫 grab 때 시작, release 에서 끝)

    def set_world(self, **kw):
        self.world.update(kw)

    def _key(self):
        wd = self.world
        xy = self.true_xy() if wd["head"] else None
        return (self.w, self.h, wd["empty"], tuple(map(tuple, wd["chips"])), tuple(wd["shift_px"]), wd["rot_deg"], tuple(wd["tilt"]),
                None if xy is None else (round(xy[0], 2), round(xy[1], 2)), wd["seed"], int(wd.get("flash") or 0))

    def _render_key(self, key):
        key = key[:9]                                     # flash 는 그림 캐시와 무관(retrieve 가 복사본에 그린다)
        if key in self.cache:
            return self.cache[key]
        w, h, empty, chips, shift_px, rot_deg, tilt, xy, seed = key
        img, _t = render_plate(self.w, self.h, head_xy=xy, params=self.params, seed=seed, chips=not empty, texture=True,
                               samples_mm=[tuple(c) for c in chips])
        dx, dy = shift_px
        if dx or dy or rot_deg or any(tilt):
            c = (self.w / 2.0, self.h / 2.0)
            R = cv2.getRotationMatrix2D(c, rot_deg, 1.0)
            M = np.vstack([R, [0, 0, 1.0]])
            M[0, 2] += dx
            M[1, 2] += dy
            P = np.eye(3)
            P[2, 0], P[2, 1] = tilt
            M = M @ P
            img = cv2.warpPerspective(img, M, (self.w, self.h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        if len(self.cache) > 12:
            self.cache.clear()
        self.cache[key] = img
        return img

    def _render_fast(self, key, n):
        """빠른 모드: 판(헤드 없음)은 한 번만 그리고, 프레임마다 복사본에 헤드 자리 표시 + 프레임 번호만 그린다."""
        base_key = key[:7] + (None,) + key[8:9]
        base = self._render_key(base_key)
        out = base.copy()                                  # 실제 카메라도 프레임마다 새 버퍼(디코딩 결과)를 준다
        xy = key[7]
        if xy is not None:
            p = true_homography(self.w, self.h) @ np.array([xy[0] + HEAD_OFFSET_MM[0], xy[1] + HEAD_OFFSET_MM[1], 1.0])
            u, v = int(p[0] / p[2]), int(p[1] / p[2])
            r = max(6, self.w // 64)
            cv2.rectangle(out, (u - r, v - r), (u + r, v + r), (20, 20, 20), -1)
        cv2.putText(out, str(n), (10, self.h - 10), cv2.FONT_HERSHEY_SIMPLEX, max(0.5, self.w / 1280.0), (255, 255, 255), 2)
        return out

    delay_s = 0.0

    def _producer(self):
        """카메라 쪽: fps 마다 프레임 하나(그때의 장면 키). 큐가 차 있으면 버린다(드라이버가 새 프레임을 못 받는다)."""
        nxt = self._t.monotonic()
        while self.opened:
            nxt += self.period
            d = nxt - self._t.monotonic()
            if d > 0:
                self._t.sleep(d)
            else:
                nxt = self._t.monotonic()                 # 밀렸으면 따라잡지 않는다(실제 카메라도 건너뛴다)
            try:
                key = self._key()
            except Exception:                             # noqa: BLE001 - 서버가 닫히는 중
                continue
            with self._cv:
                self.n_made += 1
                if len(self.q) < self.depth:
                    self.q.append((self._t.monotonic(), key, self.n_made))
                    self._cv.notify_all()
                else:
                    self.n_drop += 1

    def grab(self):
        if self.delay_s:
            self._t.sleep(self.delay_s)
        if not self.opened:
            return False
        if self._th is None:
            self._th = threading.Thread(target=self._producer, daemon=True, name="synth-cam")
            self._th.start()
        with self._cv:
            if not self.q:                                # 큐가 비었다 - 다음 프레임이 생길 때까지 기다린다(실제 grab 처럼 막힌다)
                self._cv.wait(timeout=1.0)
            if not self.q:
                return False
            self.cur = self.q.popleft()
        return True

    def retrieve(self):
        if self.cur is None:
            return None
        _t0, key, n = self.cur
        img = self._render_fast(key, n) if self.fast else self._render_key(key).copy()
        if key[9]:                                        # 시험(실시간 지연): 그리기 비용 없이 장면을 바로 바꾼다 - 왼쪽 위 1/4 을 칠한다
            col = (255, 255, 255) if key[9] % 2 else (0, 0, 0)
            cv2.rectangle(img, (0, 0), (self.w // 2, self.h // 2), col, -1)
        return img

    def read(self):
        self.grab()
        return self.retrieve()

    def release(self):
        self.opened = False


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
