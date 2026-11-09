"""unit_rt.py - R–T 측정 단위 시험 (python tools/unit_rt.py [-v] [이름일부]). 장비 · 포트 · 카메라 · VISA 를 열지 않는다(가짜 장비 + 수동 시계).

SCPI 문자열 · 범위 단계 변경 · 저항 계산 · 파일 이름 · 형식 · 히트건 켜고 끄기 · 목표 온도선 추종 · 안전 경로(전부 히트건 꺼짐 + 출력 꺼짐).
"""
import contextlib
import os
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))
sys.path.insert(0, os.path.join(ROOT, "tools"))

import fake_visa  # noqa: E402
import rt as rt_mod  # noqa: E402
import settings_schema as sch  # noqa: E402
from core import instruments as inst  # noqa: E402

REPORT = {}


class SimClock:
    def __init__(self):
        self.t = 0.0
        self.scale = 1.0

    def now(self):
        return self.t

    def advance(self, dt):
        self.t += float(dt)


class StubPower:
    heater_n = 10

    def __init__(self):
        self.owners = {}
        self.heater_ok = True
        self.log = []               # (시각 없음) ("on"|"off", n)

    def set(self, n, on, owner):
        if on:
            self.owners[n] = owner
        else:
            self.owners.pop(n, None)
        self.log.append(("on" if on else "off", n))

    def heater_off(self):
        self.owners.pop(10, None)
        self.log.append(("off", 10))


class StubMotion:
    gen = 0
    estop_locked = False
    z_ok = True

    def __init__(self):
        self.z_moves = []

    def op(self, gen=None):
        return contextlib.nullcontext()

    def move_z(self, z, gen=None):
        self.z_moves.append(z)


class StubCfg:
    def __init__(self, over=None):
        self.values = sch.defaults()
        for k, v in (over or {}).items():
            sch.set_path(self.values, k, v)

    def get(self, key):
        return sch.get_path(self.values, key)


class StubHub:
    def __init__(self, clock):
        self.clock = clock


class StubApp:
    def __init__(self, over=None, fault=None):
        self.clock = SimClock()
        self.hub = StubHub(self.clock)
        self.cfg = StubCfg(over)
        self.power = StubPower()
        self.motion = StubMotion()
        self.link_lost_count = 0
        self.pos = {"x": 0, "y": 0, "z": 20.0}
        self.ended = []
        self.lab = fake_visa.Lab(clock=self.clock.now, sleep=self.clock.advance,
                                 heater_fn=lambda: 10 in self.power.owners,
                                 cooler_fn=lambda: any(n != 10 for n in self.power.owners))
        if fault:
            self.lab.fault.update(fault)
        fake_visa.install(self.lab)
        self.k2110 = inst.K2110("USB0::0x05E6::0x2110::INSTR")
        self.k2110.open()
        self.k2450 = inst.K2450("GPIB0::18::INSTR")
        self.k2450.open()

    def on_rt_end(self, rr):
        self.ended.append(rr)

    def make(self, over=None, relays=(1,), mode="rt", name="1-1_"):
        p = rt_mod.params(self.cfg)
        p.update(over or {})
        p["file_path"] = ""
        rr = rt_mod.RTRun(self, p, name, run_dir=tempfile.mkdtemp(prefix="rt_unit_"), relays=list(relays), mode=mode, z_top_end=True)
        return rr


def short_params(**kw):
    d = {"max_temp": 40.0, "break_temp": 30.0, "repeat": 2, "precool_c": 26.0}
    d.update(kw)
    return d


class ScpiTest(unittest.TestCase):
    """명령 문자열이 매뉴얼 문법인지: 가짜 장비가 모르는 명령은 err 에 쌓는다."""

    def test_k2450_setup_order_and_syntax(self):
        app = StubApp()
        app.k2450.configure({"terminal": "FRONT", "four_wire": True, "volt": 1.0, "ilim_a": 0.01, "range_mode": "step", "start_range": 0.001, "nplc": 1.0, "filter_n": 10})
        self.assertEqual(app.lab.dev["2450"].err, [])
        cmds = [c for c in app.k2450.cmds if c != "*IDN?"]
        want = ["*RST", ":ROUT:TERM FRON", ":SENS:CURR:RSEN ON", ":SOUR:FUNC VOLT", ":SOUR:VOLT:ILIM 0.01", ":SOUR:VOLT 1",
                ":SENS:FUNC \"CURR\"", ":SENS:CURR:RANG:AUTO OFF", ":SENS:CURR:RANG 0.001", ":SENS:CURR:NPLC 1", ":SENS:CURR:AVER:TCON REP",
                ":SENS:CURR:AVER:COUN 10", ":SENS:CURR:AVER ON", ":SENS:AZER:ONCE"]
        self.assertEqual(cmds, want)
        self.assertFalse(app.lab.out_on, "출력 켬은 예비 냉각 뒤")
        s = app.lab.dev["2450"].s
        self.assertEqual((s["terminal"], s["rsen"], s["avg_n"], s["avg_on"], s["auto"], s["range"]), ("FRON", True, 10, True, False, 0.001))
        self.assertAlmostEqual(app.lab.ilim, 0.01)

    def test_k2450_rear_two_wire_auto(self):
        app = StubApp()
        app.k2450.configure({"terminal": "REAR", "four_wire": False, "volt": 2.0, "ilim_a": 0.005, "range_mode": "auto", "start_range": 0.001, "nplc": 0.5, "filter_n": 3})
        self.assertEqual(app.lab.dev["2450"].err, [])
        self.assertIn(":ROUT:TERM REAR", app.k2450.cmds)
        self.assertIn(":SENS:CURR:RSEN OFF", app.k2450.cmds)
        self.assertIn(":SENS:CURR:RANG:AUTO ON", app.k2450.cmds)
        self.assertNotIn(":SENS:CURR:RANG 0.001", app.k2450.cmds)

    def test_k2110_thermocouple(self):
        app = StubApp()
        app.k2110.configure("K", "REAL")
        self.assertEqual(app.lab.dev["2110"].err, [])
        s = app.lab.dev["2110"].s
        self.assertEqual((s["tc_type"], s["rjun"], s["unit"]), ("K", "REAL", "C"))
        t = app.k2110.read_temp()
        self.assertAlmostEqual(t, 25.0, delta=0.5)

    def test_tc_open_is_none(self):
        app = StubApp(fault={"tc_open": True})
        self.assertIsNone(app.k2110.read_temp())

    def test_scpi_check(self):
        app = StubApp()
        self.assertTrue(app.k2450.check_scpi()[0])
        app.lab.lang = "TSP"
        ok, why = app.k2450.check_scpi()
        self.assertFalse(ok)
        self.assertIn("SCPI", why)

    def test_no_driver_message(self):
        fake_visa.uninstall()
        try:
            import builtins
            real = builtins.__import__

            def imp(name, *a, **k):
                if name == "pyvisa":
                    raise ImportError("no pyvisa")
                return real(name, *a, **k)
            builtins.__import__ = imp
            try:
                items, msg = inst.list_resources()
            finally:
                builtins.__import__ = real
        finally:
            pass
        self.assertEqual(items, [])
        self.assertIn("NI-VISA", msg)


class MathTest(unittest.TestCase):
    def test_resistance(self):
        self.assertAlmostEqual(inst.resistance(1.0, 2e-3), 500.0)
        self.assertAlmostEqual(inst.resistance(2.0, -1e-6), 2e6)
        self.assertIsNone(inst.resistance(1.0, 0.0))
        self.assertIsNone(inst.resistance(1.0, None))

    def test_next_range_up_down(self):
        self.assertEqual(inst.next_range(1e-3, 0.5e-3), 1e-3)             # 범위 안 · 그대로
        self.assertEqual(inst.next_range(1e-3, 1.2e-3), 1e-2)             # 1.05 배 넘음 → 올림
        self.assertEqual(inst.next_range(1e-3, 3e-6), 1e-5)               # 한 단계 아래 범위 끝 안쪽 → 여러 단계 내림
        self.assertEqual(inst.next_range(1e-3, 0.8e-4), 1e-4)
        self.assertEqual(inst.next_range(1e-3, 0.99e-4), 1e-3)            # 0.95 배 경계: 한 단계 아래 범위(1e-4) 의 0.95 배보다 큼 · 그대로
        self.assertEqual(inst.next_range(1.0, 5.0), 1.0)                  # 최대 범위에서 더 못 올림
        self.assertEqual(inst.next_range(1e-8, 1e-12), 1e-8)

    def test_parse_overflow(self):
        self.assertIsNone(inst.parse_float("+9.91000000E+37"))
        self.assertAlmostEqual(inst.parse_float("+1.234E-03\n"), 1.234e-3)
        self.assertIsNone(inst.parse_float("abc"))


class FileFormatTest(unittest.TestCase):
    def test_names_match_labview(self):
        self.assertEqual(rt_mod.cycle_name("1-1_", 1.0, 1), "1-1_1.00V_1.dat")            # PDF 2쪽: File name · Set voltage(소수 2자리) · "V_" · 회차 · ".dat"
        self.assertEqual(rt_mod.cycle_name("1-1_", 1.0, 3), "1-1_1.00V_3.dat")
        self.assertEqual(rt_mod.merged_name("1-1_", 1.0), "1-1_1.00V_merged.dat")
        self.assertEqual(rt_mod.cycle_name("S", 0.5, 2), "S0.50V_2.dat")

    def test_line_format(self):
        self.assertEqual(rt_mod.data_line(63.25, 275318.0), "6.3250000E+01\t2.7531800E+05")
        self.assertEqual(rt_mod.HEADER, "Temp\tResistance")

    def test_unique_path(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.dat")
        self.assertEqual(rt_mod.unique_path(p), (p, False))
        open(p, "w").close()
        q, ch = rt_mod.unique_path(p)
        self.assertTrue(ch and q.endswith("a_1.dat"))
        open(q, "w").close()
        self.assertTrue(rt_mod.unique_path(p)[0].endswith("a_2.dat"))


class HeatCtlTest(unittest.TestCase):
    def test_band(self):
        self.assertTrue(rt_mod.HeatCtl.want(29.0, 30.0, 0.5, False))
        self.assertFalse(rt_mod.HeatCtl.want(31.0, 30.0, 0.5, True))
        self.assertTrue(rt_mod.HeatCtl.want(30.2, 30.0, 0.5, True))            # 띠 안 - 유지
        self.assertFalse(rt_mod.HeatCtl.want(30.2, 30.0, 0.5, False))

    def test_min_on_off(self):
        c = rt_mod.HeatCtl(2.0, 2.0, 120.0)
        self.assertEqual(c.step(0.0, True)[0], True)                          # 켠다
        self.assertEqual(c.step(1.0, False)[0], True)                         # 최소 켜짐 2 s 전엔 못 끈다
        self.assertEqual(c.step(2.0, False)[0], False)
        self.assertEqual(c.step(3.0, True)[0], False)                         # 최소 꺼짐 2 s 전엔 못 켠다
        self.assertEqual(c.step(4.0, True)[0], True)

    def test_max_on(self):
        c = rt_mod.HeatCtl(2.0, 2.0, 10.0)
        c.step(0.0, True)
        on, ev = c.step(10.0, True)
        self.assertFalse(on)
        self.assertEqual(ev, "max_on")
        self.assertFalse(c.step(11.0, True)[0])                               # 강제로 끈 뒤 최소 꺼짐
        self.assertTrue(c.step(12.0, True)[0])

    def test_target_line(self):
        self.assertIsNone(rt_mod.target_line(25.0, 0, 10))
        self.assertAlmostEqual(rt_mod.target_line(25.0, 2.0, 60.0), 27.0)
        self.assertAlmostEqual(rt_mod.target_line(80.0, -1.0, 120.0), 78.0)


class RunTest(unittest.TestCase):
    def read_dat(self, d, name):
        with open(os.path.join(d, name), encoding="utf-8", newline="") as f:
            return f.read()

    def test_full_run_files_and_hysteresis(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0, precool_c=24.5, repeat=2))
        rr.run()
        self.assertEqual((rr.status, rr.message), (rt_mod.OK, ""))
        names = sorted(os.listdir(rr.run_dir))
        for n in ("1-1_1.00V_1.dat", "1-1_1.00V_2.dat", "1-1_1.00V_merged.dat", "control.csv", "rt.json"):
            self.assertIn(n, names)
        c1 = self.read_dat(rr.run_dir, "1-1_1.00V_1.dat").split("\r\n")
        self.assertEqual(c1[0], "Temp\tResistance")
        pts = [tuple(map(float, ln.split("\t"))) for ln in c1[1:] if ln]
        self.assertGreater(len(pts), 100)
        self.assertRegex(c1[1], r"^\d\.\d{7}E[+-]\d{2}\t\d\.\d{7}E[+-]\d{2}$")
        merged = self.read_dat(rr.run_dir, "1-1_1.00V_merged.dat").split("\r\n")
        n1 = len(pts)
        n2 = len([x for x in self.read_dat(rr.run_dir, "1-1_1.00V_2.dat").split("\r\n")[1:] if x])
        self.assertEqual(len([x for x in merged[1:] if x]), n1 + n2)
        self.assertEqual(merged[0], "Temp\tResistance")
        # 히스테리시스: 가열 쪽(오름)은 66 °C 근처에서, 냉각 쪽(내림)은 58 °C 근처에서 꺾인다
        up = [(t, r) for (t, r), nxt in zip(pts, pts[1:]) if nxt[0] > t]
        half = max(range(len(pts)), key=lambda i: pts[i][0])
        heat, cool = pts[:half], pts[half:]

        def cross(seq, lim):
            for t, r in seq:
                if r < lim:
                    return t
        th, tc = cross(heat, 1e4), cross(cool[::-1], 1e4)
        self.assertTrue(60 < th < 72, th)
        self.assertTrue(50 < tc < 64, tc)
        self.assertLess(tc, th)
        # 끝: 히트건 꺼짐 · K2450 출력 꺼짐 · 쿨러 꺼짐 · Z 맨 위
        self.assertNotIn(10, app.power.owners)
        self.assertFalse(app.lab.out_on)
        self.assertEqual(app.power.owners, {})
        self.assertEqual(app.motion.z_moves, [0.0])
        self.assertEqual(rr.cycle, 2)

    def assertSafe(self, app, rr):
        self.assertNotIn(10, app.power.owners, "히트건 꺼짐")
        self.assertFalse(app.lab.out_on, "K2450 출력 꺼짐")
        self.assertTrue(rr.finished.is_set())

    def test_thermocouple_open(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        orig = app.clock.advance

        def adv(dt):
            orig(dt)
            if app.clock.t > 60 and not app.lab.fault["tc_open"]:
                app.lab.fault["tc_open"] = True
        app.lab.sleep = adv
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("온도 값 없음", rr.message)
        self.assertSafe(app, rr)

    def test_emergency_temperature(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, emergency_c=60.0, break_temp=25.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("비상 차단", rr.message)
        self.assertSafe(app, rr)
        self.assertLess(app.lab.Ts, 66.0)

    def test_phase_time_limit(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, phase_limit_s=20.0, break_temp=25.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("시간 한도", rr.message)
        self.assertSafe(app, rr)

    def test_stale_temperature(self):
        app = StubApp()
        app.lab.read_dt = 4.0                         # 읽기 한 번에 4 s > 3 s
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("갱신", rr.message)
        self.assertSafe(app, rr)

    def test_link_disconnect(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        orig = app.clock.advance

        def adv(dt):
            orig(dt)
            if app.clock.t > 40:
                app.lab.fault["disconnect"] = True
        app.lab.sleep = adv
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("통신 끊김", rr.message)
        self.assertNotIn(10, app.power.owners)

    def test_stop_during_heating(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        orig = app.clock.advance

        def adv(dt):
            orig(dt)
            if app.clock.t > 50 and 10 in app.power.owners:
                rr.stop()
        app.lab.sleep = adv
        rr.run()
        self.assertEqual(rr.status, rt_mod.STOPPED)
        self.assertSafe(app, rr)
        self.assertEqual(app.power.owners, {})

    def test_estop_generation(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        orig = app.clock.advance

        def adv(dt):
            orig(dt)
            if app.clock.t > 50 and 10 in app.power.owners:
                app.motion.gen += 1                    # 비상정지
        app.lab.sleep = adv
        rr.run()
        self.assertEqual(rr.status, rt_mod.STOPPED)
        self.assertSafe(app, rr)
        self.assertEqual(app.motion.z_moves, [], "비상정지 뒤에는 Z 를 움직이지 않는다")

    def test_link_lost_counter(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0))
        orig = app.clock.advance

        def adv(dt):
            orig(dt)
            if app.clock.t > 50:
                app.link_lost_count += 1
        app.lab.sleep = adv
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("통신 끊김", rr.message)
        self.assertSafe(app, rr)

    def test_ilim_trip_warns_continues(self):
        app = StubApp()
        app.lab.fault["ilim"] = True
        rr = app.make(short_params(max_temp=45.0, break_temp=30.0, repeat=1))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK)
        self.assertTrue(any("전류 한도" in w for w in rr.warns))
        self.assertSafe(app, rr)

    def test_range_steps_follow_current(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0, repeat=1))
        rr.run()
        rngs = [ln for ln in app.k2450.cmds if ln.startswith(":SENS:CURR:RANG 0") or ln.startswith(":SENS:CURR:RANG 1")]
        self.assertGreaterEqual(len(set(rngs)), 3, rngs)                     # 3e5 Ω(3 µA) ~ 2e2 Ω(5 mA): 범위가 여러 단계 바뀐다

    def test_heater_never_on_when_cooling_max(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=60.0, break_temp=30.0, repeat=1))
        rr.run()
        cool_pts = [p for p in rr.points if p["ph"] == "냉각"]
        self.assertTrue(cool_pts and all(p["h"] == 0 for p in cool_pts))
        heat_pts = [p for p in rr.points if p["ph"] == "가열"]
        self.assertTrue(heat_pts and all(p["h"] == 1 for p in heat_pts[:-1]), "최대 속도 가열은 계속 켠다")

    def test_forced_off_at_continuous_limit(self):
        app = StubApp({"heater.max_on_s": 20.0})
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0, repeat=1))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK)
        self.assertTrue(any("연속 켜짐 상한" in w for w in rr.warns))
        # 연속 켜짐 구간이 상한(20 s) + 읽기 간격 안
        run, longest, prev_t = 0.0, 0.0, None
        for p in rr.points:
            if p["h"]:
                run = run + (p["t"] - prev_t) if prev_t is not None and run else 0.0
                longest = max(longest, run)
            else:
                run = 0.0
            prev_t = p["t"]
        self.assertLess(longest, 20.0 + 1.0)

    def test_speed_test(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=80.0), mode="speed")
        rr.speed_s = 30.0
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        self.assertGreater(rr.speed["heat"], 10.0)                           # °C/분
        self.assertTrue(rr.speed["cool"] <= 0.5)                              # 히트건 뒤 식음(또는 정체) - 음수
        self.assertNotIn(10, app.power.owners)


class FollowTest(unittest.TestCase):
    """목표 온도선 추종: 가열 2 °C/분 · 냉각 1 °C/분. 평균 속도 오차를 보고한다."""

    def slope(self, pts, t_lo, t_hi):
        sel = [(p["t"], p["T"]) for p in pts if t_lo <= p["t"] <= t_hi and p["T"] is not None]
        n = len(sel)
        mt = sum(a for a, _ in sel) / n
        mT = sum(b for _, b in sel) / n
        return sum((a - mt) * (b - mT) for a, b in sel) / sum((a - mt) ** 2 for a, _ in sel) * 60.0

    def test_follow_rates(self):
        app = StubApp()
        rr = app.make({"max_temp": 45.0, "break_temp": 30.0, "precool_c": 26.0, "repeat": 1, "heat_rate": 2.0, "cool_rate": 1.0, "band_c": 0.5,
                       "min_on_s": 2.0, "min_off_s": 2.0, "phase_limit_s": 7200.0})
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        heat = [p for p in rr.points if p["ph"] == "가열"]
        cool = [p for p in rr.points if p["ph"] == "냉각"]
        h0, h1 = heat[0]["t"], heat[-1]["t"]
        c0, c1 = cool[0]["t"], cool[-1]["t"]
        # 처음 90 s(켜지기 전 · 지연)는 빼고 평균 속도
        rh = self.slope(heat, h0 + 90, h1)
        rc = self.slope(cool, c0 + 60, c1)
        eh, ec = rh - 2.0, rc - (-1.0)
        REPORT["가열 2 °C/분 평균 속도"] = "%.2f °C/분 (오차 %+.2f)" % (rh, eh)
        REPORT["냉각 1 °C/분 평균 속도"] = "%.2f °C/분 (오차 %+.2f)" % (rc, ec)
        # 선 대비 평균 온도 편차
        dev = [abs(p["T"] - p["tgt"]) for p in heat if p["tgt"] is not None and p["t"] > h0 + 60 and p["T"] is not None]
        REPORT["가열 중 |온도 - 목표선| 평균"] = "%.2f °C" % (sum(dev) / len(dev))
        self.assertLess(abs(eh), 0.4, "가열 평균 속도 오차 %.2f" % eh)
        self.assertLess(abs(ec), 0.4, "냉각 평균 속도 오차 %.2f" % ec)
        on_frac = sum(p["h"] for p in heat) / len(heat)
        REPORT["가열 중 히트건 켜짐 비율"] = "%.0f %%" % (100 * on_frac)
        self.assertLess(on_frac, 0.6)


class SchemaTest(unittest.TestCase):
    def test_defaults_match_labview_screen(self):
        d = sch.defaults()["rt"]
        self.assertEqual((d["precool_c"], d["max_temp"], d["break_temp"], d["repeat"], d["volt"], d["filter_n"]), (24.5, 85.0, 25.0, 3, 1.0, 10))
        self.assertEqual((d["k2450_addr"], d["file_name"], d["nplc"], d["start_range"], d["four_wire"], d["terminal"]), ("GPIB0::18::INSTR", "1-1_", 1.0, 0.001, True, "FRONT"))
        self.assertEqual(d["file_path"], "\\\\VanaM_NAS\\VanaM_VO2\\VO2 data")
        self.assertEqual((d["band_c"], d["min_on_s"], d["min_off_s"], d["phase_limit_s"], d["stale_s"]), (0.5, 2.0, 2.0, 1800.0, 3.0))

    def test_validation(self):
        v, errs, _w = sch.validate(sch.defaults())
        self.assertEqual(errs, {})
        bad = sch.defaults()
        bad["rt"]["break_temp"] = 90.0
        self.assertIn("rt.break_temp", sch.validate(bad)[1])
        bad = sch.defaults()
        bad["rt"]["emergency_c"] = 80.0
        self.assertIn("rt.emergency_c", sch.validate(bad)[1])
        bad = sch.defaults()
        bad["rt"]["start_range"] = 0.003
        self.assertIn("rt.start_range", sch.validate(bad)[1])
        bad = sch.defaults()
        bad["rt"]["cooler_custom"] = "1,10"
        self.assertIn("rt.cooler_custom", sch.validate(bad)[1])


if __name__ == "__main__":
    prog = unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:]], verbosity=2, exit=False)
    for k, v in REPORT.items():
        print("보고 · %s = %s" % (k, v))
    sys.exit(0 if prog.result.wasSuccessful() else 1)
