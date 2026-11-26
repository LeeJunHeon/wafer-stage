"""열선 출력 계산(PowerControl): 표에서 읽는 자리 = 목표 + τ × 목표선 기울기 · 앞먹임 = hold(그 자리) · PI 이득 = gain(그 자리).
예전 hold(목표) + τ × 기울기 ÷ gain(목표) 는 표 구간 경계(53.5 · 76.8 °C)에서 출력이 14.8 → 27.9 % · 33.9 → 58.7 % 로 뛰어 온도가 +7 °C 넘게 튀었다(2026-11-26 기록)."""

import numpy as np
import pytest

from app.jobs import heater as H

CURVE = "0:27.8 10:53.5 20:59.6 30:76.8 40:79.6 50:83.8 60:92.7 90:93.3"
CD = {"heat_model_k": 2.567, "heat_model_tau_s": 17.0, "heat_model_theta_s": 4.5, "heat_curve": CURVE, "heater_tc_s": 5.0, "heater_pi_filter_s": 8.0,
      "heater_max_pct": 100, "heater_lead_c": 3.0, "heater_lead_filter_s": 3.0, "heater_lead_hold_s": 5.0, "heater_jump_c": 4.0, "heater_turn_s": 15.0}
TAU = 17.0
SLOPE = 0.392                                      # 23.53 °C/분


def _pc(cd=CD, T_amb=27.8):
    return H.PowerControl(dict(cd), T_amb)


@pytest.mark.parametrize("s", [SLOPE, 0.0, -SLOPE])
@pytest.mark.parametrize("sp", [45.0, 53.4, 53.6, 60.0, 76.7, 76.9, 84.0])
def test_ff_reads_table_at_target_plus_tau_slope(sp, s):
    pc = _pc()
    assert pc.at(sp, s) == pytest.approx(sp + TAU * s)
    assert pc.ff(sp, s) == pytest.approx(pc.m.hold(sp + TAU * s))


@pytest.mark.parametrize("sp", [30.0, 53.5, 70.0, 85.0])
def test_zero_slope_is_hold_of_target(sp):
    pc = _pc()
    assert pc.ff(sp, 0.0) == pytest.approx(pc.m.hold(sp))


def test_no_jump_at_table_edges():
    pc = _pc()
    sps = np.arange(44.0, 85.0 + 1e-9, 0.05)
    ffs = [pc.ff(float(sp), SLOPE) for sp in sps]
    d = max(abs(b - a) for a, b in zip(ffs, ffs[1:]))
    assert d < 0.5, d


def test_pi_gain_read_at_same_place():
    pc = _pc()
    pc.out(49.0, 50.0, SLOPE, 0.0)
    m = pc.m
    assert pc.kc == pytest.approx(pc.q * m.gains(K=m.gain(50.0 + TAU * SLOPE))[0])
    assert pc.ti == pytest.approx(m.gains(K=m.gain(50.0 + TAU * SLOPE))[1])


@pytest.mark.parametrize("s", [SLOPE, 0.0, -SLOPE])
def test_no_table_keeps_old_formula(s):
    cd = {k: v for k, v in CD.items() if k != "heat_curve"}
    pc = _pc(cd, T_amb=27.8)
    assert not pc.m.has_curve
    for sp in (40.0, 60.0, 80.0):
        assert pc.ff(sp, s) == pytest.approx(((sp - 27.8) + TAU * s) / 2.567)


class _Heater:
    def __init__(self):
        self.q = 1.0
        self.boot_hold = 0
        self.pct = 0.0

    def power(self, p):
        self.pct = float(p)


def _tss(u, pts):
    """판의 정지 온도: 표 직선 보간(출력 → 온도) · 마지막 점 위는 마지막 구간 기울기로 연장."""
    ps = [p for p, _ in pts]
    ts = [t for _, t in pts]
    if u >= ps[-1]:
        return ts[-1] + (ts[-1] - ts[-2]) / (ps[-1] - ps[-2]) * (u - ps[-1])
    return float(np.interp(max(0.0, u), ps, ts))


def test_closed_loop_follows_line_without_jumps():
    """판 = 표 그대로(τ dT/dt = Tss(u(t − θ)) − T · 0.5 s 간격) · 43.9 °C 에서(그 온도를 유지하는 출력으로) 85 °C 까지 23.53 °C/분."""
    from app import config as config_mod
    pts = config_mod.heat_curve(CURVE)
    dt, theta, T0 = 0.5, 4.5, 43.9
    h = _Heater()
    lc = H.LineControl(h, dict(CD), T0, 85.0, +1, 0.0, 23.53, T_amb=27.8)
    u0 = lc.pc.m.hold(T0)
    h.pct = u0
    lag = int(round(theta / dt))
    hist = [u0] * lag                                   # 지연 θ 동안의 지난 출력
    T, t = T0, 0.0
    errs, dus = [], []
    last = None
    while T < 85.0 and t < 600.0:
        sp = lc.step(T, t)
        errs.append(abs(T - sp))
        if last is not None and sp < 85.0:
            dus.append(abs(h.pct - last))
        last = h.pct
        hist.append(h.pct)
        u_eff = hist.pop(0)
        T += dt * (_tss(u_eff, pts) - T) / TAU
        t += dt
    assert T >= 85.0, (T, t)
    assert max(errs) < 3.5, max(errs)
    assert max(dus) < 8.0, max(dus)
    assert len(lc.jumps) == 0, lc.jumps
    assert lc.waits == 0, lc.waits


# ---------------------------------------------------------------------------
# 온도 급변: 적분 보정은 배율이 실제로 바뀐 때만 지운다(표가 없거나 배율이 한계면 적분이 계속 쌓여야 한다 - 아니면 출력이 멈춰 "온도 상승 없음")
# ---------------------------------------------------------------------------
def _gap_run(cd, q=1.0, gap=8.0, secs=60.0, dt=0.5, T0=40.0):
    """온도를 목표보다 gap °C 낮게 고정한 채 LineControl 을 secs 초 돌린다. 반환 (heater, lc, 첫 급변 직후 출력 %, [(급변 전 q, 뒤 q, 뒤 J)])."""
    h = _Heater()
    h.q = q
    lc = H.LineControl(h, dict(cd), T0, 85.0, +1, 0.0, 23.53, T_amb=27.8)
    rec = []
    orig = lc._jump_step

    def spy(d, now):
        q0 = lc.pc.q
        orig(d, now)
        rec.append((q0, lc.pc.q, lc.pc.J))
    lc._jump_step = spy
    p_jump = None
    t = 0.0
    while t <= secs + 1e-9:
        lc.step(lc.sp - gap, t)
        if p_jump is None and lc.jumps:
            p_jump = h.pct
        t += dt
    return h, lc, p_jump, rec


def test_jump_without_table_keeps_integrating():
    cd = {k: v for k, v in CD.items() if k != "heat_curve"}
    h, lc, p_jump, rec = _gap_run(cd)
    assert lc.jumps and rec and p_jump is not None, "급변 판정이 걸려야 한다"
    assert all(q1 == q0 == 1.0 for q0, q1, _J in rec), "표가 없으면 배율 1 고정"
    assert all(J != 0.0 for _q0, _q1, J in rec), "배율이 안 바뀌면 적분 보정을 지우지 않는다"
    assert h.pct >= p_jump + 5.0, "적분이 쌓여 출력이 커진다 · 급변 직후 %.1f %% → 끝 %.1f %%" % (p_jump, h.pct)


def test_jump_at_q_max_keeps_integrating():
    h, lc, p_jump, rec = _gap_run(CD, q=H.Q_MAX)
    p_ref = p_jump if p_jump is not None else 0.0
    assert all(q1 == q0 for q0, q1, _J in rec), "배율이 한계면 그대로"
    assert h.pct >= 100.0 - 0.05 or h.pct >= p_ref + 5.0, "출력이 계속 커진다 · 급변 직후 %.1f %% → 끝 %.1f %%" % (p_ref, h.pct)


def test_jump_with_table_scales_q_and_clears_integral():
    _h, lc, _p, rec = _gap_run(CD, q=1.0)
    assert rec, "급변 판정이 걸려야 한다"
    q0, q1, J = rec[0]
    assert q1 == pytest.approx(min(H.Q_MAX, q0 * H.JUMP_UP)) and q1 > q0, rec[0]
    assert J == 0.0, "배율이 바뀌면 적분 보정을 지운다(그 몫을 배율이 한다)"
