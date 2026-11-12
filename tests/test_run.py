"""측정(app/jobs/run.py): 순서 · 히트건 한 번 켜고 끔 · 쿨러 구역 · 파일 형식 · NAS · 건너뛰기 · 멈춤 · 이어서 · 비상정지 · 안전 한계.
가짜 장비 + 빠른 시간 배율(time_scale)로 끝까지 돌린다."""

import json
import re
from pathlib import Path

import pytest

from app.jobs import files
from app.jobs.run import DONE, FAIL, SKIPPED, STOPPED, WAIT
from app.safety import Rejected
from tests.conftest import wait_for

FAST = 200.0


def _prepare(ready_ctrl, n=2, time_scale=FAST, **cfg):
    ctrl = ready_ctrl(time_scale=time_scale, **cfg)
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
    assert ctrl.lab.out_on is False, "K2450 출력이 꺼져 있어야 한다"
    assert not any(ctrl.fake_fw.relay), "히트건 · 쿨러가 모두 꺼져 있어야 한다"


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
    assert ctrl.state.get("run", "summary") == "2개 중 2개 끝남"
    # 순서: 이동 → 접촉 → (냉각) → 가열 → 냉각 → 저장
    for sid in ids:
        seq = [st for cur, st in stages if cur == sid]
        assert [s for s in seq if s != 2] == [0, 1, 3, 4, 5], seq
    # 히트건은 샘플마다 정확히 한 번 켜지고 한 번 꺼진다 · 쿨러는 샘플 자리에 맞는 릴레이
    cmds = _relay_cmds(ctrl, n0)
    assert cmds.count("r 1 1") == 2 and cmds.count("r 1 0") == 2
    grid = ctrl.cfg["cooler_grid"]
    for k, sid in enumerate(ids):
        s = next(x for x in ctrl.prep.samples if x["id"] == sid)
        cell = ctrl.cfg["plate_size_mm"] / 3.0
        want = grid[int(s["plate"][1] // cell)][int(s["plate"][0] // cell)]
        assert want == ctrl.world.zone_relay(min(range(9), key=lambda i: abs(ctrl.world.samples[i]["px"] - s["plate"][0]) + abs(ctrl.world.samples[i]["py"] - s["plate"][1])))
        on = [c for c in cmds if c.endswith(" 1") and c != "r 1 1"]
        assert on[k] == "r %d 1" % want, "샘플 %d 의 쿨러 릴레이" % sid
    assert set(c for c in cmds if c.endswith(" 1")) - {"r 1 1"} == set(on), "다른 쿨러는 켜지 않는다"
    _all_off(ctrl)
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
    assert (folder / "plate.jpg").stat().st_size > 1000 and (folder / "summary.csv").read_text(encoding="utf-8-sig").count("\n") == 3
    # 점은 state 에 없고 points() 로 받는다
    assert "points" not in json.dumps(ctrl.state.snapshot()["run"])
    p = run.points(ids[0], 10)
    assert p["n"] == len(run.items[ids[0]]["points"]) and len(p["points"]) == p["n"] - 10 and p["points"][0][2] in (0, 1)
    it = run.items[ids[0]]
    assert it["cycles"][0]["heat_rows"] > 10 and it["contact"]["r4"] > 1e5
    peak = max(pt[0] for pt in it["points"])
    assert 85.0 <= peak < 95.0, "85 °C 에서 끄고, 끈 뒤 더 오른 구간도 기록한다 (최고 %.1f)" % peak
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
    assert "측정할 샘플이 없습니다" in str(e.value)
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
    assert len(old) == 1 and sorted(p.name for p in old[0].iterdir()) == [name + "_1.00V_0.dat", name + "_1.00V_merged.dat"], "건너뛰기 전에 받은 데이터는 이전_ 폴더에 남는다"
    assert ctrl.state.get("run", "summary") == "3개 중 2개 끝남 · 멈춤 1"


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
    assert [a["code"] for a in ctrl.state.snapshot()["alarms"]] == ["estop"], "비상정지 뒤 경보는 비상정지 하나뿐"
    assert (run.folder / (run.items[ids[0]]["name"] + "_1.00V_0.dat")).stat().st_size > 60, "받은 데이터는 파일로 남는다"
    with pytest.raises(Rejected):
        ctrl.run_command("run_resume")                               # 비상정지를 풀기 전에는 이어서 못 한다
    ctrl.run_command("estop_release")
    ctrl.run_command("run_resume")
    _finish(ctrl, 40)
    assert run.items[ids[1]]["status"] == DONE


def test_contact_failure_goes_to_next_sample(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2)
    w = ctrl.world
    s = next(x for x in ctrl.prep.samples if x["id"] == ids[0])
    i = min(range(9), key=lambda k: abs(w.samples[k]["px"] - s["plate"][0]) + abs(w.samples[k]["py"] - s["plate"][1]))
    w.samples[i]["thick"] = 0.05                                     # 표면이 저장 높이 + 0.5 mm 보다 아래 → 한도까지 내려도 안 닿는다
    ctrl.run_command("run_start")
    _finish(ctrl)
    run = ctrl.runner
    assert run.items[ids[0]]["status"] == FAIL and run.items[ids[0]]["reason"] == "닿지 않았습니다"
    assert run.items[ids[1]]["status"] == DONE, "실패한 샘플은 건너뛰고 다음으로"
    assert run.state == "done" and ctrl.state.get("run", "summary") == "2개 중 1개 끝남 · 실패 1"
    assert max(int(c.split()[1]) for c in ctrl.fake_fw.log if c.startswith("gz ")) / 160.0 <= ctrl.cfg["contact_z_mm"] + 0.5 + 1e-6
    _all_off(ctrl)


# ---------------------------------------------------------------------------
# 안전 한계
# ---------------------------------------------------------------------------
def test_max_on_time_moves_on_to_cooling(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 1, heater_max_on_s=20.0)        # 20 초로는 85 °C 에 못 닿는다
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = ctrl.runner.items[ids[0]]
    assert it["status"] == DONE and "최고 온도 못 미침" in it["notes"]
    assert max(p[0] for p in it["points"]) < 85.0 and it["points"][-1][2] == 1, "냉각으로 넘어가 끝까지 기록한다"
    cmds = _relay_cmds(ctrl, n0)
    assert cmds.count("r 1 1") == 1 and cmds.count("r 1 0") == 1
    _all_off(ctrl)


def test_no_temperature_rise_fails_sample(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 2)
    ctrl.lab.t_heat = ctrl.lab.t_amb                                 # 히트건을 켜도 안 오른다(열전대가 떨어짐)
    ctrl.run_command("run_start")
    assert wait_for(lambda: ctrl.runner.items[ids[0]]["status"] == FAIL, 30)
    ctrl.lab.t_heat = 125.0
    _finish(ctrl)
    run = ctrl.runner
    assert "온도가 오르지 않습니다" in run.items[ids[0]]["reason"]
    assert run.items[ids[1]]["status"] == DONE
    _all_off(ctrl)


def test_phase_time_limit_fails_sample(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 1, run_phase_limit_s=30.0, run_stall_s=3000.0)   # 식히는 데 30 초로는 모자란다
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = ctrl.runner.items[ids[0]]
    assert it["status"] == FAIL and "넘었습니다" in it["reason"]
    assert len(it["points"]) > 20, "그때까지 받은 데이터는 남는다"
    _all_off(ctrl)
    assert ctrl.state.get("pos", "z") == 0.0


def test_cooling_stall_ends_phase_normally(ready_ctrl):
    ctrl, ids = _prepare(ready_ctrl, 1, run_cool_end_c=5.0, run_stall_s=30.0)          # 쿨러로는 5 °C 까지 못 내려간다
    ctrl.run_command("run_start")
    _finish(ctrl, 60)
    it = ctrl.runner.items[ids[0]]
    assert it["status"] == DONE and any("에서 끝냄" in n for n in it["notes"])
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
    assert not ctrl.fake_fw.relay[0], "히트건은 꺼져 있다"
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
    assert ctrl.state.get("run", "summary") == "1개 중 1개 끝남"
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
    first = {n: (folder / n).read_bytes() for n in want}
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
    second = {n: (folder / n).read_bytes() for n in want}
    assert all(second[n] != first[n] and second[n] for n in want), "새 데이터로 다시 썼다"
    old_dirs = [p for p in folder.iterdir() if p.is_dir()]
    assert len(old_dirs) == 1 and re.match(r"이전_\d{6}$", old_dirs[0].name)
    assert {p.name: p.read_bytes() for p in old_dirs[0].iterdir()} == first, "예전 파일은 바이트 그대로 옮겨졌다"
    assert second[want[0]].startswith(b"Temp   Resistance   \r\n") and second[want[0]][len(b"Temp   Resistance   \r\n"):] == second[want[1]]

    # run.json: 옮긴 것을 적는다 · 샘플의 파일 목록은 지금 있는 것만
    meta = json.loads((folder / "run.json").read_text(encoding="utf-8"))
    smp = meta["samples"][0]
    assert sorted(smp["files"]) == want and smp["cycles"][0]["file"] == want[0]
    assert len(smp["moved"]) == 1 and smp["moved"][0]["to"] == old_dirs[0].name and sorted(smp["moved"][0]["files"]) == want

    # NAS: 같은 이름으로 덮어썼다(이전_ 폴더는 PC 에만)
    assert sorted(p.name for p in nas_dir.iterdir()) == want
    assert all((nas_dir / n).read_bytes() == second[n] for n in want)
    assert meta["nas"]["ok"] is True and meta["nas"]["names"] == {n: n for n in want}
    assert not ctrl.state.has_alarm("nas")

    # 결과 탭: 새 데이터로 계산하고, 파일 목록에 이전_ 폴더가 보인다
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
    # 결과 탭 [다시 복사]도 이 기록이 쓴 이름으로만 덮어쓴다
    ctrl.run_command("result_nas_copy", {"id": "%s/%s" % (run.folder.parent.name, run.folder.name)})
    assert (other / (name + "_1.00V_0.dat")).read_bytes() == b"other record"
    assert sorted(p.name for p in other.iterdir()) == sorted([name + "_1.00V_0.dat", name + "_1.00V_0_1.dat", name + "_1.00V_merged.dat"])
