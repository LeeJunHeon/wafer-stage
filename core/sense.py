"""sense.py - 한 장 촬영 사진에서 칩을 찾는 흐름. 정렬(core/align) → 판 영역 crop → 검출(refdiff | fixed_detect) → 샘플 좌표(촬영 · 기준 두 프레임).

규칙
- 판 영역 · 제외 영역은 기준 프레임으로 저장돼 있고, 정렬 A 로 촬영 프레임에 옮겨 쓴다(frames.Align 한곳에서만 변환).
- 정렬이 실패해도 검출은 한다(단일 사진 경로). 그때 coords_ok = False · 좌표(기준 프레임)는 만들지 않는다.
- 빈 판 사진이 노출 · 화이트밸런스가 달라 차이 검출에 못 써도(use_diff = False) 기하 정렬에는 쓴다 - 그때 검출은 단일 경로.
- 샘플의 x_px · y_px = 촬영 프레임 px, ref_u · ref_v = 기준 프레임 px (정렬이 있을 때만).
"""

import time

import numpy as np

from core import align as align_mod
from core import fixed_detect, frames, geom, refdiff


def detect_shot(cur, area_ref, exclude_ref, params, mm_px, ref=None, ref_id="", use_diff=True, log=print):
    cfg = fixed_detect.cfg_of(params)
    area_ref = np.asarray(area_ref, np.float64)
    out = {"path": "single", "A": None, "cam": None, "coords_ok": True, "warnings": [], "fail": None, "frame": frames.SHOT}
    A, ar = None, None
    if ref is not None:
        t0 = time.perf_counter()
        ar = align_mod.align(ref, cur, area_ref, ref_id=ref_id)
        A = ar["A"]
        c_ref = area_ref.mean(axis=0)
        shift_px = A.shift_px(c_ref)
        shift_mm = shift_px * mm_px
        cam = {"has_ref": True, "ok": bool(ar["ok"]), "model": A.model, "M": A.M.tolist(), "theta_deg": round(ar["theta_deg"], 3),
               "shift_px": round(shift_px, 1), "shift_mm": round(shift_mm, 2), "resid": round(float(ar["resid"]), 3),
               "msg": ar.get("msg", ""), "time_ms": round((time.perf_counter() - t0) * 1000.0), "ref_id": ref_id}
        out["cam"] = cam
        log("카메라 이동 : %.1f px (%.2f mm) · 회전 %.3f° · 잔차 %.2f · %s · %d ms"
            % (shift_px, shift_mm, ar["theta_deg"], ar["resid"], A.model, cam["time_ms"]))
        if not ar["ok"]:
            out["coords_ok"] = False
            out["warnings"].append("카메라 위치 확인 필요 · 정렬 실패 · %s" % ar["msg"])
            A = None
        elif shift_mm > float(cfg["cam_shift_max_mm"]):
            out["coords_ok"] = False
            out["fail"] = "카메라 이동 큼 · %.1f mm" % shift_mm
            out["warnings"].append("카메라 위치 확인 필요 · 이동 %.1f mm (한도 %g mm)" % (shift_mm, float(cfg["cam_shift_max_mm"])))
        else:
            if shift_mm > float(cfg["cam_shift_warn_mm"]):
                out["warnings"].append("카메라 이동 %.1f mm · 보정함" % shift_mm)
            if ar.get("msg"):
                out["warnings"].append(ar["msg"])
    out["A"] = A
    if A is not None:
        area_cur = A.to_cur(area_ref)
        exclude_cur = [A.to_cur(p).tolist() for p in (exclude_ref or [])]
    else:
        area_cur, exclude_cur = area_ref, list(exclude_ref or [])
    crop, valid, (u0, v0), quad_l = geom.prep_crop(cur, area_cur, exclude_cur)
    ch, cw = crop.shape[:2]
    if A is not None and ref is not None and use_diff and bool(cfg.get("use_ref", True)):
        ref_crop = A.warp_ref_roi(ref, (u0, v0, u0 + cw, v0 + ch))
        det = refdiff.detect_crop(crop, ref_crop, valid, mm_px, params, quad_l, log)
        out["path"] = "ref"
        if det.diag.get("big_blobs", 0) > int(cfg["ref_check_blobs"]):
            out["warnings"].append("빈 판 사진 다시 촬영 권장 · 큰 차이 덩어리 %d개" % det.diag["big_blobs"])
        if ar["resid"] > float(cfg["ref_check_resid"]):
            out["warnings"].append("빈 판 사진 다시 촬영 권장 · 정렬 잔차 %.2f" % ar["resid"])
    else:
        det = fixed_detect.detect(crop, params, mm_px, valid, ref=None, area_poly=quad_l, log=log)
        if ref is not None and out["path"] == "single" and A is not None:
            out["warnings"].append("빈 판 사진 차이 검출 못 씀 · 단일 사진 검출")
    det.wafer.mm_per_px = mm_px
    inside = []
    for x in det.samples:
        cx, cy = int(round(x["x_px"])), int(round(x["y_px"]))
        if 0 <= cx < cw and 0 <= cy < ch and valid[cy, cx] > 0:
            inside.append(x)
    det.samples = inside
    for x in det.samples:
        x["x_px"] = round(float(x["x_px"]) + u0, 1)
        x["y_px"] = round(float(x["y_px"]) + v0, 1)
        if x.get("vertices_px"):
            x["vertices_px"] = [[int(a) + u0, int(b) + v0] for a, b in x["vertices_px"]]
        if A is not None:
            ru, rv = A.to_ref([[x["x_px"], x["y_px"]]])[0]
            x["ref_u"], x["ref_v"] = round(float(ru), 1), round(float(rv), 1)
        elif out["coords_ok"] and ref is None:
            x["ref_u"], x["ref_v"] = x["x_px"], x["y_px"]           # 기준 사진이 없으면 촬영 프레임을 그대로 쓴다(보정이 기준에 묶여 있지 않을 때만)
    out.update(det=det, crop=crop, valid=valid, off=(u0, v0), quad_l=quad_l, area_cur=np.asarray(area_cur).tolist(),
               exclude_cur=exclude_cur, mm_px=mm_px)
    return out
