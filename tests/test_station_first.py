"""자동 측정: 스테이션(히트건)을 시작 전 냉각보다 먼저 켠다(Runner._station_on).

2026-11-26 실장비: 957DW 스테이션은 켠 직후 heater_boot_hold_s(5 s) 동안 열선 100 % 를 유지해야 하고(H-E 방지) 그동안 핀 온도가 +42 ~ +76 °C 튄다.
예전 순서(시작 전 냉각 → 기록 시작 → 히트건 켜기)는 이 튐이 가열 기록에 들어가 24.5 → 85 °C 가열이 약 33초 만에 끝났다.
새 순서: 쿨러 켬 → 스테이션 켜기(100 % 유지 · 기록 안 함) → 시작 온도까지 냉각(송풍 켜짐 · 출력 0 %) → 기록 · 가열 → 냉각.
시험용 장비는 lab.kick_c 로 켠 직후 튐을 흉내 낸다(열전대 값 + kick_c × 모양 · 스테이션 켠 뒤 지난 초는 시험용 히트건 since_on).
"""

from app import config as config_mod
from app.jobs.run import DONE, STATION_KICK_C, WAIT
from tests.conftest import SEARCH, alarm_codes, alarm_lines, wait_for

FAST = 200.0
BOOT = dict(heater_boot_hold_s=5.0)                                 # 실장비처럼 켠 직후 100 % 유지(conftest 기본은 0)


def _one(ready_ctrl, time_scale=FAST, kick=0.0, **cfg):
    ctrl = ready_ctrl(time_scale=time_scale, **dict(SEARCH, **dict(BOOT, **cfg)))
    ctrl.lab.kick_c = kick
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[1:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    return ctrl, use[0]["id"]


def _finish(ctrl, timeout=90):
    assert wait_for(lambda: not ctrl.runner.active, timeout), "측정이 끝나지 않았다"


def _rows(ctrl, sid):
    """control.csv 의 줄: 회차 · 단계 · 경과 · 온도 · 저항 · 목표 · 열선 출력 % · 송풍 · 온도 1 · 온도 2 · 한도."""
    it = ctrl.runner.items[sid]
    text = (ctrl.runner.folder / (it["name"] + "_0.10V_control.csv")).read_text(encoding="utf-8-sig").splitlines()
    out = []
    for r in text[1:]:
        c = r.split(",")
        out.append({"cyc": int(c[0]), "phase": c[1], "t": float(c[2]), "T": float(c[3]), "pct": float(c[6]) if c[6] else None, "fan": c[7] == "1"})
    return out


def _boot_lines(ctrl):
    return [ln for ln in ctrl.logs.recent if "히트건 켬 · 측정 · 열선 100 %" in ln]


# ---------------------------------------------------------------------------
def test_kick_stays_out_of_the_heating_record(ready_ctrl):
    ctrl, sid = _one(ready_ctrl, kick=45.0)
    ctrl.run_command("run_start")
    _finish(ctrl)
    run = ctrl.runner
    it = run.items[sid]
    assert it["status"] == DONE, (it["status"], it["reason"])
    start_c = float(run.cond["run_start_c"])
    heat = [r for r in _rows(ctrl, sid) if r["phase"] == "가열"]
    assert len(heat) > 10
    assert heat[0]["T"] <= start_c + 1.0, "가열 첫 줄 %.2f °C · 시작 온도 %.1f °C" % (heat[0]["T"], start_c)
    assert heat[0]["fan"], "가열 첫 줄에 송풍은 이미 켜져 있다(스테이션은 시작 전 냉각보다 먼저 켬)"
    # 가열 첫 줄은 가열 시작의 열선 켜기(r 10 1 → 펌웨어 100 %) 직후 첫 출력 결정 전에 쓰일 수 있다 - 그 뒤로는 100 % 유지가 없다
    assert all(r["pct"] is not None and r["pct"] < 100.0 for r in heat[1:]), [r["pct"] for r in heat[:5]]
    step = max(abs(b["T"] - a["T"]) for a, b in zip(heat, heat[1:]))
    assert step < 5.0, "가열 중 이웃한 두 점 온도 차 %.2f °C(켠 직후 튐이 가열에 들어감)" % step
    assert "fast_rise" not in alarm_codes(ctrl), alarm_lines(ctrl)
    expect = config_mod.heat_time_s(dict(ctrl.cfg.values, **run.cond))
    took = heat[-1]["t"] - heat[0]["t"]
    assert took >= 0.6 * expect, "가열 %.0f초 · 예상 %.0f초" % (took, expect)


def test_boot_hold_happens_once_per_sample(ready_ctrl):
    ctrl, sid = _one(ready_ctrl, kick=45.0)
    ctrl.run_command("run_start")
    _finish(ctrl)
    assert ctrl.runner.items[sid]["status"] == DONE
    assert len(_boot_lines(ctrl)) == 1, _boot_lines(ctrl)


def test_repeat_does_not_restart_the_station(ready_ctrl):
    ctrl, sid = _one(ready_ctrl, kick=45.0, run_repeat=2)
    ctrl.run_command("run_start")
    _finish(ctrl, 150)
    run = ctrl.runner
    it = run.items[sid]
    assert it["status"] == DONE, (it["status"], it["reason"])
    assert len(_boot_lines(ctrl)) == 1, "2회차는 스테이션을 다시 켜지 않는다: %s" % _boot_lines(ctrl)
    start_c = float(run.cond["run_start_c"])
    rows = _rows(ctrl, sid)
    for cyc in (1, 2):
        heat = [r for r in rows if r["cyc"] == cyc and r["phase"] == "가열"]
        assert heat and heat[0]["T"] <= start_c + 1.0, (cyc, heat[0]["T"] if heat else None)


def test_start_temp_not_reached_with_fan_on_stops_the_run(ready_ctrl):
    """쿨러 없이 주위 온도(25 °C)가 시작 온도(15 °C)보다 높다 - 송풍을 켠 채로는 못 닿는다."""
    ctrl, sid = _one(ready_ctrl, kick=45.0, run_cooler_mode="none", run_start_c=15.0, run_stall_s=20.0)
    run = ctrl.runner
    ctrl.run_command("run_start")
    _finish(ctrl)
    it = run.items[sid]
    assert run.state == "stopped" and it["status"] == WAIT and it["reason"] == "시작 온도 미도달", (run.state, it["status"], it["reason"])
    assert "시작 온도 미도달" in run.msg
    fw = ctrl.fake_fw
    assert not fw.relay[ctrl.cfg["heater_element_relay"] - 1], "열선은 바로 꺼진다"
    assert wait_for(lambda: not fw.relay[ctrl.cfg["heater_fan_relay"] - 1], 10), "송풍은 heater_fan_after_s 뒤 꺼진다"
    assert it["points"] == [] and list(run.folder.glob("*.dat")) == [], "가열하지 않았다(.dat 없음)"


def test_hold_mode_never_turns_the_heater_on(ready_ctrl):
    ctrl, sid = _one(ready_ctrl, kick=45.0, contact_check="off", run_heater_use=False, run_hold_s=5.0, run_interval_s=0.5,
                     temp_read_s=0.1, meter_avg_count=1, meter_nplc=0.1)
    ctrl.run_command("run_start")
    _finish(ctrl)
    assert ctrl.runner.items[sid]["status"] == DONE
    assert ctrl.heatgun.log == [], ctrl.heatgun.log


def test_hot_sample_is_cooled_before_the_station_turns_on(ready_ctrl):
    """켜기 전 온도 > 차단 온도 − STATION_KICK_C 면 히트건을 끈 채(쿨러) 먼저 식힌 뒤 켠다."""
    ctrl, sid = _one(ready_ctrl, run_start_c=45.0)
    lab = ctrl.lab
    lim = float(ctrl.cfg["heater_max_c"]) - STATION_KICK_C
    with lab.lock:                                                  # 판 · 샘플이 뜨겁다(주위 70 °C · 쿨러로 40 °C 까지)
        lab.t_amb, lab.t_cool = 70.0, 40.0
        lab.cell0["T"] = 70.0
        for i in range(len(ctrl.world.samples)):
            lab._cell(i)["T"] = 70.0
        lab.Ts = lab.air = 70.0
    h = ctrl.runner.heater
    seen = []
    orig = h.on
    h.on = lambda: (seen.append(ctrl.runner._last_T), orig())[1]
    ctrl.run_command("run_start")
    _finish(ctrl, 150)
    it = ctrl.runner.items[sid]
    assert seen, "히트건을 켜지 않았다 · %s %s" % (it["status"], it["reason"])
    assert seen[0] is not None and seen[0] <= lim, "스테이션을 켠 순간 %.1f °C · 한도 %.1f °C" % (seen[0], lim)
