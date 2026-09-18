"""commands.py - 화면 명령 처리(handle_command).

서버가 상태의 주인이다: 명령으로 상태를 바꾸고 갱신된 state 를 push 한다.
화면은 요청만 보내고, 돌아온 state 를 보고서야 자기 화면을 바꾼다.

이동 계열 명령은 '시리얼 연결 + 원점' 이 있어야 통과한다. 연결·원점잡기·비상정지·
설정은 언제나 통과해야 사람이 상황을 정리할 수 있다.
"""

import asyncio
import os

from core import paths

import engine
import logger
import stagectl
import vision
from connection import push_ack, push_log, push_state, run_on_loop
import state as state_mod
from state import state

_shutdown_handler = None

# can_move(연결·원점·비상정지) 를 요구하는 명령. jog 는 여기 넣지 않는다 -
# 원점이 없을 때 상대 이동으로 끝단까지 몰고 가는 것이 원점을 잡는 절차라서,
# 여기서 막으면 원점을 영영 못 잡는다(_jog 가 모드별로 따로 검사한다).
MOVE_CMDS = ("park", "goto", "return_origin", "run", "resume", "next",
             "measure_here")
# 스테이지가 이미 움직이고 있으면 받지 않는다. 명령이 동시에 실행될 수 있게 된
# 뒤로는(ws_endpoint 가 태스크로 띄운다) 이 검사가 없으면 두 이동이 겹친다.
MOVING_BLOCKED = ("park", "goto", "return_origin", "jog", "touch_end",
                  "set_origin", "capture", "run", "measure_here")
# 순회 중에 받으면 안 되는 명령. 스테이지·카메라·설정을 순회 도중에 건드리면
# 진행 중인 이동과 충돌한다(정지 뒤에 하면 된다).
BUSY_BLOCKED = ("park", "goto", "return_origin", "jog", "park_here", "touch_end",
                "set_origin", "stage_disconnect", "capture", "settings_save")


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
    # needs_home 은 '비상정지 뒤' 전용이다. 원점이 없다는 사실은 homed_x/homed_y
    # 가 이미 말하고 can_move 가 그것으로 잠근다. 연결 직후에 이것을 세웠더니
    # 비상정지가 없었는데도 화면이 "비상정지 · 원점 등록 필요" 라고 했고,
    # jog_mode 가 세 축 모두 rel 로 굳어 Z 를 등록해도 Z 가 상대 이동이었다.
    state.stage["needs_home"] = False
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
    if stagectl.ctl.fw_version and stagectl.ctl.fw_version != "V9":
        await push_log("펌웨어 %s · 이 앱은 V9 이 필요합니다"
                       % stagectl.ctl.fw_version, "warn")
    _warn_fw_limits(banner)
    if not (state.stage["homed_x"] and state.stage["homed_y"]):
        await push_log("원점 없음 · 수동 이동에서 끝단까지 민 뒤 원점 등록", "warn")
    elif not state.stage["dirty"]:
        # 저장하고 껐다 - EEPROM 의 위치가 그대로 돌아왔다.
        await push_log("저장된 위치 복원 · X%.2f Y%.2f"
                       % (state.stage["x_mm"], state.stage["y_mm"]), "ok")
    await push_state()


def _warn_fw_limits(banner):
    """앱의 가동범위 설정이 펌웨어 한계(배너의 X 0~… 값)보다 크면 경고를 남긴다.

    펌웨어 값이 기계 한계이고 앱 설정은 그 안에서 쓰는 실사용 범위다. 설정이 더
    크면 펌웨어가 거부하므로 위험하지는 않지만, 화면이 갈 수 있다고 보여 주는
    자리에 실제로는 못 가는 셈이라 알려 둔다.
    """
    fw = stagectl.fw_limits_pulse(banner)
    app = stagectl.limits_mm()
    for ax, pulse in fw.items():
        ppmm = stagectl.stage_mod.Z_PPMM if ax == "z" else stagectl.stage_mod.PPMM
        fw_mm = pulse / ppmm
        app_mm = app[ax + "_max_mm"]
        if app_mm > fw_mm + 1e-6:
            logger.write("warn", "%s 가동범위 설정 %.1f mm 가 펌웨어 한계 %.1f mm 보다 큽니다"
                         % (ax.upper(), app_mm, fw_mm))


async def _stage_disconnect(_data):
    await stagectl.ctl.disconnect()
    # 끊긴 뒤의 위치는 아는 값이 아니다. 마지막 값을 그대로 두면 화면이 '지금
    # 거기 있다' 고 거짓말한다 - 전부 None 으로 내리고 화면은 '—' 를 그린다.
    state.stage.update({"connected": False, "port": None, "moving": False,
                        "x_mm": None, "y_mm": None, "u": None, "v": None,
                        "homed_x": False, "homed_y": False})
    await push_log("스테이지 연결 해제", "warn")
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

    모드는 축마다 따로다. 그 축에 원점이 있으면 절대 좌표로 간다 - 목표는 서버가
    만들고(화면이 준 좌표를 쓰지 않는다) 가동범위로 자른다. 원점이 없으면(또는
    비상정지 뒤면) 잘라 낼 기준이 없으므로 상대 이동으로 보낸다. 사람이 캐리지를
    끝단까지 몰고 가 원점을 등록하는 절차가 이 경로다.
    """
    axis = str(data.get("axis") or "").lower()
    if axis not in ("x", "y", "z"):
        await push_log("수동 이동 축이 잘못되었습니다: %s" % axis, "warn")
        return
    try:
        delta = float(data.get("delta_mm", 0))
    except (TypeError, ValueError):
        await push_ack("jog", False, "bad_delta")
        return

    if state.jog_mode(axis) == "rel":
        await engine.jog(axis, delta_mm=delta)
        return

    cur = state.stage[axis + "_mm"]
    if cur is None:
        await push_log("현재 위치를 모릅니다 · 원점 등록 후 사용하세요", "warn")
        await push_ack("jog", False, "no_position")
        return
    # 가동범위는 설정(limits)에서 온다. 드라이버·보정 둘 다 같은 값으로 맞춰져 있다.
    lo, hi = 0.0, stagectl.limits_mm()[axis + "_max_mm"]
    target = min(hi, max(lo, cur + delta))
    if abs(target - cur) < 0.005:          # 이미 끝이다 - 시리얼을 괴롭히지 않는다
        await push_ack("jog", False, "at_limit",
                       x_mm=state.stage["x_mm"], y_mm=state.stage["y_mm"])
        return
    await engine.jog(axis, target_mm=target)


async def _set_origin(data):
    """지금 자리를 그 축의 원점으로 등록한다(사람이 끝단까지 몰고 온 뒤).

    0 은 언제나 끝단에서 stagectl.ORIGIN_GAP_MM(2mm) 물러난 자리다. 화면에서
    이 거리를 고를 수 없게 한 이유: settings.json 의 마커 좌표가 이 원점을
    기준으로 실측되어 있다. 이격을 빼고 등록하면 하드스톱에 붙은 자리가 0 이
    되어 모든 샘플이 X·Y 로 2mm 씩 모자라게 간다(2026-09-11 실장).
    """
    if not state.stage["connected"]:
        await push_log("스테이지 미연결 · 연결 후 사용하세요", "warn")
        return
    axis = str(data.get("axis") or "xy").lower()
    if axis not in ("x", "y", "z", "xy"):
        await push_log("원점 등록 축이 잘못되었습니다: %s" % axis, "warn")
        return
    gap = stagectl.ORIGIN_GAP_MM
    axes = ["x", "y"] if axis == "xy" else [axis]
    state.stage["moving"] = True
    await push_state()
    try:
        for one in axes:
            await stagectl.ctl.jog_rel(one, gap)
        st = await stagectl.ctl.set_zero(axis)
        engine._apply_status(st)
        if st["homed_x"] and st["homed_y"]:
            engine.reset_estop()
            state.stage["last_error"] = ""
        engine.mark_moved()                # 자동 저장이 뒤따른다
        where = ("Z%.2f" % st["z_mm"] if axis == "z"
                 else "X%.2f Y%.2f" % (st["x_mm"], st["y_mm"]))
        await push_log("원점 등록 (%s · 끝단에서 %g mm) · %s"
                       % (axis.upper().replace("XY", "X·Y"), gap, where), "ok")
    except stagectl.StageError as e:
        state.stage["last_error"] = logger.short(e)
        logger.write("err", "원점 등록 실패 상세: %s" % e)
        await push_log("원점 등록 실패 · " + logger.short(e), "err")
    finally:
        state.stage["moving"] = False
        await push_state()


async def _return_origin(_data):
    """원점 복귀. Z 를 먼저 맨 위로 올린 뒤 X·Y 를 (0, 0) 으로."""
    await engine.return_origin()


async def _touch_end(data):
    """그 축을 0 쪽(끝단)으로 입력한 거리만큼 천천히 민다. 원점은 등록하지 않는다.

    가동범위를 벗어나면 펌웨어가 그 축의 원점을 해제한다(끝에 닿아 탈조하면
    좌표를 믿을 수 없다). 밀기와 등록을 나눈 이유는, 사람이 끝에 닿았다고 판단한
    그때 등록해야 하기 때문이다.
    """
    if not state.stage["connected"]:
        await push_log("스테이지 미연결 · 연결 후 사용하세요", "warn")
        return
    axis = str(data.get("axis") or "").lower()
    if axis not in ("x", "y", "z"):
        await push_log("끝단 이동 축이 잘못되었습니다: %s" % axis, "warn")
        return
    try:
        mm = float(data.get("mm", 5.0))
    except (TypeError, ValueError):
        mm = 5.0
    # Z 는 스트로크가 훨씬 짧다. 0 쪽(Z 는 위)으로 미는 것은 축마다 같다.
    top = stagectl.limits_mm()[axis + "_max_mm"]
    if not (1.0 <= mm <= top):
        await push_log("끝단 이동 거리는 1~%g mm 입니다 (%g)" % (top, mm), "warn")
        return
    was_homed = state.stage["homed_" + axis]
    await push_log("끝단 이동 (%s · %g mm)" % (axis.upper(), mm))
    state.stage["moving"] = True
    await push_state()
    try:
        await stagectl.ctl.jog_rel(axis, -mm)
        engine._apply_status(await stagectl.ctl.status())
        engine.mark_moved()
        if was_homed and not state.stage["homed_" + axis]:
            await push_log("%s 원점 해제 · 끝에 닿으면 원점 등록" % axis.upper(), "warn")
    except stagectl.StageError as e:
        state.stage["last_error"] = logger.short(e)
        logger.write("err", "끝단 이동 중단 상세(%s): %s" % (axis, e))
        await push_log("끝단 이동 중단 · " + logger.short(e), "warn")
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


def _num(v, default):
    try:
        return float(v)
    except (TypeError, ValueError):
        return float(default)


async def _settings_save(data):
    patch = {k: data[k] for k in state_mod.APP_KEYS if k in data}
    # Z 측정 깊이는 Z 가동범위 안이어야 한다(순회 때 그만큼 내린다). 저장하기 전에
    # 거른다 - 저장 뒤에 걸러 봐야 settings.json 에는 이미 잘못 남는다.
    lim = patch.get("limits")
    if not isinstance(lim, dict):
        lim = {}
    # 저장 뒤 실제로 쓰일 Z 상한과 비교한다(1~1000 mm 밖의 값은 무시되고 지금 값이 남는다).
    z_max = _num(lim.get("z_max_mm"), stagectl.z_max_mm())
    if not (stagectl.stage_mod.LIMIT_MIN_MM <= z_max <= stagectl.stage_mod.LIMIT_MAX_MM):
        z_max = stagectl.z_max_mm()
    z_meas = _num(patch.get("z_measure_mm", state.settings.get("z_measure_mm")),
                  state_mod.DEFAULT_APP["z_measure_mm"])
    if z_meas > z_max:
        await push_log("Z 측정 깊이가 가동범위보다 큽니다 (%g > %g mm)"
                       % (z_meas, z_max), "warn")
        await push_ack("settings_save", False, "z_measure_over")
        return
    if "exposure" in patch:
        # 노출값이 있으면 고정 노출, 0 이면 자동. 끄기만 하고 값을 안 주는 조합은
        # 마지막 노출에 얼어붙으므로 화면에서는 두 값을 따로 고르지 못하게 한다.
        patch["auto_exposure"] = _num(patch["exposure"], 0) == 0
    for k in ("width", "height"):
        if k in patch:
            patch[k] = int(_num(patch[k], state_mod.DEFAULT_APP[k]))
            if patch[k] < 16:
                patch[k] = state_mod.DEFAULT_APP[k]
    if "fourcc" in patch:
        patch["fourcc"] = str(patch["fourcc"] or "MJPG").upper()[:4]
    cam_before = {k: state.settings.get(k)
                  for k in ("camera_index", "wb_temperature", "exposure", "auto_exposure",
                            "width", "height", "fourcc")}
    state.save_settings(patch)          # 안에서 마커·가동범위를 core 에 반영한다
    cam_after = {k: state.settings.get(k) for k in cam_before}
    if cam_after != cam_before and state.camera.get("preview"):
        vision.holder.reopen(state.params)   # 카메라 번호·화이트밸런스·노출이 바뀌었다
    await push_log("설정을 저장했습니다 (다음 촬영부터 적용)", "ok")
    await push_state()


async def _exit(_data):
    """종료는 위치를 저장만 하고 끝난다. 스테이지를 움직이지 않는다.

    예전에는 종료가 파킹까지 이동했다. 종료를 누른 사람은 이동을 지시한 것이
    아닌데 축이 움직였고, 그 이동 중에 비상정지가 오면 예외로 빠져 save 없이
    끝나 위치를 잃었다. 위치는 펌웨어 EEPROM 에 남으므로 다음 실행에서 복원된다.

    순회 중·이동 중에는 종료를 받지 않는다 - 종료를 기다리며 이동이 이어지면
    창이 닫힌 뒤 비상정지를 누를 데가 없다.
    """
    if engine.busy():
        await push_log("순회 중 · 정지 후 종료", "warn")
        await push_ack("exit", False, "busy")
        return
    if state.stage["moving"]:
        await push_log("이동 중 · 완료 후 종료", "warn")
        await push_ack("exit", False, "moving")
        return

    await push_log("종료 · 위치 저장 (스테이지는 움직이지 않음)", "warn")
    # 비상정지 상태에서는 저장하지 않는다. 믿을 수 없는 위치를 EEPROM 에 굳히면
    # 다음 전원에서 그 값이 복원된다(원점 기록은 비상정지 때 forget 으로 지웠다).
    if state.stage["connected"] and not engine.estopped():
        try:
            for ln in await stagectl.ctl.save():
                if str(ln).strip():
                    await push_log("펌웨어: %s" % str(ln).strip())
        except Exception as e:             # noqa: BLE001
            await push_log("위치 저장 실패 · " + logger.short(e), "warn")
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
    "touch_end": _touch_end,
    "set_origin": _set_origin,
    "park": _park,
    "goto": _goto,
    "jog": _jog,
    "park_here": _park_here,
    "return_origin": _return_origin,
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
