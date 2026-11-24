"""기록 모드(측정 조건 "히트건 사용" 끔): 이동 → 접촉 → 기록 시간 동안 그 자리 온도에서 저항 기록 → 저장 → 다음 샘플. 히트건 · 쿨러는 켜지 않는다.
파일은 LabVIEW 형식 그대로(_0.dat · merged) · 요약 analysis.hold_summary(run.json sum.hold · summary.csv) · 히트건에만 필요한 시작 조건은 보지 않는다.

시험용 세계: 접촉 높이(판 − 0.6 mm)에서 세계 샘플 1 · 3(두께 0.65 · 0.7 mm)은 닿고 0(0.5 mm)은 안 닿는다.
"""

import csv
import json
import re

import pytest

from app.jobs import analysis, files
from app.jobs.run import DONE, SKIPPED, WAIT
from app.safety import Rejected
from tests.conftest import wait_for

FAST = 200.0
# 기록 5 s · 측정 간격 0.5 s - 읽기 시간 예상(온도 0.1 × 2 + 저항 0.1)이 간격 안에 들게 읽기 값을 줄인다
HOLD = dict(time_scale=FAST, run_heater_use=False, run_hold_s=5.0, run_interval_s=0.5, temp_read_s=0.1, meter_avg_count=1, meter_nplc=0.1)
OPEN_I, TOUCH = 0, (1, 3)


def _use_world(ctrl, idxs):
    w = ctrl.world
    ctrl.run_command("samples_find")
    near = {}
    for k in idxs:
        x0, y0 = w.sample_stage(k)
        near[k] = min(ctrl.prep.samples, key=lambda s: abs(s["xy"][0] - x0) + abs(s["xy"][1] - y0))["id"]
    for s in ctrl.prep.samples:
        if s["use"] != (s["id"] in near.values()):
            ctrl.run_command("sample_use", {"id": s["id"], "use": s["id"] in near.values()})
    return [near[k] for k in idxs]


def _relay_on_cmds(ctrl, n0):
    return [c for c in ctrl.fake_fw.log[n0:] if re.match(r"r \d+ 1$", c) or re.match(r"hp [1-9]", c)]


# ---------------------------------------------------------------------------
# 요약 함수
# ---------------------------------------------------------------------------
def test_hold_summary_constant_slope_and_open_points():
    rows = [(t * 1.0, 25.0, 1e5, 1e-6) for t in range(61)]
    s = analysis.hold_summary(rows, open_a=1e-9)
    assert s["n"] == 61 and s["dur_s"] == 60.0 and s["r_mean"] == pytest.approx(1e5) and s["r_sd_pct"] == pytest.approx(0.0, abs=1e-9)
    assert s["drift_pct_min"] == pytest.approx(0.0, abs=1e-9) and s["t_mean"] == 25.0 and s["n_open"] == 0 and s["n_miss"] == 0
    rows = [(t * 1.0, 25.0 + 0.01 * t, 1e5 * (1 + 0.001 * t / 60), 1e-6) for t in range(121)]       # 1 분에 0.1 % 오름
    s = analysis.hold_summary(rows)
    assert s["drift_pct_min"] == pytest.approx(0.1, rel=0.02) and s["t_min"] == 25.0 and s["t_max"] == pytest.approx(26.2)
    rows = [(0.0, 25.0, 1e5, 1e-6), (1.0, 25.0, 1e5, 1e-12), (2.0, 25.0, None, None), (3.0, 25.0, 1e5, 1e-6)]
    s = analysis.hold_summary(rows, open_a=1e-9, n_miss=2)
    assert s["n"] == 3 and s["n_open"] == 1 and s["n_miss"] == 2
    assert analysis.hold_summary([])["n"] == 0


# ---------------------------------------------------------------------------
# 측정
# ---------------------------------------------------------------------------
def test_hold_run_records_without_heater_or_coolers(ready_ctrl):
    ctrl = ready_ctrl(contact_check="off", **HOLD)
    ids = _use_world(ctrl, list(TOUCH))
    n0 = len(ctrl.fake_fw.log)
    p = ctrl.move_plan("run_start")
    assert p and "히트건 사용 안 함 · 샘플마다 5 s 기록 · 쿨러 안 씀" in p["steps"]
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    run = ctrl.runner
    assert not _relay_on_cmds(ctrl, n0), "히트건 · 쿨러 릴레이 · 열선 출력은 한 번도 켜지지 않는다: %s" % _relay_on_cmds(ctrl, n0)
    rec = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert rec["cond"]["run_heater_use"] is False and rec["cond"]["run_hold_s"] == 5.0
    for sid in ids:
        it = run.items[sid]
        assert it["status"] == DONE, (it["status"], it["reason"])
        assert len(it["cycles"]) == 1 and all(p[2] == 2 and len(p) == 4 for p in it["points"])
        n = len(it["points"])
        assert 6 <= n <= 14, "점 수 ≈ 기록 시간 ÷ 측정 간격 (%d)" % n
        dat = (run.folder / (it["name"] + "_0.10V_0.dat")).read_bytes().decode("ascii").split("\r\n")
        mer = (run.folder / (it["name"] + "_0.10V_merged.dat")).read_bytes().decode("ascii").split("\r\n")
        assert dat[0] == files.HEADER and dat[1:] == mer and len(dat) == n + 2, "LabVIEW 형식 그대로(머리글 · 탭 · CRLF)"
        assert not (run.folder / (it["name"] + "_0.10V_1.dat")).exists()
        ctl = (run.folder / (it["name"] + "_0.10V_control.csv")).read_text(encoding="utf-8-sig").splitlines()[1:]
        assert ctl and all(r.split(",")[1] == "기록" and r.split(",")[5] == "" and r.split(",")[6] == "" for r in ctl), "단계 기록 · 목표 · 열선 칸 비움"
        hd = next(s for s in rec["samples"] if s["id"] == sid)["sum"]["hold"]
        assert hd["n"] == n and hd["r_mean"] > 0 and hd["t_mean"] is not None and hd["dur_s"] >= 4.0
    with open(run.folder / "summary.csv", encoding="utf-8-sig", newline="") as f:
        rows = list(csv.reader(f))
    i = rows[0].index("기록 점 수")
    assert all(r[i] for r in rows[1:]) and "저항 흔들림(%)" in rows[0] and "1분당 변화(%)" in rows[0]


def test_hold_contact_check_skip_and_stop_resume(ready_ctrl):
    ctrl = ready_ctrl(contact_check="skip", **HOLD)
    a, b = _use_world(ctrl, [OPEN_I, TOUCH[0]])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    run = ctrl.runner
    assert run.items[a]["status"] == SKIPPED and not run.items[a]["points"] and run.items[b]["status"] == DONE
    ctrl.update_settings({"contact_check": "stop"})
    (a,) = _use_world(ctrl, [OPEN_I])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert run.items[a]["status"] == WAIT and run.state == "stopped"
    ctrl.world.samples[OPEN_I]["thick"] = 0.7
    ctrl.run_command("run_resume")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert run.items[a]["status"] == DONE and run.items[a]["sum"]["hold"]["n"] > 0


def test_hold_mode_skips_heater_only_start_conditions(make_ctrl, ready_ctrl):
    """펌웨어 열선 출력(V18) · 쿨러 배치 빈 칸은 히트건을 쓸 때만 본다."""
    ctrl = ready_ctrl(firmware="V17", contact_check="off", **dict(HOLD, run_heater_use=True))
    _use_world(ctrl, [TOUCH[0]])
    with pytest.raises(Rejected, match="펌웨어 V18 필요"):
        ctrl.run_command("run_start")
    ctrl.update_settings({"run_heater_use": False})
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    ctrl2 = ready_ctrl(contact_check="off", **dict(HOLD, run_heater_use=True))
    _use_world(ctrl2, [TOUCH[0]])
    ctrl2.update_settings({"cooler_grid": [[None, None, None], [None, None, None], [None, None, None]]})
    assert {"text": "쿨러 배치 빈 칸", "step": 7} in ctrl2.runner.gate()
    with pytest.raises(Rejected, match="쿨러 배치 빈 칸"):
        ctrl2.run_command("run_start")
    ctrl2.update_settings({"run_heater_use": False})
    assert {"text": "쿨러 배치 빈 칸", "step": 7} not in ctrl2.runner.gate()
    ctrl2.run_command("run_start")
    assert wait_for(lambda: not ctrl2.runner.active, 60)
