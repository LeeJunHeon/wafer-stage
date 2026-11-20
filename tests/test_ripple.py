"""흔들림(analysis.period_scan · analysis.ripple)과 속도 정하는 방법 "걸리는 시간"(config.heat_rate_c_min · cool_rate_c_min · point_step_c) - 순수 함수만."""

import math

import pytest

from app import config as config_mod
from app.jobs import analysis


def _wave(period, amp, slope=0.0, base=30.0, dt=0.5, n=241):
    return [(i * dt, base + slope * i * dt + amp * math.sin(2 * math.pi * i * dt / period)) for i in range(n)]


def test_period_scan_finds_period_and_amp():
    t = [i * 0.5 for i in range(121)]
    r = [0.5 * math.cos(2 * math.pi * x / 7.0) for x in t]
    P, amp = analysis.period_scan(t, r, 2.0, 30.0)
    assert P == pytest.approx(7.0, abs=0.2)
    assert amp == pytest.approx(0.5, abs=0.05)


def test_period_scan_empty_range():
    assert analysis.period_scan([0, 1, 2], [0, 1, 0], 5.0, 4.0) == (None, None)


def test_ripple_line_plus_sine():
    rp = analysis.ripple(_wave(10.0, 1.0, slope=0.2))
    assert rp["period_s"] == pytest.approx(10.0, abs=0.4)
    assert rp["amp"] == pytest.approx(1.0, abs=0.15)
    assert rp["sd"] == pytest.approx(1.0 / math.sqrt(2), abs=0.12)
    assert rp["pp"] == pytest.approx(2.0, abs=0.3)


def test_ripple_pure_line_is_flat():
    rp = analysis.ripple(_wave(10.0, 0.0, slope=0.3))
    assert rp["sd"] < 1e-6 and rp["pp"] < 1e-6


def test_ripple_too_few_points_or_short():
    none = {"sd": None, "pp": None, "period_s": None, "amp": None}
    assert analysis.ripple(_wave(10.0, 1.0, n=9)) == none                 # 점 10개 미만
    assert analysis.ripple(_wave(10.0, 1.0, dt=0.5, n=30)) == none        # 구간 14.5 s < 20 s


def test_curve_stats_has_ripple_keys():
    rows = [(t, T, None) for t, T in _wave(10.0, 1.0, slope=0.2)]
    cs = analysis.curve_stats(rows, 0.0, 200.0, 1, 0.3)
    for k in ("rip_sd", "rip_pp", "rip_period_s", "rip_amp"):
        assert cs[k] is not None


def _cfg(**kw):
    v = config_mod.defaults()
    v.update(kw)
    return v


def test_rate_by_time():
    v = _cfg(run_rate_by="time", run_start_c=25.0, run_heater_off_c=85.0, run_cool_end_c=35.0, run_heat_time_s=120.0, run_cool_time_s=100.0,
             cool_rate_separate=True, run_cool_rate_c_min=3.0, temp_test_interval_s=0.5)
    assert config_mod.heat_rate_c_min(v) == pytest.approx(30.0)
    assert config_mod.cool_rate_c_min(v) == pytest.approx(30.0)          # 냉각 속도 따로 · 냉각 속도는 쓰지 않는다
    assert config_mod.point_step_c(v, "temp_test_interval_s") == pytest.approx(0.25)
    assert config_mod.heat_time_s(v) == pytest.approx(120.0 + float(v["run_heat_hold_s"]))
    assert config_mod.cool_time_s(v) == pytest.approx(100.0)


def test_rate_by_step_unchanged():
    v = _cfg(run_rate_by="step", run_point_step_c=0.27, run_interval_s=0.9, cool_rate_separate=False)
    assert config_mod.heat_rate_c_min(v) == pytest.approx(0.27 / 0.9 * 60.0)
    assert config_mod.cool_rate_c_min(v) == pytest.approx(0.27 / 0.9 * 60.0)
    assert config_mod.point_step_c(v) == 0.27
