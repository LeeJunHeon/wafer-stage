"""unit_rt.py - R–T 측정 단위 시험 (python tools/unit_rt.py [-v] [이름일부]). 장비 · 포트 · 카메라 · VISA 를 열지 않는다(가짜 장비 + 수동 시계).

SCPI 문자열 · 범위 단계 변경 · 저항 계산 · 파일 이름 · 형식 · 히트건 켜고 끄기 · 목표 온도선 추종 · 안전 경로(전부 히트건 꺼짐 + 출력 꺼짐).
"""
import contextlib
import json
import os
import sys
import tempfile
import threading
import time
import unittest

os.environ.setdefault("WAFER2_DATA", tempfile.mkdtemp(prefix="unit_rt_data_"))     # 로그 · 설정 직전본이 실제 데이터 폴더에 쌓이지 않게

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

    alarms = None

    def set_alarm(self, text, nxt="", force=False):
        if self.alarms is None:
            self.alarms = []
        self.alarms.append(text)

    def cancel_text(self, gen):
        return "비상정지로 중단"

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
        self.assertIn("1-1_1.00V_0.dat", os.listdir(want))
        self.assertIn("1-1_1.00V_merged.dat", os.listdir(want))
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
            srv = rt_mod.nas_target(base, auto, batch, time.mktime((2026, 11, 9, 14, 30, 0, 0, 0, -1)))
            self.assertEqual(js, srv.replace(os.sep, "\\"), (auto, batch))        # 서버는 운영체제 구분자(Windows = '\\'), 화면 미리보기는 늘 '\\'(Windows 기준)
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
        """새 기본값(1 V · 한도 0.1 A · REAR · 평균 10 · 4단자)으로 끝까지 돈다. 앞면 단자 설정이면 열린 회로 경고 + control.csv 표시, 측정은 계속."""
        app = StubApp()
        rr = app.make({"max_temp": 40.0, "break_temp": 30.0, "precool_c": 26.0})
        self.assertEqual((rr.p["volt"], rr.p["ilim_a"], rr.p["repeat"], rr.p["filter_n"], rr.p["four_wire"]), (1.0, 0.1, 1, 10, True))
        rr.run()
        self.assertEqual((rr.status, rr.message), (rt_mod.OK, ""))
        self.assertFalse(any("연결 없음" in w for w in rr.warns))
        self.assertEqual(app.lab.dev["2450"].err, [])
        names = sorted(os.listdir(rr.run_dir))
        self.assertIn("1-1_1.00V_0.dat", names)
        self.assertNotIn("1-1_1.00V_1.dat", names)
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
        # 2026-11 장비 PC 기준(단독 스크립트 measure_test.py 와 같은 1 V · 4단자 · 평균 10): 예비 냉각 0 = 건너뜀 · 냉각 끝 30 · 비상 차단 110 고정
        self.assertEqual((d["precool_c"], d["max_temp"], d["break_temp"], d["repeat"], d["volt"], d["filter_n"]), (0.0, 85.0, 30.0, 1, 1.0, 10))
        self.assertEqual((d["emergency_c"], d["heat_rate"], d["cool_stall_s"], d["cool_stall_c"]), (110.0, 0.0, 180.0, 0.3))
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


class VisaErrorQueueTest(unittest.TestCase):
    """A1: SYST:ERR? 응답 앞 번호(쉼표 앞 정수)가 0 이면 오류 없음 - 2110 '+0, "No error"' · 2450 '0,"No error;0;0"'."""

    def test_error_code_formats(self):
        self.assertEqual(inst.error_code('+0, "No error"'), 0)
        self.assertEqual(inst.error_code('0,"No error;0;0"'), 0)
        self.assertEqual(inst.error_code('-113,"Undefined header"'), -113)
        self.assertEqual(inst.error_code('+100,"x"'), 100)
        self.assertIsNone(inst.error_code("garbage"))

    def test_drain_plus_zero_is_empty(self):
        class D:
            def __init__(self, rs):
                self.rs = list(rs)

            def query(self, c):
                return self.rs.pop(0)
        self.assertEqual(inst._drain_errors(D(['+0, "No error"'])), [])                 # 예전: "+0" 은 '0' 으로 시작하지 않아 오류로 읽었다
        self.assertEqual(inst._drain_errors(D(['0,"No error;0;0"'])), [])
        self.assertEqual(inst._drain_errors(D(['-113,"Undefined header"', '+0, "No error"'])), ['-113,"Undefined header"'])

    def test_k2110_configure_with_real_reply(self):
        app = StubApp()
        app.k2110.configure("K", "REAL")                                 # 가짜 K2110 도 실제처럼 '+0, "No error"' 로 답한다
        self.assertEqual(app.k2110.cmds.count("SYST:ERR?"), 1)
        app.k2110.write("SENS:TEMP:TRAN TC")
        with self.assertRaises(inst.VisaError) as c:
            app.k2110.check_errors()
        self.assertIn("-113", str(c.exception))


class VisaAddrTest(unittest.TestCase):
    """A2: 목록에서 시리얼(ASRL) 제외 · 자동 찾기(0x2110 · 0x2450 USB · 없으면 GPIB0::18) · 일련번호 없는 USB 형식."""

    def test_list_excludes_serial(self):
        StubApp()
        items, _ = inst.list_resources()
        self.assertTrue(items)
        self.assertFalse(any(i.upper().startswith("ASRL") for i in items), items)

    def test_find_addr(self):
        items = ["USB0::0x05E6::0x2110::8017862::INSTR", "GPIB0::18::INSTR", "GPIB0::6::INSTR"]
        self.assertEqual(inst.find_addr("k2110", items), items[0])
        self.assertEqual(inst.find_addr("k2450", items), "GPIB0::18::INSTR")
        self.assertEqual(inst.find_addr("k2450", items + ["USB0::0x05E6::0x2450::04584338::INSTR"]), "USB0::0x05E6::0x2450::04584338::INSTR")
        self.assertIsNone(inst.find_addr("k2110", ["ASRL4::INSTR", "GPIB0::18::INSTR"]))
        self.assertIsNone(inst.find_addr("k2450", ["ASRL5::INSTR", "USB0::0x05E6::0x2110::1::INSTR"]))
        self.assertTrue(inst.usb_no_serial("USB0::0x05E6::0x2110::INSTR"))
        self.assertFalse(inst.usb_no_serial("USB0::0x05E6::0x2110::8017862::INSTR"))
        self.assertFalse(inst.usb_no_serial("GPIB0::18::INSTR"))


class VisaLogTest(unittest.TestCase):
    """A3: <데이터>/logs/YYYYMMDD_visa.log 한 줄씩(시각 · 장비 · 주소 · 명령 · 응답 · ms · 예외) + 크기 상한."""

    def test_log_lines_and_cap(self):
        d = tempfile.mkdtemp(prefix="visalog_")
        old = (inst.LOG_DIR, inst.VISA_LOG_MAX)
        inst.LOG_DIR = d
        try:
            app = StubApp()
            app.k2110.configure("K", "REAL")
            app.k2110.read_temp()
            fp = inst.visa_log_path()
            self.assertTrue(os.path.basename(fp).endswith("_visa.log") and os.path.basename(fp)[:8].isdigit())
            txt = open(fp, encoding="utf-8").read()
            self.assertRegex(txt, r"\d\d:\d\d:\d\d\.\d{3} K2110 USB0::0x05E6::0x2110::INSTR write \*RST \(\d+\.\d ms\)")
            self.assertIn('query SYST:ERR? -> +0, "No error"', txt)
            self.assertRegex(txt, r"K2110 \S+ query READ\? -> [+-]\d")
            app.lab.fault["disconnect"] = True
            with self.assertRaises(inst.VisaError):
                app.k2110.read_temp()
            app.lab.fault["disconnect"] = False
            self.assertIn("query READ? (", open(fp, encoding="utf-8").read().splitlines()[-1])
            self.assertIn("!! TimeoutError", open(fp, encoding="utf-8").read().splitlines()[-1])
            inst.VISA_LOG_MAX = 3000
            for _ in range(80):
                app.k2110.query("*IDN?")
            self.assertTrue(os.path.exists(fp[:-4] + ".1.log"))
            self.assertLess(os.path.getsize(fp), 3000 + 400)
        finally:
            inst.LOG_DIR, inst.VISA_LOG_MAX = old


class SettingsMigrateTest(unittest.TestCase):
    """A4 · B1: 예전 기본값과 정확히 같은 값만 새 기본값으로(한 번 - settings_rev 2), 사람이 바꾼 값은 그대로. B2: 히트건 · 구역 한 번에 저장 · 맞바꾸기."""

    def _old(self):
        import copy
        from core import zones as zones_mod
        v = sch.defaults()
        v.pop("settings_rev", None)
        v["rt"].update({"volt": 0.1, "break_temp": 25.0, "emergency_c": None, "precool_c": 24.5})
        v["heater"]["relay"] = 10
        v["cooler"]["zones"] = zones_mod.default_zones(relay0=1)
        v["relay"]["items"] = copy.deepcopy(sch._relays_default_v1())
        return v

    def _load(self, raw):
        import store
        fp = os.path.join(tempfile.mkdtemp(prefix="set_"), "settings.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False)
        return store.Settings(fp), fp

    def test_old_defaults_become_new(self):
        cfg, fp = self._load(self._old())
        g = cfg.get
        self.assertEqual((g("rt.volt"), g("rt.break_temp"), g("rt.emergency_c"), g("rt.precool_c")), (1.0, 30.0, 110.0, 0.0))
        self.assertEqual(g("heater.relay"), 1)
        self.assertEqual([c["relay"] for c in g("cooler.zones")["cells"]], list(range(2, 11)))
        self.assertEqual(g("relay.items")[0]["name"], "히트건")
        self.assertEqual(g("relay.items")[1]["name"], "쿨러 1")
        on_disk = json.load(open(fp, encoding="utf-8"))
        self.assertEqual(on_disk["settings_rev"], 2)                    # 바로 저장(한 번만)
        self.assertEqual(on_disk["heater"]["relay"], 1)
        self.assertTrue(any("예전 기본값" in n for n in cfg.migrate_notes))
        REPORT["설정 이전(예전 기본값 그대로)"] = " / ".join(cfg.migrate_notes)

    def test_user_values_kept(self):
        v = self._old()
        v["rt"]["volt"] = 0.5
        v["rt"]["break_temp"] = 27.0
        cells = v["cooler"]["zones"]["cells"]
        cells[0]["relay"], cells[1]["relay"] = cells[1]["relay"], cells[0]["relay"]    # 사람이 구역을 바꿔 둠
        cfg, _fp = self._load(v)
        self.assertEqual((cfg.get("rt.volt"), cfg.get("rt.break_temp"), cfg.get("rt.emergency_c")), (0.5, 27.0, 110.0))
        self.assertEqual(cfg.get("heater.relay"), 10)                   # 구역을 바꿔 둔 사람의 히트건은 그대로(구역과 겹치지 않게)
        self.assertEqual([c["relay"] for c in cfg.get("cooler.zones")["cells"]][:2], [2, 1])
        self.assertTrue(any("이전 안 함" in n for n in cfg.migrate_notes))

    def test_rev2_not_migrated_again(self):
        v = self._old()
        v["settings_rev"] = 2
        v["rt"]["emergency_c"] = 110.0
        cfg, _fp = self._load(v)
        self.assertEqual((cfg.get("rt.volt"), cfg.get("heater.relay")), (0.1, 10))   # 판 2 의 0.1 V 는 사람이 고른 값

    def test_joint_heater_and_zones_save(self):
        """B2: R10 → R1 + 구역 R1~R9 → R2~R10 을 한 번에 저장하면 최종 상태 하나로 검증해 통과(따로따로는 서로 막는다)."""
        from core import zones as zones_mod
        v = self._old()
        v["settings_rev"] = 2
        v["rt"]["emergency_c"] = 110.0
        cfg, _fp = self._load(v)
        ok, errs, _w = cfg.save({"heater.relay": 1})
        self.assertFalse(ok)
        self.assertIn("cooler.zones", errs)
        ok, errs, _w = cfg.save({"heater.relay": 1, "cooler.zones": zones_mod.default_zones()})
        self.assertTrue(ok, errs)
        self.assertEqual(cfg.get("heater.relay"), 1)

    def test_swap_heater_zone(self):
        from core import zones as zones_mod
        z, rl, cell = sch.swap_heater_zone(zones_mod.default_zones(relay0=1), sch._relays_default_v1(), 10, 1)
        self.assertEqual(cell, 1)
        self.assertEqual(z["cells"][0]["relay"], 10)
        self.assertEqual((rl[0]["name"], rl[9]["name"]), ("히트건", "쿨러 1"))
        v = sch.defaults()
        v["heater"]["relay"], v["cooler"]["zones"], v["relay"]["items"] = 1, z, rl
        self.assertEqual(sch.validate(v)[1], {})

    def test_emergency_vs_max(self):
        v = sch.defaults()
        v["rt"]["max_temp"] = 110.0
        errs = sch.validate(v)[1]
        self.assertIn("rt.max_temp", errs)
        self.assertIn("rt.emergency_c", errs)
        p = rt_mod.params(StubCfg({"rt.max_temp": 120.0, "rt.emergency_c": 110.0}))
        self.assertIn("비상 차단", rt_mod.params_block(p))

    def test_no_syntax_warning(self):
        """A7: settings_schema 에 잘못된 escape('\\/') 가 없다(시작 시 SyntaxWarning 없음)."""
        import warnings
        src = open(os.path.join(ROOT, "backend", "settings_schema.py"), encoding="utf-8").read()
        with warnings.catch_warnings():
            warnings.simplefilter("error")
            compile(src, "settings_schema.py", "exec")


class StubStage:
    """B4 시험: relay_status 가 power.owners 를 그대로 돌려주다가 flip 이 정해지면 그 릴레이를 뒤집는다."""

    def __init__(self, app):
        self.app = app
        self.flip = None
        self.calls = 0
        self.all_off = 0

    def relay_status(self):
        self.calls += 1
        bits = [(i + 1) in self.app.power.owners for i in range(10)]
        if self.flip is not None and self.calls >= self.flip[0]:
            bits[self.flip[1] - 1] = not bits[self.flip[1] - 1]
        return bits

    def relay_all_off(self):
        self.all_off += 1
        self.app.power.owners.clear()
        return [False] * 10


class CoolStallTest(unittest.TestCase):
    """A5: 냉각 · 예비 냉각이 cool_stall_s 동안 cool_stall_c 도 안 내려가면 '냉각 멈춤 · 방 온도에 닿음' → 그 회차 정상 종료(기록 유지)."""

    def test_cooling_stall_ends_cycle_ok(self):
        app = StubApp()
        app.lab.t_cool = 27.0                                     # 쿨러로도 27 °C 까지만(방 온도) - 냉각 끝 25 °C 에 못 닿는다
        rr = app.make(short_params(max_temp=40.0, break_temp=25.0, repeat=1, precool_c=0.0, cool_stall_s=60.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        self.assertIn("냉각 1", rr.stalls)
        self.assertTrue(any("냉각 멈춤" in w for w in rr.warns))
        rows = open(os.path.join(rr.run_dir, "1-1_1.00V_0.dat"), encoding="utf-8").read().splitlines()
        self.assertGreater(len(rows), 20)
        self.assertNotIn(10, app.power.owners)

    def test_precool_stall_continues_and_zero_skips(self):
        app = StubApp()
        app.lab.t_cool = 27.0
        rr = app.make(short_params(max_temp=36.0, break_temp=28.0, repeat=1, precool_c=24.5, cool_stall_s=60.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        self.assertIn("예비 냉각", rr.stalls)
        app2 = StubApp()
        rr2 = app2.make(short_params(max_temp=36.0, break_temp=30.0, repeat=1, precool_c=0.0))
        rr2.run()
        self.assertEqual(rr2.status, rt_mod.OK, rr2.message)
        self.assertFalse(any(p["ph"] == "예비 냉각" for p in rr2.points))   # 0 = 예비 냉각 건너뜀


class FastRiseTest(unittest.TestCase):
    """A6: 가열 중 5 °C/s 보다 빨리 오르면 한 번만 경보(띠 · 로그 · 중단 안 함) + 히트건 끈 뒤 오버슈트 기록."""

    def test_fast_rise_warns_once(self):
        app = StubApp()
        app.lab.tau, app.lab.tau_sensor = 3.0, 0.3                 # 열전대가 히트건 바람을 직접 맞음(빠른 상승)
        rr = app.make(short_params(max_temp=60.0, break_temp=40.0, repeat=2, precool_c=0.0))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        self.assertEqual(len([w for w in rr.warns if rt_mod.FAST_RISE_TEXT in w]), 1)
        self.assertEqual(app.alarms, [rt_mod.FAST_RISE_TEXT])
        self.assertIsNotNone(rr.overshoot)
        meta = json.load(open(os.path.join(rr.run_dir, "rt.json"), encoding="utf-8"))
        self.assertIn("overshoot", meta)
        REPORT["빠른 가열 시험 · 끈 뒤 오버슈트"] = "%+.1f °C" % rr.overshoot

    def test_normal_rise_no_warning(self):
        app = StubApp()
        rr = app.make(short_params(max_temp=40.0, break_temp=30.0, repeat=1, precool_c=0.0))
        rr.run()
        self.assertFalse(rr._fast_warned)


class RelayStateTest(unittest.TestCase):
    """B4: R–T 중 루프마다 펌웨어 RS 와 있어야 할 상태(쿨러 켜짐 · 히트건 단계대로 · 나머지 꺼짐)를 비교 - 다르면 중단."""

    def test_mismatch_stops(self):
        app = StubApp()
        app.stage = StubStage(app)
        app.stage.flip = (40, 2)                                  # 40 번째 읽기부터 쿨러 R2 가 꺼져 있다고 답한다(감시가 끔 · 재부팅)
        rr = app.make(short_params(max_temp=40.0, break_temp=30.0, repeat=1, precool_c=0.0), relays=(2,))
        rr.run()
        self.assertEqual(rr.status, rt_mod.ERROR)
        self.assertIn("릴레이 상태 다름", rr.message)
        self.assertIn("R2 꺼짐", rr.message)
        self.assertNotIn(10, app.power.owners)
        self.assertFalse(app.lab.out_on)
        self.assertGreaterEqual(app.stage.all_off, 1)               # 오류 끝: r all 0 으로 꺼짐 확인

    def test_match_runs_to_end(self):
        app = StubApp()
        app.stage = StubStage(app)
        rr = app.make(short_params(max_temp=40.0, break_temp=30.0, repeat=1, precool_c=0.0), relays=(2,))
        rr.run()
        self.assertEqual(rr.status, rt_mod.OK, rr.message)
        self.assertGreater(app.stage.calls, 20)

    def test_fake_fw_cut_link_during_cooling(self):
        """fake_v11 cut_link: 냉각 중 4 초 끊김 → 중단 · 히트건 · 쿨러 전부 꺼짐(펌웨어 RS) · K2450 출력 꺼짐."""
        import fake_v11
        from core import power as power_mod
        from core import stage as stage_mod
        fw = fake_v11.FakeV11(time_scale=0.05)
        try:
            st = stage_mod.Stage(dry=False)
            st.ser = fw
            st.fw_version = "V11"
            st._log = lambda *a: None
            app = StubApp()
            app.stage = st
            app.power = power_mod.Power(st, heater_n=10)
            clk = app.clock
            app.lab.sleep = lambda s: (clk.advance(s), time.sleep(0.004))
            rr = app.make(short_params(max_temp=40.0, break_temp=22.0, repeat=1, precool_c=0.0), relays=(2,))
            th = threading.Thread(target=rr.run, daemon=True)
            th.start()
            t_end = time.time() + 60
            while rr.phase != "냉각" and time.time() < t_end and not rr.finished.is_set():
                time.sleep(0.02)
            self.assertEqual(rr.phase, "냉각")
            self.assertTrue(fw.relay[1], "쿨러 R2 켜짐")
            fw.cut_link(4.0)
            t0 = time.time()
            self.assertTrue(rr.finished.wait(30))
            REPORT["냉각 중 4 s 끊김 → 측정 끝까지"] = "%.1f s" % (time.time() - t0)
            self.assertEqual(rr.status, rt_mod.ERROR)
            self.assertIn("통신", rr.message)
            if rr.relay_off_failed:
                self.assertTrue(rt_mod.relays_off_retry(app, secs=15, every=0.3))
            self.assertEqual(fw.relay, [False] * 10)
            self.assertFalse(app.lab.out_on)
        finally:
            fw.close()


class GuardTest(unittest.TestCase):
    """B5(확인): VISA 가 막혀도(K2110 응답 8 s 지연) 독립 감시가 히트건을 끈다 - 걸린 시간 보고."""

    def test_guard_turns_heater_off_when_visa_hangs(self):
        app = StubApp()
        clk = app.clock
        app.lab.sleep = lambda s: (clk.advance(s), time.sleep(0.01))
        rr = app.make(short_params(max_temp=80.0, break_temp=30.0, repeat=1, precool_c=0.0))
        th = threading.Thread(target=rr.run, daemon=True)
        th.start()
        t_end = time.time() + 20
        while not (rr.phase == "가열" and 10 in app.power.owners) and time.time() < t_end:
            time.sleep(0.01)
        self.assertIn(10, app.power.owners)
        app.lab.fault["slow"] = 8.0
        t0 = time.monotonic()
        while 10 in app.power.owners and time.monotonic() - t0 < 15:
            time.sleep(0.05)
        dt = time.monotonic() - t0
        app.lab.fault["slow"] = 0.0
        rr.stop()
        self.assertTrue(rr.finished.wait(40))
        REPORT["감시 · K2110 8 s 지연 → 히트건 꺼짐"] = "%.1f s (온도 갱신 한도 %g s + 여유 2 s)" % (dt, rr.p["stale_s"])
        self.assertLess(dt, float(rr.p["stale_s"]) + 2.0 + 1.0)
        self.assertNotIn(10, app.power.owners)


class ManualModeTest(unittest.TestCase):
    """수동 측정: 이동 계층을 부르지 않는다(Z 올림 없음) · 자동과 같은 파일 형식 · 연속 측정은 control.csv 만 · 끝나면 전부 꺼짐."""

    def test_manual_rt_files_and_no_motion(self):
        app = StubApp()
        app.motion = None                                         # 이동 계층 없이도 돈다
        app.stage = StubStage(app)
        p = rt_mod.params(app.cfg)
        p.update(short_params(max_temp=40.0, break_temp=30.0, repeat=2, precool_c=0.0))
        p["file_path"] = ""
        rr = rt_mod.RTRun(app, p, "m1_", run_dir=tempfile.mkdtemp(prefix="man_"), relays=[2], mode="manual_rt")
        self.assertFalse(rr.z_top_end)
        rr.run()
        self.assertEqual((rr.status, rr.message), (rt_mod.OK, ""))
        names = sorted(os.listdir(rr.run_dir))
        for n in ("m1_1.00V_0.dat", "m1_1.00V_1.dat", "m1_1.00V_merged.dat", "control.csv", "rt.json"):
            self.assertIn(n, names)
        raw = open(os.path.join(rr.run_dir, "m1_1.00V_0.dat"), "rb").read()
        self.assertTrue(raw.startswith(b"Temp   Resistance   \r\n") and raw.endswith(b"\r\n"))
        n0 = len(raw.split(b"\r\n")) - 2
        n1 = len(open(os.path.join(rr.run_dir, "m1_1.00V_1.dat"), "rb").read().split(b"\r\n")) - 2
        nm = len(open(os.path.join(rr.run_dir, "m1_1.00V_merged.dat"), "rb").read().split(b"\r\n")) - 1
        self.assertEqual(nm, n0 + n1)
        ctl = open(os.path.join(rr.run_dir, "control.csv"), encoding="utf-8-sig").read().splitlines()
        self.assertEqual(ctl[0] + "\n", rt_mod.MANUAL_CTL_HEAD)
        self.assertEqual(len(ctl[1].split(",")), 11)
        self.assertEqual(app.power.owners, {})
        self.assertGreaterEqual(app.stage.all_off, 1)              # 끝: r all 0 으로 꺼짐 확인
        self.assertEqual(json.load(open(os.path.join(rr.run_dir, "rt.json"), encoding="utf-8"))["kind"], "수동")

    def test_manual_continuous_stop(self):
        app = StubApp()
        app.motion = None
        app.power = None                                          # 스테이지 없이 연속 측정(릴레이 안 씀)
        app.lab.heater_fn = app.lab.cooler_fn = lambda: False
        clk = app.clock
        app.lab.sleep = lambda s: (clk.advance(s), time.sleep(0.002))
        p = rt_mod.params(app.cfg)
        p["file_path"] = ""
        rr = rt_mod.RTRun(app, p, "c1_", run_dir=tempfile.mkdtemp(prefix="man_"), mode="manual_cont")
        th = threading.Thread(target=rr.run, daemon=True)
        th.start()
        t_end = time.time() + 10
        while len(rr.points) < 30 and time.time() < t_end:
            time.sleep(0.01)
        rr.stop()
        self.assertTrue(rr.finished.wait(10))
        self.assertEqual(rr.status, rt_mod.STOPPED)
        self.assertEqual(sorted(os.listdir(rr.run_dir)), ["control.csv", "rt.json"])
        self.assertFalse(app.lab.out_on)
        self.assertTrue(all(pt["R"] is not None for pt in rr.points[2:]))


def contact_env(samples, plate_z=21.0, cfg=None, lag_hook=None):
    """접촉 찾기 시험 환경: dry 스테이지 + 진짜 Motion + 가짜 K2450(스테이지 실제 X · Y · Z 를 보고 답함)."""
    from core import motion as motion_mod
    from core import stage as stage_mod
    stage_mod.DRY_MOVE_S = 0.0
    st = stage_mod.Stage(dry=True)
    st.open()
    sent = []
    orig = st._write

    def rec(cmd):
        orig(cmd)
        sent.append(cmd)
    st._write = rec
    m = motion_mod.Motion(st)
    m.sync_from_status()
    st.write_guard = m._guard
    over = {"contact.sense": True, "contact_z_mm": 20.0}
    over.update(cfg or {})
    app = StubApp(over=over)
    app.motion, app.stage = m, st
    app.lab.probe = {"plate_z": plate_z, "rc": 2.0e3, "samples": samples}

    def pos():
        p_ = (st._dry_xy["x"], st._dry_xy["y"], st._dry_xy["z"])
        if lag_hook:
            lag_hook(p_)
        return p_
    app.lab.pos_fn = pos
    app._inst_off_retry = lambda *a, **k: None
    return app, m, st, sent


def mz_values(sent):
    return [float(c.split()[1]) for c in sent if c.startswith("mz ")]


class ContactFindTest(unittest.TestCase):
    """2부: 전기로 접촉 확인하며 핀 내리기(contact.find · Motion.probe_down · 가짜 K2450 접촉 흉내)."""

    S3 = [{"x": 60.0, "y": 60.0, "half": 2.5, "z": 20.0, "ohm": 1.0e6}, {"x": 100.0, "y": 60.0, "half": 2.5, "z": 20.2, "ohm": 3.0e6},
          {"x": 140.0, "y": 60.0, "half": 2.5, "z": 19.8, "ohm": 8.0e5}]

    def find_at(self, app, m, x, y, **kw):
        import contact as contact_mod
        m.move_xy(x, y)
        return contact_mod.find(app, m.gen, mode=kw.pop("mode", "height"), z_ref=kw.pop("z_ref", 20.0), **kw)

    def test_three_surfaces(self):
        """표면 높이가 서로 다른 샘플 셋(±0.2 mm): 첫 접촉 Z = 표면 ±1 스텝, 최종 Z = 첫 접촉 + 더 누름."""
        app, m, st, sent = contact_env(self.S3)
        out = []
        for sm in self.S3:
            r = self.find_at(app, m, sm["x"], sm["y"])
            self.assertLessEqual(abs(r["first_z"] - sm["z"]), 0.025 + 1e-9, r)
            self.assertGreaterEqual(r["first_z"], sm["z"] - 1e-9)
            self.assertAlmostEqual(r["final_z"], r["first_z"] + 0.05, places=6)
            self.assertTrue(r["r4"] < r["r2"])
            self.assertAlmostEqual(r["r4"], sm["ohm"], delta=sm["ohm"] * 0.01)
            out.append("%.3f/%.3f" % (sm["z"], r["first_z"]))
        REPORT["접촉 찾기 · 표면/첫 접촉"] = " · ".join(out)
        self.assertFalse(app.lab.out_on, "끝나면 K2450 출력 꺼짐")

    def test_two_pins_first_then_four(self):
        """FORCE 두 핀만 먼저 닿음(SENSE 는 0.1 mm 더 내려가야): 4단자 확인에서 과전압 보호 · 이상값 → 더 내려 네 핀이 닿은 뒤 성공."""
        sm = [dict(self.S3[0], lag4=0.1)]
        app, m, st, sent = contact_env(sm)
        r = self.find_at(app, m, 60.0, 60.0)
        self.assertLessEqual(abs(r["first_z"] - 20.0), 0.025 + 1e-9)
        self.assertGreaterEqual(r["final_z"], 20.1 - 1e-9)
        self.assertTrue(any(":ovp" in str(p_[1]) for p_ in r["points"]), "감지 핀이 떠 있을 때 과전압 보호가 걸렸음")
        self.assertTrue(r["r4"] < r["r2"])

    def test_no_contact_stops_at_limit(self):
        """표면이 한도(저장 + 0.5)보다 아래: 한도에서 멈춤 · Z 맨 위 · 실패 · 한도 아래로 내려간 펄스 0."""
        import contact as contact_mod
        app, m, st, sent = contact_env([dict(self.S3[0], z=20.7)])
        with self.assertRaises(contact_mod.ContactFail) as c:
            self.find_at(app, m, 60.0, 60.0)
        self.assertEqual(c.exception.code, "none")
        self.assertLessEqual(max(mz_values(sent)), 20.5 + 1e-9)
        self.assertEqual(st._dry_xy["z"], 0.0)
        self.assertFalse(app.lab.out_on)

    def test_metal_plate_short(self):
        """샘플 밖(알루미늄 판): 단락으로 멈춤 · Z 맨 위 · 실패."""
        import contact as contact_mod
        app, m, st, sent = contact_env(self.S3, plate_z=20.05)
        with self.assertRaises(contact_mod.ContactFail) as c:
            self.find_at(app, m, 30.0, 30.0)
        self.assertEqual(c.exception.code, "short")
        self.assertIn("금속", str(c.exception))
        self.assertEqual(st._dry_xy["z"], 0.0)
        self.assertLessEqual(max(mz_values(sent)), 20.05 + 0.025 + 1e-9)
        self.assertFalse(app.lab.out_on)

    def test_k2450_lost_mid_search(self):
        """찾는 중 K2450 끊김: 더 내리지 않고 Z 맨 위 · 실패."""
        import contact as contact_mod
        box = {}

        def hook(p_):
            if p_[2] >= 19.85 and "z" not in box:
                box["z"] = p_[2]
                app.lab.fault["disconnect"] = True
        app, m, st, sent = contact_env(self.S3, lag_hook=hook)
        with self.assertRaises(contact_mod.ContactFail) as c:
            self.find_at(app, m, 60.0, 60.0)
        self.assertEqual(c.exception.code, "inst")
        deep = [z for z in mz_values(sent) if z > box["z"] + 1e-9]
        self.assertEqual(deep, [], "끊긴 뒤 더 내리지 않음")
        self.assertEqual(st._dry_xy["z"], 0.0)

    def test_estop_mid_search_no_more_steps(self):
        """찾는 중 비상정지(세대 바뀜): 남은 스텝 안 나감 · Z 를 움직이지 않음 · 출력 끔."""
        from core.motion import MotionError
        app, m, st, sent = contact_env(self.S3)
        orig = app.k2450.read
        box = {}

        def read():
            r = orig()
            if st._dry_xy["z"] >= 19.8 and "n" not in box:
                box["n"] = len(sent)
                box["z"] = st._dry_xy["z"]
                m.estop()                                            # 비상정지: 세대 + '!' (App.estop 과 같은 Motion 길)
            return r
        app.k2450.read_strict = read
        with self.assertRaises(MotionError) as c:
            self.find_at(app, m, 60.0, 60.0)
        self.assertEqual(c.exception.code, "estop")
        self.assertFalse([x for x in sent[box["n"]:] if x.startswith(("mz", "mx", "my", "jz"))], "비상정지 뒤 이동 줄 0")
        self.assertEqual(st._dry_xy["z"], box["z"])
        self.assertFalse(app.lab.out_on)

    def test_stop_mid_search_no_more_steps(self):
        """찾는 중 [정지]: 남은 스텝 안 나감 · Z 를 움직이지 않음(정지는 그것만) · 출력 끔."""
        from core.motion import MotionError
        app, m, st, sent = contact_env(self.S3)
        orig = app.k2450.read_strict
        box = {}

        def read():
            r = orig()
            if st._dry_xy["z"] >= 19.8 and "n" not in box:
                box["n"], box["z"] = len(sent), st._dry_xy["z"]
                m.stop()
            return r
        app.k2450.read_strict = read
        with self.assertRaises(MotionError) as c:
            self.find_at(app, m, 60.0, 60.0)
        self.assertEqual(c.exception.code, "stop")
        self.assertFalse([x for x in sent[box["n"]:] if x.startswith(("mz", "mx", "my", "jz"))])
        self.assertEqual(st._dry_xy["z"], box["z"])
        self.assertFalse(app.lab.out_on)

    def test_k2450_command_order(self):
        """K2450 명령 순서: 출력 켜기 전에 전압 소스 · 과전압 보호(가장 낮은 2 V) · 전류 한도 · 전압 · 2단자 · 자동 범위 · 평균 끔, 끝나면 출력 끔."""
        app, m, st, sent = contact_env(self.S3)
        self.find_at(app, m, 60.0, 60.0)
        cmds = app.k2450.cmds
        i_on = cmds.index(":OUTP ON")
        pre = cmds[:i_on]
        for c_ in ("*RST", ":ROUT:TERM REAR", ":SOUR:FUNC VOLT", ":SOUR:VOLT:PROT PROT2", ":SOUR:VOLT:ILIM 0.0001", ":SOUR:VOLT 0.1",
                   ":SENS:CURR:RSEN OFF", ":SENS:CURR:RANG:AUTO ON", ":SENS:CURR:NPLC 0.1", ":SENS:CURR:AVER OFF"):
            self.assertIn(c_, pre)
        self.assertLess(pre.index(":SOUR:VOLT:PROT PROT2"), pre.index(":SOUR:VOLT 0.1"))
        self.assertLess(pre.index(":SOUR:VOLT:ILIM 0.0001"), pre.index(":SOUR:VOLT 0.1"))
        outs = [c_ for c_ in cmds if c_.startswith(":OUTP")]
        self.assertEqual(outs[-1], ":OUTP OFF")
        i4 = cmds.index(":SENS:CURR:RSEN ON")                         # 4단자로 바꿀 때는 출력을 끈 채
        self.assertEqual(cmds[i4 - 1], ":OUTP OFF")
        self.assertEqual(app.lab.dev["2450"].err, [])

    def test_search_mode_from_current_z(self):
        """장비 설정 7(접촉 높이 없음): 지금 Z 부터 0.05 mm 간격 → 닿으면 0.3 mm 올렸다가 0.025 mm 간격으로 다시 · 첫 접촉 = 표면 ±1 스텝."""
        app, m, st, sent = contact_env([dict(self.S3[1], z=12.34)], cfg={"contact_z_mm": None})
        m.move_xy(100.0, 60.0)
        m.move_z(8.0)
        import contact as contact_mod
        r = contact_mod.find(app, m.gen, mode="search", z_ref=8.0)
        self.assertLessEqual(abs(r["first_z"] - 12.34), 0.025 + 1e-9, r)
        zs = mz_values(sent)
        self.assertTrue(any(b < a for a, b in zip(zs, zs[1:])), "닿은 뒤 한 번 올렸다가 다시 찾음")
        self.assertLessEqual(max(zs), 8.0 + 5.0 + 1e-9)

    def test_probe_down_floor_guard(self):
        """Motion.probe_down: 한도를 넘는 요청은 움직이기 전에 거절(한도 아래로 내려간 펄스 0) · Z 한 번 한도(jog_guard)도 지난다."""
        from core.motion import MotionError
        app, m, st, sent = contact_env(self.S3)
        m.move_xy(60.0, 60.0)
        n0 = len(sent)
        with self.assertRaises(MotionError) as c:
            m.probe_down(19.7, 20.5, lambda z: 0.9)
        self.assertEqual(c.exception.code, "floor")
        self.assertLessEqual(max(mz_values(sent[n0:])), 20.5 + 1e-9)


if __name__ == "__main__":
    prog = unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:]], verbosity=2, exit=False)
    for k, v in REPORT.items():
        print("보고 · %s = %s" % (k, v))
    sys.exit(0 if prog.result.wasSuccessful() else 1)
