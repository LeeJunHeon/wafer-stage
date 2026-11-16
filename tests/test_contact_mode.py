"""접촉 방식 · K2450 사용권.

접촉 방식(contact_mode): 기본은 "고정 높이"(저장한 접촉 높이까지만 내린다 · 저항은 기록만) / "전기 탐색"은 선택 사항.
전기 탐색의 걸음: 아래로 가는 한 걸음은 설정한 간격을 넘지 않는다(더 누름 포함) · 한도 아래로는 내리지 않는다.
K2450 사용권: 한 번에 한 기능(접촉 탐색 · 1회 측정 · 연속 측정 · 자동 측정 · 직접 명령). 접촉 · 측정 시작은 연속 측정을 먼저 끈다.
"""

import json
import threading

import pytest

from app import contact as contact_mod
from app.jobs.run import DONE
from app.safety import Rejected
from tests.conftest import SEARCH, wait_for

PPMM = 160
FAST = 200.0


def _gz(ctrl, start=0):
    """가짜 펌웨어가 받은 Z 절대 이동(펄스) 순서."""
    return [int(c.split()[1]) for c in ctrl.fake_fw.log[start:] if c.startswith("gz ")]


def _over(ctrl, i, above):
    w = ctrl.world
    x, y = w.sample_stage(i)
    ctrl.goto(x, y, round(w.sample_surface_z(i) - above, 4))
    return w.sample_surface_z(i)


def _in_thread(fn):
    out = {}

    def run():
        try:
            out["res"] = fn()
        except Rejected as e:
            out["msg"] = str(e)

    t = threading.Thread(target=run)
    t.start()
    return t, out


def _pick(ctrl, n):
    ctrl.run_command("samples_find")
    use = [s for s in ctrl.prep.samples if s["use"]]
    for s in use[n:]:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    return [s["id"] for s in use[:n]]


# ---------------------------------------------------------------------------
# 고정 높이(기본)
# ---------------------------------------------------------------------------
def test_default_is_fixed_height_and_run_goes_exactly_to_the_saved_height(ready_ctrl):
    """기본(고정 높이): 접촉 단계는 저장한 접촉 높이까지만 내린다. 전기 판단으로 더 내리지도, 안 닿았다고 실패시키지도 않는다.
    내린 뒤 잰 저항은 run.json 의 샘플 contact 에 기록만 한다."""
    ctrl = ready_ctrl(time_scale=FAST, run_heater_off_c=40.0, run_cool_end_c=36.0)      # 접촉 단계를 보는 시험: 가열 · 냉각 구간은 짧게
    assert ctrl.cfg["contact_mode"] == "fixed" and ctrl.cfg["contact_fixed_read"] is True
    cz = ctrl.cfg["contact_z_mm"]                                    # 13.4 mm: 표면이 13.5 mm 인 샘플에는 닿지 않는 높이
    ctrl.run_command("samples_find")
    x0, y0 = ctrl.world.sample_stage(0)                              # 표면 13.5 mm 인 샘플 하나만 잰다(고정 높이로는 닿지 않는다)
    far = [s for s in ctrl.prep.samples if s["use"] and abs(s["xy"][0] - x0) + abs(s["xy"][1] - y0) > 3.0]
    for s in far:
        ctrl.run_command("sample_use", {"id": s["id"], "use": False})
    (sid,) = [s["id"] for s in ctrl.prep.samples if s["use"]]
    n0 = len(ctrl.fake_fw.log)
    ctrl.run_command("run_start")
    assert wait_for(lambda: not ctrl.runner.active, 40)
    run = ctrl.runner
    assert max(_gz(ctrl, n0)) == round(cz * PPMM), "저장한 높이보다 한 펄스도 더 내려가지 않는다"
    it = run.items[sid]
    assert it["status"] == DONE, "닿지 않아도 접촉 단계에서 실패시키지 않는다: %s" % it["reason"]
    ct = it["contact"]
    assert ct["mode"] == "fixed" and ct["z"] == cz and ct["volt"] == 1.0
    assert ct["ohm"] is not None and ct["ohm"] > 1e9, "안 닿은 샘플(열린 회로)의 값도 판정 없이 기록만 한다"
    rec = json.loads((run.folder / "run.json").read_text(encoding="utf-8"))
    assert rec["cond"]["contact_mode"] == "fixed" and rec["cond"]["contact_fixed_read"] is True
    assert rec["samples"][0]["contact"] == ct and rec["samples"][0]["status"] == DONE
    assert ctrl.lab.out_on is False and not any(ctrl.fake_fw.relay)


def _bare(make_ctrl, **cfg):
    """스테이지 · 측정 장비만 연결한 것(초기 설정 값 없이). 접촉은 위치와 저항계만 있으면 된다."""
    ctrl = make_ctrl(scale=0.02, **cfg)
    ctrl.lab.read_dt = 0.002
    return ctrl


def test_fixed_contact_without_reading_and_search_commands_are_off(make_ctrl):
    ctrl = _bare(make_ctrl)
    w = ctrl.world
    x, y = w.sample_stage(4)                                         # 표면 13.4 mm
    ctrl.goto(x, y)
    g = ctrl.safety.generation
    n0 = len(ctrl.fake_fw.log)
    r = ctrl.contact_fixed(13.4, g, read=False)
    assert r == {"mode": "fixed", "z": 13.4, "ohm": None, "current": None, "tripped": None, "four_wire": None, "volt": None}
    assert _gz(ctrl, n0)[-1] == round(13.4 * PPMM) and ctrl.state.get("pos", "z") == 13.4
    assert ctrl.lab.out_on is False and ctrl.fake_fw.v == ctrl.cfg["move_speed_pps"], "저항계를 쓰지 않고, 느린 속도를 되돌린다"
    r = ctrl.contact_fixed(13.4, g, read=True)                       # 이미 그 높이: 움직이지 않고 한 번 잰다
    assert r["ohm"] is not None and abs(r["ohm"] - w.samples[4]["r_hi"]) < 0.05 * r["ohm"] and ctrl.lab.out_on is False
    with pytest.raises(Rejected) as e:                               # 핀이 접촉 높이보다 낮으면 올렸다 내리지 않는다
        ctrl.contact_fixed(13.0, g, read=False)
    assert "핀 상승 필요" in str(e.value)
    # 전기 탐색은 선택 사항: 접촉 방식이 고정 높이면 [접촉 탐색] 을 받지 않는다(화면도 버튼을 숨긴다)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("contact_find")
    assert str(e.value) == "접촉 방식 고정 높이 · 전기 탐색 선택 필요"


def test_save_current_height_as_contact_height(make_ctrl):
    """초기 설정 6 의 기본 화면: 핀을 작은 단위로 내리고 [현재 높이로 저장] → contact_z_mm = 지금 Z · 방법 "수동"."""
    ctrl = _bare(make_ctrl)
    assert ctrl.cfg["contact_z_mm"] is None and not ctrl.prep.steps()[5]["done"]
    with pytest.raises(Rejected):
        ctrl.run_command("contact_save_here")                        # 핀이 맨 위
    ctrl.jog("z", 12.0)
    for mm in (0.1, 0.05, 0.01):                                     # 작은 단위(0.01 mm 는 2 펄스 = 0.0125 mm 로 맞춰진다)
        ctrl.jog("z", mm)
    z = ctrl.state.get("pos", "z")
    assert ctrl.fake_fw.pz == 1920 + 16 + 8 + 2 and z == round(1946 / PPMM, 3)
    assert ctrl.run_command("contact_save_here") == {"contact_z_mm": z}
    assert ctrl.cfg["contact_z_mm"] == z and ctrl.calib.d["contact"]["how"] == "수동"
    assert ctrl.prep.steps()[5]["done"] and ("Z %.2f mm" % z) in ctrl.prep.steps()[5]["text"]


# ---------------------------------------------------------------------------
# 전기 탐색: 걸음 크기 · 한도
# ---------------------------------------------------------------------------
def test_search_defaults_and_criteria_are_all_settings(make_ctrl):
    ctrl = make_ctrl(connect=False)
    c = ctrl.cfg
    assert (c["contact_step_mm"], c["contact_start_above_mm"], c["contact_max_below_mm"], c["contact_search_step_mm"], c["contact_search_mm"]) == (0.1, 0.3, 0.5, 0.1, 1.0)
    p = contact_mod.params(c)                                        # 판단 기준은 코드 상수가 아니라 전부 설정이다
    assert (p["open_a"], p["short_ohm"], p["confirm_n"], p["overdrive_mm"], p["four_min_ratio"], p["search_back_mm"], p["max_reads"]) == (1e-9, 1000.0, 2, 0.05, 0.1, 0.3, 2000)
    assert not hasattr(contact_mod, "FOUR_MIN_RATIO") and not hasattr(contact_mod, "SEARCH_BACK_MM") and not hasattr(contact_mod, "MAX_READS")
    assert contact_mod.four_ok(50.0, 100.0, False, 0.1) and not contact_mod.four_ok(5.0, 100.0, False, 0.1) and contact_mod.four_ok(5.0, 100.0, False, 0.01)
    # 간격은 펄스 단위로 내림한다(실제 이동이 설정한 간격을 넘지 않게). 한 펄스 = 0.00625 mm
    one = 1.0 / PPMM
    assert round(contact_mod.step_floor(0.1, one) * PPMM) == 16 and round(contact_mod.step_floor(0.03, one) * PPMM) == 4
    assert round(contact_mod.step_floor(0.00625, one) * PPMM) == 1


@pytest.mark.parametrize("over", [{}, {"contact_step_mm": 0.03, "contact_search_step_mm": 0.08, "contact_search_back_mm": 0.2},
                                  {"contact_overdrive_mm": 0.3, "contact_step_mm": 0.05}])
def test_search_never_steps_down_more_than_the_step(make_ctrl, over):
    """찾기 모드(초기 설정 6 · 수동): 아래로 가는 걸음마다 그 단계의 간격 이하다 - 첫 하강은 탐색 간격, 되돌린 뒤와 더 누름은 하강 간격.
    위로 가는 되돌림만 한 번에 간다. 한도(시작 + 탐색 거리) 아래로는 내리지 않는다."""
    ctrl = _bare(make_ctrl, **dict({"contact_mode": "search"}, **over))
    c = ctrl.cfg
    surf = _over(ctrl, 2, above=0.55)
    z0 = ctrl.state.get("pos", "z")
    n0 = len(ctrl.fake_fw.log)
    r = ctrl.run_command("contact_find")
    seq = [round(z0 * PPMM)] + _gz(ctrl, n0)
    fine = int(c["contact_step_mm"] * PPMM + 1e-9)
    coarse = int(c["contact_search_step_mm"] * PPMM + 1e-9)
    back = next(i for i in range(1, len(seq)) if seq[i] < seq[i - 1])            # 되돌림(위로)
    assert seq[back - 1] - seq[back] == round(c["contact_search_back_mm"] * PPMM), "되돌림은 위로 한 번에"
    assert all(0 < b - a <= coarse for a, b in zip(seq[:back - 1], seq[1:back])), "첫 하강: 탐색 간격 이하 %s" % seq[:back]
    down = [b - a for a, b in zip(seq[back:], seq[back + 1:])]
    assert down and all(0 < d <= fine for d in down), "되돌린 뒤 · 더 누름: 하강 간격 %d 펄스 이하 %s" % (fine, down)
    assert max(seq) <= round((z0 + c["contact_search_mm"]) * PPMM), "한도 아래로는 내리지 않는다"
    assert abs(r["first_z"] - surf) <= c["contact_step_mm"] + 1e-9 and r["first_z"] >= surf - 1e-9
    assert r["final_z"] <= r["first_z"] + max(c["contact_overdrive_mm"], (c["contact_confirm_n"] - 1) * c["contact_step_mm"]) + 1e-9
    assert ctrl.lab.out_on is False


def test_height_mode_steps_and_the_mover_refuses_an_oversized_step(make_ctrl):
    ctrl = _bare(make_ctrl, contact_mode="search", contact_overdrive_mm=0.25)
    w = ctrl.world
    x, y = w.sample_stage(3)
    surf = w.sample_surface_z(3)                                     # 13.3 mm
    ctrl.goto(x, y)
    g = ctrl.safety.generation
    n0 = len(ctrl.fake_fw.log)
    r = ctrl.contact_descend("height", surf, g)                      # 높이 모드(측정): 저장 높이 − 0.3 mm 까지 간 뒤 0.1 mm 씩
    seq = _gz(ctrl, n0)
    start = round((surf - 0.3) * PPMM)
    steps = seq[seq.index(start):]
    assert all(0 < b - a <= 16 for a, b in zip(steps, steps[1:])), "시작 높이 뒤로는 모든 걸음이 0.1 mm(16 펄스) 이하: %s" % steps
    assert max(seq) <= round((surf + 0.5) * PPMM) and abs(r["first_z"] - surf) < 1e-6
    assert abs(r["final_z"] - (surf + 0.25)) < 0.00626, "더 누름 0.25 mm 도 나눠서 내린다"
    # 움직이는 쪽의 마지막 확인: 판단 함수가 간격보다 큰 걸음을 요청하면 움직이지 않고 실패한다
    ctrl.goto(x, y, 5.0)
    n1 = len(ctrl.fake_fw.log)
    with pytest.raises(contact_mod.ContactFail) as e:
        with ctrl._moving("시험", None):
            ctrl._probe_down(g, 5.0, 8.0, lambda z: 0.5, lambda: 0.1)
    assert e.value.code == "step" and _gz(ctrl, n1) == [], "큰 걸음은 한 번도 나가지 않는다"
    with pytest.raises(contact_mod.ContactFail) as e:                # 한도 아래로 가는 걸음도 나가지 않는다
        with ctrl._moving("시험", None):
            ctrl._probe_down(g, 5.0, 5.05, lambda z: 0.1, lambda: 0.1)
    assert e.value.code == "none" and _gz(ctrl, n1) == []


# ---------------------------------------------------------------------------
# K2450 사용권
# ---------------------------------------------------------------------------
def test_contact_search_turns_monitor_off_and_owns_the_k2450(make_ctrl):
    """연속 측정을 켠 채 접촉 탐색: 연속 측정이 꺼지고 탐색이 정상 높이(표면)에서 끝난다. 탐색 중 [1회 측정] · 직접 명령은 거절한다.
    (예전: 연속 측정이 탐색 도중 K2450 설정을 바꾸고 출력을 꺼서 탐색이 "안 닿음" 으로 읽고 한도까지 내려갔다.)"""
    ctrl = _bare(make_ctrl, **SEARCH)
    surf = _over(ctrl, 0, above=1.0)                                 # 표면 13.5 mm
    ctrl.run_command("monitor", {"on": True})
    assert wait_for(lambda: ctrl.state.get("resistance", "time") is not None, 5), "연속 측정이 돌고 있다"
    ctrl.lab.read_dt = 0.03                                          # 탐색이 연속 측정 주기(1 초)보다 오래 걸리게
    t, out = _in_thread(lambda: ctrl.run_command("contact_find"))
    assert wait_for(lambda: ctrl.prep.contact["active"] and ctrl.lab.out_on, 5)
    assert ctrl.state.get("monitor") is False, "접촉 탐색이 연속 측정을 먼저 끈다"
    with pytest.raises(Rejected) as e:
        ctrl.run_command("read_resistance")
    assert str(e.value) == "저항계 사용 중 · 접촉 탐색"
    with pytest.raises(Rejected) as e:
        ctrl.run_command("raw", {"target": "k2450", "line": "*IDN?"})
    assert str(e.value) == "저항계 사용 중 · 접촉 탐색"
    # 탐색 중에 연속 측정을 다시 켜도: 루프는 사용권을 못 얻어 그 회차를 건너뛸 뿐 탐색에 끼어들지 못한다(오류로 꺼지지도 않는다)
    ctrl.run_command("monitor", {"on": True})
    t.join(30)
    r = out.get("res")
    assert r is not None, out
    assert abs(r["first_z"] - surf) <= 0.05, "표면에서 끝난다(한도 %.2f mm 까지 내려가지 않는다)" % (surf + 4.0)
    assert ctrl.state.get("pos", "z") <= surf + 0.1 and ctrl.lab.out_on is False
    assert ctrl.state.get("monitor") is True and not ctrl.state.has_alarm("monitor")
    t0 = ctrl.state.get("resistance", "time")
    assert wait_for(lambda: ctrl.state.get("resistance", "time") != t0, 5), "탐색이 끝나면 연속 측정이 다시 읽는다"
    ctrl.run_command("monitor", {"on": False})
    assert wait_for(lambda: ctrl._k2450_user is None, 5)
    assert ctrl.run_command("read_resistance")["ohm"] > 1e3          # 아무도 쓰지 않으면 1회 측정이 된다
    assert [ln for ln in ctrl.logs.recent if "연속 측정 끔 · 접촉 탐색 시작" in ln]


def test_run_start_turns_monitor_off_and_holds_the_k2450_until_the_end(ready_ctrl):
    ctrl = ready_ctrl(time_scale=FAST, **SEARCH)
    _pick(ctrl, 1)
    # 접촉 탐색이 K2450 을 쓰는 동안에는 측정을 시작하지 않는다
    _over(ctrl, 0, above=0.5)
    ctrl.lab.read_dt = 0.03 * FAST                                   # 읽기 한 번이 실제 0.03 초 걸리게(가짜 장비의 시간은 측정 시계 기준)
    t, out = _in_thread(lambda: ctrl.run_command("contact_find"))
    assert wait_for(lambda: ctrl.prep.contact["active"] and ctrl.lab.out_on, 5)
    with pytest.raises(Rejected) as e:
        ctrl.run_command("run_start")
    assert str(e.value) == "저항계 사용 중 · 접촉 탐색" and not ctrl.runner.active
    t.join(30)
    assert "res" in out
    ctrl.lab.read_dt = 0.002
    # 연속 측정을 켠 채 측정 시작 → 꺼진다. 측정 중에는 다시 켤 수 없다
    ctrl.run_command("monitor", {"on": True})
    ctrl.run_command("run_start")
    assert ctrl.state.get("monitor") is False and ctrl._k2450_user == "자동 측정"
    with pytest.raises(Rejected):
        ctrl.run_command("monitor", {"on": True})
    with pytest.raises(Rejected):
        ctrl.run_command("read_resistance")
    ctrl.run_command("run_stop")
    assert wait_for(lambda: not ctrl.runner.active, 30)
    assert ctrl._k2450_user is None, "측정이 끝나면 사용권을 놓는다"
    assert ctrl.state.get("monitor") is False and not ctrl.state.has_alarm("monitor")
    assert ctrl.run_command("read_resistance")["ohm"] is not None
