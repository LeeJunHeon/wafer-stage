"""meters.py - 측정 장비: Keithley 2110(K형 열전대 온도) · Keithley 2450(전압 걸고 전류 재서 저항).

명령 순서는 v2 의 실장비에서 동작이 확인된 것을 그대로 옮겼다(2450: *RST → 단자 → 4단자 → 전압 소스 → 전류 측정 → 적분 · 평균 → 자동 영점).
장비마다 잠금 하나: 같은 장비에 두 곳에서 동시에 명령하지 않는다.
pyvisa 나 NI-VISA 가 없으면 그 장비만 "드라이버 없음" 으로 표시하고 프로그램은 계속 돈다.
"""

import math
import threading
import time
from typing import Callable, Optional

NO_DRIVER = "장비 통신 드라이버 없음 · NI-VISA 설치 필요"
OVERFLOW = 9.0e37                      # SCPI 범위 넘음 · 열전대 끊김 표기 9.91E+37
VISA_TIMEOUT_MS = 5000

RM_FACTORY: Optional[Callable] = None  # 장비 관리자를 꽂는 자리: callable() -> rm (없으면 pyvisa 의 ResourceManager)
LOG: Optional[Callable] = None         # (name, addr, op, cmd, resp, dt, exc) - 주고받은 줄 기록


class VisaError(Exception):
    pass


class VisaUnavailable(VisaError):
    def __init__(self):
        super().__init__(NO_DRIVER)


def _rm():
    if RM_FACTORY is not None:
        return RM_FACTORY()
    try:
        import pyvisa
    except ImportError as e:
        raise VisaUnavailable() from e
    try:
        return pyvisa.ResourceManager()
    except Exception as e:                                          # noqa: BLE001 - 드라이버 없음 · 로드 실패
        raise VisaUnavailable() from e


def read_time_s(nplc, avg_count, line_hz=60.0, conv_factor=3.0, overhead_s=0.1):
    """K2450 저항 한 번 읽기(:READ? + 한도 걸림 조회)의 예상 시간(초). 설정 조합 검사(config.problems)가 쓴다.
    범위가 바뀌는 점(read_point)은 읽기가 1~2번 더 든다.
    근거 - K2450.configure 가 거는 설정 기준:
      · 적분 시간 = NPLC ÷ 전원 주파수. 60 Hz 에서 1 PLC = 16.7 ms.
      · 평균은 반복 평균(:SENS:CURR:AVER:TCON REP)이라 읽기 한 번에 변환을 평균 횟수만큼 한다.
      · 자동 영점은 *RST 뒤 켜져 있다(configure 는 :SENS:AZER:ONCE 로 한 번 새로 잡을 뿐 끄지 않는다)
        → 변환마다 신호 · 기준 · 영점을 재는 것으로 보고 conv_factor = 3 (넉넉하게 잡은 값).
      · overhead_s = :READ? 와 :SOUR:VOLT:ILIM:TRIP? 두 번의 통신 + 범위 단계가 바뀔 때의 :SENS:CURR:RANG 쓰기.
    conv_factor · overhead_s 는 실장비에서 재 보지 않은 값이다(설정에 "실장비 확인 필요") - visa.log 의 읽기 시간으로 맞춘다.
    기본값: 10 × 1 ÷ 60 × 3 + 0.1 = 0.6 초."""
    return max(1, int(avg_count)) * float(nplc) / float(line_hz) * float(conv_factor) + float(overhead_s)


def visa_kind(addr):
    """VISA 주소의 연결 방식. GPIB0::18::INSTR → GPIB · USB0::… → USB · TCPIP… → LAN · ASRL… → 시리얼."""
    a = str(addr or "").strip().upper()
    for pre, name in (("GPIB", "GPIB"), ("USB", "USB"), ("TCPIP", "LAN"), ("ASRL", "시리얼")):
        if a.startswith(pre):
            return name
    return ""


def list_resources():
    """(주소 목록, 안내 문구). 시리얼(ASRL)은 뺀다 - 열면 보드가 재시작하고 COM4 는 다른 장비다."""
    try:
        items = [str(a) for a in _rm().list_resources()]
    except VisaUnavailable as e:
        return [], str(e)
    except Exception as e:                                          # noqa: BLE001
        return [], "장비 목록 읽기 실패 · %s" % str(e)[:40]
    return [a for a in items if visa_kind(a) != "시리얼"], ""


def usb_no_serial(addr):
    """USB0::0x05E6::0x2110::INSTR 처럼 일련번호가 빠진 USB 주소인가."""
    parts = str(addr or "").strip().upper().split("::")
    return len(parts) == 4 and parts[0].startswith("USB") and parts[-1] == "INSTR"


def _log(name, addr, op, cmd, resp, dt, exc):
    fn = LOG
    if fn is not None:
        try:
            fn(name, addr, op, cmd, resp, dt, exc)
        except Exception:                                           # noqa: BLE001
            pass


# --------------------------------------------------------------------------
# 순수 함수
# --------------------------------------------------------------------------
def error_code(resp):
    """SYST:ERR? 응답의 오류 번호. '+0,"No error"'(2110) · '0,"No error;0;0"'(2450) → 0 · '-113,"Undefined header"' → -113. 못 읽으면 None."""
    head = str(resp or "").strip().split(",", 1)[0].strip()
    try:
        return int(head)
    except ValueError:
        try:
            f = float(head)
        except ValueError:
            return None
        return int(f) if math.isfinite(f) and f == int(f) else None


def parse_float(txt):
    """READ? 응답 → float | None(범위 넘음 · 값 아님). 쉼표로 여럿이면 첫 값."""
    try:
        v = float(str(txt).strip().split(",")[0])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or abs(v) >= OVERFLOW:
        return None
    return v


def resistance(volt, current):
    """저항 = 걸어 준 전압 ÷ 측정 전류. 전류가 없거나 0 이면 None."""
    if current is None or volt is None:
        return None
    try:
        i = float(current)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(i) or i == 0.0:
        return None
    return abs(float(volt)) / abs(i)


RANGES = [1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0]          # 2450 전류 측정 범위(10 nA ~ 1 A)
START_RANGE = 1e-3                     # 시작 범위(1 mA) - v2 의 실장비 값
LIMIT_FRAC = 0.106                     # 매뉴얼: 고정 범위에서 전압 소스의 전류 한도는 측정 범위의 10.6 % 이상이어야 한다


def limit_ok(rng, ilim):
    return float(ilim) >= LIMIT_FRAC * float(rng) * (1 - 1e-9)


def clamp_range(rng, ilim):
    """한도 규칙을 어기는 큰 범위는 규칙을 지키는 가장 큰 범위로 낮춘다. 그러면 한도 전류를 못 덮는 경우(한도 > 범위)는 그대로 둔다."""
    r = float(rng)
    if limit_ok(r, ilim):
        return r
    ok = [x for x in RANGES if x <= r * (1 + 1e-9) and limit_ok(x, ilim)]
    best = max(ok) if ok else RANGES[0]
    return best if best >= float(ilim) * 0.999 else r


# ---- 범위 규칙(K2450.read_point) - 근거: 2026-11-23 실장비 visa.log ----
# · 2450 은 고정 범위에서 범위의 105 % 까지만 읽는다(사양 Overrange 105 %). 넘으면 9.9E+37 이거나 105 % 에 멈춘 값(+ 한도 표시)일 수 있다
#   (실장비가 어느 쪽인지 아직 모름) → 범위의 100 % 이상(RANGE_OVER)은 "이 범위로 못 잰 값" 으로 버리고 한 단계 올려 다시 읽는다.
#   그 전에 70 %(RANGE_UP)를 넘으면 값은 쓰고 다음 읽기부터 한 단계 올린다. 내림은 5 %(RANGE_DOWN) 아래 - 올린 뒤 · 내린 뒤 되돌아가지 않는 폭.
# · 1 mA → 1 µA 이하로 범위를 내린 직후의 첫 읽기에 :SOUR:VOLT:ILIM:TRIP? 이 1 이었다(24번 중 7번 · 전류 3.9e-11 ~ 8.4e-7 A - 한도와 거리가 멂).
#   → 한도 걸림은 표시가 아니라 전류가 실제로 한도 근처인가(LIMIT_NEAR)로 본다. 표시가 선 값은 한 번 다시 읽는다.
# · 출력을 켠 직후 1 mA 범위의 첫 값은 맞는 범위에서 다시 읽은 값과 -61 ~ +182 % 달랐다 → 켠 뒤 SETTLE_READS 번 읽어 버린다(K2450.start).
RANGE_UP = 0.7
RANGE_DOWN = 0.05
RANGE_OVER = 1.0
LIMIT_NEAR = 0.95
SETTLE_READS = 1
POINT_READS = 6                        # 한 점에 쓰는 읽기 수 한도


def _range_idx(rng):
    r = float(rng)
    return min(range(len(RANGES)), key=lambda k: abs(math.log10(RANGES[k] / r)))


def top_range(ilim):
    """한도 전류를 덮는 가장 낮은 범위(한도 규칙 clamp_range 적용). 이보다 높은 범위는 쓰지 않는다."""
    need = float(ilim)
    r = next((x for x in RANGES if x >= need * 0.999), RANGES[-1])
    return clamp_range(r, need)


def judge_read(rng, current, ilim):
    """읽은 전류 하나의 판정 → (kind, 다음 범위). current None = 범위 넘음.
    limit(진짜 전류 한도 · 범위 그대로) · over(이 범위로 못 잰 값 · 한 단계 위) · up(값은 맞음 · 다음 읽기부터 한 단계 위) ·
    down(5 % 아래가 아닌 범위까지 한 번에 내림) · ok. 한도 표시(TRIP)는 쓰지 않는다."""
    r = float(rng)
    top = top_range(ilim)
    idx = _range_idx(r)
    over = current is None
    a = 0.0 if over else abs(float(current))
    over = over or a >= RANGE_OVER * r
    if a >= LIMIT_NEAR * float(ilim) or (over and r >= top * (1 - 1e-9)):
        return "limit", r
    up = min(RANGES[min(idx + 1, len(RANGES) - 1)], top)
    if over:
        return "over", up
    if a > RANGE_UP * r and r < top * (1 - 1e-9):
        return "up", up
    if a < RANGE_DOWN * r and idx > 0:
        while idx > 0 and a < RANGE_DOWN * RANGES[idx]:
            idx -= 1
        return "down", RANGES[idx]
    return "ok", r


def is_open_circuit(current, floor_a):
    """전류 크기가 하한(기본 1 nA) 아래면 연결이 없는 것(열린 회로). 범위 넘음(None)은 열린 회로가 아니다."""
    if current is None:
        return False
    try:
        return abs(float(current)) < float(floor_a)
    except (TypeError, ValueError):
        return False


# --------------------------------------------------------------------------
class _Dev:
    NAME = "VISA"
    MODEL = ""

    def __init__(self, addr):
        self.addr = addr
        self.res = None
        self.lock = threading.RLock()                               # 장비마다 하나
        self.idn = ""

    def open(self):
        t0 = time.monotonic()
        try:
            self.res = _rm().open_resource(self.addr)
        except VisaUnavailable:
            raise
        except Exception as e:                                      # noqa: BLE001
            _log(self.NAME, self.addr, "open", "", None, time.monotonic() - t0, e)
            raise VisaError("장비 열기 실패 · %s · %s" % (self.addr, str(e)[:40]))
        _log(self.NAME, self.addr, "open", "", "ok", time.monotonic() - t0, None)
        try:
            self.res.timeout = VISA_TIMEOUT_MS
        except Exception:                                           # noqa: BLE001
            pass
        try:
            self.idn = self.query("*IDN?")
        except VisaError:
            self.close()
            raise
        if self.MODEL and self.MODEL not in self.idn:
            idn = self.idn
            self.close()
            raise VisaError("%s 아닌 장비 · %s" % (self.NAME, idn[:40]))
        return self.idn

    def write(self, cmd):
        if self.res is None:
            raise VisaError("장비 연결 안 됨")
        with self.lock:
            t0 = time.monotonic()
            try:
                self.res.write(cmd)
            except Exception as e:                                  # noqa: BLE001
                _log(self.NAME, self.addr, "write", cmd, None, time.monotonic() - t0, e)
                raise VisaError("장비 통신 끊김 · %s" % str(e)[:40])
            _log(self.NAME, self.addr, "write", cmd, None, time.monotonic() - t0, None)

    def query(self, cmd):
        if self.res is None:
            raise VisaError("장비 연결 안 됨")
        with self.lock:
            t0 = time.monotonic()
            try:
                r = str(self.res.query(cmd)).strip()
            except Exception as e:                                  # noqa: BLE001
                _log(self.NAME, self.addr, "query", cmd, None, time.monotonic() - t0, e)
                raise VisaError("장비 통신 끊김 · %s" % str(e)[:40])
            _log(self.NAME, self.addr, "query", cmd, r, time.monotonic() - t0, None)
            return r

    def close(self):
        r, self.res = self.res, None
        if r is not None:
            t0 = time.monotonic()
            try:
                r.close()
                _log(self.NAME, self.addr, "close", "", None, time.monotonic() - t0, None)
            except Exception as e:                                  # noqa: BLE001
                _log(self.NAME, self.addr, "close", "", None, time.monotonic() - t0, e)

    @property
    def is_open(self):
        return self.res is not None

    def drain_errors(self):
        """오류 큐(SYST:ERR?)를 비우며 읽는다. 번호 0(+0 포함) = 오류 없음."""
        out = []
        for _ in range(8):
            try:
                r = self.query("SYST:ERR?")
            except VisaError:
                break
            if not r or error_code(r) == 0:
                break
            out.append(r)
        return out

    def raw(self, line):
        """직접 명령: '?' 로 끝나면 질의, 아니면 쓰기."""
        line = line.strip()
        if line.endswith("?"):
            return self.query(line)
        self.write(line)
        return "보냄"


class K2110(_Dev):
    """Keithley 2110 - K형 열전대 온도(°C, 기준점 내부)."""

    NAME = "K2110"
    MODEL = "2110"

    def configure(self, tc_type="K", rjun="REAL"):
        with self.lock:
            self.write("*RST")
            self.write('SENS:FUNC "TCO"')
            self.write("SENS:TCO:TYPE %s" % tc_type)
            self.write("SENS:TCO:RJUN:RSEL %s" % rjun)
            self.write("UNIT Cel")
            errs = self.drain_errors()
            if errs:
                raise VisaError("K2110 설정 오류 · " + errs[0][:40])

    def read_temp(self):
        """1회 읽기(°C). 열전대 끊김 · 범위 넘음이면 None(값 없음)."""
        return parse_float(self.query("READ?"))


class K2450(_Dev):
    """Keithley 2450 - 전압 소스, 전류 측정. 전류 한도를 항상 건다. 실제 배선은 뒷면 단자(REAR) 4단자."""

    NAME = "K2450"
    MODEL = "2450"
    out_state = False
    range = START_RANGE                                 # 지금 전류 측정 범위(read_point 가 맞춘다)
    ilim = 0.1
    flag_stuck = False                                  # 한도 표시가 다시 읽어도 서 있었다(이번 설정에서는 표시로 다시 읽지 않음)
    last_reads = 0                                      # 마지막 read_point 가 쓴 읽기 수

    def check_scpi(self):
        """명령 세트가 SCPI 인가. 일부 펌웨어는 *LANG? 가 없다 - 그때는 SCPI 로 본다."""
        try:
            lang = self.query("*LANG?").upper()
        except VisaError:
            return True, ""
        if "SCPI" not in lang and "2450" not in lang:
            return False, "K2450 명령 세트 SCPI 전환 필요"
        return True, ""

    def terminal(self):
        try:
            t = self.query(":ROUT:TERM?").strip().upper()
        except VisaError:
            return ""
        return "REAR" if t.startswith("REAR") else "FRON" if t.startswith("FRON") else ""

    def configure(self, volt, ilim_a, four_wire=True, nplc=1.0, avg_count=10, terminal="REAR", start_range=START_RANGE):
        """v2 의 실장비 순서 그대로: *RST → 단자 → 4단자 → 전압 소스(한도 · 수준) → 전류 측정(자동 범위 끔 · 시작 범위) → 적분 → 평균 → 자동 영점.
        범위는 read_point 가 점마다 맞춘다. 출력은 켜지 않는다(start)."""
        with self.lock:
            self.flag_stuck = False
            if self.out_state:                                      # 출력이 켜진 채로는 단자를 바꾸지 않는다(매뉴얼: 바꾸면 출력이 꺼짐)
                self.output(False)
            self.write("*RST")
            self.out_state = False
            self.write(":ROUT:TERM %s" % terminal)
            got = self.terminal()
            if got != terminal:
                raise VisaError("K2450 단자 설정 실패 · %s 로 읽힘" % (got or "?"))
            self.write(":SENS:CURR:RSEN %s" % ("ON" if four_wire else "OFF"))
            self.write(":SOUR:FUNC VOLT")
            self.write(":SOUR:VOLT:ILIM %.6g" % float(ilim_a))
            self.write(":SOUR:VOLT %.6g" % float(volt))
            self.write(':SENS:FUNC "CURR"')
            self.write(":SENS:CURR:RANG:AUTO OFF")
            self.range = clamp_range(float(start_range), float(ilim_a))
            self.ilim = float(ilim_a)
            self.write(":SENS:CURR:RANG %.6g" % self.range)
            self.write(":SENS:CURR:NPLC %.6g" % float(nplc))
            self.write(":SENS:CURR:AVER:TCON REP")
            self.write(":SENS:CURR:AVER:COUN %d" % max(1, int(avg_count)))
            self.write(":SENS:CURR:AVER ON")
            self.write(":SENS:AZER:ONCE")
            errs = self.drain_errors()
            if errs:
                raise VisaError("K2450 설정 오류 · " + errs[0][:40])

    def set_range(self, r):
        self.write(":SENS:CURR:RANG %.6g" % float(r))
        self.range = float(r)

    def start(self):
        """출력 켜기 → SETTLE_READS 번 읽어 버린다(켠 직후 1 mA 범위의 첫 값은 맞는 범위에서 다시 읽은 값과 크게 다르다)."""
        with self.lock:
            self.output(True)
            for _ in range(SETTLE_READS):
                self.read()

    def read_point(self):
        """한 점 → (전류 | None, 한도 걸림). 판정(judge_read)이 over · down 이면 범위를 바꿔 다시 읽고(그 값은 버림), up 이면 값을 쓰고 다음부터 위 범위.
        한도 걸림 = 전류가 실제로 한도 근처(limit)일 때만. 한도 표시가 선 값은 같은 범위에서 한 번 다시 읽는다 - 다시 읽어도 서 있으면
        장비가 표시를 유지하는 것으로 보고(flag_stuck) 이번 설정에서는 표시 때문에 다시 읽지 않는다. 쓴 읽기 수 = last_reads."""
        with self.lock:
            retried = False
            n = 0
            try:
                while n < POINT_READS:
                    cur, flag = self.read()
                    n += 1
                    kind, new = judge_read(self.range, cur, self.ilim)
                    if kind == "limit":
                        return cur, True
                    if kind in ("over", "down"):
                        self.set_range(new)
                        retried = False                             # 새 범위의 첫 읽기 - 표시는 범위를 바꾼 순간의 것일 수 있다
                        continue
                    if flag and not self.flag_stuck:
                        if not retried:
                            retried = True
                            continue
                        self.flag_stuck = True
                    if kind == "up":
                        self.set_range(new)
                    return cur, False
                return None, False
            finally:
                self.last_reads = n

    # ---- 접촉 확인(app/contact.py): 아주 약한 전압 · 낮은 전류 한도 · 2단자 → (닿은 뒤) 4단자. v2 에서 그대로 ----
    OVP_MIN = "PROT2"                                   # 과전압 보호의 가장 낮은 단계(2 V)

    def configure_contact(self, volt, ilim_a, nplc=0.1, terminal="REAR"):
        """접촉 확인 설정. 출력을 켜기 **전에** 단자 → 전압 소스 → 과전압 보호(가장 낮은 2 V) → 전류 한도 → 전압 → 전류 측정
        (2단자 · 자동 범위 · 적분 · 평균 끔). 4단자로 바꾸면 감지 핀이 아직 떠 있을 수 있다 - 전압 소스는 감지 단자 전압을 맞추려고
        출력을 올릴 수 있으므로(열린 감지선) 과전압 보호를 걸어 시료에 큰 전압이 걸리지 않게 한다. 측정은 *RST 부터 자기 설정을 다시 한다."""
        with self.lock:
            if self.out_state:
                self.output(False)
            self.write("*RST")
            self.out_state = False
            self.write(":ROUT:TERM %s" % terminal)
            got = self.terminal()
            if got != terminal:
                raise VisaError("K2450 단자 설정 실패 · %s 로 읽힘" % (got or "?"))
            self.write(":SOUR:FUNC VOLT")
            self.write(":SOUR:VOLT:PROT %s" % self.OVP_MIN)
            self.write(":SOUR:VOLT:ILIM %.6g" % float(ilim_a))
            self.write(":SOUR:VOLT %.6g" % float(volt))
            self.write(':SENS:FUNC "CURR"')
            self.write(":SENS:CURR:RSEN OFF")
            self.write(":SENS:CURR:RANG:AUTO ON")
            self.write(":SENS:CURR:NPLC %.6g" % float(nplc))
            self.write(":SENS:CURR:AVER OFF")
            errs = self.drain_errors()
            if errs:
                raise VisaError("K2450 접촉 확인 설정 오류 · " + errs[0][:40])

    def set_four_wire(self, on):
        """2단자 ↔ 4단자. 출력을 끈 채 바꾸고 켠다(켠 채로 바꾸는 것은 매뉴얼에서 확인하지 못했다 - 끄고 바꾸는 쪽이 안전)."""
        with self.lock:
            was = self.out_state
            if was:
                self.output(False)
            self.write(":SENS:CURR:RSEN %s" % ("ON" if on else "OFF"))
            if was:
                self.output(True)

    def ovp_tripped(self):
        """과전압 보호가 걸렸는가(:SOUR:VOLT:PROT:TRIP?). 못 읽으면 VisaError(모르는 것을 '안 걸림' 으로 보지 않는다)."""
        return self.query(":SOUR:VOLT:PROT:TRIP?").strip() in ("1", "+1")

    def read_strict(self):
        """접촉 확인용 읽기: 전류와 한도 걸림을 **둘 다** 읽어야 한다(한도 걸림을 못 읽으면 단락을 놓칠 수 있다 - 예외)."""
        with self.lock:
            cur = parse_float(self.query(":READ?"))
            trip = self.query(":SOUR:VOLT:ILIM:TRIP?").strip() in ("1", "+1")
        return cur, trip

    def output(self, on):
        self.write(":OUTP %s" % ("ON" if on else "OFF"))
        self.out_state = bool(on)

    def read(self):
        """(전류 A | None, 전류 한도 걸림)."""
        cur = parse_float(self.query(":READ?"))
        try:
            trip = self.query(":SOUR:VOLT:ILIM:TRIP?").strip() in ("1", "+1")
        except VisaError:
            trip = False
        return cur, trip

    def output_off_urgent(self, wait_s=1.0):
        """비상정지용 출력 끄기: 장비 잠금을 wait_s 만 기다리고, 못 얻으면 잠금 없이 쓴다(끄는 것이 먼저다)."""
        got = self.lock.acquire(timeout=wait_s)
        try:
            self.output(False)
        finally:
            if got:
                self.lock.release()

    def measure_once(self, volt, ilim_a, four_wire=True, nplc=1.0, avg_count=10):
        """저항 한 번: 설정 → 출력 켜기(start) → 한 점(read_point) → (무슨 일이 있어도) 출력 끄기. dict(current, ohm, tripped, volt, four_wire).
        tripped = read_point 의 한도 걸림(전류가 실제로 한도 근처)만."""
        with self.lock:
            try:
                self.configure(volt, ilim_a, four_wire, nplc, avg_count)
                self.start()
                cur, trip = self.read_point()
            finally:
                self._off_quiet()
        return {"current": cur, "ohm": resistance(volt, cur), "tripped": trip, "volt": float(volt), "four_wire": bool(four_wire)}

    def _off_quiet(self):
        """출력 끄기. 실패해도 예외를 올리지 않는다(이미 오류 중일 수 있다) - 실패는 로그에 남는다."""
        try:
            self.output(False)
        except VisaError:
            self.out_state = False
