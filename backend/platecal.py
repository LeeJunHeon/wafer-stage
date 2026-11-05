"""platecal.py - 고정 보정(calib.mode = fixed)의 감지 영역 · 판 보정 명령.

판 마커 없이 쓰는 방식이다(core/plate.py 가 계산, 여기는 서버 상태·명령).
  감지 영역: 촬영본 픽셀 사각형 한 개. 보정 창에서 네 모서리를 클릭 → [적용].
  판 보정:   기준 포인터를 판 위 표시점에 맞춰 [위치 기록](스테이지 XY) + 촬영본에서
             같은 점 클릭(u, v) → 점 4개 이상 → 호모그래피 최소제곱 → [적용].
저장: data/plate_calib.json (카메라 번호 · 해상도 포함). 카메라 번호나 해상도가 저장 때와
다르면 무효 - 촬영은 하되 좌표를 만들지 않고 경보 배너를 띄운다.
"""

import math
import time

import numpy as np

from core import paths, plate
from core import stage as stage_mod

import engine
import logger
import vision
from connection import push_ack, push_log, push_state
from state import state


def _cam_index():
    return int(state.get("camera_index") or 0)


def _req_size():
    """설정에서 요청한 해상도(헤드 보정 유효성과 같은 기준). 실제 프레임 크기는 촬영 때 다시 본다."""
    return (int(state.get("width") or 0), int(state.get("height") or 0))


def refresh_state():
    """state.plate(요약 · 유효성)와 경보를 다시 계산한다. 설정 저장 · 시작 · 적용 뒤에 부른다."""
    mode = str(state.get("calib.mode") or "fixed")
    d = plate.load()
    ok, why = plate.validity(d, _cam_index(), _req_size())
    keep_shot = (state.plate or {}).get("shot")
    area_ok, area_why = plate.area_validity(d, _cam_index(), _req_size())
    state.plate = {"mode": mode, "valid": bool(ok), "area_ok": bool(area_ok), "reason": why, "calib": _calib_summary(d),
                   "data": plate.summary(d), "shot": keep_shot, "ref": _ref_state(d),
                   "size_warn": plate.area_size_warning(d, state.get("calib.area_size_mm")) if d else ""}
    if mode != "fixed" or ok or area_ok:
        # 감지 영역만 유효하면 검출만 된다 - 경보가 아니라 주의 안내(칩 · 경고줄)로 알린다
        state.set_alarm("판 보정", "")
    elif area_why.startswith("감지 영역 없음"):
        state.set_alarm("판 보정", "감지 영역 없음 · 보정 → 감지 영역 지정")
    else:
        state.set_alarm("판 보정", "판 보정 무효 · 다시 보정")
    engine._update_pointer_px()
    if ok:
        engine.migrate_tip_angles()
    engine._refresh_tip_now()
    return ok


def _calib_summary(d):
    """보정 상태 요약(화면 칩 · 보정 탭): state none|approx|calibrated, 모델, 점 수, 잔차, 점 분포, 이력."""
    out = {"state": "none", "model": None, "n": 0, "rms_mm": None, "max_mm": None, "axes": None, "has_axes": False,
           "bins": [[0] * 3 for _ in range(3)], "spread_ok": False, "suspect": False, "history": plate.hist_list()}
    if d is None:
        return out
    pts = d.get("points") or []
    out["n"] = len(pts)
    out["has_axes"] = bool(d.get("approx_axes"))
    out["axes"] = (d.get("approx_axes") or {}).get("source")
    out["suspect"] = int(d.get("suspect_run") or 0) >= 2
    if d.get("area"):
        out["bins"] = plate.point_bins(pts, d["area"], _size_mm())
        out["spread_ok"] = plate.points_spread_ok(pts, d["area"])
    if d.get("H") is not None:
        out["state"] = "approx" if d.get("approx") else "calibrated"
        out["model"] = d.get("model") or "click"
        out["rms_mm"], out["max_mm"] = d.get("rms_mm"), d.get("max_mm")
    return out


def approx_state():
    return ((state.plate or {}).get("calib") or {}).get("state") == "approx"


def _size_mm():
    v = state.get("calib.area_size_mm") or [154.0, 154.0]
    try:
        return float(v[0]), float(v[1])
    except (TypeError, ValueError, IndexError):
        return 154.0, 154.0


def _stage_mid():
    lim = stage_mod.limits_mm()
    return np.array([lim["x_max_mm"] / 2.0, lim["y_max_mm"] / 2.0])


def _edit_record(need_axes=False):
    """(d, 사유) - 감지 영역이 유효한 저장 본문. need_axes 면 근사 변환(축 방향)도 있어야 한다."""
    d = plate.load()
    ok, why = plate.area_validity(d, _cam_index(), _req_size())
    if not ok:
        return None, why
    if need_axes and not d.get("approx_axes"):
        return None, "근사 변환 없음 · 먼저 [근사 변환 만들기]"
    return d, ""


def _commit(d, reason):
    """이전 상태를 보관하고 변환을 다시 계산해 저장한다."""
    info = plate.solve_record(d, _size_mm())
    plate.hist_save(reason)
    plate.save(d)
    refresh_state()
    engine.recalc_from_plate()
    return info


def _set_axes(d, Q, source):
    """축 방향 Q 와 근사 평행이동(감지 영역 가운데 = 스테이지 가동범위 가운데)을 기록한다."""
    size = _size_mm()
    Hp = plate.plate_homography(d["area"], size)
    d["approx_axes"] = {"Q": [[float(a) for a in r] for r in np.asarray(Q)], "source": source,
                        "time": time.strftime("%Y-%m-%d %H:%M:%S")}
    center_plate = np.array([size[0] / 2.0, size[1] / 2.0])
    d["approx_t"] = [float(v) for v in (_stage_mid() - np.asarray(Q).T @ center_plate)]
    d["suspect_run"] = 0


def _area_center_px(d):
    q = np.asarray(d["area"], np.float64)
    return q.mean(axis=0)


async def approx_make(data):
    """[근사 변환 만들기]: 감지 영역 = area_size_mm 사각형(배율 · 원근) + 스테이지 축 방향(헤드 보정이 있으면 거기서, 없으면 자동 측정).
    위치 기준(오프셋)은 모르므로 감지 영역 가운데 = 스테이지 가동범위 가운데로 둔 '근사'다."""
    d, why = _edit_record()
    if d is None:
        await push_ack("plate_approx_make", False, "invalid", [why])
        return False
    import photohome
    head = photohome.load_head_calib()
    hv_ok, _hv = photohome.calib_validity(head) if head is not None else (False, "")
    src = str(data.get("source") or "")
    if hv_ok and src != "auto":
        Hp = plate.plate_homography(d["area"], _size_mm())
        Q = plate.axes_from_head(head["H"], Hp, _area_center_px(d))
        _set_axes(d, Q, "head")
        _commit(d, "근사 변환(헤드 보정)")
        await push_log("근사 변환 · 축 방향 = 헤드 보정에서 · 샘플로 보정점을 기록하세요", "ok")
        await push_ack("plate_approx_make", True, "", source="head")
        await push_state()
        return True
    step = float(state.get("calib.axis_probe_mm") or 10.0)
    if not data.get("confirm"):
        await push_ack("plate_approx_make", False, "needs_confirm",
                       ["X · Y 를 각각 %g mm 움직여 헤드 마커의 이동 방향으로 축 방향을 잽니다. Z 를 맨 위로 올리고 마커가 보이는 자리에서 움직입니다." % step])
        return False
    why = state.can_move()
    if why or engine.busy():
        await push_ack("plate_approx_make", False, "locked", [why or "순회 · 보정 중"])
        return False
    engine.run_job("plate_axes", _probe_job(step))
    return True


async def _probe_job(step):
    try:
        d, why = _edit_record()
        if d is None:
            raise RuntimeError(why)
        lim = stage_mod.limits_mm()
        x0, y0 = float(state.stage["x_mm"]), float(state.stage["y_mm"])
        sx = 1.0 if x0 + step <= lim["x_max_mm"] - 1.0 else -1.0
        sy = 1.0 if y0 + step <= lim["y_max_mm"] - 1.0 else -1.0
        await engine.lift_z()

        async def marker():
            bgr = await vision.single_shot(state.params)
            hm = plate.detect_head(bgr, state.get("photohome") or {})
            if hm is None:
                raise RuntimeError("헤드 마커 미검출 · 마커가 보이는 자리에서")
            return np.array([hm[0], hm[1]])

        state.job["message"] = "헤드 마커 확인"
        p0 = await marker()
        await engine._goto(x0 + sx * step, y0, no="axes")
        state.job["message"] = "X 이동 %g mm" % (sx * step)
        p1 = await marker()
        await engine._goto(x0, y0 + sy * step, no="axes")
        state.job["message"] = "Y 이동 %g mm" % (sy * step)
        p2 = await marker()
        await engine._goto(x0, y0, no="axes")
        dx, dy = p1 - p0, p2 - p0
        if min(np.linalg.norm(dx), np.linalg.norm(dy)) < 3.0:
            raise RuntimeError("마커 이동이 너무 작음 (%.1f · %.1f px)" % (np.linalg.norm(dx), np.linalg.norm(dy)))
        Hp = plate.plate_homography(d["area"], _size_mm())
        Q = plate.axes_from_probe(p0, p0 + sx * dx, p0 + sy * dy, Hp)
        _set_axes(d, Q, "auto")
        _commit(d, "근사 변환(축 방향 자동 측정)")
        await push_log("근사 변환 · 축 방향 자동 측정 (X %.0f px · Y %.0f px 이동) · 샘플로 보정점을 기록하세요"
                       % (np.linalg.norm(dx), np.linalg.norm(dy)), "ok")
        await push_ack("plate_approx_make", True, "", source="auto")
    except engine.Stopped:
        await push_ack("plate_approx_make", False, "stopped")
    except Exception as e:                 # noqa: BLE001
        await push_log("축 방향 측정 실패 · " + logger.short(e), "err")
        await push_ack("plate_approx_make", False, "error", [logger.short(e)])


def _refuse_reason(no):
    """[이 자리로 보정] 을 받을 수 없는 사유(문구) 또는 None."""
    import state as _st
    t = state.tool()
    if t.get("name") != _st.schema.TOOL_POINTER:
        return "활성 공구가 핀(기준 포인터)이 아님 · 공구 오프셋 보정을 쓰세요"
    if engine.busy() and not engine.paused():
        return "순회 중 · 일시정지 상태에서만"
    if state.stage.get("moving"):
        return "이동 중"
    if not state.stage.get("connected"):
        return "스테이지 미연결"
    if not (state.stage.get("homed_x") and state.stage.get("homed_y")) or state.stage.get("needs_home"):
        return "원점 없음"
    if state.stage.get("x_mm") is None:
        return "현재 위치 모름"
    s = state.sample(no)
    if s is None or s.get("u") is None:
        return "샘플 미선택"
    return None


async def sample_calib(data):
    """[이 자리로 보정]: 지금 스테이지 X·Y(핀이 칩 중앙에 맞은 자리) ↔ 선택 샘플의 사진 중심 px 를 보정점으로 기록한다.
    이상점 의심 · 카메라 이동 의심이면 확인을 받는다. 순회 중이면 일시정지 상태에서만."""
    try:
        no = int(data.get("no"))
    except (TypeError, ValueError):
        await push_ack("plate_sample_calib", False, "bad_args", ["샘플 번호 오류"])
        return False
    why = _refuse_reason(no)
    if why:
        await push_ack("plate_sample_calib", False, "refused", [why])
        return False
    d, why = _edit_record(need_axes=True)
    if d is None:
        await push_ack("plate_sample_calib", False, "no_approx", [why])
        return False
    s = state.sample(no)
    u, v = float(s["u"]), float(s["v"])
    x, y = float(state.stage["x_mm"]), float(state.stage["y_mm"])
    pred = plate.predict_mm(d, u, v)
    res_new = math.hypot(pred[0] - x, pred[1] - y) if pred is not None else None
    pts = [p for p in d.get("points") or [] if p.get("no") != no or p.get("src") != "sample"]
    others = [float(p.get("res_mm") or 0.0) for p in pts]
    n_out = float(state.get("calib.outlier_n") or 4.0)
    med = float(np.median(others)) if len(others) >= 3 else None
    thr = None if med is None else n_out * max(med, 0.3)
    suspicious = res_new is not None and thr is not None and res_new >= thr
    run = int(d.get("suspect_run") or 0)
    info = {"res_mm": None if res_new is None else round(res_new, 3), "median_mm": None if med is None else round(med, 3),
            "limit_mm": None if thr is None else round(thr, 3), "n": len(pts)}
    if suspicious and run + 1 >= 2:
        if data.get("confirm") != "invalidate":
            await push_log("카메라 이동 의심 · 연속 2번째 큰 어긋남 (%.2f mm)" % res_new, "warn")
            state.set_alarm("카메라 이동", "카메라 이동 의심 · 보정점 확인")
            await push_state()
            await push_ack("plate_sample_calib", False, "camera_move",
                           ["카메라 이동 의심 · 연속으로 현재 변환과 %.1f mm 어긋남(기준 %.1f mm). 기존 보정점 %d개를 무효로 하고 이 점부터 다시 시작합니까?" % (res_new, thr, len(pts))],
                           **info)
            return False
        pts = []                          # 기존 점 무효화(이력에는 남는다)
        run = 0
        suspicious = False
    elif suspicious and not data.get("confirm"):
        await push_ack("plate_sample_calib", False, "outlier",
                       ["이상점 의심 · 현재 변환의 예측과 %.2f mm 어긋남(다른 점 중앙값 %.2f mm의 %g배 이상). 그래도 기록합니까?" % (res_new, med, n_out)], **info)
        return False
    else:
        run = run + 1 if suspicious else 0
    pts.append({"x": round(x, 3), "y": round(y, 3), "u": round(u, 2), "v": round(v, 2), "src": "sample", "no": no,
                "time": time.strftime("%H:%M:%S")})
    d["points"], d["suspect_run"] = pts, run
    if run == 0:
        state.set_alarm("카메라 이동", "")
    inf = _commit(d, "샘플 #%d 보정점" % no)
    await push_log("보정점 #%d 기록 · 점 %d · %s · 잔차 RMS %.2f mm%s"
                   % (no, len(pts), {"approx": "근사", "offset": "오프셋", "similarity": "닮음 변환", "homography": "호모그래피"}.get(inf["model"], inf["model"]),
                      inf["rms"], "" if res_new is None else " · 예측과 %.2f mm 차이" % res_new), "ok")
    await push_ack("plate_sample_calib", True, "", model=inf["model"], rms_mm=round(inf["rms"], 3), max_mm=round(inf["max"], 3), **info)
    await push_state()
    return True


async def point_delete(data):
    """보정점 하나를 지운다(잔차가 큰 점 정리). 이전 상태는 이력에 보관된다."""
    d, why = _edit_record()
    if d is None:
        await push_ack("plate_point_delete", False, "invalid", [why])
        return False
    try:
        i = int(data.get("i"))
        d["points"].pop(i)
    except (TypeError, ValueError, IndexError):
        await push_ack("plate_point_delete", False, "bad_args", ["점 번호 오류"])
        return False
    if d.get("approx_axes"):
        inf = _commit(d, "보정점 삭제")
    else:
        plate.hist_save("보정점 삭제")
        plate.save(d)
        refresh_state()
        inf = None
    await push_log("보정점 삭제 · 남은 점 %d" % len(d["points"]), "ok")
    await push_ack("plate_point_delete", True, "")
    await push_state()
    return True


async def hist_restore(data):
    if engine.busy():
        await push_ack("plate_hist_restore", False, "busy", ["순회 · 보정 중"])
        return False
    ok, why = plate.hist_restore(str(data.get("name") or ""))
    if not ok:
        await push_ack("plate_hist_restore", False, "invalid", [why])
        return False
    refresh_state()
    engine.recalc_from_plate()
    state.set_alarm("카메라 이동", "")
    await push_log("판 보정 되돌림 · %s" % data.get("name"), "ok")
    await push_ack("plate_hist_restore", True, "")
    await push_state()
    return True


def _ref_state(d):
    """빈 판 기준 상태(화면 표시용): {exists, valid, reason, time}."""
    meta = plate.load_ref_meta()
    if not meta:
        size0 = _req_size()
        hist0 = []
        for h in plate.ref_history():
            hok, hwhy = plate.ref_validity(h["meta"], state.params, _cam_index(), size0, (d or {}).get("area"))
            hist0.append({"name": h["name"], "time": h["meta"].get("time"), "valid": bool(hok), "reason": hwhy})
        return {"exists": False, "valid": False, "reason": "없음", "time": None, "use": bool(state.get("fixed.use_ref")),
                "history": hist0}
    size = _req_size()
    ok, why = plate.ref_validity(meta, state.params, _cam_index(), size, (d or {}).get("area"))
    hist = []
    for h in plate.ref_history():
        hok, hwhy = plate.ref_validity(h["meta"], state.params, _cam_index(), size, (d or {}).get("area"))
        hist.append({"name": h["name"], "time": h["meta"].get("time"), "valid": bool(hok), "reason": hwhy})
    return {"exists": True, "valid": bool(ok), "reason": why, "time": meta.get("time"),
            "use": bool(state.get("fixed.use_ref")), "history": hist}


def chip_text():
    """상단 보정 칩 문구(검증용)."""
    p = state.plate or {}
    if p.get("mode") != "fixed":
        return ""
    if p.get("area_ok") and not p.get("valid"):
        return "검출만 · 판 보정 필요"
    if not p.get("valid"):
        return "판 보정 무효"
    return "고정 보정 · RMS %.2f mm" % float((p.get("data") or {}).get("rms_mm") or 0.0)


# --------------------------------------------------------------------------
async def shot():
    """보정 창용 단발 촬영(브라케팅 없음). 촬영본(state.frame)과 별도의 그림으로 준다."""
    if engine.busy() or state.camera.get("capturing"):
        await push_ack("plate_shot", False, "busy", ["순회 · 촬영 중"])
        return False
    try:
        bgr = await vision.single_shot(state.params)
        meta = await vision.run_blocking(vision.store_aux, bgr)
    except Exception as e:                 # noqa: BLE001
        one = logger.short(e)
        logger.write("err", "단발 촬영 실패 상세: %s" % e)
        await push_log("단발 촬영 실패 · " + one, "err")
        await push_ack("plate_shot", False, "error", [one])
        return False
    state.plate["shot"] = meta
    await push_log("단발 촬영 · %d×%d" % (meta["w"], meta["h"]), "ok")
    await push_ack("plate_shot", True, "", frame=meta)
    await push_state()
    return True


def _clean_points(points, size):
    """화면이 보낸 점 목록을 검증한다. (점 목록, 오류 문구|None)."""
    out = []
    for i, p in enumerate(points or []):
        try:
            x, y, u, v = (float(p["x"]), float(p["y"]), float(p["u"]), float(p["v"]))
        except (KeyError, TypeError, ValueError):
            return None, "점 %d · 값 오류" % (i + 1)
        if not all(math.isfinite(t) for t in (x, y, u, v)):
            return None, "점 %d · 값 오류" % (i + 1)
        if not (0 <= u <= size[0] and 0 <= v <= size[1]):
            return None, "점 %d · 촬영본 밖" % (i + 1)
        pt = {"x": round(x, 3), "y": round(y, 3), "u": round(u, 2), "v": round(v, 2),
              "src": "sample" if p.get("src") == "sample" else "click"}
        if p.get("no") is not None:
            pt["no"] = p.get("no")
        out.append(pt)
    return out, None


def _size_arg(data):
    try:
        w, h = data.get("size")
        return int(w), int(h)
    except (TypeError, ValueError):
        return _req_size()


async def pos_record():
    """[위치 기록] - 지금 스테이지 XY(기준 포인터)."""
    why = state.can_move()
    if why:
        await push_ack("plate_pos", False, "locked", [why])
        return False
    x, y = state.stage.get("x_mm"), state.stage.get("y_mm")
    if x is None or y is None:
        await push_ack("plate_pos", False, "locked", ["현재 위치 모름"])
        return False
    await push_ack("plate_pos", True, "", x=round(float(x), 3), y=round(float(y), 3))
    return True


async def fit_preview(data):
    """점 목록의 잔차 · RMS 를 계산해 돌려준다(저장 안 함)."""
    size = _size_arg(data)
    pts, err = _clean_points(data.get("points"), size)
    if pts is None:
        await push_ack("plate_calib_fit", False, "bad_args", [err])
        return False
    lim = float(state.get("calib.residual_limit_mm") or 1.0)
    f = plate.fit(pts)
    if f is None:
        await push_ack("plate_calib_fit", False, "min_points",
                       ["점 %d개 · 4개 이상 필요(한 직선 위는 안 됨)" % len(pts)],
                       n=len(pts), res_limit_mm=lim)
        return False
    res = [round(r, 4) for r in f["res"]]
    await push_ack("plate_calib_fit", True, "", res=res, rms_mm=round(f["rms"], 4),
                   max_mm=round(f["max"], 4), res_limit_mm=lim, n=len(pts),
                   over=[i for i, r in enumerate(res) if r > lim])
    return True


async def calib_apply(data):
    """[적용] - 판 보정 저장. 점 4개 미만이면 거절한다."""
    size = _size_arg(data)
    pts, err = _clean_points(data.get("points"), size)
    if pts is None:
        await push_ack("plate_calib_apply", False, "bad_args", [err])
        return False
    f = plate.fit(pts)
    if f is None:
        await push_ack("plate_calib_apply", False, "min_points",
                       ["점 %d개 · 4개 이상 필요(한 직선 위는 안 됨)" % len(pts)], n=len(pts))
        await push_log("판 보정 적용 거절 · 점 %d개 (4개 이상 필요)" % len(pts), "warn")
        return False
    lim = float(state.get("calib.residual_limit_mm") or 1.0)
    d = plate.new_record(_cam_index(), size, plate.load())
    for p, r in zip(pts, f["res"]):
        p["res_mm"] = round(r, 4)
    d.update({"points": pts, "H": f["H"], "rms_mm": round(f["rms"], 4),
              "max_mm": round(f["max"], 4), "res_limit_mm": lim, "model": "click", "approx": False, "suspect_run": 0})
    plate.hist_save("점 클릭 방식 적용")
    plate.save(d)
    refresh_state()
    n_re = engine.recalc_from_plate()
    if n_re:
        await push_log("판 보정 반영 · 편집한 샘플 포함 %d개의 mm 좌표 계산" % n_re, "ok")
    if (state.plate or {}).get("size_warn"):
        await push_log(state.plate["size_warn"], "warn")
    over = [i for i, p in enumerate(pts) if p["res_mm"] > lim]
    await push_log("판 보정 저장 · %d점 · RMS %.3f · 최대 %.3f mm%s · 다시 촬영하면 반영"
                   % (len(pts), f["rms"], f["max"],
                      "" if not over else " · 한계 %.2f 초과 %d점" % (lim, len(over))),
                   "ok" if not over else "warn")
    await push_ack("plate_calib_apply", True, "", rms_mm=round(f["rms"], 4),
                   max_mm=round(f["max"], 4), over=over)
    await push_state()
    return True


async def exclude_apply(data):
    """[적용] - 제외 영역 다각형 목록 저장(감지 영역 안에 걸리는 헤드 · 기판 등을 가린다). 빈 목록 = 모두 지움."""
    size = _size_arg(data)
    polys = []
    for i, poly in enumerate(data.get("polys") or []):
        try:
            pts = [(float(a), float(b)) for a, b in poly]
        except (TypeError, ValueError):
            await push_ack("plate_exclude_apply", False, "invalid", ["제외 영역 %d · 값 오류" % (i + 1)])
            return False
        if len(pts) < 3 or not all(math.isfinite(t) and 0 <= t <= m for p in pts for t, m in zip(p, size)):
            await push_ack("plate_exclude_apply", False, "invalid", ["제외 영역 %d · 점 3개 이상 · 촬영본 안" % (i + 1)])
            return False
        polys.append([[round(a, 1), round(b, 1)] for a, b in pts])
    d = plate.load()
    if d is None or [int(x) for x in d.get("size") or []] != [int(size[0]), int(size[1])]:
        d = plate.new_record(_cam_index(), size, d)
    d["exclude"] = polys
    plate.save(d)
    refresh_state()
    await push_log("제외 영역 저장 · %d개 · 다시 촬영하면 반영" % len(polys), "ok")
    await push_ack("plate_exclude_apply", True, "", n=len(polys))
    await push_state()
    return True


async def ref_use(data):
    """[이 기준 사용] - 보관된 기준을 현재 기준으로 되돌린다. 메타가 지금 카메라 설정과 다르면 확인을 받는다."""
    if engine.busy() or state.camera.get("capturing"):
        await push_ack("plate_ref_use", False, "busy", ["순회 · 촬영 중"])
        return False
    name = str(data.get("name") or "")
    hist = {h["name"]: h for h in plate.ref_history()}
    h = hist.get(name)
    if h is None:
        await push_ack("plate_ref_use", False, "invalid", ["보관된 기준 없음"])
        return False
    d = plate.load()
    ok, why = plate.ref_validity(h["meta"], state.params, _cam_index(), _req_size(), (d or {}).get("area"))
    if not ok and not data.get("confirm"):
        await push_ack("plate_ref_use", False, "needs_confirm", ["무효 기준 · " + why + " · 그래도 현재 기준으로 되돌립니다"], name=name)
        return False
    done, msg = await vision.run_blocking(plate.use_ref, name)
    if not done:
        await push_ack("plate_ref_use", False, "invalid", [msg])
        return False
    refresh_state()
    await push_log("빈 판 기준 되돌림 · %s%s" % (h["meta"].get("time"), "" if ok else " (무효 · %s)" % why), "ok" if ok else "warn")
    await push_ack("plate_ref_use", True, "", valid=bool(ok))
    await push_state()
    return True


async def ref_shot():
    """[빈 판 기준 촬영] - 샘플을 치운 빈 판을 한 장 찍어 기준으로 저장(브라케팅 없음)."""
    if engine.busy() or state.camera.get("capturing"):
        await push_ack("plate_ref_shot", False, "busy", ["순회 · 촬영 중"])
        return False
    d = plate.load()
    if not d or not d.get("area"):
        await push_ack("plate_ref_shot", False, "invalid", ["감지 영역 먼저 지정"])
        return False
    try:
        bgr = await vision.single_shot(state.params)
    except Exception as e:                 # noqa: BLE001
        one = logger.short(e)
        logger.write("err", "빈 판 기준 촬영 실패 상세: %s" % e)
        await push_log("빈 판 기준 촬영 실패 · " + one, "err")
        await push_ack("plate_ref_shot", False, "error", [one])
        return False
    size = (int(bgr.shape[1]), int(bgr.shape[0]))
    if [int(x) for x in d.get("size") or []] != list(size):
        await push_ack("plate_ref_shot", False, "invalid", ["감지 영역과 해상도가 다름 · 감지 영역 다시 지정"])
        return False
    await vision.run_blocking(plate.save_ref, bgr, state.params, _cam_index(), d["area"])
    refresh_state()
    await push_log("빈 판 기준 저장 · %d×%d · 노출 %s · 화이트밸런스 %s"
                   % (size[0], size[1], state.get("exposure"), state.get("wb_temperature")), "ok")
    await push_ack("plate_ref_shot", True, "")
    await push_state()
    return True


async def area_apply(data):
    """[적용] - 감지 영역 사각형 저장(볼록 · 프레임 안)."""
    size = _size_arg(data)
    quad, why = plate.check_quad(data.get("quad"), size)
    if quad is None:
        await push_ack("plate_area_apply", False, "invalid", [why])
        await push_log("감지 영역 적용 거절 · " + why, "warn")
        return False
    d = plate.new_record(_cam_index(), size, plate.load())
    d["area"] = quad
    plate.save(d)
    refresh_state()
    await push_log("감지 영역 저장 · %d×%d 기준" % size, "ok")
    await push_ack("plate_area_apply", True, "", quad=quad)
    await push_state()
    return True
