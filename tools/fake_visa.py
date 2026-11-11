"""fake_visa.py - 가짜 측정 장비(Keithley 2110 · 2450)와 열 · VO2 모델. 실제 VISA 를 열지 않는다.

  - K2110 · K2450 의 SCPI 명령을 흉내 낸다(매뉴얼 문법 그대로 - 모르는 명령은 오류 큐에 쌓고 응답하지 않는다).
  - 열 모델(Lab): 히트건 켜짐/꺼짐 · 쿨러 켜짐 · 지연(열전대 1차 지연). 시간은 clock() 으로 받는다(시뮬레이션 배율 적용).
  - VO2 저항 모델: 가열 약 66 °C · 냉각 약 58 °C 에서 3e5 ↔ 2e2 Ω 로 바뀌는 고리(히스테리시스).
  - 오류 조작(lab.fault): tc_open(열전대 끊김) · disconnect(연결 끊김) · ilim(전류 한도 걸림) · slow(느린 응답 s)
  - 핀이 접촉 높이에 있지 않으면 열전대가 공기 온도를 잰다(lab.pin_down() 이 False).
  - 접촉 찾기 흉내(lab.probe · pos_fn): K2450 이 스테이지의 실제 X · Y · Z 를 보고 답한다. 샘플마다 표면 높이 · 크기 · 저항,
    샘플 밖은 알루미늄 판(lab.probe["plate_z"] 아래로 내려가면 단락), 핀 4개 중 FORCE 두 개가 먼저(표면), SENSE 두 개는 lag4 만큼 더 내려가야 닿는다.
    2단자 = 시료 + 접촉 저항(rc) · 4단자 = 시료(네 핀 모두 닿음) / 감지 핀이 떠 있으면 전압 소스가 출력을 올려 과전압 보호(PROT)에 걸린다.

사용: install(lab) 로 instruments.RM_FACTORY 에 꽂는다. 시험은 Lab 을 직접 만들어 쓴다.
"""

import math
import threading
import time

from core import instruments as inst

T_AMB = 25.0
OVER = "+9.91000000E+37"


class Lab:
    """열 + 시료 모델. heater_fn / cooler_fn / pin_down_fn 은 콜백(앱이 릴레이 · 위치에서 읽어 준다)."""

    def __init__(self, clock=None, sleep=None, heater_fn=None, cooler_fn=None, pin_down_fn=None):
        self.clock = clock or time.monotonic
        self.sleep = sleep or time.sleep
        self.heater_fn = heater_fn or (lambda: False)
        self.cooler_fn = cooler_fn or (lambda: False)
        self.pin_down = pin_down_fn or (lambda: True)
        self.t_amb = T_AMB
        self.t_heat = 125.0            # 히트건 켠 채 오래 두면 가는 온도
        self.t_cool = 10.0             # 쿨러만 켜면 가는 온도
        self.tau = 60.0                # 판 온도 시정수(s)
        self.tau_sensor = 1.5          # 열전대 지연(s)
        self.T = T_AMB                 # 판(시료) 온도
        self.Ts = T_AMB                # 열전대 값
        self.air = T_AMB
        self.m = 0.0                   # 금속 분율(0 절연 · 1 금속) - 히스테리시스 상태
        self.t_up, self.t_down, self.width = 66.0, 58.0, 1.6
        self.r_hi, self.r_lo = 3.0e5, 2.0e2
        self.t_last = self.clock()
        self.fault = {"tc_open": False, "disconnect": False, "ilim": False, "slow": 0.0}
        self.out_on = False
        self.volt = 1.0
        self.ilim = 0.01
        self.lock = threading.RLock()
        self.dev = {}
        self.read_dt = 0.25            # 읽기 한 번에 걸리는 장비 시간(s)
        self.tripped = False
        self.cable = "REAR"            # 케이블은 K2450 뒷면 단자에 꽂혀 있다 - 앞면 단자 설정이면 열린 회로
        self.term = "FRON"             # K2450 현재 단자 설정(*RST 뒤 앞면)
        self.addrs = None              # VISA 목록(None = ADDRS) - 주소 찾기 시험이 바꾼다
        self.opened = []               # 연 주소 순서(시험)
        self.probe = None              # 접촉 찾기 흉내: {"plate_z", "rc", "samples": [{"x", "y", "half", "z", "lag4", "ohm"}]} (None = 위치와 무관)
        self.pos_fn = lambda: None     # 실제 스테이지 (x, y, z) mm - 앱이 꽂는다
        self.ovp_tripped = False
        self.prot_v = None             # :SOUR:VOLT:PROT 단계(V) - None = 걸지 않음(공장값 없음)
        self.lead_ohm = 20.0           # 2단자로 잴 때 함께 재지는 리드 · 접촉 저항(4단자는 빠진다) - 위치와 무관한 흉내에서도 2단자 > 4단자

    # ---- 물리 ------------------------------------------------------
    def _step(self):
        now = self.clock()
        dt = now - self.t_last
        self.t_last = now
        if dt <= 0:
            return
        h, c = bool(self.heater_fn()), bool(self.cooler_fn())
        target = self.t_amb
        if h:
            target = self.t_heat
        elif c:
            target = self.t_cool
        n = max(1, int(dt / 0.25))
        d = dt / n
        for _ in range(n):
            prev = self.T
            self.T += (target - self.T) * (1.0 - math.exp(-d / self.tau))
            self.air += (self.t_amb + 0.25 * (self.T - self.t_amb) - self.air) * (1.0 - math.exp(-d / 20.0))
            seen = self.T if self.pin_down() else self.air
            self.Ts += (seen - self.Ts) * (1.0 - math.exp(-d / self.tau_sensor))
            if self.T > prev:
                self.m = max(self.m, _sig((self.T - self.t_up) / self.width))
            elif self.T < prev:
                self.m = min(self.m, _sig((self.T - self.t_down) / self.width))

    def temp(self):
        with self.lock:
            self._step()
            return self.Ts

    def resistance(self):
        with self.lock:
            self._step()
            lg = math.log10(self.r_hi) * (1.0 - self.m) + math.log10(self.r_lo) * self.m
            # 절연 쪽은 온도가 오르면 완만히 줄어든다
            lg -= 0.004 * max(0.0, self.T - 25.0) * (1.0 - self.m)
            return 10.0 ** lg

    def probe_touch(self):
        """접촉 찾기 흉내: (닿은 핀 수 0 · 2 · 4, 시료 저항 Ω, 단락). 실제 스테이지 위치로 판단한다."""
        pr = self.probe
        pos = self.pos_fn()
        if not pr or pos is None:
            return None
        x, y, z = pos
        for sm in pr.get("samples") or []:
            if abs(x - sm["x"]) <= sm.get("half", 2.5) and abs(y - sm["y"]) <= sm.get("half", 2.5):
                if z < sm["z"] - 1e-9:
                    return 0, sm.get("ohm", 1e6), False
                if z < sm["z"] + sm.get("lag4", 0.0) - 1e-9:
                    return 2, sm.get("ohm", 1e6), False
                return 4, sm.get("ohm", 1e6), False
        if z >= pr.get("plate_z", 99.0) - 1e-9:
            return 4, 0.5, True                                # 샘플 밖 · 알루미늄 판
        return 0, None, False

    def read_current(self, rng, auto, rsen=False):
        """(전류 문자열, 한도 걸림). 범위 넘으면(수동 범위) 9.91E37 · 한도 걸리면 한도 전류."""
        pt = self.probe_touch()
        if pt is not None:
            return self._read_probe(pt, rng, auto, rsen)
        r = self.resistance() + (0.0 if rsen else self.lead_ohm)
        i = self.volt / r if self.out_on else 0.0
        if self.term != self.cable[:4]:
            i = 2.0e-12 if self.out_on else 0.0           # 열린 회로: 누설 전류만
        self.tripped = False
        if self.fault["ilim"] and self.out_on:
            self.tripped = True
            return "%+.8E" % self.ilim
        if i > self.ilim:
            self.tripped = True
            i = self.ilim
        if not auto and abs(i) > rng * 1.05:
            return OVER
        return "%+.8E" % i


    def _read_probe(self, pt, rng, auto, rsen):
        n, ohm, short = pt
        rc = float((self.probe or {}).get("rc", 2.0e3))
        self.tripped = False
        self.ovp_tripped = False
        if not self.out_on:
            return "%+.8E" % 0.0, False
        if short:
            i = self.volt / ohm
        elif n == 0:
            i = 2.0e-12
        elif not rsen:
            i = self.volt / (ohm + rc)                        # 2단자: FORCE 두 핀(시료 + 접촉 저항)
        elif n == 4:
            i = self.volt / ohm                               # 4단자 · 네 핀 모두: 시료 전압을 맞춤
        else:                                                 # 4단자 · 감지 핀이 떠 있음: 출력이 올라 과전압 보호에 걸린다(없으면 20 V 까지)
            vmax = self.prot_v if self.prot_v else 20.0
            self.ovp_tripped = True
            i = vmax / (ohm + rc)
        if abs(i) > self.ilim:
            self.tripped = True
            i = self.ilim
        if not auto and abs(i) > rng * 1.05:
            return OVER
        return "%+.8E" % i


def _sig(x):
    if x < -30:
        return 0.0
    if x > 30:
        return 1.0
    return 1.0 / (1.0 + math.exp(-x))


# --------------------------------------------------------------------------
class _Res:
    """pyvisa 리소스처럼 write / query / close / timeout 을 낸다."""

    def __init__(self, lab, kind, addr):
        self.lab, self.kind, self.addr = lab, kind, addr
        self.timeout = 5000
        self.err = []
        self.open_ = True
        self.s = {"range": 1e-3, "auto": False, "func": "CURR", "terminal": "FRON", "rsen": False, "nplc": 1.0, "avg_n": 1, "avg_on": False,
                  "tc_type": "?", "rjun": "?", "unit": "?", "trip_q": False}

    def _gate(self):
        lab = self.lab
        if not self.open_:
            raise inst.VisaError("closed")
        if lab.fault.get("disconnect"):
            raise TimeoutError("VI_ERROR_TMO (시뮬레이션: 연결 끊김)")
        slow = float(lab.fault.get("slow") or 0.0)
        if slow > 0:
            lim = float(self.timeout or 5000) / 1000.0
            if slow > lim:                                    # pyvisa 처럼: 응답이 시간 한도보다 늦으면 VI_ERROR_TMO
                time.sleep(lim)
                raise TimeoutError("VI_ERROR_TMO (시뮬레이션: 응답 %.1f s > 한도 %.1f s)" % (slow, lim))
            time.sleep(slow)

    def close(self):
        self.open_ = False

    def write(self, cmd):
        self._gate()
        c = cmd.strip()
        u = c.upper().replace(" ", "")
        lab, s = self.lab, self.s
        if self.kind == "2450":
            self._w2450(c, u)
        else:
            self._w2110(c, u)

    # ---- K2450 ----
    def _w2450(self, c, u):
        lab, s = self.lab, self.s
        if u == "*RST":
            s.update(range=1e-3, auto=False, avg_on=False, avg_n=1, terminal="FRON", rsen=False)
            lab.out_on = False
            lab.ilim, lab.volt = 0.1, 0.0
            lab.term = "FRON"
            lab.prot_v = None                                 # 공장값: 과전압 보호 없음
            lab.ovp_tripped = False
        elif u.startswith(":ROUT:TERM"):
            arg = u.split("TERM", 1)[1]
            if arg.startswith(("FRON", "REAR")):
                s["terminal"] = arg[:4]
                if lab.out_on:                              # 매뉴얼: 출력이 켜진 채 단자를 바꾸면 출력이 꺼진다
                    lab.out_on = False
                lab.term = arg[:4]
            else:
                self.err.append("bad terminals")
        elif u.startswith(":SENS:CURR:RSEN"):
            s["rsen"] = u.endswith("ON")
        elif u == ":SOUR:FUNC VOLT" or u == ":SOUR:FUNCVOLT" or u.startswith(":SOUR:FUNC"):
            pass
        elif u.startswith(":SOUR:VOLT:PROT") and not u.endswith("?"):
            arg = u.split(":SOUR:VOLT:PROT", 1)[1].lstrip(":LEV")
            if arg == "NONE":
                lab.prot_v = None
            elif arg.startswith("PROT") and arg[4:].isdigit() and int(arg[4:]) in (2, 5, 10, 20, 40, 60, 80, 100, 120, 140, 160, 180):
                lab.prot_v = float(arg[4:])
            else:
                self.err.append("bad protection: " + c)
        elif u.startswith(":SOUR:VOLT:ILIM"):
            lab.ilim = float(u.split("ILIM", 1)[1])
        elif u.startswith(":SOUR:VOLT") and not u.endswith("?"):
            lab.volt = float(u.split(":SOUR:VOLT", 1)[1])
        elif u.startswith(":SENS:FUNC"):
            s["func"] = "CURR"
        elif u.startswith(":SENS:CURR:RANG:AUTO"):
            s["auto"] = u.endswith("ON")
        elif u.startswith(":SENS:CURR:RANG"):
            s["range"] = float(u.split("RANG", 1)[1])
        elif u.startswith(":SENS:CURR:NPLC"):
            s["nplc"] = float(u.split("NPLC", 1)[1])
        elif u.startswith(":SENS:CURR:AVER:TCON"):
            s["tcon"] = u.split("TCON", 1)[1]
        elif u.startswith(":SENS:CURR:AVER:COUN"):
            s["avg_n"] = int(float(u.split("COUN", 1)[1]))
        elif u.startswith(":SENS:CURR:AVER"):
            s["avg_on"] = u.endswith("ON")
        elif u == ":SENS:AZER:ONCE":
            pass
        elif u.startswith(":OUTP"):
            lab.out_on = u.endswith("ON")
        else:
            self.err.append("unknown: " + c)

    # ---- K2110 ----
    def _w2110(self, c, u):
        s = self.s
        if u == "*RST":
            s["unit"] = "?"
        elif u in ('SENS:FUNC"TCO"', 'SENS:FUNC"TCOUPLE"'):
            s["func"] = "TCO"
        elif u.startswith("SENS:TCO:TYPE"):
            s["tc_type"] = u.split("TYPE", 1)[1]
        elif u.startswith("SENS:TCO:RJUN:RSEL"):
            s["rjun"] = u.split("RSEL", 1)[1]
        elif u == "UNITCEL":
            s["unit"] = "C"
        else:
            self.err.append("unknown: " + c)

    def query(self, cmd):
        self._gate()
        u = cmd.strip().upper().replace(" ", "")
        lab, s = self.lab, self.s
        if u == "*IDN?":
            return ("KEITHLEY INSTRUMENTS,MODEL 2450,SIM0001,1.7.0\n" if self.kind == "2450" else "KEITHLEY INSTRUMENTS INC.,MODEL 2110,SIM0002,02.03-02.03-02.02\n")
        if u == "SYST:ERR?":                                 # 실제 장비 응답 형식: 2110 '+0, "No error"' · 2450 '0,"No error;0;0"'
            if self.err:
                return '-113,"Undefined header; %s"\n' % self.err.pop(0)
            return '+0, "No error"\n' if self.kind == "2110" else '0,"No error;0;0"\n'
        if u == "*LANG?":
            return "SCPI\n" if getattr(lab, "lang", "SCPI") == "SCPI" else "TSP\n"
        if self.kind == "2110" and u == "READ?":
            lab.sleep(lab.read_dt)
            if lab.fault.get("tc_open"):
                return OVER
            return "%+.8E" % lab.temp()
        if self.kind == "2450" and u == ":READ?":
            lab.sleep(lab.read_dt)
            return lab.read_current(s["range"], s["auto"], rsen=s["rsen"])
        if self.kind == "2450" and u == ":SOUR:VOLT:ILIM:TRIP?":
            return "1" if lab.tripped else "0"
        if self.kind == "2450" and u == ":SOUR:VOLT:PROT:TRIP?":
            return "1" if lab.ovp_tripped else "0"
        if self.kind == "2450" and u == ":ROUT:TERM?":
            return s["terminal"] + chr(10)
        if self.kind == "2450" and u == ":OUTP?":
            return "1" if lab.out_on else "0"
        self.err.append("unknown query: " + cmd)
        return "0"


ADDRS = ["ASRL4::INSTR", "ASRL5::INSTR", "USB0::0x05E6::0x2110::SIM0002::INSTR", "GPIB0::18::INSTR", "GPIB0::6::INSTR"]


class FakeRM:
    def __init__(self, lab):
        self.lab = lab
        # 장비 PC 목록처럼: 시리얼 포트(ASRL - 스테이지 · 예전 보드)가 섞여 있다. 앱은 이것을 열면 안 된다(열면 오류로 남긴다)
        if getattr(lab, "addrs", None) is None:
            lab.addrs = list(ADDRS)
        self.opened = []                                     # 시험: 연 주소 순서

    @property
    def addrs(self):
        return self.lab.addrs

    def list_resources(self):
        return tuple(self.addrs)

    def open_resource(self, addr):
        self.opened.append(addr)
        self.lab.opened.append(addr)
        if self.lab.fault.get("disconnect"):
            raise TimeoutError("연결 안 됨")
        if addr.upper().startswith("ASRL"):
            raise RuntimeError("시리얼 포트를 측정 장비로 열었음 (시험 실패)")
        if addr not in self.addrs and not inst.usb_no_serial(addr):   # 일련번호 없는 USB 주소는 실제 NI-VISA 처럼 첫 장비로 열린다
            raise RuntimeError("VI_ERROR_RSRC_NFOUND %s" % addr)
        if "2450" in addr or addr.startswith("GPIB"):
            r = _Res(self.lab, "2450", addr)
        elif "2110" in addr or addr.startswith("USB"):
            r = _Res(self.lab, "2110", addr)
        else:
            raise ValueError("알 수 없는 주소 %s" % addr)
        self.lab.dev[r.kind] = r
        return r


def install(lab):
    inst.RM_FACTORY = lambda: FakeRM(lab)
    return lab


def uninstall():
    inst.RM_FACTORY = None
