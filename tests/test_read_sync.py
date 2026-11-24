"""읽기 방식 "동시"(run_read_mode sync): 저항(K2450)을 읽는 동안 온도(K2110)를 같이 읽고 쉬지 않고 이어서 찍는다.

  - 한 점의 주기 = max(저항 읽기 예상, 온도 횟수 × 온도 읽기 시간)(config.period_s) · 측정 간격 검사는 순차만.
  - K2450.read_point(side): 읽기 한 번마다 side.begin() · side.end() · 돌려준 읽기와 짝인 end() 값 = last_side.
  - 온도 일꾼 스레드는 측정마다 하나 · 어떤 끝에서도 남지 않는다.
실제 시간(time_scale 1)으로 돈다 - 두 장비를 동시에 읽는 시각을 보려면 배율을 걸 수 없다(배율 200 이면 1 ms 가 0.2 s).
"""

import math
import threading

import pytest

from app import config as config_mod
from app.devices import meters
from app.jobs.run import DONE
from tests.conftest import wait_for
from tests.fakes import fake_visa

T_RD, R_RD = 0.17, 0.38                                             # 2026-11-23 visa.log: 온도 READ? 0.172 s · 저항 :READ? 0.383 s


# ---------------------------------------------------------------------------
# 설정: 점 주기 · 온도 횟수 · 검사
# ---------------------------------------------------------------------------
def test_period_and_temp_n():
    v = config_mod.defaults()
    assert v["run_read_mode"] == "seq"
    assert config_mod.period_s(v) == pytest.approx(v["run_interval_s"]), "순차 = 측정 간격"
    s = dict(v, run_read_mode="sync")
    assert config_mod.sync_temp_n(s) == 2
    assert config_mod.period_s(s) == pytest.approx(0.43, abs=0.01), config_mod.period_s(s)
    assert config_mod.sync_temp_n(dict(s, meter_avg_count=5)) == 1
    assert config_mod.period_s(s, "temp_test_interval_s") == pytest.approx(v["temp_test_interval_s"]), "온도 시험은 읽기 간격 그대로"
    st = dict(s, run_rate_by="step")
    assert config_mod.heat_rate_c_min(st) == pytest.approx(float(v["run_point_step_c"]) / config_mod.period_s(s) * 60.0)
    assert config_mod.cycle_time_s(s) == pytest.approx(config_mod.period_s(s)), "동시는 측정 간격을 더하지 않는다"
    assert config_mod.expect(s)["interval"] == pytest.approx(config_mod.period_s(s))


def test_interval_check_only_in_seq():
    v = config_mod.defaults()
    need = lambda ps: [p for p in ps if p.startswith("측정 간격") and "이상 필요" in p]       # noqa: E731
    assert need(config_mod.problems(dict(v, run_interval_s=0.1))), "순차: 측정 간격이 읽기 시간보다 짧으면 거절"
    assert not need(config_mod.problems(dict(v, run_read_mode="sync", run_interval_s=0.1))), "동시: 측정 간격을 쓰지 않는다"


# ---------------------------------------------------------------------------
# K2450.read_point(side)
# ---------------------------------------------------------------------------
class _Side:
    def __init__(self):
        self.ev, self.k = [], 0

    def begin(self):
        self.ev.append("b")

    def end(self):
        self.k += 1
        self.ev.append("e")
        return self.k


def _meter(monkeypatch, ohm):
    lab = fake_visa.Lab()
    lab.read_dt = 0.0
    lab.resistance = lambda i="under": ohm
    monkeypatch.setattr(meters, "RM_FACTORY", lambda: fake_visa.FakeRM(lab))
    dev = meters.K2450("GPIB0::18::INSTR")
    dev.open()
    dev.configure(0.1, 0.1, True, 1.0, 1)
    dev.start()
    return dev


def test_read_point_side(monkeypatch):
    a = _meter(monkeypatch, 1e5)
    plain = a.read_point()
    assert a.last_side is None
    b = _meter(monkeypatch, 1e5)
    side = _Side()
    got = b.read_point(side)
    assert got == plain and got[0] == pytest.approx(1e-6, rel=1e-6), (got, plain)
    assert b.last_reads >= 2, "1 mA 에서 시작 → 범위를 내려 다시 읽음"
    assert side.ev == ["b", "e"] * b.last_reads, side.ev
    assert b.last_side == b.last_reads, "돌려준 읽기(마지막)와 짝인 end() 값"
    side = _Side()
    assert b.read_point(side)[0] == pytest.approx(1e-6, rel=1e-6) and side.ev == ["b", "e"] and b.last_side == 1


def test_read_point_side_end_on_error(monkeypatch):
    dev = _meter(monkeypatch, 1e5)

    def boom():
        raise meters.VisaError("장비 통신 끊김 · 시험")

    dev.read = boom
    side = _Side()
    with pytest.raises(meters.VisaError):
        dev.read_point(side)
    assert side.ev == ["b", "e"], "읽기가 예외여도 end"


# ---------------------------------------------------------------------------
# 기록 모드 측정(실제 시간)
# ---------------------------------------------------------------------------
def _use_world(ctrl, idxs):
    w = ctrl.world
    ctrl.run_command("samples_find")
    near = {}
    for k in idxs:
        x0, y0 = w.sample_stage(k)
        near[k] = min(ctrl.prep.samples, key=lambda s: abs(s["xy"][0] - x0) + abs(s["xy"][1] - y0))["id"]
    for s in ctrl.prep.samples:
        if s["use"] != (s["id"] in near.values()):
            ctrl.run_command("sample_use", {"id": s["id"], "use": s["id"] in near.values()})
    return [near[k] for k in idxs]


def _hold(ready_ctrl, mode="sync", hold_s=20.0, t_rd=T_RD, r_rd=R_RD, **kw):
    """세계 샘플 1(접촉 높이에서 닿음)만 고른 기록 모드 Controller. 읽기 시간은 고른 뒤에 건다(초기 설정 · 샘플 검출은 빠르게)."""
    ctrl = ready_ctrl(run_heater_use=False, run_read_mode=mode, run_hold_s=hold_s, temp_read_s=t_rd, **kw)
    (sid,) = _use_world(ctrl, [1])
    ctrl.lab.read_dt_t, ctrl.lab.read_dt_r = t_rd, r_rd
    return ctrl, sid


def _no_worker():
    return not [t for t in threading.enumerate() if t.name == "run-temp"]


def test_hold_sync_points(ready_ctrl):
    ctrl, sid = _hold(ready_ctrl)
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 90)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE, (it["status"], it["reason"])
    sy, hd = it["sum"]["sync"], it["sum"]["hold"]
    assert 0.36 <= sy["period_s"] <= 0.46, sy
    assert len(it["points"]) >= 40, len(it["points"])
    assert hd["n_miss"] == 0, hd
    true = ctrl.lab.resistance(1)
    assert all(p[1] == pytest.approx(true, rel=1e-6) for p in it["points"]), (it["points"][:3], true)
    assert sy["temp_n"] == 2 and sy["dt_max_s"] < 0.08, sy
    assert _no_worker()
    assert any("동시 읽기 · 점 주기" in ln for ln in ctrl.logs.recent), ctrl.logs.recent[-5:]


@pytest.mark.parametrize("mode", ["seq", "sync"])
def test_temperature_matches_resistance_time(ready_ctrl, mode):
    """온도가 0.4 °C/s 로 오르고 저항이 온도의 함수일 때, 기록된 (온도, 저항)이 그 함수 위에 있다(읽기 시점이 맞다)."""
    ctrl, sid = _hold(ready_ctrl, mode, hold_s=8.0)
    lab = ctrl.lab
    t0 = lab.clock()
    temp = lambda: 30.0 + 0.4 * (lab.clock() - t0)                  # noqa: E731
    lab.temp = temp
    lab.resistance = lambda i="under": 1e5 * 10.0 ** (-0.01 * (temp() - 30.0))
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 90)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE, (it["status"], it["reason"])
    pts = it["points"][2:]                                          # 첫 점은 범위를 내려 다시 읽는다(순차는 앞 온도가 이르다)
    assert len(pts) >= 6, len(it["points"])
    off = [abs(T - (30.0 - 100.0 * math.log10(R / 1e5))) for T, R, *_ in pts]
    assert max(off) < 0.03, (mode, max(off), off[:5])


def test_sync_temp_meter_error_stops(ready_ctrl):
    ctrl, sid = _hold(ready_ctrl, hold_s=60.0, t_rd=0.017, r_rd=0.038)
    ctrl.run_command("run_start")
    it = ctrl.runner.items[sid]
    assert wait_for(lambda: len(it["points"]) >= 3, 60)

    def boom():
        raise meters.VisaError("장비 통신 끊김 · 시험")

    ctrl.k2110.read_temp = boom
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert "온도계 오류" in ctrl.runner.msg, ctrl.runner.msg
    assert "온도계 오류" in it["reason"], it["reason"]
    assert not ctrl.lab.out_on, "저항계 출력 꺼짐"
    assert _no_worker()


def test_sync_thermocouple_open(ready_ctrl):
    ctrl, sid = _hold(ready_ctrl, hold_s=60.0, t_rd=0.017, r_rd=0.038)
    ctrl.run_command("run_start")
    it = ctrl.runner.items[sid]
    assert wait_for(lambda: len(it["points"]) >= 3, 60)
    ctrl.lab.fault["tc_open"] = True
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert "온도 측정 실패" in ctrl.runner.msg, ctrl.runner.msg
    assert all(abs(p[0]) < 1e3 for p in it["points"]), "온도 없는 점은 쓰지 않음"
    assert not ctrl.lab.out_on
    assert _no_worker()


@pytest.mark.parametrize("how", ["stop", "estop"])
def test_sync_worker_gone_after_stop(ready_ctrl, how):
    ctrl, sid = _hold(ready_ctrl, hold_s=60.0, t_rd=0.017, r_rd=0.038)
    ctrl.run_command("run_start")
    it = ctrl.runner.items[sid]
    assert wait_for(lambda: len(it["points"]) >= 3, 60)
    assert not _no_worker(), "동시 읽기 중에는 일꾼이 있다"
    if how == "stop":
        ctrl.run_command("run_stop")
    else:
        ctrl.estop()
    assert wait_for(lambda: not ctrl.runner.active, 60)
    assert _no_worker()
    assert not ctrl.lab.out_on
