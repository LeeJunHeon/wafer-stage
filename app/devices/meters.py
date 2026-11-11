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

RM_FACTORY: Optional[Callable] = None  # 시험 · --fake 가 가짜 장비 관리자를 꽂는다: callable() -> rm
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
            raise VisaError("장비를 열 수 없습니다 · %s · %s" % (self.addr, str(e)[:40]))
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
            raise VisaError("%s 가 아닌 장비입니다 · %s" % (self.NAME, idn[:40]))
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
        return "(보냄)"


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

    def check_scpi(self):
        """명령 세트가 SCPI 인가. 일부 펌웨어는 *LANG? 가 없다 - 그때는 SCPI 로 본다."""
        try:
            lang = self.query("*LANG?").upper()
        except VisaError:
            return True, ""
        if "SCPI" not in lang and "2450" not in lang:
            return False, "K2450 명령 세트를 SCPI 로 바꿔 주십시오 (장비 메뉴)"
        return True, ""

    def terminal(self):
        try:
            t = self.query(":ROUT:TERM?").strip().upper()
        except VisaError:
            return ""
        return "REAR" if t.startswith("REAR") else "FRON" if t.startswith("FRON") else ""

    def configure(self, volt, ilim_a, four_wire=True, nplc=1.0, avg_count=10, terminal="REAR"):
        """*RST → 단자 → 4단자 → 전압 소스(한도 · 수준) → 전류 측정(자동 범위) → 적분 → 평균 → 자동 영점. 출력은 켜지 않는다."""
        with self.lock:
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
            self.write(":SENS:CURR:RANG:AUTO ON")
            self.write(":SENS:CURR:NPLC %.6g" % float(nplc))
            if int(avg_count) > 1:
                self.write(":SENS:CURR:AVER:TCON REP")
                self.write(":SENS:CURR:AVER:COUN %d" % int(avg_count))
                self.write(":SENS:CURR:AVER ON")
            else:
                self.write(":SENS:CURR:AVER OFF")
            self.write(":SENS:AZER:ONCE")
            errs = self.drain_errors()
            if errs:
                raise VisaError("K2450 설정 오류 · " + errs[0][:40])

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
        """저항 한 번: 설정 → 출력 켜기 → 읽기 → (무슨 일이 있어도) 출력 끄기. dict(current, ohm, tripped, volt, four_wire)."""
        with self.lock:
            try:
                self.configure(volt, ilim_a, four_wire, nplc, avg_count)
                self.output(True)
                cur, trip = self.read()
            finally:
                self._off_quiet()
        return {"current": cur, "ohm": resistance(volt, cur), "tripped": trip, "volt": float(volt), "four_wire": bool(four_wire)}

    def contact_probe(self, volt, ilim_a, nplc=0.1):
        """접촉 확인용 약한 2단자 측정(평균 없음). 출력은 끝나면 끈다."""
        return self.measure_once(volt, ilim_a, four_wire=False, nplc=nplc, avg_count=1)

    def _off_quiet(self):
        """출력 끄기. 실패해도 예외를 올리지 않는다(이미 오류 중일 수 있다) - 실패는 로그에 남는다."""
        try:
            self.output(False)
        except VisaError:
            self.out_state = False
