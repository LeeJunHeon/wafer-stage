"""engine.py - 촬영·검출과 샘플 순회(레시피 실행).

원칙
  - 좌표는 '그 프레임' 의 마커로 다시 보정해서 낸다(calib.sense). 마커가 모자라면
    좌표를 만들지 않고 멈춘다 - 틀린 좌표로 움직이는 것보다 낫다.
  - 펌웨어 [거부]·타임아웃·시리얼 오류는 즉시 중단한다(다음 샘플로 넘어가지 않는다).
  - 흐름은 설정이 조합한다(CAPTURE_FLOW · POST_RUN_FLOW). 순서를 코드에 박지 않는다.
  - 샘플마다 할 일은 레시피(recipe.py)가 정한다. 엔진은 단계를 차례로 실행만 한다.

안전 불변식(레시피와 무관하게 엔진이 강제한다)
  - X·Y 를 움직이기 전에는 항상 Z 를 맨 위(0)로 올린다(Z 원점이 있을 때).
  - Z 원점이 없으면 z 단계를 실행하지 않는다(그런 레시피는 순회 시작부터 거절).
  - 비상정지는 아무것도 움직이지 않고 save 도 하지 않는다. 릴레이 정책만 적용한다.

좌표 정의
  스테이지 좌표 P 는 '기준 포인터가 가리키는 점' 이다. 공구 끝(히트건은 가열 중심)은
  P + (dx, dy) 를 가리킨다. 샘플 S 를 공구로 겨냥하려면 P = S − (dx, dy) 로 보낸다.
  가동범위 검사는 P 로 한다(state.stage_target).
"""

import asyncio
import contextlib
import csv
import math
import os
import time

from core import calib, flat, imgio, paths, plate
from core import camera as camera_core
from core import fixed_detect, tip as tip_mod
import devices

import logger
import measure as measure_mod
import recipe as recipe_mod
import relays
import stagectl
import storage
import vision
from connection import push_ack, push_log, push_state, run_on_loop
from state import state

_task = None                      # 진행 중인 순회 태스크
_job = None                       # 순회 밖의 긴 작업(헤드 보정 · 공구 가열) 태스크
_next_evt = None                  # confirm 단계에서 '다음' 을 기다리는 이벤트
_pause_evt = None                 # 일시정지 해제 이벤트(set = 진행)
_stop = False                     # 정지 요청
_stop_evt = None                  # 정지·비상정지 시 set (대기·확인을 즉시 끊는다)
_estopped = False                 # 비상정지 상태(원점을 다시 잡을 때까지 유지)
_measurer = None

_last_move_at = 0.0
_save_done = True


class StepError(Exception):
    """단계를 실행할 수 없다(Z 원점 없음 · 릴레이 거절 · 위치 재확인 실패 등)."""


class SkipSample(Exception):
    """정책(접촉 못 찾음 · 온도 시간 초과)으로 이 샘플을 건너뛴다. 순회는 다음 샘플로 간다."""


class Stopped(Exception):
    """정지 요청으로 단계가 끊겼다."""


def mark_moved():
    """이동이 하나 끝났다. 조용해지면 저장한다(디바운스, 설정 autosave_s)."""
    global _last_move_at, _save_done
    _last_move_at = time.monotonic()
    _save_done = False


async def autosave_tick():
    """loops 가 1초마다 부른다. 조건이 맞을 때 한 번만 save 를 보낸다.

    USB 가 빠져 보드가 리셋되면(DTR) 마지막 save 이후의 위치는 사라진다 - 실제로
    원점을 잃었다. 비상정지 상태에서는 보내지 않는다(믿을 수 없는 위치를 굳히면 다음
    전원에서 그 틀린 값을 '복원' 한다). 결과는 파일 로그에만 남긴다.
    """
    global _save_done
    if _save_done or not _last_move_at:
        return
    if time.monotonic() - _last_move_at < float(state.get("autosave_s") or 3.0):
        return
    if _estopped or state.stage.get("moving") or not stagectl.ctl.connected:
        return
    _save_done = True                      # 실패해도 다시 쏟아내지 않는다
    try:
        for ln in await stagectl.ctl.save():
            if str(ln).strip():
                logger.write("info", "자동 저장: %s" % str(ln).strip())
        _apply_status(await stagectl.ctl.status())
        await push_state()
    except Exception as e:                 # noqa: BLE001
        logger.write("warn", "자동 저장 실패: %s" % logger.short(e))


def paused():
    """순회가 일시정지 상태인가(보정점 기록은 이때만 순회 중에 받는다)."""
    return busy() and _pause_evt is not None and not _pause_evt.is_set()


def busy():
    """순회 또는 긴 작업(보정·가열)이 도는 중인가."""
    return ((_task is not None and not _task.done())
            or (_job is not None and not _job.done()))


def run_job(kind, coro):
    """순회 밖의 긴 작업을 태스크로 띄운다(헤드 보정 · 공구 가열). 하나만."""
    global _job, _stop, _stop_evt
    _stop = False
    _stop_evt = asyncio.Event()
    state.job.update({"kind": kind, "running": True, "i": 0, "n": 0, "message": ""})

    async def _wrap():
        try:
            await coro
        finally:
            state.job["running"] = False
            await push_state()
    _job = asyncio.create_task(_wrap())
    return _job


def stopping():
    return _stop or _estopped


def _progress(message):
    """순회 중 진행 문구. 일시정지 요청이 들어와 있으면 phase 를 running 으로
    되돌리지 않는다 - 진행 중인 단계를 마치는 동안 화면이 '순회 중' 으로 보이면
    사용자는 일시정지가 먹지 않은 줄 안다."""
    paused = _pause_evt is not None and not _pause_evt.is_set()
    _phase("paused" if paused else "running", message)


def _phase(phase, message=None, **kw):
    state.sequence["phase"] = phase
    if message is not None:
        state.sequence["message"] = message
    state.sequence.update(kw)


# --------------------------------------------------------------------------
# 촬영 + 검출 - 흐름은 설정이 조합한다
# --------------------------------------------------------------------------
async def _cap_lift_z():
    if stagectl.ctl.connected and state.stage["homed_z"]:
        await lift_z()
    return True


async def _cap_goto():
    # 캐리지·히트건이 시야에 있으면 웨이퍼를 가린다. 원점이 있으면 촬영 위치로.
    if not stagectl.ctl.connected or state.can_move() is not None:
        return True
    px, py = state.capture_xy()
    if (state.stage["x_mm"] is None or abs(state.stage["x_mm"] - px) > 0.05
            or abs(state.stage["y_mm"] - py) > 0.05):
        await push_log("촬영 위치로 X%.1f Y%.1f" % (px, py))
        state.camera["cap_text"] = "촬영 위치로 이동 중"
        await push_state()
        await _goto(px, py, no="capture")
    return True


async def _cap_photo_check():
    if not stagectl.ctl.connected:
        return True
    import photohome
    await photohome.check(auto=True)
    return True                            # 확인 결과와 무관하게 촬영은 한다(검출 좌표는 판 마커 기준)


async def _cap_shoot():
    return await _shoot_detect()


# (이름, 켜고 끄는 설정 키 | None=항상)
CAPTURE_FLOW = [
    ("lift_z", _cap_lift_z, "flow.capture_lift_z"),
    ("goto_capture", _cap_goto, "flow.capture_goto"),
    ("photo_check", _cap_photo_check, "photohome.before_capture"),
    ("shoot", _cap_shoot, None),
]


_last_img = {"img": None}          # 마지막 촬영의 검출 이미지(수동 지정 스냅이 쓴다)


def _cap_progress(text):
    """촬영 스레드가 부르는 진행 문구. 상태 변경은 루프에서 하고 화면에 알린다."""
    def _go():
        if state.camera.get("capturing"):
            state.camera["cap_text"] = str(text)
            return push_state()
    run_on_loop(_go)


camera_core.progress_hook = _cap_progress


def _cap_fail(msg):
    """촬영 실패 한 줄 - 화면이 막 위에 2~3초 보여 준다(id 로 한 번만)."""
    state.camera["cap_error"] = {"id": int(time.time() * 1000), "text": str(msg)}


async def capture():
    if busy():
        await push_log("순회 중에는 촬영할 수 없습니다", "warn")
        return False
    state.camera["capturing"] = True
    state.camera["cap_text"] = "촬영 준비"
    state.camera["cap_error"] = None
    _phase("capturing", "촬영")
    await push_state()
    try:
        ok = True
        for _name, fn, key in CAPTURE_FLOW:
            if key and not state.get(key):
                continue
            ok = await fn()
            if ok is False:
                break
        return ok
    except stagectl.StageError as e:
        one = logger.short(e)
        state.stage["last_error"] = one
        _phase("error", "촬영 전 이동 실패 · " + one)
        _cap_fail("촬영 전 이동 실패 · " + one)
        logger.write("err", "촬영 전 이동 실패 상세: %s" % e)
        await push_log("촬영 전 이동 실패 · " + one, "err")
        return False
    finally:
        state.camera["capturing"] = False
        state.camera["cap_text"] = ""
        await push_state()


async def _shoot_detect():
    try:
        bgr, meta, res, stats = await vision.capture(state.params)
        state.camera["ok"] = True
        state.camera["last_error"] = ""
        state.frame = meta
        if isinstance(stats, dict) and "chosen_focus_score" in stats:
            req, got = stats.get("frames_requested"), stats.get("frames_grabbed", 0)
            if req and got < req:
                await push_log("프레임 요청 %d · 받음 %d · 받은 만큼으로 진행" % (req, got), "warn")
            await push_log("촬영 완료 · %d프레임 중 선명도 %.0f"
                           % (got, stats.get("chosen_focus_score", 0)))
        if isinstance(stats, dict) and stats.get("expo_note"):
            await push_log("노출 · " + stats["expo_note"])
        if isinstance(stats, dict) and stats.get("fuse_crop"):
            await push_log("융합 범위 · 감지 영역 %s" % (stats["fuse_crop"],))
        if isinstance(stats, dict) and stats.get("retries"):
            await push_log("촬영 재시도 %d회 · 프레임 끊김 복구" % stats["retries"], "warn")
        await _log_capture_diag(stats.get("diag") if isinstance(stats, dict) else None)
        timing = stats.get("timing") if isinstance(stats, dict) else None
        if res is None:
            await _log_timing(timing)
            state.samples = []
            _clear_edits()
            state.calib = state.sensing = state.wafer = None
            state.markers = {}
            state.head_marker = stats.get("head_marker") if isinstance(stats, dict) else None
            if fixed_mode():
                why = (stats.get("calib_invalid") if isinstance(stats, dict) else "") or "감지 영역 없음"
                missing = bool(isinstance(stats, dict) and stats.get("area_missing"))
                msg = "감지 영역 없음 · 보정 → 감지 영역 지정" if missing else "판 보정 무효 · 다시 보정"
                _phase("error", msg)
                _cap_fail(msg)
                state.set_alarm("판 보정", msg)
                await push_log("%s · %s · 검출을 건너뜁니다" % (msg.split(" · ")[0], why), "err")
                if isinstance(stats, dict) and stats.get("nocal"):
                    nd = await vision.run_blocking(_save_nocal, stats.get("raw", bgr),
                                                   stats.get("images") or {})
                    await push_log("촬영본 저장 · %s" % os.path.basename(nd))
            else:
                need = int(state.get("calib.min_markers") or 3)
                _phase("error", "판 마커 부족(%d개 미만) · 좌표 생성 불가" % need)
                _cap_fail("판 마커 부족 · 좌표 생성 불가")
                await push_log("판 마커 %d개 미만 · 좌표를 만들지 않습니다" % need, "err")
            return False
        _last_img["img"] = bgr
        _apply_sense(res)
        if res.get("mode") == "fixed" and res.get("mm_per_px"):
            if res.get("area_only"):
                await push_log("감지 영역 크기로 배율 %.4f mm/px (판 보정 전)" % res["mm_per_px"])
            else:
                await push_log("검출 배율 %.4f mm/px · 판 보정값" % res["mm_per_px"])
        state.head_marker = stats.get("head_marker") if isinstance(stats, dict) else None
        state.camera["cap_text"] = "저장 중"
        await push_state()
        t_sv = time.perf_counter()
        d = await vision.run_blocking(_save_seq, stats.get("raw", bgr), res,
                                      stats.get("images") or {})
        if timing is not None:
            timing["save_s"] = round(time.perf_counter() - t_sv, 3)
        await _log_timing(timing)
        state.sequence["out_dir"] = d
        state.sequence["used"] = False      # 새 촬영 - 아직 순회에 쓰지 않았다
        _edit_ctx["dir"] = d                # 이번 촬영의 편집본(samples_edit.json) 자리
        _phase("ready", ("검출 %d · 대기" if not (state.calib or {}).get("area_only")
                         else "검출 %d · 판 보정 필요(좌표 없음)") % len(state.samples),
               done=0, total=len(state.samples), cur_no=None, elapsed_s=0)
        await push_log("검출 %d개 · 저장 %s" % (len(state.samples), os.path.basename(d)), "ok")
        return True
    except Exception as e:                 # noqa: BLE001
        # 화면에는 한 줄만, 원문(여러 줄)은 파일 로그에만.
        one = logger.short(e)
        state.camera["ok"] = False
        state.camera["last_error"] = one
        _phase("error", "촬영 실패 · " + one)
        _cap_fail("촬영 실패 · " + one)
        logger.write("err", "촬영 실패 상세: %s: %s" % (type(e).__name__, e))
        await push_log("촬영 실패 · " + one, "err")
        return False


async def _log_timing(t):
    """촬영→검출 단계별 소요(고해상도에서 어디가 느린지 로그로 남긴다)."""
    if not t:
        return
    sz = t.get("size") or [0, 0]
    await push_log("소요 %dx%d · 촬영 %.2f s (노출 %.2f · 안정 대기 %.2f · 프레임 %.2f) · 융합 %.2f s"
                   " · 검출 %.2f s · 헤드 마커 %.2f s · 저장 %.2f s"
                   % (sz[0], sz[1], t.get("capture_s", 0), t.get("expo_s", 0),
                      t.get("settle_s", 0), t.get("frames_s", 0), t.get("fuse_s", 0),
                      t.get("detect_s", 0), t.get("head_s", 0), t.get("save_s", 0)))


async def _log_capture_diag(diag):
    """촬영마다 남기는 진단: 브라케팅·종이 RGB·포화 비율. 사진이 왜 그렇게
    나왔는지 나중에 로그만 보고 알 수 있어야 한다."""
    if not diag:
        return
    if diag.get("bracket_warning"):
        await push_log(diag["bracket_warning"], "warn")
    for note in diag.get("bracket_notes") or []:
        await push_log("브라케팅 · " + note)
    if diag.get("bracketed"):
        await push_log("브라케팅 융합 · 노출 %s · 장별 평균 밝기 %s"
                       % (" / ".join("%g" % e for e in (diag.get("bracket_exposure") or [])),
                          " / ".join("%.0f" % m for m in (diag.get("bracket_means") or []))))
    else:
        await push_log("단일 촬영(브라케팅 없음)")
    if diag.get("flat_applied"):
        await push_log("평탄화 · 종이 RGB %s -> %s (종이 %.0f%%)"
                       % (diag.get("paper_rgb_before"), diag.get("paper_rgb_after"),
                          (diag.get("paper_frac") or 0) * 100.0))
    elif diag.get("flat_warning"):
        await push_log("평탄화 없음 · " + diag["flat_warning"], "warn")
    if diag.get("rect_sat_pct") is not None:
        await push_log("감지영역 포화(254 이상) %.1f%%" % diag["rect_sat_pct"])
    ws = diag.get("wafer_sat_pct")
    if ws is not None:
        if ws > flat.WAFER_SAT_WARN:
            await push_log("반사광으로 정보가 사라진 영역 %.0f%% · 노출을 더 낮추거나 "
                           "편광 필터가 필요합니다" % ws, "warn")
        else:
            await push_log("웨이퍼 포화 %.1f%%" % ws)


def target_in_range(sx, sy):
    """샘플 S 를 활성 공구로 겨냥할 때 스테이지 자리 P 가 가동범위 안인가."""
    px, py = state.stage_target(sx, sy)
    return calib.in_range(px, py)


def mmpx_at(u, v):
    """(u, v) 근처의 사진 px 당 mm. 보정이 있으면 그 국소 배율, 없으면(검출만) 감지 영역 크기 배율. 모르면 None."""
    cal = current_cal()
    if cal is not None and u is not None:
        try:
            x0, y0 = calib.px_to_mm(cal, u, v)
            xu, yu = calib.px_to_mm(cal, u + 1.0, v)
            xv, yv = calib.px_to_mm(cal, u, v + 1.0)
            a, b = math.hypot(xu - x0, yu - y0), math.hypot(xv - x0, yv - y0)
            if a > 0 and b > 0:
                return math.sqrt(a * b)
        except Exception:                  # noqa: BLE001
            pass
    mp = (state.sensing or {}).get("mm_px")
    return float(mp) if mp else None


def jac_at(u, v, cal=None):
    """(u, v) 에서 사진 px → 스테이지 mm 의 야코비안 [[dx/du, dx/dv], [dy/du, dy/dv]]. 판 보정이 없으면 None."""
    cal = cal or current_cal()
    if cal is None or u is None:
        return None
    try:
        x0, y0 = calib.px_to_mm(cal, u, v)
        xu, yu = calib.px_to_mm(cal, u + 1.0, v)
        xv, yv = calib.px_to_mm(cal, u, v + 1.0)
        return [[xu - x0, xv - x0], [yu - y0, yv - y0]]
    except Exception:                      # noqa: BLE001
        return None


def _area_center_px():
    q = (state.sensing or {}).get("quad")
    if q:
        return sum(p[0] for p in q) / len(q), sum(p[1] for p in q) / len(q)
    r = (state.sensing or {}).get("rect")
    if r:
        return (r[0] + r[2]) / 2.0, (r[1] + r[3]) / 2.0
    return None, None


def _refresh_tip_now():
    """공구마다 '지금 사진에서의 방향'(감지 영역 가운데 기준)을 state.tip_extra 에 둔다(공구 탭 미리보기 · 추가 모드 유령)."""
    u, v = _area_center_px()
    jac = jac_at(u, v) if u is not None else None
    out = {}
    for t in state.get("tool.items") or []:
        if isinstance(t, dict):
            out[str(t.get("name"))] = round(tip_mod.img_angle(tip_mod.tip_of(t), jac), 1)
    state.tip_extra = {"angle_now": out.get(str(state.tool().get("name"))), "tips_now": out,
                       "has_cal": jac is not None}


def migrate_tip_angles():
    """판 보정이 생기면 사진 기준 방향(tip_angle_img_deg, 예전 설정은 tip_angle_deg)에 해당하는 스테이지 각도를
    tip_angle_deg 에 한 번 채운다. 반환: 채운 공구 이름 목록."""
    u, v = _area_center_px()
    if u is None:
        d = plate.load()
        if d and d.get("area"):
            q = d["area"]
            u, v = sum(p[0] for p in q) / len(q), sum(p[1] for p in q) / len(q)
    jac = jac_at(u, v) if u is not None else None
    if jac is None:
        return []
    items = [dict(t) for t in (state.get("tool.items") or []) if isinstance(t, dict)]
    done = []
    for t in items:
        info = tip_mod.tip_of(t)
        if info["angle"] is not None:
            continue
        stage = tip_mod.stage_angle_of(info["angle_img"], jac)
        t["tip_angle_img_deg"] = info["angle_img"]
        t["tip_angle_deg"] = round(stage, 1)
        done.append((t.get("name"), info["angle_img"], t["tip_angle_deg"]))
    if not done:
        return []
    errs = state.save_settings({"tool.items": items})
    if errs:
        logger.write("warn", "공구 끝 방향 이행 저장 실패: %s" % errs)
        return []
    for nm, img, st in done:
        logger.write("info", "공구 '%s' 끝 방향 · 사진 %g° → 스테이지 %g° 로 고정" % (nm, img, st))
        run_on_loop(lambda nm=nm, img=img, st=st: push_log(
            "공구 '%s' 끝 방향 · 사진 %g° → 스테이지 %g° 로 고정" % (nm, img, st), "ok"))
    return [d[0] for d in done]


def zone_of(u, v):
    """감지 영역 사각형을 rows x cols 로 나눈 칸 (row, col) · 쿨러 릴레이 번호. 사진 기준 위→아래 · 왼쪽→오른쪽(행 우선)."""
    q = (state.sensing or {}).get("quad")
    z = state.get("cooler.zones") or {}
    if not q or not z:
        return None
    try:
        import cv2
        import numpy as np
        pts = np.asarray(q, np.float32).reshape(4, 2)
        sm, df = pts.sum(axis=1), pts[:, 0] - pts[:, 1]
        tl, br, tr, bl = pts[int(np.argmin(sm))], pts[int(np.argmax(sm))], pts[int(np.argmax(df))], pts[int(np.argmin(df))]
        M = cv2.getPerspectiveTransform(np.float32([tl, tr, br, bl]), np.float32([[0, 0], [1, 0], [1, 1], [0, 1]]))
        x, y = cv2.perspectiveTransform(np.float32([[[u, v]]]), M)[0][0]
        rows, cols = int(z["rows"]), int(z["cols"])
        r, c = min(rows - 1, max(0, int(y * rows))), min(cols - 1, max(0, int(x * cols)))
        return r, c, int(z["relays"][r * cols + c])
    except Exception:                      # noqa: BLE001
        return None


def assign_coolers():
    """샘플마다 속한 구역의 쿨러 번호(cooler)와 칸(zone). 수동으로 바꾼 샘플(cooler_manual)은 건드리지 않는다."""
    for s in state.samples:
        if s.get("cooler_manual"):
            continue
        r = zone_of(s.get("u"), s.get("v")) if s.get("u") is not None else None
        s["cooler"] = None if r is None else r[2]
        s["zone"] = None if r is None else [r[0], r[1]]


async def set_sample_cooler(no, n):
    """샘플의 쿨러를 손으로 바꾼다(n 비움/0 = 구역 기준으로 되돌림)."""
    s = state.sample(no)
    if s is None:
        await push_log("#%s 없음" % no, "warn")
        return False
    if not n:
        s["cooler_manual"] = False
        assign_coolers()
    else:
        n = int(n)
        if not 1 <= n <= 10 or n == heater_n():
            await push_log("쿨러 번호 오류 (R%s)" % n, "warn")
            return False
        s["cooler"], s["cooler_manual"] = n, True
    await push_log("샘플 #%s 쿨러 R%s%s" % (no, s["cooler"], " (수동)" if s.get("cooler_manual") else ""))
    await push_state()
    return True


def refresh_fit():
    """샘플마다 사진 배율(mmpx)과 공구 끝 맞음(fit: True 안 / False 벗어남 / None 검사 안 함)을 다시 계산한다.
    예측 샘플 · 윤곽 없는 수동 샘플은 검사하지 않는다."""
    t = tip_mod.tip_of(state.tool())
    margin = float(state.get("tool.fit_margin_mm") or 0.0)
    for s in state.samples:
        mp = mmpx_at(s.get("u"), s.get("v"))
        s["mmpx"] = None if not mp else round(mp, 5)
        ang = tip_mod.img_angle(t, jac_at(s.get("u"), s.get("v")))
        s["tip_deg"] = round(ang, 1)
        if not mp or s.get("source") == "predicted" or not s.get("verts"):
            s["fit"] = None
            continue
        s["fit"] = tip_mod.fits(tip_mod.polygon(t, mp, s["u"], s["v"], angle_deg=ang), s["verts"], margin / mp)
    _refresh_tip_now()
    assign_coolers()


def refresh_sample_status():
    """공구(오프셋)가 바뀌면 가동범위 밖(skip) 판정을 다시 한다. 대기·제외만 바꾼다."""
    for s in state.samples:
        if s["status"] in ("wait", "skip") and s.get("X") is not None:
            s["status"] = "wait" if target_in_range(s["X"], s["Y"]) else "skip"
    refresh_fit()


def _apply_sense(res):
    """calib.sense 결과를 화면 state 로 옮긴다."""
    cal, det = res["cal"], res["det"]
    area_only = cal is None                # 검출만 모드: 판 보정이 없어 좌표가 없다
    if area_only:
        cal = {}
    else:
        remember_cal(cal)                  # 포인터 역변환에 같은 보정을 쓴다
    used = list(cal.get("used_ids") or [])
    fixed = res.get("mode") == "fixed"
    state.calib = {
        "mode": "fixed" if fixed else "markers", "area_only": area_only,
        "refit": bool(res["refit"]),
        "used_ids": used,
        "missing_ids": [] if fixed else [i for i in sorted(calib.MARKER_MM) if i not in used],
        "transform": cal.get("transform"),
        "corner_rms_mm": round(float(cal.get("corner_rms_mm", 0.0)), 3),
        "corner_max_mm": round(float(cal.get("corner_max_mm", 0.0)), 3),
        "rotation_deg": round(float(cal.get("rotation_deg", 0.0)), 2),
        "corners": len(cal.get("corners_px") or []),
    }
    u0, v0, u1, v1 = res["rect"]
    state.sensing = {"rect": [int(u0), int(v0), int(u1), int(v1)],
                     "quad": res.get("quad"),
                     "mm_px": res.get("mm_per_px") if fixed else None,
                     "chip_px": (getattr(det, "diag", None) or {}).get("chip_long_px")}
    w = det.wafer
    if w.found and not area_only:
        cx, cy = w.center_px[0] + u0, w.center_px[1] + v0
        state.wafer = {"found": True, "cx": round(cx, 1), "cy": round(cy, 1),
                       "r_px": round(w.major_px / 2.0, 1),
                       "center_mm": [round(v, 2) for v in calib.px_to_mm(cal, cx, cy)]}
    else:
        state.wafer = {"found": False, "cx": 0, "cy": 0, "r_px": 0, "center_mm": None}
    # corners 는 numpy 배열이라 `or []` 로 기본값을 주면 "truth value is ambiguous"
    # 로 터진다. None 검사를 명시적으로 한다.
    state.markers = {}
    state.set_alarm("판 보정", "")         # 이번 촬영은 좌표가 나왔다
    for i in used:
        q = (cal.get("markers") or {}).get(int(i), {}).get("corners")
        if q is None:
            continue
        state.markers[int(i)] = [[round(float(x), 1), round(float(y), 1)] for x, y in q]
    mm_by_no = {r[0]: (r[3], r[4]) for r in res["rows"]}
    # 체크 상태는 검출 샘플끼리만 넘긴다. 수동 번호(N+1…)가 다음 촬영에서 같은
    # 번호를 받은 검출 샘플에 체크 해제를 물려주면 안 된다.
    old_on = {s["no"]: s.get("on", True) for s in state.samples if not s.get("manual")}
    _clear_edits()
    state.samples = []
    for s in det.samples:
        X, Y = mm_by_no.get(s["no"], (0.0, 0.0))
        state.samples.append({
            "no": s["no"], "shape": s.get("shape", ""),
            "u": float(s["x_px"]), "v": float(s["y_px"]),
            "X": None if X is None else round(float(X), 2),
            "Y": None if Y is None else round(float(Y), 2),
            "verts": [[int(a), int(b)] for a, b in (s.get("vertices_px") or [])],
            "on": bool(old_on.get(s["no"], True)),
            "status": ("nocoord" if X is None else
                       "wait" if target_in_range(X, Y) else "skip"),
            "value": None, "unit": None,
            "edge_completed": bool(s.get("edge_completed")),
            "edge_only": bool(s.get("edge_only")),
            "weak": bool(s.get("weak")),
            "source": s.get("source") or "detected",
            "strength": s.get("strength"),
            "area_mm2": s.get("area_mm2"),
            "manual": False,
        })
    state.next_no = max([s["no"] for s in state.samples], default=0) + 1
    refresh_fit()
    state.set_warnings(list(det.warnings) + (["판 보정 없음 · 검출만 · 좌표 없음 · 보정 → 판 보정"]
                                             if area_only else []))


def _save_nocal(bgr, images):
    """감지 영역이 없어 검출하지 못한 촬영본 - data/out/<시각>_nocal/ 에 raw(+fused)를 남긴다.
    보정 창의 감지 영역 탭이 최근 촬영본(state.frame)으로 바로 쓴다."""
    d = os.path.join(paths.OUT_DIR, time.strftime("%Y%m%d_%H%M%S") + "_nocal")
    os.makedirs(d, exist_ok=True)
    imgio.imwrite_u(os.path.join(d, "raw.png"), bgr)
    if images.get("fused.png") is not None:
        imgio.imwrite_u(os.path.join(d, "fused.png"), images["fused.png"])
    logger.write("info", "감지 영역 없음 · 촬영본만 저장: %s" % d)
    return d


def _save_seq(bgr, res, images=None):
    """이번 촬영의 근거를 한 폴더에 남긴다.

    bgr 은 raw.png 로 남길 원본(브라케팅이면 첫 장), images 는 그 밖에 남길
    그림 {"fused.png": .., "flat.png": ..}. 검출은 flat(없으면 fused) 로 했다.
    """
    d = os.path.join(paths.OUT_DIR, "seq_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    # save_samples_image 가 raw_name 으로 원본도 함께 쓴다(따로 imwrite 하면 2MB
    # 짜리 PNG 를 두 번 인코딩하게 된다).
    calib.save_samples_image(bgr, res, state.params, outdir=d,
                             ann_name="annotated.jpg", raw_name="raw.png")
    for name, img in (images or {}).items():
        imgio.imwrite_u(os.path.join(d, name), img)
    storage.atomic_write_json(
        os.path.join(d, "samples.json"),
        [{"no": no, "u": round(u, 1), "v": round(v, 1),
          "X": None if x is None else round(x, 2), "Y": None if y is None else round(y, 2)}
         for no, u, v, x, y in res["rows"]])
    return d


# --------------------------------------------------------------------------
# 이동
# --------------------------------------------------------------------------
def heater_n():
    try:
        return int(state.get("heater.relay") or 10)
    except (TypeError, ValueError):
        return 10


async def heater_off(why=""):
    """히트건 릴레이를 끈다(이미 꺼져 있으면 아무 일도 안 한다). 실패해도 예외를 내지 않는다."""
    try:
        n = heater_n()
        if 1 <= n <= len(state.relays) and not state.relays[n - 1]["on"]:
            return True
        ok, _r = await relays.set_relay(n, False, by_engine=True)
        if ok and why:
            await push_log("히트건 끔 · " + why, "warn")
        return ok
    except Exception as e:                 # noqa: BLE001
        logger.write("warn", "히트건 끄기 실패: %s" % e)
        return False


async def _goto(x_mm, y_mm, no=None):
    """X·Y 이동 한 번(검사 없음). Z 상승은 부르는 쪽(goto_xy · 단계)이 먼저 한다.

    안전 불변식: X·Y 이동 전에는 히트건이 반드시 꺼져 있다(여기서 강제)."""
    await heater_off("X·Y 이동 전")
    # 성공한 이동은 옛 오류 문구를 지운다(배너에 지난 오류가 남지 않게).
    state.stage["last_error"] = ""
    state.stage["moving"] = True
    await push_state()
    try:
        reports = await stagectl.ctl.goto(x_mm, y_mm)
        storage.append_jsonl(paths.SEQ_LOG_PATH, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sample_no": no,
            "target_mm": [round(float(x_mm), 2), round(float(y_mm), 2)],
            "report": [r.strip() for r in reports]})
        st = await stagectl.ctl.status()
        _apply_status(st)
        mark_moved()
        return reports
    finally:
        state.stage["moving"] = False


def _apply_status(st):
    z = st.get("z_mm")
    state.stage.update({
        "x_mm": round(st["x_mm"], 2), "y_mm": round(st["y_mm"], 2),
        "z_mm": None if z is None else round(z, 2),
        "homed_x": st["homed_x"], "homed_y": st["homed_y"],
        "homed_z": bool(st.get("homed_z")), "dirty": st["dirty"],
    })
    _update_pointer_px()


def _update_pointer_px():
    """포인터 픽셀 위치. 보정이 있으면 기계좌표를 역변환해서 화면에 찍는다."""
    d = _last_cal()
    if d is None or state.stage["x_mm"] is None:
        state.stage["u"] = state.stage["v"] = None
        return
    try:
        u, v = calib.mm_to_px(d, state.stage["x_mm"], state.stage["y_mm"])
        state.stage["u"], state.stage["v"] = round(u, 1), round(v, 1)
    except Exception:                      # noqa: BLE001
        state.stage["u"] = state.stage["v"] = None


_CAL_CACHE = {"d": None}


def remember_cal(d):
    _CAL_CACHE["d"] = d


def fixed_mode():
    return str(state.get("calib.mode") or "fixed") == "fixed"


def fixed_cal():
    """고정 보정의 현재 변환(판 보정이 유효할 때만, 아니면 None)."""
    if not (state.plate or {}).get("valid"):
        return None
    return plate.current_cal()


def _last_cal():
    if fixed_mode():
        return fixed_cal()                 # 저장한 판 보정이 곧 현재 보정이다
    if _CAL_CACHE["d"] is not None:
        return _CAL_CACHE["d"]
    return calib.load_matrix()


def current_cal():
    """지금 쓰는 보정(없으면 None). 촬영본 클릭 → mm 변환에 쓴다.

    markers: 이번 촬영의 마커 보정. fixed: 저장한 판 보정(촬영과 무관하게 같다)."""
    if fixed_mode():
        return fixed_cal()
    return _CAL_CACHE["d"]


async def goto_sample(no):
    """선택 샘플 이동. 활성 공구 오프셋을 적용한다(P = S − d)."""
    s = state.sample(no)
    if s is None:
        await push_log("#%s 는 이번 검출에 없습니다" % no, "warn")
        return False
    if s.get("X") is None:
        await push_log("#%s · 판 보정 필요 · 좌표 없음" % no, "warn")
        return False
    px, py = state.stage_target(s["X"], s["Y"])
    t = state.tool()
    if t.get("dx_mm") or t.get("dy_mm"):
        await push_log("#%s · %s 오프셋 → 스테이지 X%.1f Y%.1f" % (no, t["name"], px, py))
    return await goto_xy(px, py, no=no)


async def goto_xy(x_mm, y_mm, no=None):
    """X·Y 절대 이동. 불변식대로 Z 를 먼저 맨 위로 올린다."""
    why = state.can_move()
    if why:
        await push_log(why, "warn")
        return False
    if not calib.in_range(x_mm, y_mm):
        await push_log("(%.1f, %.1f) · 가동범위 %s mm 밖"
                       % (x_mm, y_mm, calib.range_text()), "warn")
        return False
    try:
        await lift_z()
        await _goto(x_mm, y_mm, no=no)
        await push_log("이동 완료 X%.1f Y%.1f%s"
                       % (x_mm, y_mm, "" if no is None else " (#%s)" % no), "ok")
        return True
    except stagectl.StageError as e:
        one = logger.short(e)
        state.stage["last_error"] = one
        logger.write("err", "이동 실패 상세: %s" % e)
        await push_log("이동 실패 · " + one, "err")
        return False
    finally:
        await push_state()


async def jog(axis, target_mm=None, delta_mm=None):
    """한 축만 옮긴다(수동 이동 팝업).

    원점이 있으면 절대 좌표로(target_mm, 서버가 가동범위로 자른 값), 없으면
    상대 이동으로(delta_mm) 간다. 원점 없이 절대 좌표를 말할 수는 없고, 그렇다고
    못 움직이게 하면 사람이 캐리지를 끝단까지 몰고 갈 방법이 없다.

    이미 이동 중이면 거절한다 - 조그를 큐에 쌓으면 손을 뗀 뒤에도 계속 움직인다.
    조그는 Z 를 올리지 않는다 - 공구 끝을 표시점에 맞출 때 Z 를 내린 채 조금씩 민다.
    """
    rel = delta_mm is not None
    ax = str(axis).lower()
    if ax not in ("x", "y", "z"):
        await push_ack("jog", False, "bad_axis")
        return False
    # Z 는 X·Y 원점 잠금과 무관하다 - 연결만 있으면 움직인다.
    if rel or ax == "z":
        if not stagectl.ctl.connected:
            await push_log("스테이지 미연결 · 연결 후 사용하세요", "warn")
            await push_ack("jog", False, "locked")
            return False
    else:
        why = state.can_move()
        if why:
            await push_log(why, "warn")
            await push_ack("jog", False, "locked")
            return False
    if state.stage["moving"]:
        await push_ack("jog", False, "moving")
        return False
    amount = float(delta_mm if rel else target_mm)
    state.stage["last_error"] = ""
    state.stage["moving"] = True
    await push_state()
    try:
        if rel:
            # 속도는 드라이버가 맞춘다(jog_rel 은 원점 없을 때 조그 속도).
            report = await stagectl.ctl.jog_rel(ax, amount)
        else:
            mover = {"x": stagectl.ctl.move_x, "y": stagectl.ctl.move_y,
                     "z": stagectl.ctl.move_z}[ax]
            report = await mover(amount, stagectl.stage_mod.JOG_PPS)
        storage.append_jsonl(paths.SEQ_LOG_PATH, {
            "time": time.strftime("%Y-%m-%d %H:%M:%S"), "sample_no": "jog",
            "mode": "rel" if rel else "abs", "axis": ax, "amount_mm": round(amount, 2),
            "report": [str(report).strip()]})
        _apply_status(await stagectl.ctl.status())
        mark_moved()
        ok = True
    except stagectl.StageError as e:
        ok = False
        state.stage["last_error"] = logger.short(e)
        logger.write("err", "수동 이동 실패 상세(%s %.2f): %s" % (ax, amount, e))
        await push_log("수동 이동 실패 · " + logger.short(e), "err")
    finally:
        state.stage["moving"] = False
        await push_state()
    await push_ack("jog", ok, "" if ok else "error",
                   x_mm=state.stage["x_mm"], y_mm=state.stage["y_mm"])
    return ok


async def lift_z():
    """X·Y 를 움직이기 전에 Z 를 맨 위(0)로 올린다(안전 불변식).

    Z 가 내려가 있는 채로 X·Y 가 움직이면 히트건·프로브가 시료를 긁는다.
    Z 원점이 없으면 0 이 어디인지 모르므로 경고만 남기고 X·Y 는 그대로 간다
    (z 단계가 있는 레시피는 순회 시작에서 이미 거절한다).
    이동 실패(비상정지 포함)는 그대로 던진다 - 호출자가 X·Y 를 보내면 안 된다.
    """
    if not state.stage["homed_z"]:
        await push_log("Z 원점 없음 · Z 는 두고 이동", "warn")
        return False
    if state.stage["z_mm"] in (0, 0.0):
        return True                        # 이미 맨 위다
    await move_z(0.0)
    await push_log("Z 맨 위로", "ok")
    return True


async def move_z(z_mm):
    """Z 절대 이동(원점·범위 검사는 부르는 쪽)."""
    state.stage["moving"] = True
    await push_state()
    try:
        await stagectl.ctl.move_z(z_mm)
        _apply_status(await stagectl.ctl.status())
        mark_moved()
    finally:
        state.stage["moving"] = False
        await push_state()


async def park():
    px, py = state.park_xy()
    return await goto_xy(px, py, no="park")


async def return_origin():
    """원점 복귀. Z 를 먼저 맨 위로 올리고 X·Y 를 (0, 0) 으로."""
    why = state.can_move()
    if why:
        await push_log(why, "warn")
        return False
    await push_log("원점 복귀 · Z0 → X0 Y0")
    return await goto_xy(0.0, 0.0, no="home")


# --------------------------------------------------------------------------
# 샘플 편집(수동 추가 · 삭제 · 되돌리기)
# --------------------------------------------------------------------------
# 번호는 다시 매기지 않는다 - annotated.jpg · results.csv · 로그의 번호가 서로
# 어긋나지 않게. 지운 번호는 빈 번호로 남고, 추가는 이번 촬영에서 한 번도 안 쓴
# 다음 번호(state.next_no)를 받는다. 좌표 변환은 이번 촬영의 보정(_CAL_CACHE)만
# 쓴다 - _last_cal() 은 저장된 옛 보정으로 떨어질 수 있어 편집에는 쓰지 않는다.

# 기존 샘플 중심에서 이 안이면 같은 칩으로 본다(tools/flat_check.py 의 적중 기준
# TRUTH_MM 과 같은 값).
EDIT_OVERLAP_MM = 2.5
_edit_ctx = {"dir": None}          # 이번 촬영의 결과 폴더(편집본을 쓰는 곳)


def _clear_edits():
    state.edits = []
    _edit_ctx["dir"] = None


def _source(s):
    return "manual" if (s.get("manual") or s.get("source") == "manual") else "detect"


def _edit_refusal(verb):
    """편집을 받을 수 없는 사유 (reason, 화면 문구) 또는 None."""
    if busy():
        return "busy", "순회 중 · %s 안 함" % verb
    if state.camera.get("capturing"):
        return "capturing", "촬영 중 · %s 안 함" % verb
    if state.calib is None or state.sensing is None:
        return "no_capture", "촬영 결과 없음 · 촬영 후 %s" % verb
    if current_cal() is None and not state.calib.get("area_only"):
        return "no_capture", "촬영 결과 없음 · 촬영 후 %s" % verb
    return None                            # 검출만 모드(판 보정 전)도 픽셀 좌표로 편집한다


def _in_rect(u, v):
    quad = state.sensing.get("quad")
    if quad:                               # 고정 보정: 감지 영역은 사각형
        import cv2
        import numpy as np
        return cv2.pointPolygonTest(np.asarray(quad, np.float32).reshape(-1, 1, 2),
                                    (float(u), float(v)), False) >= 0
    u0, v0, u1, v1 = state.sensing["rect"]
    return u0 <= u <= u1 and v0 <= v <= v1


def _in_poly(u, v, verts):
    """점이 다각형 안인가(광선 교차). verts = [[u, v], ...] 프레임 px."""
    inside = False
    n = len(verts)
    for i in range(n):
        (a, b), (c, d) = verts[i], verts[(i + 1) % n]
        if (b > v) != (d > v) and u < (c - a) * (v - b) / (d - b) + a:
            inside = not inside
    return inside


def _overlap(u, v, skip_no=None):
    """겹치는 기존 샘플 번호 또는 None. 중심 2.5 mm 안(픽셀 거리 x 배율)이거나 검출 윤곽 안."""
    for s in state.samples:
        if s["no"] == skip_no:
            continue
        mp = mmpx_at(u, v) or 0.15
        if math.hypot(s["u"] - u, s["v"] - v) * mp <= EDIT_OVERLAP_MM:
            return s["no"]
        verts = s.get("verts") or []
        if len(verts) >= 3 and _in_poly(u, v, verts):
            return s["no"]
    return None


def _xy(u, v):
    """(X, Y) mm — 판 보정이 없으면 (None, None)."""
    cal = current_cal()
    if cal is None:
        return None, None
    return calib.px_to_mm(cal, u, v)


def _add_now(u, v, X, Y, snapped=None):
    """번호 → 목록 → 스택을 await 없이 한 번에. 명령마다 태스크라 사이에 await 가
    끼면 빠른 두 번 클릭이 같은 번호를 받는다."""
    no = state.next_no
    state.next_no += 1
    s = {
        "no": no, "shape": "",
        "u": round(float(u), 1), "v": round(float(v), 1),
        "X": None if X is None else round(float(X), 2),
        "Y": None if Y is None else round(float(Y), 2),
        "verts": [],
        "on": True,
        "status": "nocoord" if X is None else ("wait" if target_in_range(X, Y) else "skip"),
        "value": None, "unit": None,
        "edge_completed": False, "edge_only": False, "weak": False,
        "strength": None, "area_mm2": None,
        "manual": True, "source": "manual",
    }
    if snapped:                            # 스냅으로 칩 윤곽을 찾았다 - 윤곽 · 모양을 달아 맞음 검사가 된다
        s.update({"shape": snapped.get("shape", ""), "verts": snapped.get("vertices_px") or [],
                  "strength": snapped.get("strength"), "area_mm2": snapped.get("area_mm2")})
    state.samples.append(s)
    state.samples.sort(key=lambda x: x["no"])
    state.edits.append({"op": "add", "no": no})
    refresh_fit()
    return s


def _check_new(u, v, X, Y):
    """추가 전 검사. 거절이면 (reason, 화면 문구), 통과면 None."""
    if not _in_rect(u, v):
        return "outside", "감지영역 밖 · 추가 안 함"
    if X is not None and not target_in_range(X, Y):
        return "out_of_range", "가동범위 밖 · 추가 안 함"
    hit = _overlap(u, v)
    if hit is not None:
        return "overlap", "#%s 과 겹침 · 추가 안 함" % hit
    return None


async def _add_refused(reason, msg):
    await push_log(msg, "warn")
    await push_ack("sample_add", False, reason, no=None)
    return False


def _after_edit(kind, s, **extra):
    """편집마다: 편집본 파일 · 순회 기록 한 줄 · 준비 상태의 개수."""
    d = _edit_ctx["dir"]
    if d:
        try:
            storage.atomic_write_json(
                os.path.join(d, "samples_edit.json"),
                [{"no": x["no"], "u": round(x["u"], 1), "v": round(x["v"], 1),
                  "X": None if x["X"] is None else round(x["X"], 2),
                  "Y": None if x["Y"] is None else round(x["Y"], 2), "source": _source(x)}
                 for x in state.samples])
        except OSError as e:
            logger.write("warn", "samples_edit.json 저장 실패: %s" % e)
    rec = {"time": time.strftime("%Y-%m-%d %H:%M:%S"), "edit": kind, "no": s["no"],
           "u": s["u"], "v": s["v"], "X": s["X"], "Y": s["Y"]}
    rec.update(extra)
    storage.append_jsonl(paths.SEQ_LOG_PATH, rec)
    if state.sequence.get("phase") == "ready":
        _phase("ready", "샘플 %d · 대기" % len(state.samples), total=len(state.samples))


async def _added(s, via):
    _after_edit("add", s, via=via)
    await push_log("수동 샘플 #%d 추가 · %s" % (s["no"], "X%.1f Y%.1f" % (s["X"], s["Y"])
                                                if s["X"] is not None else "픽셀 좌표(판 보정 전)"), "ok")
    await push_state()                     # 샘플이 목록에 있는 state 가 ack 보다 먼저
    await push_ack("sample_add", True, no=s["no"])
    return True


async def add_sample_px(u, v):
    """촬영본 클릭 자리(프레임 px)를 샘플(프로브 목표점)로 추가한다."""
    why = _edit_refusal("추가")
    if why:
        return await _add_refused(*why)
    try:
        u, v = float(u), float(v)
    except (TypeError, ValueError):
        return await _add_refused("bad_args", "좌표 오류 · 추가 안 함")
    if not (math.isfinite(u) and math.isfinite(v)):
        return await _add_refused("bad_args", "좌표 오류 · 추가 안 함")
    if not _in_rect(u, v):
        return await _add_refused("outside", "감지영역 밖 · 추가 안 함")
    snapped = None
    if bool(state.get("sample_add.snap", True)):
        snapped = await _snap(u, v)
        if snapped:
            nu, nv = snapped["x_px"], snapped["y_px"]
            mp = mmpx_at(u, v) or 0.0
            await push_log("스냅 · 칩 중심으로 %.1f mm 이동" % (math.hypot(nu - u, nv - v) * mp), "ok")
            u, v = nu, nv
        else:
            await push_log("스냅 · 칩을 못 찾음 · 클릭한 자리 그대로")
    X, Y = _xy(u, v)
    why = _check_new(u, v, X, Y)
    if why:
        return await _add_refused(*why)
    return await _added(_add_now(u, v, X, Y, snapped), "click")


async def _snap(u, v):
    """클릭 주변 칩 찾기(fixed 모드 · 고정 환경 검출기일 때만). 샘플 dict | None."""
    img = _last_img["img"]
    mp = mmpx_at(u, v)
    if img is None or not mp or not fixed_mode():
        return None
    if str((state.params.get("fixed") or {}).get("detector", "fixed")) == "legacy":
        return None
    try:
        return await vision.run_blocking(fixed_detect.snap, img, u, v, state.params, mp,
                                         (state.sensing or {}).get("chip_px"))
    except Exception as e:                 # noqa: BLE001
        logger.write("warn", "스냅 실패: %s" % e)
        return None


async def move_sample(no, u, v):
    """샘플 중심을 (u, v)로 옮긴다(끌기). 편집 스택에 기록 · 되돌리기 가능 · source = manual."""
    why = _edit_refusal("이동")
    if why:
        await push_log(why[1], "warn")
        await push_ack("sample_move", False, why[0])
        return False
    try:
        no, u, v = int(no), float(u), float(v)
    except (TypeError, ValueError):
        await push_ack("sample_move", False, "bad_args")
        return False
    s = state.sample(no)
    if s is None or not (math.isfinite(u) and math.isfinite(v)):
        await push_log("#%s 없음 · 이동 안 함" % no, "warn")
        await push_ack("sample_move", False, "bad_args")
        return False
    if not _in_rect(u, v):
        await push_log("감지영역 밖 · 이동 안 함", "warn")
        await push_ack("sample_move", False, "outside")
        return False
    X, Y = _xy(u, v)
    if X is not None and not target_in_range(X, Y):
        await push_log("가동범위 밖 · 이동 안 함", "warn")
        await push_ack("sample_move", False, "out_of_range")
        return False
    hit = _overlap(u, v, skip_no=no)
    if hit is not None:
        await push_log("#%s 과 겹침 · 이동 안 함" % hit, "warn")
        await push_ack("sample_move", False, "overlap")
        return False
    keep = {k: s.get(k) for k in ("u", "v", "X", "Y", "status", "source")}
    s.update({"u": round(u, 1), "v": round(v, 1),
              "X": None if X is None else round(float(X), 2),
              "Y": None if Y is None else round(float(Y), 2), "source": "manual"})
    if s["X"] is None:
        s["status"] = "nocoord"
    elif s["status"] in ("wait", "skip", "nocoord"):
        s["status"] = "wait" if target_in_range(s["X"], s["Y"]) else "skip"
    state.edits.append({"op": "move", "no": no, "old": keep})
    refresh_fit()
    _after_edit("move", s)
    await push_log("샘플 #%d 이동 · 중심 (%.0f, %.0f) px" % (no, s["u"], s["v"]), "ok")
    await push_state()
    await push_ack("sample_move", True, no=no)
    return True


def recalc_from_plate():
    """판 보정이 새로 생기면(적용) 저장된 픽셀 좌표로 샘플 mm 를 계산한다(검출만 모드에서 편집한 것 포함).
    반환: 계산한 샘플 수."""
    cal = current_cal()
    if cal is None or state.calib is None:
        return 0
    n = 0
    for s in state.samples:
        X, Y = calib.px_to_mm(cal, s["u"], s["v"])
        s["X"], s["Y"] = round(float(X), 2), round(float(Y), 2)
        if s["status"] in ("nocoord", "wait", "skip"):
            s["status"] = "wait" if target_in_range(X, Y) else "skip"
        n += 1
    state.calib["area_only"] = False
    refresh_fit()
    return n


async def add_sample_here():
    """지금 스테이지 위치를 샘플로 추가한다. 포인터를 칩에 맞춘 뒤라 보정 오차가
    끼지 않는다(사진에 거의 안 보이는 칩용). 원점·연결은 commands 가 막는다."""
    why = _edit_refusal("추가")
    if why:
        return await _add_refused(*why)
    if current_cal() is None:
        return await _add_refused("no_calib", "판 보정 필요 · 추가 안 함")
    X, Y = state.stage["x_mm"], state.stage["y_mm"]
    if X is None or Y is None:
        return await _add_refused("bad_args", "현재 위치 모름 · 추가 안 함")
    X, Y = float(X), float(Y)
    u, v = calib.mm_to_px(current_cal(), X, Y)
    why = _check_new(u, v, X, Y)
    if why:
        return await _add_refused(*why)
    return await _added(_add_now(u, v, X, Y), "here")


async def delete_sample(no):
    """샘플을 목록에서 뺀다. 지운 dict 는 스택에 그대로 둔다(되돌리면 복원)."""
    why = _edit_refusal("삭제")
    if why:
        await push_log(why[1], "warn")
        return False
    try:
        no = int(no)
    except (TypeError, ValueError):
        await push_log("샘플 번호 오류 · 삭제 안 함", "warn")
        return False
    s = state.sample(no)
    if s is None:
        await push_log("#%s 없음 · 삭제 안 함" % no, "warn")
        return False
    state.samples.remove(s)
    state.edits.append({"op": "del", "sample": s})
    _after_edit("delete", s)
    await push_log("샘플 #%d 삭제" % no, "ok")
    await push_state()
    return True


async def undo_edit():
    """마지막 편집 하나를 취소한다."""
    why = _edit_refusal("되돌리기")
    if why:
        await push_log(why[1], "warn")
        return False
    if not state.edits:
        await push_log("되돌릴 편집 없음")
        return False
    e = state.edits.pop()
    if e["op"] == "add":
        s = state.sample(e["no"])
        if s is not None:
            state.samples.remove(s)
        s = s or {"no": e["no"], "u": None, "v": None, "X": None, "Y": None}
        _after_edit("undo", s, undo_of="add")
        await push_log("되돌리기 · #%d 추가 취소" % e["no"], "ok")
    elif e["op"] == "move":
        s = state.sample(e["no"])
        if s is not None:
            s.update(e["old"])
            refresh_fit()
            _after_edit("undo", s, undo_of="move")
            await push_log("되돌리기 · #%d 이동 취소" % e["no"], "ok")
    else:
        s = e["sample"]
        state.samples.append(s)
        state.samples.sort(key=lambda x: x["no"])
        refresh_fit()
        _after_edit("undo", s, undo_of="delete")
        await push_log("되돌리기 · #%d 복원" % s["no"], "ok")
    await push_state()
    return True


# --------------------------------------------------------------------------
# 순회
# --------------------------------------------------------------------------
def needs_confirm():
    """확인 없이 시작하면 안 되는 사유 목록 (없으면 빈 리스트)."""
    out = []
    for w in state.warnings:
        if "cut off" in w:
            out.append("웨이퍼가 감지영역 밖")
        if "glare covers" in w:
            try:
                pct = float(w.split("glare covers")[1].split("%")[0])
            except (IndexError, ValueError):
                pct = 0.0
            if pct >= 50.0:
                out.append("반사광 %.0f%% · 조명 확산 필요" % pct)
    bad = [str(s["no"]) for s in state.samples if s.get("on") and s.get("fit") is False]
    if bad:
        out.append("공구 끝이 샘플 밖(핀 벗어남): #%s" % ", #".join(bad))
    c = state.calib or {}
    if c.get("missing_ids") and state.get("calib.confirm_3markers", True):
        out.append("마커 id%s 미검출 · %d점 보정(오차 ≈1 mm)"
                   % (",".join(str(i) for i in c["missing_ids"]), c.get("corners", 0)))
    return out


def run_refusal(rc=None):
    """순회를 시작할 수 없는 사유 목록(레시피·Z 원점·새 촬영)."""
    rc = rc or state.recipe()
    out = []
    v, errs, _w = state.check(state.candidate())
    prefix = None
    for i, r in enumerate(state.get("recipe.items") or []):
        if r is rc:
            prefix = "recipe.items[%d]" % i
    bad = [m for k, m in errs.items() if prefix and k.startswith(prefix)]
    if bad:
        out.append("레시피 '%s' 오류 · %s" % (rc.get("name"), bad[0]))
    if recipe_mod.uses_z(rc) and not state.stage.get("homed_z"):
        out.append("Z 원점 없음 · Z 단계가 있는 레시피는 시작 안 함")
    if any(s.get("X") is None for s in state.samples):
        out.append("판 보정 필요 · 좌표 없는 샘플")
    if ((state.plate or {}).get("calib") or {}).get("state") == "approx":
        out.append("판 보정 근사 상태 · 샘플로 보정점을 기록한 뒤 순회 (보정 → 샘플로 보정)")
    off = [n for n in recipe_mod.relays_used(rc, state.settings) if not recipe_mod.relay_enabled(n, state.settings)]
    if off:
        out.append("릴레이 %s 사용 안 함 · 릴레이 탭에서 사용 지정" % ", ".join("R%d" % n for n in off))
    if recipe_mod.needs_temp(rc) and str(state.get("temp.driver") or "dummy") == "dummy" and not stagectl.ctl.dry:
        out.append("온도 센서 미연결(더미 드라이버) · 가열 · 온도 대기 단계가 있는 레시피는 시작 안 함")
    if any(isinstance(st, dict) and st.get("type") == "cooler" and st.get("target") == "sample"
           for st in rc.get("steps") or []):
        none_c = [s["no"] for s in state.samples if s.get("on") and not s.get("cooler")]
        if none_c:
            out.append("쿨러 구역 없는 샘플 #%s" % ", #".join(str(n) for n in none_c))
    if state.get("flow.require_fresh_capture") and state.sequence.get("used"):
        out.append("새 촬영 필요 · 이 촬영으로 이미 순회함")
    return out


async def start_run(mode="auto", only=None):
    """(ok, 사유 목록)."""
    global _task, _next_evt, _pause_evt, _stop, _stop_evt, _measurer
    if busy():
        await push_log("이미 순회 중입니다", "warn")
        return False, ["이미 순회 중"]
    why = state.can_move()
    if why:
        await push_log(why, "warn")
        return False, [why]
    rc = state.recipe()
    bad = run_refusal(rc)
    if bad:
        for b in bad:
            await push_log(b, "warn")
        return False, bad
    if state.sequence.get("phase") == "done":
        # 같은 검출로 한 바퀴 더 돈다. 가동범위 밖(skip)은 그대로 두고 나머지만
        # 되돌린다(다시 촬영하지 않았으므로 좌표는 그대로다).
        for s in state.samples:
            if s.get("on") and s["status"] != "skip":
                s["status"] = "wait"
                s["value"] = None
                s["unit"] = None
    refresh_sample_status()
    todo = [s for s in state.samples
            if s.get("on") and s["status"] not in ("done", "skip")
            and (not only or s["no"] in only)]
    if not todo:
        await push_log("순회할 샘플이 없습니다 (측정 대상 체크를 확인하세요)", "warn")
        return False, ["순회할 샘플 없음"]
    _stop = False
    _next_evt = asyncio.Event()
    _stop_evt = asyncio.Event()
    _pause_evt = asyncio.Event()
    _pause_evt.set()
    _measurer = measure_mod.make(state.settings)
    devices.hub.clear_trip()
    _cooler_on.clear()
    steps = [dict(st) for st in rc.get("steps") or []]
    if mode == "confirm":
        steps.append({"type": "confirm"})    # 확인 모드 = 샘플마다 끝에 확인 대기
    tool = state.tool()
    state.sequence.update({
        "mode": mode, "done": 0, "total": len(todo), "elapsed_s": 0,
        "recipe": rc.get("name"), "tool": tool.get("name"),
        "steps": [recipe_mod.describe(st, state.settings) for st in steps],
        "step_total": len(steps), "step_index": None, "step_desc": "", "step_left_s": None,
        "used": True})
    _task = asyncio.create_task(_run_loop(todo, steps, rc.get("name"), tool))
    return True, []


def _steps_log(rec):
    d = state.sequence.get("out_dir")
    if d:
        storage.append_jsonl(os.path.join(d, "steps.jsonl"), rec)


def _ts():
    return time.strftime("%Y-%m-%d %H:%M:%S")


async def _pause_point():
    """단계 사이에서 일시정지를 받는다."""
    if _pause_evt is not None and not _pause_evt.is_set():
        _phase("paused", "일시정지")
        await push_state()
        await _pause_evt.wait()
    if _stop:
        raise Stopped()


# ---- 단계 실행기 (recipe.STEP_TYPES 와 짝) --------------------------------
async def _run_move_sample(step, s, ctx):
    px, py = state.stage_target(s["X"], s["Y"])
    if not calib.in_range(px, py):
        raise StepError("#%s 스테이지 자리 X%.1f Y%.1f 가 가동범위 밖" % (s["no"], px, py))
    s["status"] = "moving"
    await push_state()
    await lift_z()                         # 불변식: X·Y 이동 전에 Z 를 맨 위로
    await _goto(px, py, no=s["no"])
    if state.get("photohome.after_move"):
        import photohome
        ok, msg = await photohome.verify_at(px, py)
        if not ok:
            if state.get("photohome.after_move_fail") == "continue":
                await push_log("#%s 위치 재확인 실패 · 계속 (%s)" % (s["no"], msg), "warn")
            else:
                raise StepError("#%s 위치 재확인 실패 · %s" % (s["no"], msg))
    return {"stage_xy": [round(px, 2), round(py, 2)], "sample_xy": [s["X"], s["Y"]]}


async def _run_z(step, s, ctx):
    if not state.stage.get("homed_z"):
        raise StepError("Z 원점 없음 · Z 단계 실행 안 함")
    z = recipe_mod.z_target_mm(step, ctx["values"])
    zmax = stagectl.z_max_mm()
    if z is None or z < 0 or z > zmax + 1e-9:
        raise StepError("Z %s mm · 가동범위 0~%g 밖" % (z, zmax))
    if state.stage.get("z_mm") is not None and abs(state.stage["z_mm"] - z) < 0.005:
        return {"z_mm": z}
    await move_z(z)
    return {"z_mm": z}


async def _run_relay(step, s, ctx):
    ok, reason = await relays.set_relay(step.get("n"), bool(step.get("on")), by_engine=True)
    if not ok:
        raise StepError("R%s %s 실패 (%s)" % (step.get("n"), "켜기" if step.get("on") else "끄기",
                                            reason))
    return {"n": step.get("n"), "on": bool(step.get("on"))}


async def _run_wait(step, s, ctx):
    """대기. 정지는 즉시 끊고, 일시정지는 남은 시간을 멈춘다."""
    left = float(step.get("s") or 0)
    last_push = 0.0
    while left > 0:
        if _stop:
            raise Stopped()
        if _pause_evt is not None and not _pause_evt.is_set():
            state.sequence["step_left_s"] = round(left, 1)
            _phase("paused", "일시정지 · 대기 남은 %.0f s" % left)
            await push_state()
            await _pause_evt.wait()        # 시간은 흐르지 않는다
            if _stop:
                raise Stopped()
            _progress(ctx["desc"])
        dt = min(0.1, left)
        try:
            await asyncio.wait_for(_stop_evt.wait(), timeout=dt)
        except asyncio.TimeoutError:
            pass
        left -= dt
        state.sequence["step_left_s"] = round(max(0.0, left), 1)
        if time.monotonic() - last_push >= 0.5:
            last_push = time.monotonic()
            await push_state()
    if _stop:
        raise Stopped()
    return {}


async def _run_measure(step, s, ctx):
    s["status"] = "measuring"
    await push_state()
    try:
        r = await _measurer.measure(dict(s), {"out_dir": state.sequence["out_dir"],
                                              "params": state.params})
    except Exception as e:                 # noqa: BLE001
        r = None
        await push_log("측정 오류(#%s): %s" % (s["no"], e), "warn")
    if r:
        s["value"] = r.get("value")
        s["unit"] = r.get("unit")
    return {"value": s.get("value"), "unit": s.get("unit")}


async def _run_confirm(step, s, ctx):
    _phase("waiting_confirm", "#%s 확인 대기" % s["no"])
    await push_state()
    _next_evt.clear()
    await _next_evt.wait()
    if _stop:
        raise Stopped()
    _progress("#%s 확인 완료" % s["no"])
    return {}


# ---- 측정 과정 단계 (쿨러 · 접촉 · 온도 · 가열 · 기록) -----------------------
_cooler_on = set()                         # 엔진이 켠 쿨러 릴레이 번호
_rec = {"task": None, "fh": None, "writer": None, "path": None, "no": None}


def _measure_idle():
    """단계가 끝나면 그래프 목표선 · 남은 시간 표시를 내린다."""
    devices.hub.target = None
    devices.hub.left_s = None
    devices.hub.label = ""


async def _vwait(dt_v):
    """가상 시간 dt_v 를 기다린다. 정지가 오면 즉시 깬다."""
    try:
        await asyncio.wait_for(_stop_evt.wait(), timeout=max(0.0, dt_v) / devices.scale())
    except asyncio.TimeoutError:
        pass
    if _stop:
        raise Stopped()


async def _ask(msg):
    """사람 확인 한 번(정책 confirm): [다음] 이면 계속, 정지면 중단."""
    _phase("waiting_confirm", msg)
    await push_state()
    _next_evt.clear()
    await _next_evt.wait()
    if _stop:
        raise Stopped()
    _progress(msg)


def _cooler_targets(step, s):
    zones = devices.zone_relays()
    tg = step.get("target")
    if tg == "n":
        return [int(step.get("n"))]
    if tg == "all":
        return list(zones)
    if str(state.get("cooler.mode") or "sample") == "all":
        return list(zones)
    n = s.get("cooler")
    if not n:
        raise StepError("#%s 쿨러 구역 없음" % s["no"])
    return [int(n)]


async def _run_cooler(step, s, ctx):
    want = _cooler_targets(step, s)
    on = step.get("action") == "on"
    if on and step.get("target") == "sample" and str(state.get("cooler.mode") or "sample") == "sample":
        for n in sorted(_cooler_on - set(want)):             # 다른 구역에 켜 둔 쿨러는 끈다
            await relays.set_relay(n, False, by_engine=True)
            _cooler_on.discard(n)
    for n in want:
        ok, reason = await relays.set_relay(n, on, by_engine=True)
        if not ok:
            raise StepError("R%s 쿨러 %s 실패 (%s)" % (n, "켜기" if on else "끄기", reason))
        (_cooler_on.add if on else _cooler_on.discard)(n)
    return {"relays": want, "on": on}


async def _run_contact(step, s, ctx):
    if not state.stage.get("homed_z"):
        raise StepError("Z 원점 없음 · 접촉 탐색 안 함")
    a, b = recipe_mod.contact_range(step, ctx["values"])
    zmax = stagectl.z_max_mm()
    press = float(step.get("press_mm") or 0.0)
    if a is None or b is None or a < 0 or b + press > zmax + 1e-9 or a >= b:
        raise StepError("접촉 탐색 Z %s~%s mm · 가동범위 0~%g 안이어야 함 (공구 작업 높이 확인)"
                        % (None if a is None else round(a, 2), None if b is None else round(b, 2), zmax))
    if devices.hub.contact is None and str(state.get("contact.driver") or "dummy") not in ("dummy",):
        devices.hub.configure()
    if await devices.hub.contact_now() is None and devices.hub.contact is None:
        raise StepError("접촉 드라이버 사용 안 함")
    step_mm = float(step.get("step_mm"))
    settle = float(step.get("settle_s") or 0.0)
    z = round(a, 4)
    await move_z(z)
    found = False
    n_steps = 0
    while True:
        await _vwait(settle)
        c = await devices.hub.contact_now()
        if c is None:
            raise StepError("접촉 신호를 읽을 수 없음")
        if c:
            found = True
            break
        if z + step_mm > b + 1e-9:
            break
        z = round(z + step_mm, 4)
        n_steps += 1
        await move_z(z)
    if found:
        s["contact_z"] = z
        if press > 0:
            await move_z(round(min(z + press, zmax), 4))
        await push_log("#%s 접촉 Z %.2f mm (추가 누름 %.2f)" % (s["no"], z, press), "ok")
        return {"found": True, "contact_z": z, "press_mm": press, "steps": n_steps}
    s["contact_z"] = None
    pol = step.get("on_fail")
    msg = "접촉 못 찾음 (Z %.2f~%.2f mm)" % (a, b)
    if pol == "skip":
        raise SkipSample(msg)
    if pol == "confirm":
        await push_log("#%s %s" % (s["no"], msg), "warn")
        await _ask("#%s %s · [다음]=계속 · 정지" % (s["no"], msg))
        return {"found": False, "confirmed": True}
    raise StepError("#%s %s" % (s["no"], msg))


async def _timeout_policy(step, s, msg):
    pol = step.get("on_timeout")
    if pol == "skip":
        raise SkipSample(msg)
    if pol == "stop":
        raise StepError("#%s %s" % (s["no"], msg))
    if pol == "confirm":
        await push_log("#%s %s" % (s["no"], msg), "warn")
        await _ask("#%s %s · [다음]=계속 · 정지" % (s["no"], msg))
    else:
        await push_log("#%s %s · 계속" % (s["no"], msg), "warn")


async def _run_wait_temp(step, s, ctx, kind="wait_temp"):
    hub = devices.hub
    if kind == "cool":
        await heater_off("냉각 대기")
    cond, tc = step.get("cond"), float(step.get("target_c"))
    hold, max_s = float(step.get("hold_s") or 0.0), float(step.get("max_s"))
    hub.target, hub.label = tc, ("냉각 대기" if kind == "cool" else "온도 대기")
    t0 = devices.vnow()
    ok_since = None
    reached = None
    while True:
        if _stop:
            raise Stopped()
        if hub.tripped:
            raise StepError(hub.tripped)
        c = hub.last_c
        now = devices.vnow()
        sat = c is not None and (c <= tc if cond == "<=" else c >= tc)
        if sat:
            ok_since = now if ok_since is None else ok_since
            if now - ok_since >= hold:
                reached = now - t0
                break
        else:
            ok_since = None
        if now - t0 >= max_s:
            break
        hub.left_s = round(max(0.0, max_s - (now - t0)), 1)
        state.sequence["step_left_s"] = hub.left_s
        await _vwait(0.2)
    det = {"temp": None if hub.last_c is None else round(hub.last_c, 1), "target_c": tc, "cond": cond}
    if reached is None:
        det.update({"timed_out": True, "waited_s": round(devices.vnow() - t0, 1)})
        await _timeout_policy(step, s, "%s 시간 초과 (%s %g °C · 현재 %s °C)"
                              % ("냉각 대기" if kind == "cool" else "온도 대기", cond, tc, det["temp"]))
    else:
        det.update({"timed_out": False, "reached_s": round(reached, 1)})
    return det


async def _run_cool(step, s, ctx):
    return await _run_wait_temp(step, s, ctx, kind="cool")


async def _run_heat(step, s, ctx):
    hub = devices.hub
    n = heater_n()
    if not recipe_mod.relay_enabled(n, ctx["values"]):
        raise StepError("히트건 R%d 사용 안 함 · 릴레이 탭에서 사용 지정" % n)
    if str(state.get("temp.driver") or "dummy") == "dummy" and not stagectl.ctl.dry:
        raise StepError("온도 센서 미연결(더미) · 가열 안 함")
    hub.clear_trip()
    hub.configure()
    if hub.last_c is None:
        await hub.tick()
    if hub.last_c is None:
        raise StepError("온도 센서 오류 · 가열 안 함")
    end = step.get("end")
    tc = float(step.get("target_c") or 0.0)
    ts = float(step.get("time_s") or 0.0)
    max_s = float(step.get("max_s"))
    hub.target, hub.label = (tc if end in ("temp", "either") else None), "가열"
    ok, reason = await relays.set_relay(n, True, by_engine=True)
    if not ok:
        raise StepError("히트건 켜기 실패 (%s)" % reason)
    t0 = devices.vnow()
    why = ""
    el = 0.0
    try:
        while True:
            if _stop:
                raise Stopped()
            if hub.tripped:
                raise StepError(hub.tripped)
            c = hub.last_c
            el = devices.vnow() - t0
            if end in ("temp", "either") and c is not None and c >= tc:
                why = "temp"
                break
            if end in ("time", "either") and el >= ts:
                why = "time"
                break
            if el >= max_s:
                why = "max"
                break
            hub.left_s = round(max(0.0, (max_s if end == "temp" else min(max_s, ts if end == "time" else max_s)) - el), 1)
            state.sequence["step_left_s"] = hub.left_s
            await _vwait(0.2)
    finally:
        await heater_off("가열 끝")         # 정상 · 정지 · 오류 · 비상정지 어느 경우든
    det = {"end": why, "elapsed_s": round(el, 1), "temp": None if hub.last_c is None else round(hub.last_c, 1),
           "target_c": tc if end in ("temp", "either") else None, "timed_out": why == "max"}
    if why == "max":
        await push_log("#%s 가열 최대 시간 %g s 도달" % (s["no"], max_s), "warn")
    return det


# ---- 기록(CSV) ----
async def _rec_loop(s, ctx_params):
    import csv as _csv
    t0 = devices.vnow()
    last_meas = None
    next_meas = 0.0
    while True:
        now = devices.vnow() - t0
        z = state.stage.get("z_mm")
        hub = devices.hub
        sp = float(state.get("measure.sample_period_s") or 0.0)
        if sp > 0 and now >= next_meas:
            next_meas = now + sp
            try:
                r = await _measurer.measure(dict(s), {"out_dir": state.sequence.get("out_dir"), "params": state.params})
                last_meas = None if not r else r.get("value")
            except Exception as e:             # noqa: BLE001
                logger.write("warn", "기록 중 측정 오류: %s" % e)
        _rec["writer"].writerow([
            time.strftime("%Y-%m-%d %H:%M:%S"), round(now, 2), state.sequence.get("step_desc") or "",
            "" if hub.last_c is None else round(hub.last_c, 2), 1 if state.measure.get("heater_on") else 0,
            " ".join("R%d" % n for n in (state.measure.get("cooler_on") or [])),
            "" if z is None else round(z, 3),
            "" if state.measure.get("contact") is None else (1 if state.measure["contact"] else 0),
            "" if last_meas is None else last_meas])
        _rec["fh"].flush()
        await devices.vsleep(float(state.get("temp.period_s") or 0.5))


async def _start_recorder(s):
    import csv as _csv
    await _stop_recorder()
    d = state.sequence.get("out_dir")
    if not d:
        return None
    p = os.path.join(d, "sample_%02d_log.csv" % int(s["no"]))
    fh = open(p, "w", encoding="utf-8-sig", newline="")
    w = _csv.writer(fh)
    w.writerow(["시각", "경과_s", "단계", "온도_C", "히트건", "쿨러", "Z_mm", "접촉", "측정값"])
    _rec.update({"fh": fh, "writer": w, "path": p, "no": s["no"]})
    _rec["task"] = asyncio.create_task(_rec_loop(s, state.params))
    state.measure["recording"] = True
    return os.path.basename(p)


async def _stop_recorder():
    t = _rec.get("task")
    if t is not None:
        t.cancel()
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
    fh = _rec.get("fh")
    if fh is not None:
        with contextlib.suppress(Exception):
            fh.close()
    name = os.path.basename(_rec["path"]) if _rec.get("path") else None
    _rec.update({"task": None, "fh": None, "writer": None, "path": None, "no": None})
    state.measure["recording"] = False
    return name


async def _run_log(step, s, ctx):
    if step.get("action") == "start":
        name = await _start_recorder(s)
        return {"action": "start", "file": name}
    name = await _stop_recorder()
    return {"action": "end", "file": name}


async def _end_sample(s):
    """샘플이 끝났을 때(정상 · 건너뜀 · 오류 · 정지 어느 경우든): 기록 닫기, 쿨러는 설정대로."""
    await _stop_recorder()
    _measure_idle()
    if not bool(state.get("cooler.keep_on", True)) and _cooler_on and not _estopped:
        for n in sorted(_cooler_on):
            await relays.set_relay(n, False, by_engine=True)
        _cooler_on.clear()


RUNNERS = {
    "move_sample": _run_move_sample,
    "z": _run_z,
    "relay": _run_relay,
    "wait": _run_wait,
    "measure": _run_measure,
    "confirm": _run_confirm,
    "cooler": _run_cooler,
    "contact": _run_contact,
    "wait_temp": _run_wait_temp,
    "heat": _run_heat,
    "cool": _run_cool,
    "log": _run_log,
}


async def _run_loop(todo, steps, recipe_name, tool):
    global _stop
    t0 = time.monotonic()
    mode = state.sequence["mode"]
    _phase("running", "순회 시작 · %d" % len(todo))
    await push_log("순회 시작: %d개 · 레시피 '%s' · 공구 '%s' · 모드 %s"
                   % (len(todo), recipe_name, tool.get("name"), mode), "ok")
    _steps_log({"time": _ts(), "event": "run_start", "recipe": recipe_name,
                "tool": tool.get("name"), "tool_offset": [tool.get("dx_mm"), tool.get("dy_mm")],
                "mode": mode, "samples": [s["no"] for s in todo],
                "steps": [dict(x) for x in steps]})
    storage.append_jsonl(paths.SEQ_LOG_PATH, {
        "time": _ts(), "event": "run_start", "recipe": recipe_name,
        "tool": tool.get("name"), "samples": len(todo)})
    await push_state()
    result = "done"
    values = state.settings
    try:
        for s in todo:
            if _stop:
                result = "stopped"
                break
            await _pause_point()
            state.sequence["elapsed_s"] = int(time.monotonic() - t0)
            state.sequence["cur_no"] = s["no"]
            try:
                i = 0
                counters = {}
                guard = 0
                while i < len(steps):
                    guard += 1
                    if guard > 5000:
                        raise StepError("단계 반복이 너무 많음")
                    step = steps[i]
                    await _pause_point()
                    if step["type"] == "repeat":
                        a, c = int(step["from"]), int(step["count"])
                        counters[i] = counters.get(i, 0) + 1
                        if counters[i] < c:
                            for k in range(a - 1, i):      # 안쪽 반복은 다시 센다
                                counters.pop(k, None)
                            _steps_log({"time_start": _ts(), "time_end": _ts(), "sample_no": s["no"],
                                        "step": i + 1, "type": "repeat", "desc": recipe_mod.describe(step, values),
                                        "result": "ok", "detail": {"round": counters[i], "of": c}})
                            i = a - 1
                            continue
                        counters.pop(i, None)
                        i += 1
                        continue
                    desc = recipe_mod.describe(step, values)
                    label = "#%s · %d/%d · %s" % (s["no"], i + 1, len(steps), desc)
                    state.sequence.update({"step_index": i, "step_desc": label,
                                           "step_left_s": None})
                    _progress(label)
                    await push_state()
                    t_start = _ts()
                    ctx = {"values": values, "desc": label, "index": i}
                    try:
                        res = await RUNNERS[step["type"]](step, s, ctx)
                        _steps_log({"time_start": t_start, "time_end": _ts(), "sample_no": s["no"],
                                    "step": i + 1, "type": step["type"], "desc": desc,
                                    "result": "ok", "detail": res or {}})
                    except Stopped:
                        _steps_log({"time_start": t_start, "time_end": _ts(), "sample_no": s["no"],
                                    "step": i + 1, "type": step["type"], "desc": desc,
                                    "result": "stopped"})
                        raise
                    except SkipSample as e:
                        _steps_log({"time_start": t_start, "time_end": _ts(), "sample_no": s["no"],
                                    "step": i + 1, "type": step["type"], "desc": desc,
                                    "result": "skipped", "detail": {"reason": str(e)}})
                        raise
                    except (StepError, stagectl.StageError) as e:
                        _steps_log({"time_start": t_start, "time_end": _ts(), "sample_no": s["no"],
                                    "step": i + 1, "type": step["type"], "desc": desc,
                                    "result": "error", "detail": {"error": str(e)}})
                        raise
                    finally:
                        _measure_idle()
                    i += 1
                s["status"] = "done"
                state.sequence["done"] += 1
            except SkipSample as e:
                s["status"] = "skip"
                await push_log("#%s 건너뜀 · %s" % (s["no"], e), "warn")
            finally:
                await _end_sample(s)
            state.sequence["elapsed_s"] = int(time.monotonic() - t0)
            await push_state()
    except Stopped:
        result = "stopped"
        for s in todo:
            if s["status"] in ("moving", "measuring"):
                s["status"] = "wait"
    except (StepError, stagectl.StageError) as e:
        result = "error"
        cur = state.sample(state.sequence.get("cur_no"))
        if cur is not None:
            cur["status"] = "error"
        one = logger.short(e)
        if _estopped:
            # 비상정지가 만든 오류다. phase 는 estop() 이 세운 stopped 를 유지한다.
            await push_log("비상정지로 단계 중단 (#%s)" % state.sequence.get("cur_no"), "warn")
        else:
            state.stage["last_error"] = one if isinstance(e, stagectl.StageError) else ""
            logger.write("err", "순회 단계 실패 상세: %s" % e)
            _phase("error", "순회 중단 · " + one)
            await push_log("순회 중단 · " + one, "err")
    except asyncio.CancelledError:
        result = "error"
        raise
    finally:
        state.sequence["elapsed_s"] = int(time.monotonic() - t0)
        state.sequence["cur_no"] = None
        state.sequence["step_index"] = None
        state.sequence["step_left_s"] = None
        _steps_log({"time": _ts(), "event": "run_end", "result":
                    "estop" if _estopped else result, "done": state.sequence["done"]})
        await _finish(result)


# ---- 끝난 뒤 흐름 - 설정이 조합한다 ----------------------------------------
async def _post_lift():
    if state.stage.get("homed_z"):
        await lift_z()


async def _post_after_photo():
    """순회 후 확인 촬영: 촬영 위치로 가서 한 장 더 찍어 같은 폴더에 after.png."""
    d = state.sequence.get("out_dir")
    if not d:
        return
    px, py = state.capture_xy()
    await _goto(px, py, no="after_photo")
    bgr = await vision.single_shot(state.params)
    await vision.run_blocking(imgio.imwrite_u, os.path.join(d, "after.png"), bgr)
    await push_log("확인 촬영 · after.png", "ok")


async def _post_goto():
    dest = state.get("after_run") or "park"
    if dest == "stay":
        return
    if dest == "capture":
        x, y = state.capture_xy()
    elif dest == "origin":
        x, y = 0.0, 0.0
    else:
        x, y = state.park_xy()
    await _goto(x, y, no=dest)


async def _post_save():
    for ln in await stagectl.ctl.save():
        if str(ln).strip():
            await push_log("펌웨어: %s" % str(ln).strip())


# (이름, 함수, 켜는 설정 키 | None, 정상 완료에서만?)
POST_RUN_FLOW = [
    ("lift_z", _post_lift, None, False),
    ("after_photo", _post_after_photo, "flow.after_photo", True),
    ("after_run", _post_goto, None, False),
    ("save", _post_save, None, False),
]


async def _finish(result):
    """정상·정지·오류: 릴레이 정책 → Z 맨 위 → 확인 촬영 → 순회 후 갈 곳 → save.

    비상정지는 예외다. 이동은 '또 움직이는 것' 이고 save 는 믿을 수 없는 위치를
    EEPROM 에 굳히는 것이라, 둘 다 하지 않는다(릴레이 정책은 estop() 이 이미 적용).
    """
    await _stop_recorder()
    await heater_off("순회 종료")           # 정지 · 오류 · 비상정지: 릴레이 정책과 무관하게 항상
    _cooler_on.clear()
    devices.hub.target = None
    devices.hub.label = ""
    devices.hub.left_s = None
    if _estopped:
        try:
            _write_results()
        except Exception as e:             # noqa: BLE001
            await push_log("results.csv 저장 실패: %s" % e, "warn")
        _phase("stopped", "비상정지 · 원점 설정 필요")
        await push_state()
        return
    try:
        await relays.apply_policy("error" if result == "error" else "stop",
                                  "순회 오류" if result == "error" else "순회 종료")
    except Exception as e:                 # noqa: BLE001
        await push_log("릴레이 정책 적용 실패: %s" % e, "warn")
    # 오류 문구는 정리 동안 '순회 후 정리' 로 덮이므로 먼저 받아 둔다.
    err_msg = state.sequence.get("message") if (result == "error"
                                                and state.sequence.get("phase") == "error") else None
    _phase("parking", "순회 후 정리")
    await push_state()
    for name, fn, key, only_done in POST_RUN_FLOW:
        if key and not state.get(key):
            continue
        if only_done and result != "done":
            continue
        if _estopped:
            break
        try:
            await fn()
        except Exception as e:             # noqa: BLE001
            await push_log("순회 후 %s 실패: %s" % (name, logger.short(e)), "warn")
    try:
        _write_results()
    except Exception as e:                 # noqa: BLE001
        await push_log("results.csv 저장 실패: %s" % e, "warn")
    if _estopped:
        _phase("stopped", "비상정지 · 원점 설정 필요")
    elif result == "error":
        _phase("error", err_msg or "오류 중단")
    elif result == "stopped":
        _phase("stopped", "정지")
        await push_log("순회 정지 (완료 %d개)" % state.sequence["done"], "warn")
    else:
        _phase("done", "완료 %d/%d" % (state.sequence["done"], state.sequence["total"]))
        await push_log("순회 완료 · %d개 · %d초" % (state.sequence["done"],
                                              state.sequence["elapsed_s"]), "ok")
    await push_state()


def _write_results():
    d = state.sequence.get("out_dir")
    if not d:
        return
    p = os.path.join(d, "results.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["no", "shape", "X_mm", "Y_mm", "value", "unit", "status", "time",
                    "source"])
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for s in state.samples:
            w.writerow([s["no"], s.get("shape", ""), s["X"], s["Y"],
                        "" if s.get("value") is None else s["value"],
                        s.get("unit") or "", s["status"], ts, _source(s)])


async def pause():
    if _pause_evt is not None and busy():
        _pause_evt.clear()
        _phase("paused", "일시정지")
        await push_log("일시정지 · 단계 사이에서 멈춤", "warn")
        await push_state()


async def resume():
    if _pause_evt is not None:
        _pause_evt.set()
        _phase("running", state.sequence.get("step_desc") or "재개")
        await push_log("재개")
        await push_state()


async def next_step():
    if _next_evt is not None:
        _next_evt.set()


async def stop():
    """정지: 지금 단계를 즉시 끊는다(대기·확인). 진행 중인 이동 한 번은 끝까지 간다."""
    global _stop
    _stop = True
    if _stop_evt is not None:
        _stop_evt.set()
    if _pause_evt is not None:
        _pause_evt.set()
    if _next_evt is not None:
        _next_evt.set()
    await push_log("정지 요청 · 지금 단계를 끊고 정리", "warn")


_last_estop_at = 0.0
ESTOP_DEDUP_S = 2.0                # 이 안에 다시 오면 중단만 하고 로그는 생략


async def estop():
    """확인 없이 즉시. 이동도 save 도 하지 않는다. 릴레이 정책만 적용한다.

    "!" 가 맨 먼저 나간다(동기 코드, await 이전). 정책이 '끄기' 면 같은 쓰기에
    "r all 0" 을 붙인다 - 비상정지 자체는 늦어지지 않는다.

    태스크를 cancel 하지 않는다 - 진행 중이던 이동은 펌웨어 보고 줄의 "[!] 중단"
    으로 StageError 가 되어 순회 루프가 스스로 빠져나온다. cancel 하면 finally
    가 이동까지 실행해 '비상정지 직후 다시 움직이는' 일이 벌어진다.

    화면은 WebSocket 과 HTTP 두 경로로 보낸다(둘 중 하나가 막혀도 도착하게).
    그래서 같은 누름이 두 번 들어오는데, 중단은 두 번 해도 무해하지만 로그와
    원점 기록 삭제까지 두 번 할 이유는 없다.
    """
    global _stop, _estopped, _last_estop_at
    _stop = True
    dup = _estopped and (time.monotonic() - _last_estop_at) < ESTOP_DEDUP_S
    _estopped = True
    _last_estop_at = time.monotonic()
    off = relays.policy_off("estop") and relays.supported()
    ok = stagectl.ctl.abort(relays_off=off, off_relays=(heater_n(),))
    if dup:
        return
    state.stage["needs_home"] = True       # 원점을 다시 잡기 전까지 이동 잠금
    if _stop_evt is not None:
        _stop_evt.set()
    if _pause_evt is not None:
        _pause_evt.set()
    if _next_evt is not None:
        _next_evt.set()
    state.stage["moving"] = False
    _phase("stopped", "비상정지 · 원점 설정 필요")
    await push_log("비상정지 · 펌웨어 중단 명령 전송%s%s"
                   % (" · 릴레이 전부 끄기" if off else "", "" if ok else " (실패)"),
                   "err" if not ok else "warn")
    await push_log("정지 후 위치 신뢰 불가 · 원점 등록 또는 사진 호밍", "warn")
    await push_state()
    # 펌웨어의 원점 기록도 지운다. 그대로 두면 다음 부팅에서 EEPROM 의 '원점OK'
    # 가 복원되어, 믿을 수 없는 좌표를 가진 채 절대 모드로 켜진다(2026-09-11).
    # 워커 큐를 거치므로 진행 중이던 이동이 [!] 중단으로 끝난 뒤에 나간다.
    if stagectl.ctl.connected:
        try:
            _apply_status(await stagectl.ctl.forget())
            await push_log("원점 기록 삭제 · 두 축 원점 등록 필요", "warn")
        except Exception as e:         # noqa: BLE001
            await push_log("원점 기록 삭제 실패 · " + logger.short(e), "warn")
        await relays.sync()
        await push_state()


def estopped():
    return _estopped


def reset_estop():
    """원점을 다시 잡았다 - 잠금을 푼다(원점 등록 · 사진 호밍이 부른다).

    비상정지로 멈춰 있던 화면도 함께 정리한다. 이동이 다시 되는데 phase 가
    stopped/error 로 남아 있으면 사용자는 아직 잠긴 줄 안다.
    """
    global _estopped
    was = _estopped
    _estopped = False
    state.stage["needs_home"] = False
    if was and state.sequence.get("phase") in ("stopped", "error"):
        _phase("ready" if state.samples else "idle", "원점 설정 완료",
               cur_no=None)
