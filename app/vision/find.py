"""find.py - 보정한 판 사진에서 샘플 찾기. 검출 방식과 값은 v2 것 그대로(fixed_detect · refdiff), 여기서는 입력 · 출력만 맞춘다.

- 입력: 원본에서 판 영역만 잘라 위에서 본 모습으로 보정한 사진(1 mm = ppmm 픽셀). 빈 판 사진도 같은 변환으로 보정한 것.
  검출기는 안에서 샘플 긴 변이 60 픽셀이 되게 줄여서 찾는다(v2 가 쓰던 작업 해상도 그대로).
- 빈 판 사진 정렬은 두 사진이 실제로 다른 곳(샘플)을 빼고 맞춘다(refdiff._align_at - 로그에 뺀 비율 "다른 곳 14 %").
  그래도 잘 안 맞으면(v2 의 정렬 잔차 기준) 막지 않고 사진 한 장으로 찾는 방식으로 넘어간다(주의 "… · 오검출 주의" - 판 얼룩을 샘플로 잡을 수 있다). 빈 판 사진이 없어도 같다.
- 배치(layout): "free"(기본 · 샘플을 자유롭게 놓는다 - 격자 예측 없음) · "grid"(rows × cols 격자 - 빈 자리를 예측). 검출기의 튜닝 값(fixed_detect.DEFAULTS)은 그대로다.
  예전에는 검출기 기본(layout auto · 3 × 5 · emit_predicted)이 실제로 없는 자리에 "미확인" 샘플을 만들고 판 밖에도 냈다.
- 격자 예측으로만 만든 약한 후보(source = predicted)와 약한 샘플 다시 찾기로 찾은 것(source = local)은 "확인 필요"(weak)로 표시한다(부르는 쪽이 기본으로 빼 둔다).
- 약한 샘플 다시 찾기(faint - 빈 판 비교의 약한 칩 재탐색)는 부르는 쪽이 켤 때만(설정 detect_faint · 기본 끔 - 잡음 수준 얼룩이 샘플로 나왔다).
- 빈 판 정렬이 되면 반환 "shift" = 빈 판 사진 대비 사진 밀림(판 mm · 회전 °) - 기록 · 알림용(좌표는 고치지 않는다).
- 판 사각형 밖(가운데가 0 ~ 한 변 밖)의 샘플은 어떤 배치에서도 내지 않는다.
- 출력(샘플마다, 판 mm): 가운데 · 윤곽 · 가로 세로 · 넓이 · 약함 여부. 순서는 사진에서 윗줄부터 왼쪽 → 오른쪽.
- 핀 놓을 자리(pin_place): 윤곽 안에서 핀 사각형이 가장 넉넉히 들어가는 자리(같으면 가운데에 가까운 곳 - 삼각형 · 사다리꼴은 넓은 쪽으로).
"""

import time

import cv2
import numpy as np

from app.vision import fixed_detect, refdiff

PARAMS = {"fixed": {}}                              # v2 기본값 그대로(fixed_detect.DEFAULTS)
ROW_K = 0.6                                         # 줄 묶기: 가운데 y 가 샘플 긴 변의 이 배 안이면 같은 줄


def _quiet(*_a, **_k):
    pass


def layout_params(layout="free", rows=3, cols=5, faint=False):
    """검출기에 넘길 배치 값(튜닝 값은 DEFAULTS 그대로 · 배치와 약한 칩 재탐색 켜기만 덮는다)."""
    if layout == "grid":
        p = {"fixed": {"layout": "grid", "grid_rows": int(rows), "grid_cols": int(cols), "emit_predicted": True}}
    else:
        p = {"fixed": {"layout": "free", "emit_predicted": False}}
    p["fixed"]["ref_faint_on"] = bool(faint)
    return p


def find_samples(cur, empty, ppmm, margin_mm=0.0, log=None, layout="free", rows=3, cols=5, faint=False):
    """cur · empty = 보정한 판 사진(BGR, 같은 크기, 새겨진 사각형 바깥을 margin_mm 만큼 더 담은 것). empty 는 None 이어도 된다.
    반환 {"samples": [...], "notes": [주의 문구], "path": "빈 판 비교" | "사진 한 장", "ms": 걸린 시간,
    "shift": 빈 판 사진 대비 밀림 {"dx", "dy", "mm"(판 mm), "rot"(°)} | None(정렬 안 됨)}. 샘플 좌표는 판 mm(사각형 꼭짓점 A = 0, 0)."""
    t0 = time.perf_counter()
    lg = log or _quiet
    params = layout_params(layout, rows, cols, faint)
    mm_px = 1.0 / float(ppmm)
    H, W = cur.shape[:2]
    m = int(round(float(margin_mm) * ppmm))
    valid = np.zeros((H, W), np.uint8)                              # 찾을 곳 = 새겨진 사각형 안(v2 의 판 영역과 같다). 경계 근처 후보는 검출기가 뺀다
    valid[m:H - m, m:W - m] = 255
    area = [[m, m], [W - m, m], [W - m, H - m], [m, H - m]]
    notes = []
    res = None
    shift = None
    path = "사진 한 장"
    if empty is not None and empty.shape == cur.shape:
        bright = None
        try:
            empty, bright = match_light(empty, cur, valid)               # 정렬 전에 밝기 · 대비를 맞춘다(문을 열어 빛이 바뀌면 정렬이 실패했다)
            a = refdiff.align(empty, cur, valid)
        except cv2.error as e:
            a = {"ok": False, "msg": str(e).splitlines()[0][:60], "resid": 9.9}
        if a["ok"]:
            ref_w = refdiff.warp_ref_roi(empty, a["M"], (0, 0, W, H))
            res = refdiff.detect_crop(cur, ref_w, valid, mm_px, params, area, lg)
            path = "빈 판 비교"
            lg("빈 판 사진 정렬 · 이동 %.1f, %.1f px · 잔차 %.2f · 다른 곳 %.0f %%" % (a["dx"], a["dy"], a["resid"], 100.0 * float(a.get("excl") or 0.0)))
            shift = {"dx": round(float(a["dx"]) * mm_px, 2), "dy": round(float(a["dy"]) * mm_px, 2),
                     "mm": round(float(np.hypot(a["dx"], a["dy"])) * mm_px, 2), "rot": round(float(a["theta_deg"]), 2)}
        else:
            why = "밝기 차이 %s %% · 잔차 %s · 이동 %s px" % ("—" if bright is None else "%.0f" % bright, "%.2f" % a["resid"] if a.get("resid") is not None else "—",
                                                     "%.0f" % float(np.hypot(a.get("dx") or 0.0, a.get("dy") or 0.0)) if "dx" in a else "—")
            notes.append("빈 판 사진 불일치 · 사진 한 장으로 검출 · 오검출 주의 · %s" % why)
            lg("빈 판 사진 정렬 실패 · %s · %s" % (a.get("msg", ""), why))
    if res is None:
        res = fixed_detect.detect(cur, params, mm_px, valid, None, area, lg)
    L = float(res.diag.get("chip_long_px") or (fixed_detect.DEFAULTS["chip_mm"] * ppmm)) * mm_px       # 샘플 긴 변(mm)
    out = []
    size_x, size_y = (W - 2 * m) * mm_px, (H - 2 * m) * mm_px          # 판 사각형 한 변(mm)
    outside = 0
    for sm in res.samples:
        px, py = float(sm["x_px"]) * mm_px - margin_mm, float(sm["y_px"]) * mm_px - margin_mm
        if not (0.0 <= px <= size_x and 0.0 <= py <= size_y):
            outside += 1                                                # 판 사각형 밖: 내지 않는다
            continue
        cnt = np.asarray(sm["_cnt"], np.float32).reshape(-1, 1, 2)
        poly = cv2.approxPolyDP(cnt, 0.15 * ppmm, True).reshape(-1, 2)                                 # 0.15 mm 안에서 꼭짓점을 줄인다
        if len(poly) < 3:
            poly = cnt.reshape(-1, 2)
        out.append({
            "plate": [round(float(sm["x_px"]) * mm_px - margin_mm, 2), round(float(sm["y_px"]) * mm_px - margin_mm, 2)],
            "outline": [[round(float(x) * mm_px - margin_mm, 2), round(float(y) * mm_px - margin_mm, 2)] for x, y in poly],
            "w": float(sm["w_mm"]), "h": float(sm["h_mm"]), "area": float(sm["area_mm2"]),
            "source": sm["source"], "weak": sm["source"] in ("predicted", "local"),
        })
    out = order_rows(out, L)
    if outside:
        lg("판 밖 후보 %d개 제외" % outside)
    if any(s["weak"] for s in out):
        notes.append("미확인 샘플 %d개 · 확인 필요" % sum(1 for s in out if s["weak"]))
    return {"samples": out, "notes": notes, "path": path, "ms": round((time.perf_counter() - t0) * 1000.0), "shift": shift}


def match_light(ref, cur, valid):
    """빈 판 사진(ref)의 밝기 · 대비를 지금 사진(cur)에 맞춘다: 찾을 곳(valid) 안에서 채널마다 평균 · 표준편차를 같게.
    반환 (맞춘 ref, 맞추기 전 밝기 차이 % = |평균 차이| ÷ ref 평균). 검출기의 값(v2)은 그대로 - 입력만 맞춘다."""
    m = valid > 0
    out = ref.astype(np.float32).copy()
    c32 = cur.astype(np.float32)
    mr_all, mc_all = float(out[m].mean()), float(c32[m].mean())
    for ch in range(out.shape[2] if out.ndim == 3 else 1):
        a = out[:, :, ch] if out.ndim == 3 else out
        b = c32[:, :, ch] if c32.ndim == 3 else c32
        ma, sa = float(a[m].mean()), float(a[m].std())
        mb, sb = float(b[m].mean()), float(b[m].std())
        a[:] = (a - ma) * (sb / sa if sa > 1e-6 else 1.0) + mb
    diff = abs(mc_all - mr_all) / max(1e-6, mr_all) * 100.0
    return np.clip(out, 0, 255).astype(np.uint8), diff


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


PIN_RES_MM = 0.05                                   # 핀 놓을 자리 격자(mm)


def pin_place(outline, center, long_dir, long_mm, short_mm, margin_mm):
    """핀(긴 쪽 long_mm × 짧은 쪽 short_mm · 긴 쪽 = long_dir 판 좌표 단위 벡터)을 놓을 자리. outline · center = 판 mm.
    다 들어가는 자리가 있으면 윤곽 경계까지 가장 넉넉한 곳(clear 최대 - PIN_RES_MM 안은 같게 봄), 없으면 가장 많이 겹치는 곳 · 후보 중 center 에 가장 가까운 점.
    반환 {"at": [x, y] 판 mm, "fits": 다 들어가고 clear ≥ margin_mm, "inside": 겹친 비율 최대(0~1), "clear": 다 들어갈 때 가장 넉넉한 거리(mm) · 아니면 0}."""
    cx, cy = float(center[0]), float(center[1])
    pts = np.asarray(outline, np.float64).reshape(-1, 2)
    if len(pts) < 3:
        return {"at": [cx, cy], "fits": False, "inside": 0.0, "clear": 0.0}
    ux, uy = float(long_dir[0]), float(long_dir[1])
    n = max(1e-9, (ux * ux + uy * uy) ** 0.5)
    ux, uy = ux / n, uy / n
    vx, vy = -uy, ux
    rel = pts - (cx, cy)
    q = np.stack([rel[:, 0] * ux + rel[:, 1] * uy, rel[:, 0] * vx + rel[:, 1] * vy], axis=1)      # 핀 좌표(x = 긴 쪽 · y = 그 수직)
    res = PIN_RES_MM
    pad = long_mm / 2.0 + 1.0
    x0, y0 = float(q[:, 0].min()) - pad, float(q[:, 1].min()) - pad
    W = int(np.ceil((float(q[:, 0].max()) + pad - x0) / res)) + 1
    H = int(np.ceil((float(q[:, 1].max()) + pad - y0) / res)) + 1
    img = np.zeros((H, W), np.uint8)
    poly = np.round((q - (x0, y0)) / res * 16.0).astype(np.int32).reshape(-1, 1, 2)            # 1/16 화소 정밀도(shift 4)
    cv2.fillPoly(img, [poly], 1, lineType=cv2.LINE_8, shift=4)
    kw = int(round(long_mm / res)) | 1
    kh = int(round(short_mm / res)) | 1
    inside = cv2.boxFilter(img.astype(np.float32), -1, (kw, kh), normalize=True, borderType=cv2.BORDER_CONSTANT)
    dist = cv2.distanceTransform(img, cv2.DIST_L2, 5) * res
    clear = cv2.erode(dist, np.ones((kh, kw), np.uint8), borderType=cv2.BORDER_CONSTANT, borderValue=0)
    imax, cmax = float(inside.max()), float(clear.max())
    full = imax >= 0.999 and cmax > 0
    cand = (clear >= cmax - res) if full else (inside >= imax - 0.005)
    ys, xs = np.nonzero(cand)
    gx, gy = x0 + xs * res, y0 + ys * res                                                       # 격자점의 핀 좌표(center = 0, 0)
    k = int(np.argmin(gx * gx + gy * gy))
    a, b = float(gx[k]), float(gy[k])
    return {"at": [cx + a * ux + b * vx, cy + a * uy + b * vy], "fits": bool(full and cmax >= margin_mm),
            "inside": imax, "clear": cmax if full else 0.0}
