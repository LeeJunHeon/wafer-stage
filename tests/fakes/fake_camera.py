"""fake_camera.py - 가짜 카메라: cv2.VideoCapture 모양(isOpened · set · get · read · grab · retrieve · release).

가짜 세계(tests/fakes/world.py)를 약간 비스듬한(사다리꼴) 사진으로 그린다: 바닥 · 금속 판 · 새겨진 사각형 · 샘플(올려 있을 때) ·
헤드(판 위에 있으면 샘플을 가린다) · 헤드 마커(실제 ArUco 4X4_50 id 10 무늬 - 판보다 높은 평면이라 시차가 생긴다).
헤드는 head_xy_fn() 이 주는 스테이지 X · Y(가짜 스테이지의 실제 위치)를 따라 움직인다.
장면은 가로 최대 2040 으로 그린 뒤 요청한 해상도로 늘린다(4080×3060 도 가볍게 흉내 낸다).
시험용: size_override(실제로 열리는 해상도를 요청과 다르게) · fail(프레임이 안 나옴).
"""

import time

import cv2
import numpy as np

from tests.fakes.world import World

HEAD_ID = 10
BASE_MAX_W = 2040
PLATE_MARGIN_MM = 20.0                              # 금속 판은 새겨진 사각형보다 이만큼 크다
_BG_CACHE = {}                                      # 장면 → (배경, 잡음)


def _marker(px):
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    m = cv2.aruco.generateImageMarker(d, HEAD_ID, px)
    b = max(2, px // 5)                                               # 마커 둘레의 흰 여백(검출에 필요)
    return cv2.copyMakeBorder(m, b, b, b, b, cv2.BORDER_CONSTANT, value=255)


class FakeCapture:
    def __init__(self, width=640, height=480, head_xy_fn=None, size_override=None, fourcc_override=None, world=None):
        self.default = (int(width), int(height))
        self.size_override = size_override
        self.fourcc = fourcc_override
        self.head_xy_fn = head_xy_fn or (lambda: (0.0, 0.0))
        self.world = world or World()
        self.opened = True
        self.fail = False
        self._props = {}
        self._built = None
        self._last = None
        self._mk = _marker(200)

    @property
    def w(self):
        return self._size()[0]

    @property
    def h(self):
        return self._size()[1]

    def _size(self):
        if self.size_override:
            return tuple(self.size_override)
        w = int(self._props.get(cv2.CAP_PROP_FRAME_WIDTH, self.default[0]))
        h = int(self._props.get(cv2.CAP_PROP_FRAME_HEIGHT, self.default[1]))
        return w, h

    def _ensure(self):
        key = (self._size(), self.world.version)
        if self._built == key:
            return
        self._built = key
        w, h = key[0]
        self._bw = min(BASE_MAX_W, w)
        self._bh = max(1, int(round(self._bw * h / w)))
        wd = self.world
        scene = repr((self._bw, self._bh, wd.loaded, wd.size_mm, wd.angle_deg, wd.plate_a, wd.view, wd.keystone,
                      [(s["px"], s["py"], s["w"], s["h"]) for s in wd.samples] if wd.loaded else None))
        hit = _BG_CACHE.get(scene)                                    # 같은 장면의 배경은 한 번만 그린다(그리는 데 0.3 초쯤 든다)
        if hit is None:
            bg = self._background()
            rng = np.random.default_rng(11)
            hit = (bg, np.rint(rng.normal(0.0, 1.0, bg.shape)).astype(np.int16))            # 배경 · 프레임 잡음 한 장(매번 밀어서 쓴다)
            if len(_BG_CACHE) >= 8:
                _BG_CACHE.pop(next(iter(_BG_CACHE)))
            _BG_CACHE[scene] = hit
        self._bg, self._fn = hit
        self._rng = np.random.default_rng(11)

    # ---- cv2.VideoCapture 모양 ----
    def isOpened(self):
        return self.opened

    def set(self, prop, val):
        self._props[prop] = val
        return True

    def get(self, prop):
        if prop == cv2.CAP_PROP_FOURCC:
            f = self.fourcc if self.fourcc else self._props.get(cv2.CAP_PROP_FOURCC, "MJPG")
            if isinstance(f, str):
                return float(cv2.VideoWriter_fourcc(*f)) if len(f) == 4 else 0.0
            return float(f)
        if prop == cv2.CAP_PROP_FRAME_WIDTH:
            return float(self.w)
        if prop == cv2.CAP_PROP_FRAME_HEIGHT:
            return float(self.h)
        return float(self._props.get(prop, 0))

    def grab(self):
        if not self.opened or self.fail:
            return False
        self._last = time.monotonic()
        return True

    def retrieve(self):
        if not self.opened or self.fail:
            return False, None
        return True, self._render()

    def read(self):
        return (self.grab() and self.retrieve()) or (False, None)

    def release(self):
        self.opened = False

    # ---- 장면 ----
    def _poly(self, img, stage_pts, color, height=0.0):
        p = self.world.project(stage_pts, self._bw, self._bh, height)
        cv2.fillConvexPoly(img, np.rint(p * 8).astype(np.int32), color, lineType=cv2.LINE_AA, shift=3)

    def _background(self):
        wd = self.world
        s, mg = wd.size_mm, PLATE_MARGIN_MM
        img = np.full((self._bh, self._bw, 3), (58, 60, 64), np.uint8)                       # 어두운 바닥
        plate = [wd.plate_to_stage(*p) for p in ((-mg, -mg), (s + mg, -mg), (s + mg, s + mg), (-mg, s + mg))]
        self._poly(img, plate, (196, 198, 200))                                             # 금속 판
        rng = np.random.default_rng(7)
        pmask = np.zeros(img.shape[:2], np.uint8)
        cv2.fillConvexPoly(pmask, np.rint(wd.project(plate, self._bw, self._bh)).astype(np.int32), 255)
        tex = np.zeros(img.shape[:2], np.float32)                                           # 판의 무늬: 얼룩 + 긁힘(빈 판 사진과 맞출 때 쓰이는 실제 판의 결)
        for _ in range(30):
            c = wd.project([wd.plate_to_stage(*rng.uniform(-mg, s + mg, 2))], self._bw, self._bh)[0]
            r_px = float(rng.uniform(5, 16)) * self._bw / 360.0
            cv2.circle(tex, (int(c[0]), int(c[1])), int(r_px), float(rng.uniform(-2.5, 2.5)), -1, cv2.LINE_AA)
        tex = cv2.GaussianBlur(tex, (0, 0), max(1.0, self._bw / 300.0))
        for _ in range(420):                                                                # 가는 긁힘(검출이 진짜 사진처럼 걸러 내야 한다)
            a = rng.uniform(-mg, s + mg, 2)
            b = np.clip(a + rng.uniform(-22, 22, 2), -mg, s + mg)
            pa, pb = wd.project([wd.plate_to_stage(*a), wd.plate_to_stage(*b)], self._bw, self._bh)
            cv2.line(tex, tuple(int(v) for v in np.rint(pa)), tuple(int(v) for v in np.rint(pb)), float(rng.uniform(-18, 12)),
                     1 if rng.random() < 0.7 else 2, cv2.LINE_AA)
        grain = cv2.GaussianBlur(rng.normal(0.0, 1.0, img.shape[:2]).astype(np.float32), (0, 0), sigmaX=max(4.0, self._bw / 170.0), sigmaY=0.7)
        tex += grain * (3.0 / max(1e-6, float(grain.std())))                                # 결(한 방향으로 쓸린 금속 무늬)
        tex[pmask == 0] = 0
        img = np.clip(img.astype(np.float32) + tex[:, :, None], 0, 255).astype(np.uint8)
        sq = wd.project(wd.corners_stage(), self._bw, self._bh)                               # 새겨진 사각형(가는 어두운 선)
        cv2.polylines(img, [np.rint(sq * 8).astype(np.int32)], True, (120, 122, 126), max(1, self._bw // 900), cv2.LINE_AA, shift=3)
        if wd.loaded:
            for k, sm in enumerate(wd.samples):
                hw, hh = sm["w"] / 2.0, sm["h"] / 2.0
                q = [wd.plate_to_stage(sm["px"] + dx, sm["py"] + dy) for dx, dy in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]
                m = np.zeros(img.shape[:2], np.uint8)
                cv2.fillConvexPoly(m, np.rint(wd.project(q, self._bw, self._bh) * 8).astype(np.int32), 255, lineType=cv2.LINE_AA, shift=3)
                a_ = (m.astype(np.float32) / 255.0)[:, :, None]
                tint = np.float32([-30 + 3 * (k % 3), -28 + 2 * (k % 4), -22])              # 반투명한 회색 조각: 판의 결이 비치고 조금 어둡다
                img = np.clip(img.astype(np.float32) + a_ * tint, 0, 255).astype(np.uint8)
        # 조명 기울기 + 센서 잡음(완전히 평평한 그림에서는 검출의 잡음 추정이 0 이 된다)
        gx = np.linspace(-6, 6, self._bw, dtype=np.float32)[None, :, None]
        noise = rng.normal(0.0, 1.2, img.shape).astype(np.float32)
        return np.clip(img.astype(np.float32) + gx + noise, 0, 255).astype(np.uint8)

    def _render(self):
        self._ensure()
        wd = self.world
        img = self._bg.copy()
        hx, hy = self.head_xy_fn()
        x0, y0, x1, y1 = wd.head_rect
        body = [(hx + x0, hy + y0), (hx + x1, hy + y0), (hx + x1, hy + y1), (hx + x0, hy + y1)]
        self._poly(img, body, (44, 46, 50), wd.head_h)                                       # 헤드 윗면(샘플을 가린다)
        m = wd.marker_mm * 0.5 * (self._mk.shape[0] / 200.0)                                 # 흰 여백까지의 반 변(mm)
        cx, cy = hx + wd.marker_off[0], hy + wd.marker_off[1]
        quad = wd.project([(cx - m, cy + m), (cx + m, cy + m), (cx + m, cy - m), (cx - m, cy - m)], self._bw, self._bh, wd.head_h)   # 사진에서 왼쪽 위부터 시계 방향
        n = self._mk.shape[0]
        bx0, by0 = np.floor(quad.min(axis=0)).astype(int) - 1
        bx1, by1 = np.ceil(quad.max(axis=0)).astype(int) + 2
        cx0, cy0, cx1, cy1 = max(0, bx0), max(0, by0), min(self._bw, bx1), min(self._bh, by1)
        if cx1 > cx0 and cy1 > cy0:
            H = cv2.getPerspectiveTransform(np.float32([[0, 0], [n, 0], [n, n], [0, n]]), np.float32(quad - [cx0, cy0]))
            size = (int(cx1 - cx0), int(cy1 - cy0))
            patch = cv2.warpPerspective(self._mk, H, size, flags=cv2.INTER_LINEAR, borderValue=0)
            mask = cv2.warpPerspective(np.full_like(self._mk, 255), H, size, flags=cv2.INTER_NEAREST, borderValue=0)
            roi = img[cy0:cy1, cx0:cx1]
            roi[mask > 0] = cv2.cvtColor(patch, cv2.COLOR_GRAY2BGR)[mask > 0]
        img = np.clip(img.astype(np.int16) + np.roll(self._fn, (int(self._rng.integers(0, 97)), int(self._rng.integers(0, 89))), (0, 1)), 0, 255).astype(np.uint8)   # 프레임마다 다른 센서 잡음
        w, h = self._size()
        if (w, h) != (self._bw, self._bh):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
        return img


def open_fake(head_xy_fn=None, world=None, **kw):
    """Camera(open_capture=...) 에 꽂는 함수를 만든다(Camera 가 요청한 크기를 set 하면 그 크기로 열린다)."""
    def opener(idx, flag):
        return FakeCapture(head_xy_fn=head_xy_fn, world=world, **kw)
    return opener
