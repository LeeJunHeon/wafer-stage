"""refdiff.py - 빈 판 기준 차이 검출 · 촬영마다 카메라 이동 보정(정렬).

기준 사진(빈 판, data/plate_ref.png)을 현재 사진에 맞추고(고역 통과 → 위상 상관 초기값 → ECC 유클리드: 이동 + 회전) 두 사진의 차이로 칩을 찾는다.
판의 긁힘 · 얼룩 · 조명 기울기는 두 사진에 똑같이 있어 차이에서 사라지므로 단일 사진 경로(core/fixed_detect.py)보다 반투명 칩에 강하다.

  align(ref, cur, mask)           → {"M": 2x3 기준→현재, "dx", "dy", "theta_deg", "resid", "ok"}
  diff_d(ref_w, cur, L_px, ...)   → d 영상(채널별 강건 z 의 √Σz², 조명 기울기 제거)
  detect(...)                      → FixedResult 호환(샘플 dict: detect · fixed_detect 와 같은 키 + source)

좌표계 규칙: 감지 영역 · 제외 영역 · 판 보정은 '기준 촬영 시점의 사진 좌표'(기준 좌표)로 정의돼 있다고 보고, 현재 사진에서는 정렬 변환 M 으로 옮겨 쓴다.
샘플 사진 좌표는 현재 사진(표시용)과 기준 좌표(mm 변환용) 둘 다 다룬다(plate.sense_fixed 가 변환).
"""

import math
import time

import cv2
import numpy as np

from core import fixed_detect as fd

ALIGN_SCALE = 0.25            # 정렬은 1/4 해상도로
HP_SIGMA = 10.0               # 고역 통과(원본 - 가우시안 σ10) 뒤 σ1.5 로 다시 흐림
FAINT_MIN_RATIO = 0.6         # 약한 칩 면적 하한(예상 칩 면적 배)
RING_RATIO = 1.0              # 약한 칩: 테두리 평균 d / 내부 평균 d 가 이 이상
RESID_FAIL = 2.5              # 정렬 잔차가 이 이상이면 정렬 실패(경고 기준은 fixed.ref_check_resid)
MAX_SHIFT_FRAC = 0.12         # 위상 상관 초기값이 프레임의 이 비율보다 크면 정렬 실패로 본다


def _hp(gray):
    g = gray.astype(np.float32)
    return cv2.GaussianBlur(g - cv2.GaussianBlur(g, (0, 0), HP_SIGMA), (0, 0), 1.5)


def _align_at(ref, cur, mask, scale):
    """기준(ref) → 현재(cur) 유클리드 변환 M(2x3, 원해상도 px). mask = 정렬에 쓸 곳(255) - 제외 영역 · 칩이 있을 법한 곳은 뺀다.

    반환: {"ok", "M", "dx", "dy", "theta_deg", "resid", "init", "msg"}. 정렬 실패(상관이 약하거나 변위가 너무 큼)면 ok False.
    resid = 정렬 뒤 고역 영상 차이의 표준편차 / 기준 고역 영상 표준편차 (0 에 가까울수록 잘 맞음).
    """
    h, w = cur.shape[:2]
    a = cv2.cvtColor(ref, cv2.COLOR_BGR2GRAY)
    b = cv2.cvtColor(cur, cv2.COLOR_BGR2GRAY)
    sa = cv2.resize(a, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    sb = cv2.resize(b, None, fx=scale, fy=scale, interpolation=cv2.INTER_AREA)
    ha, hb = _hp(sa), _hp(sb)
    sm = None
    if mask is not None:
        sm = cv2.resize(mask, (sa.shape[1], sa.shape[0]), interpolation=cv2.INTER_NEAREST)
    win = cv2.createHanningWindow((ha.shape[1], ha.shape[0]), cv2.CV_32F)
    (dx0, dy0), cc = cv2.phaseCorrelate(ha, hb, win)          # ha 를 hb 에 맞추려면 (dx0, dy0) 만큼 옮긴다
    out = {"ok": False, "M": np.array([[1.0, 0, 0], [0, 1.0, 0]]), "dx": 0.0, "dy": 0.0, "theta_deg": 0.0,
           "resid": 9.9, "init": [dx0 / scale, dy0 / scale], "msg": ""}
    if abs(dx0) > MAX_SHIFT_FRAC * ha.shape[1] or abs(dy0) > MAX_SHIFT_FRAC * ha.shape[0]:
        out["msg"] = "위상 상관 이동이 너무 큼 (%.0f, %.0f px)" % (dx0 / scale, dy0 / scale)
        return out
    warp = np.array([[1.0, 0, dx0], [0, 1.0, dy0]], np.float32)
    try:
        crit = (cv2.TERM_CRITERIA_EPS | cv2.TERM_CRITERIA_COUNT, 80, 1e-6)
        w2 = warp.copy()
        _cc, w2 = cv2.findTransformECC(ha, hb, w2, cv2.MOTION_EUCLIDEAN, crit,
                                       None if sm is None else sm, 5)
        warp = w2
    except cv2.error as e:
        out["msg"] = "ECC 실패: %s" % str(e).splitlines()[0][:60]
        # 위상 상관 초기값만으로 계속(ECC 가 중간에 망가뜨린 변환은 쓰지 않는다)
    Ms = np.asarray(warp, np.float64)
    M = Ms.copy()
    M[:, 2] = Ms[:, 2] / scale                                  # 이동만 원해상도로(회전 · 평행이동은 닮음 변환이라 선형부는 그대로)
    th = math.degrees(math.atan2(M[1, 0], M[0, 0]))
    # 잔차: 정렬한 기준 고역 영상과 현재 고역 영상의 차이
    wa = cv2.warpAffine(ha, Ms.astype(np.float32), (hb.shape[1], hb.shape[0]), flags=cv2.INTER_LINEAR + cv2.WARP_INVERSE_MAP)
    m = np.ones_like(hb, bool) if sm is None else (sm > 0)
    m[:8, :] = m[-8:, :] = False
    m[:, :8] = m[:, -8:] = False
    sd = float(np.std(hb[m])) + 1e-6
    resid = float(np.std((wa - hb)[m])) / sd
    out.update({"M": M, "dx": float(M[0, 2]), "dy": float(M[1, 2]), "theta_deg": th, "resid": resid,
                "ok": bool(resid < RESID_FAIL and np.isfinite(resid)), "msg": out["msg"]})
    if not out["ok"] and not out["msg"]:
        out["msg"] = "정렬 잔차가 큼 (%.2f)" % resid
    return out


def align(ref, cur, mask=None, scales=(ALIGN_SCALE, 0.5)):
    """기준 → 현재 정렬. 1/4 해상도로 먼저 하고, 실패하거나 잔차가 크면 1/2 해상도로 다시(가는 긁힘이 많은 판) - 잔차가 더 작은 쪽을 쓴다."""
    best = None
    for sc in scales:
        a = _align_at(ref, cur, mask, sc)
        if best is None or (a["ok"], not a["msg"], -a["resid"]) > (best["ok"], not best["msg"], -best["resid"]):
            best = a
        if best["ok"] and best["resid"] < 1.0 and not best["msg"]:
            break
    return best


def warp_ref_roi(ref, M, roi):
    """기준을 현재 사진 좌표 ROI (x0, y0, x1, y1) 에 맞춰 잘라 낸다(M = 기준→현재)."""
    x0, y0, x1, y1 = roi
    Mr = np.asarray(M, np.float64).copy()
    Mr[0, 2] -= x0
    Mr[1, 2] -= y0
    return cv2.warpAffine(ref, Mr, (x1 - x0, y1 - y0), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REPLICATE)


def _odd(n):
    n = int(round(n))
    return max(3, n | 1)


def diff_d(ref_w, cur, valid, L_px, sigma=1.5, k_chips=3.0):
    """기준(정렬됨)과 현재의 Lab 채널별 차이 → 완만한 성분(medianBlur, 칩 길이 x k) 제거 → 강건 z → d = √Σz². 반환 (d, 채널 σ)."""
    a = cv2.GaussianBlur(cv2.cvtColor(ref_w, cv2.COLOR_BGR2LAB).astype(np.float32), (0, 0), sigma)
    b = cv2.GaussianBlur(cv2.cvtColor(cur, cv2.COLOR_BGR2LAB).astype(np.float32), (0, 0), sigma)
    diff = b - a
    vm = valid > 0
    k = min(_odd(k_chips * L_px), _odd(min(valid.shape[:2]) - 2))
    d2 = np.zeros(valid.shape[:2], np.float32)
    sig = []
    for c in range(3):
        ch = diff[:, :, c]
        base = cv2.medianBlur(np.clip(np.rint(ch + 128.0), 0, 255).astype(np.uint8), k).astype(np.float32) - 128.0
        r = ch - base
        sel = r[vm] if vm.any() else r.ravel()
        med = float(np.median(sel))
        mad = float(np.median(np.abs(sel - med))) * 1.4826
        s = max(fd.MAD_FLOOR, mad)
        z = (r - med) / s
        d2 += z * z
        sig.append(round(s, 2))
    d = np.sqrt(d2)
    d[~vm] = 0.0
    return d, sig


def _fill_holes(m):
    """바깥에서 flood fill 한 뒤 반전 = 내부 구멍. 닫힌 테두리를 면으로 메운다."""
    h, w = m.shape
    pad = cv2.copyMakeBorder(m, 1, 1, 1, 1, cv2.BORDER_CONSTANT, value=0)
    ff = pad.copy()
    cv2.floodFill(ff, np.zeros((h + 4, w + 4), np.uint8), (0, 0), 255)
    holes = cv2.bitwise_not(ff)[1:-1, 1:-1]
    return cv2.bitwise_or(m, holes)


def _components(mask, d, exp_area, cfg, offset=(0, 0)):
    """모양 필터를 통과한 후보와 칩 크기 범위 밖 큰 덩어리 수. 후보 dict 는 fixed_detect.components 와 같은 키."""
    ko, kc = int(cfg["ref_open"]), int(cfg["ref_close"])
    m = mask
    if str(cfg.get("ref_order", "close_first")) == "close_first":
        # 반투명 칩은 테두리만 차이로 보인다 - 얇은 테두리가 open 에 지워지기 전에 닫고 구멍을 메운 뒤 연다
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((kc, kc), np.uint8))
        m = _fill_holes(m)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((ko, ko), np.uint8))
    else:
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((ko, ko), np.uint8))
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((kc, kc), np.uint8))
        m = _fill_holes(m)
    n, lab, stats, cent = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
    lo, hi = [float(v) for v in cfg["ref_area_ratio"]]
    out, big = [], 0
    for i in range(1, n):
        area = float(stats[i, cv2.CC_STAT_AREA])
        if area > hi * exp_area:
            big += 1
            continue
        if area < lo * exp_area:
            continue
        bx, by, bw, bh = (int(stats[i, k]) for k in (cv2.CC_STAT_LEFT, cv2.CC_STAT_TOP, cv2.CC_STAT_WIDTH, cv2.CC_STAT_HEIGHT))
        sub = (lab[by:by + bh, bx:bx + bw] == i).astype(np.uint8)
        cs, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
        if not cs:
            continue
        cnt = max(cs, key=cv2.contourArea)
        pts = cnt.reshape(-1, 2) + [bx, by]
        hull = cv2.convexHull(pts.astype(np.int32))
        ha = cv2.contourArea(hull)
        if ha < 1:
            continue
        sol = area / ha
        (rcx, rcy), (rw, rh), ang = cv2.minAreaRect(hull)
        long_, short_ = max(rw, rh), max(1e-6, min(rw, rh))
        ok = sol >= float(cfg["ref_min_solidity"]) and long_ / short_ <= float(cfg["ref_max_aspect"])
        ys, xs = np.nonzero(sub)
        out.append({"cx": float(cent[i][0]), "cy": float(cent[i][1]), "area": area, "long": long_, "short": short_,
                    "sol": sol, "fill": area / max(1e-6, rw * rh), "asp": long_ / short_, "ok": ok,
                    "strength": float(np.median(d[ys + by, xs + bx])), "hull": hull.reshape(-1, 2), "ang": float(ang)})
    return out, big


def drop_near_edge(cands, valid, cfg, L_work):
    """감지 영역 경계(valid 의 바깥)에서 ref_edge_k x 칩 길이 안에 껍질이 닿은 후보를 뺀다. 반환: (남은 후보, 뺀 개수)."""
    dist = cv2.distanceTransform((valid > 0).astype(np.uint8), cv2.DIST_L2, 3)
    band = float(cfg["ref_edge_k"]) * L_work
    keep, dropped = [], 0
    for c in cands:
        hh = c["hull"]
        dmin = float(min(dist[min(max(int(y), 0), dist.shape[0] - 1), min(max(int(x), 0), dist.shape[1] - 1)] for x, y in hh))
        if dmin < band:
            dropped += 1
        else:
            keep.append(c)
    return keep, dropped


def faint_chips(d_soft, d_hard, exp_area, taken, cfg, L_work):
    """약한 칩 재탐색: σ2 로 흐린 d 를 낮은 기준으로 다시 이진화해(ref_faint 부터 1.0 위까지) 닫힌 테두리를 면으로 메운 덩어리 중 칩 크기 범위인 것을 찾는다.
    반투명 칩은 내부 차이가 없고 테두리만 보이므로 테두리가 내부보다 d 가 높아야 한다(ring/inner >= RING_RATIO). 이미 찾은 후보(taken) 근처는 건너뛴다."""
    lo, hi = [float(v) for v in cfg["ref_area_ratio"]]
    lo = max(lo, FAINT_MIN_RATIO)                          # 약한 칩은 작은 얼룩과 구분이 어려워 더 큰 것만
    base = float(cfg["ref_faint"])
    out = []
    centers = list(taken)
    for th in (base + 1.0, base + 0.5, base):
        m = (d_soft > th).astype(np.uint8) * 255
        m = cv2.morphologyEx(m, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        m = _fill_holes(m)
        m = cv2.morphologyEx(m, cv2.MORPH_OPEN, np.ones((5, 5), np.uint8))
        n, lab, stats, cent = cv2.connectedComponentsWithStats((m > 0).astype(np.uint8), 8)
        for i in range(1, n):
            area = float(stats[i, cv2.CC_STAT_AREA])
            if not (lo * exp_area <= area <= hi * exp_area):
                continue
            bw, bh = int(stats[i, cv2.CC_STAT_WIDTH]), int(stats[i, cv2.CC_STAT_HEIGHT])
            if max(bw, bh) > 2.2 * L_work or max(bw, bh) < 0.4 * L_work:
                continue
            bx, by = int(stats[i, cv2.CC_STAT_LEFT]), int(stats[i, cv2.CC_STAT_TOP])
            sub = (lab[by:by + bh, bx:bx + bw] == i).astype(np.uint8)
            cs, _ = cv2.findContours(sub, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_NONE)
            if not cs:
                continue
            cnt = max(cs, key=cv2.contourArea)
            hull = cv2.convexHull(cnt)
            ha = cv2.contourArea(hull)
            if ha < 1 or area / ha < float(cfg["ref_min_solidity"]):
                continue
            (rcx, rcy), (rw, rh), ang = cv2.minAreaRect(hull)
            long_, short_ = max(rw, rh), max(1e-6, min(rw, rh))
            if long_ / short_ > float(cfg["ref_max_aspect"]):
                continue
            cx, cy = float(cent[i][0]), float(cent[i][1])
            if any(math.hypot(cx - t[0], cy - t[1]) < 0.6 * L_work for t in centers):
                continue
            er = cv2.erode(sub, np.ones((9, 9), np.uint8))
            ring, inner = (sub > 0) & (er == 0), er > 0
            if not inner.any() or not ring.any():
                continue
            dd = d_soft[by:by + bh, bx:bx + bw]
            if float(dd[ring].mean()) < float(dd[inner].mean()) * RING_RATIO:
                continue
            pts = hull.reshape(-1, 2) + [bx, by]
            out.append({"cx": cx, "cy": cy, "area": area, "long": long_, "short": short_, "sol": area / ha,
                        "fill": area / max(1e-6, rw * rh), "asp": long_ / short_, "ok": True,
                        "strength": float(dd[ring].mean()), "hull": pts, "ang": float(ang)})
            centers.append((cx, cy))
    return out


def detect_crop(crop, ref_crop, valid, mm_px, params, area_poly=None, log=print):
    """감지 영역 crop(현재) 과 정렬한 기준 crop 의 차이로 칩을 찾는다 → FixedResult(샘플 좌표 = crop 원해상도 px, fixed_detect 와 같은 dict + source).

    배치(layout auto|grid)는 fixed_detect.decide_grid 가 판단한다 - 격자가 맞으면 빈 칸을 예측(weak)하고, 격자에 안 맞는 후보도 샘플로 유지한다.
    """
    t_all = time.perf_counter()
    cfg = fd.cfg_of(params)
    H0, W0 = crop.shape[:2]
    res = fd.FixedResult()
    res.wafer = fd._legacy.Wafer(found=False, surface="plate", mm_per_px=float(mm_px), scale_source="settings")
    L_orig = float(cfg["chip_mm"]) / max(mm_px, 1e-9)
    Lw = float(cfg["work_long_px"])
    s = min(1.0, Lw / L_orig)
    if s < 0.999:
        sz = (max(32, int(round(W0 * s))), max(32, int(round(H0 * s))))
        cs = cv2.resize(crop, sz, interpolation=cv2.INTER_AREA)
        rs = cv2.resize(ref_crop, sz, interpolation=cv2.INTER_AREA)
        vs = cv2.resize(valid, sz, interpolation=cv2.INTER_NEAREST)
        s = sz[0] / float(W0)
    else:
        cs, rs, vs, s = crop, ref_crop, valid, 1.0
    Lw_real = L_orig * s
    t0 = time.perf_counter()
    d, sig = diff_d(rs, cs, vs, Lw_real, k_chips=3.0)
    t_d = time.perf_counter() - t0
    exp_area = 0.35 * Lw_real * Lw_real
    mask = (d > float(cfg["ref_seed"])).astype(np.uint8) * 255
    cands, big = _components(mask, d, exp_area, cfg)
    good, n_edge = drop_near_edge([c for c in cands if c["ok"]], vs, cfg, Lw_real)
    soft = cv2.GaussianBlur(d, (0, 0), 2.0)
    faint, _n2 = drop_near_edge(faint_chips(soft, d, exp_area, [(c["cx"], c["cy"]) for c in good], cfg, Lw_real), vs, cfg, Lw_real)
    log("기준 차이  : 채널 σ %s · 후보 %d · 모양 통과 %d · 경계 근처 %d · 약한 칩 %d · 큰 덩어리 %d" % (sig, len(cands), len(good), n_edge, len(faint), big))
    warnings = []
    ap = None if area_poly is None else (np.asarray(area_poly, np.float64) * s).astype(np.float32).reshape(-1, 1, 2)
    allc = [(c, "detected", False) for c in good] + [(c, "local", True) for c in faint]
    grid, why = fd.decide_grid([(c["cx"], c["cy"]) for c, _s, _w in allc], cfg, ap)
    if str(cfg["layout"]) == "grid" and grid is None and why:
        warnings.append(why)
    samples = []
    n_pred = 0
    if grid is None:
        log("배치      : %s" % (why or "자동 판단 → 자유 배치"))
        for c, src, weak in allc:
            samples.append(fd._make_sample(c, s, mm_px, src, weak, W0, H0))
    else:
        log("격자      : %dx%d · 인라이어 %d/%d · 간격 %.1f px(원본)" % (grid["ni"], grid["nj"], grid["n_inl"], len(allc), grid["spacing"] / s))
        tol = 0.5 * grid["spacing"]
        taken = {}
        extra = []
        for c, src, weak in allc:
            best = None
            for key, p in grid["slots"].items():
                dd = math.hypot(c["cx"] - p[0], c["cy"] - p[1])
                if dd < tol and (best is None or dd < best[0]):
                    best = (dd, key)
            if best is not None and (best[1] not in taken or c["strength"] > taken[best[1]][0]["strength"]):
                if best[1] in taken:
                    extra.append(taken[best[1]])
                taken[best[1]] = (c, src, weak)
            else:
                extra.append((c, src, weak))
        for key in sorted(grid["slots"], key=lambda kk: (kk[1], kk[0])):
            if key in taken:
                c, src, weak = taken[key]
                sm = fd._make_sample(c, s, mm_px, src, weak, W0, H0)
            elif bool(cfg["emit_predicted"]):
                n_pred += 1
                sm = fd._predicted_sample(grid["slots"][key], grid["u"], Lw_real, exp_area, s, mm_px, W0, H0)
            else:
                continue
            sm["_slot"] = key
            samples.append(sm)
        for c, src, weak in extra:
            samples.append(fd._make_sample(c, s, mm_px, src, weak, W0, H0))
        if extra:
            warnings.append("격자 밖 후보 %d개 유지 · 확인 필요" % len(extra))
        if n_pred:
            warnings.append("예측 %d (확인 필요)" % n_pred)
    samples.sort(key=lambda sm: (round(sm["y_px"] / max(L_orig, 1.0)), sm["x_px"]))
    for i, sm in enumerate(samples, 1):
        sm["no"] = i
    res.samples, res.warnings = samples, warnings
    cnt = {k: sum(1 for x in samples if x["source"] == k) for k in ("detected", "local", "predicted")}
    res.detect_ms = (time.perf_counter() - t_all) * 1000.0
    res.diag = {"chip_long_px": round(L_orig, 1), "scale": round(s, 4), "sources": cnt, "big_blobs": big, "sigma": sig,
                "grid": None if grid is None else {"inliers": grid["n_inl"], "n_good": len(allc), "spacing_px": round(grid["spacing"] / s, 1),
                                                    "uv": [(grid["u"] / s).tolist(), (grid["v"] / s).tolist()]},
                "time_ms": {"diff": round(t_d * 1000.0, 1)}, "path": "ref"}
    log("출처      : 검출 %d · 보완 %d · 예측 %d · 시간 %.0f ms (차이 %.0f)" % (cnt["detected"], cnt["local"], cnt["predicted"], res.detect_ms, t_d * 1000.0))
    return res
