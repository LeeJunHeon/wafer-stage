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
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock)
    ctrl.estop()
    with pytest.raises(Rejected) as e:
        ctrl.jog("x", 1.0)
    assert "비상정지" in str(e.value)
    with pytest.raises(Rejected):
        ctrl.relay_on(2)
    with pytest.raises(Rejected):
        ctrl.goto(10, 10)
    ctrl.relay_off(2)                                   # 끄기는 된다
    clock.advance(2.0)                                  # 정지 직후 2초는 해제를 받지 않는다
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
    assert [ln for ln in ctrl.logs.recent if "히트건 끔 · 비상정지" in ln], "끈 까닭이 프로그램 로그에 남는다"
    # 비상정지를 풀면 바로 다시 켤 수 있다(다시 켜기 대기는 없다)
    ctrl.run_command("estop_release")
    ctrl.feed_temperature(30.0)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True


# ---- 해제: 정지 직후 2초는 받지 않는다 · 해제는 위치와 릴레이 상태를 다시 읽기만 한다 ----
def test_release_is_refused_right_after_estop_and_allowed_after_two_seconds(make_ctrl):
    """Esc 를 연달아 눌러 바로 풀리지 않게: 정지 뒤 2초 동안은 해제를 거절하고(화면은 state.estop_hold 로 해제 버튼을 꺼 둔다), 지나면 된다."""
    from fastapi.testclient import TestClient
    from app.server import create_app
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock)
    assert ctrl.state.get("estop_hold") is False
    ctrl.estop("시험")
    assert ctrl.state.get("estop_locked") is True and ctrl.state.get("estop_hold") is True
    with pytest.raises(Rejected) as e:
        ctrl.run_command("estop_release")
    assert str(e.value) == "비상정지 직후 · 2초 뒤 해제 가능"
    with TestClient(create_app(ctrl)) as c:                           # 서버 길도 같은 글로 거절한다
        assert c.post("/api/estop/release").json() == {"ok": False, "msg": "비상정지 직후 · 2초 뒤 해제 가능"}
        clock.advance(1.9)
        assert c.post("/api/cmd/estop_release", json={}).json()["ok"] is False
        assert ctrl.state.get("estop_locked") is True and ctrl.state.get("estop_hold") is True, "거절돼도 정지는 그대로"
        clock.advance(0.2)                                            # 2초가 지났다
        assert wait_for(lambda: ctrl.state.get("estop_hold") is False, 3), "해제할 수 있게 되면 화면에 알린다"
        assert ctrl.state.get("estop_locked") is True, "시간이 지났다고 저절로 풀리지 않는다"
        assert c.post("/api/estop/release").json() == {"ok": True, "result": {"locked": False}}
    assert ctrl.state.get("estop_locked") is False and not ctrl.state.has_alarm("estop")
    # 다시 정지하면 2초를 다시 센다
    ctrl.estop("시험")
    clock.advance(1.0)
    ctrl.estop("시험")                                                 # 정지 중에 또 눌렀다: 그때부터 다시 2초
    clock.advance(1.5)
    with pytest.raises(Rejected):
        ctrl.estop_release()
    clock.advance(0.6)
    ctrl.estop_release()
    assert ctrl.state.get("estop_locked") is False


def test_after_release_relays_stay_off_and_dropped_moves_do_not_run(make_ctrl):
    """해제 뒤 상태: 릴레이 · K2450 출력은 꺼진 채 · 대기 중이던 이동(대기 칸의 조그 · 조그의 남은 조각 · 줄 서 있던 이동)은 버려진 채.
    해제는 st · rs 로 위치와 릴레이 상태를 다시 읽기만 한다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, scale=0.4)
    fw = ctrl.fake_fw
    ctrl.relay_on(2)
    ctrl.relay_on(5)
    ctrl.k2450.output(True)
    res = []

    def long_jog():
        try:
            ctrl.jog("y", 120.0)                                     # 50 mm 조각 셋: 첫 조각을 가는 중에 멈춘다(남은 조각은 가지 않는다)
        except Rejected as e:
            res.append(str(e))

    t = threading.Thread(target=long_jog)
    t.start()
    assert wait_for(lambda: ctrl.stage.moving, 5)
    assert ctrl.jog("y", 30.0) == {"queued": True}                   # 대기 칸에 받아 둔 조그
    f2 = ctrl.stage.submit("gx 5000", ctrl.safety.generation, stage_mod.is_move_done, 10, kind="move")      # 줄 서 있는 이동
    ctrl.estop("시험")
    t.join(10)
    assert res == ["이동 중단 · 위치 확인 필요"]
    with pytest.raises(stage_mod.Discarded):
        f2.result(10)
    assert wait_for(lambda: not any(fw.relay), 5) and ctrl.lab.out_on is False
    y = ctrl.state.get("pos", "y")
    assert 0.0 < y < 50.0
    n = len(fw.log)
    clock.advance(2.0)
    assert ctrl.run_command("estop_release") == {"locked": False}
    assert set(fw.log[n:]) <= {"st", "rs"} and "st" in fw.log[n:], "해제는 위치와 릴레이 상태를 다시 읽기만 한다: %s" % fw.log[n:]
    assert wait_for(lambda: not ctrl.stage.moving, 2)
    import time
    time.sleep(0.3)                                                  # 버려진 이동이 뒤늦게 나가지 않는지
    assert not any(c.startswith(("gx", "gy", "gz", "jx", "jy", "jz", "r ")) for c in fw.log[n:]), "버려진 이동 · 릴레이 명령은 살아나지 않는다: %s" % fw.log[n:]
    assert not any(fw.relay) and all(r is False for r in ctrl.state.get("relays")), "릴레이는 꺼진 채"
    assert ctrl.lab.out_on is False, "K2450 출력도 꺼진 채"
    assert ctrl.state.get("heater_on_since") is None and ctrl.state.get("move") is None
    assert ctrl.state.get("pos", "y") == y and ctrl.state.get("pos", "x") == 0.0, "멈춘 위치 그대로"
    assert [a["code"] for a in ctrl.state.snapshot()["alarms"]] == [], "해제 뒤 남는 경보 없음"
    ctrl.jog("x", 1.0)                                               # 풀린 뒤에는 새 명령만 실행된다
    assert abs(ctrl.state.get("pos", "x") - 1.0) < 0.01 and ctrl.state.get("pos", "y") == y
