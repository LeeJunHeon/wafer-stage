"""비상정지: '!' 가 바로 나감 · 이동 중 다른 줄 없음 · 릴레이 꺼짐 · 기다리던 명령 버려짐(세대) · 잠금."""

import threading

import pytest

from app.devices import stage as stage_mod
from app.safety import Rejected
from tests.conftest import FakeClock, wait_for, with_thermometer


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


def test_estop_marks_heater_off_at_once_and_leaves_only_the_estop_alarm(make_ctrl):
    """비상정지는 히트건 감시를 그 순간 "꺼짐"으로 돌린다. 릴레이 끄기 응답을 기다리는 사이에 시간이 흐르고 감시가 돌아도
    "온도를 3 초 넘게 읽지 못해 히트건을 껐습니다" 경보가 덧붙지 않는다(비상정지 뒤 경보는 비상정지 하나)."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True and ctrl.state.get("heater_on_since") is not None
    seen = {}
    orig = ctrl.stage.bang

    def slow_bang():                                              # '!' 를 쓰기 직전: 릴레이는 아직 켜져 있고, 그 사이에 10 초가 흐르고 감시가 돈다
        clock.advance(10.0)
        seen.update(since=ctrl.state.get("heater_on_since"), relay=ctrl.fake_fw.relay[0])
        ctrl.check_heater()
        return orig()

    ctrl.stage.bang = slow_bang
    ctrl.estop("시험")
    assert seen == {"since": None, "relay": True}, "비상정지를 받은 순간 화면의 히트건은 꺼짐이어야 한다"
    assert [a["code"] for a in ctrl.state.snapshot()["alarms"]] == ["estop"], ctrl.state.snapshot()["alarms"]
    assert ctrl.fake_fw.relay[0] is False and ctrl.state.get("heater_on_since") is None
    # 끈 뒤 다시 켜기 대기(10 초)는 비상정지 순간부터 센다
    ctrl.run_command("estop_release")
    ctrl.feed_temperature(30.0)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True
