"""장비 연결의 선택 목록(초기 설정 1) · 설정 설명 · 폴더 선택.
목록은 프로그램을 실행한 컴퓨터의 것: 포트 · 측정 장비는 목록만 읽고 열지 않는다. 카메라는 0~3 번을 잠깐 열어 보고 열리는 것만 넣되,
연결 중인 카메라 번호는 열어 보지 않고 그대로 넣는다. [새로고침] 은 뒤에서 읽고 결과를 state.devlist 로 보낸다."""

import os
import time

import cv2
import pytest
from fastapi.testclient import TestClient

from app import config as config_mod
from app.control import port_label
from app.safety import Rejected
from app.server import create_app
from tests.conftest import wait_for
from tests.fakes.fake_camera import FakeCapture


class Opener:
    """카메라 여는 자리에 꽂는 것: present 에 있는 번호만 열리고, 연 번호를 적어 둔다."""

    def __init__(self, present, delay=0.0):
        self.present, self.delay, self.calls = set(present), delay, []

    def __call__(self, idx, flag):
        self.calls.append(idx)
        if self.delay:
            time.sleep(self.delay)
        cap = FakeCapture()
        cap.opened = idx in self.present
        return cap


def test_port_label_shows_name_and_description():
    assert port_label("COM5", "Arduino Mega 2560 (COM5)") == "COM5 · Arduino Mega 2560"
    assert port_label("COM7", "USB-SERIAL CH340(COM7)") == "COM7 · USB-SERIAL CH340"
    assert port_label("COM4", "n/a") == "COM4" and port_label("COM3", "") == "COM3" and port_label("COM3", "COM3") == "COM3"


def test_device_lists_are_read_without_opening_ports_or_meters(make_ctrl):
    ctrl = make_ctrl(connect=False)
    op = ctrl.camera.open_capture = Opener({1, 2})
    d = ctrl.list_devices()
    assert d["ports"] == [{"value": "COM5", "label": "COM5 · Arduino Mega 2560"}]
    assert ctrl.fake_fw is None and not ctrl.stage.connected, "목록을 읽는다고 포트를 열지 않는다"
    assert [x["value"] for x in d["visa"]] == [ctrl.cfg["k2110_addr"], ctrl.cfg["k2450_addr"]] and d["note"] == ""
    assert ctrl.lab.opened == [], "목록을 읽는다고 측정 장비를 열지 않는다"
    # 카메라: 0~3 번을 열어 보고 열리는 것만 "번호 · 가로×세로"(설정의 해상도를 요청했을 때 잡히는 크기)
    assert d["cameras"] == [{"value": 1, "label": "1 · 320×240"}, {"value": 2, "label": "2 · 320×240"}]
    assert set(op.calls) == {0, 1, 2, 3} and op.calls.count(1) == 1, "열리는 번호는 한 번만 열어 본다"
    assert not ctrl.camera.connected, "열어 본 카메라는 바로 닫는다"
    line = [ln for ln in ctrl.logs.recent if "장비 목록" in ln][-1]
    assert "카메라 1 2" in line and "열기 실패 0 3" in line, "카메라 열기 실패는 프로그램 로그 한 줄로 남는다: %s" % line


def test_connected_camera_is_listed_without_being_opened_again(make_ctrl):
    ctrl = make_ctrl(camera=True)                                    # 카메라 1 번 연결됨
    assert ctrl.state.get("devices", "camera", "target") == "1"
    op = ctrl.camera.open_capture = Opener({1, 3})
    d = ctrl.list_devices()
    assert 1 not in op.calls, "연결 중인 카메라 번호는 열어 보지 않는다"
    assert d["cameras"] == [{"value": 1, "label": "1 · 320×240"}, {"value": 3, "label": "3 · 320×240"}]
    assert ctrl.camera.connected and ctrl.camera.fresh_frame()[0] is not None, "연결은 그대로"


def test_refresh_runs_in_background_and_publishes_lists(make_ctrl):
    ctrl = make_ctrl(connect=False)
    op = ctrl.camera.open_capture = Opener({1}, delay=0.05)
    assert ctrl.state.get("devlist") == {"busy": False, "t": None, "ports": [], "visa": [], "cameras": [], "note": ""}
    with TestClient(create_app(ctrl)) as c:
        t0 = time.monotonic()
        assert c.post("/api/cmd/devices_refresh", json={}).json() == {"ok": True, "result": {"busy": True}}
        assert time.monotonic() - t0 < 0.3, "읽는 동안 기다리지 않는다(뒤에서 읽는다)"
        assert c.get("/api/state").json()["devlist"]["busy"] is True
        assert c.post("/api/cmd/devices_refresh", json={}).json()["ok"] is True       # 읽는 중에 또 눌러도 한 번만 읽는다
        assert wait_for(lambda: ctrl.state.get("devlist", "busy") is False, 5)
        dl = c.get("/api/state").json()["devlist"]
    assert dl["t"] is not None and dl["ports"][0]["label"] == "COM5 · Arduino Mega 2560"
    assert [x["label"] for x in dl["cameras"]] == ["1 · 320×240"] and len(dl["visa"]) == 2
    assert op.calls.count(1) == 1, "두 번 눌러도 목록은 한 번만 읽었다"
    assert ctrl.fake_fw is None and ctrl.lab.opened == [] and not ctrl.camera.connected, "아무 장비도 열려 있지 않다"


def test_opencv_console_warnings_are_turned_off_before_cv2_loads():
    assert os.environ.get("OPENCV_LOG_LEVEL") == "OFF"               # app/__init__.py 가 cv2 보다 먼저 정한다
    assert cv2.utils.logging.getLogLevel() == cv2.utils.logging.LOG_LEVEL_SILENT


# ---- 설정 설명 · 폴더 찾아보기 ----
def test_every_setting_has_a_description_and_hint(make_ctrl):
    ctrl = make_ctrl(connect=False)
    for it in config_mod.ITEMS:
        lines = it.desc.split("\n")
        assert it.desc.strip() and 1 <= len(lines) <= 2, it.key
        assert all(ln.strip() and not ln.rstrip().endswith((".", "니다", "세요")) for ln in lines), "개조식 1~2줄: %s" % it.key
    with TestClient(create_app(ctrl)) as c:
        rows = {r["key"]: r for r in c.get("/api/settings").json()}
    assert rows["x_max_mm"]["desc"].startswith("X 축") and rows["x_max_mm"]["hint"] == "단위 mm · 기본값 247.6 · 범위 1~400"
    assert rows["auto_connect"]["hint"] == "기본값 켬" and rows["run_cooler_mode"]["hint"] == "기본값 샘플 아래 구역"
    assert rows["contact_z_mm"]["hint"] == "단위 mm · 기본값 없음 · 범위 0~100"
    assert [k for k, r in rows.items() if r["folder"]] == ["data_dir", "nas_dir"], "폴더 항목에만 [찾아보기]"


def test_pick_folder_needs_a_window_and_returns_the_chosen_path(make_ctrl):
    ctrl = make_ctrl(connect=False)
    assert ctrl.state.get("can_pick_folder") is False                # 창 없이 실행(--no-window): 버튼을 숨긴다
    with pytest.raises(Rejected) as e:
        ctrl.run_command("pick_folder", {"start": ""})
    assert str(e.value) == "폴더 선택 창 사용 불가"
    seen = []
    answer = {"path": r"D:\data\nas"}
    ctrl.set_folder_picker(lambda start: (seen.append(start), answer["path"])[1])
    assert ctrl.state.get("can_pick_folder") is True
    with TestClient(create_app(ctrl)) as c:
        assert c.post("/api/cmd/pick_folder", json={"start": r"D:\data"}).json() == {"ok": True, "result": {"path": r"D:\data\nas"}}
        answer["path"] = None                                        # 취소
        assert c.post("/api/cmd/pick_folder", json={"start": "../data_v3"}).json() == {"ok": True, "result": {"path": None}}
    assert seen[0] == r"D:\data" and os.path.isabs(seen[1]) and seen[1].endswith("data_v3"), "상대 경로는 프로그램 폴더 기준으로 바꿔 넘긴다"
    assert ctrl.cfg["nas_dir"] == "", "고르기만 하고 설정은 바꾸지 않는다(화면이 입력 칸에 넣는다)"
