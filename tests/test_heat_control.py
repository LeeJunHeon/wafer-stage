"""측정 중 가열 제어(app/jobs/heater.py · app/jobs/run.py _heat): 송풍을 켜 둔 채 열선을 켜고 꺼서 목표선을 따른다(run_heat_mode "ramp" 기본).
목표선 SP(t) = 가열 시작 온도 + 가열 속도 × 경과(상한 가열 끝 온도). 온도 < SP − 허용 폭 → 켬 · > SP → 끔 · 그 사이 그대로 · 최소 켜짐 · 꺼짐.
"full" = 열선 계속 켬. 유지 시간 · 냉각 중 송풍 · 반복 때 열선만 다시 켬 · 가열 무반응은 열선이 켜진 시간만 · 점마다 목표 온도 · 열선 기록."""

import json

from app.jobs.heater import element_decision, ramp_setpoint
from app.jobs.run import DONE
from tests.conftest import SEARCH, wait_for

FAST = 200.0


def _one_sample(ready_ctrl, time_scale=FAST, **cfg):
    ctrl = ready_ctrl(time_scale=time_scale, **dict(SEARCH, **cfg))
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    return ctrl, use[0]["id"]


def _finish(ctrl, timeout=60):
    assert wait_for(lambda: not ctrl.runner.active, timeout), "측정이 끝나지 않았다"


def _rows(ctrl, sid):
    """control.csv 의 줄: [회차, 단계, 경과, 온도, 저항, 목표, 열선, 송풍]."""
    it = ctrl.runner.items[sid]
    text = (ctrl.runner.folder / (it["name"] + "_1.00V_control.csv")).read_text(encoding="utf-8-sig").splitlines()
    out = []
    for r in text[1:]:
        c = r.split(",")
        out.append({"cyc": int(c[0]), "phase": c[1], "t": float(c[2]), "T": float(c[3]), "sp": float(c[5]) if c[5] else None,
                    "el": c[6] == "1", "fan": c[7] == "1"})
    return out


def _min_on_off(elog, cyc, min_on, min_off):
    """열선을 바꾼 기록(결정한 시각)으로 최소 켜짐 · 꺼짐을 본다. 마지막 줄(가열 끝에 끈 것)은 제어가 아니라 뺀다."""
    e = [x for x in elog if x[2] == cyc][:-1]
    for a, b in zip(e, e[1:]):
        if a[1] != b[1]:
            assert b[0] - a[0] >= (min_on if a[1] else min_off) - 1e-9, (a, b)
    return e


# ---------------------------------------------------------------------------
def test_setpoint_and_decision():
    assert ramp_setpoint(30.0, 3.0, 600.0, 85.0) == 60.0 and ramp_setpoint(30.0, 3.0, 5000.0, 85.0) == 85.0
    d = lambda T, on, since=10.0: element_decision(T, 50.0, 0.5, on, since, 2.0, 2.0)       # noqa: E731
    assert d(49.4, False) is True and d(50.1, True) is False, "목표 − 허용 폭 아래 켬 · 목표 위 끔"
    assert d(49.8, False) is False and d(49.8, True) is True, "그 사이는 그대로"
    assert d(50.1, True, since=1.0) is True and d(49.0, False, since=1.5) is False, "최소 켜짐 · 꺼짐 전에는 바꾸지 않는다"


def test_ramp_keeps_a_simple_plant_inside_the_band():
    """지연 없는 단순한 판(켜면 0.5 °C/s 오르고 꺼지면 0.1 °C/s 식음)을 1 초마다 제어하면: 온도가 목표선 − 허용 폭 − 한 걸음 ~ 목표선 + 한 걸음 안.
    한 걸음 = 최소 켜짐 · 꺼짐(2 초)과 판정 간격(1 초) 동안의 오르내림 - 판정은 1 초마다라 넘은 것을 알아채는 데 한 간격이 더 걸린다."""
    T, on, since, band, rate, top = 30.0, True, 0.0, 0.5, 3.0, 85.0
    up, down, dt, min_t = 0.5, 0.1, 1.0, 2.0
    worst_lo = worst_hi = 0.0
    for k in range(1, 1200):
        T += (up if on else -down) * dt
        sp = ramp_setpoint(30.0, rate, k * dt, top)
        new = element_decision(T, sp, band, on, since, min_t, min_t)
        since = 0.0 if new != on else since + dt
        on = new
        worst_lo, worst_hi = min(worst_lo, T - (sp - band)), max(worst_hi, T - sp)
    assert worst_lo >= -(down * (min_t + dt) + rate / 60.0 * dt) - 1e-9 and worst_hi <= up * (min_t + dt) + 1e-9, (worst_lo, worst_hi)


def test_default_ramp_follows_the_target_to_the_end(ready_ctrl):
    """기본 설정(3 °C/분 · 허용 폭 0.5 °C · 최소 켜짐 · 꺼짐 2 초)으로 한 샘플을 끝까지: 목표선을 따라 85 °C 에 닿는다.
    열선은 여러 번 켜고 끄며(최소 시간 지킴) 송풍은 가열 내내 켜져 있다. 점마다 목표 온도 · 열선 · 송풍을 기록한다."""
    ctrl, sid = _one_sample(ready_ctrl)
    c = ctrl.cfg
    assert (c["run_heat_mode"], c["run_heat_rate_c_min"], c["heater_band_c"], c["heater_min_on_s"], c["heater_min_off_s"]) == ("ramp", 3.0, 0.5, 2.0, 2.0)
    run = ctrl.runner
    seen = {}
    ctrl.run_command("run_start")
    assert wait_for(lambda: (run.stage == 3 and ctrl.state.get("run", "heat") and seen.update(h=ctrl.state.get("run", "heat"))) or seen, 30)
    assert seen["h"]["mode"] == "ramp" and isinstance(seen["h"]["sp"], float) and isinstance(seen["h"]["element"], bool), "화면 한 줄: 목표 온도 · 열선"
    _finish(ctrl, 120)
    it = run.items[sid]
    assert it["status"] == DONE and "가열 끝 온도 미도달" not in it["notes"]
    heat = [r for r in _rows(ctrl, sid) if r["phase"] == "가열"]
    assert all(r["fan"] for r in heat), "가열 중 송풍은 계속 켜짐"
    t0 = heat[0]["t"]
    for r in heat:                                                   # 기록한 목표 = 시작 온도 + 3 °C/분 × 경과(상한 85)
        assert abs(r["sp"] - min(85.0, heat[0]["sp"] + 3.0 * (r["t"] - t0) / 60.0)) < 0.2, r
    # 목표선을 따라간다: 시작 1 분(판이 데워지는 지연) 뒤 ~ 가열 끝 온도 1 °C 전 구간에서
    #   · 온도의 기울기(최소제곱) = 가열 속도 ± 15 % · 끝난 시각 = (85 − 시작) ÷ 속도 ± 10 %
    #   · 평균 오차(온도 − 목표) < 허용 폭 + 기록된 점 사이의 가장 큰 온도 변화(판정 간격은 컴퓨터 속도에 따라 다르다 - 고정 숫자가 아니라 기록 기준)
    #   열선을 켜고 끄는 사이의 오르내림 자체는 히트건 · 판의 지연이 정한다(시험용 히트건은 끈 뒤에도 8 초쯤 더 데운다)
    ramp = [r for r in heat if r["sp"] < 85.0 - 1.0 and r["t"] - t0 > 60.0]
    n = len(ramp)
    xs, ys = [r["t"] for r in ramp], [r["T"] for r in ramp]
    mx, my = sum(xs) / n, sum(ys) / n
    slope = sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / sum((x - mx) ** 2 for x in xs) * 60.0
    step = max(abs(b["T"] - a["T"]) for a, b in zip(heat, heat[1:]))
    mean_err = sum(r["T"] - r["sp"] for r in ramp) / n
    assert n > 100 and abs(slope - 3.0) < 0.45, "가열 기울기 %.2f °C/분" % slope
    assert abs(mean_err) < 0.5 + step, "평균 오차 %.2f °C · 허용 %.2f" % (mean_err, 0.5 + step)
    expect = (85.0 - heat[0]["sp"]) / 3.0 * 60.0
    reach = next(r["t"] for r in heat if r["T"] >= 85.0) - t0
    assert abs(reach - expect) < 0.1 * expect, "85 °C 에 닿은 시각 %.0f초 · 예상 %.0f초" % (reach, expect)
    assert sum(1 for i in range(1, len(heat)) if heat[i]["el"] != heat[i - 1]["el"]) > 10, "열선을 켜고 끈다"
    e = _min_on_off(it["element_log"], 1, 2.0, 2.0)
    assert e[0][1] == 1 and len(e) > 10
    meta = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert meta["samples"][0]["element_log"] == [list(x) for x in it["element_log"]], "열선을 바꾼 기록은 run.json 에 남는다"
    assert wait_for(lambda: not any(ctrl.fake_fw.relay), 10)


def test_full_mode_keeps_the_element_on(ready_ctrl):
    ctrl, sid = _one_sample(ready_ctrl, run_heat_mode="full")
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE
    heat = [r for r in _rows(ctrl, sid) if r["phase"] == "가열"]
    assert all(r["el"] and r["fan"] for r in heat) and all(r["sp"] == 85.0 for r in heat), "계속 가열: 열선 · 송풍 계속 켜짐 · 목표 = 가열 끝 온도"
    cmds = [x for x in ctrl.fake_fw.log[n0:] if x.startswith("r ")]
    assert cmds.count("r 10 1") == 1 and cmds.count("r 10 0") == 1 and cmds.index("r 10 1") < cmds.index("r 1 1")
    assert max(r["T"] for r in heat) >= 85.0


def test_hold_time_keeps_the_end_temperature(ready_ctrl):
    """가열 끝 온도에 닿으면 유지 시간 동안 같은 방식으로 유지한 뒤 끝."""
    ctrl, sid = _one_sample(ready_ctrl, run_heat_rate_c_min=20.0, run_heat_hold_s=120.0)
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE
    heat = [r for r in _rows(ctrl, sid) if r["phase"] == "가열"]
    first = next(i for i, r in enumerate(heat) if r["T"] >= 85.0)
    hold = heat[first:]
    assert hold[-1]["t"] - hold[0]["t"] >= 120.0 - 1e-6, "유지 시간만큼 더 가열 단계"
    assert all(r["sp"] == 85.0 for r in hold) and any(not r["el"] for r in hold) and any(r["el"] for r in hold[len(hold) // 2:]), "유지 중에도 열선을 켜고 끈다"
    late = hold[len(hold) // 2:]
    step = max(abs(b["T"] - a["T"]) for a, b in zip(heat, heat[1:]))          # 기록된 점 사이의 가장 큰 온도 변화(판정 간격은 컴퓨터 속도에 따라 다르다)
    mean = sum(r["T"] for r in late) / len(late)
    assert abs(mean - 85.0) < 0.5 + step, "가열 끝 온도 근처에 머문다 · 평균 %.2f °C · 허용 %.2f" % (mean, 0.5 + step)


def test_fan_during_cooling_setting(ready_ctrl):
    """냉각 중 송풍 켬(기본): 냉각 동안 송풍 유지 · 샘플 끝에 끔. 끔: 열선 끈 뒤 heater_fan_after_s(30 초) 에 송풍 끔."""
    for keep in (True, False):
        ctrl, sid = _one_sample(ready_ctrl, run_heat_rate_c_min=20.0, run_fan_during_cool=keep)
        ctrl.run_command("run_start")
        _finish(ctrl)
        rows = _rows(ctrl, sid)
        cool = [r for r in rows if r["phase"] == "냉각"]
        t_off = max(r["t"] for r in rows if r["phase"] == "가열")              # 마지막 가열 점 바로 뒤에 열선을 끈다
        assert not any(r["el"] for r in cool), "냉각 중 열선 꺼짐"
        if keep:
            assert all(r["fan"] for r in cool), "냉각 내내 송풍"
        else:
            gap = max(b["t"] - a["t"] for a, b in zip(rows, rows[1:]))         # 실제로 기록된 점 간격(컴퓨터 속도에 따라 다르다)
            assert all(r["fan"] for r in cool if r["t"] < t_off + 30.0 - gap), "열선 끈 뒤 30 초 동안은 송풍 유지"
            assert not any(r["fan"] for r in cool if r["t"] > t_off + 30.0 + 2 * gap), "그 뒤 송풍 끔"
        assert wait_for(lambda: not any(ctrl.fake_fw.relay), 10)
        ctrl.shutdown()


def test_repeat_turns_only_the_element_on_while_the_fan_runs(ready_ctrl):
    """반복 2회 · 냉각 중 송풍 켬: 두 번째 가열은 송풍이 켜져 있으므로 열선만 다시 켠다(켜기 순서는 처음 한 번). 인터락 오류 없음(conftest)."""
    ctrl, sid = _one_sample(ready_ctrl, run_heat_rate_c_min=20.0, run_repeat=2)
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("run_start")
    _finish(ctrl, 90)
    assert ctrl.runner.items[sid]["status"] == DONE
    cmds = [x for x in ctrl.fake_fw.log[n0:] if x.startswith("r ")]
    assert cmds.count("r 1 1") == 1 and cmds.count("r 1 0") == 1, "송풍은 샘플 처음에 한 번 켜고 끝에 한 번 끈다"
    rows = _rows(ctrl, sid)
    assert {r["cyc"] for r in rows} == {1, 2} and all(r["fan"] for r in rows), "두 회차 모두 송풍 켜짐"
    for cyc in (1, 2):
        e = _min_on_off(ctrl.runner.items[sid]["element_log"], cyc, 2.0, 2.0)
        assert e[0][1] == 1, "회차마다 열선을 다시 켠다"
    assert ctrl.heatgun.errors == []


def test_no_rise_counts_only_element_on_time(ready_ctrl):
    """가열 무반응은 열선이 켜져 있던 시간만 센다: 아주 느린 가열 속도(열선이 거의 꺼져 있다)에서 벽시계로는 10 초 넘게 0.5 °C 가 안 올라도 실패가 아니다."""
    ctrl, sid = _one_sample(ready_ctrl, run_heat_rate_c_min=0.5, run_heater_off_c=40.0, run_cool_end_c=35.0, run_no_rise_s=10.0)
    ctrl.run_command("run_start")
    _finish(ctrl, 90)
    it = ctrl.runner.items[sid]
    assert it["status"] == DONE, it["reason"]
    heat = [r for r in _rows(ctrl, sid) if r["phase"] == "가열"]
    on = sum(b["t"] - a["t"] for a, b in zip(heat, heat[1:]) if a["el"])
    assert heat[-1]["t"] - heat[0]["t"] > 3 * on, "열선은 대부분 꺼져 있었다"
