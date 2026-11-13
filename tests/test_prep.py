"""초기 설정 단계: 좌표 맞추기 · 헤드 마커 위치 확인 · 샘플 찾기 · 전기로 접촉 찾기. 가짜 세계(tests/fakes/world.py)의 참값과 비교한다."""

import math

import pytest

from app import contact as contact_mod
from app.safety import Rejected
from tests.conftest import wait_for

W, H = 1280, 960


def _dist(a, b):
    return math.hypot(a[0] - b[0], a[1] - b[1])


def _nearest(world, plate_xy):
    return min(range(len(world.samples)), key=lambda k: _dist(plate_xy, (world.samples[k]["px"], world.samples[k]["py"])))


def _manual_setup(ctrl):
    """초기 설정 3 · 5단계를 사람이 하듯이: 헤드 자리 → 사진 → 꼭짓점 4개 → 핀을 꼭짓점에 대고 [여기가 A~D]."""
    w = ctrl.world
    ctrl.goto(5.0, 125.0)
    assert ctrl.run_command("photo_head_set")["marker"] is True, "그 자리에서 헤드 마커가 보인다"
    ctrl.run_command("photo_take")
    ctrl.run_command("plate_set", {"corners": [[float(x), float(y)] for x, y in w.plate_corners_px(W, H)]})
    out = None
    for name, xy in zip("ABCD", w.corners_stage()):
        if name == "D":
            g = ctrl.calib.d_guess()
            assert _dist(g, xy) < 0.2, "A · B · C 로 계산한 D 예상 자리"
            ctrl.run_command("coord_goto_d")
            assert ctrl.state.get("pos", "z") == 0.0
        ctrl.goto(*xy)
        out = ctrl.run_command("coord_set", {"name": name})
    return out


# ---------------------------------------------------------------------------
# 좌표
# ---------------------------------------------------------------------------
def test_four_corners_make_transform(make_ctrl):
    ctrl = make_ctrl(scale=0.02, camera=True, camera_width=W, camera_height=H)
    w = ctrl.world
    out = _manual_setup(ctrl)
    assert "marker" in out, "꼭짓점 4점 뒤에 헤드 마커를 이어서 기억한다"
    v = ctrl.prep.view()
    assert v["coords"]["ready"] and v["coords"]["marker"] is not None
    q = v["coords"]["quality"]
    assert q["max_side_err"] < 0.1 and q["max_angle_err"] < 0.1 and q["warn"] is False
    T = ctrl.calib.plate_to_stage(w.size_mm)
    from app import geometry as geo
    for i, s in enumerate(w.samples):                                # 판 위 어느 점이든 스테이지 X · Y 로 0.3 mm 안
        x, y = geo.apply1(T, s["px"], s["py"])
        assert _dist((x, y), w.sample_stage(i)) < 0.3
    steps = ctrl.prep.steps()
    assert steps[2]["done"] and steps[4]["done"] and "4점" in steps[4]["text"]


def test_bad_corner_warns_but_does_not_block(make_ctrl):
    ctrl = make_ctrl(scale=0.02, camera=True, camera_width=W, camera_height=H)
    _manual_setup(ctrl)
    d = ctrl.world.corners_stage()[3]
    ctrl.goto(d[0] + 3.0, d[1] + 1.0)                                # D 를 3 mm 틀리게 다시 찍는다
    ctrl.run_command("coord_set", {"name": "D"})
    q = ctrl.prep.view()["coords"]["quality"]
    assert q["warn"] is True and q["max_side_err"] > 2.0
    assert ctrl.prep.view()["coords"]["ready"], "주의만 띄우고 막지 않는다"


def test_corners_must_be_clockwise_from_top_left(make_ctrl):
    ctrl = make_ctrl(scale=0.02, camera=True, camera_width=W, camera_height=H)
    ctrl.run_command("photo_take")
    cs = [[float(x), float(y)] for x, y in ctrl.world.plate_corners_px(W, H)]
    with pytest.raises(Rejected):
        ctrl.run_command("plate_set", {"corners": cs[::-1]})
    with pytest.raises(Rejected):
        ctrl.run_command("plate_set", {"corners": cs[:3]})
    ctrl.run_command("plate_set", {"corners": cs})
    assert ctrl.prep.jpeg("plate") is not None, "꼭짓점을 정하면 보정 사진이 나온다"


def test_correction_point_improves_fit(ready_ctrl):
    ctrl = ready_ctrl()
    w = ctrl.world
    a = w.corners_stage()[0]
    ctrl.calib.set_corner_stage("A", (a[0] + 1.5, a[1] - 1.0))       # 꼭짓점 A 를 틀리게 찍은 상황
    ctrl.run_command("samples_find")

    def errors():
        return [_dist(s["xy"], w.sample_stage(_nearest(w, s["plate"]))) for s in ctrl.prep.samples]

    before = errors()
    assert max(before) > 0.5
    s = ctrl.prep.samples[0]
    r = ctrl.run_command("plate_goto", {"x": s["plate"][0] + 2.0, "y": s["plate"][1] - 1.0})
    assert r["sample"] == s["name"], "샘플 근처를 누르면 샘플 가운데로 붙는다"
    assert ctrl.state.get("pos", "z") == 0.0
    ctrl.goto(*w.sample_stage(_nearest(w, s["plate"])))              # 사람이 조그로 맞춘 자리
    ctrl.run_command("coord_add_here")
    ex = ctrl.prep.view()["coords"]["extra"]
    assert len(ex) == 1 and ex[0]["err"] is not None
    one = errors()
    assert one[0] < before[0], "보정 점을 더하면 그 샘플 자리가 맞아진다 (%.2f → %.2f mm)" % (before[0], one[0])
    with pytest.raises(Rejected):
        ctrl.run_command("coord_add_here")                           # 누른 자리가 없으면 더하지 않는다
    for k in (2, 4, 6, 8):                                           # 쓰면서 조금씩: 점이 늘수록 전체가 맞아 간다(전체 점의 최소제곱)
        s = ctrl.prep.samples[k]
        ctrl.run_command("plate_goto", {"x": s["plate"][0], "y": s["plate"][1]})
        ctrl.goto(*w.sample_stage(_nearest(w, s["plate"])))
        ctrl.run_command("coord_add_here")
    many = errors()
    assert sum(many) < sum(before) and max(many) < max(before), "%.2f → %.2f mm" % (sum(before) / 9, sum(many) / 9)
    ctrl.run_command("coord_remove", {"index": 0})
    assert len(ctrl.prep.view()["coords"]["extra"]) == 4


def test_replate_drops_pixel_memory(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.run_command("samples_find")
    s = ctrl.prep.samples[0]
    ctrl.run_command("plate_goto", {"x": s["plate"][0], "y": s["plate"][1]})
    ctrl.run_command("coord_add_here")
    ctrl.run_command("photo_take")
    r = ctrl.run_command("plate_set", {"corners": [[float(x), float(y)] for x, y in ctrl.world.plate_corners_px(W, H)]})
    assert r["dropped"] == 1 and ctrl.calib.d["marker"] is None, "판 영역을 다시 정하면 사진 픽셀로 기억한 것은 버린다"
    assert ctrl.calib.coords_ready(), "꼭짓점에 핀을 대고 기억한 스테이지 자리는 그대로 쓴다"


# ---------------------------------------------------------------------------
# 헤드 마커 위치 확인
# ---------------------------------------------------------------------------
def _alarm(ctrl, code):
    return next((a for a in ctrl.state.get("alarms") if a["code"] == code), None)


def test_position_check_notice_and_alarm(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.run_command("photo_take")
    assert _alarm(ctrl, "pos_check") is None and ctrl.prep.pos_off_mm < 0.5
    ctrl.fake_fw.slip(3.0, 0.0)                                      # 원점이 3 mm 틀어진 상황(카운터는 그대로, 실제 헤드만 밀림)
    ctrl.goto(60.0, 60.0)
    ctrl.run_command("photo_take")
    a = _alarm(ctrl, "pos_check")
    assert a is not None and a["level"] == "주의" and 2.5 < ctrl.prep.pos_off_mm < 3.5
    assert ctrl.prep.pos_block is False
    ctrl.fake_fw.slip(9.0, 0.0)                                      # 모두 12 mm
    ctrl.goto(60.0, 60.0)
    ctrl.run_command("photo_take")
    a = _alarm(ctrl, "pos_check")
    assert a is not None and a["level"] == "경보" and 11.0 < ctrl.prep.pos_off_mm < 13.0
    assert ctrl.prep.pos_block is True, "10 mm 넘으면 측정 시작을 막는다"
    ctrl.run_command("set_origin", {"axis": "x", "mode": "here"})    # 원점을 다시 맞추면 풀린다
    assert ctrl.prep.pos_block is False and _alarm(ctrl, "pos_check") is None


def test_marker_not_visible_is_only_a_notice(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.calib.set_marker(None, None)
    ctrl._remember(photo_head_xy=[125.0, 240.0])                     # 마커가 사진 밖으로 나가는 자리
    with pytest.raises(Rejected):
        ctrl.run_command("marker_learn")
    assert ctrl.calib.d["marker"] is None
    ctrl.run_command("photo_take")
    assert ctrl.prep.pos_block is False, "마커 기억이 없으면 위치 확인을 하지 않는다(막지 않는다)"


# ---------------------------------------------------------------------------
# 샘플 찾기
# ---------------------------------------------------------------------------
def test_find_all_samples(ready_ctrl):
    ctrl = ready_ctrl()
    w = ctrl.world
    r = ctrl.run_command("samples_find")
    assert r["n"] == len(w.samples) == 9
    seen = set()
    for s in ctrl.prep.samples:
        i = _nearest(w, s["plate"])
        seen.add(i)
        assert _dist(s["plate"], (w.samples[i]["px"], w.samples[i]["py"])) < 0.5, "가운데가 0.5 mm 안"
        assert _dist(s["xy"], w.sample_stage(i)) < 0.5
        assert s["small"] == (w.samples[i]["h"] < 10.6), "핀(10 × 2 mm + 여유 0.3)이 안 들어가는 샘플만 '핀보다 작음'"
        assert s["use"] == (not s["small"]), "핀보다 작은 샘플은 기본으로 빼 둔다"
        assert len(s["outline"]) >= 4 and s["area"] > 10
    assert len(seen) == 9
    ys = [s["plate"][1] for s in ctrl.prep.samples]
    assert ys[0] <= ys[-1] and [s["name"] for s in ctrl.prep.samples][:3] == ["1-1", "1-2", "1-3"], "윗줄부터 번호를 붙인다"
    assert ctrl.prep.find_info["path"] == "빈 판 비교" and ctrl.prep.find_info["ms"] < 5000
    assert ctrl.state.get("pos", "z") == 0.0 and ctrl.prep.photo["at_head"]
    ctrl.run_command("sample_use", {"id": ctrl.prep.samples[0]["id"], "use": False})
    assert ctrl.prep.samples[0]["use"] is False


def test_empty_plate_finds_nothing(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.world.set_loaded(False)                                     # 샘플을 치운다
    assert ctrl.run_command("samples_find")["n"] == 0
    ctrl.world.set_loaded(True)
    assert ctrl.run_command("samples_find")["n"] == 9


def test_find_without_empty_plate_photo(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.calib.d["empty"] = None                                     # 빈 판 사진이 없어도 찾는다(사진 한 장으로)
    r = ctrl.run_command("samples_find")
    assert r["n"] == 9 and ctrl.prep.find_info["path"] == "사진 한 장"
    assert any("빈 판 사진 없음" in n for n in r["notes"])


def test_pin_size_setting_changes_small_flag(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.run_command("samples_find")
    assert sum(1 for s in ctrl.prep.samples if s["small"]) == 1
    ctrl.cfg.update({"pin_long_mm": 3.0})
    ctrl.prep.on_settings()
    assert sum(1 for s in ctrl.prep.samples if s["small"]) == 0


# ---------------------------------------------------------------------------
# 전기로 접촉 찾기
# ---------------------------------------------------------------------------
def _over_sample(ctrl, i, above=1.0):
    w = ctrl.world
    x, y = w.sample_stage(i)
    ctrl.goto(x, y, w.sample_surface_z(i) - above)
    return w.sample_surface_z(i)


def _max_z_sent(ctrl):
    return max((int(c.split()[1]) for c in ctrl.fake_fw.log if c.startswith("gz ")), default=0) / 160.0


def test_contact_on_sample(ready_ctrl):
    ctrl = ready_ctrl()
    surf = _over_sample(ctrl, 2)
    r = ctrl.run_command("contact_find")
    assert abs(r["first_z"] - surf) <= 0.05, "닿은 높이가 실제 표면과 0.05 mm 안"
    assert abs(r["final_z"] - (r["first_z"] + 0.05)) < 1e-6, "닿은 뒤 0.05 mm 더 누른다"
    assert r["r4"] < r["r2"] and abs(r["r4"] - ctrl.world.samples[2]["r_hi"]) < 0.02 * r["r4"]
    assert ctrl.lab.out_on is False, "끝나면 출력이 꺼져 있다"
    assert ctrl.fake_fw.v == ctrl.cfg["move_speed_pps"], "느린 속도를 되돌린다"
    ctrl.run_command("contact_save")
    assert abs(ctrl.cfg["contact_z_mm"] - r["first_z"]) < 1e-6 and ctrl.prep.steps()[5]["done"]


def test_contact_two_pins_first_needs_more_descent(ready_ctrl):
    ctrl = ready_ctrl()
    ctrl.world.samples[0]["lag4"] = 0.1                              # 감지 핀 두 개는 0.1 mm 더 내려가야 닿는다
    surf = _over_sample(ctrl, 0)
    r = ctrl.run_command("contact_find")
    assert abs(r["first_z"] - surf) <= 0.05
    assert r["final_z"] >= surf + 0.1 - 1e-6, "네 핀이 다 닿을 때까지 더 내린다"
    assert ctrl.lab.out_on is False


def test_contact_metal_plate_is_short(ready_ctrl):
    ctrl = ready_ctrl()
    w = ctrl.world
    x, y = w.plate_to_stage(20.0, 20.0)                              # 샘플이 없는 판 위
    ctrl.goto(x, y, w.plate_z - 1.0)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("contact_find")
    assert "금속 판" in str(e.value)
    assert ctrl.state.get("pos", "z") == 0.0, "실패하면 핀을 맨 위로 올린다"
    assert ctrl.lab.out_on is False
    assert ctrl.prep.contact["fail"] == contact_mod.FAIL_TEXT["short"]


def test_contact_nothing_within_search_distance(ready_ctrl):
    ctrl = ready_ctrl(contact_search_mm=1.0)
    w = ctrl.world
    x, y = w.sample_stage(1)
    z0 = w.sample_surface_z(1) - 3.0                                 # 표면이 찾기 거리(1 mm)보다 멀다
    ctrl.goto(x, y, z0)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("contact_find")
    assert "미접촉" in str(e.value)
    assert _max_z_sent(ctrl) <= z0 + 1.0 + 1e-6, "한도 아래로는 한 펄스도 내려가지 않는다"
    assert ctrl.state.get("pos", "z") == 0.0 and ctrl.lab.out_on is False


def test_contact_height_mode_never_goes_below_limit(ready_ctrl):
    ctrl = ready_ctrl()
    w = ctrl.world
    x, y = w.sample_stage(3)
    surf = w.sample_surface_z(3)
    ctrl.goto(x, y)
    saved = surf - 1.0                                               # 저장한 높이가 1 mm 높게 틀려 있다 → 한도(저장 + 0.5 mm)까지 내려도 안 닿는다
    g = ctrl.safety.generation
    with pytest.raises(contact_mod.ContactFail) as e:
        ctrl.contact_descend("height", saved, g)
    assert e.value.code == "none"
    assert _max_z_sent(ctrl) <= saved + 0.5 + 1e-6
    assert ctrl.state.get("pos", "z") == 0.0 and ctrl.lab.out_on is False
    r = ctrl.contact_descend("height", surf + 0.1, g)                # 저장한 높이가 0.1 mm 낮아도 표면에서 멈춘다
    assert abs(r["first_z"] - surf) <= 0.05 and ctrl.lab.out_on is False


def test_contact_estop_stops_descent_and_turns_output_off(ready_ctrl):
    ctrl = ready_ctrl()
    surf = _over_sample(ctrl, 4, above=3.0)
    ctrl.lab.read_dt = 0.05                                          # 찾는 데 몇 초 걸리게
    import threading
    out = {}

    def run():
        try:
            ctrl.run_command("contact_find")
        except Rejected as e:
            out["msg"] = str(e)

    th = threading.Thread(target=run)
    th.start()
    assert wait_for(lambda: ctrl.lab.out_on and ctrl.prep.contact["active"], 5)
    ctrl.estop("시험")
    th.join(timeout=10)
    assert "비상정지" in out.get("msg", "")
    assert ctrl.lab.out_on is False
    z = ctrl.state.get("pos", "z")
    assert 0.0 < z < surf, "비상정지면 핀을 움직이지 않는다(맨 위로 올리지도 않는다)"


def test_contact_needs_k2450(ready_ctrl):
    ctrl = ready_ctrl()
    _over_sample(ctrl, 0)
    ctrl.disconnect_meter("k2450")
    n = len(ctrl.fake_fw.log)
    with pytest.raises(Rejected):
        ctrl.run_command("contact_find")
    assert not [c for c in ctrl.fake_fw.log[n:] if c.startswith("gz ")], "저항계가 없으면 내리지 않는다"


def test_pin_position_can_be_drawn_on_the_plate_photo(ready_ctrl):
    """수동 측정 탭 "보정" 보기: 화면은 state.prep.coords.to_plate(스테이지 X · Y → 판 mm)로 지금 핀 자리를 판 사진 위에 그린다.
    샘플의 스테이지 자리를 이 변환으로 옮기면 그 샘플의 판 자리가 나와야 한다. 좌표를 지우면 변환도 없다."""
    ctrl = ready_ctrl()
    ctrl.run_command("samples_find")
    H = ctrl.state.get("prep", "coords", "to_plate")
    assert H is not None and len(H) == 3 and all(len(r) == 3 for r in H)

    def to_plate(x, y):
        w = H[2][0] * x + H[2][1] * y + H[2][2]
        return (H[0][0] * x + H[0][1] * y + H[0][2]) / w, (H[1][0] * x + H[1][1] * y + H[1][2]) / w

    assert len(ctrl.prep.samples) >= 9
    for s in ctrl.prep.samples:
        px, py = to_plate(*s["xy"])
        assert abs(px - s["plate"][0]) < 0.05 and abs(py - s["plate"][1]) < 0.05, (s["plate"], (px, py))
    # 판 꼭짓점 A 에 댄 자리는 판 (0, 0)
    a = ctrl.state.get("prep", "coords", "stage")["A"]
    assert max(abs(v) for v in to_plate(*a)) < 0.5
    ctrl.run_command("coord_clear")
    assert ctrl.state.get("prep", "coords", "to_plate") is None
