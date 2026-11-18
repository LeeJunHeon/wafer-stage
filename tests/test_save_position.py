"""종료 · 연결 끊기 · 측정 끝의 위치 저장(save): 펌웨어 V11 은 위치를 RAM 에만 두고 EEPROM 에 "save 이후 움직였는가" 만 적는다.
포트를 열면 보드가 재시작하고, save 없이 움직였으면 "save 하지 않고 꺼짐 → 위치 불신" 으로 원점이 없어진다(시험용 보드 FakeV11.reopen 이 같은 규칙).
닫기 · [연결 끊기] 는 이동을 멈추고 릴레이를 끈 뒤 save → st 로 DIRTY=0 을 확인한다. 비상정지 잠금 중이면 저장하지 않는다."""

import threading

from app.jobs.run import DONE
from tests.conftest import SEARCH, wait_for
from tests.fakes.fake_v11 import FakeV11


def _mm(fw):
    return (fw.px / 160.0, fw.py / 160.0, fw.pz / 160.0)


def _homed(ctrl):
    return all(ctrl.state.get("homed", a) for a in "xyz")


def test_shutdown_saves_and_next_start_keeps_the_origin(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    ctrl.set_origin("all", "here")
    ctrl.jog("x", 5.0)
    ctrl.jog("y", 3.0)
    assert fw.dirty, "움직이면 DIRTY"
    pos = _mm(fw)
    ctrl.shutdown()
    assert not fw.dirty and fw.eeprom == "restored", "닫을 때 save"
    assert any(ln for ln in ctrl.logs.recent if "위치 저장 · 종료 · X 5.000 · Y 3.000" in ln)
    again = make_ctrl(board=fw)                                      # 다시 실행 = 같은 보드를 다시 연다(보드 재시작)
    assert _homed(again) and _mm(again.fake_fw) == pos, "원점 있음 · 위치 같음"
    assert again.state.get("stage_mem")["code"] == "restored"


def test_without_save_the_origin_is_lost(make_ctrl):
    """비교: save 없이 다시 열면(강제 종료처럼) 원점이 없다 - 펌웨어 설계대로."""
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    ctrl.set_origin("all", "here")
    ctrl.jog("x", 5.0)
    fw.reopen()
    assert fw.eeprom == "unsaved" and not (fw.hx or fw.hy or fw.hz)


def test_disconnect_saves_and_reconnect_keeps_the_origin(make_ctrl):
    ctrl = make_ctrl(keep_board=True)
    fw = ctrl.fake_fw
    ctrl.set_origin("all", "here")
    ctrl.jog("x", 4.0)
    ctrl.run_command("disconnect_stage")
    assert not fw.dirty
    ctrl.run_command("connect_stage")
    assert ctrl.fake_fw is fw and _homed(ctrl) and abs(ctrl.state.get("pos", "x") - 4.0) < 1e-6


def test_shutdown_while_moving_stops_then_saves(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    ctrl.set_origin("all", "here")
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", _try(lambda: ctrl.goto(200.0, 10.0))), daemon=True)
    t.start()
    assert wait_for(lambda: ctrl.stage.moving and fw.busy and fw.log and fw.log[-1].startswith("gx"), 5)
    ctrl.shutdown()
    t.join(10)
    assert fw.cuts and fw.cuts[-1]["out"] < fw.cuts[-1]["cmd"], "이동 중이면 멈춘다"
    assert not fw.dirty and fw.saved == (fw.px, fw.py, fw.pz), "멈춘 위치(st 와 같은 카운터)를 저장"
    again = make_ctrl(board=fw)
    assert _homed(again) and 0.0 < again.state.get("pos", "x") < 200.0


def _try(fn):
    try:
        return fn()
    except Exception as e:                                           # noqa: BLE001 - 정지로 취소된 이동
        return e


def test_estop_locked_shutdown_does_not_save(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    ctrl.set_origin("all", "here")
    ctrl.jog("x", 5.0)
    ctrl.estop("시험")
    ctrl.shutdown()
    assert fw.dirty, "비상정지 잠금 중이면 저장하지 않는다"
    assert any("위치 저장 건너뜀 · 종료 · 비상정지 잠금" in ln for ln in ctrl.logs.recent)
    again = make_ctrl(board=fw)
    assert not _homed(again) and again.state.get("stage_mem")["code"] == "unsaved", "다음 실행 때 원점을 다시 잡는다"


def test_run_end_saves_position(ready_ctrl):
    ctrl = ready_ctrl(time_scale=200.0, **dict(SEARCH, run_heat_rate_c_min=20.0, run_cool_rate_c_min=20.0))
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 90)
    assert ctrl.runner.items[use[0]["id"]]["status"] == DONE
    assert ctrl.fake_fw.dirty is False and ctrl.stage.dirty is False, "측정 끝(핀 맨 위) 뒤 DIRTY=0"
    assert any("위치 저장 · 측정 끝" in ln for ln in ctrl.logs.recent)


def test_banner_eeprom_state_goes_to_screen(make_ctrl):
    for code in ("restored", "unsaved", "none"):
        fw = FakeV11(time_scale=0.05, homed=code == "restored", eeprom=code)
        ctrl = make_ctrl(board=fw)
        mem = ctrl.state.get("stage_mem")
        assert mem["code"] == code and mem["text"], mem
        assert _homed(ctrl) == (code == "restored")
        assert any(mem["text"] in ln for ln in ctrl.logs.recent)
        ctrl.shutdown()
