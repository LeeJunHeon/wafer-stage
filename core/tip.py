"""tip.py - 공구 끝 모양(사진 위에 그리는 닿는 자리)과 샘플 윤곽 안에 들어가는지 검사.

공구마다 tip_shape("rect"|"circle"), tip_w_mm, tip_h_mm(원이면 지름 = tip_w_mm), tip_angle_deg(사진 기준:
0 = 긴 변(tip_w)이 사진 가로, 90 = 사진 세로). 판 보정과 무관하게 각도는 사진 기준이다. 샘플 중심 = 공구 끝이
닿을 자리. 서버(맞음 검사)와 화면(그리기 · 끌기 미리보기)이 같은 규칙을 쓴다(화면은 js/camera.js 에 같은 식).
"""

import math

import cv2
import numpy as np

# 공구 이름별 기본값(설정에 tip_* 가 없는 예전 공구용). 기준 포인터(핀) 10 x 2 mm 90°, 그 밖(히트건 등) 지름 5 mm 원.
POINTER_NAME = "기준 포인터"
DEFAULT_POINTER = {"tip_shape": "rect", "tip_w_mm": 10.0, "tip_h_mm": 2.0, "tip_angle_deg": 90.0}
DEFAULT_OTHER = {"tip_shape": "circle", "tip_w_mm": 5.0, "tip_h_mm": 5.0, "tip_angle_deg": 0.0}


def _num(v, d):
    try:
        x = float(v)
        return x if math.isfinite(x) else d
    except (TypeError, ValueError):
        return d


def tip_of(tool):
    """공구 dict → {shape, w, h, angle}(mm · 도). 빠진 값은 기본값."""
    tool = tool or {}
    base = DEFAULT_POINTER if tool.get("name") == POINTER_NAME else DEFAULT_OTHER
    shape = tool.get("tip_shape") if tool.get("tip_shape") in ("rect", "circle") else base["tip_shape"]
    w = _num(tool.get("tip_w_mm"), base["tip_w_mm"])
    h = _num(tool.get("tip_h_mm"), base["tip_h_mm"])
    if shape == "circle":
        h = w
    return {"shape": shape, "w": max(w, 0.0), "h": max(h, 0.0),
            "angle": _num(tool.get("tip_angle_deg"), base["tip_angle_deg"])}


def polygon(tip, mmpx, cx, cy, n=24):
    """공구 끝 윤곽(사진 px 점 목록). mmpx = 사진 px 당 mm."""
    k = 1.0 / max(mmpx, 1e-9)
    if tip["shape"] == "circle":
        r = tip["w"] * k / 2.0
        return [(cx + r * math.cos(2 * math.pi * i / n), cy + r * math.sin(2 * math.pi * i / n)) for i in range(n)]
    hw, hh = tip["w"] * k / 2.0, tip["h"] * k / 2.0
    th = math.radians(tip["angle"])
    c, s = math.cos(th), math.sin(th)
    return [(cx + x * c - y * s, cy + x * s + y * c) for x, y in ((-hw, -hh), (hw, -hh), (hw, hh), (-hw, hh))]


def fits(poly, verts, margin_px):
    """공구 끝 윤곽이 샘플 윤곽(verts) 안에 들어가는가(margin_px 만큼은 벗어나도 봐준다).
    윤곽이 없으면 None(검사 안 함)."""
    if not verts or len(verts) < 3:
        return None
    contour = np.asarray(verts, np.float32).reshape(-1, 1, 2)
    worst = min(cv2.pointPolygonTest(contour, (float(x), float(y)), True) for x, y in poly)
    return bool(worst >= -float(margin_px))
