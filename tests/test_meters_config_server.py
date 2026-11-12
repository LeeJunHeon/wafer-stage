"""측정 장비 응답 해석 · VISA 목록 · 설정 · 서버."""

import json

import pytest
from fastapi.testclient import TestClient

from app.config import Config, ConfigError, defaults
from app.devices import meters
from app.server import create_app
from tests.conftest import wait_for


# ---- SYST:ERR? · VISA 목록 ----
@pytest.mark.parametrize("resp,code", [
    ('+0,"No error"', 0), ('+0, "No error"', 0), ('0,"No error;0;0"', 0),
    ('-113,"Undefined header"', -113), ('', None), ('abc', None),
])
def test_syst_err_parsing(resp, code):
    assert meters.error_code(resp) == code


def test_no_error_replies_are_not_reported_as_errors(make_ctrl):
    ctrl = make_ctrl()
    assert ctrl.k2110.drain_errors() == []
    assert ctrl.k2450.drain_errors() == []


def test_visa_list_drops_serial_ports(make_ctrl):
    ctrl = make_ctrl(connect=False)
    assert any(a.startswith("ASRL") for a in ctrl.lab.addrs), "가짜 목록에는 ASRL 이 들어 있다"
    items = ctrl.list_devices()["visa"]
    assert items and not any(a.upper().startswith("ASRL") for a in items)
    assert "GPIB0::18::INSTR" in items


def test_serial_address_is_never_opened_as_meter(make_ctrl):
    from app.safety import Rejected
    ctrl = make_ctrl(connect=False)
    with pytest.raises(Rejected):
        ctrl.connect_meter("k2450", "ASRL5::INSTR")
    assert not [a for a in ctrl.lab.opened if a.upper().startswith("ASRL")]


def test_meters_read_and_k2450_output_is_off_after_measure(make_ctrl):
    ctrl = make_ctrl()
    t = ctrl.read_temp()
    assert t["temp"] is not None and 0 < t["temp"] < 60
    r = ctrl.read_resistance()
    assert r["ohm"] and r["ohm"] > 1e3
    assert ctrl.lab.out_on is False
    ctrl.lab.fault["tc_open"] = True
    assert ctrl.read_temp()["temp"] is None                       # 열전대 끊김 = 값 없음


def test_missing_driver_marks_only_that_device(monkeypatch, tmp_path):
    from app.control import Controller
    cfg = Config(tmp_path / "s.json").load()
    cfg.update({"data_dir": str(tmp_path / "d")})
    ctrl = Controller(cfg)                                        # 가짜 장비를 꽂지 않은 프로그램 그대로
    monkeypatch.setattr(meters, "RM_FACTORY", None)
    monkeypatch.setattr(meters, "_rm", lambda: (_ for _ in ()).throw(meters.VisaUnavailable()))
    from app.safety import Rejected
    with pytest.raises(Rejected) as e:
        ctrl.connect_meter("k2110")
    assert "드라이버" in str(e.value)
    assert ctrl.state.get("devices", "k2110", "why") == "드라이버 없음"
    assert ctrl.state.get("devices", "stage", "state") == "끊김"      # 다른 장비는 그대로
    ctrl.shutdown()


# ---- 설정 ----
def test_defaults_without_file(tmp_path):
    cfg = Config(tmp_path / "none.json").load()
    assert cfg.values == defaults()
    assert cfg["stage_port"] == "COM5" and cfg["heater_max_c"] == 110.0 and cfg["heater_max_on_s"] == 180.0
    assert cfg["cooler_grid"] == [[2, 3, 4], [5, 6, 7], [8, 9, 10]]
    assert cfg["x_max_mm"] == 247.6 and cfg["y_max_mm"] == 247.8 and cfg["z_max_mm"] == 45.0


@pytest.mark.parametrize("key,bad", [
    ("x_max_mm", -1), ("x_max_mm", "abc"), ("heater_max_c", 1e9), ("stage_port", 5), ("meter_four_wire", "yes"),
    ("meter_avg_count", 0), ("cooler_grid", [[2, 3, 4], [5, 6, 7], [8, 9, 9]]), ("cooler_grid", [[1, 2, 3], [4, 5, 6], [7, 8, 9]]),
    ("no_such_key", 1),
])
def test_invalid_values_are_rejected(tmp_path, key, bad):
    cfg = Config(tmp_path / "s.json").load()
    before = dict(cfg.values)
    with pytest.raises(ConfigError):
        cfg.update({key: bad})
    assert cfg.values == before


def test_update_is_all_or_nothing_and_saves_roundtrip(tmp_path):
    cfg = Config(tmp_path / "s.json").load()
    with pytest.raises(ConfigError):
        cfg.update({"stage_port": "COM7", "x_max_mm": -5})
    assert cfg["stage_port"] == "COM5"
    cfg.update({"stage_port": "COM7", "heater_max_c": 90})
    cfg.save()
    again = Config(tmp_path / "s.json").load()
    assert again["stage_port"] == "COM7" and again["heater_max_c"] == 90.0


def test_old_or_broken_file_is_tolerated(tmp_path):
    p = tmp_path / "s.json"
    p.write_text(json.dumps({"stage_port": "COM8", "old_v2_key": {"a": 1}, "x_max_mm": "나쁨"}), encoding="utf-8")
    cfg = Config(p).load()
    assert cfg["stage_port"] == "COM8" and cfg["x_max_mm"] == 247.6
    assert cfg.loaded_notes and "x_max_mm" not in cfg.loaded_notes[0] and "X 가동 범위" in cfg.loaded_notes[0]
    p.write_text("{깨짐", encoding="utf-8")
    assert Config(p).load().values == defaults()


# ---- 서버 ----
def test_server_state_estop_and_rejections(make_ctrl):
    ctrl = make_ctrl(connect=False)
    with TestClient(create_app(ctrl)) as c:
        s = c.get("/api/state").json()
        for k in ("devices", "pos", "homed", "relays", "temp", "resistance", "estop_locked", "alarms", "heater_on_since"):
            assert k in s
        r = c.post("/api/cmd/jog", json={"axis": "x", "mm": 1}).json()
        assert r["ok"] is False and "연결" in r["msg"]
        assert c.post("/api/estop").json()["ok"] is True
        assert wait_for(lambda: c.get("/api/state").json()["estop_locked"], 3)
        assert "<title>" in c.get("/").text
        with c.websocket_connect("/ws") as ws:
            assert ws.receive_json()["type"] == "state"
            ws.send_json({"id": 7, "cmd": "relay_on", "args": {"n": 2}})
            while True:
                m = ws.receive_json()
                if m["type"] == "reply":
                    break
            assert m["id"] == 7 and m["ok"] is False


def test_server_connect_and_manual_commands(make_ctrl):
    ctrl = make_ctrl(connect=False)
    with TestClient(create_app(ctrl)) as c:
        assert c.post("/api/cmd/connect_stage", json={}).json()["ok"] is True
        assert c.post("/api/cmd/jog", json={"axis": "x", "mm": 2}).json()["ok"] is True
        assert abs(c.get("/api/state").json()["pos"]["x"] - 2.0) < 0.01
        r = c.post("/api/cmd/raw", json={"target": "stage", "line": "gx 100"}).json()
        assert r["ok"] is False and "이동" in r["msg"]
        r = c.post("/api/cmd/raw", json={"target": "stage", "line": "st"}).json()
        assert r["ok"] and "ST " in r["result"]["reply"]
        assert c.put("/api/settings", json={"x_max_mm": -1}).status_code == 400
