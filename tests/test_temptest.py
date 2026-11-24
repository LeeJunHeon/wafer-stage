"""온도 시험(app/jobs/temptest.py · 수동 제어 탭 [온도 시험]): 샘플 · 저항계 · 모터 없이 히트건 · 쿨러의 온도 거동만 잰다.
예비 냉각 → 가열(full · ramp - 자동 측정과 같은 판정 함수) → 냉각 → 요약 · 파일(temp.csv · summary.txt · graph.png).
[정지] · 비상정지 · 온도 못 읽음에서도 히트건 끄는 순서 · 쿨러 끔 · 기록 저장. 자동 측정 · 접촉 탐색 · 히트건 수동 버튼과 서로 막는다.
시험용 히트건 인터락(송풍 먼저 → 열선)은 conftest 가 시험마다 끝에 확인한다."""

import pytest

from app import control as control_mod
from app.config import ConfigError
from app.jobs import temptest as tt
from app.safety import Rejected
from tests.conftest import alarm_lines, wait_for

FAST = 200.0


def _ctrl(make_ctrl, **cfg):
    """열전대를 판에 붙인 시험 장치: 샘플 · 핀 없이 열 모델의 시료 하나를 읽는다(히트건 · 쿨러가 바로 데우고 식힌다)."""
    ctrl = make_ctrl(time_scale=FAST, **cfg)
    ctrl.lab.world = None
    return ctrl


def _run(ctrl, timeout=60):
    ctrl.run_command("temptest_start")
    assert ctrl.temptest.active
    assert wait_for(lambda: not ctrl.temptest.active, timeout), "온도 시험이 끝나지 않았다"
    return ctrl.temptest


def _csv(t):
    text = (t.folder / "temp.csv").read_bytes()
    assert text.startswith(b"\xef\xbb\xbf"), "Excel 에서 한글이 깨지지 않게 UTF-8 BOM"
    lines = text.decode("utf-8-sig").splitlines()
    assert lines[0] == ",".join(tt.CSV_HEADER)
    return [r.split(",") for r in lines[1:]]


def _all_off(ctrl, timeout=10):
    return wait_for(lambda: not any(ctrl.fake_fw.relay), timeout)


# ---------------------------------------------------------------------------
def test_summary_marks_and_times():
    """도달 표: 가열 시작 온도보다 큰 간격의 배수 + 목표 · 시간은 직선 보간 · 넘침은 열선을 끈 뒤 최고 온도."""
    assert tt.marks(27.3, 50.0, 5.0) == [30.0, 35.0, 40.0, 45.0, 50.0]
    assert tt.marks(30.0, 42.0, 5.0) == [35.0, 40.0, 42.0]
    cd = dict(run_heater_off_c=50.0, run_cool_end_c=30.0, temp_test_mark_c=10.0, run_heat_mode="ramp")
    pts = [[10.0, 30.0, 25.0, 1, 1, tt.HEAT], [20.0, 40.0, 35.0, 1, 1, tt.HEAT], [30.0, 50.0, 45.0, 0, 1, tt.HEAT],
           [40.0, 53.0, None, 0, 1, tt.COOL], [50.0, 40.0, None, 0, 1, tt.COOL], [60.0, 29.0, None, 0, 1, tt.COOL]]
    ev = {"t_heat": 0.0, "T0": 20.0, "t_heat_end": 30.0}
    s = tt.summarize(pts, [[0.0, 1], [25.0, 0], [30.0, 0]], ev, cd)
    assert [(m["c"], m["s"]) for m in s["marks"]] == [(30.0, 10.0), (40.0, 20.0), (50.0, 30.0)]
    h = s["heat"]
    assert h["reach_s"] == 30.0 and h["rate_c_min"] == 60.0 and h["on_n"] == 1 and h["on_s"] == 25.0
    assert h["peak_c"] == 53.0 and h["over_c"] == 3.0 and h["rms_c"] == 5.0 and h["max_c"] == 5.0
    c = s["cool"]
    assert c["reached"] and c["s"] == 30.0 and c["from_c"] == 53.0 and c["to_c"] == 29.0


@pytest.mark.parametrize("mode", ["full", "ramp"])
def test_runs_to_the_end(make_ctrl, mode):
    """full · ramp 각각 끝까지: 도달 표가 오름차순 · 목표 도달 시간 · 파일 3개 · 끝나면 송풍 · 쿨러 모두 꺼짐."""
    ctrl = _ctrl(make_ctrl, run_heat_mode=mode, run_heat_rate_c_min=20.0)
    t = _run(ctrl)
    assert t.result == tt.DONE, (t.result, alarm_lines(ctrl))
    s = ctrl.state.get("temptest", "summary")
    ms = s["marks"]
    assert [m["c"] for m in ms] == sorted(m["c"] for m in ms) and ms[-1]["c"] == 85.0 and ms[-1]["top"]
    assert all(m["s"] is not None for m in ms) and [m["s"] for m in ms] == sorted(m["s"] for m in ms), "도달 표: 온도 · 시간 모두 오름차순"
    h = s["heat"]
    assert h["reach_s"] == ms[-1]["s"] and h["reach_s"] > 0 and h["rate_c_min"] > 0 and h["on_n"] >= 1 and h["over_c"] is not None
    assert s["cool"]["reached"] and s["cool"]["to_c"] <= 30.0
    for name in ("temp.csv", "summary.txt", "graph.png"):
        assert (t.folder / name).is_file(), name
    assert (t.folder / "graph.png").read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
    assert t.folder.parent.name == "temp_test" and t.folder.parent.parent == ctrl.cfg.data_path()
    text = (t.folder / "summary.txt").read_text(encoding="utf-8-sig")
    assert "결과 · 완료" in text and "85 °C" in text and "[도달 표]" in text
    rows = _csv(t)
    heat = [r for r in rows if r[2] == "가열"]
    assert heat and [r for r in rows if r[2] == "냉각"]
    if mode == "full":
        assert all(r[5] == "1" and r[6] == "1" and float(r[4]) == 85.0 for r in heat), "계속 가열: 열선 · 송풍 켜짐 · 목표 = 가열 끝 온도"
        assert h["rms_c"] is None
    else:
        assert sum(1 for a, b in zip(heat, heat[1:]) if a[5] != b[5]) > 2, "가열 속도 제어: 열선을 켜고 끈다"
        assert h["rms_c"] is not None and h["on_n"] > 1
    assert all(r[7] == "R2 R3 R4" for r in rows), "쿨러는 시험 내내 켜져 있다(temp_test_coolers 기본 = 쿨러 릴레이 전부)"
    assert _all_off(ctrl), "끝나면 송풍(뒤에) · 쿨러 모두 꺼짐"


def test_precool_stall_aborts_without_heating(make_ctrl):
    """시작 온도까지 못 식으면(정체) 가열하지 않고 "중단 · 시작 온도 미도달 · 현재 xx.x °C" · 쿨러 끔 · 기록 · 요약 저장."""
    ctrl = _ctrl(make_ctrl, run_heat_mode="full", run_start_c=5.0, run_heater_off_c=40.0, run_cool_end_c=30.0, run_stall_s=20.0)
    t = _run(ctrl)
    assert t.result.startswith("중단 · 시작 온도 미도달 · 현재 ") and t.result.endswith(" °C"), t.result
    s = ctrl.state.get("temptest", "summary")
    assert s["pre"]["stalled"] and s["pre"]["T"] > 5.0 and s["heat"] is None and s["T0"] is None, "가열하지 않았다"
    rows = _csv(t)
    assert rows and all(r[2] == "예비 냉각" and r[5] == "0" and r[6] == "0" for r in rows), "예비 냉각만 · 히트건 꺼짐"
    assert "결과 · 중단 · 시작 온도 미도달" in (t.folder / "summary.txt").read_text(encoding="utf-8-sig")
    assert _all_off(ctrl), "쿨러 끔"


def test_cooling_modes_and_spacing(make_ctrl):
    """냉각 속도 제어: 냉각 중에도 열선을 켜고 끄며 목표선을 따라 내려간다(송풍 켜짐). 자연 냉각: 냉각 중 열선 꺼짐.
    점 간격 요약 = 기록된 줄 수와 같다."""
    for mode in ("ramp", "free"):
        ctrl = _ctrl(make_ctrl, run_heat_rate_c_min=20.0, run_cool_rate_c_min=10.0, run_cool_mode=mode)
        t = _run(ctrl)
        assert t.result == tt.DONE, t.result
        rows = _csv(t)
        cool = [r for r in rows if r[2] == "냉각"]
        s = ctrl.state.get("temptest", "summary")
        if mode == "ramp":
            assert all(r[6] == "1" and r[4] != "" for r in cool), "송풍 켜짐 · 목표 기록"
            assert sum(1 for a, b in zip(cool, cool[1:]) if a[5] != b[5]) > 2, "냉각 중 열선을 켜고 끈다"
            assert s["cool"]["rms_c"] is not None
            assert abs(s["cool"]["rate_c_min"] - 10.0) < 3.0, "평균 냉각 속도 %.2f" % s["cool"]["rate_c_min"]
        else:
            assert all(r[5] == "0" and r[4] == "" for r in cool), "자연 냉각: 열선 꺼짐 · 목표 없음"
        assert s["spacing"]["cool"]["n"] == len(cool) and s["spacing"]["heat"]["n"] == len([r for r in rows if r[2] in ("가열", "유지")])
        assert "[점 간격]" in (t.folder / "summary.txt").read_text(encoding="utf-8-sig")
        assert _all_off(ctrl)
        ctrl.shutdown()


def test_stop_while_heating(make_ctrl):
    """가열 중 [정지]: 열선 바로 끔 → 송풍은 heater_fan_after_s(30 초) 뒤 끔 · 쿨러 끔 · 그때까지 기록과 요약 저장."""
    ctrl = _ctrl(make_ctrl, run_heat_rate_c_min=2.0)
    ctrl.run_command("temptest_start")
    assert wait_for(lambda: ctrl.temptest.phase == tt.HEAT and ctrl.fake_fw.relay[9] and ctrl.fake_fw.relay[0]
                    and any(p[5] == tt.HEAT for p in ctrl.temptest.pts), 20), "가열 점 기록"
    ctrl.run_command("temptest_stop")
    assert wait_for(lambda: not ctrl.temptest.active, 10)
    t = ctrl.temptest
    assert t.result == tt.STOPPED
    assert not ctrl.fake_fw.relay[9] and not any(ctrl.fake_fw.relay[n - 1] for n in (2, 3, 4)), "열선 · 쿨러는 바로 꺼짐"
    assert _all_off(ctrl), "송풍은 뒤에 꺼짐"
    log = ctrl.heatgun.log
    t_el = next(x[0] for x in reversed(log) if x[1] and not x[2])             # 송풍 켜진 채 열선 꺼짐
    t_fan = next(x[0] for x in reversed(log) if not x[1] and not x[2])        # 둘 다 꺼짐
    assert t_fan - t_el >= 30.0 - 1e-6, "송풍은 열선 끈 뒤 30 초 · %.1f초" % (t_fan - t_el)
    assert (t.folder / "summary.txt").read_text(encoding="utf-8-sig").count("결과 · 정지") == 1
    assert (t.folder / "graph.png").is_file() and _csv(t)
    assert ctrl.state.get("temptest", "summary")["heat"]["reach_s"] is None, "목표 미도달"


def test_estop_ends_the_test_and_turns_everything_off(make_ctrl):
    ctrl = _ctrl(make_ctrl, run_heat_rate_c_min=2.0)
    ctrl.run_command("temptest_start")
    assert wait_for(lambda: ctrl.temptest.phase == tt.HEAT and ctrl.fake_fw.relay[9], 20)
    ctrl.estop()
    assert not any(ctrl.fake_fw.relay), "비상정지: r all 0 - 송풍 · 열선 · 쿨러 전부 바로"
    assert wait_for(lambda: not ctrl.temptest.active, 10)
    assert ctrl.temptest.result == tt.ESTOPPED
    assert (ctrl.temptest.folder / "summary.txt").is_file()
    with pytest.raises(Rejected):
        ctrl.run_command("temptest_start")                         # 비상정지 중에는 시작 불가


def test_temperature_loss_aborts(make_ctrl):
    """가열 중 온도를 못 읽으면 중단(측정 주체 규칙): 열선 끔 · 이유 · 기록 저장."""
    ctrl = _ctrl(make_ctrl, run_heat_rate_c_min=2.0)
    ctrl.run_command("temptest_start")
    assert wait_for(lambda: ctrl.temptest.phase == tt.HEAT and len(ctrl.temptest.pts) > 5, 20)
    ctrl.lab.fault["tc_open"] = True
    assert wait_for(lambda: not ctrl.temptest.active, 10)
    r = ctrl.temptest.result
    assert r.startswith(tt.ABORTED + " · ") and "온도 측정 실패" in r, r
    assert not ctrl.fake_fw.relay[9]
    assert any(a.startswith("temptest: 온도 시험 중단") for a in alarm_lines(ctrl)), alarm_lines(ctrl)
    ctrl.lab.fault["tc_open"] = False
    assert _all_off(ctrl)


def test_blocks_other_jobs_while_running(make_ctrl):
    """온도 시험 중: 자동 측정 · 접촉 탐색 · 쿨러 [켜 보기] · 히트건 수동 버튼 · 개별 릴레이 버튼 · 설정 변경 거절. [전체 끄기] 는 시험을 정지하고 끈다."""
    ctrl = _ctrl(make_ctrl, run_heat_rate_c_min=2.0, contact_mode="search")
    ctrl.run_command("temptest_start")
    assert wait_for(lambda: ctrl.temptest.phase == tt.HEAT and ctrl.fake_fw.relay[0], 20)
    for name, args in (("run_start", {}), ("run_resume", {}), ("contact_find", {}), ("cooler_only", {"n": 2}), ("heater_on", {}),
                       ("heater_off", {}), ("heater_element", {"on": False}), ("relay_on", {"n": 2}), ("relay_off", {"n": 2})):
        with pytest.raises(Rejected, match="온도 시험 중 실행 불가"):
            ctrl.run_command(name, args)
    with pytest.raises(Rejected, match="온도 시험 중 실행 불가"):
        ctrl.runner.start()                                         # 명령 줄을 거치지 않아도(측정 시작이 직접 본다)
    with pytest.raises(Rejected, match="온도 시험 중 실행 불가"):
        ctrl.update_settings({"run_heat_rate_c_min": 5.0})
    with pytest.raises(Rejected, match="이미 온도 시험 중"):
        ctrl.run_command("temptest_start")
    assert ctrl.temptest.active and ctrl.fake_fw.relay[0], "거절한 명령은 시험에 손대지 않았다"
    ctrl.run_command("relays_off")                                  # [전체 끄기]: 시험을 정지하고(정리) 나머지를 끈다
    assert not ctrl.temptest.active and ctrl.temptest.result == tt.STOPPED
    assert not ctrl.fake_fw.relay[9] and not any(ctrl.fake_fw.relay[n - 1] for n in (2, 3, 4))
    assert _all_off(ctrl)


def test_other_jobs_block_the_start(make_ctrl, ready_ctrl):
    """히트건 수동 켜짐 · 쿨러 켜 보기 · 접촉 탐색 · 자동 측정이 도는 동안 온도 시험 시작 거절."""
    ctrl = _ctrl(make_ctrl, heater_fan_after_s=600.0)               # 송풍이 늦게 꺼지는 동안(실제 3 초)을 본다
    ctrl.run_command("heater_on")
    with pytest.raises(Rejected, match="히트건 끈 뒤 시작"):
        ctrl.run_command("temptest_start")
    ctrl.run_command("heater_off")
    with pytest.raises(Rejected, match="히트건 끈 뒤 시작"):         # 송풍이 아직 켜져 있다(열선 끈 뒤 30 초)
        ctrl.run_command("temptest_start")
    assert _all_off(ctrl, 15)
    ctrl.run_command("cooler_only", {"n": 3})
    with pytest.raises(Rejected, match="쿨러 끈 뒤 시작"):
        ctrl.run_command("temptest_start")
    ctrl.run_command("cooler_only", {"n": 0})
    with ctrl._use_k2450(control_mod.USE_CONTACT):                  # 접촉 탐색이 K2450 을 쓰는 동안
        with pytest.raises(Rejected, match="접촉 탐색 중 실행 불가"):
            ctrl.run_command("temptest_start")
    assert not ctrl.temptest.active and ctrl.temptest.folder is None, "거절된 시작은 폴더를 만들지 않는다"
    ctrl.disconnect_meter("k2110")
    with pytest.raises(Rejected, match="온도계 연결 필요"):
        ctrl.run_command("temptest_start")
    # 자동 측정 중
    rc = ready_ctrl(time_scale=FAST, contact_check="off")            # 안 닿는 샘플을 건너뛰어 측정이 먼저 끝나지 않게
    rc.run_command("samples_find")
    rc.run_command("run_start")
    try:
        with pytest.raises(Rejected):
            rc.run_command("temptest_start")                        # 명령 줄이 측정 중 거절
        with pytest.raises(Rejected, match="자동 측정 중 실행 불가"):
            rc.temptest.start()
    finally:
        rc.run_command("run_stop")
        assert wait_for(lambda: not rc.runner.active, 30)


def test_settings_are_checked(make_ctrl):
    """쿨러 릴레이 밖의 번호 · 온도 읽기 시간보다 짧은 간격 · 온도 못 읽음 허용 시간에 가까운 간격은 거절. 빈 쿨러 목록은 된다."""
    ctrl = make_ctrl(connect=False)
    with pytest.raises(Rejected, match="온도 시험 쿨러 R5 · 쿨러 릴레이 아님"):
        ctrl.update_settings({"temp_test_coolers": [2, 5]})
    with pytest.raises(Rejected, match="온도 읽기 시간 0.2초 이상 필요"):     # 온도 읽기 시간 기본 0.2 초(2026-11-23 visa.log 172 ms)
        ctrl.update_settings({"temp_test_interval_s": 0.1})
    with pytest.raises(Rejected, match="온도 시험 읽기 간격 3초 · 2.10초 미만 필요"):
        ctrl.update_settings({"temp_test_interval_s": 3.0})
    with pytest.raises(ConfigError):
        ctrl.update_settings({"temp_test_coolers": [2, 2]})
    ctrl.update_settings({"temp_test_coolers": []})
    assert ctrl.cfg["temp_test_coolers"] == []
    ctrl.update_settings({"temp_test_coolers": [3], "temp_test_interval_s": 1.0, "temp_test_mark_c": 2.0, "temp_test_precool": False})
    assert ctrl.cfg["temp_test_coolers"] == [3]


def test_no_precool_and_selected_coolers(make_ctrl):
    """예비 냉각 끔: 쿨러만 켜고 바로 가열 · 켤 쿨러는 temp_test_coolers 만."""
    ctrl = _ctrl(make_ctrl, run_heat_mode="full", temp_test_precool=False, temp_test_coolers=[3], run_heater_off_c=40.0)
    t = _run(ctrl)
    assert t.result == tt.DONE, t.result
    rows = _csv(t)
    assert rows[0][2] == "가열" and not any(r[2] == "예비 냉각" for r in rows)
    assert all(r[7] == "R3" for r in rows)
    assert ctrl.state.get("temptest", "summary")["pre"] is None
    assert _all_off(ctrl)
