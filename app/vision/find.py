"""find.py - 보정한 판 사진에서 샘플 찾기. 검출 방식과 값은 v2 것 그대로(fixed_detect · refdiff), 여기서는 입력 · 출력만 맞춘다.

- 입력: 원본에서 판 영역만 잘라 위에서 본 모습으로 보정한 사진(1 mm = ppmm 픽셀). 빈 판 사진도 같은 변환으로 보정한 것.
  검출기는 안에서 샘플 긴 변이 60 픽셀이 되게 줄여서 찾는다(v2 가 쓰던 작업 해상도 그대로).
- 빈 판 사진과 비교가 잘 안 되면(v2 의 정렬 잔차 기준) 막지 않고 사진 한 장으로 찾는 방식으로 넘어간다. 빈 판 사진이 없어도 같다.
- 격자 예측으로만 만든 약한 후보(source = predicted)는 "확인 필요"(weak)로 표시한다(부르는 쪽이 기본으로 빼 둔다).
- 출력(샘플마다, 판 mm): 가운데 · 윤곽 · 가로 세로 · 넓이 · 약함 여부. 순서는 사진에서 윗줄부터 왼쪽 → 오른쪽.
"""

import time

import cv2
import numpy as np

from app.vision import fixed_detect, refdiff

PARAMS = {"fixed": {}}                              # v2 기본값 그대로(fixed_detect.DEFAULTS)
ROW_K = 0.6                                         # 줄 묶기: 가운데 y 가 샘플 긴 변의 이 배 안이면 같은 줄


def _quiet(*_a, **_k):
    pass


def find_samples(cur, empty, ppmm, margin_mm=0.0, log=None):
    """cur · empty = 보정한 판 사진(BGR, 같은 크기, 새겨진 사각형 바깥을 margin_mm 만큼 더 담은 것). empty 는 None 이어도 된다.
    반환 {"samples": [...], "notes": [주의 문구], "path": "빈 판 비교" | "사진 한 장", "ms": 걸린 시간}. 샘플 좌표는 판 mm(사각형 꼭짓점 A = 0, 0)."""
    t0 = time.perf_counter()
    lg = log or _quiet
    mm_px = 1.0 / float(ppmm)
    H, W = cur.shape[:2]
    m = int(round(float(margin_mm) * ppmm))
    valid = np.zeros((H, W), np.uint8)                              # 찾을 곳 = 새겨진 사각형 안(v2 의 판 영역과 같다). 경계 근처 후보는 검출기가 뺀다
    valid[m:H - m, m:W - m] = 255
    area = [[m, m], [W - m, m], [W - m, H - m], [m, H - m]]
    notes = []
    res = None
    path = "사진 한 장"
    if empty is not None and empty.shape == cur.shape:
        try:
            a = refdiff.align(empty, cur, valid)
        except cv2.error as e:
            a = {"ok": False, "msg": str(e).splitlines()[0][:60], "resid": 9.9}
        if a["ok"]:
            ref_w = refdiff.warp_ref_roi(empty, a["M"], (0, 0, W, H))
            res = refdiff.detect_crop(cur, ref_w, valid, mm_px, PARAMS, area, lg)
            path = "빈 판 비교"
            lg("빈 판 사진 정렬 · 이동 %.1f, %.1f px · 잔차 %.2f" % (a["dx"], a["dy"], a["resid"]))
        else:
            notes.append("빈 판 사진과 달라 사진 한 장으로 찾았습니다")
            lg("빈 판 사진 정렬 실패 · %s" % a.get("msg", ""))
    if res is None:
        res = fixed_detect.detect(cur, PARAMS, mm_px, valid, None, area, lg)
    L = float(res.diag.get("chip_long_px") or (fixed_detect.DEFAULTS["chip_mm"] * ppmm)) * mm_px       # 샘플 긴 변(mm)
    out = []
    for sm in res.samples:
        cnt = np.asarray(sm["_cnt"], np.float32).reshape(-1, 1, 2)
        simp = cv2.approxPolyDP(cnt, 0.15 * ppmm, True).reshape(-1, 2)                                 # 0.15 mm 안에서 꼭짓점을 줄인다
        if len(simp) < 3:
            simp = cnt.reshape(-1, 2)
        out.append({
            "plate": [round(float(sm["x_px"]) * mm_px - margin_mm, 2), round(float(sm["y_px"]) * mm_px - margin_mm, 2)],
            "outline": [[round(float(x) * mm_px - margin_mm, 2), round(float(y) * mm_px - margin_mm, 2)] for x, y in simp],
            "w": float(sm["w_mm"]), "h": float(sm["h_mm"]), "area": float(sm["area_mm2"]),
            "source": sm["source"], "weak": sm["source"] == "predicted",
        })
    out = order_rows(out, L)
    if any(s["weak"] for s in out):
        notes.append("불확실한 샘플 %d개를 확인하세요" % sum(1 for s in out if s["weak"]))
    return {"samples": out, "notes": notes, "path": path, "ms": round((time.perf_counter() - t0) * 1000.0)}


def order_rows(samples, long_mm):
    """사진에서 윗줄부터, 줄 안에서는 왼쪽 → 오른쪽. 가운데 y 가 가까운 것끼리 한 줄로 묶는다."""
    rows = []
    for s in sorted(samples, key=lambda q: q["plate"][1]):
        if rows and abs(s["plate"][1] - rows[-1]["y"]) <= ROW_K * long_mm:
            rows[-1]["items"].append(s)
            rows[-1]["y"] = sum(q["plate"][1] for q in rows[-1]["items"]) / len(rows[-1]["items"])
        else:
            rows.append({"y": s["plate"][1], "items": [s]})
    out = []
    for r in rows:
        out.extend(sorted(r["items"], key=lambda q: q["plate"][0]))
    return out


def pin_fits(outline, center, long_dir, long_mm, short_mm, margin_mm):
    """샘플 가운데에 핀(긴 쪽 long_mm × 짧은 쪽 short_mm)을 놓았을 때 여유 margin_mm 을 두고 윤곽 안에 들어가는가.
    outline · center = 판 mm, long_dir = 핀 긴 쪽 방향(판 좌표의 단위 벡터)."""
    poly = np.asarray(outline, np.float32).reshape(-1, 1, 2)
    if len(poly) < 3:
        return False
    ux, uy = long_dir
    n = max(1e-9, (ux * ux + uy * uy) ** 0.5)
    ux, uy = ux / n, uy / n
    vx, vy = -uy, ux
    hl, hs = long_mm / 2.0 + margin_mm, short_mm / 2.0 + margin_mm
    for a, b in ((hl, hs), (hl, -hs), (-hl, hs), (-hl, -hs)):
        x, y = center[0] + a * ux + b * vx, center[1] + a * uy + b * vy
        if cv2.pointPolygonTest(poly, (float(x), float(y)), False) < 0:
            return False
    return True
