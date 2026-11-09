"""unit_rt.py - R–T 측정 단위 시험 (python tools/unit_rt.py [-v] [이름일부]). 장비 · 포트 · 카메라 · VISA 를 열지 않는다(가짜 장비 + 수동 시계).

SCPI 문자열 · 범위 단계 변경 · 저항 계산 · 파일 이름 · 형식 · 히트건 켜고 끄기 · 목표 온도선 추종 · 안전 경로(전부 히트건 꺼짐 + 출력 꺼짐).
"""
import contextlib
import os
import sys
import tempfile
import time
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
    d = {"max_temp": 40.0, "break_temp": 30.0, "repeat": 2, "precool_c": 26.0, "volt": 1.0, "ilim_a": 0.01}      # 전압 · 한도는 옛 값(범위 넘침 시험용) - 새 기본값은 SchemaTest · Rear 시험
    d.update(kw)
    return d


class ScpiTest(unittest.TestCase):
    """명령 문자열이 매뉴얼 문법인지: 가짜 장비가 모르는 명령은 err 에 쌓는다."""

    def test_k2450_setup_order_and_syntax(self):
        app = StubApp()
        app.k2450.configure({"terminal": "FRONT", "four_wire": True, "volt": 1.0, "ilim_a": 0.01, "range_mode": "step", "start_range": 0.001, "nplc": 1.0, "filter_n": 10})
        self.assertEqual(app.lab.dev["2450"].err, [])
        cmds = [c for c in app.k2450.cmds if c not in ("*IDN?", "SYST:ERR?")]
        want = ["*RST", ":ROUT:TERM FRON", ":ROUT:TERM?", ":SENS:CURR:RSEN ON", ":SOUR:FUNC VOLT", ":SOUR:VOLT:ILIM 0.01", ":SOUR:VOLT 1",
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

    def test_k2450_rear_default_order_and_check(self):
        """실제 장비는 뒷면 3축 단자: *RST → :ROUT:TERM REAR → :ROUT:TERM? 확인 → 나머지 설정, 출력 켬은 그 뒤."""
        app = StubApp()
        p = rt_mod.params(app.cfg)
        self.assertEqual(p["terminal"], "REAR")
        app.k2450.configure({"terminal": p["terminal"], "four_wire": True, "volt": 0.1, "ilim_a": 0.1, "range_mode": "step", "start_range": 0.001, "nplc": 1.0, "filter_n": 10})
        cmds = [c for c in app.k2450.cmds if c not in ("*IDN?", "SYST:ERR?")]
        self.assertEqual(cmds[:3], ["*RST", ":ROUT:TERM REAR", ":ROUT:TERM?"])
        self.assertEqual(app.k2450.terminal(), "REAR")
        self.assertFalse(app.lab.out_on)
        app.k2450.output(True)
        i, _t = app.k2450.read()
        self.assertGreater(i, 1e-7)                                  # 케이블이 뒷면 → 전류가 흐른다
        app.k2450.configure({"terminal": "REAR", "four_wire": True, "volt": 0.1, "ilim_a": 0.1, "range_mode": "step", "start_range": 0.001, "nplc": 1.0, "filter_n": 10})
        self.assertFalse(app.lab.out_on, "출력이 켜진 채로 단자를 바꾸지 않는다 - 다시 설정하면 먼저 꺼진다")

    def test_k2450_terminal_mismatch_raises(self):
        app = StubApp()
        orig = app.lab.dev["2450"].query
        app.lab.dev["2450"].query = lambda c: "FRON" + chr(10) if c.strip().upper() == ":ROUT:TERM?" else orig(c)
        with self.assertRaises(inst.VisaError):
            app.k2450.configure({"terminal": "REAR", "four_wire": True, "volt": 0.1, "ilim_a": 0.1, "range_mode": "step", "start_range": 0.001, "nplc": 1.0, "filter_n": 10})

    def test_open_circuit_helper(self):
        self.assertTrue(inst.is_open_circuit(2e-12, 1e-9))
        self.assertTrue(inst.is_open_circuit(-5e-10, 1e-9))
        self.assertFalse(inst.is_open_circuit(1e-6, 1e-9))
        self.assertFalse(inst.is_open_circuit(None, 1e-9))                 # 범위 넘음은 열린 회로가 아니다

    def test_limit_rule_ranges(self):
        """고정 범위에서는 한도 ≥ 범위의 10.6 % . 한도 0.1 A 면 1 A 범위는 쓰지 않는다."""
        for r in inst.RANGES:
            self.assertEqual(inst.limit_ok(r, 0.1), r <= 0.9, r)
        self.assertEqual(inst.clamp_range(1.0, 0.1), 0.1)
        self.assertEqual(inst.clamp_range(1e-3, 0.1), 1e-3)
        self.assertEqual(inst.clamp_range(1.0, 0.01), 0.01)
        # 0.1 A 한도에서 전류 0 ~ 한도를 훑어도 고른 범위는 규칙을 지킨다
        rng = 1e-3
        for k in range(0, 200):
            cur = 10 ** (-9 + 8 * k / 199.0)
            rng = inst.clamp_range(inst.next_range(rng, cur), 0.1)
            self.assertTrue(inst.limit_ok(rng, 0.1), (cur, rng))

    def test_k2110_thermocouple(self):
        app = StubApp()
        app.k2110.configure("K", "REAL")
        self.assertEqual(app.lab.dev["2110"].err, [])
        s = app.lab.dev["2110"].s
        self.assertEqual((s["tc_type"], s["rjun"], s["unit"]), ("K", "REAL", "C"))
        t = app.k2110.read_temp()
        self.assertAlmostEqual(t, 25.0, delta=0.5)

    def test_k2110_manual_syntax_and_error_queue(self):
        app = StubApp()
        app.k2110.configure("K", "REAL")
        cmds = [c for c in app.k2110.cmds if c not in ("*IDN?",)]
        self.assertEqual(cmds, ["*RST", 'SENS:FUNC "TCO"', "SENS:TCO:TYPE K", "SENS:TCO:RJUN:RSEL REAL", "UNIT Cel", "SYST:ERR?"])
        # 문법이 틀린 명령은 오류 큐에 쌓이고 configure 가 거절한다
        app.k2110.write("SENS:TEMP:TRAN TC")
        with self.assertRaises(inst.VisaError):
            app.k2110.check_errors()

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
        # 실제 LabVIEW 파일(2026-11-06): 5-1-ncd(380)_0.10V_0.dat · _merged.dat - 파일 회차 번호는 0 부터(화면의 n회 = n+1)
        self.assertEqual(rt_mod.cycle_name("5-1-ncd(380)_", 0.1, 1), "5-1-ncd(380)_0.10V_0.dat")
        self.assertEqual(rt_mod.cycle_name("1-1_", 1.0, 3), "1-1_1.00V_2.dat")
        self.assertEqual(rt_mod.merged_name("5-1-ncd(380)_", 0.1), "5-1-ncd(380)_0.10V_merged.dat")
        self.assertEqual(rt_mod.cycle_name("S", 0.5, 2), "S0.50V_1.dat")

    def test_line_format(self):
        self.assertEqual(rt_mod.data_line(23.691786, 165652.95), "2.3691786E+1\t1.6565295E+5")                # 기존 파일의 실제 줄
        self.assertEqual(rt_mod.data_line(112.34567, 0.5), "1.1234567E+2\t5.0000000E-1")
        self.assertEqual(rt_mod.HEADER.encode() + b"\r\n", b"Temp   Resistance   \r\n")

    def test_exponent_edges(self):
        f = rt_mod.fmt_e
        self.assertEqual(f(9.99999996), "1.0000000E+1")          # 반올림으로 지수가 올라감
        self.assertEqual(f(9.9999999), "9.9999999E+0")
        self.assertEqual(f(0.0), "0.0000000E+0")
        self.assertEqual(f(1.0), "1.0000000E+0")
        self.assertEqual(f(0.0123456789), "1.2345679E-2")
        self.assertEqual(f(-0.5), "-5.0000000E-1")
        self.assertEqual(f(1.5e-12), "1.5000000E-12")
        self.assertEqual(f(2.5e105), "2.5000000E+105")
        self.assertEqual(f(99999999.6), "1.0000000E+8")

    def test_read_dat_header_variants(self):
        d = tempfile.mkdtemp()
        a = os.path.join(d, "a.dat")
        open(a, "wb").write(b"Temp   Resistance   \r\n2.3691786E+1\t1.6565295E+5\r\n2.4E+1\t1.0E+5\r\n")
        m = os.path.join(d, "m.dat")
        open(m, "wb").write(b"2.3691786E+1\t1.6565295E+5\r\n-1.5E-1\t9.9E-1\r\n")
        self.assertEqual(rt_mod.read_dat(a), [(23.691786, 165652.95), (24.0, 100000.0)])
        self.assertEqual(rt_mod.read_dat(m), [(23.691786, 165652.95), (-0.15, 0.99)])

    def test_unique_path(self):
        d = tempfile.mkdtemp()
        p = os.path.join(d, "a.dat")
        self.assertEqual(rt_mod.unique_path(p), (p, False))
        open(p, "w").close()
        q, ch = rt_mod.unique_path(p)
        self.assertTrue(ch and q.endswith("a_1.dat"))
        open(q, "w").close()
        self.assertTrue(rt_mod.unique_path(p)[0].endswith("a_2.dat"))


class SavePathTest(unittest.TestCase):
    BASE = r"\\VanaM_NAS\VanaM_Measurement\VO2\VO2 data (NAS)\data"
    NOW = time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1))

    def test_nas_target_combinations(self):
        f, B = rt_mod.nas_target, self.BASE
        self.assertEqual(f(B, True, "6", self.NOW), B + r"\2026\20261109\6")              # 사용자가 쓰는 구조: 기본 폴더\연도\날짜\묶음
        self.assertEqual(f(B, True, "", self.NOW), B + r"\2026\20261109")                # 묶음 비우면 붙이지 않는다
        self.assertEqual(f(B, False, "", self.NOW), B)                                    # 날짜 자동 끔 = 기본 폴더 그대로
        self.assertEqual(f(B, False, "6", self.NOW), B + r"\6")
        self.assertEqual(f(B + chr(92), True, " 6 ", self.NOW), B + r"\2026\20261109\6")      # 끝 구분자 · 공백 정리
        self.assertEqual(f("", True, "6", self.NOW), "")                                  # 기본 폴더가 비면 PC 에만
        self.assertEqual(f(None, True, "6", self.NOW), "")
        self.assertEqual(f(r"D:\data", True, "", self.NOW), r"D:\data\2026\20261109")
        # 연말 경계: 날짜는 측정 시작 시각 기준
        self.assertTrue(f(B, True, "", time.mktime((2026, 12, 31, 23, 59, 59, 0, 0, -1))).endswith(r"\2026\20261231"))
        self.assertTrue(f(B, True, "", time.mktime((2027, 1, 1, 0, 0, 1, 0, 0, -1))).endswith(r"\2027\20270101"))

    def test_defaults_and_validation(self):
        d = sch.defaults()["rt"]
        self.assertEqual(d["file_path"], r"\\VanaM_NAS\VanaM_Measurement\VO2\VO2 data (NAS)\data")
        self.assertEqual((d["date_folder"], d["batch_folder"]), (True, ""))
        bad = sch.defaults()
        bad["rt"]["batch_folder"] = "a/b"
        self.assertIn("rt.batch_folder", sch.validate(bad)[1])
        bad["rt"]["batch_folder"] = ".."
        self.assertIn("rt.batch_folder", sch.validate(bad)[1])
        bad["rt"]["batch_folder"] = "6"
        self.assertNotIn("rt.batch_folder", sch.validate(bad)[1])

    def test_run_uses_nas_target(self):
        """측정이 NAS 로 복사하는 폴더 = nas_target(시작 날짜). PC 쪽 폴더는 그대로."""
        app = StubApp()
        base = tempfile.mkdtemp(prefix="nas_")
        rr = app.make({"max_temp": 36.0, "break_temp": 30.0, "precool_c": 26.0, "repeat": 1})
        rr.p["file_path"], rr.p["date_folder"], rr.p["batch_folder"] = base, True, "6"
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK)
        want = rt_mod.nas_target(base, True, "6", rr.t0)
        self.assertEqual(rr.nas_dir, want)
        self.assertIn("1-1_0.10V_0.dat", os.listdir(want))
        self.assertIn("1-1_0.10V_merged.dat", os.listdir(want))
        self.assertNotIn("control.csv", os.listdir(want))
        self.assertTrue(rr.run_dir.startswith(tempfile.gettempdir()) or "rt_unit_" in rr.run_dir)


class VisaKindTest(unittest.TestCase):
    def test_kind(self):
        self.assertEqual(inst.visa_kind("GPIB0::18::INSTR"), "GPIB")
        self.assertEqual(inst.visa_kind("USB0::0x05E6::0x2110::1234567::INSTR"), "USB")
        self.assertEqual(inst.visa_kind("TCPIP0::192.168.0.5::inst0::INSTR"), "LAN")
        self.assertEqual(inst.visa_kind("ASRL3::INSTR"), "시리얼")
        self.assertEqual(inst.visa_kind(""), "")
        self.assertEqual(inst.visa_kind("foo"), "")

    def test_list_shows_gpib_and_usb_and_direct_entry(self):
        app = StubApp()
        items, msg = inst.list_resources()
        self.assertTrue(any(i.startswith("GPIB") for i in items) and any(i.startswith("USB") for i in items), items)
        d = inst.K2450("GPIB0::6::INSTR")                  # 목록에 없는 주소를 직접 입력해도 연다
        d.open()
        self.assertTrue(d.is_open)
        self.assertEqual(inst.visa_kind(d.addr), "GPIB")


class GraphRangeTest(unittest.TestCase):
    """화면(JS)의 기본 범위: 저항 1E+1–1E+6, 온도 20–100 °C. 넘으면 넓힌다. node 로 frontend/js/rtutil.js 를 직접 시험."""

    def run_js(self, expr):
        import json as _json
        import shutil
        import subprocess
        node = shutil.which("node")
        if not node:
            self.skipTest("node 없음")
        src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend", "js", "rtutil.js")
        code = "const vm=require('vm'),fs=require('fs');const UI={};vm.runInNewContext(fs.readFileSync(process.argv[1],'utf8'),{UI,Math,Number,String,Date});console.log(JSON.stringify(%s));" % expr
        out = subprocess.run([node, "-e", code, src], capture_output=True, text=True, encoding="utf-8", timeout=30)
        self.assertEqual(out.returncode, 0, out.stderr)
        return _json.loads(out.stdout)

    def test_default_range(self):
        self.assertEqual(self.run_js("UI.rtRange([],[])"), {"x": [20, 100], "y": [10, 1000000]})
        self.assertEqual(self.run_js("UI.rtRange([25,60,90],[300000,200,5000])"), {"x": [20, 100], "y": [10, 1000000]})   # 범위 안이면 그대로

    def test_widen_when_exceeded(self):
        self.assertEqual(self.run_js("UI.rtRange([12,121],[5,2e7])"), {"x": [10, 125], "y": [1, 100000000]})
        self.assertEqual(self.run_js("UI.rtRange([null,50],[null,0,-1,500])"), {"x": [20, 100], "y": [10, 1000000]})        # 값 없음 · 0 이하 저항은 무시

    def test_nas_path_preview_matches_server(self):
        now = "new Date(2026,10,9,14,30)"
        base = r"\\VanaM_NAS\VanaM_Measurement\VO2\VO2 data (NAS)\data"
        for auto, batch in ((True, "6"), (True, ""), (False, ""), (False, "6")):
            js = self.run_js("UI.nasPath(%s,%s,%s,%s)" % (_json_s(base), "true" if auto else "false", _json_s(batch), now))
            self.assertEqual(js, rt_mod.nas_target(base, auto, batch, time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1))), (auto, batch))
        self.assertEqual(self.run_js("UI.nasPath('',true,'6',%s)" % now), "")


def _json_s(x):
    import json as _json
    return _json.dumps(x)


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
        for n in ("1-1_1.00V_0.dat", "1-1_1.00V_1.dat", "1-1_1.00V_merged.dat", "control.csv", "rt.json"):
            self.assertIn(n, names)
        c1 = self.read_dat(rr.run_dir, "1-1_1.00V_0.dat").split("\r\n")
        self.assertEqual(c1[0], "Temp   Resistance   ")
        raw = open(os.path.join(rr.run_dir, "1-1_1.00V_0.dat"), "rb").read()
        self.assertTrue(raw.startswith(b"Temp   Resistance   \r\n") and raw.endswith(b"\r\n") and b"\t" in raw)
        m3 = open(os.path.join(rr.run_dir, "1-1_1.00V_merged.dat"), "rb").read().split(b"\r\n")[:2]
        c3 = raw.split(b"\r\n")[1:3]
        self.assertEqual(m3, c3, "merged 는 머리글 없이 첫 회차 줄부터")
        pts = [tuple(map(float, ln.split("\t"))) for ln in c1[1:] if ln]
        self.assertGreater(len(pts), 100)
        self.assertRegex(c1[1], r"^-?\d\.\d{7}E[+-]\d{1,3}\t-?\d\.\d{7}E[+-]\d{1,3}$")
        merged = self.read_dat(rr.run_dir, "1-1_1.00V_merged.dat").split("\r\n")
        n1 = len(pts)
        n2 = len([x for x in self.read_dat(rr.run_dir, "1-1_1.00V_1.dat").split("\r\n")[1:] if x])
        self.assertEqual(len([x for x in merged if x]), n1 + n2)
        self.assertNotIn("Temp", merged[0])
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

    def test_new_defaults_run_and_open_circuit(self):
        """새 기본값(0.1 V · 한도 0.1 A · REAR)으로 끝까지 돈다. 앞면 단자 설정이면 열린 회로 경고 + control.csv 표시, 측정은 계속."""
        app = StubApp()
        rr = app.make({"max_temp": 40.0, "break_temp": 30.0, "precool_c": 26.0})
        self.assertEqual((rr.p["volt"], rr.p["ilim_a"], rr.p["repeat"]), (0.1, 0.1, 1))
        rr.run()
        self.assertEqual((rr.status, rr.message), (rt_mod.OK, ""))
        self.assertFalse(any("연결 없음" in w for w in rr.warns))
        self.assertEqual(app.lab.dev["2450"].err, [])
        names = sorted(os.listdir(rr.run_dir))
        self.assertIn("1-1_0.10V_0.dat", names)
        self.assertNotIn("1-1_0.10V_1.dat", names)
        for c in app.k2450.cmds:                                    # 고른 범위가 한도 규칙을 지킨다
            if c.startswith(":SENS:CURR:RANG ") and "AUTO" not in c:
                self.assertTrue(inst.limit_ok(float(c.split()[-1]), 0.1), c)
        app2 = StubApp()
        rr2 = app2.make({"max_temp": 40.0, "break_temp": 30.0, "precool_c": 26.0, "terminal": "FRONT"})
        rr2.run()
        self.assertEqual(rr2.status, rt_mod.OK)
        self.assertTrue(any("연결 없음" in w for w in rr2.warns), rr2.warns)
        self.assertEqual(len([w for w in rr2.warns if "연결 없음" in w]), 1, "경고는 한 번")
        ctl = open(os.path.join(rr2.run_dir, "control.csv"), encoding="utf-8-sig").read().splitlines()
        self.assertTrue(ctl[0].endswith("열린 회로"))
        self.assertTrue(any(x.endswith(",1") for x in ctl[1:]))
        self.assertTrue(all(x.split(",")[-1] in ("0", "1") for x in ctl[1:]))

    def test_ilim_trip_warns_continues(self):
        app = StubApp()
        app.lab.fault["ilim"] = True
        rr = app.make(short_params(max_temp=45.0, break_temp=30.0, repeat=1))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK)
        self.assertTrue(any("전류 한도" in w for w in rr.warns))
        self.assertSafe(app, rr)

    def test_no_rise_when_thermocouple_not_on_plate(self):
        app = StubApp()
        app.lab.pin_down = lambda: False                 # 핀이 들려 열전대가 공기 온도를 잰다(판은 오르는데 값은 안 오름)
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0, repeat=1))
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertTrue("안 오름" in rr.message or "시간 한도" in rr.message, rr.message)
        self.assertSafe(app, rr)

    def test_exception_in_setup_still_finishes(self):
        app = StubApp()
        rr = app.make(short_params())
        rr.run_dir = os.path.join(tempfile.mkdtemp(), "f")
        open(rr.run_dir, "w").write("파일")               # 폴더를 못 만든다
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertTrue(rr.finished.is_set())
        self.assertIn(rr.state, ("error",))
        self.assertSafe(app, rr)

    def test_overflow_jumps_range_and_trip_not_recorded(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=85.0, break_temp=25.0, repeat=1))
        rr.run()
        rngs = [float(c.split()[-1]) for c in app.k2450.cmds if c.startswith(":SENS:CURR:RANG ") and "AUTO" not in c]
        # 범위가 3e5 Ω(3 µA) → 2e2 Ω(5 mA)로 갈 때 한 번에 한도(10 mA) 범위로 올라간다
        self.assertIn(1e-2, rngs)
        for a, b in zip(rngs, rngs[1:]):
            self.assertFalse(b > a * 10.5 and b < 1e-2, (a, b))

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
        self.assertEqual((d["precool_c"], d["max_temp"], d["break_temp"], d["repeat"], d["volt"], d["filter_n"]), (24.5, 85.0, 25.0, 1, 0.1, 10))
        self.assertEqual((d["k2450_addr"], d["file_name"], d["nplc"], d["start_range"], d["four_wire"], d["terminal"]), ("GPIB0::18::INSTR", "1-1_", 1.0, 0.001, True, "REAR"))
        self.assertEqual((d["ilim_a"], d["open_a"]), (0.1, 1e-9))
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
