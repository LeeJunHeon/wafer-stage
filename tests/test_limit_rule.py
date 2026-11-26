"""K2450 범위마다 거는 전류 한도 규칙(2026-11-26 실장비 오류 5077).

  - 고정 범위 R 에서 보낼 수 있는 한도는 0.106 R ~ 1.05 R(매뉴얼 2450-901-01 "Source limits" 표 · 12-80) - 밖이면 장비가 받지 않고(한도 그대로) 오류 큐에 5077.
  - RANG 을 받은 장비는 한도를 스스로 맞춘다(1.06 R 위면 1.06 R 로 · 0.106 R 아래면 0.106 R 로 - 오류 큐에 안 남김).
  - 예전 코드는 RANG 다음에 1.06 × R 을 보내 configure 끝의 drain_errors 가 "K2450 설정 오류 · 5077" 로 멈췄다(샘플 "건너뜀 · 저항 못 읽음").
  - configure 는 *RST 직후 오류 큐를 읽고 비운다(앞선 작업이 남긴 오류로 멈추지 않게) - 끝의 drain_errors 는 이번 설정의 오류만 본다.
"""

import pytest

from app.devices import meters
from tests.fakes import fake_visa

VOLT, ILIM = 0.1, 0.1
E5077 = '5077,"Cannot change source Limit for measure range %g amps;1;0 0"'


def _meter(monkeypatch):
    lab = fake_visa.Lab()
    lab.read_dt = 0.0
    box = {"ohm": 1e5}
    lab.resistance = lambda i="under": box["ohm"]
    monkeypatch.setattr(meters, "RM_FACTORY", lambda: fake_visa.FakeRM(lab))
    dev = meters.K2450("GPIB0::18::INSTR")
    dev.open()
    return lab, dev, box


def _spy(monkeypatch, dev, hook=None):
    sent = []
    write = dev.write

    def w(cmd):
        sent.append(cmd)
        write(cmd)
        if hook is not None:
            hook(cmd)
    monkeypatch.setattr(dev, "write", w)
    return sent


def _fixed(dev, r):
    dev.write("*RST")
    dev.write(":SENS:CURR:RANG:AUTO OFF")
    dev.write(":SENS:CURR:RANG %g" % r)


# ---------------------------------------------------------------------------
# 시험용 장비가 실장비와 같은 규칙인가
# ---------------------------------------------------------------------------
def test_fake_rejects_limit_outside_fixed_range_rule(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    _fixed(dev, 1e-3)
    assert dev.drain_errors() == []
    dev.write(":SOUR:VOLT:ILIM 0.0005")
    assert lab.ilim == pytest.approx(5e-4) and dev.drain_errors() == []
    dev.write(":SOUR:VOLT:ILIM 0.00106")
    assert lab.ilim == pytest.approx(5e-4), "범위의 106 % 는 받지 않는다(한도 그대로)"
    errs = dev.drain_errors()
    assert len(errs) == 1 and errs[0].startswith("5077"), errs
    dev.write(":SOUR:VOLT:ILIM 0.00105")
    assert lab.ilim == pytest.approx(1.05e-3) and dev.drain_errors() == [], "범위의 105 % 는 받는다"
    dev.write(":SOUR:VOLT:ILIM 0.0001")
    assert lab.ilim == pytest.approx(1.05e-3), "범위의 10 % 는 받지 않는다(10.6 % 미만)"
    errs = dev.drain_errors()
    assert len(errs) == 1 and errs[0].startswith("5077"), errs


def test_fake_range_up_raises_low_limit_without_error(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    _fixed(dev, 1e-6)
    dev.write(":SOUR:VOLT:ILIM 1.05e-06")
    assert lab.ilim == pytest.approx(1.05e-6) and dev.drain_errors() == []
    dev.write(":SENS:CURR:RANG 1e-05")
    assert lab.ilim == pytest.approx(1.06e-6), "새 범위의 10.6 % 로 장비가 맞춘다"
    assert dev.drain_errors() == [], "범위를 바꿔 맞춘 한도는 오류 큐에 안 남는다"


def test_fake_cls_clears_queue_and_syst_err_reports_code(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    _fixed(dev, 1e-3)
    dev.write(":SOUR:VOLT:ILIM 0.00106")
    dev.write("*CLS")
    assert dev.drain_errors() == []
    dev.write(":SOUR:VOLT:ILIM 0.00106")
    assert dev.query("SYST:ERR?").startswith("5077,")


# ---------------------------------------------------------------------------
# K2450 드라이버
# ---------------------------------------------------------------------------
def test_configure_relimit_sends_105_percent(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    sent = _spy(monkeypatch, dev)
    dev.configure(VOLT, ILIM, True, 1.0, 1, relimit=True)
    k = sent.index(":SENS:CURR:RANG 0.001")
    assert sent[k + 1] == ":SOUR:VOLT:ILIM 0.00105", sent[k:k + 2]
    assert lab.ilim == pytest.approx(1.05e-3) and dev.ilim_now == pytest.approx(1.05e-3)
    assert meters.eff_limit(1e-3, ILIM) == pytest.approx(1.05e-3)
    assert dev.drain_errors() == []


def test_relimit_sweep_all_limits_in_rule_and_no_errors(monkeypatch):
    lab, dev, box = _meter(monkeypatch)
    sent = _spy(monkeypatch, dev)
    dev.configure(VOLT, ILIM, True, 1.0, 1, relimit=True)
    dev.start()
    n = 20
    down = [1.4e6 * (150.0 / 1.4e6) ** (k / n) for k in range(n + 1)]
    for ohm in down + down[::-1][1:]:
        box["ohm"] = ohm
        cur, trip = dev.read_point()
        assert cur is not None and cur == pytest.approx(VOLT / ohm, rel=1e-6), (ohm, cur, dev.range, lab.ilim)
        assert trip is False, (ohm, cur, dev.range, lab.ilim)
    dev.output(False)
    auto, rng, checked = True, None, 0
    for c in sent:                                                   # 보낸 모든 ILIM 이 그때 범위의 0.106 ~ 1.05 배 안(자동 범위 중에 보낸 것은 빼고)
        if c == "*RST":
            auto = True
        elif c.startswith(":SENS:CURR:RANG:AUTO"):
            auto = c.endswith("ON")
        elif c.startswith(":SENS:CURR:RANG "):
            rng = float(c.split()[-1])
        elif c.startswith(":SOUR:VOLT:ILIM ") and not auto:
            x = float(c.split()[-1])
            assert 0.106 * rng * (1 - 1e-9) <= x <= 1.05 * rng * (1 + 1e-9), (c, rng)
            checked += 1
    assert checked > 5, sent
    assert dev.drain_errors() == []


def test_configure_clears_old_errors_but_reports_new(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    dev.res.err.append(E5077 % 1e-3)                                # 앞선 작업이 남긴 오류
    dev.configure(VOLT, ILIM, True, 1.0, 1)                         # 시작 때 비우므로 멈추지 않는다
    assert dev.drain_errors() == []

    def bad(cmd):
        if cmd == ":SENS:AZER:ONCE":
            dev.res.err.append(E5077 % 1e-3)                        # 이번 설정 도중에 생긴 오류
    _spy(monkeypatch, dev, hook=bad)
    with pytest.raises(meters.VisaError):
        dev.configure(VOLT, ILIM, True, 1.0, 1)


def test_configure_relimit_off_sends_no_limit_after_range(monkeypatch):
    lab, dev, _box = _meter(monkeypatch)
    sent = _spy(monkeypatch, dev)
    dev.configure(VOLT, ILIM, True, 1.0, 1, relimit=False)
    k = sent.index(":SENS:CURR:RANG 0.001")
    assert not any(c.startswith(":SOUR:VOLT:ILIM ") for c in sent[k + 1:]), sent[k:]
    assert lab.ilim == pytest.approx(1.06e-3) and dev.ilim_now == pytest.approx(1.06e-3), "장비가 스스로 낮춘 한도"
    assert dev.drain_errors() == []
