"""측정(app/jobs/run.py): 순서 · 히트건 한 번 켜고 끔 · 쿨러 구역 · 파일 형식 · NAS · 건너뛰기 · 멈춤 · 이어서 · 비상정지 · 안전 한계.
가짜 장비 + 빠른 시간 배율(time_scale)로 끝까지 돌린다."""

import json
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from app.config import BY_KEY
from app.jobs import files
from app.jobs.run import DONE, FAIL, SKIPPED, STOPPED, WAIT
from app.safety import Rejected
from app.server import create_app
from tests.conftest import SEARCH, alarm_codes, alarm_lines, wait_for

FAST = 200.0
# 가열은 기본(가열 속도 제어)으로 하되 가열 속도를 높여 시험 시간을 줄인다(85 − 30 °C 를 20 °C/분 = 165 초).
# 기본 가열 속도(3 °C/분)로 끝까지 재는 것은 tests/test_heat_control.py 가 따로 본다.
RAMP = dict(run_heat_rate_c_min=20.0)


def _prepare(ready_ctrl, n=2, time_scale=FAST, **cfg):
    """이 파일의 측정 시험은 전기 탐색으로 접촉한다고 명시한다(샘플마다 표면 높이가 달라 고정 높이로는 일부만 닿는다).
    고정 높이(기본)는 tests/test_contact_mode.py 가 따로 본다."""
    ctrl = ready_ctrl(time_scale=time_scale, **dict(SEARCH, **dict(RAMP, **cfg)))
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[n:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    return ctrl, [s["id"] for s in use[:n]]


def _finish(ctrl, timeout=40):
    assert wait_for(lambda: not ctrl.runner.active, timeout), "측정이 끝나지 않았다"


def _relay_cmds(ctrl, start=0):
    return [c for c in ctrl.fake_fw.log[start:] if re.match(r"r \d+ [01]$", c)]


def _all_off(ctrl):
    """측정이 끝난 뒤: K2450 출력 · 열선 · 쿨러는 꺼져 있고, 송풍은 열선을 끈 뒤 heater_fan_after_s 에 꺼진다."""
    fw = ctrl.fake_fw
    assert ctrl.lab.out_on is False, "K2450 출력이 꺼져 있어야 한다"
    assert not fw.relay[ctrl.cfg["heater_element_relay"] - 1], "열선은 바로 꺼진다"
    assert not any(fw.relay[n - 1] for n in ctrl.cfg["cooler_relays"]), "쿨러가 모두 꺼져 있어야 한다"
    assert wait_for(lambda: not any(fw.relay), 10), "송풍도 heater_fan_after_s 뒤 꺼져야 한다: %s" % fw.relay


def _heater_on_seqs(cmds):
    """릴레이 명령 중 히트건 켜기 순서(열선 r 10 1 → 송풍 r 1 1)의 수. 송풍 켜기는 언제나 열선을 켠 바로 뒤여야 한다."""
    n = 0
    for i, c in enumerate(cmds):
        if c == "r 1 1":
            assert i > 0 and cmds[i - 1] == "r 10 1", "송풍은 열선 바로 뒤에만 켠다: %s" % cmds[max(0, i - 3):i + 1]
            n += 1
    return n


# ---------------------------------------------------------------------------
def test_run_two_samples_to_the_end(ready_ctrl, tmp_path):
    nas = tmp_path / "nas"
    ctrl, ids = _prepare(ready_ctrl, 2, nas_dir=str(nas))
    stages = []
    orig = ctrl.runner._set
    ctrl.runner._set = lambda stage, line, frac=0.0: (stages.append((ctrl.runner.cur, stage)), orig(stage, line, frac))[1]
    n0 = len(ctrl.fake_fw.log)
    r = ctrl.run_command("run_start")
    assert r["n"] == 2
    with pytest.raises(Rejected):
        ctrl.run_command("jog", {"axis": "x", "mm": 1})             # 측정 중에는 손으로 못 움직인다
    with pytest.raises(Rejected):
        ctrl.run_command("relay_on", {"n": 5})
    with pytest.raises(Rejected):
        ctrl.run_command("read_resistance")
    assert "ST " in ctrl.run_command("raw", {"target": "stage", "line": "st"})["reply"], "직접 명령의 읽기(st)는 된다"
    _finish(ctrl)
    run = ctrl.runner
    assert run.state == "done" and [run.items[i]["status"] for i in ids] == [DONE, DONE]
    assert ctrl.state.get("run", "summary") == "2개 중 2개 완료"
    # 순서: 이동 → 접촉 → (냉각) → 가열 → 냉각 → 저장
    for sid in ids:
        seq = [st for cur, st in stages if cur == sid]
        assert [s for s in seq if s != 2] == [0, 1, 3, 4, 5], seq
    # 히트건: 샘플마다 켜기 순서(열선 → 송풍) 한 번 · 송풍 끄기 한 번. 가열 속도 제어라 그 사이 열선은 여러 번 켜고 끈다
    cmds = _relay_cmds(ctrl, n0)
    _all_off(ctrl)
    cmds = _relay_cmds(ctrl, n0)
    assert _heater_on_seqs(cmds) == 2 and cmds.count("r 1 0") == 2
    assert cmds.count("r 10 1") > 2 and cmds.count("r 10 0") == cmds.count("r 10 1"), "가열 속도 제어: 송풍을 켜 둔 채 열선만 켜고 끈다"
    # 쿨러는 샘플 위치에 맞는 릴레이
    grid, cool = ctrl.cfg["cooler_grid"], ctrl.cfg["cooler_relays"]
    on = [c for c in cmds if c.endswith(" 1") and int(c.split()[1]) in cool]
    for k, sid in enumerate(ids):
        s = next(x for x in ctrl.prep.samples if x["id"] == sid)
        cell = ctrl.cfg["plate_size_mm"] / 3.0
        want = grid[int(s["plate"][1] // cell)][int(s["plate"][0] // cell)]
        assert want == ctrl.world.zone_relay(min(range(9), key=lambda i: abs(ctrl.world.samples[i]["px"] - s["plate"][0]) + abs(ctrl.world.samples[i]["py"] - s["plate"][1])))
        assert on[k] == "r %d 1" % want, "샘플 %d 의 쿨러 릴레이" % sid
    assert len(on) == 2, "다른 쿨러는 켜지 않는다: %s" % on
    assert not [c for c in cmds if c.endswith(" 1") and int(c.split()[1]) not in cool + [1, 10]], "미배정 릴레이는 켜지 않는다"
    assert ctrl.state.get("pos", "z") == 0.0, "끝나면 핀이 맨 위"
    # 파일 이름과 바이트 단위 형식
    folder = run.folder
    assert folder.parent.parent == ctrl.cfg.data_path() and re.match(r"\d{4}-\d{2}-\d{2}$", folder.parent.name) and re.match(r"\d{6}_1$", folder.name)
    for sid in ids:
        name = run.items[sid]["name"]
        cyc = (folder / ("%s_1.00V_0.dat" % name)).read_bytes()
        mer = (folder / ("%s_1.00V_merged.dat" % name)).read_bytes()
        assert cyc.startswith(b"Temp   Resistance   \r\n"), "머리글: 공백 3 · 탭 없음 · CRLF"
        body = cyc[len(b"Temp   Resistance   \r\n"):]
        assert body == mer, "merged 는 머리글 없이 같은 줄"
        assert body.endswith(b"\r\n") and b"\n" not in body.replace(b"\r\n", b""), "줄끝은 전부 CRLF"
        lines = body.decode("ascii").split("\r\n")[:-1]
        assert len(lines) == len(run.items[sid]["points"]) > 50
        # 가열 제어 기록(control.csv): 점마다 한 줄 · 가열 점에는 목표 온도 · 열선 · 송풍
        ctl = (folder / ("%s_1.00V_control.csv" % name)).read_text(encoding="utf-8-sig").splitlines()
        assert ctl[0] == "회차,단계,경과(s),온도(°C),저항(Ω),목표 온도(°C),열선,송풍" and len(ctl) - 1 == len(lines)
        heat = [r.split(",") for r in ctl[1:] if r.split(",")[1] == "가열"]
        assert len(heat) == run.items[sid]["cycles"][0]["heat_rows"] and all(r[5] and r[7] == "1" for r in heat), "가열 중에는 목표 온도 · 송풍 켜짐"
        assert {r[6] for r in heat} == {"0", "1"}, "열선은 켜졌다 꺼졌다 한다"
        assert [r[3] + "\t" + r[4] for r in (x.split(",") for x in ctl[1:])] == lines, "control.csv 의 온도 · 저항 = .dat 의 줄"
        for ln in lines:
            assert re.match(r"^\d\.\d{7}E[+-]\d+\t\d\.\d{7}E[+-]\d+$", ln), ln     # LabVIEW %.7E · 탭 · 지수에 0 을 채우지 않음
        t0, r0 = lines[0].split("\t")
        assert abs(float(t0) - run.items[sid]["points"][0][0]) < 1e-3 and abs(float(r0) / run.items[sid]["points"][0][1] - 1) < 1e-6
        assert files.fmt_e(23.691786) == "2.3691786E+1" and files.fmt_e(0.05) == "5.0000000E-2"
        # NAS: 기본 폴더 / 연도 / YYYYMMDD / 묶음 에 .dat 만
    nas_dirs = list(nas.glob("*/*/1"))
    assert len(nas_dirs) == 1 and re.match(r"\d{8}$", nas_dirs[0].parent.name)
    assert sorted(p.name for p in nas_dirs[0].iterdir()) == sorted(p.name for p in folder.glob("*.dat"))
    meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    assert meta["state"] == "done" and len(meta["samples"]) == 2 and meta["cond"]["run_heater_off_c"] == 85.0 and meta["nas"]["ok"] is True
    assert meta["cond"]["run_heat_mode"] == "ramp" and meta["cond"]["heater_element_relay"] == 10 and meta["cond"]["run_heat_rate_c_min"] == 20.0
    assert (folder / "plate.jpg").stat().st_size > 1000 and (folder / "summary.csv").read_text(encoding="utf-8-sig").count("\n") == 3
    # 점은 state 에 없고 points() 로 받는다
    assert "points" not in json.dumps(ctrl.state.snapshot()["run"])
    p = run.points(ids[0], 10)
    assert p["n"] == len(run.items[ids[0]]["points"]) and len(p["points"]) == p["n"] - 10 and p["points"][0][2] in (0, 1)
    it = run.items[ids[0]]
    assert it["cycles"][0]["heat_rows"] > 10 and it["contact"]["r4"] > 1e5
    peak = max(pt[0] for pt in it["points"])
    assert 85.0 <= peak < 95.0, "85 °C 에서 열선을 끄고, 끈 뒤 더 오른 구간도 기록한다 (최고 %.1f)" % peak
    assert it["points"][-1][0] < 30.0


def test_unwritable_nas_does_not_stop_run(ready_ctrl, tmp_path):
    blocker = tmp_path / "not_a_folder"
    blocker.write_text("x")
    ctrl, ids = _prepare(ready_ctrl, 1, nas_dir=str(blocker / "sub"))
    ctrl.run_command("run_start")
    _finish(ctrl)
    assert ctrl.runner.items[ids[0]]["status"] == DONE
    assert any(a["code"] == "nas" and a["level"] == "주의" for a in ctrl.state.get("alarms")), "못 쓰면 주의만"
    meta = json.loads((ctrl.runner.folder / "run.json").read_text(encoding="utf-8"))
    assert meta["nas"]["ok"] is False and len(meta["nas"]["pending"]) == 2


def test_start_needs_preparation(ready_ctrl):
    ctrl = ready_ctrl(time_scale=FAST)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("run_start")
    assert "측정할 샘플 없음" in str(e.value)
    ctrl.run_command("samples_find")
    ctrl._remember(contact_z_mm=None)
    assert {"text": "접촉 높이", "step": 6} in ctrl.runner.gate()
    with pytest.raises(Rejected) as e:
        ctrl.run_command("run_start")
    assert "접촉 높이" in str(e.value)
    ctrl._remember(contact_z_mm=13.4)
    ctrl.prep.pos_block = True
    with pytest.raises(Rejected):
        ctrl.run_command("run_start")                                # 헤드 위치 경보(10 mm 넘음)면 시작을 막는다
    ctrl.prep.pos_block = False
    ctrl.disconnect_meter("k2110")
    assert any(m["step"] == 1 for m in ctrl.runner.gate())
    assert ctrl.calib.d["empty"] is not None
    ctrl.calib.d["empty"] = None
    assert not any("빈 판" in m["text"] for m in ctrl.runner.gate()), "빈 판 사진은 없어도 된다"


def test_names_and_batch(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.run_command("samples_find")
    a, b = ctrl.prep.samples[0], ctrl.prep.samples[1]
    ctrl.run_command("sample_rename", {"id": a["id"], "name": "기준"})
    for bad in ("a/b", "", "x?", "끝.", " 앞빈칸"):
        with pytest.raises(Rejected):
            ctrl.run_command("sample_rename", {"id": b["id"], "name": bad})
    with pytest.raises(Rejected):
        ctrl.run_command("sample_rename", {"id": b["id"], "name": "기준"})   # 겹치는 이름
    ctrl.run_command("batch_set", {"name": "A7"})
    assert a["name"] == "기준" and b["name"] == "A7-%d" % b["id"] and ctrl.cfg["batch_name"] == "A7"
    with pytest.raises(Rejected):
        ctrl.run_command("batch_set", {"name": "A:7"})


# ---------------------------------------------------------------------------
def test_skip_pause_resume_stop_requeue(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 3, time_scale=60.0)
    run = ctrl.runner
    ctrl.run_command("run_start")
    assert wait_for(lambda: run.cur == ids[0] and run.stage == 3 and len(run.items[ids[0]]["points"]) > 5, 20)
    ctrl.run_command("run_skip")                                     # 첫 샘플 건너뛰기
    assert wait_for(lambda: run.cur == ids[1], 20)
    assert run.items[ids[0]]["status"] == SKIPPED
    assert (run.folder / (run.items[ids[0]]["name"] + "_1.00V_0.dat")).stat().st_size > 60, "건너뛴 샘플도 받은 데이터는 파일로 남는다"
    ctrl.run_command("run_pause_after", {"on": True})                # 이 샘플 끝나면 멈춤
    _finish(ctrl)
    assert run.state == "stopped" and run.items[ids[1]]["status"] == DONE and run.items[ids[2]]["status"] == WAIT
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == 0.0
    folder = run.folder
    ctrl.run_command("run_resume")                                   # 이어서 측정: 남은 "할 차례"부터 같은 폴더에
    assert wait_for(lambda: run.cur == ids[2] and run.stage in (3, 4), 20)
    ctrl.run_command("run_stop")
    _finish(ctrl)
    assert run.folder == folder and run.state == "stopped" and run.items[ids[2]]["status"] == STOPPED
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == 0.0, "중지하면 핀을 맨 위로"
    with pytest.raises(Rejected):
        ctrl.run_command("run_resume")                               # 남은 "할 차례"가 없다
    ctrl.run_command("run_requeue", {"id": ids[0]})                  # 끝난(건너뛴) 샘플을 다시 측정 목록에
    ctrl.run_command("run_resume")
    _finish(ctrl, 60)
    assert run.items[ids[0]]["status"] == DONE and run.state == "done", "남은 '대기'가 없으면 끝(멈춘 샘플은 요약에 남는다)"
    name = run.items[ids[0]]["name"]
    assert sorted(p.name for p in folder.glob(name + "_*.dat")) == [name + "_1.00V_0.dat", name + "_1.00V_merged.dat"], "다시 잰 파일도 원래 이름으로"
    old = [p for p in folder.iterdir() if p.is_dir() and p.name.startswith("이전_")]
    assert len(old) == 1 and sorted(p.name for p in old[0].iterdir()) == [name + "_1.00V_0.dat", name + "_1.00V_control.csv", name + "_1.00V_merged.dat"], \
        "건너뛰기 전에 받은 데이터(가열 제어 기록 포함)는 이전_ 폴더에 남는다"
    assert ctrl.state.get("run", "summary") == "3개 중 2개 완료 · 중지 1"


def test_estop_during_run(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2, time_scale=60.0)
    run = ctrl.runner
    ctrl.run_command("run_start")
    assert wait_for(lambda: run.cur == ids[0] and run.stage == 3 and ctrl.fake_fw.relay[0] and len(run.items[ids[0]]["points"]) > 5, 20)
    z = ctrl.state.get("pos", "z")
    ctrl.estop("시험")
    _finish(ctrl, 20)
    assert run.state == "stopped" and run.items[ids[0]]["status"] == STOPPED and run.items[ids[0]]["reason"] == "비상정지"
    assert run.items[ids[1]]["status"] == WAIT
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == z > 0, "비상정지면 핀을 올리지 않는다"
    assert alarm_codes(ctrl) == ["estop"], "비상정지 뒤 경보는 비상정지 하나뿐: %s" % alarm_lines(ctrl)
    assert (run.folder / (run.items[ids[0]]["name"] + "_1.00V_0.dat")).stat().st_size > 60, "받은 데이터는 파일로 남는다"
    with pytest.raises(Rejected):
        ctrl.run_command("run_resume")                               # 비상정지를 풀기 전에는 이어서 못 한다
    assert wait_for(lambda: not ctrl.state.get("estop_hold"), 5), "정지 뒤 2초(측정 시계)가 지나면 해제할 수 있다"
    ctrl.run_command("estop_release")
    # 해제는 위치와 릴레이 상태를 다시 읽기만 한다: 측정은 중지된 채(스스로 이어지지 않는다) · 히트건 · 쿨러 · K2450 출력은 꺼진 채
    time.sleep(0.3)
    assert not run.active and run.state == "stopped" and run.items[ids[0]]["status"] == STOPPED and run.items[ids[1]]["status"] == WAIT
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == z, "핀도 그 자리"
    ctrl.run_command("run_resume")
    _finish(ctrl, 40)
    assert run.items[ids[1]]["status"] == DONE


def _burn(stop):
    """CPU 를 쓰는 파이썬 스레드(GIL 을 잡고 계산만 한다)."""
    x = 0
    while not stop.is_set():
        for i in range(2000):
            x += i * i


def test_estop_under_cpu_load_leaves_only_the_estop_alarm(ready_ctrl):
    """같은 프로세스에 CPU 스레드 2개를 띄운 채 배율 60 으로 가열하다 비상정지 → 비상정지가 새로 만든 경보는 비상정지 하나뿐.
    비상정지 직전 경보 목록과 직후를 비교한다: 부하가 심하면 비상정지 전에 안전 감시가 (맞게) 온도 경보를 낼 수 있고, 그것은 비상정지가 만든 것이 아니다.
    예전: temp_stale_s 3 초가 배율 60 에서 실제 0.05 초라, 부하로 한 회차가 실제 0.2~0.8 초 걸리면 안전 감시가 (맞게) "온도 측정 실패 · 히트건 차단"
    (+ run_slow) 을 비상정지보다 먼저 냈다. 지금은 시험 장치가 그런 창을 실제 시간 1 초(상한 안에서)로 정한다(conftest.real_time_windows).
    부하는 가열을 시작한 뒤에 띄운다 - 실제 시간 창이 문제 되는 것은 히트건이 켜진 동안이다."""
    # 가열 구간을 길게(부하로 느려진 회차가 측정 중에 두 번 이상 오게). 부하로 온도를 실제 1 초 넘게 못 읽으면 안전 감시가 (맞게) 열선을 끄고
    # 냉각으로 넘어간다 - 그래도 비상정지 때까지 측정이 이어지게 냉각 끝 온도를 낮추고 냉각 정체 판정을 길게 한다(한 단계 한도까지 냉각)
    # 통신 감시(watchdog_ms)는 시험용 보드에서 배율 없는 실제 시간이다. CPU 스레드가 GIL 을 잡아 시리얼 일꾼이 2 초 넘게 못 돌면 보드가 (맞게)
    # "통신 끊김 · 릴레이 차단" 을 낸다 - 시험 장치의 실제 시간 창이므로 이 시험에서는 넓힌다(conftest 의 시간 창 원칙과 같다)
    ctrl = ready_ctrl(time_scale=60.0, run_heater_off_c=100.0, run_cool_end_c=5.0, run_stall_s=3000.0, watchdog_ms=20000)
    assert ctrl.cfg["temp_stale_s"] == 60.0 and ctrl.cfg["heater_max_on_s"] == 240.0
    ctrl.run_command("samples_find")
    x1, y1 = ctrl.world.sample_stage(1)                              # 표면 13.35 mm - 고정 높이(13.4 mm)로 닿는 샘플 하나
    for s in ctrl.prep.samples:
        near = abs(s["xy"][0] - x1) + abs(s["xy"][1] - y1) < 3.0
        if s["use"] != near:
            ctrl.run_command("sample_use", {"id": s["id"], "use": near})
    (sid,) = [s["id"] for s in ctrl.prep.samples if s["use"]]
    run = ctrl.runner
    stop = threading.Event()
    load = [threading.Thread(target=_burn, args=(stop,), daemon=True) for _ in range(2)]
    try:
        ctrl.run_command("run_start")
        assert wait_for(lambda: run.stage == 3 and ctrl.fake_fw.relay[0] and run.items[sid]["points"], 20), "가열 시작"
        n0 = len(run.items[sid]["points"])
        for t in load:
            t.start()
        assert wait_for(lambda: run.active and len(run.items[sid]["points"]) >= n0 + 2, 30), \
            "부하 아래 두 회차 · 단계 %s · %s · %s" % (run.stage, alarm_lines(ctrl), [(i["status"], i["reason"]) for i in run.items.values()])      # 부하로 안전 감시가 열선을 껐으면 냉각 단계일 수 있다(비상정지는 같다)
        before = set(alarm_codes(ctrl))                              # 부하로 실제 1 초 넘게 온도를 못 읽으면 안전 감시가 (맞게) 경보를 이미 냈을 수 있다
        ctrl.estop("시험")
        assert wait_for(lambda: not run.active, 30)
    finally:
        stop.set()
        for t in load:
            if t.ident is not None:                                  # 띄운 스레드만 기다린다
                t.join()
    assert run.items[sid]["status"] == STOPPED and run.items[sid]["reason"] == "비상정지"
    new = set(alarm_codes(ctrl)) - before
    assert new == {"estop"}, "비상정지가 새로 만든 경보는 비상정지 하나뿐 · 직전 %s · 지금 %s" % (sorted(before), alarm_lines(ctrl))
    _all_off(ctrl)


def test_contact_failure_goes_to_next_sample(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2)
    w = ctrl.world
    s = next(x for x in ctrl.prep.samples if x["id"] == ids[0])
    i = min(range(9), key=lambda k: abs(w.samples[k]["px"] - s["plate"][0]) + abs(w.samples[k]["py"] - s["plate"][1]))
    w.samples[i]["thick"] = 0.05                                     # 표면이 저장 높이 + 0.5 mm 보다 아래 → 한도까지 내려도 안 닿는다
    ctrl.run_command("run_start")
    _finish(ctrl)
    run = ctrl.runner
    assert run.items[ids[0]]["status"] == FAIL and run.items[ids[0]]["reason"] == "미접촉"
    assert run.items[ids[1]]["status"] == DONE, "실패한 샘플은 건너뛰고 다음으로"
    assert run.state == "done" and ctrl.state.get("run", "summary") == "2개 중 1개 완료 · 실패 1"
    assert max(int(c.split()[1]) for c in ctrl.fake_fw.log if c.startswith("gz ")) / 160.0 <= ctrl.cfg["contact_z_mm"] + 0.5 + 1e-6
    _all_off(ctrl)


# ---------------------------------------------------------------------------
# 안전 한계
# ---------------------------------------------------------------------------
def test_max_on_time_moves_on_to_cooling(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 1, run_heat_mode="full", heater_max_on_s=20.0, run_no_rise_s=10.0)   # 열선을 계속 켜는 방식 · 20 초로는 85 °C 에 못 닿는다
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = ctrl.runner.items[ids[0]]
    assert it["status"] == DONE and "가열 끝 온도 미도달" in it["notes"]
    assert max(p[0] for p in it["points"]) < 85.0 and it["points"][-1][2] == 1, "냉각으로 넘어가 끝까지 기록한다"
    _all_off(ctrl)
    cmds = _relay_cmds(ctrl, n0)
    assert _heater_on_seqs(cmds) == 1 and cmds.count("r 10 1") == 1 and cmds.count("r 10 0") == 1 and cmds.count("r 1 0") == 1, cmds
    assert any("히트건 끔 · 최대 켜짐 시간" in ln for ln in ctrl.logs.recent)


def test_no_temperature_rise_fails_sample(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2)
    ctrl.lab.t_heat = ctrl.lab.t_amb                                 # 히트건을 켜도 안 오른다(열전대가 떨어짐)
    ctrl.run_command("run_start")
    assert wait_for(lambda: ctrl.runner.items[ids[0]]["status"] == FAIL, 30)
    ctrl.lab.t_heat = 125.0
    _finish(ctrl)
    run = ctrl.runner
    assert "온도 상승 없음" in run.items[ids[0]]["reason"]
    assert run.items[ids[1]]["status"] == DONE
    _all_off(ctrl)


def test_phase_time_limit_fails_sample(ready_ctrl):
    # 식히는 데 30 초로는 모자란다. 가열은 계속 가열(30 초 안에 85 °C 에 닿는다 - 가열 속도 제어는 예상 가열 시간이 한도를 넘어 시작이 거절된다)
    ctrl, ids = _prepare(ready_ctrl, 1, run_heat_mode="full", run_phase_limit_s=30.0, run_stall_s=3000.0)
    ctrl.run_command("run_start")
    _finish(ctrl)
    run = ctrl.runner
    it = run.items[ids[0]]
    assert it["status"] == FAIL and "초과" in it["reason"]
    # 그때까지 받은 데이터는 남는다: 받은 점 수(컴퓨터 속도에 따라 다르다)가 있고, 파일의 줄과 하나하나 같다
    n = len(it["points"])
    assert n > 0 and it["cycles"][0]["rows"] == n
    cyc = (run.folder / (it["name"] + "_1.00V_0.dat")).read_bytes().decode("ascii").split("\r\n")
    mer = (run.folder / (it["name"] + "_1.00V_merged.dat")).read_bytes().decode("ascii").split("\r\n")
    assert cyc[0] == files.HEADER and cyc[-1] == "" and mer[-1] == "" and cyc[1:] == mer
    assert [files.data_line(t, r) for t, r, _ph in it["points"]] == cyc[1:-1], "화면 점 = 파일 줄"
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == 0.0


def test_cooling_stall_ends_phase_normally(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 1, run_cool_end_c=5.0, run_stall_s=30.0)          # 쿨러로는 5 °C 까지 못 내려간다
    ctrl.run_command("run_start")
    _finish(ctrl, 60)
    it = ctrl.runner.items[ids[0]]
    assert it["status"] == DONE and any("에서 종료" in n for n in it["notes"])
    assert 9.0 < it["points"][-1][0] < 14.0


def test_instrument_error_stops_whole_run(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2, time_scale=60.0)
    run = ctrl.runner
    ctrl.run_command("run_start")
    assert wait_for(lambda: run.cur == ids[0] and run.stage == 3 and len(run.items[ids[0]]["points"]) > 3, 20)
    ctrl.lab.fault["disconnect"] = True                              # 측정 장비 연결 끊김
    _finish(ctrl, 20)
    ctrl.lab.fault["disconnect"] = False
    assert run.state == "stopped" and run.items[ids[0]]["status"] == FAIL and run.items[ids[1]]["status"] == WAIT
    a = next(a for a in ctrl.state.get("alarms") if a["code"] == "run")
    assert a["level"] == "경보"
    assert not ctrl.fake_fw.relay[9], "열선은 꺼져 있다"
    assert wait_for(lambda: not ctrl.fake_fw.relay[0], 10), "송풍도 뒤에 꺼진다"
    assert ctrl.state.get("pos", "z") == 0.0


def test_run_stays_active_until_files_are_saved_and_state_is_published(ready_ctrl):
    """마지막 파일 저장(· NAS 복사)이 끝나고 끝난 상태를 화면에 보낸 뒤에야 "측정 중이 아님"이 된다.
    그 전에는 새 측정 · 손 조작을 받지 않는다(같은 기록에 섞여 쓰지 않게)."""
    import threading
    ctrl, ids = _prepare(ready_ctrl, 1)
    run = ctrl.runner
    gate, reached = threading.Event(), threading.Event()
    orig = run._save_summary
    run._save_summary = lambda: (reached.set(), gate.wait(20), orig())[2]
    ctrl.run_command("run_start")
    assert reached.wait(40), "마지막 저장까지 가지 못했다"
    try:
        assert run.items[ids[0]]["status"] == DONE and run.active, "저장이 끝나기 전에는 아직 측정 중"
        assert ctrl.state.get("run", "state") == "running" and ctrl.state.get("run", "active") is True
        with pytest.raises(Rejected):
            ctrl.run_command("run_start")
        with pytest.raises(Rejected):
            ctrl.run_command("jog", {"axis": "x", "mm": 1})
    finally:
        gate.set()
    _finish(ctrl)
    assert ctrl.state.get("run", "state") == "done" and ctrl.state.get("run", "active") is False
    assert ctrl.state.get("run", "summary") == "1개 중 1개 완료"
    assert json.loads((run.folder / "run.json").read_text(encoding="utf-8"))["state"] == "done"
    assert (run.folder / "summary.csv").is_file()


def test_remeasure_moves_old_files_and_keeps_labview_names(ready_ctrl, tmp_path):
    """같은 기록 안에서 다시 재면: 예전 .dat · merged 는 "이전_<HHMMSS>" 폴더로 가고, 새 파일은 원래 이름(_1 을 붙이지 않는다)으로 쓴다.
    run.json 에 옮긴 것을 적고, NAS 에는 같은 이름으로 덮어쓴다(예전 것은 PC 의 이전_ 폴더에만)."""
    nas = tmp_path / "nas"
    ctrl, ids = _prepare(ready_ctrl, 1, nas_dir=str(nas))
    run = ctrl.runner
    ctrl.run_command("run_start")
    _finish(ctrl)
    folder, name = run.folder, run.items[ids[0]]["name"]
    want = [name + "_1.00V_0.dat", name + "_1.00V_merged.dat"]
    ctl = name + "_1.00V_control.csv"                                # 가열 제어 기록(PC 에만 - NAS 에는 .dat 만)
    first = {n: (folder / n).read_bytes() for n in want + [ctl]}
    nas_dir = Path(json.loads((folder / "run.json").read_text(encoding="utf-8"))["nas"]["dir"])
    assert sorted(p.name for p in nas_dir.iterdir()) == want and all((nas_dir / n).read_bytes() == first[n] for n in want)

    for s in ctrl.world.samples:                                     # 두 번째 측정은 곡선이 달라야 예전 것과 구분된다
        s["t_up"] += 3.0
    ctrl.run_command("run_requeue", {"id": ids[0]})
    ctrl.run_command("run_resume")
    _finish(ctrl)
    assert run.folder == folder and run.items[ids[0]]["status"] == DONE

    # PC: 원래 이름 그대로 새 데이터, 예전 것은 이전_<HHMMSS> 폴더에 그대로
    assert sorted(p.name for p in folder.iterdir() if p.suffix == ".dat") == want, "이름 뒤에 _1 이 붙은 파일이 없어야 한다"
    assert sorted(p.name for p in folder.iterdir() if p.suffix == ".csv") == sorted([ctl, "summary.csv"])
    second = {n: (folder / n).read_bytes() for n in want + [ctl]}
    assert all(second[n] != first[n] and second[n] for n in want + [ctl]), "새 데이터로 다시 썼다"
    old_dirs = [p for p in folder.iterdir() if p.is_dir()]
    assert len(old_dirs) == 1 and re.match(r"이전_\d{6}$", old_dirs[0].name)
    assert {p.name: p.read_bytes() for p in old_dirs[0].iterdir()} == first, "예전 파일은 바이트 그대로 옮겨졌다"
    assert second[want[0]].startswith(b"Temp   Resistance   \r\n") and second[want[0]][len(b"Temp   Resistance   \r\n"):] == second[want[1]]

    # run.json: 옮긴 것을 적는다 · 샘플의 파일 목록은 지금 있는 것만
    meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    smp = meta["samples"][0]
    assert sorted(smp["files"]) == sorted(want + [ctl]) and smp["cycles"][0]["file"] == want[0]
    assert len(smp["moved"]) == 1 and smp["moved"][0]["to"] == old_dirs[0].name and sorted(smp["moved"][0]["files"]) == sorted(want + [ctl])

    # NAS: 같은 이름으로 덮어썼다(이전_ 폴더는 PC 에만)
    assert sorted(p.name for p in nas_dir.iterdir()) == want
    assert all((nas_dir / n).read_bytes() == second[n] for n in want)
    assert meta["nas"]["ok"] is True and meta["nas"]["names"] == {n: n for n in want}
    assert not ctrl.state.has_alarm("nas")

    # 측정 결과 탭: 새 데이터로 계산하고, 파일 목록에 이전_ 폴더가 보인다
    from app import results
    rec = results.load_record(ctrl.cfg.data_path(), "%s/%s" % (folder.parent.name, folder.name))
    assert rec["samples"][0]["n"] == len(run.items[ids[0]]["points"]) and any(f.startswith(old_dirs[0].name) for f in rec["files"])


def test_nas_does_not_overwrite_another_records_file(ready_ctrl, tmp_path):
    """NAS 폴더(연도/날짜/묶음)에 다른 기록의 같은 이름 파일이 이미 있으면 덮어쓰지 않는다(_1 을 붙이고 주의). 이 기록이 쓴 파일만 덮어쓴다."""
    nas = tmp_path / "nas"
    ctrl, ids = _prepare(ready_ctrl, 1, nas_dir=str(nas))
    run = ctrl.runner
    name = next(s["name"] for s in ctrl.prep.samples if s["id"] == ids[0])
    import time as _t
    other = Path(files.nas_target(str(nas), True, ctrl.cfg["batch_name"], _t.time()))
    other.mkdir(parents=True)
    (other / (name + "_1.00V_0.dat")).write_bytes(b"other record")
    ctrl.run_command("run_start")
    _finish(ctrl)
    assert (other / (name + "_1.00V_0.dat")).read_bytes() == b"other record", "다른 기록의 파일은 그대로"
    assert (other / (name + "_1.00V_0_1.dat")).read_bytes() == (run.folder / (name + "_1.00V_0.dat")).read_bytes()
    meta = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert meta["nas"]["names"][name + "_1.00V_0.dat"] == name + "_1.00V_0_1.dat"
    assert ctrl.state.has_alarm("nas")
    # 측정 결과 탭 [다시 복사]도 이 기록이 쓴 이름으로만 덮어쓴다
    ctrl.run_command("result_nas_copy", {"id": "%s/%s" % (run.folder.parent.name, run.folder.name)})
    assert (other / (name + "_1.00V_0.dat")).read_bytes() == b"other record"
    assert sorted(p.name for p in other.iterdir()) == sorted([name + "_1.00V_0.dat", name + "_1.00V_0_1.dat", name + "_1.00V_merged.dat"])


def test_fast_rise_is_only_a_notice_and_changed_limits_apply_to_the_next_run(ready_ctrl):
    """1 초에 run_fast_rise_c_s 넘게 오르면 주의 알림만 한 번 띄우고 히트건은 끄지 않는다.
    측정 한계는 "전체 설정"에서 바꾸면 다음 측정부터 그 값으로 판정한다. 로그 세 가지는 설정과 상관없이 항상 저장된다."""
    ctrl, ids = _prepare(ready_ctrl, 1)
    run = ctrl.runner
    with TestClient(create_app(ctrl)) as c:
        assert c.put("/api/settings", json={"run_fast_rise_c_s": 0.5}).json() == {"ok": True}      # 가짜 판은 1 초에 1 °C 넘게 오른다
        n0 = len(ctrl.fake_fw.log)
        assert c.post("/api/cmd/run_start", json={}).json()["ok"]
        _finish(ctrl)
        it = run.items[ids[0]]
        assert run.cond["run_fast_rise_c_s"] == 0.5
        al = [a for a in ctrl.state.get("alarms") if a["code"] == "fast_rise"]
        assert len(al) == 1 and al[0]["level"] == "주의" and "온도 급상승" in al[0]["text"]
        cmds = _relay_cmds(ctrl, n0)
        assert _heater_on_seqs(cmds) == 1, "빠르게 올라도 안전 규칙이 열선을 끄지 않는다(켜기 순서 한 번 · 그 뒤는 가열 속도 제어)"
        assert it["status"] == DONE and max(p[0] for p in it["points"]) >= 85.0 and "가열 끝 온도 미도달" not in it["notes"]
        assert not ctrl.state.has_alarm("heater")

        # 다음 측정: 가열 무반응 시간 · 기준을 바꾸면 그 값으로 판정한다(1 초 안에 20 °C 는 못 오른다)
        assert c.put("/api/settings", json={"run_no_rise_s": 1, "run_no_rise_c": 20}).json() == {"ok": True}
        assert c.post("/api/cmd/run_start", json={}).json()["ok"]
        _finish(ctrl)
        assert run.cond["run_no_rise_s"] == 1 and run.cond["run_no_rise_c"] == 20
        assert run.items[ids[0]]["status"] == FAIL and "온도 상승 없음" in run.items[ids[0]]["reason"]
        _all_off(ctrl)

    # 로그: 프로그램 · 시리얼 · VISA 가 모두 파일로 남는다(끄는 설정은 없다). 히트건을 켜고 끈 것과 주체가 프로그램 로그에 있다
    assert not any("log" in k for k in BY_KEY)
    day = next(p for p in (ctrl.cfg.data_path() / "logs").iterdir() if p.is_dir())
    for name in ("app", "serial", "visa"):
        assert (day / (name + ".log")).stat().st_size > 0, name
    text = (day / "app.log").read_text(encoding="utf-8")
    assert text.count("히트건 켬 · 측정") == 2 and text.count("히트건 끔 · 측정") == 2
