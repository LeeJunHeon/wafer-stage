"""engine.py - 촬영·검출과 샘플 순회 (run_sequence.py 의 흐름을 asyncio 로).

원칙
  - 좌표는 '그 프레임' 의 마커로 다시 보정해서 낸다(calib.sense). 마커가 모자라면
    좌표를 만들지 않고 멈춘다 - 틀린 좌표로 움직이는 것보다 낫다.
  - 펌웨어 [거부]·타임아웃·시리얼 오류는 즉시 중단한다(다음 샘플로 넘어가지 않는다).
  - 어떻게 끝나든(완료·정지·오류) 파킹 → save 를 시도한다.
"""

import asyncio
import csv
import os
import time

import calib
import logger
import measure as measure_mod
import paths
import stagectl
import storage
import vision
from connection import push_log, push_state
from state import state

_task = None                      # 진행 중인 순회 태스크
_next_evt = None                  # confirm 모드에서 '다음' 을 기다리는 이벤트
_pause_evt = None                 # 일시정지 해제 이벤트(set = 진행)
_stop = False                     # 정지 요청
_estopped = False                 # 비상정지 상태(원점을 다시 잡을 때까지 유지)
_measurer = None


def busy():
    return _task is not None and not _task.done()


def _progress(message):
    """순회 중 진행 문구. 일시정지 요청이 들어와 있으면 phase 를 running 으로
    되돌리지 않는다 - 진행 중인 샘플을 마치는 동안 화면이 '순회 중' 으로 보이면
    사용자는 일시정지가 먹지 않은 줄 안다."""
    paused = _pause_evt is not None and not _pause_evt.is_set()
    _phase("paused" if paused else "running", message)


def _phase(phase, message=None, **kw):
    state.sequence["phase"] = phase
    if message is not None:
        state.sequence["message"] = message
    state.sequence.update(kw)


# --------------------------------------------------------------------------
# 촬영 + 검출
# --------------------------------------------------------------------------
async def capture():
    if busy():
        await push_log("순회 중에는 촬영할 수 없습니다", "warn")
        return False
    state.camera["capturing"] = True
    _phase("capturing", "촬영·검출 중")
    await push_state()
    try:
        # 캐리지가 시야에 있으면 웨이퍼를 가린다. 연결돼 있으면 파킹부터.
        if stagectl.ctl.connected and state.can_move() is None:
            px, py = state.park_xy()
            if (state.stage["x_mm"] is None
                    or abs(state.stage["x_mm"] - px) > 0.05
                    or abs(state.stage["y_mm"] - py) > 0.05):
                await push_log("촬영 전 파킹 X%.1f Y%.1f" % (px, py))
                await _goto(px, py, no="park")

        bgr, meta, res, stats = await vision.capture(state.params)
        state.camera["ok"] = True
        state.camera["last_error"] = ""
        state.frame = meta
        if isinstance(stats, dict) and "chosen_focus_score" in stats:
            await push_log("촬영 완료 - %d프레임 중 선명도 %.0f"
                           % (stats.get("frames_grabbed", 0),
                              stats.get("chosen_focus_score", 0)))
        if res is None:
            state.samples = []
            state.calib = state.sensing = state.wafer = None
            state.markers = {}
            _phase("error", "마커 부족 - 좌표를 만들 수 없습니다")
            await push_log("마커를 3개 이상 찾지 못했습니다 - 이동하지 않습니다", "err")
            return False
        _apply_sense(res)
        d = await vision.run_blocking(_save_seq, bgr, res)
        state.sequence["out_dir"] = d
        _phase("ready", "검출 %d개 - 검토 후 시작" % len(state.samples),
               done=0, total=len(state.samples), cur_no=None, elapsed_s=0)
        await push_log("검출 %d개 · 저장 %s" % (len(state.samples), os.path.basename(d)), "ok")
        return True
    except Exception as e:                 # noqa: BLE001
        state.camera["ok"] = False
        state.camera["last_error"] = str(e)
        _phase("error", "촬영 실패: %s" % e)
        logger.exc("촬영 실패", e)
        await push_log("촬영 실패: %s" % e, "err")
        return False
    finally:
        state.camera["capturing"] = False
        await push_state()


def _apply_sense(res):
    """calib.sense 결과를 화면 state 로 옮긴다."""
    cal, det = res["cal"], res["det"]
    remember_cal(cal)                      # 포인터 역변환에 같은 보정을 쓴다
    used = list(cal.get("used_ids") or [])
    state.calib = {
        "refit": bool(res["refit"]),
        "used_ids": used,
        "missing_ids": [i for i in sorted(calib.MARKER_MM) if i not in used],
        "transform": cal.get("transform"),
        "corner_rms_mm": round(float(cal.get("corner_rms_mm", 0.0)), 3),
        "corner_max_mm": round(float(cal.get("corner_max_mm", 0.0)), 3),
        "rotation_deg": round(float(cal.get("rotation_deg", 0.0)), 2),
        "corners": len(cal.get("corners_px") or []),
    }
    u0, v0, u1, v1 = res["rect"]
    state.sensing = {"rect": [int(u0), int(v0), int(u1), int(v1)]}
    w = det.wafer
    if w.found:
        cx, cy = w.center_px[0] + u0, w.center_px[1] + v0
        state.wafer = {"found": True, "cx": round(cx, 1), "cy": round(cy, 1),
                       "r_px": round(w.major_px / 2.0, 1),
                       "center_mm": [round(v, 2) for v in calib.px_to_mm(cal, cx, cy)]}
    else:
        state.wafer = {"found": False, "cx": 0, "cy": 0, "r_px": 0, "center_mm": None}
    # corners 는 numpy 배열이라 `or []` 로 기본값을 주면 "truth value is ambiguous"
    # 로 터진다. None 검사를 명시적으로 한다.
    state.markers = {}
    for i in used:
        q = (cal.get("markers") or {}).get(int(i), {}).get("corners")
        if q is None:
            continue
        state.markers[int(i)] = [[round(float(x), 1), round(float(y), 1)] for x, y in q]
    mm_by_no = {r[0]: (r[3], r[4]) for r in res["rows"]}
    old_on = {s["no"]: s.get("on", True) for s in state.samples}
    state.samples = []
    for s in det.samples:
        X, Y = mm_by_no.get(s["no"], (0.0, 0.0))
        state.samples.append({
            "no": s["no"], "shape": s.get("shape", ""),
            "u": float(s["x_px"]), "v": float(s["y_px"]),
            "X": round(float(X), 2), "Y": round(float(Y), 2),
            "verts": [[int(a), int(b)] for a, b in (s.get("vertices_px") or [])],
            "on": bool(old_on.get(s["no"], True)),
            "status": "wait" if calib.in_range(X, Y) else "skip",
            "value": None, "unit": None,
            "edge_completed": bool(s.get("edge_completed")),
            "area_mm2": s.get("area_mm2"),
        })
    state.set_warnings(det.warnings)


def _save_seq(bgr, res):
    """이번 촬영의 근거를 한 폴더에 남긴다."""
    d = os.path.join(paths.OUT_DIR, "seq_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    # save_samples_image 가 raw_name 으로 원본도 함께 쓴다(따로 imwrite 하면 2MB
    # 짜리 PNG 를 두 번 인코딩하게 된다).
    calib.save_samples_image(bgr, res, state.params, outdir=d,
                             ann_name="annotated.jpg", raw_name="raw.png")
    storage.atomic_write_json(
        os.path.join(d, "samples.json"),
        [{"no": no, "u": round(u, 1), "v": round(v, 1), "X": round(x, 2), "Y": round(y, 2)}
         for no, u, v, x, y in res["rows"]])
    return d


# --------------------------------------------------------------------------
# 이동
# --------------------------------------------------------------------------
async def _goto(x_mm, y_mm, no=None):
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
        return reports
    finally:
        state.stage["moving"] = False


def _apply_status(st):
    state.stage.update({
        "x_mm": round(st["x_mm"], 2), "y_mm": round(st["y_mm"], 2),
        "homed_x": st["homed_x"], "homed_y": st["homed_y"], "dirty": st["dirty"],
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


def _last_cal():
    if _CAL_CACHE["d"] is not None:
        return _CAL_CACHE["d"]
    return calib.load_matrix()


async def goto_sample(no):
    s = state.sample(no)
    if s is None:
        await push_log("#%s 는 이번 검출에 없습니다" % no, "warn")
        return False
    return await goto_xy(s["X"], s["Y"], no=no)


async def goto_xy(x_mm, y_mm, no=None):
    why = state.can_move()
    if why:
        await push_log(why, "warn")
        return False
    if not calib.in_range(x_mm, y_mm):
        await push_log("(%.1f, %.1f) 는 가동범위 %g~%g mm 밖입니다"
                       % (x_mm, y_mm, calib.AXIS_MIN, calib.AXIS_MAX), "warn")
        return False
    try:
        await _goto(x_mm, y_mm, no=no)
        await push_log("이동 완료 X%.1f Y%.1f%s"
                       % (x_mm, y_mm, "" if no is None else " (#%s)" % no), "ok")
        return True
    except stagectl.StageError as e:
        state.stage["last_error"] = str(e)
        await push_log("이동 실패: %s" % e, "err")
        return False
    finally:
        await push_state()


async def park():
    px, py = state.park_xy()
    return await goto_xy(px, py, no="park")


# --------------------------------------------------------------------------
# 순회
# --------------------------------------------------------------------------
def needs_confirm():
    """확인 없이 시작하면 안 되는 사유 목록 (없으면 빈 리스트)."""
    out = []
    for w in state.warnings:
        if "cut off" in w:
            out.append("웨이퍼가 감지영역에 잘렸습니다")
        if "glare covers" in w:
            try:
                pct = float(w.split("glare covers")[1].split("%")[0])
            except (IndexError, ValueError):
                pct = 0.0
            if pct >= 50.0:
                out.append("글레어가 감지영역의 %.0f%% 입니다" % pct)
    c = state.calib or {}
    if c.get("missing_ids"):
        out.append("마커 id %s 가려짐 - %d점 호모그래피(오차 약 1mm)"
                   % (", ".join(str(i) for i in c["missing_ids"]), c.get("corners", 0)))
    return out


async def start_run(mode="auto", dwell_s=None, only=None):
    global _task, _next_evt, _pause_evt, _stop, _measurer
    if busy():
        await push_log("이미 순회 중입니다", "warn")
        return False
    why = state.can_move()
    if why:
        await push_log(why, "warn")
        return False
    if state.sequence.get("phase") == "done":
        # 같은 검출로 한 바퀴 더 돈다. 가동범위 밖(skip)은 그대로 두고 나머지만
        # 되돌린다(다시 촬영하지 않았으므로 좌표는 그대로다).
        for s in state.samples:
            if s.get("on") and s["status"] != "skip":
                s["status"] = "wait"
                s["value"] = None
                s["unit"] = None
    todo = [s for s in state.samples
            if s.get("on") and s["status"] not in ("done", "skip")
            and (not only or s["no"] in only)]
    if not todo:
        await push_log("순회할 샘플이 없습니다 (측정 대상 체크를 확인하세요)", "warn")
        return False
    _stop = False
    _next_evt = asyncio.Event()
    _pause_evt = asyncio.Event()
    _pause_evt.set()
    _measurer = measure_mod.make(state.settings)
    state.sequence.update({"mode": mode, "dwell_s": float(dwell_s or 0), "done": 0,
                           "total": len(todo), "elapsed_s": 0})
    _task = asyncio.create_task(_run_loop(todo))
    return True


async def _run_loop(todo):
    global _stop
    t0 = time.monotonic()
    mode = state.sequence["mode"]
    dwell = float(state.sequence["dwell_s"])
    _phase("running", "순회 시작 - %d개" % len(todo))
    await push_log("순회 시작: %d개 · 모드 %s · 대기 %.1f초" % (len(todo), mode, dwell), "ok")
    await push_state()
    ok = True
    try:
        for s in todo:
            if _stop:
                break
            await _pause_evt.wait()
            if _stop:
                break
            state.sequence["elapsed_s"] = int(time.monotonic() - t0)
            if not calib.in_range(s["X"], s["Y"]):
                s["status"] = "skip"
                await push_log("#%s 건너뜀 - 가동범위 밖" % s["no"], "warn")
                await push_state()
                continue
            state.sequence["cur_no"] = s["no"]
            s["status"] = "moving"
            _progress("#%s 로 이동 중" % s["no"])
            await push_state()
            try:
                await _goto(s["X"], s["Y"], no=s["no"])
            except stagectl.StageError as e:
                s["status"] = "error"
                state.stage["last_error"] = str(e)
                _phase("error", "이동 실패: %s" % e)
                await push_log("이동 실패로 순회를 중단합니다: %s" % e, "err")
                ok = False
                break
            await push_log("#%s 도착 X%.1f Y%.1f" % (s["no"], s["X"], s["Y"]))

            s["status"] = "measuring"
            _progress("#%s 측정" % s["no"])
            await push_state()
            try:
                r = await _measurer.measure(dict(s), {"out_dir": state.sequence["out_dir"],
                                                      "params": state.params})
            except Exception as e:         # noqa: BLE001
                r = None
                await push_log("측정 오류(#%s): %s" % (s["no"], e), "warn")
            if r:
                s["value"] = r.get("value")
                s["unit"] = r.get("unit")

            if mode == "confirm":
                _phase("waiting_confirm", "#%s 확인 대기 - '다음' 을 누르세요" % s["no"])
                await push_state()
                _next_evt.clear()
                await _next_evt.wait()
                if _stop:
                    s["status"] = "wait"
                    break
                _progress("#%s 확인 완료" % s["no"])
            elif dwell > 0:
                await asyncio.sleep(dwell)

            s["status"] = "done"
            state.sequence["done"] += 1
            state.sequence["elapsed_s"] = int(time.monotonic() - t0)
            await push_state()
    except asyncio.CancelledError:
        ok = False
        raise
    finally:
        state.sequence["elapsed_s"] = int(time.monotonic() - t0)
        state.sequence["cur_no"] = None
        await _finish(ok)


async def _finish(ok):
    """정상·정지·오류면 파킹 → save 를 시도한다.

    비상정지는 예외다. 파킹은 '또 움직이는 것' 이고 save 는 믿을 수 없는 위치를
    EEPROM 에 굳히는 것이라, 둘 다 하지 않는다.
    """
    stopped = _stop
    if _estopped:
        try:
            _write_results()
        except Exception as e:             # noqa: BLE001
            await push_log("results.csv 저장 실패: %s" % e, "warn")
        _phase("stopped", "비상정지 - 원점잡기(fz) 후 사용")
        await push_state()
        return
    _phase("parking", "파킹 중")
    await push_state()
    try:
        px, py = state.park_xy()
        await _goto(px, py, no="park")
    except Exception as e:                 # noqa: BLE001
        await push_log("파킹 실패: %s" % e, "warn")
    try:
        for ln in await stagectl.ctl.save():
            if ln.strip():
                await push_log("펌웨어: %s" % ln.strip())
    except Exception as e:                 # noqa: BLE001
        await push_log("save 실패: %s" % e, "warn")
    try:
        _write_results()
    except Exception as e:                 # noqa: BLE001
        await push_log("results.csv 저장 실패: %s" % e, "warn")
    if not ok:
        _phase("error", "오류로 중단됨")
    elif stopped:
        _phase("stopped", "사용자 정지")
        await push_log("순회 정지 (완료 %d개)" % state.sequence["done"], "warn")
    else:
        _phase("done", "순회 완료 %d개" % state.sequence["done"])
        await push_log("순회 완료 - %d개, %d초" % (state.sequence["done"],
                                              state.sequence["elapsed_s"]), "ok")
    await push_state()


def _write_results():
    d = state.sequence.get("out_dir")
    if not d:
        return
    p = os.path.join(d, "results.csv")
    with open(p, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["no", "shape", "X_mm", "Y_mm", "value", "unit", "status", "time"])
        ts = time.strftime("%Y-%m-%d %H:%M:%S")
        for s in state.samples:
            w.writerow([s["no"], s.get("shape", ""), s["X"], s["Y"],
                        "" if s.get("value") is None else s["value"],
                        s.get("unit") or "", s["status"], ts])


async def pause():
    if _pause_evt is not None:
        _pause_evt.clear()
        _phase("paused", "일시정지 - 현재 이동을 마치고 멈춥니다")
        await push_log("일시정지", "warn")
        await push_state()


async def resume():
    if _pause_evt is not None:
        _pause_evt.set()
        _phase("running", "재개")
        await push_log("재개")
        await push_state()


async def next_step():
    if _next_evt is not None:
        _next_evt.set()


async def stop():
    global _stop
    _stop = True
    if _pause_evt is not None:
        _pause_evt.set()
    if _next_evt is not None:
        _next_evt.set()
    await push_log("정지 요청 - 현재 이동을 마치고 파킹합니다", "warn")


async def estop():
    """확인 없이 즉시. 파킹도 save 도 하지 않는다.

    태스크를 cancel 하지 않는다 - 진행 중이던 이동은 펌웨어 보고 줄의 "[!] 중단"
    으로 StageError 가 되어 순회 루프가 스스로 빠져나온다. cancel 하면 finally
    가 파킹까지 실행해 '비상정지 직후 다시 움직이는' 일이 벌어진다.
    """
    global _stop, _estopped
    _stop = True
    _estopped = True
    ok = stagectl.ctl.abort()
    state.stage["needs_home"] = True       # 원점을 다시 잡기 전까지 이동 잠금
    if _pause_evt is not None:
        _pause_evt.set()
    if _next_evt is not None:
        _next_evt.set()
    state.stage["moving"] = False
    _phase("stopped", "비상정지 - 원점잡기(fz) 후 사용")
    await push_log("비상정지 - 펌웨어에 '!' 전송%s" % ("" if ok else " (실패)"),
                   "err" if not ok else "warn")
    await push_log("정지 후에는 위치를 신뢰할 수 없습니다 - 원점잡기(fz)를 다시 하세요", "warn")
    await push_state()


def estopped():
    return _estopped


def reset_estop():
    """원점을 다시 잡았다 - 잠금을 푼다(commands.stage_home 이 부른다)."""
    global _estopped
    _estopped = False
    state.stage["needs_home"] = False
