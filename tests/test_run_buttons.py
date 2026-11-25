"""자동 측정 버튼: [나머지 측정](run_resume = 체크한 샘플 중 완료 아닌 것 · 같은 폴더) · [이 샘플 다시 측정] · [이 샘플만 측정](run_one = 그 샘플 하나만) ·
확인 창(move_plan)의 제목 · 잴 샘플 이름 · 저장 위치 · 이전 데이터 줄. 기록 모드(히트건 사용 끔 · 짧은 기록 시간)로 빠르게 돈다."""

import pytest

from app.jobs.run import DONE, SKIPPED, STOPPED, WAIT
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


def _statuses(run, ids):
    return [run.items[i]["status"] for i in ids]


def _done_run(ready_ctrl):
    """3개를 다 잰 뒤 첫 샘플 = 건너뜀 · 둘째 = 완료 · 셋째 = 중지로 둔다(상태만 - 파일은 셋 다 있다)."""
    ctrl, ids = _ctrl(ready_ctrl, 3)
    run = ctrl.runner
    ctrl.run_command("run_start")
    _finish(ctrl)
    assert _statuses(run, ids) == [DONE] * 3
    run.items[ids[0]].update(status=SKIPPED, reason="안 닿음 · 9.54 GΩ")
    run.items[ids[2]].update(status=STOPPED, reason="")
    run.state = "stopped"
    run.publish()
    return ctrl, ids


def _old_dirs(run):
    return [p for p in run.folder.iterdir() if p.is_dir() and p.name.startswith("이전_")]


def test_rest_remeasures_skipped_and_stopped_only(ready_ctrl):
    ctrl, (a, b, c) = _done_run(ready_ctrl)
    run = ctrl.runner
    assert run.rest_ids() == [a, c] and ctrl.state.get("run", "rest") == 2, "건너뜀 · 중지 둘(완료는 빼고)"
    folder = run.folder
    t_b = run.items[b]["t0"]
    ctrl.run_command("run_resume")
    _finish(ctrl)
    assert _statuses(run, [a, b, c]) == [DONE] * 3 and run.folder == folder
    assert run.items[b]["t0"] == t_b, "완료는 다시 재지 않는다"
    assert _old_dirs(run), "다시 잰 샘플의 예전 파일은 이전_ 폴더로"
    assert run.rest_ids() == [] and ctrl.state.get("run", "rest") == 0
    with pytest.raises(Rejected, match="남은 샘플 없음"):
        ctrl.run_command("run_resume")


def test_rest_skips_unchecked_and_rejection_changes_nothing(ready_ctrl):
    ctrl, (a, b, c) = _done_run(ready_ctrl)
    run = ctrl.runner
    ctrl.run_command("sample_use", {"id": a, "use": False})         # 체크를 푼 건너뜀 샘플은 다시 재지 않는다
    assert run.rest_ids() == [c] and ctrl.state.get("run", "rest") == 1
    sc = next(s for s in ctrl.prep.samples if s["id"] == c)
    xy0, sc["xy"] = sc["xy"], None                                  # 좌표를 못 구하는 샘플 → 거절
    with pytest.raises(Rejected, match="위치 계산 실패"):
        ctrl.run_command("run_resume")
    assert _statuses(run, [a, b, c]) == [SKIPPED, DONE, STOPPED], "거절되면 어떤 샘플의 상태도 그대로"
    assert not run.resumes, "거절되면 재개 기록도 남기지 않는다"
    sc["xy"] = xy0
    ctrl.run_command("run_resume")
    _finish(ctrl)
    assert _statuses(run, [a, b, c]) == [SKIPPED, DONE, DONE]
    assert not next(s for s in ctrl.prep.samples if s["id"] == a)["use"], "체크는 그대로"


def test_run_one_remeasures_only_that_sample(ready_ctrl):
    ctrl, (a, b, c) = _ctrl(ready_ctrl, 3)
    run = ctrl.runner
    ctrl.run_command("run_start")
    ctrl.run_command("run_pause_after", {"on": True})               # 첫 샘플 뒤 정지 → 둘은 대기
    _finish(ctrl)
    assert _statuses(run, [a, b, c]) == [DONE, WAIT, WAIT]
    folder, t_a = run.folder, run.items[a]["t0"]
    use0 = {s["id"]: s["use"] for s in ctrl.prep.samples}
    ctrl.run_command("run_one", {"id": a})                          # [이 샘플 다시 측정]
    _finish(ctrl)
    assert run.folder == folder and run.items[a]["status"] == DONE and run.items[a]["t0"] != t_a, "그 샘플만 같은 폴더에 다시"
    assert _statuses(run, [b, c]) == [WAIT, WAIT] and not run.items[b]["points"] and not run.items[c]["points"], "다른 대기 샘플은 대기 그대로"
    assert _old_dirs(run), "예전 파일은 이전_ 폴더로"
    assert {s["id"]: s["use"] for s in ctrl.prep.samples} == use0, "체크는 그대로"
    ctrl.run_command("run_one", {"id": c})                          # [이 샘플만 측정](대기)
    _finish(ctrl)
    assert _statuses(run, [a, b, c]) == [DONE, WAIT, DONE]


def test_run_one_without_record_starts_new_run_and_refused_while_running(ready_ctrl):
    ctrl, ids = _ctrl(ready_ctrl, 2)
    run = ctrl.runner
    off = next(s["id"] for s in ctrl.prep.samples if not s["use"] and s["xy"] is not None)
    use0 = {s["id"]: s["use"] for s in ctrl.prep.samples}
    ctrl.update_settings({"run_hold_s": 600.0})
    ctrl.run_command("run_one", {"id": off})                        # 기록이 없을 때: 그 샘플 하나로 새 측정(체크하지 않은 샘플이어도)
    assert wait_for(lambda: run.cur == off, 20)
    assert run.order == [off]
    with pytest.raises(Rejected):
        ctrl.run_command("run_one", {"id": ids[0]})                 # 측정 중 거절
    ctrl.run_command("run_stop")
    _finish(ctrl)
    assert {s["id"]: s["use"] for s in ctrl.prep.samples} == use0, "체크는 그대로"


def test_move_plan_titles_samples_folder_and_redo(ready_ctrl):
    ctrl, (a, b, c) = _ctrl(ready_ctrl, 3)
    run = ctrl.runner
    names = [next(s["name"] for s in ctrl.prep.samples if s["id"] == i) for i in (a, b, c)]
    p = ctrl.move_plan("run_start")
    assert p["title"] == "측정 시작" and p["where"] == "새 폴더" and p["samples"] == "잴 샘플 3개 · " + " → ".join(names) and "redo" not in p, p
    p = ctrl.move_plan("run_one", {"id": a})
    assert p["title"] == "이 샘플만 측정" and p["where"] == "새 폴더" and p["samples"] == "잴 샘플 1개 · " + names[0], p
    ctrl.run_command("run_start")
    ctrl.run_command("run_pause_after", {"on": True})
    _finish(ctrl)
    rec = "같은 폴더 · %s/%s" % (run.folder.parent.name, run.folder.name)
    p = ctrl.move_plan("run_start")
    assert p["title"] == "새로 측정" and p["where"] == "새 폴더", p
    p = ctrl.move_plan("run_resume")
    assert p["title"] == "나머지 측정" and p["where"] == rec and p["samples"] == "잴 샘플 2개 · " + " → ".join(names[1:]) and "redo" not in p, p
    p = ctrl.move_plan("run_one", {"id": a})
    assert p["title"] == "이 샘플 다시 측정" and p["where"] == rec and p["samples"] == "잴 샘플 1개 · " + names[0], p
    assert p["redo"] == "다시 재는 샘플의 이전 데이터 → '이전_시각' 폴더로 옮김", p
    p = ctrl.move_plan("run_one", {"id": b})
    assert p["title"] == "이 샘플만 측정" and "redo" not in p, p
    run.items[b].update(status=SKIPPED)                              # 나머지에 다시 재는 샘플이 섞이면 이전 데이터 줄
    p = ctrl.move_plan("run_resume")
    assert p["redo"] and p["samples"].startswith("잴 샘플 2개"), p
