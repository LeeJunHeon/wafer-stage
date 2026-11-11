"""비상정지: '!' 가 바로 나감 · 이동 중 다른 줄 없음 · 릴레이 꺼짐 · 기다리던 명령 버려짐(세대) · 잠금."""

import threading

import pytest

from app.devices import stage as stage_mod
from app.safety import Rejected
from tests.conftest import wait_for


def start_long_move(ctrl, result):
    def run():
        try:
            ctrl.goto(200.0, 10.0)
        except Rejected as e:
            result.append(str(e))
    t = threading.Thread(target=run)
    t.start()
    assert wait_for(lambda: ctrl.stage.moving, 5), "이동이 시작되지 않았다"
    return t


def test_bang_during_move_stops_and_turns_relays_off(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    ctrl.k2450.output(True)
    ctrl.relay_on(2)
    assert fw.relay[1] is True
    res = []
    t = start_long_move(ctrl, res)
    n_bang = len(fw.bang_times)
    ctrl.estop()
    t.join(10)
    assert len(fw.bang_times) == n_bang + 1, "'!' 가 가짜 펌웨어에 도착해야 한다"
    assert fw.cuts and fw.cuts[-1]["out"] < fw.cuts[-1]["cmd"], "이동이 중간에 끊겨야 한다"
    assert res and "중단" in res[0], res
    assert not any(fw.relay), "릴레이가 전부 꺼져야 한다"
    assert ctrl.lab.out_on is False, "K2450 출력이 꺼져야 한다"
    assert ctrl.state.get("estop_locked") is True


def test_nothing_but_bang_is_written_while_moving(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    res = []
    t = start_long_move(ctrl, res)
    i = max(k for k, ln in enumerate(fw.log) if ln.startswith("gx"))
    ctrl.estop()
    t.join(10)
    after = fw.log[i + 1:]
    assert after and after[0] == "r all 0", "이동 줄 다음에 나간 줄은 r all 0 뿐이어야 한다: %s" % after[:3]
    assert fw.overflow == 0


def test_queued_command_is_discarded_by_generation(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    g = ctrl.safety.generation
    st = ctrl.stage
    f1 = st.submit("gx 16000", g, stage_mod.is_move_done, 10, kind="move")
    assert wait_for(lambda: st.moving, 5)
    f2 = st.submit("gy 5000", g, stage_mod.is_move_done, 10, kind="move")
    ctrl.estop()
    with pytest.raises(stage_mod.MoveAborted):
        f1.result(10)
    with pytest.raises(stage_mod.Discarded):
        f2.result(10)
    assert not any(ln.startswith("gy") for ln in fw.log), "기다리던 gy 는 펌웨어에 가면 안 된다"


def test_estop_locks_moves_and_relay_on_until_release(make_ctrl):
    ctrl = make_ctrl()
    ctrl.estop()
    with pytest.raises(Rejected) as e:
        ctrl.jog("x", 1.0)
    assert "비상정지" in str(e.value)
    with pytest.raises(Rejected):
        ctrl.relay_on(2)
    with pytest.raises(Rejected):
        ctrl.goto(10, 10)
    ctrl.relay_off(2)                                   # 끄기는 된다
    ctrl.estop_release()
    assert ctrl.state.get("estop_locked") is False
    assert not ctrl.state.has_alarm("estop")
    ctrl.relay_on(2)
    assert ctrl.fake_fw.relay[1] is True


def test_estop_without_stage_still_locks_and_turns_k2450_off(make_ctrl):
    ctrl = make_ctrl(connect=False)
    ctrl.connect_meter("k2450")
    ctrl.k2450.output(True)
    assert ctrl.lab.out_on is True
    ctrl.estop()
    assert ctrl.state.get("estop_locked") is True
    assert ctrl.lab.out_on is False
