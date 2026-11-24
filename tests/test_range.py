"""K2450 전류 범위 규칙(meters.judge_read · K2450.read_point · start) - 근거 2026-11-23 실장비 visa.log.

  - 범위를 내린 직후의 한도 표시(TRIP 1)는 전류 한도가 아니다 → 한도 걸림은 전류가 실제로 한도 근처일 때만.
  - 출력을 켠 직후의 첫 값은 버린다(start).
  - 고정 범위 105 % 넘음은 장비가 9.91E37(over) 또는 105 % 에 멈춘 값 + 한도 표시(clamp) - 둘 다에서 값이 맞아야 한다.
"""

import pytest

from app import contact as contact_mod
from app.devices import meters
from app.jobs.run import DONE
from tests.conftest import wait_for
from tests.fakes import fake_visa

VOLT, ILIM = 0.1, 0.1
R = 1e-6                                                            # 판정 표의 기준 범위(1 µA)


# ---------------------------------------------------------------------------
# 판정 표
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("frac, kind, new", [
    (0.049, "down", 1e-7), (0.05, "ok", R), (0.7, "ok", R), (0.71, "up", 1e-5),
    (0.99, "up", 1e-5), (1.0, "over", 1e-5), (None, "over", 1e-5),
])
def test_judge_read_table(frac, kind, new):
    cur = None if frac is None else frac * R
    k, n = meters.judge_read(R, cur, ILIM)
    assert k == kind and n == pytest.approx(new), (frac, k, n)


def test_judge_read_limit_and_lowest():
    top = meters.top_range(ILIM)
    assert top == pytest.approx(0.1) and meters.top_range(0.02) == pytest.approx(0.1) and meters.top_range(1e-4) == pytest.approx(1e-4)
    assert meters.judge_read(top, None, ILIM)[0] == "limit", "가장 높은 범위에서 넘침 = 진짜 한도"
    assert meters.judge_read(top, 1.0 * top, ILIM)[0] == "limit"
    assert meters.judge_read(top, 0.96 * ILIM, ILIM)[0] == "limit", "한도의 95 % 이상"
    assert meters.judge_read(top, 0.8 * top, ILIM)[0] == "ok", "가장 높은 범위에서는 올리지 않는다"
    assert meters.judge_read(1e-3, None, 1e-3) == ("limit", 1e-3)
    low = meters.RANGES[0]
    assert meters.judge_read(low, 1e-12, ILIM) == ("ok", low), "가장 낮은 범위에서는 내리지 않는다"
    assert meters.judge_read(1e-3, 1e-12, ILIM) == ("down", low), "여러 단계를 한 번에 · 가장 낮은 범위에서 멈춤"
    assert meters.judge_read(1e-3, 2e-6, ILIM) == ("down", 1e-5)


def test_judge_read_no_bounce():
    k, n = meters.judge_read(R, 0.71 * R, ILIM)
    assert k == "up" and meters.judge_read(n, 0.71 * R, ILIM) == ("ok", n), "올린 뒤 내려가지 않는다"
    k, n = meters.judge_read(R, 0.049 * R, ILIM)
    assert k == "down" and meters.judge_read(n, 0.049 * R, ILIM) == ("ok", n), "내린 뒤 올라가지 않는다"


# ---------------------------------------------------------------------------
# 가짜 장비로 읽기
# ---------------------------------------------------------------------------
def _meter(monkeypatch, over_mode="over", flag="", gain=0.0):
    lab = fake_visa.Lab()
    lab.read_dt = 0.0
    lab.over_mode, lab.first_gain = over_mode, gain
    lab.fault["range_flag"] = flag
    box = {"ohm": 1e5}
    lab.resistance = lambda i="under": box["ohm"]                   # 시료 저항을 시험이 정한다(4단자 - 리드 저항 없음)
    monkeypatch.setattr(meters, "RM_FACTORY", lambda: fake_visa.FakeRM(lab))
    dev = meters.K2450("GPIB0::18::INSTR")
    dev.open()
    return lab, dev, box


def _sweep(f):
    up, i = [], 1e-7
    while i <= 1e-2:
        up.append(i)
        i *= f
    return up + up[::-1]


MODES = [(o, fl) for o in ("over", "clamp") for fl in ("", "once", "latch")]


@pytest.mark.parametrize("over_mode, flag", MODES)
def test_read_point_follows_current_both_ways(monkeypatch, over_mode, flag):
    _lab, dev, box = _meter(monkeypatch, over_mode, flag)
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    for f in (1.3, 1.9):
        for i in _sweep(f):
            box["ohm"] = VOLT / i
            cur, trip = dev.read_point()
            assert cur is not None and cur == pytest.approx(i, rel=1e-6), (f, i, cur, dev.range)
            assert not trip, (f, i)
            assert dev.last_reads <= meters.POINT_READS, (f, i, dev.last_reads)
    dev.output(False)


@pytest.mark.parametrize("over_mode, flag", MODES)
def test_read_point_jump_1000x(monkeypatch, over_mode, flag):
    _lab, dev, box = _meter(monkeypatch, over_mode, flag)
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    for i in (1e-5, 1e-5, 1e-2, 1e-5, 3e-6, 3e-3, 3e-6):
        box["ohm"] = VOLT / i
        cur, trip = dev.read_point()
        assert cur == pytest.approx(i, rel=1e-6) and not trip and dev.last_reads <= meters.POINT_READS, (i, cur, dev.last_reads)
    dev.output(False)


@pytest.mark.parametrize("over_mode", ["over", "clamp"])
def test_short_is_metal(monkeypatch, over_mode):
    _lab, dev, box = _meter(monkeypatch, over_mode)
    box["ohm"] = 0.5
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    cur, trip = dev.read_point()
    assert trip and cur == pytest.approx(ILIM), (cur, trip, dev.range)
    dev.output(False)
    r = dev.measure_once(VOLT, ILIM, True, 1.0, 1)
    assert r["tripped"] is True
    assert contact_mod.judge_fixed(r["ohm"], 1e8, None, tripped=r["tripped"])[0] == "metal"


@pytest.mark.parametrize("flag", ["once", "latch"])
def test_range_flag_is_not_limit(monkeypatch, flag):
    """1 mA → 1 µA 로 내린 직후의 한도 표시는 전류 한도가 아니다(1 MΩ 은 닿은 샘플)."""
    _lab, dev, box = _meter(monkeypatch, "over", flag)
    box["ohm"] = 1e6
    for _ in range(2):
        r = dev.measure_once(VOLT, ILIM, True, 1.0, 1)
        assert r["tripped"] is False and r["ohm"] == pytest.approx(1e6, rel=1e-6), r
        assert contact_mod.judge_fixed(r["ohm"], 1e8, None, tripped=r["tripped"]) is None
        assert dev.last_reads <= meters.POINT_READS


@pytest.mark.parametrize("ohm", [1e6, 2e4, 3e2])
def test_first_read_after_output_on_is_dropped(monkeypatch, ohm):
    lab, dev, box = _meter(monkeypatch, "over", "", gain=0.3)
    box["ohm"] = ohm
    r = dev.measure_once(VOLT, ILIM, True, 1.0, 1)
    assert r["ohm"] == pytest.approx(ohm, rel=1e-6) and not r["tripped"], r
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    cur, _trip = dev.read_point()
    assert cur == pytest.approx(VOLT / ohm, rel=1e-6)
    dev.output(False)


# ---------------------------------------------------------------------------
# 기록 모드 한 번: 범위를 내린 직후의 한도 표시 · 켠 직후의 어긋난 값이 있어도 첫 점부터 같은 저항
# ---------------------------------------------------------------------------
def test_hold_run_first_point_true_with_flag_and_first_gain(ready_ctrl):
    ctrl = ready_ctrl(time_scale=200.0, run_heater_use=False, run_hold_s=5.0, run_interval_s=0.5, temp_read_s=0.1, meter_avg_count=1, meter_nplc=0.1)
    lab, w = ctrl.lab, ctrl.world
    lab.fault["range_flag"] = "once"
    lab.first_gain = 0.3
    k = 1                                                           # 세계 샘플 1 은 접촉 높이에서 닿는다
    ctrl.run_command("samples_find")
    x0, y0 = w.sample_stage(k)
    sid = min(ctrl.prep.samples, key=lambda s: abs(s["xy"][0] - x0) + abs(s["xy"][1] - y0))["id"]
    for s in ctrl.prep.samples:
        if s["use"] != (s["id"] == sid):
            ctrl.run_command("sample_use", {"id": s["id"], "use": s["id"] == sid})
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 60)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE, (it["status"], it["reason"], it.get("contact"))
    assert it["contact"].get("tripped") is False, it["contact"]
    true = lab.resistance(k)
    rs = [p[1] for p in it["points"]]
    assert len(rs) >= 5 and rs[0] == pytest.approx(true, rel=1e-6), (rs[:3], true)
    assert all(r == pytest.approx(rs[0], rel=1e-6) for r in rs), rs
    assert it["sum"]["hold"]["n_miss"] == 0 and it["sum"]["hold"]["n"] == len(rs)
