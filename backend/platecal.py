"""platecal.py - 고정 보정(calib.mode = fixed)의 감지 영역 · 판 보정 명령.

판 마커 없이 쓰는 방식이다(core/plate.py 가 계산, 여기는 서버 상태·명령).
  감지 영역: 촬영본 픽셀 사각형 한 개. 보정 창에서 네 모서리를 클릭 → [적용].
  판 보정:   기준 포인터를 판 위 표시점에 맞춰 [위치 기록](스테이지 XY) + 촬영본에서
             같은 점 클릭(u, v) → 점 4개 이상 → 호모그래피 최소제곱 → [적용].
저장: data/plate_calib.json (카메라 번호 · 해상도 포함). 카메라 번호나 해상도가 저장 때와
다르면 무효 - 촬영은 하되 좌표를 만들지 않고 경보 배너를 띄운다.
"""

import math

from core import paths, plate

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
    state.plate = {"mode": mode, "valid": bool(ok), "reason": why,
                   "data": plate.summary(d), "shot": keep_shot}
    if mode != "fixed" or ok:
        state.set_alarm("판 보정", "")
    else:
        state.set_alarm("판 보정", "판 보정 무효 · 다시 보정")
    engine._update_pointer_px()
    return ok


def chip_text():
    """상단 보정 칩 문구(검증용)."""
    p = state.plate or {}
    if p.get("mode") != "fixed":
        return ""
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
        out.append({"x": round(x, 3), "y": round(y, 3), "u": round(u, 2), "v": round(v, 2)})
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
              "max_mm": round(f["max"], 4), "res_limit_mm": lim})
    plate.save(d)
    refresh_state()
    over = [i for i, p in enumerate(pts) if p["res_mm"] > lim]
    await push_log("판 보정 저장 · %d점 · RMS %.3f · 최대 %.3f mm%s · 다시 촬영하면 반영"
                   % (len(pts), f["rms"], f["max"],
                      "" if not over else " · 한계 %.2f 초과 %d점" % (lim, len(over))),
                   "ok" if not over else "warn")
    await push_ack("plate_calib_apply", True, "", rms_mm=round(f["rms"], 4),
                   max_mm=round(f["max"], 4), over=over)
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
