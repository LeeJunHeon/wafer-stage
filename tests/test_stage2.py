"""2단계 보완: X · Y 끝단 밀기 전 Z · 조그 대기 칸 · 이동 중 정보 · 카메라 · 자동 연결 · 계속 읽기 · 화면 파일."""

import threading

import pytest
from fastapi.testclient import TestClient

from app.config import Config, ConfigError
from app.safety import Rejected
from app.server import create_app
from tests.conftest import wait_for


def idx(log, prefix, start=0):
    return next(i for i in range(start, len(log)) if log[i].startswith(prefix))


# ---- 1. X · Y 끝단 밀기 전 Z 맨 위 ----
@pytest.mark.parametrize("axis", ["x", "y"])
def test_end_push_raises_z_first_and_confirms(make_ctrl, axis):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    fw.set_z_mm(10.0)
    ctrl.stage.read_status()
    n = len(fw.log)
    ctrl.set_origin(axis, "end")
    log = fw.log[n:]
    iz, ifz = idx(log, "gz 0"), idx(log, "fz " + axis)
    assert iz < ifz
    assert any(ln == "st" for ln in log[iz + 1:ifz]), "gz 0 뒤 끝단 밀기 전에 st 로 Z=0 을 확인해야 한다"
    assert fw.pz == 0


def test_end_push_refused_without_z_origin(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    fw.hz = False
    ctrl.stage.read_status()
    n = len(fw.log)
    with pytest.raises(Rejected) as e:
        ctrl.set_origin("x", "end")
    assert str(e.value) == "Z 원점을 먼저 잡아 주십시오"
    assert not any(ln.startswith(("fz", "gz", "jz")) for ln in fw.log[n:])


def test_end_push_of_z_itself_does_not_need_z_top(make_ctrl):
    ctrl = make_ctrl()
    ctrl.fake_fw.hz = False
    ctrl.stage.read_status()
    ctrl.set_origin("z", "end")
    assert ctrl.state.get("homed", "z") is True


# ---- 2. 조그 대기 칸 ----
def long_jog(ctrl):
    res = []

    def run():
        try:
            res.append(ctrl.jog("x", 100.0))
        except Rejected as e:
            res.append(str(e))
    t = threading.Thread(target=run)
    t.start()
    assert wait_for(lambda: ctrl.stage.moving, 5)
    return t, res


def test_jog_during_move_is_queued_and_same_direction_adds(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    t, res = long_jog(ctrl)
    assert ctrl.jog("x", 10.0) == {"queued": True}
    assert ctrl.jog("x", 10.0) == {"queued": True}
    mv = ctrl.state.get("move")
    assert mv["pending"] == {"axis": "x", "mm": 20.0}
    t.join(20)
    assert [ln for ln in fw.log if ln.startswith("jx")] == ["jx 8000", "jx 8000", "jx 3200"], "대기 조그는 합쳐서 한 번(20 mm = 3200 펄스)"
    assert abs(ctrl.state.get("pos", "x") - 120.0) < 0.01
    assert ctrl.state.get("move") is None


def test_different_jog_replaces_pending(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    t, res = long_jog(ctrl)
    ctrl.jog("x", 10.0)
    ctrl.jog("y", 5.0)                                          # 다른 축 - 대기를 바꾼다
    ctrl.jog("x", -10.0)                                        # 또 다른 것(같은 축 반대 방향) - 또 바꾼다
    assert ctrl.state.get("move")["pending"] == {"axis": "x", "mm": -10.0}
    t.join(20)
    tail = [ln for ln in fw.log if ln.startswith("j")][2:]
    assert tail == ["jx -1600"]


def test_estop_clears_pending_jog(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    fw = ctrl.fake_fw
    t, res = long_jog(ctrl)
    ctrl.jog("x", 10.0)
    ctrl.estop()
    t.join(20)
    assert not any(ln.startswith("jx 1600") for ln in fw.log), "대기 중이던 조그가 비상정지 뒤 나가면 안 된다"
    assert ctrl.state.get("move") is None
    assert ctrl._jog_pending is None


def test_other_moves_are_still_refused_while_jogging(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    t, res = long_jog(ctrl)
    with pytest.raises(Rejected) as e:
        ctrl.goto(10, 10)
    assert "다른 이동이 진행 중" in str(e.value)
    with pytest.raises(Rejected):
        ctrl.set_origin("z", "here")
    ctrl.estop()
    t.join(20)


# ---- 3. 이동 중 정보 ----
def test_move_info_has_what_and_target_and_clears(make_ctrl):
    ctrl = make_ctrl(scale=0.4)
    res = []
    t = threading.Thread(target=lambda: res.append(ctrl.goto(120.0, 30.0)))
    t.start()
    assert wait_for(lambda: ctrl.state.get("move") is not None, 5)
    mv = ctrl.state.get("move")
    assert mv["what"] == "좌표로 가는 중" and mv["target"] == {"x": 120.0, "y": 30.0, "z": 0.0}
    t.join(20)
    assert ctrl.state.get("move") is None


# ---- 카메라 ----
def test_camera_preview_is_jpeg_and_disconnect_gives_204(make_ctrl):
    ctrl = make_ctrl(camera=True)
    assert ctrl.state.get("devices", "camera", "state") == "연결"
    assert "320×240" in ctrl.state.get("devices", "camera", "info")
    with TestClient(create_app(ctrl)) as c:
        r = c.get("/api/camera/preview.jpg")
        assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and r.content[:2] == b"\xff\xd8"
        assert ctrl.camera.last_frame().shape[:2] == (240, 320)
        ctrl.disconnect_camera()
        assert c.get("/api/camera/preview.jpg").status_code == 204


def test_camera_head_marker_follows_fake_stage(make_ctrl):
    import cv2
    ctrl = make_ctrl(camera=True, camera_width=640, camera_height=480)
    ctrl.disconnect_camera()
    ctrl.connect_camera()

    def marker_center():
        f = ctrl.camera.last_frame()
        d = cv2.aruco.ArucoDetector(cv2.aruco.getPredefinedDictionary(cv2.aruco.DICT_4X4_50))
        corners, ids, _ = d.detectMarkers(f)
        assert ids is not None and 10 in ids.flatten()
        pts = corners[list(ids.flatten()).index(10)][0]
        return pts.mean(axis=0)

    ctrl.goto(60.0, 120.0)
    ctrl.disconnect_camera()
    ctrl.connect_camera()
    a = marker_center()
    ctrl.goto(160.0, 120.0)
    ctrl.disconnect_camera()
    ctrl.connect_camera()
    b = marker_center()
    assert b[0] - a[0] > 50, "스테이지가 +X 로 가면 헤드 마커가 사진에서 오른쪽으로 움직여야 한다 (%s → %s)" % (a, b)


def test_camera_format_mismatch_raises_notice(make_ctrl):
    from sim.fake_camera import open_fake
    ctrl = make_ctrl()
    ctrl.camera.open_capture = open_fake(size_override=(160, 120))
    ctrl.connect_camera()
    al = [a for a in ctrl.state.snapshot()["alarms"] if a["code"] == "camera_format"]
    assert al and al[0]["level"] == "주의" and "160x120" in al[0]["text"].replace("×", "x")


def test_missing_camera_only_marks_camera(make_ctrl):
    from sim.fake_camera import FakeCapture
    ctrl = make_ctrl()
    cap = FakeCapture()
    cap.opened = False
    ctrl.camera.open_capture = lambda i, f: cap
    with pytest.raises(Rejected):
        ctrl.connect_camera()
    assert ctrl.state.get("devices", "camera", "state") == "오류"
    assert ctrl.state.get("devices", "stage", "state") == "연결"


# ---- 자동 연결 · 계속 읽기 · 설정 ----
def test_auto_connect_opens_saved_devices_only(make_ctrl):
    ctrl = make_ctrl(connect=False)
    ctrl.auto_connect()
    for k in ("stage", "k2110", "k2450", "camera"):
        assert ctrl.state.get("devices", k, "state") == "연결", k
    assert not [a for a in ctrl.lab.opened if a.upper().startswith("ASRL")]
    assert ctrl.lab.opened == [ctrl.cfg["k2110_addr"], ctrl.cfg["k2450_addr"]], "저장된 주소 둘만 연다(찾아다니지 않는다)"


def test_auto_connect_failure_does_not_stop_others(make_ctrl, monkeypatch):
    ctrl = make_ctrl(connect=False)

    def boom(*a, **k):
        raise Rejected("포트를 열 수 없습니다")
    monkeypatch.setattr(ctrl, "connect_stage", boom)
    ctrl.auto_connect()
    assert ctrl.state.get("devices", "k2450", "state") == "연결"
    assert ctrl.state.get("devices", "camera", "state") == "연결"


def test_auto_connect_off_does_nothing(make_ctrl):
    ctrl = make_ctrl(connect=False, auto_connect=False)
    ctrl.auto_connect()
    assert ctrl.state.get("devices", "stage", "state") == "끊김"


def test_monitor_reads_every_second_and_estop_stops_it(make_ctrl):
    ctrl = make_ctrl()
    ctrl.monitor(True)
    assert wait_for(lambda: ctrl.state.get("resistance", "ohm") is not None, 4)
    t1 = ctrl.state.get("resistance", "time")
    assert wait_for(lambda: ctrl.state.get("resistance", "time") != t1, 4), "계속 읽는 동안 값이 갱신된다"
    ctrl.estop()
    assert ctrl.state.get("monitor") is False
    with pytest.raises(Rejected):
        ctrl.monitor(True)


def test_new_settings_defaults_and_validation(tmp_path):
    cfg = Config(tmp_path / "s.json").load()
    assert cfg["camera_index"] == 1 and (cfg["camera_width"], cfg["camera_height"]) == (4080, 3060) and cfg["camera_fourcc"] == "MJPG"
    assert cfg["camera_focus_fixed"] is True and cfg["auto_connect"] is True and cfg["photo_head_xy"] is None
    cfg.update({"photo_head_xy": [12, 34.5]})
    assert cfg["photo_head_xy"] == [12.0, 34.5]
    for bad in ([1], "x", [1, "a"]):
        with pytest.raises(ConfigError):
            cfg.update({"photo_head_xy": bad})
    cfg.update({"photo_head_xy": None})


def test_state_publishes_cfg_for_the_screen(make_ctrl):
    ctrl = make_ctrl(connect=False)
    c = ctrl.state.get("cfg")
    assert c["heater_max_c"] == 110.0 and c["watchdog_ms"] == 2000 and c["cooler_grid"][0] == [2, 3, 4]
    with TestClient(create_app(ctrl)) as cl:
        assert cl.put("/api/settings", json={"heater_max_c": 100}).json()["ok"]
        assert ctrl.state.get("cfg", "heater_max_c") == 100.0


# ---- 화면 파일 ----
def test_ui_files_are_served(make_ctrl):
    ctrl = make_ctrl(connect=False)
    with TestClient(create_app(ctrl)) as c:
        page = c.get("/")
        assert page.status_code == 200 and "VO₂ 온도–저항 측정" in page.text
        for url in ("/ui/app.js", "/ui/app.css", "/ui/fonts/PretendardVariable.woff2"):
            assert c.get(url).status_code == 200, url


def test_shutdown_runs_once(make_ctrl):
    """창 닫기와 서버 종료가 둘 다 종료 처리를 불러도 한 번만 한다(로그에 '종료' 한 줄)."""
    ctrl = make_ctrl()
    ctrl.shutdown()
    ctrl.shutdown()
    assert sum(1 for ln in ctrl.logs.recent if ln.endswith("종료")) == 1
