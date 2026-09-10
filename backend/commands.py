"""commands.py - 화면 명령 처리(handle_command).

서버가 상태의 주인이다: 명령으로 상태를 바꾸고 갱신된 state 를 push 한다.
화면은 요청만 보내고, 돌아온 state 를 보고서야 자기 화면을 바꾼다.

이동 계열 명령은 '시리얼 연결 + 원점' 이 있어야 통과한다. 연결·원점잡기·비상정지·
설정은 언제나 통과해야 사람이 상황을 정리할 수 있다.
"""

import asyncio
import os

from core import calib

import engine
import logger
import stagectl
from connection import push_ack, push_log, push_state
from state import state

_shutdown_handler = None

MOVE_CMDS = ("park", "goto", "run", "resume", "next", "measure_here")
# 순회 중에 받으면 안 되는 명령. 스테이지·카메라·설정을 순회 도중에 건드리면
# 진행 중인 이동과 충돌한다(정지 뒤에 하면 된다).
BUSY_BLOCKED = ("park", "goto", "stage_home", "stage_disconnect", "capture",
                "settings_save")


def set_shutdown_handler(fn):
    global _shutdown_handler
    _shutdown_handler = fn


async def handle_command(data):
    cmd = str(data.get("cmd") or "")
    try:
        if cmd in BUSY_BLOCKED and engine.busy():
            await push_log("순회 중 - 정지 후 사용하세요 (%s)" % cmd, "warn")
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
        state.stage["last_error"] = str(e)
        await push_log("스테이지 오류: %s" % e, "err")
        await push_state()
    except Exception as e:                 # noqa: BLE001
        logger.exc("명령 처리 실패(%s)" % cmd, e)
        await push_log("명령 처리 실패(%s): %s" % (cmd, e), "err")
        await push_state()


# --------------------------------------------------------------------------
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
        await push_log("상태 조회 실패: %s" % e, "warn")
    if not (state.stage["homed_x"] and state.stage["homed_y"]):
        state.stage["needs_home"] = True
        await push_log("원점이 없습니다 - '원점 잡기(fz)' 를 먼저 실행하세요", "warn")
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


async def _stage_home(data):
    if not state.stage["connected"]:
        await push_log("스테이지 미연결 - 연결 후 사용하세요", "warn")
        return
    axis = str(data.get("axis") or "xy").lower()
    await push_log("원점 탐색 시작 (%s) - 끝에 닿으면 드르륵 소리가 정상입니다" % axis)
    state.stage["moving"] = True
    await push_state()
    try:
        st = await stagectl.ctl.find_zero(axis, data.get("search_pulses"))
        engine._apply_status(st)
        if st["homed_x"] and st["homed_y"]:
            engine.reset_estop()           # 비상정지 잠금 해제
            state.stage["last_error"] = "" # 원점을 다시 잡았으니 옛 오류는 지운다
        else:
            state.stage["needs_home"] = True
        await push_log("원점 설정 완료 - X%.2f Y%.2f" % (st["x_mm"], st["y_mm"]), "ok")
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


async def _estop(_data):
    # 확인 없이 즉시. 잠금 검사도 거치지 않는다(항상 가능해야 한다).
    await engine.estop()


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
    state.save_settings(patch)
    calib.set_marker_mm(state.settings.get("marker_mm_xy"))
    await push_log("설정을 저장했습니다 (다음 촬영부터 적용)", "ok")
    await push_state()


async def _exit(_data):
    await push_log("종료합니다 - 파킹 후 저장", "warn")
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
    "stage_home": _stage_home,
    "park": _park,
    "goto": _goto,
    "capture": _capture,
    "run": _run,
    "pause": _pause,
    "resume": _resume,
    "next": _next,
    "stop": _stop,
    "estop": _estop,
    "set_on": _set_on,
    "set_all": _set_all,
    "measure_here": _measure_here,
    "open_out_dir": _open_out_dir,
    "open_results": _open_results,
    "settings_save": _settings_save,
    "exit": _exit,
}
