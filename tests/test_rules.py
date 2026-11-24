"""직접 명령 허용 목록 · 수동 Z 하강 한도 · 설정 조합 검사 · 히트건 릴레이 번호 변경 · 측정 시작 조건(카메라 없음) · 사용 안 할 포트."""

import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app import control as control_mod
from app.config import Config
from app.devices.meters import read_time_s
from app.safety import Rejected
from app.server import create_app
from tests.conftest import FakeClock, alarm_lines, wait_for

PPMM = 160
FAST = 200.0


def _why(fn, *a, **k):
    with pytest.raises(Rejected) as e:
        fn(*a, **k)
    return str(e.value)


# ---------------------------------------------------------------------------
# 3. 직접 명령: 허용 목록
# ---------------------------------------------------------------------------
def test_stage_raw_is_an_allow_list_of_read_only_commands(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    ctrl.jog("x", 5.0)
    n = len(fw.log)
    no = "직접 명령은 읽기만 가능 · 이동 · 원점 · 릴레이 · 설정은 버튼 사용"
    for line in ("x1 200", "x2 200", "wd 0", "wd 60000", "zx", "zy", "zz", "z", "e 1", "e 0", "forget", "save", "v 9000", "v", "a 100", "b 100", "w", "w 5",
                 "r 5 1", "r 5 0", "r all 0", "sp 10 10", "home", "gx 100", "jz 800", "mz 3", "g 10 10", "fz z", "rx 100 2", "st 1", "nonsense"):
        assert _why(ctrl.run_command, "raw", {"target": "stage", "line": line}) == no, line
    sent = [c for c in fw.log[n:] if c not in ("st", "rs")]
    assert sent == [], "거절한 줄은 한 글자도 보드에 가지 않는다: %s" % sent
    assert fw.wd_ms == 2000 and fw.v == 6000 and all(ctrl.state.get("homed", a) for a in "xyz") and not any(fw.relay)
    # 읽기 전용은 된다: st · p · rs · wd(값 없이) · ? - 그대로 스테이지에 넘어간다(직접 명령은 0.8 초 동안 답을 모으므로 넘기는 것만 본다)
    assert control_mod.STAGE_RAW_OK == ("st", "p", "rs", "wd", "?")
    passed, real = [], ctrl.stage.raw
    ctrl.stage.raw = lambda line, gen: passed.append(line) or ["ok"]
    for line in ("st", "p", "rs", "RS", " st ", "?"):
        assert ctrl.run_command("raw", {"target": "stage", "line": line}) == {"reply": "ok"}, line
    assert passed == ["st", "p", "rs", "rs", "st", "?"]
    ctrl.stage.raw = real
    # 실제로 한 번: 값을 붙이지 않은 wd 는 지금 감시 시간만 찍는다(바꾸지 않는다)
    assert "통신 감시 2000ms" in ctrl.run_command("raw", {"target": "stage", "line": "wd"})["reply"] and fw.wd_ms == 2000
    assert abs(ctrl.state.get("pos", "x") - 5.0) < 0.01
    # 비상정지 중에도 읽기는 된다. '!' 는 지금처럼 비상정지
    assert ctrl.run_command("raw", {"target": "stage", "line": "!"}) == {"reply": "비상정지"} and ctrl.state.get("estop_locked") is True
    ctrl.stage.raw = lambda line, gen: passed.append(line) or ["ok"]
    assert ctrl.run_command("raw", {"target": "stage", "line": "st"}) == {"reply": "ok"} and passed[-1] == "st"
    assert _why(ctrl.run_command, "raw", {"target": "stage", "line": "zx"}) == no


def test_meter_raw_is_query_only(make_ctrl):
    ctrl = make_ctrl()
    no = "직접 명령은 조회만 가능 · 출력과 설정은 버튼 사용"
    for line in (":OUTP ON", ":outp on", ":SOUR:VOLT 5", ":SOUR:FUNC VOLT", "*RST", "*CLS", ":SENS:CURR:RANG 1e-3", "*IDN?;:OUTP ON", ":READ?", "READ?", ":MEAS:CURR?", ":INIT", "*TST?"):
        assert _why(ctrl.run_command, "raw", {"target": "k2450", "line": line}) == no, line
    assert ctrl.lab.out_on is False, ":OUTP ON 은 장비에 가지 않는다"
    assert "2450" in ctrl.run_command("raw", {"target": "k2450", "line": "*IDN?"})["reply"]
    assert ctrl.run_command("raw", {"target": "k2450", "line": ":OUTP?"})["reply"] == "0"
    assert ctrl.run_command("raw", {"target": "k2450", "line": "SYST:ERR?"})["reply"]
    for line in ("*RST", 'SENS:FUNC "VOLT"', "MEAS:VOLT?", "UNIT Cel"):                       # 온도계도 조회만(MEAS…? 는 열전대 설정을 푼다)
        assert _why(ctrl.run_command, "raw", {"target": "k2110", "line": line}) == no, line
    assert "2110" in ctrl.run_command("raw", {"target": "k2110", "line": "*IDN?"})["reply"]
    assert float(ctrl.run_command("raw", {"target": "k2110", "line": "READ?"})["reply"]) < 60.0
    assert control_mod.meter_raw_ok("k2450", ":SENS:CURR:RANG? MAX") and not control_mod.meter_raw_ok("k2450", "")


# ---------------------------------------------------------------------------
# 4. 수동 Z 하강 한도(기본 끔)
# ---------------------------------------------------------------------------
def _moves(ctrl, n):
    return [c for c in ctrl.fake_fw.log[n:] if c[:2] in ("gx", "gy", "gz", "jx", "jy", "jz")]


def test_z_floor_is_off_by_default_and_changes_nothing(make_ctrl):
    ctrl = make_ctrl()
    assert ctrl.cfg["z_floor_mode"] == "off" and ctrl.safety.z_floor() == (None, "")
    assert ctrl.prep.view()["z_floor"] == {"mode": "off", "mm": None, "why": ""}
    ctrl.goto(20.0, 20.0, 30.0)
    ctrl.jog("z", 10.0)
    assert ctrl.state.get("pos", "z") == 40.0                        # 지금과 같다: 가동 범위(45 mm) 안이면 어디든


@pytest.mark.parametrize("over,floor", [({"z_floor_mode": "contact", "contact_z_mm": 13.4}, 13.1),          # 접촉 높이 − 여유 0.3
                                        ({"z_floor_mode": "contact", "contact_z_mm": 13.4, "z_floor_margin_mm": 1.0}, 12.4),
                                        ({"z_floor_mode": "fixed", "z_floor_mm": 10.0}, 10.0)])
def test_z_floor_rejects_the_whole_command_below_the_limit(make_ctrl, over, floor):
    ctrl = make_ctrl(**over)
    text = "Z 하강 한도 %.2f mm" % floor
    assert ctrl.safety.z_floor() == (floor, "") and ctrl.prep.view()["z_floor"]["mm"] == floor
    # 좌표 이동(직접 입력): 경계까지는 되고, 그 아래는 X · Y 도 움직이지 않고 통째로 거절한다
    ctrl.goto(10.0, 10.0, floor)
    assert ctrl.state.get("pos", "z") == floor
    n = len(ctrl.fake_fw.log)
    assert _why(ctrl.goto, 30.0, 30.0, round(floor + 0.01, 3)) == text
    assert _moves(ctrl, n) == [] and ctrl.state.get("pos", "x") == 10.0, "한도까지만 가는 것이 아니라 명령 전체를 거절한다"
    assert _why(ctrl.run_command, "goto", {"x": 30, "y": 30, "z": floor + 1}) == text
    # 수동 조그 Z: 한도에 있으면 한 펄스도 더 못 내린다. 올리는 것은 된다. X · Y 는 상관없다
    assert _why(ctrl.jog, "z", 0.01) == text and _moves(ctrl, n) == []
    ctrl.jog("z", -1.0)
    ctrl.jog("z", 1.0)                                               # 한도까지는 내려간다
    assert ctrl.state.get("pos", "z") == floor
    ctrl.jog("z", -0.5)
    assert _why(ctrl.jog, "z", 0.6) == text and abs(ctrl.state.get("pos", "z") - (floor - 0.5)) < 1e-6, "넘는 조그는 조금도 움직이지 않는다"
    ctrl.jog("x", 3.0)
    assert ctrl.state.get("pos", "x") == 13.0


def test_z_floor_needs_a_value_and_does_not_limit_contact(make_ctrl):
    ctrl = make_ctrl(scale=0.02, contact_mode="search", z_floor_mode="contact", contact_z_mm=13.4)
    ctrl.lab.read_dt = 0.002
    cz = ctrl.cfg["contact_z_mm"]                                    # 13.4 → 한도 13.1
    w = ctrl.world
    x, y = w.sample_stage(0)                                         # 표면 13.5 mm - 수동 한도보다 아래
    ctrl.goto(x, y, 13.0)
    # 측정의 접촉 단계 · 접촉 탐색은 이 한도와 별개다(자기 한도가 있다)
    r = ctrl.run_command("contact_find")
    assert abs(r["first_z"] - 13.5) < 1e-6 and ctrl.state.get("pos", "z") > 13.1
    ctrl.run_command("z_top")
    g = ctrl.safety.generation
    assert ctrl.contact_fixed(cz, g, read=False)["z"] == cz and ctrl.state.get("pos", "z") == cz
    ctrl.run_command("z_top")
    # 한도를 켰는데 정할 수 없으면(접촉 높이 없음) 내리는 명령을 거절한다 - 말없이 한도 없이 움직이지 않는다
    ctrl._remember(contact_z_mm=None)
    assert ctrl.prep.view()["z_floor"] == {"mode": "contact", "mm": None, "why": "Z 하강 한도 계산 불가 · 접촉 높이 필요"}
    assert _why(ctrl.jog, "z", 1.0) == "Z 하강 한도 계산 불가 · 접촉 높이 필요"
    assert _why(ctrl.goto, x, y, 5.0) == "Z 하강 한도 계산 불가 · 접촉 높이 필요"
    with TestClient(create_app(ctrl)) as c:                          # 고정 값 방식은 값이 있어야 저장된다
        r = c.put("/api/settings", json={"z_floor_mode": "fixed"})
        assert r.status_code == 400 and r.json()["msg"] == "Z 하강 한도 고정 값 필요"
        assert c.put("/api/settings", json={"z_floor_mode": "fixed", "z_floor_mm": 9.5}).json() == {"ok": True}
        assert _why(ctrl.goto, x, y, 9.6) == "Z 하강 한도 9.50 mm"
        ctrl.goto(x, y, 9.5)
        assert c.get("/api/state").json()["prep"]["z_floor"] == {"mode": "fixed", "mm": 9.5, "why": ""}      # 초기 설정 6 화면에 보이는 값


# ---------------------------------------------------------------------------
# 5. 설정 조합 검사
# ---------------------------------------------------------------------------
def test_read_time_estimate_and_default_settings_pass():
    assert abs(read_time_s(1.0, 10, 60, 3.0, 0.1) - 0.6) < 1e-9      # 10회 × 1 PLC(16.7 ms) × 3 + 0.1
    assert abs(read_time_s(0.1, 1, 60, 3.0, 0.1) - 0.105) < 1e-9 and read_time_s(1.0, 10, 50, 3.0, 0.1) > 0.6
    d = config_mod.defaults()
    assert config_mod.problems(d) == []
    assert abs(config_mod.cycle_time_s(d) - (0.9 + 2 * 0.2 + 10 / 60 * 2.0 + 0.1)) < 1e-9     # 측정 간격 0.9 + 온도 0.2 × 2(저항 앞뒤) + 저항 0.43 < 3 초 × 0.7
    assert config_mod.read_time_s(d) < d["run_interval_s"], "기본 설정의 읽기 시간 예상(온도 두 번 + 저항)은 기본 측정 간격 안"
    for it in config_mod.ITEMS:                                      # 새로 생긴 값도 모두 설명이 있다(전체 설정 · 말풍선)
        assert it.desc.strip() and it.label, it.key


@pytest.mark.parametrize("patch,msg", [
    ({"run_interval_s": 2.0}, "읽기 한 회차 예상 2.83초 · 2.10초 미만 필요 · 측정 간격과 평균 횟수 확인"),     # 간격 + 온도 0.2 × 2 + 저항(평균 × NPLC ÷ 60 × 2 + 0.1)
    ({"meter_avg_count": 40}, "읽기 한 회차 예상 2.73초 · 2.10초 미만 필요 · 측정 간격과 평균 횟수 확인"),
    ({"meter_nplc": 5.0}, "읽기 한 회차 예상 3.07초 · 2.10초 미만 필요 · 측정 간격과 평균 횟수 확인"),
    ({"temp_stale_s": 2.0}, "읽기 한 회차 예상 1.73초 · 1.40초 미만 필요 · 측정 간격과 평균 횟수 확인"),
    ({"run_heater_off_c": 110.0}, "가열 끝 온도 110 °C · 차단 온도 110 °C 미만 필요"),
    ({"run_cool_end_c": 85.0}, "냉각 끝 온도 85 °C · 가열 끝 온도 85 °C 미만 필요"),
    ({"run_start_c": 90.0}, "시작 온도 90 °C · 가열 끝 온도 85 °C 미만 필요"),
    ({"run_no_rise_s": 180.0}, "가열 무반응 시간 180초 · 열선 연속 최대 시간 180초 미만 필요"),
    ({"contact_step_mm": 0.2}, "하강 간격 0.2 mm · 탐색 간격 0.1 mm 이하 필요"),
    ({"contact_search_mm": 0.1, "contact_search_step_mm": 0.2, "contact_search_back_mm": 0.4}, "탐색 간격 0.2 mm · 탐색 거리 0.1 mm 이하 필요"),
    ({"contact_overdrive_mm": 0.6}, "더 누르는 거리 0.6 mm · 저장 높이 아래 한도 0.5 mm 이하 필요"),
    ({"contact_search_back_mm": 0.1}, "탐색 되돌림 거리 0.1 mm · 탐색 간격 × 확인 횟수 0.2 mm 이상 필요"),
    ({"contact_open_a": 1e-3, "contact_ilim_a": 1e-4}, "미접촉 판정 전류 0.001 A · 접촉 확인 전류 한도 0.0001 A 미만 필요"),
    ({"stage_port": "com4"}, "스테이지 포트 com4 · 사용 안 할 포트에 포함됨"),
    ({"run_heat_rate_c_min": 1.0}, "예상 가열 시간 3300초 · 한 단계 시간 한도 1800초 미만 필요 · 가열 속도 확인"),
    ({"run_heat_rate_c_min": 2.0, "run_heat_hold_s": 300.0}, "예상 가열 시간 1950초 · 한 단계 시간 한도 1800초 미만 필요 · 가열 속도 확인"),
    ({"heater_max_on_s": 50.0, "heater_min_on_s": 50.0}, "열선 최소 켜짐 50초 · 열선 연속 최대 시간 50초 미만 필요"),
    ({"heater_band_c": 0}, "목표 아래 허용 폭 0 °C · 0 보다 커야 함"),
    ({"cooler_grid": [[2, 2, 2], [3, 3, 3], [4, 4, 5]]}, "쿨러 배치 R5 · 쿨러 릴레이 아님"),
])
def test_settings_save_rejects_bad_combinations_with_the_reason(make_ctrl, patch, msg):
    ctrl = make_ctrl(connect=False)
    before = dict(ctrl.cfg.values)
    with TestClient(create_app(ctrl)) as c:
        r = c.put("/api/settings", json=patch)
        assert r.status_code == 400 and r.json() == {"ok": False, "msg": msg}
    assert ctrl.cfg.values == before and ctrl.state.get("cfg") == before, "거절되면 하나도 바뀌지 않는다"


def test_combination_check_at_run_start_and_old_problems_do_not_block_other_changes(ready_ctrl):
    ctrl = ready_ctrl(time_scale=FAST, contact_mode="search")
    ctrl.run_command("samples_find")
    with TestClient(create_app(ctrl)) as c:
        # 맞는 조합은 한꺼번에 바꾸면 저장된다
        assert c.put("/api/settings", json={"run_interval_s": 2.0, "temp_stale_s": 6.0}).json() == {"ok": True}
        assert c.put("/api/settings", json={"run_interval_s": 1.0, "temp_stale_s": 3.0}).json() == {"ok": True}
        # 예전 설정 파일에 이미 맞지 않는 조합이 들어 있던 경우(저장 검사를 거치지 않고 들어온 값)
        ctrl.cfg.update({"run_interval_s": 5.0})
        ctrl.publish_cfg()

        def slow_text(**kw):                                         # 숫자는 공식에서(측정 간격 + 온도 읽기 × 2 + 저항 읽기 예상 · 허용 = temp_stale_s × 사용 비율)
            v = dict(ctrl.cfg.values, **kw)
            return "읽기 한 회차 예상 %.2f초 · %.2f초 미만 필요 · 측정 간격과 평균 횟수 확인" % (
                config_mod.cycle_time_s(v), float(v["temp_stale_s"]) * float(v["stale_use_ratio"]))

        slow = slow_text()
        assert config_mod.problems(ctrl.cfg.values) == [slow]
        assert c.post("/api/cmd/run_start", json={}).json() == {"ok": False, "msg": slow}, "측정 시작도 같은 검사를 한다"
        assert not ctrl.runner.active and ctrl._k2450_user is None
        assert c.put("/api/settings", json={"batch_name": "7"}).json() == {"ok": True}, "이미 있던 문제 때문에 다른 값을 못 고치지 않는다"
        r = c.put("/api/settings", json={"run_interval_s": 4.0})                 # 여전히 맞지 않는 값으로는 못 바꾼다
        assert r.status_code == 400 and r.json()["msg"] == slow_text(run_interval_s=4.0)
        assert c.put("/api/settings", json={"run_interval_s": 1.0}).json() == {"ok": True}
        assert c.post("/api/cmd/run_start", json={}).json()["ok"] is True
        ctrl.run_command("run_stop")
        assert wait_for(lambda: not ctrl.runner.active, 30)
        # 재개(이어서 측정)도 검사한다
        ctrl.cfg.update({"temp_stale_s": 1.0})
        r = c.post("/api/cmd/run_resume", json={}).json()
        assert r["ok"] is False and r["msg"] == slow_text(), r


def test_slow_reading_cycle_raises_one_alarm_with_the_cause(ready_ctrl):
    """측정 중 실제 한 회차가 온도 못 읽음 허용 시간을 넘으면 경보 한 번. (설정으로는 맞는데 장비가 느린 경우)
    한 점 = 온도 → 저항 → 온도(읽기 세 번). 읽기 한 번을 25 초(측정 시계)로 늦추면 온도 읽기 사이는 약 50 초(허용 60 초 안 - 안전 감시가 히트건을 끄지 않는다)이고
    한 회차는 75 초 이상(허용 넘음 → run_slow). 예전 40 초는 온도 사이가 80 초라 안전 감시가 먼저 히트건을 꺼 그 회차가 끝나고 경보까지 가지 않을 때가 있었다."""
    ctrl = ready_ctrl(time_scale=FAST, contact_mode="search", temp_stale_s=60.0)     # 허용 시간을 직접 준다(배율 200 에서 실제 0.3 초)
    ctrl.run_command("samples_find")
    for s in [s for s in ctrl.prep.samples if s["use"]][1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    sid = next(s["id"] for s in ctrl.prep.samples if s["use"])
    ctrl.run_command("run_start")
    run = ctrl.runner
    assert wait_for(lambda: run.stage == 3, 20), "가열 단계까지"
    assert not ctrl.state.has_alarm("run_slow"), alarm_lines(ctrl)
    ctrl.lab.read_dt = 25.0                                          # 장비가 느려졌다: 읽기 한 번 25 초(측정 시계) → 온도 사이 약 50 초(< 60) · 한 회차 75 초 이상(> 60)
    assert wait_for(lambda: ctrl.state.has_alarm("run_slow") or not run.active, 20), "경보 또는 측정 끝"
    assert ctrl.state.has_alarm("run_slow"), "한 회차가 허용을 넘었다 · 경보 %s · 히트건 차단 %s" % (alarm_lines(ctrl), ctrl.heater_trips)
    ctrl.lab.read_dt = 0.002
    ctrl.run_command("run_stop")
    assert wait_for(lambda: not run.active, 30)
    al = [a for a in ctrl.state.get("alarms") if a["code"] == "run_slow"]
    assert len(al) == 1 and al[0]["level"] == "경보" and al[0]["dismiss"] is True
    assert al[0]["text"].startswith("읽기 한 회차 ") and al[0]["text"].endswith("초 · 허용 60초 초과 · 측정 간격과 평균 횟수 확인"), al
    assert run.items[sid]["notes"].count("읽기 회차 지연") == 1, "측정마다 한 번만 알린다"
    assert [ln for ln in ctrl.logs.recent if "읽기 한 회차" in ln and "NPLC" in ln], "원인(측정 간격 · NPLC · 평균)을 로그에 남긴다"


# ---------------------------------------------------------------------------
# 6. 릴레이 역할(송풍 · 열선 · 쿨러) 변경
# ---------------------------------------------------------------------------
def test_relay_roles_cannot_change_while_any_relay_is_on(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock, meters=False)
    fw = ctrl.fake_fw
    with TestClient(create_app(ctrl)) as c:
        ctrl.relay_on(3)                                             # 쿨러 하나만 켜져 있어도
        for patch in ({"heater_fan_relay": 5}, {"heater_element_relay": 6}, {"cooler_relays": [2, 3, 4, 7]}):
            r = c.put("/api/settings", json=patch)
            assert r.status_code == 400 and r.json()["msg"] == "릴레이 모두 끈 뒤 변경", patch
        assert (ctrl.cfg["heater_fan_relay"], ctrl.cfg["heater_element_relay"], ctrl.cfg["cooler_relays"]) == (1, 10, [2, 3, 4])
        assert c.put("/api/settings", json={"heater_fan_relay": 1, "batch_name": "3"}).json() == {"ok": True}, "번호가 그대로면 상관없다"
        ctrl.relays_off()
        # 역할이 겹치면 거절(조합 검사)
        for patch, msg in (({"heater_element_relay": 2}, "R2 열선 · 쿨러 릴레이와 겹침"), ({"heater_fan_relay": 10}, "송풍 · 열선 릴레이 R10 겹침"),
                           ({"cooler_relays": [2, 3]}, "쿨러 배치 R4 · 쿨러 릴레이 아님")):
            r = c.put("/api/settings", json=patch)
            assert r.status_code == 400 and r.json()["msg"] == msg, (patch, r.json())
        n = len(fw.log)
        assert c.put("/api/settings", json={"cooler_relays": [2, 3, 4, 7], "heater_element_relay": 5}).json() == {"ok": True}
        assert "rs" in fw.log[n:], "바뀌면 릴레이 상태를 다시 읽는다"
        assert ctrl.state.get("cfg", "heater_element_relay") == 5 and ctrl.state.get("cfg", "cooler_relays") == [2, 3, 4, 7]
        assert [ln for ln in ctrl.logs.recent if "릴레이 역할 변경 · " in ln and "열선 릴레이 10 → 5" in ln]
        # 새 역할 기준: R7 은 쿨러로 켜진다 · R10 은 이제 미배정 · 열선은 R5(배선도 옮겼다 - 시험용 히트건도 R5 를 본다)
        ctrl.relay_on(7)
        assert _why(ctrl.relay_on, 10) == "R10 미배정 · 쿨러 릴레이 아님"
        assert _why(ctrl.relay_on, 5) == "히트건 릴레이 · 히트건 버튼 사용"
        ctrl.relays_off()
        ctrl.heatgun.el_n = 5
        n = len(fw.log)
        ctrl.run_command("heater_on")
        assert [x for x in fw.log[n:] if x.startswith("r ")] == ["r 5 1", "r 1 1"]
        assert _why(ctrl.jog, "x", 1.0) == "히트건 켜짐 · X · Y 이동 불가"
        clock.advance(181.0)
        ctrl.check_heater()
        assert fw.relay[4] is False and fw.relay[0] is True, "열선 연속 최대 시간이 지나면 새 열선(R5)을 끈다 · 송풍은 뒤에"


# ---------------------------------------------------------------------------
# 7. 측정 시작 조건: 카메라 없음
# ---------------------------------------------------------------------------
def test_run_does_not_need_the_camera(ready_ctrl):
    ctrl = ready_ctrl(time_scale=FAST, contact_mode="search")
    ctrl.run_command("samples_find")
    for s in [s for s in ctrl.prep.samples if s["use"]][1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    ctrl.disconnect_camera()
    assert ctrl.state.get("devices", "camera", "state") == "끊김"
    assert ctrl.runner.gate() == [] and ctrl.state.get("run", "gate") == [], "측정 · 재개는 카메라를 쓰지 않는다"
    assert _why(ctrl.run_command, "samples_find") == "카메라 연결 안 됨", "촬영 · 샘플 검출은 카메라를 요구한다"
    assert ctrl.run_command("run_start")["n"] == 1
    assert (ctrl.runner.folder / "plate.jpg").is_file(), "판 사진은 찍어 둔 것을 쓴다"
    ctrl.run_command("run_stop")
    assert wait_for(lambda: not ctrl.runner.active, 30)
    assert ctrl.runner.gate() == []
    ctrl.disconnect_meter("k2110")                                   # 온도계 · 저항계 · 스테이지는 여전히 필요하다
    assert ctrl.runner.gate() == [{"text": "온도계 연결", "step": 1}]


# ---------------------------------------------------------------------------
# 8. 사용 안 할 포트
# ---------------------------------------------------------------------------
def test_blocked_port_is_listed_but_never_opened(make_ctrl, monkeypatch, tmp_path):
    ctrl = make_ctrl(connect=False)
    monkeypatch.setattr(control_mod, "serial_ports", lambda: [("COM4", "Arduino Uno (COM4)"), ("COM5", "Arduino Mega 2560 (COM5)")])
    assert Config(tmp_path / "none.json").load()["blocked_ports"] == ["COM4"]
    ports = ctrl.list_devices()["ports"]
    assert ports == [{"value": "COM4", "label": "COM4 · Arduino Uno (사용 안 함)", "blocked": True},
                     {"value": "COM5", "label": "COM5 · Arduino Mega 2560", "blocked": False}]
    for port in ("COM4", "com4", " COM4 "):
        assert _why(ctrl.connect_stage, port) == "%s 사용 안 함 · 다른 포트 선택 필요" % port.strip()
    assert ctrl.fake_fw is None and not ctrl.stage.connected, "사용 안 할 포트는 한 번도 열지 않는다"
    assert ctrl.state.get("devices", "stage", "state") == "오류" and "사용 안 함" in ctrl.state.get("devices", "stage", "why")
    # 자동 연결도 열지 않는다(예전 설정 파일에 그 포트가 저장돼 있던 경우). 다른 장비는 그대로 연결한다
    ctrl.cfg.update({"stage_port": "COM4"})
    ctrl.auto_connect()
    assert ctrl.fake_fw is None and not ctrl.stage.connected
    assert ctrl.state.get("devices", "k2450", "state") == "연결"
    assert [ln for ln in ctrl.logs.recent if "스테이지 연결 거절 · COM4 는 사용 안 할 포트" in ln]
    assert ctrl.connect_stage("COM5")["port"] == "COM5"
    with TestClient(create_app(ctrl)) as c:                          # 목록은 설정이다(전체 설정에서 고친다)
        rows = {r["key"]: r for r in c.get("/api/settings").json()}
        assert rows["blocked_ports"]["kind"] == "list" and rows["blocked_ports"]["value"] == ["COM4"] and rows["blocked_ports"]["desc"]
        assert c.put("/api/settings", json={"blocked_ports": "COM4"}).status_code == 400
        assert c.put("/api/settings", json={"blocked_ports": ["COM4", "COM9"]}).json() == {"ok": True}
    assert [p["blocked"] for p in ctrl.list_devices()["ports"]] == [True, False]
