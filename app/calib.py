"""calib.py - 초기 설정 단계에서 정한 보정 값. <데이터 폴더>/calib/calib.json 과 calib/empty_plate.png 에 둔다(settings.json 과 따로).

언제든 다시 할 수 있고, 다시 하면 덮어쓴다. 값이 없는 단계는 None 이다.

  origin   : 축마다 원점을 등록한 때 · 방법 {"x": {"t", "how"}, …}
  plate    : 사진에서 누른 판 꼭짓점 A · B · C · D 픽셀(원본 해상도) · 사진 크기 · 때
  empty    : 빈 판 사진을 찍은 때(파일은 empty_plate.png)
  coords   : 꼭짓점 A~D 에 핀을 대고 기억한 스테이지 X · Y, 보정 점(사진 픽셀 ↔ 스테이지 X · Y, 최근 30점)
  marker   : 촬영 위치에서 본 헤드 마커 픽셀 · "mm 당 픽셀" 2×2 행렬
  contact  : 접촉 높이를 저장한 때 · 그때 잰 저항(높이 값은 settings contact_z_mm)

변환(사진 → 판 mm, 사진 → 스테이지 X · Y)은 여기 값에서 그때그때 만든다(따로 저장하지 않는다).
"""

import json
import os
import tempfile
import threading
import time
from pathlib import Path

import numpy as np

from app import geometry as geo

CORNERS = ("A", "B", "C", "D")
MAX_EXTRA = 30                                      # 보정 점은 최근 30점까지


class Calib:
    def __init__(self, data_dir):
        self.dir = Path(data_dir) / "calib"
        self.path = self.dir / "calib.json"
        self.empty_path = self.dir / "empty_plate.png"
        self._lock = threading.RLock()
        self.d = {"origin": {}, "plate": None, "empty": None, "coords": {"stage": {}, "extra": []}, "marker": None, "contact": None, "cooler": None}
        self.note = ""
        self.load()

    # ---- 읽기 · 쓰기 ----
    def load(self):
        if not self.path.exists():
            return
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.note = "보정 값 파일 읽기 실패 · %s" % str(e)[:40]
            return
        if isinstance(raw, dict):
            for k in self.d:
                if k in raw:
                    self.d[k] = raw[k]
            c = self.d.get("coords") or {}
            self.d["coords"] = {"stage": dict(c.get("stage") or {}), "extra": list(c.get("extra") or [])}
            self.d["origin"] = dict(self.d.get("origin") or {})

    def save(self):
        with self._lock:
            self.dir.mkdir(parents=True, exist_ok=True)
            fd, tmp = tempfile.mkstemp(dir=str(self.dir), prefix=".calib.", suffix=".tmp")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(self.d, f, ensure_ascii=False, indent=1)
                os.replace(tmp, self.path)
            finally:
                if os.path.exists(tmp):
                    os.unlink(tmp)

    # ---- 값 바꾸기 ----
    def set_origin(self, axes, how):
        with self._lock:
            for ax in axes:
                self.d["origin"][ax] = {"t": time.time(), "how": how}
        self.save()

    def set_plate(self, corners_px, size):
        """판 꼭짓점을 다시 정하면 사진 픽셀로 기억한 것(보정 점 · 헤드 마커)은 버린다 - 카메라가 움직였을 수 있다. 반환: 버린 보정 점 수."""
        with self._lock:
            dropped = len(self.d["coords"]["extra"])
            self.d["plate"] = {"corners": [[float(x), float(y)] for x, y in corners_px], "size": [int(size[0]), int(size[1])], "t": time.time()}
            self.d["coords"]["extra"] = []
            self.d["marker"] = None
        self.save()
        return dropped

    def set_empty(self, size):
        with self._lock:
            self.d["empty"] = {"t": time.time(), "size": [int(size[0]), int(size[1])]}
        self.save()

    def set_corner_stage(self, name, xy):
        with self._lock:
            self.d["coords"]["stage"][name] = [round(float(xy[0]), 3), round(float(xy[1]), 3)]
            self.d["coords"]["t"] = time.time()
        self.save()

    def add_extra(self, px, xy):
        with self._lock:
            ex = self.d["coords"]["extra"]
            ex.append({"px": [round(float(px[0]), 1), round(float(px[1]), 1)], "xy": [round(float(xy[0]), 3), round(float(xy[1]), 3)], "t": time.time()})
            del ex[:-MAX_EXTRA]
        self.save()

    def remove_extra(self, index):
        with self._lock:
            ex = self.d["coords"]["extra"]
            if 0 <= int(index) < len(ex):
                del ex[int(index)]
        self.save()

    def clear_coords(self):
        with self._lock:
            self.d["coords"] = {"stage": {}, "extra": []}
            self.d["marker"] = None
        self.save()

    def set_marker(self, px, jac):
        with self._lock:
            self.d["marker"] = None if px is None else {"px": [float(px[0]), float(px[1])], "J": [[float(v) for v in r] for r in jac], "t": time.time()}
        self.save()

    def set_contact(self, info):
        with self._lock:
            self.d["contact"] = dict(info, t=time.time())
        self.save()

    def set_cooler(self):
        with self._lock:
            self.d["cooler"] = {"t": time.time()}
        self.save()

    # ---- 변환 ----
    def plate_h(self, size_mm):
        """사진 → 판 mm. 판 꼭짓점이 없으면 None."""
        p = self.d.get("plate")
        if not p:
            return None
        return geo.photo_to_plate(p["corners"], size_mm)

    def coord_pairs(self):
        """(사진 픽셀, 스테이지 X · Y) 짝: 꼭짓점 넷(둘 다 있을 때) + 보정 점."""
        p = self.d.get("plate")
        st = self.d["coords"]["stage"]
        src, dst = [], []
        if p:
            for i, n in enumerate(CORNERS):
                if n in st:
                    src.append(p["corners"][i])
                    dst.append(st[n])
        if len(src) == 4:
            for e in self.d["coords"]["extra"]:
                src.append(e["px"])
                dst.append(e["xy"])
        return src, dst

    def stage_h(self):
        """사진 → 스테이지 X · Y. 꼭짓점 4점이 모여야 만든다(보정 점이 있으면 전체 점의 최소제곱)."""
        src, dst = self.coord_pairs()
        if len(src) < 4:
            return None
        return geo.homography(src, dst)

    def coords_ready(self):
        return self.stage_h() is not None

    def d_guess(self):
        """A · B · C 를 찍었으면 D 의 예상 자리(A + C − B)."""
        st = self.d["coords"]["stage"]
        if all(n in st for n in "ABC") and "D" not in st:
            a, b, c = (np.asarray(st[n], float) for n in "ABC")
            g = a + c - b
            return [round(float(g[0]), 2), round(float(g[1]), 2)]
        return None

    def extra_errors(self):
        """보정 점마다 맞춘 변환과의 차이(mm)."""
        H = self.stage_h()
        out = []
        for e in self.d["coords"]["extra"]:
            if H is None:
                out.append(None)
            else:
                x, y = geo.apply1(H, *e["px"])
                out.append(round(float(np.hypot(x - e["xy"][0], y - e["xy"][1])), 2))
        return out

    def plate_to_stage(self, size_mm):
        """판 mm → 스테이지 X · Y 호모그래피(사진을 거쳐서). 없으면 None."""
        Hp, Hs = self.plate_h(size_mm), self.stage_h()
        if Hp is None or Hs is None:
            return None
        try:
            return Hs @ np.linalg.inv(Hp)
        except np.linalg.LinAlgError:
            return None

    def stage_to_plate(self, size_mm):
        """스테이지 X · Y → 판 mm 호모그래피(화면이 지금 핀 자리를 판 사진 위에 그릴 때 쓴다). 없으면 None."""
        T = self.plate_to_stage(size_mm)
        if T is None:
            return None
        try:
            return np.linalg.inv(T)
        except np.linalg.LinAlgError:
            return None

    def head_offset_mm(self, marker_px):
        """지금 본 헤드 마커 픽셀이 기억한 자리와 얼마나 다른가(mm). 마커 기억이 없으면 None."""
        m = self.d.get("marker")
        if not m or marker_px is None:
            return None
        try:
            J = np.asarray(m["J"], float)                        # 열 = X · Y 로 1 mm 움직일 때 마커가 움직이는 픽셀
            d = np.linalg.solve(J, np.asarray(marker_px, float) - np.asarray(m["px"], float))
        except np.linalg.LinAlgError:
            return None
        return float(np.hypot(d[0], d[1]))
