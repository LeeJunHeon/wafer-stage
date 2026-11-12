"""이동 정지(motion_stop · /api/stop): 이동만 멈추고 잠그지 않는다(비상정지와 다르다).
긴 이동 중 정지 → 목표 전에 멈춤 · 위치가 st 와 같음 · 잠기지 않음 · 바로 다음 조그 됨 / 여러 걸음 이동의 남은 걸음이 안 감 /
히트건 · 쿨러 · K2450 출력은 그대로 / 멈춘 상태에서 정지 → ok / 측정 중 정지 → 거절 / 접촉 탐색 중 정지 → 출력 꺼짐 · 핀 그 자리 /
명령 줄을 거치지 않음(/api/stop)."""

import threading

import pytest
from fastapi.testclient import TestClient

from app.safety import Rejected
from app.server import create_app
from tests.conftest import wait_for

PPMM = 160


def _move_in_thread(fn):
    """이동 명령을 다른 스레드에서 시작한다. 거절 글은 out["msg"] 에 남는다."""
    out = {}

    def run():
        try:
            out["res"] = fn()
        except Rejected as e:
            out["msg"] = str(e)

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _fw_mm(ctrl):
    fw = ctrl.fake_fw
    return fw.px / PPMM, fw.py / PPMM, fw.pz / PPMM


def test_stop_during_long_goto_halts_before_target_and_does_not_lock(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    t, out = _move_in_thread(lambda: ctrl.goto(200.0, 10.0))
    assert wait_for(lambda: ctrl.stage.moving, 5), "이동이 시작되지 않았다"
    n_bang = len(fw.bang_times)
    r = ctrl.run_command("motion_stop")
    t.join(10)
    assert len(fw.bang_times) == n_bang + 1, "'!' 가 바로 나가야 한다"
    assert fw.cuts and fw.cuts[-1]["out"] < fw.cuts[-1]["cmd"], "이동이 중간에 끊겨야 한다"
    assert out.get("msg") == "정지로 취소됨"
    # 목표 전에 멈췄고, 화면의 위치는 st 로 다시 읽은 값(= 펌웨어 카운터)과 같다
    x = ctrl.state.get("pos", "x")
    assert 0.0 < x < 200.0 and r["pos"]["x"] == x
    assert (x, ctrl.state.get("pos", "y"), ctrl.state.get("pos", "z")) == tuple(round(v, 3) for v in _fw_mm(ctrl))
    assert ctrl.state.get("move") is None and not ctrl.stage.moving
    # 잠기지 않는다: 경보 없음 · 원점 그대로 · 바로 다음 조그가 된다
    assert ctrl.state.get("estop_locked") is False and r["locked"] is False and ctrl.state.get("alarms") == []
    assert all(ctrl.state.get("homed", a) for a in "xyz")
    ctrl.jog("x", 1.0)
    assert abs(ctrl.state.get("pos", "x") - (x + 1.0)) < 0.01
    assert [ln for ln in ctrl.logs.recent if "이동 정지 (화면) · '!' 전송 · " in ln and ln.rstrip().endswith("ms")]


def test_stop_drops_remaining_steps_and_pending_jog(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    # 여러 걸음 이동(X → Y): X 를 가는 중에 정지하면 Y 는 가지 않는다
    t, out = _move_in_thread(lambda: ctrl.goto(150.0, 120.0))
    assert wait_for(lambda: ctrl.stage.moving and any(c.startswith("gx") for c in fw.log), 5)
    n = len(fw.log)
    ctrl.motion_stop("시험")
    t.join(10)
    assert out.get("msg") == "정지로 취소됨"
    assert not any(c.startswith(("gx", "gy", "gz", "jx", "jy", "jz")) for c in fw.log[n:]), "남은 걸음(gy)은 나가지 않는다: %s" % fw.log[n:]
    assert ctrl.state.get("pos", "y") == 0.0
    # 대기 칸에 받아 둔 조그도 버린다
    t, out = _move_in_thread(lambda: ctrl.jog("y", 100.0))
    assert wait_for(lambda: ctrl.stage.moving, 5)
    assert ctrl.jog("y", 50.0) == {"queued": True}
    assert ctrl.state.get("move", "pending") == {"axis": "y", "mm": 50.0}
    n = len(fw.log)
    ctrl.motion_stop("시험")
    t.join(10)
    assert not any(c.startswith("jy") for c in fw.log[n:]), "대기 조그와 남은 조각은 나가지 않는다: %s" % fw.log[n:]
    assert 0.0 < ctrl.state.get("pos", "y") < 100.0 and ctrl.state.get("move") is None
    assert ctrl.state.get("estop_locked") is False


def test_stop_leaves_heater_coolers_and_k2450_output_as_they_are(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    ctrl.relay_on(1)                                                 # 히트건(수동)
    ctrl.relay_on(3)                                                 # 쿨러
    ctrl.k2450.output(True)
    since = ctrl.state.get("heater_on_since")
    n = len(fw.log)
    t, out = _move_in_thread(lambda: ctrl.jog("z", 30.0))            # 히트건이 켜져 있어도 Z 는 움직인다
    assert wait_for(lambda: ctrl.stage.moving, 5)
    ctrl.motion_stop("시험")
    t.join(10)
    assert 0.0 < ctrl.state.get("pos", "z") < 30.0
    assert fw.relay[0] is True and fw.relay[2] is True, "히트건 · 쿨러는 그대로 켜져 있다"
    assert ctrl.state.get("heater_on_since") == since and ctrl.state.get("relays")[0] is True
    assert ctrl.lab.out_on is True, "K2450 출력도 그대로"
    assert not any(c.startswith("r ") for c in fw.log[n:]), "정지는 릴레이 명령을 보내지 않는다: %s" % fw.log[n:]
    assert ctrl.state.get("estop_locked") is False and ctrl.state.get("alarms") == []


def test_stop_when_idle_is_ok_and_changes_nothing(make_ctrl):
    ctrl = make_ctrl()
    ctrl.goto(20.0, 30.0)
    pos = dict(ctrl.state.get("pos"))
    for _ in range(2):
        r = ctrl.run_command("motion_stop")
        assert r["pos"] == pos and r["locked"] is False
    assert dict(ctrl.state.get("pos")) == pos and ctrl.state.get("alarms") == []
    assert all(ctrl.state.get("homed", a) for a in "xyz")
    ctrl.jog("x", -1.0)                                              # 그 뒤에도 바로 움직인다
    assert abs(ctrl.state.get("pos", "x") - 19.0) < 0.01
    # 스테이지가 연결돼 있지 않으면 거절(버튼도 꺼져 있다)
    ctrl.disconnect_stage()
    with pytest.raises(Rejected) as e:
        ctrl.run_command("motion_stop")
    assert str(e.value) == "스테이지 연결 안 됨"


def test_stop_is_refused_during_a_run(ready_ctrl):
    ctrl = ready_ctrl(time_scale=60.0)
    ctrl.run_command("samples_find")
    for s in [s for s in ctrl.prep.samples if s["use"]][1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    ctrl.run_command("run_start")
    assert wait_for(lambda: ctrl.runner.stage == 3, 20)
    n = len(ctrl.fake_fw.bang_times)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("motion_stop")
    assert str(e.value) == "측정 중 · 측정 중지 또는 비상정지 사용"
    with TestClient(create_app(ctrl)) as c:
        assert c.post("/api/stop").json() == {"ok": False, "msg": "측정 중 · 측정 중지 또는 비상정지 사용"}
        assert len(ctrl.fake_fw.bang_times) == n and ctrl.runner.active, "측정은 그대로 간다"
        ctrl.run_command("run_stop")
        assert wait_for(lambda: not ctrl.runner.active, 30)


def test_stop_during_contact_search_turns_output_off_and_keeps_pin(ready_ctrl):
    ctrl = ready_ctrl()
    w = ctrl.world
    x, y = w.sample_stage(4)
    surf = w.sample_surface_z(4)
    ctrl.goto(x, y, surf - 3.0)
    ctrl.lab.read_dt = 0.05                                          # 탐색이 몇 초 걸리게
    t, out = _move_in_thread(lambda: ctrl.run_command("contact_find"))
    assert wait_for(lambda: ctrl.lab.out_on and ctrl.prep.contact["active"], 5)
    ctrl.run_command("motion_stop")
    t.join(10)
    assert out.get("msg") == "정지로 취소됨"
    assert ctrl.lab.out_on is False, "탐색을 끝내고 출력을 끈다"
    assert ctrl.prep.contact["active"] is False
    z = ctrl.state.get("pos", "z")
    assert surf - 3.0 <= z < surf, "핀은 그 자리에 둔다(맨 위로 올리지 않는다)"
    assert ctrl.state.get("estop_locked") is False
    ctrl.jog("z", -1.0)                                              # 정지 직후 바로 다시 움직인다
    assert abs(ctrl.state.get("pos", "z") - (z - 1.0)) < 0.01


def test_stop_endpoint_does_not_wait_behind_moves(make_ctrl):
    """/api/stop 은 명령 줄을 거치지 않는다: 이동 명령이 돌고 있어도 바로 나가고, 결과(위치)를 돌려준다. WS · /api/cmd 의 motion_stop 도 같은 길."""
    ctrl = make_ctrl(scale=0.4)
    with TestClient(create_app(ctrl)) as c:
        res = {}
        th = threading.Thread(target=lambda: res.update(c.post("/api/cmd/goto", json={"x": 200, "y": 10}).json()))
        th.start()
        assert wait_for(lambda: ctrl.stage.moving, 5)
        r = c.post("/api/stop").json()
        th.join(10)
        assert r["ok"] is True and 0.0 < r["result"]["pos"]["x"] < 200.0 and r["result"]["locked"] is False
        assert res == {"ok": False, "msg": "정지로 취소됨"}
        assert c.get("/api/state").json()["estop_locked"] is False
        # 멈춰 있을 때 · 명령 이름으로 불러도 된다
        assert c.post("/api/stop").json()["ok"] is True
        assert c.post("/api/cmd/motion_stop", json={}).json()["ok"] is True
        with c.websocket_connect("/ws") as ws:
            ws.send_json({"id": 7, "cmd": "motion_stop"})
            for _ in range(20):
                m = ws.receive_json()
                if m.get("type") == "reply":
                    break
            assert m["id"] == 7 and m["ok"] is True
        assert c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()["ok"] is True


def test_estop_still_locks_after_stop_exists(make_ctrl):
    """비상정지 동작은 그대로: 이동 정지와 달리 잠그고 릴레이를 끈다."""
    ctrl = make_ctrl(scale=0.4)
    ctrl.relay_on(3)
    t, out = _move_in_thread(lambda: ctrl.goto(200.0, 10.0))
    assert wait_for(lambda: ctrl.stage.moving, 5)
    ctrl.estop("시험")
    t.join(10)
    assert out.get("msg") == "이동 중단 · 위치 확인 필요"
    assert ctrl.state.get("estop_locked") is True and not any(ctrl.fake_fw.relay)
    with pytest.raises(Rejected) as e:
        ctrl.jog("x", 1.0)
    assert str(e.value) == "비상정지 상태"
