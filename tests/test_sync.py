"""전원 맞춤(펌웨어 V17): HZ 줄 읽기 · 시험용 보드의 hpz · hpo · hpt 응답 · [위상 찾기] 고르기(pick_phase) · [박자 보정] 계산(solve_trim)."""

import pytest

from app.devices import stage as stage_mod
from app.devices.stage import StageError
from app.jobs.temptest import pick_phase, solve_trim
from app.safety import Rejected


# ---- HZ · HP 줄 읽기 ----
def test_parse_hz_line():
    z = stage_mod.parse_hz("HZ Z=1 L=1 F=5998 A=0 E=-12 O=250 R=0 N=120")
    assert z == {"src": 1, "lock": True, "hz": 59.98, "amp": 0, "err_us": -12, "off": 250, "trim": 0, "n": 120}
    z = stage_mod.parse_hz("HZ Z=2 L=0 F=0 A=3 E=0 O=0 R=-926 N=0")
    assert z["lock"] is False and z["hz"] is None and z["amp"] == 3 and z["trim"] == -926
    with pytest.raises(StageError):
        stage_mod.parse_hz("HZ Z=1 L=1")


def test_parse_hp_sync_tail():
    h = stage_mod.parse_hp("HP P=30.5 T=1000 M=1 S=17 F=1 E=10 J=0 Z=2 L=1")
    assert h["src"] == 2 and h["lock"] is True
    h = stage_mod.parse_hp("HP P=30.5 T=1000 M=1 S=17 F=1 E=10 J=0")            # V16: Z · L 없음
    assert h["src"] is None and h["lock"] is None


# ---- 시험용 보드 V17 응답 ----
def test_fake_board_sync_commands(make_ctrl):
    c = make_ctrl(meters=False)
    st = c.stage
    assert st.firmware == "V17" and st.heat_capable
    assert st.read_sync()["src"] == 0                                           # 연결 때 설정 기본값(없음)을 보냈다
    st.heat_sync_set("src", "zc")
    assert st.heat_sync["src"] == 1 and st.heat_sync["lock"] is True
    st.heat_sync_set("off", 250)
    assert st.heat_sync["off"] == 250
    st.heat_sync_set("trim", -926)
    assert st.heat_sync["trim"] == -926
    assert "영점 모듈 250 ‰" in st.method_text() and "영점 모듈" not in st.method_text(sync=False)
    with pytest.raises(stage_mod.StageRejected):                                # 기준 사용 중에는 박자 1 · 2 거절
        st.heat_timing("detune")
    c.fake_fw.zc_present = False
    assert st.read_sync()["lock"] is False
    with pytest.raises(stage_mod.StageRejected):
        st.heat_sync_set("off", 1000)                                           # 0~999 밖
    st.heat_sync_set("src", "hum")
    assert st.heat_sync["lock"] is True and st.heat_sync["amp"] == 200
    st.heat_sync_set("src", "none")
    assert "보정 -926 ppm" in st.method_text()


def test_sync_problem_blocks_start(make_ctrl):
    c = make_ctrl(heater_sync="zc")
    assert c.sync_problem() is None
    c.fake_fw.zc_present = False
    assert c.sync_problem() == "전원 맞춤 신호 없음 · 영점 모듈(D19) 확인"
    with pytest.raises(Rejected) as e:
        c.temptest.start("test")
    assert "전원 맞춤 신호 없음" in str(e.value)


# ---- [위상 찾기] 고르기 ----
def _rows(means, sds=None):
    sds = sds or [0.3] * len(means)
    return [{"off": 100 * i, "mean_c": m, "sd_c": s} for i, (m, s) in enumerate(zip(means, sds))]


def test_pick_phase_with_edges():
    # 200~600 이 한 상태(60 °C 근처) · 나머지가 다른 상태(67 °C) → 가장 긴 구간은 200~600(5개) · 가운데 400
    # 두 구간이 5개씩으로 같으면 흔들림 평균이 작은 쪽
    rows = _rows([67, 67, 60, 60.2, 60.1, 59.9, 60, 67, 67.2, 66.9], [0.5, 0.5, 0.3, 0.3, 0.3, 0.3, 0.3, 0.5, 0.5, 0.5])
    pick, run, span = pick_phase(rows)
    assert run == [200, 300, 400, 500, 600] and pick == 400 and span == pytest.approx(7.3)


def test_pick_phase_without_edges():
    # 전체 폭 2 °C 미만 → 경계 없음 · 흔들림이 가장 작은 위상
    sds = [0.5, 0.4, 0.6, 0.2, 0.5, 0.5, 0.7, 0.3, 0.4, 0.5]
    pick, run, span = pick_phase(_rows([60, 60.5, 61, 60.2, 60.8, 61.5, 60.1, 60.9, 60.4, 60.6], sds))
    assert pick == 300 and len(run) == 10 and span < 2


def test_pick_phase_wraps_900_to_0():
    # 800 · 900 · 0 · 100 이 한 상태(4개 · 900 과 0 이 이웃) · 200~700 은 두 상태로 나뉨(3개 · 3개)
    means = [60, 60.1, 70, 70.2, 70.1, 65, 65.2, 65.1, 59.9, 60.2]
    sds = [0.2, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.3, 0.4, 0.1]
    pick, run, _ = pick_phase(_rows(means, sds))
    assert run == [800, 900, 0, 100]
    assert pick == 900                                                           # 짝수 개: 가운데 둘(900 · 0) 중 흔들림이 작은 쪽(900 0.1 · 0 0.2)


def test_pick_phase_skips_missing():
    rows = _rows([60, 61, 60.5]) + [{"off": 300, "mean_c": None, "sd_c": None}]
    pick, run, _ = pick_phase(rows)
    assert pick == 0 and 300 not in run
    assert pick_phase([]) == (None, [], None)


# ---- [박자 보정] 계산 ----
def test_solve_trim_same_sign():
    # +600 ppm 으로 박자가 빨라졌다(9.0 → 5.6 s) → 어긋남 + → 보정 −
    r = solve_trim(9.0, 5.6)
    assert r == {"drift_ppm": 926, "trim_ppm": -926}


def test_solve_trim_opposite_sign():
    # +600 ppm 으로 박자가 느려졌다(9.0 → 27 s) → 어긋남 − → 보정 +
    r = solve_trim(9.0, 27.0)
    assert r == {"drift_ppm": -926, "trim_ppm": 926}
    assert solve_trim(9.0, None) == {"drift_ppm": -926, "trim_ppm": 926}       # 두 번째에서 박자가 안 보이면(더 길다) 같은 판단


def test_solve_trim_no_period_and_limit():
    assert solve_trim(None, 5.0) is None
    assert solve_trim(0.1, 0.05)["trim_ppm"] == -3000                           # ±3000 안으로
