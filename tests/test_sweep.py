"""[환경 맞추기] 계산(heater.fit_sweep)과 훑기 시간 검사(config.temptest_problems) - 순수 함수만."""

import math
import random

import pytest

from app import config as config_mod
from app.jobs import heater


def _tss(u):
    """휘는 정지 곡선: 0 % 30 °C · 100 % 85 °C."""
    return 30.0 + 0.8 * u - 0.0025 * u * u


def _record(tau=12.0, theta=1.0, noise=0.15, dt=0.5, seed=1):
    """0 % 10 s → 0 → 100 % 120 s 올림 → 20 s 유지 → 120 s 내림 → 0 % 20 s. 1차 지연 + 지연 θ 를 잘게 나눠 적분 · 온도에 잡음."""
    rnd = random.Random(seed)

    def u_at(t):
        if t < 10:
            return 0.0
        if t < 130:
            return 100.0 * (t - 10) / 120.0
        if t < 150:
            return 100.0
        if t < 270:
            return 100.0 - 100.0 * (t - 150) / 120.0
        return 0.0

    ts, us, Ts = [], [], []
    y, sub = _tss(0.0), 20
    n = int(290 / dt) + 1
    for i in range(n):
        t = i * dt
        ts.append(t)
        us.append(round(u_at(t), 1))
        Ts.append(y + rnd.gauss(0.0, noise))
        h = dt / sub
        for j in range(sub):
            y += h / tau * (_tss(u_at(t + j * h - theta)) - y)
    return ts, us, Ts


def test_fit_sweep_finds_model_and_curve():
    t, u, T = _record()
    fit = heater.fit_sweep(t, u, T, 100.0)
    assert fit is not None
    assert abs(fit["tau"] + fit["theta"] - 13.0) <= 2.0
    assert len(fit["pts"]) == 11
    for uk, ck in fit["pts"]:
        assert abs(ck - _tss(uk)) <= 1.5, (uk, ck, _tss(uk))
    assert fit["rms"] < 0.5


def test_fit_sweep_needs_points_and_top():
    t, u, T = _record()
    assert heater.fit_sweep(t[:59], u[:59], T[:59], 100.0) is None      # 점 60개 미만
    assert heater.fit_sweep(t, u, T, 4.0) is None                       # 최고 출력 5 % 미만


def _cfg(**kw):
    v = config_mod.defaults()
    v.update(kw)
    return v


def test_sweep_time_check():
    ok = _cfg(temp_sweep_s=120.0, temp_sweep_hold_s=20.0, heater_full_pct=90, heater_max_pct=100, heater_max_on_s=180.0)
    assert config_mod.sweep_full_s(ok) == pytest.approx(2 * 120.0 * 0.1 + 20.0)          # 1 − 90/100 = 0.0999… 라 == 는 43.99999 에서 틀린다
    assert not [p for p in config_mod.temptest_problems(ok) if p.startswith("훑기")]
    bad = _cfg(temp_sweep_s=600.0, temp_sweep_hold_s=120.0, heater_full_pct=50, heater_max_pct=100, heater_max_on_s=180.0)
    msg = [p for p in config_mod.temptest_problems(bad) if p.startswith("훑기")]
    assert msg and "720초" in msg[0]
