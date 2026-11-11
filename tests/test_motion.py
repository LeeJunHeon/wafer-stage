"""이동: 자동 X · Y 전 Z 맨 위 확인 · 가동 범위 · 조그 나눠 보내기 · 보드 재시작 · 연결 규칙."""

import pytest

from app.devices.stage import Stage, StageError, move_time_s, parse_rs, parse_st
from app.safety import Rejected, Safety
from app.config import Config
from app.state import State
from tests.conftest import wait_for


def idx(log, prefix, start=0):
    return next(i for i in range(start, len(log)) if log[i].startswith(prefix))


def test_goto_raises_z_first_and_confirms_with_st(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    fw.set_z_mm(10.0)
    ctrl.stage.read_status()
    n = len(fw.log)
    ctrl.goto(50.0, 40.0)
    log = fw.log[n:]
    iz, ix, iy = idx(log, "gz 0"), idx(log, "gx"), idx(log, "gy")
    assert iz < ix < iy
    assert any(ln == "st" for ln in log[iz + 1:ix]), "gz 0 뒤 X 이동 전에 st 로 Z=0 을 확인해야 한다"
    assert abs(ctrl.state.get("pos", "x") - 50.0) < 0.01 and abs(ctrl.state.get("pos", "y") - 40.0) < 0.01


def test_goto_does_not_move_xy_when_z_top_not_confirmed(make_ctrl, monkeypatch):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    fw.set_z_mm(10.0)
    ctrl.stage.read_status()
    real_goto = ctrl.stage.goto
    monkeypatch.setattr(ctrl.stage, "goto", lambda ax, p, g: None if ax == "z" else real_goto(ax, p, g))   # Z 가 올라가지 않았다
    n = len(fw.log)
    with pytest.raises(Rejected) as e:
        ctrl.goto(50.0, 40.0)
    assert "맨 위" in str(e.value)
    assert not any(ln.startswith(("gx", "gy")) for ln in fw.log[n:])


def test_jog_does_not_raise_z_automatically(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    fw.set_z_mm(10.0)
    ctrl.stage.read_status()
    n = len(fw.log)
    ctrl.jog("x", 2.0)
    assert not any(ln.startswith(("gz", "jz")) for ln in fw.log[n:])


def test_target_range_refused_only_for_homed_axes(make_ctrl):
    ctrl = make_ctrl()
    with pytest.raises(Rejected) as e:
        ctrl.goto(300.0, 10.0)
    assert "가동 범위" in str(e.value)
    with pytest.raises(Rejected):
        ctrl.jog("x", 300.0)
    s = Safety(Config(), State())
    assert s.reject_target("x", 300.0, homed=False) is None          # 원점 없는 축은 막지 않는다
    assert s.reject_target("x", 300.0, homed=True) is not None
    assert s.reject_target("z", 44.9, homed=True) is None
    assert s.reject_target("z", 45.1, homed=True) is not None


def test_long_jog_is_split_into_8000_pulse_pieces(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    n = len(fw.log)
    ctrl.jog("x", 100.0)                                           # 16000 펄스
    jx = [ln for ln in fw.log[n:] if ln.startswith("jx")]
    assert jx == ["jx 8000", "jx 8000"]


def test_origin_here_and_end_save_afterwards(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    n = len(fw.log)
    ctrl.set_origin("z", "here")
    log = fw.log[n:]
    assert idx(log, "zz") < idx(log, "save")
    n = len(fw.log)
    ctrl.set_origin("x", "end")
    log = fw.log[n:]
    assert idx(log, "fz x") < idx(log, "save")
    assert ctrl.state.get("homed", "x") is True


def test_board_reboot_locks_and_resends_watchdog(make_ctrl):
    ctrl = make_ctrl()
    fw = ctrl.fake_fw
    ctrl.relay_on(2)
    fw.reboot()
    assert wait_for(lambda: ctrl.state.get("estop_locked"), 5)
    assert wait_for(lambda: ctrl.state.has_alarm("reboot"), 5)
    assert wait_for(lambda: fw.wd_ms == ctrl.cfg["watchdog_ms"], 5), "재시작 뒤 wd 를 다시 보내야 한다"
    with pytest.raises(Rejected):
        ctrl.jog("x", 1.0)


def test_connect_refuses_non_v11_firmware():
    class Stub:
        def __init__(self):
            self.lines = [b"=== 3-AXIS V10 ===\n"]
            self.in_waiting = 0

        def readline(self):
            return self.lines.pop(0) if self.lines else b""

        def write(self, d):
            return len(d)

        def flush(self):
            pass

        def close(self):
            pass

    st = Stage(lambda name: Stub())
    with pytest.raises(StageError) as e:
        st.connect("COM9", banner_wait_s=1.0)
    assert "V11" in str(e.value) and not st.connected


def test_connect_refuses_when_no_banner():
    class Silent:
        in_waiting = 0

        def readline(self):
            return b""

        def write(self, d):
            return len(d)

        def flush(self):
            pass

        def close(self):
            pass

    st = Stage(lambda name: Silent())
    with pytest.raises(StageError):
        st.connect("COM9", banner_wait_s=0.5)


def test_only_the_chosen_port_is_opened():
    opened = []

    def opener(name):
        opened.append(name)
        raise OSError("없음")

    st = Stage(opener)
    with pytest.raises(StageError):
        st.connect("COM5")
    assert opened == ["COM5"]


def test_pure_parsers_and_move_time():
    st = parse_st("ST X=160 Y=320 X2=160 Z=0 HX=1 HY=0 HZ=1 DIRTY=1")
    assert st == {"x": 160, "y": 320, "z": 0, "hx": True, "hy": False, "hz": True, "dirty": True}
    assert parse_rs("RS R1=1 R2=0 R3=0 R4=0 R5=0 R6=0 R7=0 R8=0 R9=0 R10=1")[0] is True
    assert move_time_s(16000, 6000, 40000, 300) == pytest.approx(2.0 * (6000 - 300) / 40000 + (16000 - 2 * (6000 ** 2 - 300 ** 2) / 80000) / 6000)
    assert move_time_s(0, 6000, 40000, 300) == 0.0
    with pytest.raises(StageError):
        parse_st("ST X=1")
