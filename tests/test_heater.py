"""히트건 = 송풍(heater_fan_relay R1) + 열선(heater_element_relay R10). 켜고 끄는 순서는 Controller 의 히트건 함수 한 곳이 정한다.
켜기: 열선 → heater_start_gap_s → 송풍(송풍이 켜져 있으면 열선만) · 끄기: 열선 → heater_fan_after_s 뒤 송풍 · 송풍만 켜는 길은 없다(인터락).
켠 주체를 구분한다: "수동"(히트건 버튼) / "측정"(측정 작업 - RelayHeater). 안전 판정은 열선을 본다.
수동: 온도계 없이 켜짐 · 온도를 못 읽어도 안 꺼짐 · 열선 연속 최대 시간과 (온도가 들어오면) 차단 온도에서는 열선 꺼짐.
측정: 온도를 읽을 수 있어야 켜짐 · 온도를 못 읽으면 열선 꺼짐 · 차단 온도 · 열선 연속 최대 시간.
공통: 끈 뒤 바로 다시 켤 수 있음 · 히트건(송풍 또는 열선)이 켜진 동안 X · Y 이동 거절 · 켜고 끈 것과 까닭이 프로그램 로그에 남음.
시험용 히트건(tests/fakes/fake_heatgun.py)의 인터락 오류 · 송풍 단독 켜짐은 conftest 가 시험마다 끝에 확인한다."""

import json
import re

import pytest
from fastapi.testclient import TestClient

from app.config import BY_KEY, Config
from app.devices.stage import StageError
from app.safety import BY_MANUAL, BY_RUN, Rejected
from app.server import create_app
from tests.conftest import FakeClock, with_thermometer

FAN, EL = 0, 9                                                       # 시험용 보드 릴레이 목록의 자리(R1 송풍 · R10 열선)


def _heater_alarm(ctrl):
    a = [x for x in ctrl.state.snapshot()["alarms"] if x["code"] == "heater"]
    return a[0]["text"] if a else None


def _log(ctrl, text):
    return [ln for ln in ctrl.logs.recent if text in ln]


def _relay_cmds(ctrl, start=0):
    return [c for c in ctrl.fake_fw.log[start:] if re.match(r"r (\d+|all) [01]$", c)]


def _gun(ctrl):
    fw = ctrl.fake_fw
    return fw.relay[FAN], fw.relay[EL]


# ---- 켜는 순서 · 끄는 순서 ----
def test_on_sequence_is_element_then_fan_after_gap(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    n0 = len(ctrl.fake_fw.log)
    t0 = clock()
    ctrl.run_command("heater_on")
    assert _relay_cmds(ctrl, n0) == ["r 10 1", "r 1 1"], "열선 먼저 → 송풍"
    log = ctrl.heatgun.log
    (t_el, f1, e1), (t_fan, f2, e2) = log[-2], log[-1]
    assert (f1, e1, f2, e2) == (False, True, True, True)
    assert t_el == t0 and t_fan - t_el == ctrl.cfg["heater_start_gap_s"] == 1.0, "열선과 송풍 사이 heater_start_gap_s"
    assert ctrl.heatgun.heating() and ctrl.state.get("heater") == {"fan": True, "element": True, "fan_off_at": None}
    assert _log(ctrl, "히트건 켬 · 수동")


def test_off_sequence_element_now_fan_after_delay(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.run_command("heater_on")
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("heater_off")
    assert _relay_cmds(ctrl, n0) == ["r 10 0"], "끄기: 열선만 바로"
    assert _gun(ctrl) == (True, False) and ctrl.state.get("heater", "fan_off_at") is not None
    assert _log(ctrl, "히트건 끔 · 수동 · 송풍 30초 뒤 끔")
    with pytest.raises(Rejected) as e:
        ctrl.jog("x", 1.0)                                           # 송풍만 켜져 있어도 히트건 켜짐
    assert str(e.value) == "히트건 켜짐 · X · Y 이동 불가"
    clock.advance(29.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, False), "heater_fan_after_s(30초) 전에는 송풍 유지"
    clock.advance(1.5)
    ctrl.check_heater()
    assert _gun(ctrl) == (False, False) and ctrl.state.get("heater_on_since") is None
    assert _relay_cmds(ctrl, n0) == ["r 10 0", "r 1 0"] and _log(ctrl, "송풍 끔 · 열선 끈 뒤 30초")
    ctrl.jog("x", 1.0)                                               # 다 꺼지면 X · Y 이동


def test_turning_on_again_while_fan_runs_only_turns_element_on(make_ctrl):
    """끄는 중(송풍만 켜짐)에 다시 켜면 송풍이 켜져 있으므로 열선만 켠다 - 송풍 늦게 끄기는 거둔다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.run_command("heater_on")
    ctrl.run_command("heater_off")
    clock.advance(10.0)
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("heater_on")
    assert _relay_cmds(ctrl, n0) == ["r 10 1"] and _gun(ctrl) == (True, True) and ctrl.heatgun.heating()
    assert ctrl.state.get("heater", "fan_off_at") is None
    clock.advance(60.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, True), "다시 켰으면 송풍을 끄지 않는다"


def test_element_toggles_while_fan_stays_on(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("heater_element", {"on": True})             # 송풍이 꺼진 채 열선만은 안 켠다(켜기 순서는 [켜기])
    assert str(e.value) == "송풍 꺼짐 · 히트건 [켜기] 필요" and _gun(ctrl) == (False, False)
    ctrl.run_command("heater_on")
    for _ in range(3):
        ctrl.run_command("heater_element", {"on": False})
        assert _gun(ctrl) == (True, False) and not ctrl.heatgun.heating()
        ctrl.run_command("heater_element", {"on": True})
        assert _gun(ctrl) == (True, True) and ctrl.heatgun.heating(), "송풍을 켜 둔 채 열선만 바꾸면 인터락 오류 없음"
    clock.advance(100.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, True), "열선만 끈 것은 히트건 끄기가 아니다 - 송풍을 끄지 않는다"
    assert ctrl.heatgun.errors == []


def test_single_relay_buttons_cannot_switch_fan_or_element(make_ctrl):
    ctrl = make_ctrl(meters=False)
    for cmd, n in (("relay_on", 1), ("relay_on", 10), ("relay_off", 1), ("relay_off", 10)):
        with pytest.raises(Rejected) as e:
            ctrl.run_command(cmd, {"n": n})
        assert str(e.value) == "히트건 릴레이 · 히트건 버튼 사용"
    with pytest.raises(Rejected) as e:
        ctrl.run_command("relay_on", {"n": 5})                       # 미배정 릴레이(예전 쿨러 R5)
    assert str(e.value) == "R5 미배정 · 쿨러 릴레이 아님"
    with pytest.raises(Rejected):
        ctrl.run_command("cooler_only", {"n": 10})                    # 초기 설정 7 [켜 보기] 도 쿨러 릴레이만
    with pytest.raises(Rejected):
        ctrl.run_command("raw", {"target": "stage", "line": "r 1 1"})   # 직접 명령은 읽기만
    ctrl.run_command("relay_on", {"n": 3})
    ctrl.run_command("cooler_only", {"n": 4})
    assert [i + 1 for i, r in enumerate(ctrl.fake_fw.relay) if r] == [4], "켜 보기는 누른 쿨러 하나만"
    assert not any(c.startswith(("r 1 1", "r 10 1")) for c in ctrl.fake_fw.log)


def test_failed_fan_step_turns_both_off_and_refuses(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    real = ctrl.stage.relay

    def relay(n, on, gen, prio=5):
        if n == 1 and on:
            raise StageError("응답 없음")
        return real(n, on, gen, prio=prio)
    ctrl.stage.relay = relay
    with pytest.raises(Rejected) as e:
        ctrl.run_command("heater_on")
    assert str(e.value).startswith("R1 켜기 실패")
    assert _gun(ctrl) == (False, False), "실패하면 둘 다 끈다"
    assert _log(ctrl, "히트건 끔 · 켜기 실패")


def test_element_cut_during_start_gap_never_turns_fan_on(make_ctrl):
    """켜기 순서의 대기 중에 안전 감시가 열선을 끄면 송풍만 켜지 않는다(그 뒤 열선을 켜면 인터락 오류가 되므로)."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.relay_on(2)
    ctrl.sleep = lambda s: (clock.advance(s), ctrl.feed_temperature(112.0))   # 기다리는 동안 차단 온도
    with pytest.raises(Rejected) as e:
        ctrl.run_command("heater_on")
    assert str(e.value) == "히트건 켜기 취소됨 · 열선 꺼짐"
    assert _gun(ctrl) == (False, False) and ctrl.heatgun.fan_alone == []


# ---- 수동 ----
def test_manual_heater_turns_on_without_thermometer(make_ctrl):
    ctrl = make_ctrl(meters=False)                            # 온도계 없음 · 온도를 한 번도 못 읽음
    assert ctrl.k2110 is None and ctrl.safety.last_temp is None
    ctrl.run_command("heater_on")                             # 히트건 [켜기]
    assert _gun(ctrl) == (True, True) and ctrl.state.get("heater_on_since") is not None
    assert ctrl.safety.heater_by == BY_MANUAL
    assert _log(ctrl, "히트건 켬 · 수동")
    ctrl.run_command("heater_off")
    assert _gun(ctrl)[1] is False and _log(ctrl, "히트건 끔 · 수동")


def test_manual_heater_stays_on_when_thermometer_is_lost(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)                             # 온도계가 있고 온도 30 °C 를 읽었다
    ctrl.heater_on(BY_MANUAL)
    ctrl.k2110 = None                                         # 켠 뒤 온도계를 끊었다
    clock.advance(10.0)                                       # temp_stale_s(3 초)를 훌쩍 넘김
    ctrl.check_heater()
    assert _gun(ctrl) == (True, True), "수동으로 켰으면 온도를 못 읽어도 끄지 않는다"
    assert _heater_alarm(ctrl) is None


def test_manual_element_cut_at_max_continuous_time_fan_later(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)               # 온도는 끝까지 안 들어온다
    ctrl.heater_on(BY_MANUAL)
    clock.advance(178.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, True)
    clock.advance(2.0)                                        # 열선 연속 180 초(켜기 대기 1 초 포함)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, False), "열선만 바로 끈다"
    assert "연속 가동 · 차단" in _heater_alarm(ctrl) and _log(ctrl, "히트건 끔 · 최대 켜짐 시간")
    clock.advance(31.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (False, False), "송풍은 heater_fan_after_s 뒤"
    assert ctrl.state.get("heater_on_since") is None


def test_max_continuous_time_counts_only_unbroken_element_on(make_ctrl):
    """열선 연속 최대 시간은 열선이 끊김 없이 켜진 시간이다: 송풍을 켜 둔 채 열선을 껐다 켜면 다시 센다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.heater_on(BY_MANUAL)
    for _ in range(3):
        clock.advance(150.0)
        ctrl.check_heater()
        ctrl.heater_element(False, BY_MANUAL)
        ctrl.heater_element(True, BY_MANUAL)
    assert _gun(ctrl) == (True, True) and _heater_alarm(ctrl) is None, "합쳐 450 초여도 한 번에 150 초씩"


def test_cut_temperature_turns_element_off_now_and_fan_later(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.heater_on(BY_MANUAL)                                 # 온도 없이 켰다
    clock.advance(1.0)
    ctrl.feed_temperature(80.0)
    assert _gun(ctrl) == (True, True)
    clock.advance(1.0)
    n0 = len(ctrl.fake_fw.log)
    ctrl.feed_temperature(112.0)                              # 온도가 들어오면 차단 온도(110 °C)에서 끈다
    assert _relay_cmds(ctrl, n0) == ["r 10 0"] and _gun(ctrl) == (True, False), "열선만 즉시"
    assert "도달 · 히트건 차단" in _heater_alarm(ctrl)
    assert _log(ctrl, "히트건 끔 · 차단 온도")
    clock.advance(30.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (False, False), "송풍은 뒤에 꺼진다"
    # 켤 때 이미 차단 온도 이상이면(방금 읽은 온도) 거절
    ctrl.feed_temperature(112.0)
    with pytest.raises(Rejected) as e:
        ctrl.heater_on(BY_MANUAL)
    assert "차단 온도" in str(e.value) and _gun(ctrl) == (False, False)
    # 그 온도가 낡았으면(더 안 들어오면) 수동은 온도 조건을 보지 않는다
    clock.advance(10.0)
    ctrl.heater_on(BY_MANUAL)
    assert _gun(ctrl) == (True, True)


# ---- 측정 ----
def test_run_heater_needs_readable_temperature(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    heater = ctrl.runner.heater                               # 측정이 쓰는 가열 장치
    with pytest.raises(Rejected) as e:
        heater.on()                                           # 온도계 없음
    assert "온도" in str(e.value) and _gun(ctrl) == (False, False)
    with_thermometer(ctrl, clock)
    clock.advance(5.0)                                        # 읽은 지 5 초 - 낡은 온도
    with pytest.raises(Rejected):
        heater.on()
    ctrl.feed_temperature(30.0)
    heater.on()
    assert _gun(ctrl) == (True, True) and heater.is_on and heater.element_on and ctrl.safety.heater_by == BY_RUN
    assert _log(ctrl, "히트건 켬 · 측정")
    heater.off()
    assert _gun(ctrl) == (True, False) and heater.fan_pending and _log(ctrl, "히트건 끔 · 측정")


def test_run_heater_cut_when_temperature_unreadable(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.runner.heater.on()                                   # 켜기 대기 1 초 동안 온도는 안 들어온다
    clock.advance(1.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, True)                         # 3 초 안에는 버틴다
    clock.advance(2.0)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, False)
    assert "온도 측정 실패" in _heater_alarm(ctrl)
    assert _log(ctrl, "히트건 끔 · 온도 못 읽음")
    with pytest.raises(Rejected):
        ctrl.runner.heater.element(True)                      # 온도를 못 읽으면 측정은 열선을 다시 켜지 못한다


def test_run_heater_cut_at_max_temperature_and_max_on_time(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    ctrl.runner.heater.on()
    assert ctrl.state.get("heater_on_since") is not None
    ctrl.feed_temperature(112.0)                              # 110 °C 이상
    assert _gun(ctrl)[1] is False and ctrl.state.has_alarm("heater")
    ctrl.run_command("alarm_clear", {"code": "heater"})
    ctrl.feed_temperature(40.0)
    ctrl.runner.heater.on()                                   # 끈 뒤 바로 다시 켠다(송풍이 켜져 있어 열선만)
    for _ in range(18):
        clock.advance(10.0)
        ctrl.feed_temperature(40.0)                           # 온도는 계속 읽힌다
    assert _gun(ctrl)[1] is False
    assert "연속 가동 · 차단" in _heater_alarm(ctrl)


# ---- 공통 ----
def test_heater_can_be_turned_on_again_right_after_off(make_ctrl):
    """다시 켜기 대기는 없다: 끈 직후(시계가 가지 않아도) 수동 · 측정 모두 바로 켜진다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)
    for _ in range(3):
        ctrl.run_command("heater_on")
        assert _gun(ctrl) == (True, True)
        ctrl.run_command("heater_off")
        assert _gun(ctrl)[1] is False
    ctrl.feed_temperature(30.0)
    ctrl.runner.heater.on()
    ctrl.runner.heater.off()
    ctrl.feed_temperature(30.0)
    ctrl.runner.heater.on()
    assert _gun(ctrl) == (True, True)
    assert BY_KEY["heater_min_off_s"].group == "가열 제어", "열선 최소 꺼짐은 측정의 가열 속도 제어에만 쓴다(수동 다시 켜기 대기 아님)"


def test_xy_moves_refused_while_heater_on_but_z_allowed(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with_thermometer(ctrl, clock)

    def off_now(fn):
        fn()
        clock.advance(31.0)
        ctrl.check_heater()
    for turn_on, turn_off in ((lambda: ctrl.heater_on(BY_MANUAL), lambda: off_now(lambda: ctrl.heater_off(BY_MANUAL))),
                              (ctrl.runner.heater.on, lambda: off_now(ctrl.runner.heater.off))):    # 수동 · 측정 모두
        ctrl.feed_temperature(30.0)
        turn_on()
        for call in (lambda: ctrl.jog("x", 1.0), lambda: ctrl.jog("y", 1.0), lambda: ctrl.goto(20, 20)):
            with pytest.raises(Rejected) as e:
                call()
            assert "히트건 켜짐 · X · Y 이동 불가" == str(e.value)
        turn_off()
    ctrl.heater_on(BY_MANUAL)
    z0 = ctrl.state.get("pos", "z")
    ctrl.jog("z", 1.0)                                        # Z 는 움직인다
    assert abs(ctrl.state.get("pos", "z") - z0 - 1.0) < 0.01
    # 화면에는 명령 응답의 msg 가 알림으로 뜬다(ui/core.js api())
    with TestClient(create_app(ctrl)) as c:
        r = c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()
        assert r == {"ok": False, "msg": "히트건 켜짐 · X · Y 이동 불가"}
        off_now(lambda: ctrl.heater_off(BY_MANUAL))
        assert c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()["ok"] is True


def test_estop_turns_everything_off_at_once_and_locks(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.heater_on(BY_MANUAL)
    ctrl.relay_on(3)
    n0 = len(ctrl.fake_fw.log)
    ctrl.estop("시험")
    assert "r all 0" in _relay_cmds(ctrl, n0) and not any(ctrl.fake_fw.relay), "비상정지는 송풍까지 전부 바로 끈다"
    assert ctrl.state.get("heater") == {"fan": False, "element": False, "fan_off_at": None}
    assert _log(ctrl, "히트건 끔 · 비상정지")
    with pytest.raises(Rejected) as e:
        ctrl.heater_on(BY_MANUAL)
    assert str(e.value) == "비상정지 상태"
    clock.advance(2.0)                                               # 정지 직후 2초는 해제를 받지 않는다
    ctrl.run_command("estop_release")
    ctrl.heater_on(BY_MANUAL)
    assert _gun(ctrl) == (True, True)


def test_all_off_button_follows_off_order(make_ctrl):
    """[전체 끄기]: 쿨러는 바로, 히트건은 열선 → 송풍(뒤에)."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    ctrl.heater_on(BY_MANUAL)
    ctrl.relay_on(2)
    ctrl.run_command("relays_off")
    assert _gun(ctrl) == (True, False) and not ctrl.fake_fw.relay[1]
    clock.advance(30.0)
    ctrl.check_heater()
    assert not any(ctrl.fake_fw.relay)


# ---- 설정 ----
def test_heater_limits_are_settings_and_apply_at_once(make_ctrl, tmp_path):
    """차단 온도 · 열선 연속 최대 시간 · 온도 못 읽음 허용 시간은 "전체 설정"에서 바꿀 수 있고, 바꾼 값이 바로 감시에 쓰인다."""
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    with TestClient(create_app(ctrl)) as c:
        keys = {r["key"] for r in c.get("/api/settings").json()}
        assert {"heater_max_c", "heater_max_on_s", "temp_stale_s", "run_no_rise_s", "run_no_rise_c", "run_fast_rise_c_s",
                "heater_fan_relay", "heater_element_relay", "cooler_relays", "heater_start_gap_s", "heater_fan_after_s"} <= keys
        assert "heater_relay" not in keys and "heater_relay" not in BY_KEY, "히트건 릴레이 하나는 없앴다(송풍 · 열선 둘)"
        # 이 세 값만 바꾸면 다른 설정과 맞지 않는다(가열 끝 온도 85 °C ≥ 차단 50 °C 등) → 조합 검사가 이유와 함께 거절한다
        r = c.put("/api/settings", json={"heater_max_c": 50, "heater_max_on_s": 5, "temp_stale_s": 1})
        assert r.status_code == 400 and r.json()["msg"] == "가열 끝 온도 85 °C · 차단 온도 50 °C 미만 필요"
        assert ctrl.cfg["heater_max_c"] == 110.0, "거절되면 하나도 바뀌지 않는다"
        # 맞는 조합으로 같이 바꾼다(가열 끝 온도 · 무반응 시간 · 읽기 시간 · 최소 켜짐도 함께 줄인다)
        patch = {"heater_max_c": 50, "heater_max_on_s": 5, "temp_stale_s": 1, "run_heater_off_c": 45, "run_no_rise_s": 3, "heater_min_on_s": 1,
                 "run_interval_s": 0.05, "meter_avg_count": 1, "meter_nplc": 0.1, "temp_read_s": 0.05, "read_overhead_s": 0.05}
        assert c.put("/api/settings", json=patch).json() == {"ok": True}
        # 차단 온도 50 °C
        ctrl.heater_on(BY_MANUAL)
        ctrl.feed_temperature(49.0)
        assert _gun(ctrl)[1] is True
        ctrl.feed_temperature(51.0)
        assert _gun(ctrl)[1] is False and "51 °C 도달" in _heater_alarm(ctrl)
        # 열선 연속 최대 시간 5 초
        ctrl.feed_temperature(30.0)
        ctrl.heater_on(BY_MANUAL)
        clock.advance(4.0)
        ctrl.check_heater()
        assert _gun(ctrl)[1] is True
        clock.advance(1.5)
        ctrl.check_heater()
        assert _gun(ctrl)[1] is False and "열선 5초 연속 가동" in _heater_alarm(ctrl)
        # 온도 못 읽음 허용 시간 1 초(측정이 켰을 때)
        with_thermometer(ctrl, clock)
        ctrl.runner.heater.on()
        clock.advance(1.5)
        ctrl.check_heater()
        assert _gun(ctrl)[1] is False and "온도 측정 실패" in _heater_alarm(ctrl)


def test_gap_and_fan_after_times_are_settings(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False, heater_start_gap_s=2.5, heater_fan_after_s=5.0)
    t0 = clock()
    ctrl.heater_on(BY_MANUAL)
    assert ctrl.heatgun.log[-1][0] - t0 == 2.5
    ctrl.heater_off(BY_MANUAL)
    clock.advance(4.9)
    ctrl.check_heater()
    assert _gun(ctrl) == (True, False)
    clock.advance(0.2)
    ctrl.check_heater()
    assert _gun(ctrl) == (False, False)


def test_old_settings_file_is_migrated(tmp_path):
    """예전 settings.json(히트건 릴레이 하나 · 쿨러 9개 배치): heater_relay 는 버리고, 쿨러 릴레이 밖 번호를 쓰는 배치는 기본값으로 + 초기 설정 7 미완료.
    로그에 한 줄씩 남기고(경보는 없음) 새 형식으로 저장한다."""
    p = tmp_path / "settings.json"
    p.write_text(json.dumps({"heater_relay": 1, "cooler_grid": [[2, 3, 4], [5, 6, 7], [8, 9, 10]], "heater_max_c": 95.0}), encoding="utf-8")
    cfg = Config(p).load()
    assert cfg["heater_max_c"] == 95.0 and cfg["cooler_grid"] == [[2, 2, 2], [3, 3, 3], [4, 4, 4]] and cfg.cooler_reset
    assert cfg.loaded_notes == [] and len(cfg.migrated) == 2 and "heater_relay" in cfg.migrated[0] and "쿨러 배치" in cfg.migrated[1]
    from app.calib import Calib
    from tests.conftest import build_ctrl
    from tests.fakes import fake_visa
    data = tmp_path / "data"
    Calib(data).set_cooler()                                         # 예전에 초기 설정 7 을 끝냈다
    p.write_text(json.dumps({"heater_relay": 3, "cooler_grid": [[2, 3, 4], [5, 6, 7], [8, 9, 10]], "data_dir": str(data), "auto_connect": False}),
                 encoding="utf-8")
    c2 = build_ctrl(Config(p).load())
    try:
        assert c2.calib.d["cooler"] is None and c2.prep.steps()[6] == {"done": False, "text": "기본 배치 · 확인 필요"}
        lines = [ln for ln in c2.logs.recent if "설정 이행" in ln]
        assert len(lines) == 2 and not [a for a in c2.state.snapshot()["alarms"] if a["code"] == "config"]
        saved = json.loads(p.read_text(encoding="utf-8"))
        assert "heater_relay" not in saved and saved["cooler_grid"] == [[2, 2, 2], [3, 3, 3], [4, 4, 4]] and saved["heater_element_relay"] == 10
        assert Config(p).load().migrated == [], "한 번 옮기면 다음에는 그대로"
    finally:
        c2.shutdown()
        fake_visa.uninstall()
    # 역할이 겹친 파일은 역할 세 값을 기본값으로
    p.write_text(json.dumps({"heater_fan_relay": 2, "cooler_relays": [2, 3]}), encoding="utf-8")
    cfg = Config(p).load()
    assert (cfg["heater_fan_relay"], cfg["heater_element_relay"], cfg["cooler_relays"]) == (1, 10, [2, 3, 4]) and "겹침" in cfg.migrated[0]
