"""[측정 재개] 가 지금 좌표 · 지금 접촉 설정을 쓴다(측정 조건은 시작 때 그대로) · 저항 한 점의 온도 = 저항 앞 · 뒤 온도의 평균(읽기 시점 맞추기).

시험용 세계의 접촉 높이(판 − 0.6 mm)는 두께 0.5 mm 샘플(세계 번호 0)에 닿지 않는다 - 접촉 확인 "측정 멈춤" 으로 세운 뒤 고쳐서 재개한다.
"""

import json
import math

import pytest

from app.jobs.run import CONTROL_HEADER, DONE, WAIT
from app.safety import Rejected
from tests.conftest import wait_for

FAST = 200.0
SHORT = dict(time_scale=FAST, run_heater_off_c=40.0, run_cool_end_c=36.0, run_heat_mode="full")
OPEN_I, TOUCH_I = 0, 1


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


def _stopped_on_open_sample(ready_ctrl):
    ctrl = ready_ctrl(contact_check="stop", **SHORT)
    (a,) = _use_world(ctrl, [OPEN_I])
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert ctrl.runner.items[a]["status"] == WAIT and ctrl.runner.state == "stopped"
    return ctrl, a


# ---------------------------------------------------------------------------
# 재개
# ---------------------------------------------------------------------------
def test_resume_uses_current_position_and_contact_height(ready_ctrl):
    ctrl, a = _stopped_on_open_sample(ready_ctrl)
    run = ctrl.runner
    old_xy = list(run.items[a]["xy"])
    old_cz = run.cond["contact_z_mm"]
    volt0 = run.cond["meter_volt"]
    s = next(q for q in ctrl.prep.samples if q["id"] == a)
    ctrl.goto(s["xy"][0] + 1.0, s["xy"][1] + 0.5)                    # 핀이 실제로 있는 자리가 계산보다 (1.0, 0.5) 옆 → 보정 점 하나 = 판 전체 평행 이동
    ctrl.run_command("coord_add_at", {"x": s["pin"][0], "y": s["pin"][1]})
    new_xy = list(s["xy"])
    assert math.hypot(new_xy[0] - old_xy[0], new_xy[1] - old_xy[1]) > 1.0
    new_cz = round(old_cz + 0.15, 3)                                 # 두께 0.5 mm 샘플에도 닿는 높이
    ctrl.update_settings({"contact_z_mm": new_cz, "meter_volt": 0.2})  # 측정 조건(전압)은 재개해도 시작 때 값
    p = ctrl.move_plan("run_resume")
    assert p is not None and p["changes"].startswith("위치 갱신 1개") and ("%.3f → %.3f mm" % (old_cz, new_cz)) in p["changes"], p
    n0 = len(ctrl.logs.recent)
    ctrl.run_command("run_resume")
    assert wait_for(lambda: not run.active, 60)
    it = run.items[a]
    assert it["status"] == DONE, (it["status"], it["reason"])
    assert abs(it["xy"][0] - new_xy[0]) < 1e-6 and abs(it["xy"][1] - new_xy[1]) < 1e-6, "지금 좌표로 간다"
    assert it["contact"]["z"] == new_cz and run.cond["contact_z_mm"] == new_cz, "지금 접촉 높이로 내린다"
    assert run.cond["meter_volt"] == volt0, "측정 조건은 시작 때 값 그대로"
    logs = ctrl.logs.recent[n0:]
    assert any("] 재개 · %s 위치 갱신 · X " % it["name"] in ln for ln in logs), logs     # 로그 줄 앞에 "시각 [등급] " 이 붙는다
    assert any("] 재개 · 접촉 높이 %.3f → %.3f mm" % (old_cz, new_cz) in ln for ln in logs), logs
    rec = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert rec["resumes"] and rec["resumes"][0]["moved"] == [it["name"]] and rec["resumes"][0]["contact"]["contact_z_mm"] == [old_cz, new_cz]
    assert rec["cond"]["meter_volt"] == volt0


def test_resume_refused_when_sample_is_gone(ready_ctrl):
    ctrl, a = _stopped_on_open_sample(ready_ctrl)
    ctrl.prep.samples = [s for s in ctrl.prep.samples if s["id"] != a]
    with pytest.raises(Rejected, match="없음"):
        ctrl.run_command("run_resume")
    assert not ctrl.runner.active


def test_resume_refused_when_position_cannot_be_computed(ready_ctrl):
    ctrl, a = _stopped_on_open_sample(ready_ctrl)
    next(s for s in ctrl.prep.samples if s["id"] == a)["xy"] = None
    with pytest.raises(Rejected, match="위치 계산 실패 · 좌표 보정 확인"):
        ctrl.run_command("run_resume")


# ---------------------------------------------------------------------------
# 읽기 시점: 온도 T1 → 저항 → 온도 T2 · 기록 = 평균
# ---------------------------------------------------------------------------
def test_read_tr_averages_and_falls_back(ready_ctrl):
    ctrl = ready_ctrl()
    run = ctrl.runner
    run._read_r = lambda: (1e-6, 1e5, False)
    for seq, want in (([30.0, 31.0], 30.5), ([30.0, None], 30.0), ([None, 31.0], 31.0)):
        vals = list(seq)
        run._temp_raw = lambda: vals.pop(0)
        T, T1, T2, _cur, R, _trip = run._read_tr()
        assert T == want and (T1, T2) == tuple(seq) and R == 1e5
    vals = [None, None]
    run._temp_raw = lambda: vals.pop(0)
    run._t_miss = 0
    assert run._read_tr()[0] is None and run._t_miss == 1, "둘 다 없으면 그 점 없음 · 못 읽음 한 번"


def test_recorded_temperature_is_the_mean_of_before_and_after(ready_ctrl):
    """온도계가 읽을 때마다 +0.5 · −0.5 °C 번갈아 어긋나게 → control.csv 의 기록 온도 = (온도 1 + 온도 2) ÷ 2 · 열 10개."""
    ctrl = ready_ctrl(contact_check="off", **SHORT)
    (a,) = _use_world(ctrl, [TOUCH_I])
    dev = ctrl.k2110
    orig = dev.read_temp
    k = {"n": 0}

    def skewed():
        v = orig()
        k["n"] += 1
        return None if v is None else v + (0.5 if k["n"] % 2 else -0.5)

    dev.read_temp = skewed
    try:
        ctrl.run_command("run_start")
        assert wait_for(lambda: not ctrl.runner.active, 60)
    finally:
        dev.read_temp = orig
    run = ctrl.runner
    it = run.items[a]
    assert it["status"] == DONE, (it["status"], it["reason"])
    lines = (run.folder / (it["name"] + "_0.10V_control.csv")).read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].split(",") == list(CONTROL_HEADER) and len(CONTROL_HEADER) == 10
    rows = [r.split(",") for r in lines[1:]]
    assert rows and all(len(r) == 10 for r in rows)
    for r in rows:
        t, t1, t2 = float(r[3]), float(r[8]), float(r[9])
        assert abs(t - (t1 + t2) / 2) < 2e-3, r
    assert any(abs(float(r[8]) - float(r[9])) > 0.5 for r in rows), "앞 · 뒤 온도가 다르게 들어왔다(온도 감시 스레드도 읽어 번갈아가 가끔 어긋난다 - 하나라도)"
