"""히트건(R1). 켠 주체를 구분한다: "수동"(릴레이 켜기 버튼) / "측정"(측정 작업 - RelayHeater).
수동: 온도계 없이 켜짐 · 온도를 못 읽어도 안 꺼짐 · 최대 켜짐 시간과 (온도가 들어오면) 차단 온도에서는 꺼짐.
측정: 온도를 읽을 수 있어야 켜짐 · 온도를 못 읽으면 꺼짐 · 차단 온도 · 최대 켜짐 시간.
공통: 끈 뒤 바로 다시 켤 수 있음(다시 켜기 대기 없음) · 켜진 동안 X · Y 이동 거절 · 켜고 끈 것과 까닭이 프로그램 로그에 남음."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import BY_KEY, Config
from app.safety import BY_MANUAL, BY_RUN, Rejected
from app.server import create_app
from tests.conftest import FakeClock, with_thermometer


def _heater_alarm(ctrl):
    a = [x for x in ctrl.state.snapshot()["alarms"] if x["code"] == "heater"]
    return a[0]["text"] if a else None


def _log(ctrl, text):
    return [ln for ln in ctrl.logs.recent if text in ln]


# ---- 수동 ----
def test_manual_heater_turns_on_without_thermometer(make_ctrl):
    ctrl = make_ctrl(meters=False)                            # 온도계 없음 · 온도를 한 번도 못 읽음
    assert ctrl.k2110 is None and ctrl.safety.last_temp is None
    ctrl.run_command("relay_on", {"n": 1})                    # 릴레이 켜기 버튼
    assert ctrl.fake_fw.relay[0] is True and ctrl.state.get("heater_on_since") is not None
    assert ctrl.safety.heater_by == BY_MANUAL
    assert _log(ctrl, "히트건 켬 · 수동")
    ctrl.run_command("relay_off", {"n": 1})
    assert ctrl.fake_fw.relay[0] is False and ctrl.state.get("heater_on_since") is None
    assert _log(ctrl, "히트건 끔 · 수동")


def test_manual_heater_stays_on_when_thermometer_is_lost(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)                             # 온도계가 있고 온도 30 °C 를 읽었다
    ctrl.relay_on(1)
    ctrl.k2110 = None                                         # 켠 뒤 온도계를 끊었다
    clock.advance(10.0)                                       # temp_stale_s(3 초)를 훌쩍 넘김
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is True, "수동으로 켰으면 온도를 못 읽어도 끄지 않는다"
    assert _heater_alarm(ctrl) is None


def test_manual_heater_cut_at_max_on_time(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)               # 온도는 끝까지 안 들어온다
    ctrl.relay_on(1)
    clock.advance(179.0)
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is True
    clock.advance(2.0)                                        # 181 초
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is False and ctrl.state.get("heater_on_since") is None
    assert "가동 · 차단" in _heater_alarm(ctrl)
    assert _log(ctrl, "히트건 끔 · 최대 켜짐 시간")


def test_manual_heater_cut_at_max_temperature_when_temperature_comes_in(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.relay_on(1)                                          # 온도 없이 켰다
    clock.advance(1.0)
    ctrl.feed_temperature(80.0)
    assert ctrl.fake_fw.relay[0] is True
    clock.advance(1.0)
    ctrl.feed_temperature(112.0)                              # 온도가 들어오면 차단 온도(110 °C)에서 끈다
    assert ctrl.fake_fw.relay[0] is False
    assert "도달 · 히트건 차단" in _heater_alarm(ctrl)
    assert _log(ctrl, "히트건 끔 · 차단 온도")
    # 켤 때 이미 차단 온도 이상이면(방금 읽은 온도) 거절
    with pytest.raises(Rejected) as e:
        ctrl.relay_on(1)
    assert "차단 온도" in str(e.value) and ctrl.fake_fw.relay[0] is False
    # 그 온도가 낡았으면(더 안 들어오면) 수동은 온도 조건을 보지 않는다
    clock.advance(10.0)
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True


# ---- 측정 ----
def test_run_heater_needs_readable_temperature(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    heater = ctrl.runner.heater                               # 측정이 쓰는 가열 장치
    with pytest.raises(Rejected) as e:
        heater.on()                                           # 온도계 없음
    assert "온도" in str(e.value) and ctrl.fake_fw.relay[0] is False
    with_thermometer(ctrl, clock)
    clock.advance(5.0)                                        # 읽은 지 5 초 - 낡은 온도
    with pytest.raises(Rejected):
        heater.on()
    ctrl.feed_temperature(30.0)
    heater.on()
    assert ctrl.fake_fw.relay[0] is True and heater.is_on and ctrl.safety.heater_by == BY_RUN
    assert _log(ctrl, "히트건 켬 · 측정")
    heater.off()
    assert ctrl.fake_fw.relay[0] is False and _log(ctrl, "히트건 끔 · 측정")


def test_run_heater_cut_when_temperature_unreadable(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.runner.heater.on()
    clock.advance(2.0)
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is True                      # 3 초 안에는 버틴다
    clock.advance(2.0)
    ctrl.check_heater()
    assert ctrl.fake_fw.relay[0] is False
    assert "온도 측정 실패" in _heater_alarm(ctrl)
    assert _log(ctrl, "히트건 끔 · 온도 못 읽음")


def test_run_heater_cut_at_max_temperature_and_max_on_time(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.runner.heater.on()
    assert ctrl.state.get("heater_on_since") is not None
    ctrl.feed_temperature(112.0)                              # 110 °C 이상
    assert ctrl.fake_fw.relay[0] is False and ctrl.state.has_alarm("heater") and ctrl.state.get("heater_on_since") is None
    ctrl.run_command("alarm_clear", {"code": "heater"})
    ctrl.feed_temperature(40.0)
    ctrl.runner.heater.on()                                   # 끈 뒤 바로 다시 켠다(기다리는 시간 없음)
    for _ in range(18):
        clock.advance(10.0)
        ctrl.feed_temperature(40.0)                           # 온도는 계속 읽힌다
    assert ctrl.fake_fw.relay[0] is False
    assert "가동 · 차단" in _heater_alarm(ctrl)


# ---- 공통 ----
def test_heater_can_be_turned_on_again_right_after_off(make_ctrl):
    """다시 켜기 대기는 없다: 끈 직후(시계가 가지 않아도) 수동 · 측정 모두 바로 켜진다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    for _ in range(3):
        ctrl.relay_on(1)
        assert ctrl.fake_fw.relay[0] is True
        ctrl.relay_off(1)
        assert ctrl.fake_fw.relay[0] is False
    ctrl.runner.heater.on()
    ctrl.runner.heater.off()
    ctrl.runner.heater.on()
    assert ctrl.fake_fw.relay[0] is True
    assert not hasattr(ctrl.safety, "heater_off_t")


def test_xy_moves_refused_while_heater_on_but_z_allowed(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    for turn_on, turn_off in ((lambda: ctrl.relay_on(1), lambda: ctrl.relay_off(1)), (ctrl.runner.heater.on, ctrl.runner.heater.off)):   # 수동 · 측정 모두
        turn_on()
        for call in (lambda: ctrl.jog("x", 1.0), lambda: ctrl.jog("y", 1.0), lambda: ctrl.goto(20, 20)):
            with pytest.raises(Rejected) as e:
                call()
            assert "히트건 켜짐 · X · Y 이동 불가" == str(e.value)
        turn_off()
    ctrl.relay_on(1)
    z0 = ctrl.state.get("pos", "z")
    ctrl.jog("z", 1.0)                                        # Z 는 움직인다
    assert abs(ctrl.state.get("pos", "z") - z0 - 1.0) < 0.01
    # 화면에는 명령 응답의 msg 가 알림으로 뜬다(ui/core.js api())
    with TestClient(create_app(ctrl)) as c:
        r = c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()
        assert r == {"ok": False, "msg": "히트건 켜짐 · X · Y 이동 불가"}
        ctrl.relay_off(1)
        assert c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()["ok"] is True


def test_estop_locks_heater_for_manual_too(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.relay_on(1)
    ctrl.estop("시험")
    assert ctrl.fake_fw.relay[0] is False and _log(ctrl, "히트건 끔 · 비상정지")
    with pytest.raises(Rejected) as e:
        ctrl.relay_on(1)
    assert str(e.value) == "비상정지 상태"
    clock.advance(2.0)                                               # 정지 직후 2초는 해제를 받지 않는다
    ctrl.run_command("estop_release")
    ctrl.relay_on(1)
    assert ctrl.fake_fw.relay[0] is True


# ---- 설정 ----
def test_heater_limits_are_settings_and_apply_at_once(make_ctrl, tmp_path):
    """차단 온도 · 최대 켜짐 시간 · 온도 못 읽음 허용 시간은 "전체 설정"에서 바꿀 수 있고, 바꾼 값이 바로 감시에 쓰인다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with TestClient(create_app(ctrl)) as c:
        keys = {r["key"] for r in c.get("/api/settings").json()}
        assert {"heater_max_c", "heater_max_on_s", "temp_stale_s", "run_no_rise_s", "run_no_rise_c", "run_fast_rise_c_s"} <= keys
        assert "heater_min_off_s" not in keys and "heater_min_off_s" not in BY_KEY, "다시 켜기 대기 설정은 없앴다"
        assert c.put("/api/settings", json={"heater_max_c": 50, "heater_max_on_s": 5, "temp_stale_s": 1}).json() == {"ok": True}
        # 차단 온도 50 °C
        ctrl.relay_on(1)
        ctrl.feed_temperature(49.0)
        assert ctrl.fake_fw.relay[0] is True
        ctrl.feed_temperature(51.0)
        assert ctrl.fake_fw.relay[0] is False and "51 °C 도달" in _heater_alarm(ctrl)
        # 최대 켜짐 시간 5 초
        ctrl.feed_temperature(30.0)
        ctrl.relay_on(1)
        clock.advance(4.0)
        ctrl.check_heater()
        assert ctrl.fake_fw.relay[0] is True
        clock.advance(1.5)
        ctrl.check_heater()
        assert ctrl.fake_fw.relay[0] is False and "히트건 5초 가동" in _heater_alarm(ctrl)
        # 온도 못 읽음 허용 시간 1 초(측정이 켰을 때)
        with_thermometer(ctrl, clock)
        ctrl.runner.heater.on()
        clock.advance(1.5)
        ctrl.check_heater()
        assert ctrl.fake_fw.relay[0] is False and "온도 측정 실패" in _heater_alarm(ctrl)


def test_old_settings_file_with_removed_key_loads_quietly(tmp_path):
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"heater_min_off_s": 10.0, "heater_max_c": 95.0}), encoding="utf-8")
    cfg = Config(p).load()
    assert cfg.loaded_notes == [] and cfg["heater_max_c"] == 95.0 and "heater_min_off_s" not in cfg.values
    cfg.save()
    assert "heater_min_off_s" not in json.loads(p.read_text(encoding="utf-8"))
