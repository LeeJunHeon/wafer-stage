"""calibstore.py - 좌표 보정(판 영역 · 제외 영역 · 기준점 · 변환) 과 헤드 위치 보정 저장소. 모두 **기준 프레임**(빈 판 사진)이다.

- 각 기록에 ref_id(어느 빈 판 사진 기준인지)를 둔다. 현재 빈 판 사진과 ref_id 가 다르면 무효.
- 기준점을 지우면 다시 적합한다: 4점 미만이면 변환을 지우고(applied 해제) 호출 쪽이 샘플 좌표를 다시 계산한다.
- 적용은 5점부터. 퇴화(한 직선 · 한쪽에 몰림)는 geom.degenerate_reason 으로 거절한다.
- 빈 판 사진을 바꿀 때: replace_ref(A) - 옛 -> 새 정렬 A 로 판 영역 · 제외 영역 · 기준점 픽셀 · 헤드 위치 보정 픽셀을 옮기고 다시 푼다.
"""

import time

import numpy as np

import store
from core import frames, geom, paths

MIN_APPLY = geom.APPLY_MIN_POINTS


def _now():
    return time.strftime("%Y-%m-%d %H:%M:%S")


class Calib:
    def __init__(self, path=None):
        self.path = path or paths.CALIB_PATH
        d = store.read_json(self.path)
        self.d = d if isinstance(d, dict) and "points" in d else self._new()
        self.H = None if self.d.get("H") is None else np.asarray(self.d["H"], np.float64)

    @staticmethod
    def _new():
        return {"created": _now(), "ref_id": None, "size": None, "area": None, "exclude": [], "points": [], "H": None,
                "rms_mm": None, "max_mm": None, "applied": False, "size_mm": None, "next_no": 1}

    def save(self):
        self.d["H"] = None if self.H is None else self.H.tolist()
        store.write_json(self.path, self.d)

    # ---- 읽기 --------------------------------------------------------
    @property
    def area(self):
        return self.d.get("area")

    @property
    def points(self):
        return self.d["points"]

    def status(self, ref_id=None, frame_size=None):
        """(코드, 문구): done | need | invalid."""
        if not self.d.get("area"):
            return "need", "판 영역 필요"
        if self.d.get("ref_id") and ref_id and self.d["ref_id"] != ref_id:
            return "invalid", "무효 · 빈 판 사진이 바뀜"
        if frame_size and self.d.get("size") and list(self.d["size"]) != [int(frame_size[0]), int(frame_size[1])]:
            return "invalid", "무효 · 해상도가 바뀜"
        if self.H is None or not self.d.get("applied"):
            n = len(self.points)
            return "need", "필요 · 기준점 %d / %d" % (n, MIN_APPLY)
        return "done", "완료 · 기준점 %d · 잔차 %.2f mm" % (len(self.points), self.d.get("rms_mm") or 0.0)

    def ready(self, ref_id=None, frame_size=None):
        return self.status(ref_id, frame_size)[0] == "done"

    def px_to_mm(self, u, v):
        if self.H is None:
            return None
        return geom.px_to_mm(self.H, u, v)

    def mm_to_px(self, x, y):
        if self.H is None:
            return None
        q = np.linalg.inv(self.H) @ np.array([x, y, 1.0])
        return float(q[0] / q[2]), float(q[1] / q[2])

    def local_mm_per_px(self, center=None):
        if self.H is None:
            return None
        c = center if center is not None else np.mean(np.asarray(self.d["area"], np.float64), axis=0)
        return geom.local_mm_per_px(self.H, c)

    def measured_size_mm(self):
        """좌표 보정으로 잰 판 영역 실측 크기(가로 · 세로 mm). 보정 전이면 None."""
        if self.H is None or not self.d.get("area"):
            return None
        q = np.asarray([self.px_to_mm(u, v) for u, v in geom.order_quad(self.d["area"])])
        ln = lambda a, b: float(np.hypot(*(q[a] - q[b])))           # noqa: E731
        return [round((ln(0, 1) + ln(2, 3)) / 2.0, 1), round((ln(1, 2) + ln(3, 0)) / 2.0, 1)]

    # ---- 판 영역 · 제외 영역 ------------------------------------------
    def set_area(self, quad, ref_id, size):
        q, why = geom.check_quad(quad, size)
        if q is None:
            return False, why
        self.d.update(area=q, ref_id=ref_id or self.d.get("ref_id"), size=[int(size[0]), int(size[1])])
        self.d["size_mm"] = self.measured_size_mm()
        self.save()
        return True, ""

    def set_exclude(self, polys):
        self.d["exclude"] = [[[round(float(u), 1), round(float(v), 1)] for u, v in p] for p in polys if len(p) >= 3]
        self.save()

    # ---- 기준점 -------------------------------------------------------
    def add_point(self, u, v, x, y, src="click", shot=None):
        no = int(self.d.get("next_no", 1))
        self.d["next_no"] = no + 1
        self.d["points"].append({"no": no, "u": round(float(u), 2), "v": round(float(v), 2), "x": round(float(x), 3), "y": round(float(y), 3),
                                 "src": src, "shot": shot})
        self.refit()
        self.save()
        return no

    def delete_point(self, no):
        n0 = len(self.points)
        self.d["points"] = [p for p in self.points if p["no"] != int(no)]
        if len(self.points) == n0:
            return False
        self.refit()
        self.save()
        return True

    def point_near(self, shot, x, y, tol_mm=0.5):
        """샘플로 기록한 점 구분: (촬영 id · 번호) 가 같거나 같은 자리에 이미 점이 있음."""
        for p in self.points:
            if shot is not None and p.get("shot") == shot:
                return p
            if abs(p["x"] - x) < tol_mm and abs(p["y"] - y) < tol_mm:
                return p
        return None

    def refit(self):
        """점으로 변환을 다시 푼다. 4점 미만 · 퇴화면 변환을 지우고 applied 를 해제한다. 반환: 사유 문구(성공이면 "")."""
        pts = self.points
        why = geom.degenerate_reason(pts, self.d.get("area"))
        f = None if why else geom.fit(pts, self.d.get("area"))
        if f is None:
            self.H = None
            self.d.update(rms_mm=None, max_mm=None, applied=False)
            for p in pts:
                p.pop("res_mm", None)
            return why or "변환 못 품"
        self.H = f["H"]
        for p, r in zip(pts, f["res"]):
            p["res_mm"] = round(r, 3)
        self.d.update(rms_mm=round(f["rms"], 3), max_mm=round(f["max"], 3))
        self.d["size_mm"] = self.measured_size_mm()
        return ""

    def warnings(self):
        out = []
        if len(self.points) == 4:
            out.append("점 4개는 잔차가 항상 0 · 칩 가운데에 1점 이상 추가")
        return out

    def apply(self):
        """5점부터. 반환 (ok, 사유)."""
        if len(self.points) < MIN_APPLY:
            return False, "기준점 %d개 · %d개부터 적용" % (len(self.points), MIN_APPLY)
        why = self.refit()
        if why:
            return False, why
        self.d["applied"] = True
        self.save()
        return True, ""

    # ---- 빈 판 사진 교체 -----------------------------------------------
    def replace_ref(self, A, new_id, size=None):
        """A = frames.Align(옛 기준 -> 새 기준). 판 영역 · 제외 영역 · 기준점 픽셀을 옮기고 다시 푼다."""
        if self.d.get("area"):
            self.d["area"] = [[round(float(a), 1), round(float(b), 1)] for a, b in A.to_cur(self.d["area"])]
        self.d["exclude"] = [[[round(float(a), 1), round(float(b), 1)] for a, b in A.to_cur(p)] for p in self.d.get("exclude") or []]
        for p in self.points:
            p["u"], p["v"] = (round(float(t), 2) for t in A.to_cur([[p["u"], p["v"]]])[0])
        was = self.d.get("applied")
        self.d["ref_id"] = new_id
        if size:
            self.d["size"] = [int(size[0]), int(size[1])]
        why = self.refit()
        self.d["applied"] = bool(was and not why and len(self.points) >= MIN_APPLY)
        self.save()
        return why


class HeadCalib:
    """헤드 마커 픽셀(기준 프레임) -> 스테이지 mm. 격자 n x n 자동 이동 촬영으로 얻은 쌍."""

    def __init__(self, path=None):
        self.path = path or paths.HEAD_CALIB_PATH
        d = store.read_json(self.path)
        self.d = d if isinstance(d, dict) and "pairs" in d else {"ref_id": None, "size": None, "pairs": [], "H": None, "rms_mm": None, "applied": False}
        self.H = None if self.d.get("H") is None else np.asarray(self.d["H"], np.float64)

    def save(self):
        self.d["H"] = None if self.H is None else self.H.tolist()
        store.write_json(self.path, self.d)

    def clear(self):
        self.d.update(pairs=[], H=None, rms_mm=None, applied=False)
        self.H = None

    def add(self, u, v, x, y, ref_id, size):
        self.d["pairs"].append({"u": round(float(u), 2), "v": round(float(v), 2), "x": round(float(x), 3), "y": round(float(y), 3)})
        self.d["ref_id"], self.d["size"] = ref_id, [int(size[0]), int(size[1])]

    def fit(self, quad_mm=None):
        """반환 (ok, 사유). 퇴화 거절은 좌표 보정과 같은 규칙. 헤드 마커는 판과 다른 평면이라 픽셀 분포가 아니라 **스테이지 mm 분포**를
        판 영역(mm)과 비교한다."""
        pairs = self.d["pairs"]
        why = geom.degenerate_reason([{"u": p["x"], "v": p["y"]} for p in pairs], quad_mm)
        f = None if why else geom.fit(pairs, None)
        if f is None:
            self.H = None
            return False, why or "변환 못 품"
        self.H = f["H"]
        for p, r in zip(pairs, f["res"]):
            p["res_mm"] = round(r, 3)
        self.d["rms_mm"] = round(f["rms"], 3)
        return True, ""

    def apply(self, rms_limit_mm):
        if self.H is None:
            return False, "변환 없음"
        if len(self.d["pairs"]) < MIN_APPLY:
            return False, "점 %d개 · %d개부터 적용" % (len(self.d["pairs"]), MIN_APPLY)
        if (self.d.get("rms_mm") or 0.0) > rms_limit_mm:
            return False, "잔차 %.2f mm · 한도 %.2f mm 초과" % (self.d["rms_mm"], rms_limit_mm)
        self.d["applied"] = True
        self.save()
        return True, ""

    def status(self, ref_id=None, frame_size=None):
        if self.H is None or not self.d.get("applied"):
            return "need", "필요"
        if ref_id and self.d.get("ref_id") and self.d["ref_id"] != ref_id:
            return "invalid", "무효 · 빈 판 사진이 바뀜"
        if frame_size and self.d.get("size") and list(self.d["size"]) != [int(frame_size[0]), int(frame_size[1])]:
            return "invalid", "무효 · 해상도가 바뀜"
        return "done", "완료 · 잔차 %.2f mm" % (self.d.get("rms_mm") or 0.0)

    def predict(self, u, v):
        """기준 프레임 마커 픽셀 -> 스테이지 (X, Y) mm."""
        return None if self.H is None else geom.px_to_mm(self.H, u, v)

    def replace_ref(self, A, new_id):
        for p in self.d["pairs"]:
            p["u"], p["v"] = (round(float(t), 2) for t in A.to_cur([[p["u"], p["v"]]])[0])
        self.d["ref_id"] = new_id
        ok, _why = self.fit(None)
        self.d["applied"] = bool(self.d.get("applied") and ok)
        self.save()


def to_ref_point(A, uv):
    """촬영 프레임 점 -> 기준 프레임. A 가 없으면(정렬 없음) None - 호출 쪽이 좌표를 막는다."""
    if A is None:
        return None
    u, v = A.to_ref([list(uv)])[0]
    return float(u), float(v)


__all__ = ["Calib", "HeadCalib", "to_ref_point", "frames"]
