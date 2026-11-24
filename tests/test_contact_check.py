"""접촉 확인(contact_check - 고정 높이로 내린 뒤 저항 한 번으로 판정 · contact.judge_fixed) · 가열 중단 샘플 실패 · 수동 조작 로그 · 측정 시작 창 사진 줄.

시험용 세계의 접촉 높이(판 − 0.6 mm)는 두께 0.5 · 0.55 mm 샘플(세계 번호 0 · 2 · 5 · 7)에는 닿지 않고(열린 회로 - 수십 GΩ) 0.6 mm 이상에는 닿는다(수백 kΩ).
"""

import pytest

from app import config as config_mod
from app import contact as contact_mod
from app.jobs.run import DONE, FAIL, SKIPPED, WAIT
from tests.conftest import wait_for

FAST = 200.0
SHORT = dict(time_scale=FAST, run_heater_off_c=40.0, run_cool_end_c=36.0, run_heat_mode="full")     # 가열 · 냉각은 짧게(접촉 단계를 보는 시험)
OPEN_I, TOUCH_I = 0, 1                                               # 세계 샘플: 0 = 두께 0.5 mm(안 닿음) · 1 = 0.65 mm(닿음)


# ---------------------------------------------------------------------------
# 판정 함수
# ---------------------------------------------------------------------------
def test_judge_fixed_bounds():
    j = contact_mod.judge_fixed
    assert j(15e3, 1e8) is None
    assert j(1e8, 1e8) is None, "상한과 같으면 닿음"
    assert j(2.56e9, 1e8) == ("open", "안 닿음 · 2.56 GΩ")
    assert j(586e6, 1e8) == ("open", "안 닿음 · 586 MΩ")
    assert j(None, 1e8) == ("none", "저항 못 읽음")
    assert j(0.8, 1e8, 10.0) == ("metal", "금속 판 의심 · 0.8 Ω")
    assert j(10.0, 1e8, 10.0) is None, "하한과 같으면 닿음"
    assert j(0.8, 1e8, None) is None, "하한이 없으면 금속 판을 보지 않는다"
    assert j(5e3, 1e8, tripped=True)[0] == "metal", "전류 한도 걸림 = 금속 판 의심"
    assert contact_mod.ohm_text(912e3) == "912 kΩ" and contact_mod.ohm_text(2.2e3) == "2.2 kΩ"


def test_problems_need_read_when_check_is_on():
    v = {i.key: i.default for i in config_mod.ITEMS}
    assert not [p for p in config_mod.problems(v) if "접촉" in p]
    bad = config_mod.problems(dict(v, contact_check="skip", contact_fixed_read=False))
    assert "접촉 확인 사용 중 · 접촉 후 저항 기록 켬 필요" in bad
    assert not [p for p in config_mod.problems(dict(v, contact_check="off", contact_fixed_read=False)) if "접촉 확인" in p]
    assert [p for p in config_mod.problems(dict(v, contact_min_ohm=1e9, contact_max_ohm=1e8)) if "금속 판 판정 하한" in p]


# ---------------------------------------------------------------------------
# 측정
# ---------------------------------------------------------------------------
def _use_world(ctrl, idxs):
    """세계 샘플 번호(idxs)에 해당하는 검출 샘플만 포함. 반환: 그 샘플 id(idxs 순서)."""
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


def test_skip_moves_on_without_heating(ready_ctrl):
    ctrl = ready_ctrl(contact_check="skip", **SHORT)
    a, b = _use_world(ctrl, [OPEN_I, TOUCH_I])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    run = ctrl.runner
    ia, ib = run.items[a], run.items[b]
    assert ia["status"] == SKIPPED and ia["reason"].startswith("안 닿음 · ") and "GΩ" in ia["reason"], (ia["status"], ia["reason"])
    assert ia["contact"]["judge"] == "open" and not ia["points"] and not ia["element_log"], "안 닿은 샘플은 가열하지 않는다"
    assert ib["status"] == DONE and ib["contact"]["judge"] == "ok", (ib["status"], ib["reason"])
    assert run.state == "done"
    assert any("접촉 확인 · 안 닿음" in ln for ln in ctrl.logs.recent)


def test_stop_halts_run_and_resume_starts_from_that_sample(ready_ctrl):
    ctrl = ready_ctrl(contact_check="stop", **SHORT)
    (a,) = _use_world(ctrl, [OPEN_I])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    run = ctrl.runner
    it = run.items[a]
    assert it["status"] == WAIT and it["reason"].startswith("안 닿음 · ") and not it["points"]
    assert run.state == "stopped"
    al = [x["text"] for x in ctrl.state.get("alarms") if x["code"] == "run"]
    assert al and al[0].startswith("측정 멈춤 · %s 접촉 안 됨 · " % it["name"]), al
    ctrl.world.samples[OPEN_I]["thick"] = 0.7                        # 샘플을 고쳐 놓음 → [측정 재개] 가 그 샘플부터
    ctrl.run_command("run_resume")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert run.items[a]["status"] == DONE and run.items[a]["contact"]["judge"] == "ok", (run.items[a]["status"], run.items[a]["reason"])


def test_off_only_records(ready_ctrl):
    ctrl = ready_ctrl(contact_check="off", **SHORT)
    (a,) = _use_world(ctrl, [OPEN_I])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    it = ctrl.runner.items[a]
    assert it["status"] == DONE and "judge" not in it["contact"] and it["contact"]["ohm"] > 1e9


def test_heater_cut_by_safety_fails_sample_after_cooling(ready_ctrl):
    """안전 규칙(차단 온도)이 가열을 끊으면: 냉각까지 기록하고 그 샘플은 실패 · 이유 "가열 중단 · …" · 남은 회차는 하지 않는다."""
    ctrl = ready_ctrl(contact_check="off", time_scale=FAST, run_heater_off_c=60.0, run_cool_end_c=40.0, run_heat_mode="full", run_repeat=2)
    (a,) = _use_world(ctrl, [TOUCH_I])
    ctrl.run_command("run_start")
    run = ctrl.runner
    assert wait_for(lambda: run.stage == 3, 30), "가열 단계"
    ctrl.cfg.values["heater_max_c"] = 45.0                           # 시험: 측정 도중 차단 온도를 가열 끝 온도 아래로(감시는 바로 쓴다)
    assert wait_for(lambda: not run.active, 60)
    it = run.items[a]
    assert it["status"] == FAIL and it["reason"].startswith("가열 중단 · "), (it["status"], it["reason"])
    assert it["points"] and it["points"][-1][2] == 1, "냉각까지 기록"
    assert len(it["cycles"]) == 1, "남은 회차는 하지 않는다"


# ---------------------------------------------------------------------------
# 수동 조작 로그 · 측정 시작 창
# ---------------------------------------------------------------------------
def test_manual_actions_are_logged(ready_ctrl):
    ctrl = ready_ctrl()
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("read_temp")
    ctrl.run_command("read_resistance")
    ctrl.run_command("goto", {"x": 60.0, "y": 60.0})
    ctrl.run_command("jog", {"axis": "z", "mm": 0.1})
    ctrl.run_command("z_top")
    ctrl.run_command("monitor", {"on": True})
    assert wait_for(lambda: ctrl.state.get("resistance", "ohm") is not None, 10)
    ctrl.run_command("monitor", {"on": False})
    new = ctrl.logs.recent[n0:]

    def has(prefix):
        return any(prefix in ln for ln in new)

    assert has("수동 측정 · 온도 ") and has("수동 측정 · 저항 ") and has(" · X 60.000 · Y 60.000 · Z ")
    assert has("좌표 이동 · X 60.0 · Y 60.0 → 끝 X 60.000")
    assert has("조그 · Z 하강 0.10 mm · Z 0.000 → 0.100")
    assert has("좌표 이동 · 핀 상승 · Z 0.100 → 끝")
    assert has("연속 측정 시작 · 저항 · X 60.000") and has("연속 측정 끝 · ")


def test_run_start_plan_shows_photo_time_even_without_move_confirm(ready_ctrl):
    ctrl = ready_ctrl(confirm_auto_move=False)
    assert ctrl.move_plan("run_start") is None or ctrl.move_plan("run_start")["photo"] == "사진 없음"
    ctrl.run_command("samples_find")
    p = ctrl.move_plan("run_start")
    assert p is not None, "측정 시작 창은 자동 이동 전 확인 설정과 상관없이 뜬다"
    n_all = len(ctrl.prep.samples)
    n_use = sum(1 for s in ctrl.prep.samples if s["use"] and s["xy"] is not None)
    assert p["photo"].startswith("사진 ") and p["photo"].endswith("샘플 %d개 중 %d개 측정" % (n_all, n_use)) and p["photo_old"] is False
    ctrl.prep.photo["t"] -= 2 * 3600
    assert ctrl.move_plan("run_start")["photo_old"] is True, "1시간 넘으면 주의 색"
    assert ctrl.move_plan("tour_start") is None, "다른 이동 확인은 설정을 따른다"
