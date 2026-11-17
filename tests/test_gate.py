"""측정 시작 조건(필요 항목) 화면 표시가 입력이 바뀐 그 순간 맞는지.

runner.gate() 는 늘 맞지만, 화면에 보내는 state.run.gate 는 예전에는 1초 주기 publish 와 일부 명령 뒤에만 다시 계산돼 최대 1초 늦었다.
지금은 입력이 바뀌는 길(state 의 devices · homed · estop_locked · cfg / calib 저장 / prep.pos_block)이 Controller.gate_inputs_changed 한 곳으로 모인다.
아래 시험은 모두 바꾼 직후 기다리지 않고 비교한다.
"""

from app.state import State
from tests.conftest import FakeClock


def _same(ctrl, what):
    shown, real = ctrl.state.get("run", "gate"), ctrl.runner.gate()
    assert shown == real, "%s 직후 화면 %s · 실제 %s" % (what, shown, real)
    return [g["text"] for g in real]


def test_gate_follows_device_connections_at_once(make_ctrl):
    ctrl = make_ctrl(connect=False)
    assert "스테이지 연결" in _same(ctrl, "시작")
    ctrl.connect_stage()
    assert "스테이지 연결" not in _same(ctrl, "스테이지 연결")
    ctrl.connect_meter("k2110")
    ctrl.connect_meter("k2450")
    assert not {"온도계 연결", "저항계 연결"} & set(_same(ctrl, "측정 장비 연결"))
    ctrl.disconnect_meter("k2110")
    assert "온도계 연결" in _same(ctrl, "온도계 끊기")
    ctrl.connect_meter("k2110")
    ctrl.state.set_device("k2450", "오류", "통신 끊김", ctrl.cfg["k2450_addr"])     # 장비 오류(읽기 실패 등)도 같은 길
    assert "저항계 연결" in _same(ctrl, "저항계 오류")
    ctrl.disconnect_stage()
    assert "스테이지 연결" in _same(ctrl, "스테이지 끊기")


def test_gate_follows_origin_estop_and_contact_height_at_once(make_ctrl):
    clock = FakeClock()
    ctrl = make_ctrl(clock=clock)
    ctrl.stage.call("forget", None, lambda ln: "기록 삭제" in ln, 3.0)     # 세 축 원점을 지운다(시험용 보드에 바로 - 화면은 이 명령을 보낼 수 없다)
    ctrl.stage.read_status(None)
    assert "세 축 원점" in _same(ctrl, "원점 지움")
    ctrl.set_origin("all", "here")
    assert "세 축 원점" not in _same(ctrl, "원점 등록")
    ctrl.estop("시험")
    assert "비상정지 해제" in _same(ctrl, "비상정지")
    clock.advance(2.0)
    ctrl.estop_release()
    assert "비상정지 해제" not in _same(ctrl, "비상정지 해제")
    assert "접촉 높이" in _same(ctrl, "시작")
    ctrl.jog("z", 12.0)
    ctrl.contact_save_here()
    assert "접촉 높이" not in _same(ctrl, "접촉 높이 저장")
    ctrl._remember(contact_z_mm=None)                                # 설정(전체 설정 · 조건 창)으로 바뀌어도
    assert "접촉 높이" in _same(ctrl, "접촉 높이 지움")


def test_gate_follows_plate_coords_and_head_check_at_once(ready_ctrl):
    ctrl = ready_ctrl()
    assert _same(ctrl, "초기 설정 완료") == []
    ctrl.run_command("coord_clear")
    assert "좌표 보정" in _same(ctrl, "좌표 초기화")
    ctrl.prep.pos_block = True                                       # 헤드 위치 확인 경보(10 mm 넘음)
    assert "헤드 위치 확인" in _same(ctrl, "헤드 위치 경보")
    ctrl.prep.clear_pos_check()
    assert "헤드 위치 확인" not in _same(ctrl, "위치 확인 풀림")
    ctrl.calib.d["plate"] = None                                     # 판 영역을 다시 고르는 중 - 저장되는 순간 다시 계산한다
    ctrl.calib.save()
    assert "판 영역" in _same(ctrl, "판 영역 지움")


def test_state_watch_runs_once_per_real_change_outside_the_lock():
    s = State()
    seen = []
    s.watch(("homed", "devices"), lambda: seen.append(s._lock._is_owned()))
    s.set("homed", "x", True)
    s.set("homed", "x", True)                                        # 같은 값이면 부르지 않는다
    s.set_device("stage", "연결", "", "COM5")
    s.set_device("stage", "연결", "", "COM5")
    s.set("pos", "x", 1.0)                                           # 보지 않는 이름
    s.update("devices", **{})
    assert seen == [False, False], "바뀐 두 번만 · 상태 잠금 밖에서"
