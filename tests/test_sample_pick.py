"""측정할 샘플 고르기(순서 표 체크): sample_use_all(전체 선택 · 해제) · [측정 재개] 는 체크한 "대기" 만 잰다(체크를 푼 대기는 "대기" 로 남는다).
기록 모드(히트건 사용 끔 · 짧은 기록 시간)로 빠르게 돈다."""

import pytest

from app.jobs.run import WAIT
from app.safety import Rejected
from tests.conftest import wait_for

FAST = 200.0
HOLD = dict(time_scale=FAST, run_heater_use=False, run_hold_s=20.0, contact_check="off")


def _ctrl(ready_ctrl, n=3):
    ctrl = ready_ctrl(**HOLD)
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    assert len(use) >= n + 1, len(use)
    for s in use[n:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    return ctrl, [s["id"] for s in use[:n]]


def _finish(ctrl, timeout=60):
    assert wait_for(lambda: not ctrl.runner.active, timeout), "측정이 끝나지 않았다"


def test_sample_use_all_keeps_small_and_weak(ready_ctrl):
    ctrl = ready_ctrl(**HOLD)
    ctrl.run_command("samples_find")
    ss = ctrl.prep.samples
    assert len(ss) >= 4, len(ss)
    small, weak, weak_on, plain = ss[0], ss[1], ss[2], ss[3]
    small.update(small=True, use=False)
    weak.update(weak=True, use=False)
    weak_on.update(weak=True, use=True)                             # 사람이 이미 체크한 미확인 샘플 - 전체 선택이 건드리지 않는다
    plain["use"] = False
    r = ctrl.run_command("sample_use_all", {"use": True})
    assert small["use"] is False and weak["use"] is False and weak_on["use"] is True, "핀보다 작음 · 미확인은 지금 값 그대로"
    assert all(s["use"] for s in ss if not s.get("small") and not s.get("weak")), "나머지는 모두 체크"
    assert r["n"] == sum(1 for s in ss if s["use"])
    ctrl.run_command("sample_use_all", {"use": False})
    assert not any(s["use"] for s in ss), "전체 해제는 전부"


def test_sample_use_all_refused_while_running(ready_ctrl):
    ctrl, ids = _ctrl(ready_ctrl, 2)
    ctrl.update_settings({"run_hold_s": 600.0})
    ctrl.run_command("run_start")
    assert wait_for(lambda: ctrl.runner.cur is not None, 20)
    with pytest.raises(Rejected):
        ctrl.run_command("sample_use_all", {"use": False})
    with pytest.raises(Rejected):
        ctrl.prep.sample_use_all(False)                             # 명령 표를 거치지 않아도 측정 중에는 거절(_no_run)
    assert all(s["use"] for s in ctrl.prep.samples if s["id"] in ids)
    ctrl.run_command("run_stop")
    _finish(ctrl)


def test_resume_measures_only_checked_waiting_samples(ready_ctrl):
    ctrl, (a, b, c) = _ctrl(ready_ctrl, 3)
    run = ctrl.runner
    ctrl.run_command("run_start")
    ctrl.run_command("run_pause_after", {"on": True})               # 첫 샘플 뒤 정지
    _finish(ctrl)
    assert run.state == "stopped" and run.items[a]["status"] != WAIT and run.items[b]["status"] == WAIT and run.items[c]["status"] == WAIT
    ctrl.run_command("sample_use", {"id": b, "use": False})         # 남은 둘 중 하나의 체크를 푼다
    p = ctrl.move_plan("run_resume")
    assert p is not None and p["purpose"].startswith("샘플 1개"), p
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("run_resume")
    _finish(ctrl)
    assert run.items[c]["status"] != WAIT, "체크한 대기는 잰다"
    assert run.items[b]["status"] == WAIT and not run.items[b]["points"], "체크를 푼 대기는 재지 않고 대기로 남는다"
    assert run.state == "stopped", "대기가 남았다"
    assert any("측정 시작 · 나머지 측정 · 묶음" in ln and " · 1개 · " in ln for ln in ctrl.logs.recent[n0:]), ctrl.logs.recent[n0:]
    with pytest.raises(Rejected, match="남은 샘플 없음"):
        ctrl.run_command("run_resume")                              # 체크한 대기가 없다
    ctrl.run_command("sample_use", {"id": b, "use": True})          # 다시 체크하고 재개하면 잰다
    ctrl.run_command("run_resume")
    _finish(ctrl)
    assert run.items[b]["status"] != WAIT
    assert run.state == "done"
