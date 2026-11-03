"""photohome.py - 사진 호밍(헤드 마커)과 보정 절차(헤드 마커 · 판 마커 · 공구 오프셋).

사진 호밍 원리
  Z 축 고정부(Z 모터 윗면 - 높이가 변하지 않는 곳)에 ArUco 마커(헤드 마커)를 붙인다.
  천장 카메라가 그 마커를 찍으면 픽셀 중심(모서리 4점 평균, 서브픽셀)이 나오고,
  헤드 마커 평면 전용 호모그래피 H 로 스테이지 좌표 P(기준 포인터가 가리키는 점)를
  계산한다. 판 마커와 높이가 달라 판 보정(calib)을 그대로 쓸 수 없어 따로 보정한다.

  재 본 P 와 펌웨어가 기록한 위치의 차이 d:
    d < 허용 오차            → 그대로
    허용 ~ 경고 오차         → sp 로 자동 보정 + 로그
    d > 경고 또는 원점 없음  → 확인 요구(화면 대화상자) 뒤 sp
  sp 뒤에는 save 한다. 원점이 없어도 된다 - 원점을 잃었을 때 복구하는 수단이다.

헤드 마커 보정
  원점이 잡힌 상태에서 격자 점(가동범위 안, 여백)을 차례로 이동 → Z 맨 위 → 안정 대기
  → 한 장(브라케팅 없음, 실패 시 재시도) → 헤드 마커 중심 → (픽셀 ↔ 스테이지 XY) 쌍 →
  호모그래피 최소제곱 → 점별 잔차 · RMS. 사람이 [적용]을 눌러야 data/head_calib.json.
  카메라 번호·해상도가 보정 때와 다르면 보정은 무효이고 사진 호밍을 막는다.
"""

import asyncio
import json
import math
import os
import time

import cv2
import numpy as np

from core import calib, paths

import engine
import logger
import relays
import stagectl
import storage
import vision
from connection import push_ack, push_log, push_state
from state import state

PENDING_TTL_S = 120.0                      # 확인 대기 중인 사진 호밍 결과의 유효 시간
_pending = {"home": None, "head": None, "marker": {}, "tool": {}}


# --------------------------------------------------------------------------
# 헤드 마커 검출 · 보정 파일
# --------------------------------------------------------------------------
def detect_head(bgr, values):
    """(u, v) 헤드 마커 중심(px) 또는 None."""
    aruco = getattr(cv2, "aruco", None)
    if aruco is None or bgr is None:
        return None
    ph = values.get("photohome") or {}
    name = "DICT_" + str(ph.get("dict", "4X4_50"))
    dic = aruco.getPredefinedDictionary(getattr(aruco, name, aruco.DICT_4X4_50))
    dp = aruco.DetectorParameters()
    dp.cornerRefinementMethod = aruco.CORNER_REFINE_SUBPIX
    corners, ids, _rej = aruco.ArucoDetector(dic, dp).detectMarkers(bgr)
    if ids is None:
        return None
    want = int(ph.get("marker_id", 10))
    for c, i in zip(corners, ids.reshape(-1)):
        if int(i) == want:
            q = np.asarray(c).reshape(4, 2).astype(np.float64)
            return float(q[:, 0].mean()), float(q[:, 1].mean())
    return None


def load_head_calib():
    try:
        with open(paths.HEAD_CALIB_PATH, encoding="utf-8") as f:
            d = json.load(f)
        d["H"] = np.asarray(d["H"], np.float64)
        return d
    except (OSError, ValueError, KeyError, TypeError):
        return None


def calib_validity(cal=None, frame_size=None):
    """(유효한가, 사유). 카메라 번호·해상도·마커가 보정 때와 같아야 한다."""
    cal = cal if cal is not None else load_head_calib()
    if cal is None:
        return False, "헤드 보정 없음"
    v = state.settings
    if int(cal.get("camera_index", -1)) != int(v.get("camera_index", 1)):
        return False, "카메라 번호가 보정 때와 다름"
    size = list(frame_size) if frame_size else [int(v.get("width", 0)), int(v.get("height", 0))]
    if list(cal.get("size") or []) != size:
        return False, "해상도가 보정 때와 다름 (%s → %s)" % (
            "×".join(str(x) for x in cal.get("size") or []), "×".join(str(x) for x in size))
    ph = v.get("photohome") or {}
    if int(cal.get("marker_id", -1)) != int(ph.get("marker_id", 10)):
        return False, "헤드 마커 번호가 보정 때와 다름"
    return True, ""


def refresh_state():
    """state.photohome 의 보정 요약·유효성을 다시 계산한다."""
    cal = load_head_calib()
    ok, why = calib_validity(cal)
    state.photohome["calib"] = None if cal is None else {
        "created": cal.get("created"), "rms_mm": cal.get("rms_mm"),
        "max_mm": cal.get("max_mm"), "n": len(cal.get("points") or []),
        "size": cal.get("size"), "camera_index": cal.get("camera_index")}
    state.photohome["calib_valid"] = ok
    state.photohome["calib_reason"] = why
    # 보정이 있는데 무효면 경보(사진 호밍이 막힌다). 보정이 아예 없으면 안내만.
    state.set_alarm("사진 호밍", "보정 무효 · " + why if (cal is not None and not ok) else "")


def px_to_stage(H, u, v):
    p = H @ np.array([float(u), float(v), 1.0])
    return float(p[0] / p[2]), float(p[1] / p[2])


async def _shot_find(retries):
    """헤드 마커가 보일 때까지 찍는다. (u, v, (w, h)) 또는 (None, None, size)."""
    size = None
    for i in range(1 + max(0, int(retries))):
        try:
            bgr = await vision.single_shot(state.params)
        except Exception as e:             # noqa: BLE001
            logger.write("warn", "헤드 마커 촬영 실패 %d · %s" % (i + 1, e))
            await asyncio.sleep(0.3)
            continue
        size = (int(bgr.shape[1]), int(bgr.shape[0]))
        uv = await vision.run_blocking(detect_head, bgr, state.settings)
        if uv is not None:
            return uv[0], uv[1], size
    return None, None, size


async def measure():
    """지금 자리에서 한 장 찍어 헤드 마커로 실제 위치를 잰다(이동 없음).

    {"ok", "reason", "x", "y", "u", "v"} - reason: no_calib|calib_invalid|not_found
    """
    cal = load_head_calib()
    ok, why = calib_validity(cal)
    if not ok:
        return {"ok": False, "reason": "no_calib" if cal is None else "calib_invalid",
                "msg": why}
    u, v, size = await _shot_find(int(state.get("photohome.shot_retries") or 0))
    if size is not None:
        ok, why = calib_validity(cal, size)
        if not ok:
            return {"ok": False, "reason": "calib_invalid", "msg": why}
    if u is None:
        return {"ok": False, "reason": "not_found",
                "msg": "헤드 마커 미검출 · 수동 이동으로 시야 안에"}
    x, y = px_to_stage(cal["H"], u, v)
    return {"ok": True, "reason": "", "x": round(x, 3), "y": round(y, 3),
            "u": round(u, 2), "v": round(v, 2)}


def _recorded():
    """믿을 수 있는 기록 위치 (x, y) 또는 None(원점 없음 · 비상정지 뒤)."""
    st = state.stage
    if not (st.get("homed_x") and st.get("homed_y")) or st.get("needs_home"):
        return None
    if st.get("x_mm") is None or st.get("y_mm") is None:
        return None
    return float(st["x_mm"]), float(st["y_mm"])


def _set_status(status, msg, d=None, xy=None):
    state.photohome.update({"status": status, "msg": msg,
                            "d_mm": None if d is None else round(d, 3),
                            "time": time.strftime("%H:%M:%S"),
                            "x_mm": None if xy is None else round(xy[0], 2),
                            "y_mm": None if xy is None else round(xy[1], 2)})


async def apply_sp(x, y):
    """펌웨어 좌표를 (x, y) 로 맞추고 save. 범위 밖이면 StageError."""
    lim = stagectl.limits_mm()
    if not (0 <= x <= lim["x_max_mm"] and 0 <= y <= lim["y_max_mm"]):
        raise stagectl.StageError("잰 위치 X%.2f Y%.2f 가 가동범위 밖" % (x, y))
    engine._apply_status(await stagectl.ctl.set_position(x, y))
    if stagectl.ctl.dev is not None:
        stagectl.ctl.dev.reset_abort()
    engine.reset_estop()
    state.stage["last_error"] = ""
    for ln in await stagectl.ctl.save():
        if str(ln).strip():
            logger.write("info", "사진 호밍 저장: %s" % str(ln).strip())
    engine._apply_status(await stagectl.ctl.status())


async def check(auto=False):
    """사진 호밍 한 번. auto(촬영 전 자동 확인)면 확인이 필요해도 묻지 않고 경고만.

    돌려주는 것: "ok" | "corrected" | "needs_confirm" | "failed" | "skipped"
    """
    if not stagectl.ctl.connected:
        return "skipped"
    m = await measure()
    if not m["ok"]:
        if auto and m["reason"] == "no_calib":
            await push_log("헤드 보정 없음 · 위치 확인 건너뜀")
            return "skipped"
        _set_status("failed", m["msg"])
        await push_log("위치 확인 실패 · " + m["msg"], "warn")
        await push_state()
        return "failed"
    meas = (m["x"], m["y"])
    rec = _recorded()
    tol = float(state.get("photohome.tol_mm") or 0.3)
    warn = float(state.get("photohome.warn_mm") or 5.0)
    d = None if rec is None else math.hypot(meas[0] - rec[0], meas[1] - rec[1])
    if d is not None and d < tol:
        _set_status("ok", "정상 · 차이 %.2f mm" % d, d, meas)
        await push_log("위치 확인 정상 · 차이 %.2f mm" % d, "ok")
        await push_state()
        return "ok"
    if d is not None and d <= warn:
        await apply_sp(*meas)
        _set_status("corrected", "보정됨 · 차이 %.2f mm" % d, d, meas)
        await push_log("위치 자동 보정 · 차이 %.2f mm → X%.2f Y%.2f" % (d, meas[0], meas[1]),
                       "warn")
        await push_state()
        return "corrected"
    why = "원점 없음" if d is None else "차이 %.2f mm > 경고 %.2f" % (d, warn)
    _pending["home"] = {"x": meas[0], "y": meas[1], "d": d, "rec": rec,
                        "t": time.monotonic()}
    _set_status("failed", "확인 필요 · " + why, d, meas)
    await push_log("위치 확인 필요 · %s (잰 위치 X%.2f Y%.2f)" % (why, meas[0], meas[1]), "warn")
    await push_state()
    if not auto:
        await push_ack("photo_home", False, "needs_confirm", [why],
                       measured=[meas[0], meas[1]],
                       recorded=None if rec is None else [rec[0], rec[1]],
                       d_mm=None if d is None else round(d, 3))
    return "needs_confirm"


async def photo_home():
    """[사진 호밍] 버튼. 결과를 ack 로 돌려준다(확인 요구는 check 가 보낸다)."""
    r = await check(auto=False)
    if r != "needs_confirm":
        await push_ack("photo_home", r in ("ok", "corrected"), r,
                       d_mm=state.photohome.get("d_mm"))
    return r


async def photo_home_apply():
    """확인 대화상자에서 [확인]. 서버가 잰 값(클라이언트 값이 아니라)으로 sp."""
    p = _pending.get("home")
    if not p or time.monotonic() - p["t"] > PENDING_TTL_S:
        await push_log("확인할 사진 호밍 결과 없음 · 다시 실행", "warn")
        await push_ack("photo_home_apply", False, "expired")
        return False
    if _recorded() != p["rec"]:
        await push_log("그 사이 위치가 바뀜 · 사진 호밍 다시 실행", "warn")
        await push_ack("photo_home_apply", False, "moved")
        return False
    _pending["home"] = None
    try:
        await apply_sp(p["x"], p["y"])
    except stagectl.StageError as e:
        _set_status("failed", logger.short(e))
        await push_log("사진 호밍 적용 실패 · " + logger.short(e), "err")
        await push_ack("photo_home_apply", False, "error")
        await push_state()
        return False
    _set_status("corrected", "보정됨 · 확인 후 적용", p["d"], (p["x"], p["y"]))
    await push_log("사진 호밍 적용 · X%.2f Y%.2f" % (p["x"], p["y"]), "ok")
    await push_ack("photo_home_apply", True)
    await push_state()
    return True


async def verify_at(px, py):
    """샘플 이동 후 재확인. (ok, 사유). 넘으면 한 번 sp + 다시 이동, 그래도 넘으면 실패."""
    tol = float(state.get("photohome.tol_mm") or 0.3)
    for attempt in (1, 2):
        m = await measure()
        if not m["ok"]:
            return False, m["msg"]
        d = math.hypot(m["x"] - px, m["y"] - py)
        if d < tol:
            _set_status("ok", "정상 · 차이 %.2f mm" % d, d, (m["x"], m["y"]))
            return True, ""
        if attempt == 2:
            _set_status("failed", "재확인 실패 · 차이 %.2f mm" % d, d, (m["x"], m["y"]))
            return False, "차이 %.2f mm" % d
        await push_log("도착 위치 차이 %.2f mm · 바로잡고 다시 이동" % d, "warn")
        await apply_sp(m["x"], m["y"])
        await engine._goto(px, py, no="recheck")
    return False, "재확인 실패"


# --------------------------------------------------------------------------
# 헤드 마커 보정
# --------------------------------------------------------------------------
def grid_points():
    lim = stagectl.limits_mm()
    n = int(state.get("photohome.grid_n") or 3)
    m = float(state.get("photohome.grid_margin_mm") or 0.0)
    xs = np.linspace(m, lim["x_max_mm"] - m, n)
    ys = np.linspace(m, lim["y_max_mm"] - m, n)
    if lim["x_max_mm"] - 2 * m <= 0 or lim["y_max_mm"] - 2 * m <= 0:
        return []
    pts = []
    for i, x in enumerate(xs):
        row = list(ys) if i % 2 == 0 else list(ys)[::-1]   # 뱀 모양 - 이동이 짧다
        for y in row:
            pts.append((round(float(x), 2), round(float(y), 2)))
    return pts


def fit_head(points):
    """points [{x, y, u, v}] → (H, 잔차 목록, rms, max)."""
    src = np.array([[p["u"], p["v"]] for p in points], np.float64)
    dst = np.array([[p["x"], p["y"]] for p in points], np.float64)
    H, _m = cv2.findHomography(src, dst, 0)
    if H is None:
        return None, [], None, None
    res = []
    for p in points:
        x, y = px_to_stage(H, p["u"], p["v"])
        res.append(math.hypot(x - p["x"], y - p["y"]))
    rms = float(np.sqrt(np.mean(np.square(res))))
    return H, res, rms, float(max(res))


async def head_calib_start():
    why = state.can_move()
    if why:
        await push_log("헤드 보정 불가 · " + why, "warn")
        await push_ack("head_calib", False, "locked", [why])
        return False
    pts = grid_points()
    if len(pts) < 4:
        await push_ack("head_calib", False, "bad_grid", ["격자 점 4개 미만 · 여백 확인"])
        return False
    engine.run_job("head_calib", _head_calib_job(pts))
    return True


async def _head_calib_job(pts):
    state.job.update({"n": len(pts), "i": 0, "message": "헤드 보정 시작", "result": None})
    await push_log("헤드 마커 보정 · %d점" % len(pts))
    await push_state()
    got = []
    settle = float(state.get("photohome.settle_s") or 0.5)
    retries = int(state.get("photohome.shot_retries") or 3)
    try:
        for i, (x, y) in enumerate(pts):
            if engine.stopping():
                raise engine.Stopped()
            state.job.update({"i": i + 1, "message": "%d/%d · X%.0f Y%.0f" % (i + 1, len(pts), x, y)})
            await push_state()
            await engine.lift_z()
            await engine._goto(x, y, no="head_calib")
            await asyncio.sleep(settle)
            u, v, size = await _shot_find(retries)
            if u is None:
                raise engine.StepError("X%.0f Y%.0f 에서 헤드 마커 미검출" % (x, y))
            got.append({"x": state.stage["x_mm"], "y": state.stage["y_mm"],
                        "u": round(u, 3), "v": round(v, 3)})
        H, res, rms, mx = fit_head(got)
        if H is None:
            raise engine.StepError("호모그래피 계산 실패")
        for p, r in zip(got, res):
            p["res_mm"] = round(r, 4)
        lim = float(state.get("photohome.rms_limit_mm") or 0.5)
        size = list(vision.camera_size(state.params)) if size is None else list(size)
        ph = state.get("photohome") or {}
        result = {"created": time.strftime("%Y-%m-%d %H:%M:%S"),
                  "camera_index": int(state.get("camera_index") or 0), "size": size,
                  "marker_id": int(ph.get("marker_id", 10)),
                  "marker_mm": float(ph.get("marker_mm", 40.0)),
                  "dict": ph.get("dict", "4X4_50"),
                  "H": [[float(a) for a in row] for row in H],
                  "points": got, "rms_mm": round(rms, 4), "max_mm": round(mx, 4),
                  "rms_limit_mm": lim, "ok": rms <= lim}
        _pending["head"] = result
        state.job.update({"message": "RMS %.3f mm%s" % (rms, "" if rms <= lim else " · 한계 초과"),
                          "result": {k: result[k] for k in ("rms_mm", "max_mm", "ok", "points",
                                                            "rms_limit_mm", "size")}})
        await push_log("헤드 보정 완료 · RMS %.3f · 최대 %.3f mm%s"
                       % (rms, mx, "" if rms <= lim else " · 한계 %.2f 초과 · 다시 권장" % lim),
                       "ok" if rms <= lim else "warn")
        await push_ack("head_calib", True, "", result=state.job["result"])
    except engine.Stopped:
        state.job.update({"message": "정지 · 보정 안 됨", "result": None})
        await push_log("헤드 보정 정지", "warn")
        await push_ack("head_calib", False, "stopped")
    except (engine.StepError, stagectl.StageError) as e:
        state.job.update({"message": "실패 · " + logger.short(e), "result": None})
        await push_log("헤드 보정 실패 · " + logger.short(e), "err")
        await push_ack("head_calib", False, "error", [logger.short(e)])
    finally:
        await push_state()


async def head_calib_apply():
    r = _pending.get("head")
    if not r:
        await push_ack("head_calib_apply", False, "no_result")
        return False
    storage.atomic_write_json(paths.HEAD_CALIB_PATH, r)
    _pending["head"] = None
    state.job["result"] = None
    refresh_state()
    await push_log("헤드 보정 저장 · RMS %.3f mm · %d점" % (r["rms_mm"], len(r["points"])), "ok")
    await push_ack("head_calib_apply", True)
    await push_state()
    return True


# --------------------------------------------------------------------------
# 판 마커 좌표 등록(기준 포인터를 마커 중심에 맞춰 기록)
# --------------------------------------------------------------------------
async def marker_record(mid):
    why = state.can_move()
    if why:
        await push_ack("calib_marker", False, "locked", [why])
        return False
    try:
        mid = str(int(mid))
    except (TypeError, ValueError):
        await push_ack("calib_marker", False, "bad_args")
        return False
    x, y = float(state.stage["x_mm"]), float(state.stage["y_mm"])
    old = (state.get("marker_mm_xy") or {}).get(mid)
    diff = None if not old else math.hypot(x - float(old[0]), y - float(old[1]))
    _pending["marker"][mid] = [round(x, 2), round(y, 2)]
    await push_log("판 마커 id%s 기록 · X%.2f Y%.2f%s"
                   % (mid, x, y, "" if diff is None else " (이전과 %.2f mm)" % diff))
    await push_ack("calib_marker", True, "", id=mid, new=[round(x, 2), round(y, 2)],
                   old=old, diff_mm=None if diff is None else round(diff, 3))
    return True


async def marker_apply(mid):
    mid = str(mid)
    new = _pending["marker"].get(mid)
    if not new:
        await push_ack("calib_marker_apply", False, "no_result")
        return False
    errs = state.save_settings({"marker_mm_xy." + mid: new})
    if errs:
        await push_ack("calib_marker_apply", False, "invalid", errors=errs)
        return False
    _pending["marker"].pop(mid, None)
    await push_log("판 마커 id%s 좌표 저장 · X%.2f Y%.2f" % (mid, new[0], new[1]), "ok")
    await push_ack("calib_marker_apply", True, id=mid)
    await push_state()
    return True


# --------------------------------------------------------------------------
# 공구 오프셋 보정
#   방법 1(맞추기): 기준점에 기준 포인터 → [기록 A], 공구 끝을 같은 점에 → [기록 B]
#                   d = A − B  (공구 끝은 P + d 를 가리키므로 B + d = A)
#   방법 2(자국):   P0 에서 단독 가열 → 공구를 치우고 촬영 → 자국 중심 클릭 → M
#                   d = M − P0
# --------------------------------------------------------------------------
def _tool_result():
    t = _pending["tool"]
    out = {"A": t.get("A"), "B": t.get("B"), "P0": t.get("P0"), "M": t.get("M")}
    if t.get("A") and t.get("B"):
        out["d1"] = [round(t["A"][0] - t["B"][0], 3), round(t["A"][1] - t["B"][1], 3)]
    if t.get("P0") and t.get("M"):
        out["d2"] = [round(t["M"][0] - t["P0"][0], 3), round(t["M"][1] - t["P0"][1], 3)]
    return out


async def tool_record(which):
    which = str(which or "").upper()
    if which not in ("A", "B"):
        await push_ack("calib_tool", False, "bad_args")
        return False
    why = state.can_move()
    if why:
        await push_ack("calib_tool", False, "locked", [why])
        return False
    _pending["tool"][which] = [round(float(state.stage["x_mm"]), 3),
                               round(float(state.stage["y_mm"]), 3)]
    await push_log("공구 오프셋 기록 %s · X%.2f Y%.2f" % ((which,) + tuple(_pending["tool"][which])))
    await push_ack("calib_tool", True, "", result=_tool_result())
    return True


async def tool_heat_start(n, seconds, z_mm):
    why = state.can_move()
    if why:
        await push_ack("calib_tool_heat", False, "locked", [why])
        return False
    try:
        n, seconds, z_mm = int(n), float(seconds), float(z_mm)
    except (TypeError, ValueError):
        await push_ack("calib_tool_heat", False, "bad_args")
        return False
    errs = []
    if not relays.item(n).get("enabled"):
        errs.append("R%d 사용 안 함" % n)
    if not 0 < seconds <= 600:
        errs.append("가열 시간 0~600 s")
    if not state.stage.get("homed_z"):
        errs.append("Z 원점 없음")
    elif not 0 <= z_mm <= stagectl.z_max_mm():
        errs.append("Z 0~%g mm" % stagectl.z_max_mm())
    if errs:
        await push_ack("calib_tool_heat", False, "invalid", errs)
        return False
    engine.run_job("tool_heat", _tool_heat_job(n, seconds, z_mm))
    return True


async def _tool_heat_job(n, seconds, z_mm):
    p0 = [round(float(state.stage["x_mm"]), 3), round(float(state.stage["y_mm"]), 3)]
    state.job.update({"n": 4, "i": 0, "message": "가열 준비", "result": None})
    on = False
    try:
        state.job.update({"i": 1, "message": "Z %.1f mm" % z_mm})
        await push_state()
        await engine.move_z(z_mm)
        ok, reason = await relays.set_relay(n, True, by_engine=True)
        if not ok:
            raise engine.StepError("R%d 켜기 실패 (%s)" % (n, reason))
        on = True
        left = seconds
        while left > 0:
            if engine.stopping():
                raise engine.Stopped()
            state.job.update({"i": 2, "message": "가열 · 남은 %.0f s" % left})
            await push_state()
            await asyncio.sleep(min(0.5, left))
            left -= 0.5
        await relays.set_relay(n, False, by_engine=True)
        on = False
        state.job.update({"i": 3, "message": "Z 맨 위"})
        await engine.lift_z()
        _pending["tool"]["P0"] = p0
        _pending["tool"].pop("M", None)
        state.job.update({"i": 4, "message": "가열 끝 · 공구를 치우고 촬영 후 자국 클릭"})
        await push_log("공구 자국 가열 끝 · P0 X%.2f Y%.2f" % tuple(p0), "ok")
        await push_ack("calib_tool_heat", True, "", result=_tool_result())
    except engine.Stopped:
        state.job["message"] = "정지"
        await push_ack("calib_tool_heat", False, "stopped")
    except (engine.StepError, stagectl.StageError) as e:
        state.job["message"] = "실패 · " + logger.short(e)
        await push_log("공구 자국 가열 실패 · " + logger.short(e), "err")
        await push_ack("calib_tool_heat", False, "error", [logger.short(e)])
    finally:
        if on and not engine.estopped():
            await relays.set_relay(n, False, by_engine=True)   # 어떻게 끝나든 끈다
        await push_state()


async def tool_mark(u, v):
    """촬영본의 자국 중심 클릭(프레임 px) → M (판 보정으로 mm)."""
    if not _pending["tool"].get("P0"):
        await push_ack("calib_tool_mark", False, "no_heat", ["먼저 가열"])
        return False
    cal = engine.current_cal()
    if cal is None:
        await push_ack("calib_tool_mark", False, "no_capture", ["촬영 결과 없음"])
        return False
    try:
        u, v = float(u), float(v)
    except (TypeError, ValueError):
        await push_ack("calib_tool_mark", False, "bad_args")
        return False
    if not (math.isfinite(u) and math.isfinite(v)):
        await push_ack("calib_tool_mark", False, "bad_args")
        return False
    x, y = calib.px_to_mm(cal, u, v)
    _pending["tool"]["M"] = [round(x, 3), round(y, 3)]
    r = _tool_result()
    await push_log("자국 중심 X%.2f Y%.2f · 오프셋 dX %.2f dY %.2f"
                   % (x, y, r["d2"][0], r["d2"][1]), "ok")
    await push_ack("calib_tool_mark", True, "", result=r)
    return True


async def tool_apply(method, name=None):
    r = _tool_result()
    d = r.get("d1") if str(method) == "1" else r.get("d2")
    if not d:
        await push_ack("calib_tool_apply", False, "no_result")
        return False
    name = name or state.tool().get("name")
    items = [dict(t) for t in state.get("tool.items") or []]
    hit = next((t for t in items if t.get("name") == name), None)
    if hit is None:
        await push_ack("calib_tool_apply", False, "no_tool")
        return False
    hit["dx_mm"], hit["dy_mm"] = float(d[0]), float(d[1])
    errs = state.save_settings({"tool.items": items})
    if errs:
        await push_ack("calib_tool_apply", False, "invalid", errors=errs)
        return False
    engine.refresh_sample_status()
    await push_log("공구 '%s' 오프셋 저장 · dX %.2f dY %.2f" % (name, d[0], d[1]), "ok")
    await push_ack("calib_tool_apply", True, tool=name, d=d)
    await push_state()
    return True


def pending_tool():
    return _tool_result()


def has_pending_head():
    return _pending.get("head") is not None


def head_calib_exists():
    return os.path.exists(paths.HEAD_CALIB_PATH)
