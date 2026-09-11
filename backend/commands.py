"""commands.py - 화면 명령 처리(handle_command).

서버가 상태의 주인이다: 명령으로 상태를 바꾸고 갱신된 state 를 push 한다.
화면은 요청만 보내고, 돌아온 state 를 보고서야 자기 화면을 바꾼다.

이동 계열 명령은 '시리얼 연결 + 원점' 이 있어야 통과한다. 연결·원점잡기·비상정지·
설정은 언제나 통과해야 사람이 상황을 정리할 수 있다.
"""

import asyncio
import contextlib
import os

from core import calib, paths

import engine
import logger
import stagectl
import vision
from connection import push_ack, push_log, push_state, run_on_loop
from state import state

_shutdown_handler = None

# can_move(연결·원점·비상정지) 를 요구하는 명령. jog 는 여기 넣지 않는다 -
# 원점이 없을 때 상대 이동으로 끝단까지 몰고 가는 것이 원점을 잡는 절차라서,
# 여기서 막으면 원점을 영영 못 잡는다(_jog 가 모드별로 따로 검사한다).
MOVE_CMDS = ("park", "goto", "run", "resume", "next", "measure_here")
# 스테이지가 이미 움직이고 있으면 받지 않는다. 명령이 동시에 실행될 수 있게 된
# 뒤로는(ws_endpoint 가 태스크로 띄운다) 이 검사가 없으면 두 이동이 겹친다.
MOVING_BLOCKED = ("park", "goto", "jog", "home_touch", "set_origin", "capture",
                  "run", "measure_here")
# 순회 중에 받으면 안 되는 명령. 스테이지·카메라·설정을 순회 도중에 건드리면
# 진행 중인 이동과 충돌한다(정지 뒤에 하면 된다).
BUSY_BLOCKED = ("park", "goto", "jog", "park_here", "home_touch", "set_origin",
                "stage_disconnect", "capture", "settings_save")


def set_shutdown_handler(fn):
    global _shutdown_handler
    _shutdown_handler = fn


async def handle_command(data):
    cmd = str(data.get("cmd") or "")
    try:
        # 비상정지가 맨 앞이다. 어떤 검사도 거치지 않는다 - engine.estop() 의
        # 첫 줄들(ctl.abort())은 await 이전에 도는 동기 코드라, 여기까지 오기만
        # 하면 포트에 "!" 가 바로 나간다.
        if cmd == "estop":
            await engine.estop()
            return
        if cmd in MOVING_BLOCKED and state.stage.get("moving"):
            await push_log("이동 중 · 명령 무시 (%s)" % cmd, "warn")
            if cmd == "jog":
                await push_ack("jog", False, "moving")
            return
        if cmd in BUSY_BLOCKED and engine.busy():
            await push_log("순회 중 · 명령 무시 (%s)" % cmd, "warn")
            # 조그는 ack 를 기다렸다 다음 스텝을 보낸다. 거절도 알려 줘야
            # 화면이 '보낸 채로' 멈추지 않는다.
            if cmd == "jog":
                await push_ack("jog", False, "busy")
            return
        if cmd in MOVE_CMDS:
            why = state.can_move()
            if why:
                if cmd == "run":
                    await push_ack("run", False, "locked", [why])
                await push_log(why, "warn")
                return

        fn = _TABLE.get(cmd)
        if fn is None:
            await push_log("알 수 없는 명령: %s" % cmd, "warn")
            return
        await fn(data)
    except stagectl.StageError as e:
        one = logger.short(e)
        state.stage["last_error"] = one
        logger.write("err", "스테이지 오류 상세(%s): %s" % (cmd, e))
        await push_log("스테이지 오류 · " + one, "err")
        await push_state()
    except Exception as e:                 # noqa: BLE001
        logger.exc("명령 처리 실패(%s)" % cmd, e)
        await push_log("명령 처리 실패(%s) · %s" % (cmd, logger.short(e)), "err")
        await push_state()


# --------------------------------------------------------------------------
async def _camera_changed(ok, err):
    state.camera["ok"] = bool(ok)
    state.camera["last_error"] = "" if ok else logger.short(err or "카메라 오류")
    if ok:
        await push_log("카메라 준비 · index %s" % state.camera["index"], "ok")
    else:
        # 문구 자체가 이미 "카메라 N 열기 실패 …" 다. 앞에 또 붙이지 않는다.
        await push_log(state.camera["last_error"], "err")
    await push_state()


def _on_camera_status(ok, err):
    """미리보기 스레드가 알려 오는 카메라 상태(상태가 바뀔 때만 불린다).

    스레드에서 state 를 직접 고치지 않는다 - 루프로 넘겨 거기서 바꾸고 알린다.
    """
    run_on_loop(lambda: _camera_changed(ok, err))


def start_preview():
    """서버 기동과 함께 미리보기를 켠다(server.lifespan 이 부른다)."""
    vision.holder.on_status = _on_camera_status
    vision.holder.start(state.params)
    state.camera["preview"] = True


async def _preview_start(_data):
    vision.holder.on_status = _on_camera_status
    vision.holder.start(state.params)
    state.camera["preview"] = True
    await push_log("미리보기 시작")
    await push_state()


async def _preview_stop(_data):
    # holder.stop() 은 스레드 join(최대 2초)이라 루프에서 직접 부르면 화면이 멎는다.
    loop = asyncio.get_running_loop()
    await loop.run_in_executor(None, vision.holder.stop)
    state.camera["preview"] = False
    await push_log("미리보기 정지")
    await push_state()


async def _list_ports(_data):
    """설정창의 시리얼 포트 목록. 못 읽어도 빈 목록으로 답한다(창이 멈추지 않게)."""
    ports = []
    try:
        for p in stagectl.stage_mod.list_ports():
            ports.append({"device": p.device, "description": p.description or ""})
    except Exception as e:                 # noqa: BLE001
        await push_log("포트 목록을 읽지 못했습니다: %s" % e, "warn")
    await push_ack("list_ports", True, ports=ports)


async def _stage_connect(data):
    port = data.get("port") or state.settings.get("serial_port")
    banner = await stagectl.ctl.connect(port)
    state.stage["connected"] = True
    state.stage["port"] = stagectl.ctl.port
    state.stage["needs_home"] = stagectl.ctl.needs_home
    state.stage["last_error"] = ""
    await push_log("스테이지 연결: %s" % stagectl.ctl.port, "ok")
    for ln in (banner or "").splitlines():
        if ln.strip():
            await push_log("펌웨어: %s" % ln.strip())
    try:
        engine._apply_status(await stagectl.ctl.status())
    except stagectl.StageError as e:
        await push_log("상태 조회 실패 · " + logger.short(e), "warn")
    state.stage["fw"] = stagectl.ctl.fw_version
    if stagectl.ctl.fw_version and stagectl.ctl.fw_version != "V7":
        await push_log("펌웨어 %s · 수동 원점은 V7 이 필요합니다"
                       % stagectl.ctl.fw_version, "warn")
    if not (state.stage["homed_x"] and state.stage["homed_y"]):
        state.stage["needs_home"] = True
        await push_log("원점 없음 · 수동 이동에서 끝단까지 옮긴 뒤 원점 등록", "warn")
    await push_state()


async def _stage_disconnect(_data):
    await stagectl.ctl.disconnect()
    # 끊긴 뒤의 위치는 아는 값이 아니다. 마지막 값을 그대로 두면 화면이 '지금
    # 거기 있다' 고 거짓말한다 - 전부 None 으로 내리고 화면은 '—' 를 그린다.
    state.stage.update({"connected": False, "port": None, "moving": False,
                        "x_mm": None, "y_mm": None, "u": None, "v": None,
                        "homed_x": False, "homed_y": False})
    await push_log("스테이지 연결 해제", "warn")
    await push_state()


async def _home_touch(data):
    """끝단 맞춤. 입력한 거리만큼만 끝단 쪽으로 밀고 물러나 0 으로 등록한다.

    거리를 화면에서 받는다 - 예전처럼 스트로크 전체를 미는 기본값을 쓰면 이미
    끝에 닿아 있을 때 수십 초를 갈아 먹는다(2026-09-11).
    """
    if not state.stage["connected"]:
        await push_log("스테이지 미연결 · 연결 후 사용하세요", "warn")
        return
    axis = str(data.get("axis") or "xy").lower()
    default_mm = stagectl.stage_mod.HOME_SEARCH_MM_DEFAULT
    try:
        search_mm = float(data.get("search_mm", default_mm))
    except (TypeError, ValueError):
        search_mm = default_mm
    if search_mm > stagectl.stage_mod.HOME_SEARCH_MM_MAX:
        await push_log("끝단 맞춤 거리는 %gmm 까지입니다 (%g)"
                       % (stagectl.stage_mod.HOME_SEARCH_MM_MAX, search_mm), "warn")
        return
    await push_log("끝단 맞춤 (%s · %gmm) · 끝에 닿는 소리는 정상입니다"
                   % (axis, search_mm))
    state.stage["moving"] = True
    await push_state()
    try:
        st = await stagectl.ctl.find_zero(axis, search_mm)
        engine._apply_status(st)
        engine.mark_moved()                # 원점도 저장 대상이다
        if st["homed_x"] and st["homed_y"]:
            engine.reset_estop()           # 비상정지 잠금 해제
            state.stage["last_error"] = "" # 원점을 다시 잡았으니 옛 오류는 지운다
            await push_log("끝단 맞춤 완료 · X%.2f Y%.2f" % (st["x_mm"], st["y_mm"]), "ok")
        else:
            # 축 하나만 잡힌 상태다. '완료' 로 찍으면 안 된다.
            state.stage["needs_home"] = True
            await push_log("끝단 맞춤 중단 · 다시 실행하세요", "warn")
    except stagectl.StageError as e:
        # 비상정지로 끊긴 경우가 대부분이다. 여기서 받아 '중단' 으로 끝낸다
        # (밖으로 던지면 '스테이지 오류' 로만 찍혀 무엇이 중단됐는지 안 보인다).
        state.stage["needs_home"] = True
        state.stage["last_error"] = logger.short(e)
        logger.write("err", "끝단 맞춤 중단 상세: %s" % e)
        await push_log("끝단 맞춤 중단 · " + logger.short(e), "warn")
    finally:
        state.stage["moving"] = False
        await push_state()


async def _park(_data):
    await engine.park()


async def _goto(data):
    if "no" in data:
        await engine.goto_sample(int(data["no"]))
    else:
        await engine.goto_xy(float(data.get("x", 0)), float(data.get("y", 0)))


async def _jog(data):
    """수동 이동.

    원점이 있으면 절대 좌표로 간다 - 목표는 서버가 만들고(화면이 준 좌표를 쓰지
    않는다) 가동범위로 자른다. 원점이 없으면(또는 비상정지 뒤면) 잘라 낼 기준이
    없으므로 상대 이동으로 보낸다. 사람이 캐리지를 끝단까지 몰고 가 원점을
    등록하는 절차가 이 경로다.
    """
    axis = str(data.get("axis") or "").lower()
    if axis not in ("x", "y"):
        await push_log("수동 이동 축이 잘못되었습니다: %s" % axis, "warn")
        return
    try:
        delta = float(data.get("delta_mm", 0))
    except (TypeError, ValueError):
        await push_ack("jog", False, "bad_delta")
        return

    if state.jog_mode() == "rel":
        await engine.jog(axis, delta_mm=delta)
        return

    cur = state.stage["x_mm"] if axis == "x" else state.stage["y_mm"]
    if cur is None:
        await push_log("현재 위치를 모릅니다 · 원점 등록 후 사용하세요", "warn")
        await push_ack("jog", False, "no_position")
        return
    target = min(calib.AXIS_MAX, max(calib.AXIS_MIN, cur + delta))
    if abs(target - cur) < 0.005:          # 이미 끝이다 - 시리얼을 괴롭히지 않는다
        await push_ack("jog", False, "at_limit",
                       x_mm=state.stage["x_mm"], y_mm=state.stage["y_mm"])
        return
    await engine.jog(axis, target_mm=target)


async def _set_origin(data):
    """지금 자리를 원점으로 등록한다(사람이 끝단까지 몰고 온 뒤).

    끝단에 바짝 붙은 자리를 0 으로 잡으면 이후 모든 이동이 하드스톱에 눌린다.
    기본 2mm 물러난 자리를 0 으로 등록한다.
    """
    if not state.stage["connected"]:
        await push_log("스테이지 미연결 · 연결 후 사용하세요", "warn")
        return
    try:
        gap = float(data.get("gap_mm", 2.0))
    except (TypeError, ValueError):
        gap = 2.0
    gap = max(0.0, min(20.0, gap))
    state.stage["moving"] = True
    await push_state()
    try:
        if gap > 0:
            old = await stagectl.ctl.set_speed(stagectl.stage_mod.JOG_SLOW_PPS)
            try:
                await stagectl.ctl.jog_rel("x", gap)
                await stagectl.ctl.jog_rel("y", gap)
            finally:
                with contextlib.suppress(Exception):
                    await stagectl.ctl.set_speed(old)
        st = await stagectl.ctl.set_zero("xy")
        engine._apply_status(st)
        engine.reset_estop()
        state.stage["last_error"] = ""
        engine.mark_moved()                # 자동 저장이 뒤따른다
        await push_log("원점 등록 · %gmm 이격 · X%.2f Y%.2f"
                       % (gap, st["x_mm"], st["y_mm"]), "ok")
    except stagectl.StageError as e:
        state.stage["last_error"] = logger.short(e)
        logger.write("err", "원점 등록 실패 상세: %s" % e)
        await push_log("원점 등록 실패 · " + logger.short(e), "err")
    finally:
        state.stage["moving"] = False
        await push_state()


async def _park_here(_data):
    """지금 서 있는 자리를 파킹 위치로 저장한다."""
    x, y = state.stage["x_mm"], state.stage["y_mm"]
    if x is None or y is None:
        await push_log("현재 위치를 모릅니다 · 원점 설정 후 사용하세요", "warn")
        return
    xy = [round(float(x), 1), round(float(y), 1)]
    state.save_settings({"park_xy": xy})
    await push_log("파킹 위치 저장 · X%.1f Y%.1f" % (xy[0], xy[1]), "ok")
    await push_state()


async def _capture(_data):
    await engine.capture()


async def _run(data):
    reasons = engine.needs_confirm()
    if reasons and not data.get("confirm"):
        await push_ack("run", False, "needs_confirm", reasons)
        return
    only = data.get("only")
    only = set(int(n) for n in only) if only else None
    ok = await engine.start_run(str(data.get("mode") or "auto"),
                                data.get("dwell_s", state.settings.get("dwell_s", 5)),
                                only)
    await push_ack("run", ok, "" if ok else "start_failed")


async def _pause(_data):
    await engine.pause()


async def _resume(_data):
    await engine.resume()


async def _next(_data):
    await engine.next_step()


async def _stop(_data):
    await engine.stop()


async def _set_on(data):
    s = state.sample(int(data.get("no", -1)))
    if s is not None:
        s["on"] = bool(data.get("on", True))
    await push_state()


async def _set_all(data):
    on = bool(data.get("on", True))
    for s in state.samples:
        s["on"] = on
    await push_state()


async def _measure_here(_data):
    import measure as measure_mod
    m = measure_mod.make(state.settings)
    r = await m.measure({"no": None, "X": state.stage["x_mm"], "Y": state.stage["y_mm"]},
                        {"params": state.params})
    await push_log("현재 위치 측정: %s" % ("값 없음" if not r else r), "info")
    await push_state()


async def _open_out_dir(_data):
    """결과 폴더를 탐색기로 연다. 창 없이 도는 환경에서는 경로만 로그로 알린다."""
    d = state.sequence.get("out_dir")
    if not d:
        await push_log("아직 결과 폴더가 없습니다 (촬영 후 생깁니다)", "warn")
        return
    opener = getattr(os, "startfile", None)
    if opener is None:
        await push_log("결과 폴더: %s" % d)
        return
    try:
        opener(d)
        await push_log("결과 폴더를 열었습니다: %s" % d, "ok")
    except OSError as e:
        await push_log("폴더를 열지 못했습니다: %s (%s)" % (d, e), "warn")


async def _open_log_dir(_data):
    """날짜별 로그 폴더를 연다."""
    d = paths.LOGS_DIR
    opener = getattr(os, "startfile", None)
    if opener is None:
        await push_log("로그 폴더: %s" % d)
        return
    try:
        opener(d)
        await push_log("로그 폴더를 열었습니다: %s" % d, "ok")
    except OSError as e:
        await push_log("폴더를 열지 못했습니다: %s (%s)" % (d, e), "warn")


async def _open_results(_data):
    """결과 CSV 를 연다. 없으면 폴더를 연다(창 없는 환경에서는 경로만 로그)."""
    d = state.sequence.get("out_dir")
    if not d:
        await push_log("아직 결과 폴더가 없습니다 (촬영 후 생깁니다)", "warn")
        return
    csv_p = os.path.join(d, "results.csv")
    target = csv_p if os.path.exists(csv_p) else d
    opener = getattr(os, "startfile", None)
    if opener is None:
        await push_log("결과 %s: %s" % ("CSV" if target is csv_p else "폴더", target))
        return
    try:
        opener(target)
        await push_log("결과를 열었습니다: %s" % target, "ok")
    except OSError as e:
        await push_log("열지 못했습니다: %s (%s)" % (target, e), "warn")


async def _settings_save(data):
    patch = {k: data[k] for k in ("serial_port", "camera_index", "park_xy", "dwell_s",
                                  "marker_mm_xy", "measure") if k in data}
    old_index = state.camera["index"]
    state.save_settings(patch)
    calib.set_marker_mm(state.settings.get("marker_mm_xy"))
    if state.camera["index"] != old_index and state.camera.get("preview"):
        vision.holder.reopen(state.params)   # 카메라 번호가 바뀌었다
    await push_log("설정을 저장했습니다 (다음 촬영부터 적용)", "ok")
    await push_state()


async def _exit(_data):
    await push_log("종료 · 파킹 후 위치 저장", "warn")
    if engine.busy():
        # 순회 중이면 먼저 멈춘다. 이동 한가운데서 포트를 닫으면 스테이지가
        # 그 이동을 끝까지 하고 우리는 그 위치를 모른 채 끝난다.
        await push_log("순회를 정지하고 기다립니다", "warn")
        await engine.stop()
        for _ in range(600):               # 최대 60초
            if not engine.busy():
                break
            await asyncio.sleep(0.1)
    try:
        if state.stage["connected"] and state.can_move() is None:
            await engine.park()
            await stagectl.ctl.save()
    except Exception as e:                 # noqa: BLE001
        await push_log("종료 정리 중 오류: %s" % e, "warn")
    try:
        await stagectl.ctl.disconnect()
    except Exception:                      # noqa: BLE001
        pass
    await push_ack("exit", True)
    await asyncio.sleep(0.2)
    if _shutdown_handler:
        _shutdown_handler()


_TABLE = {
    "stage_connect": _stage_connect,
    "stage_disconnect": _stage_disconnect,
    "home_touch": _home_touch,
    "set_origin": _set_origin,
    "park": _park,
    "goto": _goto,
    "jog": _jog,
    "park_here": _park_here,
    "capture": _capture,
    "run": _run,
    "pause": _pause,
    "resume": _resume,
    "next": _next,
    "stop": _stop,
    "set_on": _set_on,
    "set_all": _set_all,
    "measure_here": _measure_here,
    "open_out_dir": _open_out_dir,
    "open_results": _open_results,
    "open_log_dir": _open_log_dir,
    "preview_start": _preview_start,
    "preview_stop": _preview_stop,
    "list_ports": _list_ports,
    "settings_save": _settings_save,
    "exit": _exit,
}
