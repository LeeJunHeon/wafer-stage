"""초기 설정 7 · 쿨러 배치: 칸 하나씩 정하기(빈 칸 포함) · 빈 칸 샘플 측정 시작 거절 · 장비 앞쪽 · 오른쪽 조합 검사 · 장비 앞 기준 보기 변환.

cooler_grid 의 뜻(판 mm 기준 3×3 · 0 행 = A–B 변 쪽 · 0 열 = A–D 변 쪽)은 그대로다. 장비 앞 기준은 화면에서 보이는 방향만 돌린다.
"""

import pytest

from app import config as config_mod
from app import geometry as geo
from app.config import Config, ConfigError
from app.safety import Rejected
from tests.conftest import SEARCH

# 이 장비(측정 PC)에서 등록한 꼭짓점 스테이지 X · Y: 사진의 A 가 장비 앞에서 앞쪽 오른쪽 모서리다
MACHINE = {"A": (159.0, 158.0), "B": (159.0, 0.0), "C": (3.0, 0.0), "D": (3.0, 157.0)}


# ---------------- 칸 하나씩 정하기 · 빈 칸 ----------------
def test_cooler_cell_sets_without_turning_on_and_same_value_clears(make_ctrl):
    ctrl = make_ctrl(meters=False)
    n0 = len(ctrl.fake_fw.log)
    r = ctrl.run_command("cooler_cell", {"row": 0, "col": 1, "n": 4})            # 쿨러를 켜지 않아도 정한다
    assert r["cooler_grid"][0] == [2, 4, 2]
    assert not [ln for ln in ctrl.fake_fw.log[n0:] if ln.startswith("r ")], "칸 정하기는 릴레이를 켜지 않는다"
    r = ctrl.run_command("cooler_cell", {"row": 0, "col": 1, "n": 4})            # 같은 값이 든 칸을 다시 누르면 비운다
    assert r["cooler_grid"][0] == [2, None, 2]
    r = ctrl.run_command("cooler_cell", {"row": 2, "col": 2, "n": None})         # [비우기]
    assert r["cooler_grid"][2] == [4, 4, None]
    r = ctrl.run_command("cooler_cell", {"row": 0, "col": 1, "n": 3})            # 빈 칸에 다시 넣기
    assert r["cooler_grid"][0] == [2, 3, 2]
    # 저장된다(다시 읽어도 같다 · 빈 칸은 null 로)
    saved = Config(ctrl.cfg.path).load()
    assert saved["cooler_grid"] == [[2, 3, 2], [3, 3, 3], [4, 4, None]] and not saved.cooler_reset
    # 단계 글: 빈 칸이 있으면 미완료 · 주의
    st7 = ctrl.prep.steps()[6]
    assert st7 == {"done": False, "warn": True, "text": "빈 칸 1개"}
    ctrl.run_command("cooler_cell", {"row": 2, "col": 2, "n": 4})
    st7 = ctrl.prep.steps()[6]
    assert st7["done"] and not st7.get("warn")


def test_cooler_cell_rejects_non_cooler_relay_and_out_of_range(make_ctrl):
    ctrl = make_ctrl(meters=False)
    for args, why in (({"row": 0, "col": 0, "n": 1}, "R1 · 쿨러 릴레이 아님"), ({"row": 0, "col": 0, "n": 10}, "R10 · 쿨러 릴레이 아님"),
                      ({"row": 3, "col": 0, "n": 2}, "칸 범위 밖")):
        with pytest.raises(Rejected) as e:
            ctrl.run_command("cooler_cell", args)
        assert str(e.value) == why
    assert ctrl.cfg["cooler_grid"] == [[2, 2, 2], [3, 3, 3], [4, 4, 4]]


def test_grid_check_takes_empty_cells(tmp_path):
    item = config_mod.BY_KEY["cooler_grid"]
    assert config_mod.check(item, [[None, 2, 2], [3, None, 3], [4, 4, None]]) == [[None, 2, 2], [3, None, 3], [4, 4, None]]
    with pytest.raises(ConfigError):
        config_mod.check(item, [[0, 2, 2], [3, 3, 3], [4, 4, 4]])
    v = dict(config_mod.defaults(), cooler_grid=[[None, 2, 2], [3, 3, 3], [4, 4, 4]])
    assert config_mod.role_problems(v) == [], "빈 칸은 쿨러 릴레이 검사에서 뺀다"
    # 빈 칸이 있는 예전 파일도 기본값으로 되돌리지 않는다
    p = tmp_path / "settings.json"
    p.write_text('{"cooler_grid": [[2, null, 2], [3, 3, 3], [4, 4, 4]]}', encoding="utf-8")
    cfg = Config(p).load()
    assert cfg["cooler_grid"] == [[2, None, 2], [3, 3, 3], [4, 4, 4]] and not cfg.cooler_reset


# ---------------- 빈 칸 샘플 측정 시작 거절 ----------------
def test_run_start_rejects_sample_in_empty_cell(ready_ctrl):
    ctrl = ready_ctrl(**SEARCH)
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    s0 = use[0]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    row, col = geo.cooler_cell(s0["plate"], ctrl.cfg["plate_size_mm"])
    ctrl.run_command("cooler_cell", {"row": row, "col": col, "n": None})
    gate = ctrl.runner.gate()
    assert {"text": "쿨러 배치 빈 칸", "step": 7} in gate
    assert ctrl.state.get("run", "gate") == gate, "화면의 시작 조건도 그 순간 같은 줄"
    with pytest.raises(Rejected) as e:
        ctrl.run_command("run_start")
    assert "쿨러 배치 빈 칸" in str(e.value)
    # 그 샘플을 빼면(빈 칸에 샘플 없음) 막지 않는다
    ctrl.run_command("sample_use", {"id": s0["id"], "use": False})
    ctrl.run_command("sample_use", {"id": use[1]["id"], "use": True})
    r1, c1 = geo.cooler_cell(use[1]["plate"], ctrl.cfg["plate_size_mm"])
    if (r1, c1) != (row, col):
        assert {"text": "쿨러 배치 빈 칸", "step": 7} not in ctrl.runner.gate()
    # 쿨러 "전체" · "사용 안 함" 은 배치를 쓰지 않으므로 빈 칸을 보지 않는다
    ctrl.run_command("sample_use", {"id": s0["id"], "use": True})
    ctrl.update_settings({"run_cooler_mode": "all"})
    assert {"text": "쿨러 배치 빈 칸", "step": 7} not in ctrl.runner.gate()


def test_empty_cell_names_only_for_zone_mode():
    v = dict(config_mod.defaults(), cooler_grid=[[None, 2, 2], [3, 3, 3], [4, 4, 4]])
    samples = [{"name": "a", "plate": [10.0, 10.0]}, {"name": "b", "plate": [80.0, 80.0]}]
    from app.jobs.run import Runner
    assert Runner.empty_cell_names(v, samples) == ["a"]
    assert Runner.empty_cell_names(dict(v, run_cooler_mode="all"), samples) == []
    assert Runner.empty_cell_names(dict(v, run_cooler_mode="none"), samples) == []


# ---------------- 장비 앞쪽 · 오른쪽 설정 ----------------
def test_front_right_defaults_and_same_axis_rejected(make_ctrl):
    d = config_mod.defaults()
    assert d["machine_front_side"] == "x_far" and d["machine_right_side"] == "y_far" and d["cooler_view"] == "front"
    assert config_mod.side_problems(d) == []
    for f, r in (("x_far", "x_motor"), ("y_far", "y_far"), ("y_motor", "y_far")):
        assert config_mod.side_problems(dict(d, machine_front_side=f, machine_right_side=r)), (f, r)
    assert config_mod.side_problems(dict(d, machine_front_side="y_motor", machine_right_side="x_far")) == []
    ctrl = make_ctrl(connect=False)
    with pytest.raises(Rejected) as e:                                          # 조합 검사(저장 때 새로 생기는 문제)
        ctrl.update_settings({"machine_right_side": "x_motor"})
    assert "같은 축" in str(e.value)
    assert ctrl.cfg["machine_right_side"] == "y_far"
    for key in ("machine_front_side", "machine_right_side", "cooler_view"):
        assert config_mod.BY_KEY[key].desc, "설명(말풍선)"


# ---------------- 장비 앞 기준 보기 변환 ----------------
def test_front_layout_this_machine_a_is_front_right():
    lay = geo.front_layout(MACHINE, "x_far", "y_far")
    assert lay["corners"] == {"A": [2, 2], "B": [2, 0], "C": [0, 0], "D": [0, 2]}, "A = 앞쪽 오른쪽 · B 앞쪽 왼쪽 · C 뒤 왼쪽 · D 뒤 오른쪽"
    # 화면 칸 → cooler_grid 칸: 사진 기준과 180° 돌아가 있다
    assert lay["cells"] == [[[2, 2], [2, 1], [2, 0]], [[1, 2], [1, 1], [1, 0]], [[0, 2], [0, 1], [0, 0]]]
    # 장비 앞 기준 화면의 앞쪽 오른쪽 칸(아래 오른쪽) = cooler_grid 의 A 칸
    assert lay["cells"][2][2] == [0, 0]


def test_front_layout_follows_settings_and_needs_four_corners():
    # 사람이 Y 모터 쪽에 서고 오른쪽이 X 먼 쪽이면: 앞 = Y 작은 쪽(B · C) · 오른쪽 = X 큰 쪽(A · B) → B 가 앞쪽 오른쪽
    lay = geo.front_layout(MACHINE, "y_motor", "x_far")
    assert lay["corners"]["B"] == [2, 2] and lay["corners"]["D"] == [0, 0]
    # 칸 하나하나가 서로 다른 cooler_grid 칸이다
    flat = [tuple(c) for row in lay["cells"] for c in row]
    assert len(set(flat)) == 9
    assert geo.front_layout({k: MACHINE[k] for k in "ABC"}, "x_far", "y_far") is None, "꼭짓점 4개 필요"
    assert geo.front_layout(MACHINE, "x_far", "x_motor") is None, "같은 축은 못 그린다"


def test_prep_view_gives_front_layout_only_with_four_corners(ready_ctrl):
    ctrl = ready_ctrl()
    cv = ctrl.prep.view()["cooler"]
    assert cv["layout"] is not None and cv["why"] == ""
    assert cv["front"] == "X 먼 쪽" and cv["back"] == "X 모터 쪽" and cv["right"] == "Y 먼 쪽" and cv["left"] == "Y 모터 쪽"
    ctrl.run_command("coord_clear")
    cv = ctrl.prep.view()["cooler"]
    assert cv["layout"] is None and cv["why"] == "장비 앞 기준은 초기 설정 5 후"
