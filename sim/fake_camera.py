"""fake_camera.py - 가짜 카메라: cv2.VideoCapture 모양(isOpened · set · get · read · grab · retrieve · release).

판(회색 사각형) · 샘플 칩(어두운 사각형 3×5) · 헤드 마커(ArUco 4X4_50 id 10, 40 mm)가 보이는 합성 사진을 만든다.
헤드 마커는 head_xy_fn() 이 주는 스테이지 X · Y(mm)를 따라 움직인다(가짜 스테이지의 실제 위치).
장면은 가로 최대 1280 으로 그린 뒤 요청한 해상도로 늘린다(4080×3060 도 가볍게 흉내 낸다).
시험용: size_override(실제로 열리는 해상도를 요청과 다르게) · fail(프레임이 안 나옴).
"""

import threading
import time

import cv2
import numpy as np

PLATE_MM = (247.6, 247.8)
HEAD_MM = 40.0
HEAD_ID = 10


def _marker(px):
    d = cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50)
    return cv2.aruco.generateImageMarker(d, HEAD_ID, px)


class FakeCapture:
    def __init__(self, width=640, height=480, head_xy_fn=None, size_override=None, fourcc_override=None):
        self.default = (int(width), int(height))
        self.size_override = size_override
        self.fourcc = fourcc_override
        self.head_xy_fn = head_xy_fn or (lambda: (0.0, 0.0))
        self.opened = True
        self.fail = False
        self._props = {}
        self._built = None
        self._last = None

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
        size = self._size()
        if self._built == size:
            return
        self._built = size
        w, h = size
        self._base_w = min(1280, w)
        self._base_h = max(1, int(round(self._base_w * h / w)))
        self.s = 0.9 * min(self._base_w / PLATE_MM[0], self._base_h / PLATE_MM[1])            # 판 전체가 화면 안에(1 mm = s 화소)
        self.ox, self.oy = (self._base_w - PLATE_MM[0] * self.s) / 2.0, (self._base_h - PLATE_MM[1] * self.s) / 2.0
        self._bg = self._background()
        px = max(8, int(HEAD_MM * self.s))
        m = max(2, px // 5)                                           # 마커 둘레의 흰 여백(검출에 필요)
        self._mk = cv2.copyMakeBorder(_marker(px), m, m, m, m, cv2.BORDER_CONSTANT, value=255)

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
    def _background(self):
        img = np.full((self._base_h, self._base_w, 3), (58, 60, 64), np.uint8)             # 어두운 바닥
        x0, y0 = int(self.ox), int(self.oy)
        x1, y1 = int(self.ox + PLATE_MM[0] * self.s), int(self.oy + PLATE_MM[1] * self.s)
        cv2.rectangle(img, (x0, y0), (x1, y1), (196, 198, 200), -1)                         # 판
        cv2.rectangle(img, (x0, y0), (x1, y1), (120, 122, 126), 2)
        for r in range(3):                                                                  # 샘플 칩 3×5
            for c in range(5):
                cx = 62 + c * 31.0
                cy = 78 + r * 31.0
                p0 = (int(self.ox + (cx - 5) * self.s), int(self.oy + (cy - 5) * self.s))
                p1 = (int(self.ox + (cx + 5) * self.s), int(self.oy + (cy + 5) * self.s))
                cv2.rectangle(img, p0, p1, (92, 96, 104), -1)
        return img

    def _render(self):
        self._ensure()
        img = self._bg.copy()
        hx, hy = self.head_xy_fn()
        n = self._mk.shape[0]
        px = int(self.ox + hx * self.s - n / 2)
        py = int(self.oy + hy * self.s - n / 2)
        x0, y0 = max(0, px), max(0, py)
        x1, y1 = min(img.shape[1], px + n), min(img.shape[0], py + n)
        if x1 > x0 and y1 > y0:
            img[y0:y1, x0:x1] = cv2.cvtColor(self._mk[y0 - py:y1 - py, x0 - px:x1 - px], cv2.COLOR_GRAY2BGR)
        w, h = self._size()
        if (w, h) != (self._base_w, self._base_h):
            img = cv2.resize(img, (w, h), interpolation=cv2.INTER_LINEAR)
        return img


def open_fake(head_xy_fn=None, **kw):
    """Camera(open_capture=...) 에 꽂는 함수를 만든다(Camera 가 요청한 크기를 set 하면 그 크기로 열린다)."""
    def opener(idx, flag):
        return FakeCapture(head_xy_fn=head_xy_fn, **kw)
    return opener
