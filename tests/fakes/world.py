"""world.py - 가짜 세계 하나. 가짜 카메라 · 가짜 K2450 · 열 모델이 모두 이것을 본다(서로 다른 그림을 그리지 않게).

  판      : 새겨진 사각형 한 변 size_mm. 꼭짓점 A 의 스테이지 자리 plate_a, 스테이지 축에서 angle_deg 만큼 돌아가 있다.
            판 좌표 (px, py) mm: A = (0, 0), A→B 가 +x, A→D 가 +y. 사진에서 A 는 왼쪽 위(+x 오른쪽 · +y 아래)다.
            스테이지 Y 는 사진에서 위쪽이 + 라서 판 +y 는 스테이지 −Y 쪽이다(좌표 맞추기가 이런 뒤집힘도 맞춰야 한다).
  샘플    : 판 좌표의 가운데 · 크기 · 두께 · 저항(VO2 전이). loaded 가 False 면 판이 비어 있다.
  높이    : plate_z = 핀이 맨 금속 판에 닿는 스테이지 Z(mm). 샘플 표면 = plate_z − 두께.
  카메라  : 약간 비스듬한(사다리꼴) 사진. 판 평면의 점은 호모그래피로, 판보다 높은 점(헤드 · 헤드 마커)은 시차만큼 밀려 보인다.
  헤드    : 핀 가운데가 스테이지 X · Y. 헤드 윗면(높이 head_h)에 ArUco 마커(40 mm)가 있다 - 핀에서 marker_off 만큼 떨어져 있다.
"""

import math

import numpy as np

SIZE_MM = 154.0
STAGE_MAX = (247.6, 247.8)


def default_samples():
    """3×3 으로 놓인 9개: 8개는 핀(10×2 mm)이 들어가는 크기(긴 쪽이 스테이지 Y 방향), 하나(마지막)는 핀보다 작다. 자리 · 크기를 조금씩 흩뜨린다."""
    out = []
    jit = [(0.8, -0.6), (-6.5, 4.9), (0.3, 7.4), (-0.9, -8.3), (11.6, 9.7), (-5.2, -0.8), (7.9, 3.2), (-9.7, 6.5), (0.4, -5.4)]   # 줄을 맞추지 않고 놓는다
    k = 0
    for r in range(3):
        for c in range(3):
            jx, jy = jit[k]
            small = k == 8
            out.append({
                "px": 40.0 + c * 37.0 + jx, "py": 38.0 + r * 39.0 + jy,            # 판 좌표 가운데(mm)
                "w": 5.0 if small else 5.0 + 0.4 * (k % 3), "h": 5.0 if small else 12.0 + 0.3 * (k % 4),   # w = 판 x 쪽 · h = 판 y 쪽(mm). v2 검출이 맞춰진 크기(긴 변 약 11 mm)
                "thick": 0.5 + 0.05 * ((k * 3) % 5),                                 # 두께(mm) - 샘플마다 표면 높이가 다르다
                "r_hi": 3.0e5 * (1.0 + 0.15 * k), "r_lo": 2.0e2 * (1.0 + 0.1 * k),
                "t_up": 66.0 + 0.4 * k, "t_down": 58.0 + 0.3 * k,
                "lag4": 0.0,                                                         # 감지 핀 두 개가 더 내려가야 닿는 거리(mm)
            })
            k += 1
    return out


class World:
    def __init__(self):
        self.size_mm = SIZE_MM
        self.angle_deg = 1.0                      # 판이 스테이지 축에서 돌아간 각
        self.plate_a = (47.0, 201.0)              # 꼭짓점 A 의 스테이지 X · Y(mm)
        self.plate_z = 14.0                       # 핀이 맨 판에 닿는 Z(mm)
        self.samples = default_samples()
        self.loaded = True
        self.version = 0                          # 장면이 바뀔 때마다 올린다(가짜 카메라가 배경을 다시 그린다)
        # 카메라(천장): 스테이지 X · Y 범위를 조금 넘게 본다. 사다리꼴 - 위쪽(스테이지 Y 큰 쪽)이 조금 좁다.
        self.cam_xy = (125.0, 125.0)              # 카메라 바로 아래 스테이지 자리
        self.cam_h = 600.0                        # 판 평면에서 카메라까지 높이(mm)
        self.view = (-55.0, -10.0, 305.0, 260.0)  # 사진에 담기는 스테이지 범위 x0 · y0 · x1 · y1 (mm)
        self.keystone = 0.03                      # 위쪽 가장자리가 양쪽에서 이만큼(폭 비율) 좁아진다
        # 헤드
        self.head_h = 60.0                        # 헤드 윗면(마커) 높이(mm, 판 평면 위)
        self.marker_mm = 40.0
        self.marker_off = (0.0, 32.0)             # 핀 가운데 → 마커 가운데(스테이지 mm)
        self.head_rect = (-36.0, -26.0, 36.0, 66.0)   # 헤드 윗면 사각형(핀 가운데 기준 x0 · y0 · x1 · y1)
        self.cooler_map = [[2, 3, 4], [5, 6, 7], [8, 9, 10]]    # 실제 쿨러 배치(판 3×3 칸 → 릴레이 번호)

    # ---- 판 좌표 ↔ 스테이지 ----
    def _uv(self):
        t = math.radians(self.angle_deg)
        return (math.cos(t), math.sin(t)), (math.sin(t), -math.cos(t))              # 판 +x · +y 의 스테이지 방향

    def plate_to_stage(self, px, py):
        (ux, uy), (vx, vy) = self._uv()
        return (self.plate_a[0] + px * ux + py * vx, self.plate_a[1] + px * uy + py * vy)

    def stage_to_plate(self, x, y):
        (ux, uy), (vx, vy) = self._uv()
        dx, dy = x - self.plate_a[0], y - self.plate_a[1]
        return (dx * ux + dy * uy, dx * vx + dy * vy)                               # u · v 는 서로 수직인 단위 벡터

    def corners_stage(self):
        """꼭짓점 A · B · C · D 의 스테이지 X · Y."""
        s = self.size_mm
        return [self.plate_to_stage(*p) for p in ((0, 0), (s, 0), (s, s), (0, s))]

    # ---- 카메라 ----
    def _h_world(self, w, h):
        """판 평면의 스테이지 좌표 → 사진 픽셀 호모그래피(사다리꼴)."""
        import cv2
        x0, y0, x1, y1 = self.view
        k = self.keystone * w
        src = np.float32([[x0, y1], [x1, y1], [x1, y0], [x0, y0]])                  # 스테이지: 왼쪽 위 · 오른쪽 위 · 오른쪽 아래 · 왼쪽 아래(Y 는 위가 +)
        dst = np.float32([[k, 0], [w - 1 - k, 0], [w - 1, h - 1], [0, h - 1]])
        return cv2.getPerspectiveTransform(src, dst)

    def project(self, pts, w, h, height=0.0):
        """스테이지 좌표 점들(N×2, 높이 height mm) → 사진 픽셀(N×2). 높은 점은 카메라 바로 아래에서 멀어지는 쪽으로 밀려 보인다(시차)."""
        import cv2
        p = np.asarray(pts, np.float64).reshape(-1, 2)
        if height:
            c = np.asarray(self.cam_xy, np.float64)
            p = c + (p - c) * (self.cam_h / (self.cam_h - float(height)))
        return cv2.perspectiveTransform(p.reshape(-1, 1, 2).astype(np.float64), self._h_world(w, h)).reshape(-1, 2)

    def plate_corners_px(self, w, h):
        """새겨진 사각형 꼭짓점 A · B · C · D 의 사진 픽셀."""
        return self.project(self.corners_stage(), w, h)

    def marker_center_px(self, x, y, w, h):
        """핀이 스테이지 (x, y) 에 있을 때 헤드 마커 가운데의 사진 픽셀."""
        return self.project([(x + self.marker_off[0], y + self.marker_off[1])], w, h, self.head_h)[0]

    # ---- 샘플 ----
    def sample_stage(self, i):
        s = self.samples[i]
        return self.plate_to_stage(s["px"], s["py"])

    def sample_surface_z(self, i):
        return self.plate_z - float(self.samples[i]["thick"])

    def sample_at(self, x, y):
        """스테이지 (x, y) 아래의 샘플 번호(0부터). 없으면 None."""
        if not self.loaded:
            return None
        px, py = self.stage_to_plate(x, y)
        for i, s in enumerate(self.samples):
            if abs(px - s["px"]) <= s["w"] / 2.0 and abs(py - s["py"]) <= s["h"] / 2.0:
                return i
        return None

    def on_plate(self, x, y, margin=20.0):
        px, py = self.stage_to_plate(x, y)
        return -margin <= px <= self.size_mm + margin and -margin <= py <= self.size_mm + margin

    def zone_relay(self, i):
        """샘플 i 아래 쿨러의 릴레이 번호(실제 배치)."""
        s = self.samples[i]
        c = min(2, max(0, int(s["px"] / (self.size_mm / 3.0))))
        r = min(2, max(0, int(s["py"] / (self.size_mm / 3.0))))
        return self.cooler_map[r][c]

    def touch(self, x, y, z):
        """핀이 (x, y, z) 에 있을 때: (닿은 핀 수 0 · 2 · 4, 샘플 번호 | None, 단락). 샘플 밖 판 위에서 plate_z 까지 내려가면 금속 판(단락)."""
        i = self.sample_at(x, y)
        if i is not None:
            surf = self.sample_surface_z(i)
            if z < surf - 1e-9:
                return 0, i, False
            if z < surf + float(self.samples[i].get("lag4", 0.0)) - 1e-9:
                return 2, i, False
            return 4, i, False
        if self.on_plate(x, y) and z >= self.plate_z - 1e-9:
            return 4, None, True
        return 0, None, False

    def set_loaded(self, on):
        self.loaded = bool(on)
        self.version += 1
