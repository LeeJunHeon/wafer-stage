"""frames.py - 좌표 계층: 기준 좌표계 하나(빈 판 사진)와 프레임 태그.

규칙
- 저장하는 픽셀 좌표(판 영역 · 기준점 · 제외 영역 · 헤드 위치 보정)는 전부 **기준 프레임**이다.
- 프레임 사이 변환은 이 모듈의 Align(정렬 M) 으로만, 한곳에서 한다. 다른 곳에서 M 을 직접 곱하지 않는다.
- 프레임 태그: REF(빈 판 사진) · SHOT(그 촬영 사진) · SNAP(단발 촬영). 태그가 다른 점끼리는 섞지 않는다(PxPoint 의 tag 확인).
"""

from dataclasses import dataclass

import cv2
import numpy as np

REF, SHOT, SNAP = "ref", "shot", "snap"
TAGS = (REF, SHOT, SNAP)


class FrameError(Exception):
    pass


@dataclass(frozen=True)
class PxPoint:
    """프레임 태그가 붙은 픽셀 점."""
    u: float
    v: float
    tag: str = REF

    def __post_init__(self):
        if self.tag not in TAGS:
            raise FrameError("프레임 태그 오류 · %r" % (self.tag,))

    def xy(self):
        return (self.u, self.v)


def _h3(M):
    M = np.asarray(M, np.float64)
    if M.shape == (2, 3):
        M = np.vstack([M, [0.0, 0.0, 1.0]])
    if M.shape != (3, 3) or not np.all(np.isfinite(M)):
        raise FrameError("정렬 변환 모양 오류")
    return M


class Align:
    """기준 → 현재(그 촬영) 변환. M 은 2x3(유클리드) 또는 3x3(원근). 항등이면 identity()."""

    def __init__(self, M, tag=SHOT, model="euclid", resid=0.0, ref_id=""):
        self.M = _h3(M)
        self.Minv = np.linalg.inv(self.M)
        self.tag = tag                      # 현재 쪽 프레임 태그
        self.model = model
        self.resid = float(resid)
        self.ref_id = ref_id                # 어느 빈 판 사진 기준인지

    @staticmethod
    def identity(tag=SHOT, ref_id=""):
        return Align(np.eye(3), tag, "identity", 0.0, ref_id)

    @staticmethod
    def _apply(H, pts):
        P = np.asarray(pts, np.float64).reshape(-1, 2)
        Q = np.column_stack([P, np.ones(len(P))]) @ H.T
        return Q[:, :2] / Q[:, 2:3]

    def to_cur(self, pts):
        """기준 좌표 점(들) → 현재 사진 좌표. 입력 N×2 또는 (u, v) → N×2."""
        return self._apply(self.M, pts)

    def to_ref(self, pts):
        """현재 사진 좌표 점(들) → 기준 좌표."""
        return self._apply(self.Minv, pts)

    def pt_to_cur(self, p):
        if p.tag != REF:
            raise FrameError("기준 프레임 점만 현재로 옮길 수 있음 (%s)" % p.tag)
        u, v = self.to_cur([p.xy()])[0]
        return PxPoint(float(u), float(v), self.tag)

    def pt_to_ref(self, p):
        if p.tag != self.tag:
            raise FrameError("프레임 태그 불일치 · %s ≠ %s" % (p.tag, self.tag))
        u, v = self.to_ref([p.xy()])[0]
        return PxPoint(float(u), float(v), REF)

    def shift_px(self, center_ref):
        """판 중심의 이동량(px)."""
        c = np.asarray(center_ref, np.float64).reshape(1, 2)
        return float(np.hypot(*(self.to_cur(c)[0] - c[0])))

    def theta_deg(self):
        return float(np.degrees(np.arctan2(self.M[1, 0], self.M[0, 0])))

    def warp_ref_roi(self, ref_bgr, roi):
        """기준 사진을 현재 사진 좌표 ROI (x0, y0, x1, y1) 에 맞춰 잘라 낸다."""
        x0, y0, x1, y1 = roi
        T = np.array([[1.0, 0, -x0], [0, 1.0, -y0], [0, 0, 1.0]]) @ self.M
        size = (x1 - x0, y1 - y0)
        if self.model == "homography":
            return cv2.warpPerspective(ref_bgr, T, size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)
        return cv2.warpAffine(ref_bgr, T[:2], size, flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)

    def as_dict(self):
        return {"M": self.M.tolist(), "model": self.model, "resid": round(self.resid, 3), "ref_id": self.ref_id, "tag": self.tag}

    @staticmethod
    def from_dict(d):
        return Align(d["M"], d.get("tag", SHOT), d.get("model", "euclid"), d.get("resid", 0.0), d.get("ref_id", ""))


def move_polys(polys, A):
    """기준 프레임 다각형 목록(판 영역 · 제외 영역)을 정렬 A 로 옮긴다(빈 판 사진 교체 때 새 기준으로)."""
    return [A.to_cur(p).tolist() for p in polys]
