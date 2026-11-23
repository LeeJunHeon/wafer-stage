"""히트건 · 쿨러 없이 "샘플 인식 → 이동 → 접촉": X · Y 조그 잠금(safety.reject_xy_jog) · [보정 점 추가](coord_add_at) 거리 검사 · 같은 자리 교체 ·
샘플로 이동 + 내리기(prep.go_and_lower - plate_goto · sample_goto) · 자동 순회(app/jobs/tour.py)."""

import threading

import pytest

from app.safety import Rejected
from tests.conftest import wait_for

PPMM = 160


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["res"] = fn()
        except Rejected as e:
            out["msg"] = str(e)

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _found(ctrl):
    ctrl.run_command("samples_find")
    assert ctrl.prep.find_info["path"] == "빈 판 비교", "시험용 장비도 빈 판 비교 경로를 탄다: %s" % ctrl.prep.find_info
    todo = [s for s in ctrl.prep.samples if s["xy"] is not None]
    assert todo, "샘플이 검출돼야 한다"
    return todo


def _cz(ctrl):
    return float(ctrl.cfg["contact_z_mm"])


def _gz_down(lines):
    """Z 를 내리는 절대 이동 줄(gz 0 이 아닌 것)."""
    return [ln for ln in lines if ln.startswith("gz ") and ln.split()[1] != "0"]


# ---------------------------------------------------------------------------
# [1] X · Y 조그 잠금
# ---------------------------------------------------------------------------
def test_xy_jog_locked_near_contact_height(ready_ctrl):
    ctrl = ready_ctrl()
    cz = _cz(ctrl)
    ctrl.goto(60.0, 60.0, cz)                                       # 핀이 접촉 높이에 있다
    for ax in ("x", "y"):
        with pytest.raises(Rejected, match="접촉 높이 근처"):
            ctrl.run_command("jog", {"axis": ax, "mm": 1.0})
    ctrl.run_command("jog", {"axis": "z", "mm": 0.1})                # Z 조그는 영향 없음
    ctrl.run_command("jog", {"axis": "z", "mm": -1.0})               # 여유 높이(0.5 mm)보다 위로 올리면 풀린다
    x0 = ctrl.state.get("pos", "x")
    ctrl.run_command("jog", {"axis": "x", "mm": 1.0})
    assert abs(ctrl.state.get("pos", "x") - (x0 + 1.0)) < 0.01


def test_xy_jog_free_without_contact_height_or_with_zero_clear(ready_ctrl):
    ctrl = ready_ctrl()
    cz = _cz(ctrl)
    ctrl.goto(60.0, 60.0, cz)
    ctrl.update_settings({"xy_jog_clear_mm": 0})                     # 0 = 검사 안 함
    ctrl.run_command("jog", {"axis": "x", "mm": 1.0})
    ctrl.update_settings({"xy_jog_clear_mm": 0.5})
    with pytest.raises(Rejected, match="접촉 높이 근처"):
        ctrl.run_command("jog", {"axis": "y", "mm": 1.0})
    ctrl._remember(contact_z_mm=None)                                 # 접촉 높이가 없으면 지금처럼 된다
    ctrl.run_command("jog", {"axis": "y", "mm": 1.0})


# ---------------------------------------------------------------------------
# [3] 샘플로 이동 + 내리기
# ---------------------------------------------------------------------------
def test_sample_goto_lower_lifts_pin_first_then_lowers(ready_ctrl):
    ctrl = ready_ctrl()
    todo = _found(ctrl)
    cz = _cz(ctrl)
    a, b = todo[0], todo[-1]
    ctrl.goto(a["xy"][0], a["xy"][1], cz)                            # 핀이 다른 샘플 위에서 내려가 있다
    fw = ctrl.fake_fw
    n = len(fw.log)
    r = ctrl.run_command("sample_goto", {"id": b["id"], "lower": True})
    assert r["lowered"] is True and r["sample"] == b["name"]
    log = fw.log[n:]
    iz = next(i for i, ln in enumerate(log) if ln.startswith("gz "))
    ixy = next(i for i, ln in enumerate(log) if ln.startswith("gx ") or ln.startswith("gy "))
    assert log[iz] == "gz 0" and iz < ixy, "X · Y 가 움직이기 전에 Z 가 0 이 된다: %s" % log
    assert abs(ctrl.state.get("pos", "x") - b["xy"][0]) < 0.01 and abs(ctrl.state.get("pos", "y") - b["xy"][1]) < 0.01
    assert abs(ctrl.state.get("pos", "z") - cz) < 0.01
    assert ctrl.prep.last_click["sample"] == b["name"] and ctrl.prep.last_click["plate"] == b["pin"], "선택 위치 = 그 샘플의 핀 놓을 자리"
    lc = dict(ctrl.prep.last_click)
    ctrl.run_command("coord_add_at", {"x": b["pin"][0], "y": b["pin"][1]})
    assert ctrl.prep.last_click == lc, "보정 점 추가는 선택 위치를 바꾸지 않는다"


def test_sample_goto_without_lower_stays_up(ready_ctrl):
    ctrl = ready_ctrl()
    s = _found(ctrl)[0]
    r = ctrl.run_command("sample_goto", {"id": s["id"]})
    assert r["lowered"] is False and ctrl.state.get("pos", "z") == 0.0


def test_stop_during_xy_move_does_not_lower(ready_ctrl):
    ctrl = ready_ctrl(scale=0.4)
    todo = _found(ctrl)
    x_now = ctrl.state.get("pos", "x")
    s = max(todo, key=lambda q: abs(q["xy"][0] - x_now))             # X 로 가장 멀리 가는 샘플(이동이 길게)
    fw = ctrl.fake_fw
    n = len(fw.log)
    t, out = _in_thread(lambda: ctrl.run_command("sample_goto", {"id": s["id"], "lower": True}))
    assert wait_for(lambda: ctrl.stage.moving and fw.busy and fw.log and (fw.log[-1].startswith("gx") or fw.log[-1].startswith("gy")), 10), "X · Y 이동이 시작되지 않았다"
    ctrl.run_command("motion_stop")
    t.join(15)
    assert out.get("msg") == "정지로 취소됨"
    assert not _gz_down(fw.log[n:]), "정지하면 내리지 않는다"
    assert ctrl.state.get("pos", "z") == 0.0


def test_plate_goto_lower_off_sample_does_not_lower(ready_ctrl):
    ctrl = ready_ctrl()
    _found(ctrl)
    fw = ctrl.fake_fw
    n = len(fw.log)
    r = ctrl.run_command("plate_goto", {"x": 1.0, "y": 1.0, "lower": True})       # 판 구석 - 샘플이 아닌 자리
    assert r["sample"] is None and r["lowered"] is False
    assert not _gz_down(fw.log[n:]) and ctrl.state.get("pos", "z") == 0.0


def test_plate_goto_lower_on_sample_lowers(ready_ctrl):
    ctrl = ready_ctrl()
    s = _found(ctrl)[0]
    r = ctrl.run_command("plate_goto", {"x": s["plate"][0] + 1.0, "y": s["plate"][1], "lower": True})
    assert r["sample"] == s["name"] and r["lowered"] is True
    assert abs(ctrl.state.get("pos", "z") - _cz(ctrl)) < 0.01


def test_sample_goto_lower_rejected_without_contact_height(ready_ctrl):
    ctrl = ready_ctrl()
    s = _found(ctrl)[0]
    ctrl._remember(contact_z_mm=None)
    fw = ctrl.fake_fw
    n = len(fw.log)
    with pytest.raises(Rejected, match="접촉 높이 필요"):
        ctrl.run_command("sample_goto", {"id": s["id"], "lower": True})
    assert not any(ln.startswith(("gx", "gy", "gz")) for ln in fw.log[n:]), "이동 전에 거절한다"


# ---------------------------------------------------------------------------
# [2] 보정 점 추가 거리 검사
# ---------------------------------------------------------------------------
def test_coord_add_at_far_same_and_new_point(ready_ctrl):
    ctrl = ready_ctrl()
    todo = _found(ctrl)
    s, t = todo[0], todo[-1]
    x, y = s["xy"]
    ex = lambda: ctrl.calib.d["coords"]["extra"]                    # noqa: E731
    n0 = len(ex())
    far = x + 15.0 if x + 15.0 < float(ctrl.cfg["x_max_mm"]) else x - 15.0
    ctrl.goto(far, y)                                                # 핀이 누른 자리에서 15 mm
    with pytest.raises(Rejected, match="핀에서"):
        ctrl.run_command("coord_add_at", {"x": s["pin"][0], "y": s["pin"][1]})
    assert len(ex()) == n0
    ctrl.goto(x, y)
    r = ctrl.run_command("coord_add_at", {"x": s["pin"][0], "y": s["pin"][1]})
    assert r["replaced"] is False and r["n"] == n0 + 1 and r["moved"] < 0.5
    assert "left" not in r
    e = ex()[-1]
    assert _dist_xy(ctrl.calib.to_stage(e["px"]), e["xy"]) < 0.01 and _dist_xy(e["xy"], ctrl.prep._xy()) < 0.01, "추가한 자리는 정확히 핀 X · Y 를 지난다"
    t0 = ex()[-1]["t"]
    ctrl.goto(x + 0.5, y)                                            # 같은 자리(3 mm 안)를 다시: 점 수 그대로 · 그 점을 교체
    r = ctrl.run_command("coord_add_at", {"x": s["pin"][0] + 1.0, "y": s["pin"][1]})
    assert r["replaced"] is True and r["n"] == n0 + 1
    e = ex()[-1]
    assert _dist_xy(ctrl.calib.to_stage(e["px"]), e["xy"]) < 0.01 and _dist_xy(e["xy"], ctrl.prep._xy()) < 0.01, "교체한 자리도 정확히"
    assert abs(ex()[-1]["xy"][0] - (x + 0.5)) < 0.01 and ex()[-1]["t"] >= t0
    assert _dist_xy(s["pin"], t["pin"]) > float(ctrl.cfg["coord_same_mm"])
    ctrl.goto(*t["xy"])                                              # 다른 자리: 추가
    r = ctrl.run_command("coord_add_at", {"x": t["pin"][0], "y": t["pin"][1]})
    assert r["replaced"] is False and r["n"] == n0 + 2
    e = ex()[-1]
    assert _dist_xy(ctrl.calib.to_stage(e["px"]), ctrl.prep._xy()) < 0.01
    ctrl.run_command("coord_remove", {"index": r["n"] - 1})           # [마지막 지우기]
    assert len(ex()) == n0 + 1


def _dist_xy(a, b):
    return ((a[0] - b[0]) ** 2 + (a[1] - b[1]) ** 2) ** 0.5


# ---------------------------------------------------------------------------
# [4] 자동 순회
# ---------------------------------------------------------------------------
def test_tour_visits_all_used_samples_and_ends_up(ready_ctrl):
    ctrl = ready_ctrl(tour_dwell_s=0.05)
    _found(ctrl)
    todo = ctrl.tour.targets()
    r = ctrl.run_command("tour_start")
    assert r["n"] == len(todo)
    assert wait_for(lambda: not ctrl.tour.active, 120), "순회가 끝나지 않았다"
    v = ctrl.state.get("tour")
    assert v["result"] == "완료", v
    assert [row["name"] for row in v["rows"]] == [s["name"] for s in todo]
    assert all(row["z"] is not None and abs(row["z"] - _cz(ctrl)) < 0.01 for row in v["rows"])
    assert ctrl.state.get("pos", "z") == 0.0, "끝나면 핀 맨 위"
    assert not ctrl.heatgun.problems() and not any(ctrl.state.get("relays")), "히트건 · 쿨러는 쓰지 않는다"


def test_tour_stop_midway_skips_rest_and_lifts_pin(ready_ctrl):
    ctrl = ready_ctrl(tour_dwell_s=5.0)
    _found(ctrl)
    n = len(ctrl.tour.targets())
    assert n >= 2
    ctrl.run_command("tour_start")
    assert wait_for(lambda: ctrl.state.get("tour")["step"] == "접촉 높이", 30), "첫 샘플 접촉 높이에 닿지 않았다"
    with pytest.raises(Rejected, match="순회 중"):
        ctrl.run_command("jog", {"axis": "z", "mm": -1.0})            # 순회 중에는 손으로 하는 이동을 막는다
    with pytest.raises(Rejected, match="순회 중"):
        ctrl.update_settings({"tour_dwell_s": 1.0})
    ctrl.run_command("tour_stop")
    assert wait_for(lambda: not ctrl.tour.active, 30)
    v = ctrl.state.get("tour")
    assert v["result"] == "정지" and len(v["rows"]) < n
    assert ctrl.state.get("pos", "z") == 0.0, "정지하면 핀을 맨 위로 올린 뒤 끝낸다"


def test_tour_rejected_when_heater_on_or_no_contact_height(ready_ctrl):
    ctrl = ready_ctrl()
    _found(ctrl)
    ctrl.run_command("heater_on")
    with pytest.raises(Rejected, match="히트건"):
        ctrl.run_command("tour_start")
    ctrl.run_command("heater_off_now")
    ctrl._remember(contact_z_mm=None)
    with pytest.raises(Rejected, match="접촉 높이 필요"):
        ctrl.run_command("tour_start")
    assert not ctrl.tour.active
