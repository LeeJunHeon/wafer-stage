"""K2450 실제 전압 · 과전압 보호 · 접촉 때 2단자 값(2026-11-26 실장비: 같은 샘플 0.1 V 에서 4단자 15.9 µA(6.3 kΩ) / 2단자 0.129 µA(778 kΩ) -
감지 전압을 맞추려고 FORCE 출력을 크게 올린 상태로 보였다 · 프로그램은 실제 전압을 읽지 않아 알 수 없었다).

  - 읽기 :READ? "defbuffer1", READ, SOUR, SOURSTAT → 전류 · 실제 전압(형식 거절이면 그 연결 동안 :READ?)
  - 측정 과전압 보호(meter_ovp): 설정 전압 × 2 이상인 가장 낮은 단계
  - 실제 전압 어긋남(meter_volt_tol): 점은 그대로 기록 · 샘플마다 주의 한 번 · 어긋난 점 수 · control.csv 끝 두 열(전류 · 전압)
  - 고정 높이 접촉 뒤 2단자도 기록(contact_pair_read) · four_ok 주의 · 수동 [2·4단자 비교](read_pair)
시험용 장비 lab.sense_frac = 감지 두 핀이 FORCE 전압 중 보는 비율(None = 정상)."""

import json

import pytest

from app import results
from app.devices import meters
from app.jobs import run as run_mod
from app.jobs.run import DONE
from tests.conftest import alarm_lines, wait_for
from tests.fakes import fake_visa

VOLT, ILIM = 0.1, 0.1
K_I, K_V = run_mod.CONTROL_HEADER.index("전류(A)"), run_mod.CONTROL_HEADER.index("전압(V)")


def _meter(monkeypatch, ohm=1e5):
    lab = fake_visa.Lab()
    lab.read_dt = 0.0
    box = {"ohm": ohm}
    lab.resistance = lambda i="under": box["ohm"]
    monkeypatch.setattr(meters, "RM_FACTORY", lambda: fake_visa.FakeRM(lab))
    dev = meters.K2450("GPIB0::18::INSTR")
    dev.open()
    return lab, dev, box


def _spy_q(monkeypatch, dev):
    sent = []
    q = dev.query

    def w(cmd):
        sent.append(cmd)
        return q(cmd)
    monkeypatch.setattr(dev, "query", w)
    return sent


# ---------------------------------------------------------------------------
# 읽기 형식
# ---------------------------------------------------------------------------
def test_new_read_format_gives_current_and_voltage(monkeypatch):
    lab, dev, box = _meter(monkeypatch)
    sent = _spy_q(monkeypatch, dev)
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    cur, trip = dev.read_point()
    assert cur == pytest.approx(VOLT / box["ohm"], rel=1e-6) and not trip
    assert dev.last_volt == pytest.approx(VOLT) and dev._vread is True
    assert meters.READ_IV in sent and ":READ?" not in sent, sent
    dev.output(False)
    r = dev.measure_once(VOLT, ILIM, True, 1.0, 1)
    assert r["volt_meas"] == pytest.approx(VOLT) and r["ovp"] is False and r["ohm"] == pytest.approx(box["ohm"], rel=1e-6), r


def test_rejected_read_format_falls_back_to_plain_read(monkeypatch):
    lab, dev, box = _meter(monkeypatch)
    lab.fault["no_vread"] = True                                     # 장비가 그 형식에 응답하지 않는다(시간 초과)
    warns = []
    monkeypatch.setattr(meters, "WARN", warns.append)
    sent = _spy_q(monkeypatch, dev)
    dev.configure(VOLT, ILIM, True, 1.0, 1)
    dev.start()
    for _ in range(3):
        cur, trip = dev.read_point()
        assert cur == pytest.approx(VOLT / box["ohm"], rel=1e-6) and not trip and dev.last_volt is None
    assert dev._vread is False and len(warns) == 1 and warns[0].startswith("K2450 전압 되읽기 형식 거절 · 전류만 읽음"), warns
    assert sent.count(meters.READ_IV) == 1, "거절된 뒤로는 그 연결 동안 :READ? 만"
    assert dev.drain_errors() == [], "거절 때 오류 큐를 비웠다"
    dev.close()
    dev.open()                                                       # 다시 연결하면 다시 본다
    assert dev._vread is None


def test_read_format_reject_does_not_stop_a_run(ready_ctrl):
    ctrl = ready_ctrl(**HOLD_KW)
    ctrl.lab.fault["no_vread"] = True
    it = _hold_one(ctrl)
    assert it["status"] == DONE, (it["status"], it["reason"])
    assert any("K2450 전압 되읽기 형식 거절" in ln for ln in ctrl.logs.recent)
    rows = _rows(ctrl, it)
    assert rows and all(r[K_V] == "" and r[K_I] for r in rows), rows[:2]


# ---------------------------------------------------------------------------
# 과전압 보호
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("volt, level", [(0.1, "PROT2"), (1.0, "PROT2"), (2.0, "PROT5"), (20.0, "PROT40"), (100.0, "PROT180")])
def test_ovp_level(volt, level):
    assert meters.ovp_level(volt) == level


@pytest.mark.parametrize("ovp", [True, False])
def test_configure_sends_ovp_before_output(monkeypatch, ovp):
    lab, dev, _box = _meter(monkeypatch)
    sent = []
    w = dev.write
    monkeypatch.setattr(dev, "write", lambda c: (sent.append(c), w(c))[1])
    dev.configure(2.0, ILIM, True, 1.0, 1, ovp=ovp)
    prot = [c for c in sent if c.startswith(":SOUR:VOLT:PROT ")]
    if ovp:
        assert prot == [":SOUR:VOLT:PROT PROT5"] and sent.index(prot[0]) < sent.index(":SOUR:VOLT:ILIM 0.1"), sent
        assert lab.prot_v == 5.0
    else:
        assert prot == [] and lab.prot_v is None
    assert not any(c.startswith(":OUTP") for c in sent), "설정은 출력을 켜기 전"


# ---------------------------------------------------------------------------
# 측정: 실제 전압 어긋남 · control.csv 두 열 · 접촉 때 2단자
# ---------------------------------------------------------------------------
HOLD_KW = dict(time_scale=200.0, run_heater_use=False, run_hold_s=5.0, run_interval_s=0.5, temp_read_s=0.1, meter_avg_count=1, meter_nplc=0.1)


def _hold_one(ctrl, start=True):
    w = ctrl.world
    ctrl.run_command("samples_find")
    x0, y0 = w.sample_stage(1)
    sid = min(ctrl.prep.samples, key=lambda s: abs(s["xy"][0] - x0) + abs(s["xy"][1] - y0))["id"]
    for s in ctrl.prep.samples:
        if s["use"] != (s["id"] == sid):
            ctrl.run_command("sample_use", {"id": s["id"], "use": s["id"] == sid})
    if not start:
        return sid
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 90)
    return ctrl.runner.items[sid]


def _rows(ctrl, it):
    lines = (ctrl.runner.folder / (it["name"] + "_0.10V_control.csv")).read_text(encoding="utf-8-sig").splitlines()
    assert lines[0].split(",") == list(run_mod.CONTROL_HEADER)
    return [r.split(",") for r in lines[1:]]


def _volt_warns(ctrl):
    return [ln for ln in ctrl.logs.recent if "실제 전압 " in ln and "저항 값 신뢰 불가" in ln]


def test_floating_sense_heating_run_records_points_and_warns_once(ready_ctrl):
    ctrl = ready_ctrl(time_scale=200.0, contact_check="off")
    ctrl.lab.sense_frac = 0.01                                       # 감지 핀이 FORCE 전압의 1 % 만 본다 → FORCE 가 보호 단계(2 V)까지 오른다
    it = _hold_one(ctrl)
    run = ctrl.runner
    assert it["status"] == DONE, (it["status"], it["reason"])
    rows = _rows(ctrl, it)
    n = len(it["points"])
    assert n > 20 and len(rows) == n, "점은 그대로 기록"
    assert len(_volt_warns(ctrl)) == 1, _volt_warns(ctrl)
    assert "과전압 보호 걸림" in _volt_warns(ctrl)[0], _volt_warns(ctrl)
    for r in rows:
        cur, vm = float(r[K_I]), float(r[K_V])
        assert vm == pytest.approx(0.02, rel=1e-3), r                # FORCE 2 V × 1 %
        assert float(r[4]) == pytest.approx(VOLT / abs(cur), rel=1e-5), "저항 계산식은 그대로(설정 전압 ÷ 전류)"
    assert it["volt_bad_n"] == n and ("실제 전압 어긋남 · %d점" % n) in it["notes"], it["notes"]
    meta = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert meta["samples"][0]["volt_bad_n"] == n and meta["cond"]["meter_ovp"] is True
    import csv
    with open(run.folder / "summary.csv", encoding="utf-8-sig", newline="") as f:
        head, row = list(csv.reader(f))[:2]
    assert row[head.index("실제 전압 어긋난 점 수")] == str(n)
    assert any("실제 전압" in a for a in alarm_lines(ctrl)), alarm_lines(ctrl)
    ct = it["contact"]                                               # 고정 높이 접촉: 4단자 다음 2단자 - 감지 핀이 떠 비율이 작다
    assert ct["r4"] is not None and ct["r2"] is not None and ct["ratio"] < 0.1 and ct["four_ok"] is False, ct
    assert any(x.startswith("4단자 값 이상 · 2단자의 ") for x in it["notes"]), it["notes"]
    assert any("4단자 값이 2단자의 " in ln and "감지 핀(SENSE) 접촉 · 배선 확인" in ln for ln in ctrl.logs.recent)


def test_normal_sense_no_warn_and_voltage_matches(ready_ctrl):
    ctrl = ready_ctrl(**HOLD_KW)
    it = _hold_one(ctrl)
    assert it["status"] == DONE, (it["status"], it["reason"])
    assert _volt_warns(ctrl) == [] and not it.get("volt_bad_n")
    rows = _rows(ctrl, it)
    assert rows and all(float(r[K_V]) == pytest.approx(VOLT, rel=1e-3) for r in rows), rows[:2]
    ct = it["contact"]
    assert ct["four_ok"] is True and ct["r4"] < ct["r2"] and not any(x.startswith("4단자 값 이상") for x in it["notes"]), ct
    assert any("샘플 %s · 접촉 · 고정 높이 Z " % it["name"] in ln and " · 4단자 " in ln and " · 2단자 " in ln and " · 비율 " in ln for ln in ctrl.logs.recent)


def test_pair_read_off_measures_only_once(ready_ctrl):
    ctrl = ready_ctrl(contact_pair_read=False, **HOLD_KW)
    it = _hold_one(ctrl)
    assert it["status"] == DONE and "r2" not in it["contact"] and "four_ok" not in it["contact"], it["contact"]


def test_old_condition_without_new_keys_uses_defaults(ready_ctrl, monkeypatch):
    keys = ("meter_ovp", "meter_volt_tol", "contact_pair_read")
    monkeypatch.setattr(run_mod, "COND_KEYS", tuple(k for k in run_mod.COND_KEYS if k not in keys))
    ctrl = ready_ctrl(**HOLD_KW)
    it = _hold_one(ctrl)
    assert it["status"] == DONE and not any(k in ctrl.runner.cond for k in keys)
    assert ctrl.lab.prot_v == 2.0 and "r2" in it["contact"], "없는 키는 기본값(과전압 보호 켬 · 2단자도 기록)"


# ---------------------------------------------------------------------------
# 수동 측정 · [2·4단자 비교]
# ---------------------------------------------------------------------------
def _at_sample(make_ctrl):
    ctrl = make_ctrl(scale=0.02)
    ctrl.lab.read_dt = 0.002
    x, y = ctrl.world.sample_stage(4)                                # 표면 13.4 mm
    ctrl.goto(x, y)
    ctrl.contact_fixed(13.4, ctrl.safety.generation, read=False)
    return ctrl


def test_manual_read_logs_current_and_real_voltage(make_ctrl):
    ctrl = _at_sample(make_ctrl)
    r = ctrl.run_command("read_resistance")
    assert r["volt_meas"] == pytest.approx(VOLT) and r["volt_bad"] is False
    st = ctrl.state.get("resistance")
    assert st["volt_meas"] == pytest.approx(VOLT) and st["volt_bad"] is False
    line = [ln for ln in ctrl.logs.recent if "수동 측정 · 저항 " in ln][-1]
    assert " · 전류 " in line and " · 실제 0.1000 V · " in line and "[info]" in line, line
    ctrl.lab.sense_frac = 0.01
    r = ctrl.run_command("read_resistance")
    assert r["volt_bad"] is True and r["ovp"] is True
    line = [ln for ln in ctrl.logs.recent if "수동 측정 · 저항 " in ln][-1]
    assert "[warn]" in line and "설정 전압 못 맞춤" in line and "과전압 보호 걸림" in line, line


def test_read_pair_returns_both_and_logs(make_ctrl):
    ctrl = _at_sample(make_ctrl)
    r = ctrl.run_command("read_pair")
    assert set(r) == {"r4", "r2", "ratio", "i4", "i2", "v4", "v2", "ovp", "force_v", "ok"}
    assert r["ok"] is True and r["r4"] < r["r2"] and r["ratio"] == pytest.approx(r["r4"] / r["r2"])
    assert r["force_v"] == pytest.approx(abs(r["i4"]) * r["r2"]) and r["v4"] == pytest.approx(VOLT) and r["v2"] == pytest.approx(VOLT)
    line = [ln for ln in ctrl.logs.recent if "2·4단자 비교" in ln][-1]
    assert "[info]" in line and " · 4단자 " in line and " · 2단자 " in line and " · 비율 " in line and "FORCE 쪽 약 " in line, line
    ctrl.lab.sense_frac = 0.01
    r = ctrl.run_command("read_pair")
    assert r["ok"] is False and r["ovp"] is True
    line = [ln for ln in ctrl.logs.recent if "2·4단자 비교" in ln][-1]
    assert "[warn]" in line and "4단자 값 이상" in line and "과전압 보호 걸림" in line, line


def test_read_pair_refused_during_run(ready_ctrl):
    from app.safety import Rejected
    ctrl = ready_ctrl(**dict(HOLD_KW, run_hold_s=60.0))
    _hold_one(ctrl, start=False)
    ctrl.run_command("run_start")
    try:
        with pytest.raises(Rejected):
            ctrl.run_command("read_pair")
    finally:
        ctrl.run_command("run_stop")
        assert wait_for(lambda: not ctrl.runner.active, 60)


# ---------------------------------------------------------------------------
# 예전 control.csv(열 11개)도 읽힌다
# ---------------------------------------------------------------------------
def test_old_control_csv_with_11_columns_is_read(tmp_path):
    head = "회차,단계,경과(s),온도(°C),저항(Ω),목표 온도(°C),열선 출력(%),송풍,온도 1(°C),온도 2(°C),한도"
    rows = ["1,기록,%.2f,2.5000000E+1,1.0000000E+5,,,0,25.000,25.000,0" % t for t in (0.0, 0.5, 1.0)]
    (tmp_path / "1-1_0.10V_control.csv").write_text("﻿" + "\r\n".join([head] + rows) + "\r\n", encoding="utf-8")
    pts = results.hold_points(tmp_path, {"file": "1-1_0.10V_0.dat"})
    assert pts == [[0.0, 1e5, 2], [0.5, 1e5, 2], [1.0, 1e5, 2]]
