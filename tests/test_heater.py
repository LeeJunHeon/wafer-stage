"""히트건(R1): 온도계 없으면 거절 · 110 °C 에서 꺼짐 · 온도 못 읽으면 꺼짐 · 최대 켜짐 시간 · 다시 켜기 대기 · 켜진 동안 X · Y 이동 거절."""

import pytest

from app.safety import Rejected
from tests.conftest import FakeClock, with_thermometer


def test_heater_refused_without_thermometer(make_ctrl):
    ctrl = make_ctrl(meters=False)
    with pytest.raises(Rejected) as e:
        ctrl.relay_on(1)
    assert "온도" in str(e.value)
    assert ctrl.fake_fw.relay[0] is False


def test_heater_on_with_thermometer_and_cuts_at_limit(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True
    assert ctrl.state.get("heater_on_since") is not None
    ctrl.feed_temperature(112.0)                              # 110 °C 이상
    assert ctrl.fake_fw.relay[0] is False
    assert ctrl.state.has_alarm("heater")
    assert ctrl.state.get("heater_on_since") is None


def test_heater_cut_when_temperature_unreadable(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    clock.advance(2.0)
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is True                      # 3 초 안에는 버틴다
    clock.advance(2.0)
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is False
    assert "온도 측정 실패" in [a for a in ctrl.state.snapshot()["alarms"] if a["code"] == "heater"][0]["text"]


def test_heater_cut_at_max_on_time(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    for _ in range(18):
        clock.advance(10.0)
        ctrl.feed_temperature(40.0)                           # 온도는 계속 읽힌다
    assert ctrl.fake_fw.relay[0] is False
    assert "가동 · 차단" in [a for a in ctrl.state.snapshot()["alarms"] if a["code"] == "heater"][0]["text"]


def test_heater_cannot_restart_within_min_off_time(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    ctrl.relay_off(1)
    clock.advance(4.0)
    ctrl.feed_temperature(30.0)
    with pytest.raises(Rejected) as e:
        ctrl.relay_on(1)
    assert "히트건 대기" in str(e.value)
    clock.advance(7.0)                                        # 끈 지 11 초
    ctrl.feed_temperature(30.0)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True


def test_xy_moves_refused_while_heater_on_but_z_allowed(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.relay_on(1)
    for call in (lambda: ctrl.jog("x", 1.0), lambda: ctrl.jog("y", 1.0), lambda: ctrl.goto(20, 20)):
        with pytest.raises(Rejected) as e:
            call()
        assert "히트건 켜짐 · X · Y 이동 불가" == str(e.value)
    ctrl.jog("z", 1.0)                                        # Z 는 움직인다
    assert abs(ctrl.state.get("pos", "z") - 1.0) < 0.01
