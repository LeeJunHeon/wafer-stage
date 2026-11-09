"""instruments.py - 측정 장비 드라이버(pyvisa): Keithley 2110(K형 열전대 온도) · Keithley 2450(전압 걸고 전류 측정).

SCPI 문법은 사용자 PC 의 매뉴얼에서 확인했다(2110: 5장 TCOuple / 2450 Reference Manual 12장).
 K2110  [SENSe:]TCOuple:TYPE K · [SENSe:]TCOuple:RJUNction:RSELect REAL(기준점 내부) · [SENSe:]UNIT C · 1회 읽기 READ?
 K2450  *LANG? · :ROUTe:TERMinals FRONt|REAR · :SENSe:CURRent:RSENse ON(4단자) · :SOURce:FUNCtion VOLTage ·
        :SOURce:VOLTage <V> · :SOURce:VOLTage:ILIMit <A>(+ :ILIMit:TRIPped?) · :SENSe:FUNCtion "CURRent" ·
        :SENSe:CURRent:RANGe:AUTO OFF · :SENSe:CURRent:RANGe <A> · :SENSe:CURRent:NPLCycles 1 ·
        :SENSe:CURRent:AVERage:TCONtrol REPeat · :SENSe:CURRent:AVERage:COUNt n · :SENSe:CURRent:AVERage ON ·
        :SENSe:AZERo:ONCE · :OUTPut ON|OFF · :READ?

pyvisa 나 NI-VISA 가 없으면 VisaUnavailable 로 알리고 프로그램은 계속 돈다. 가짜 장비는 RM_FACTORY 를 바꿔 꽂는다(tools/fake_visa.py).
열전대 끊김 · 범위 넘음(9.9E37)은 "값 없음"(None)으로 돌려준다.
"""

import math
import threading

NO_DRIVER = "장비 통신 드라이버 없음 · NI-VISA 설치 필요"
OVERFLOW = 9.0e37                      # SCPI 범위 넘음 · 열전대 끊김 표기 9.91E+37
RANGES = [1e-8, 1e-7, 1e-6, 1e-5, 1e-4, 1e-3, 1e-2, 1e-1, 1.0]          # 2450 전류 측정 범위(10 nA ~ 1 A)
VISA_TIMEOUT_MS = 5000

RM_FACTORY = None                      # 시험 · 시뮬레이션이 가짜 장비 관리자를 꽂는다: callable() -> rm


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
        return pyvisa.ResourceManager()                 # NI-VISA 백엔드(기본)
    except Exception as e:                              # noqa: BLE001 - 드라이버 없음 · 로드 실패
        raise VisaUnavailable() from e


def list_resources():
    """(주소 목록, 안내 문구). 드라이버가 없으면 빈 목록 + 안내."""
    try:
        rm = _rm()
        return list(rm.list_resources()), ""
    except VisaUnavailable as e:
        return [], str(e)
    except Exception as e:                              # noqa: BLE001
        return [], "장비 목록 읽기 실패 · %s" % str(e)[:40]


def open_resource(addr):
    rm = _rm()
    try:
        res = rm.open_resource(addr)
    except Exception as e:                              # noqa: BLE001
        raise VisaError("장비 열기 실패 · %s · %s" % (addr, str(e)[:40])) from e
    try:
        res.timeout = VISA_TIMEOUT_MS
    except Exception:                                   # noqa: BLE001
        pass
    return res


# --------------------------------------------------------------------------
# 순수 함수
# --------------------------------------------------------------------------
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


def next_range(cur_range, current, up=1.05, down=0.95):
    """전류 범위 단계 변경(기존 프로그램 하위 VI 방식).

    전류 크기가 범위 끝 근처(up 배 넘음)면 10배 올리고, 한 단계 아래 범위의 끝(0.95 × 범위/10) 안쪽으로 떨어지면 10배 내린다.
    기존 VI 는 1.05 · 0.95 비교와 ×10 / ×0.1 반복으로 그림에만 보이는 값이라, 조건은 이렇게 읽었다(보고에 적음).
    """
    try:
        a = abs(float(current))
    except (TypeError, ValueError):
        return cur_range
    r = float(cur_range)
    idx = min(range(len(RANGES)), key=lambda k: abs(math.log10(RANGES[k] / r)))
    if a > up * RANGES[idx] and idx < len(RANGES) - 1:
        # 필요한 만큼 올린다(한 번에 여러 단계)
        while idx < len(RANGES) - 1 and a > up * RANGES[idx]:
            idx += 1
    elif idx > 0 and a < down * RANGES[idx - 1]:
        while idx > 0 and a < down * RANGES[idx - 1]:
            idx -= 1
    return RANGES[idx]


def visa_kind(addr):
    """VISA 주소의 연결 방식. GPIB0::18::INSTR → GPIB · USB0::… → USB · TCPIP… → LAN · ASRL… → 시리얼 · 그 밖 → ""."""
    a = str(addr or "").strip().upper()
    for pre, name in (("GPIB", "GPIB"), ("USB", "USB"), ("TCPIP", "LAN"), ("ASRL", "시리얼")):
        if a.startswith(pre):
            return name
    return ""


def is_open_circuit(current, floor_a):
    """전류 크기가 하한(기본 1 nA) 아래면 연결이 없는 것(열린 회로). 범위 넘음(None)은 열린 회로가 아니다."""
    if current is None:
        return False
    try:
        return abs(float(current)) < float(floor_a)
    except (TypeError, ValueError):
        return False


def parse_float(txt):
    """READ? 응답 한 줄 → float | None(범위 넘음 · 값 아님). 쉼표로 여럿이면 첫 값."""
    try:
        v = float(str(txt).strip().split(",")[0])
    except (TypeError, ValueError):
        return None
    if not math.isfinite(v) or abs(v) >= OVERFLOW:
        return None
    return v


# --------------------------------------------------------------------------
class _Dev:
    def __init__(self, addr):
        self.addr = addr
        self.res = None
        self.lock = threading.RLock()
        self.idn_text = ""
        self.cmds = []                                  # 시험용: 보낸 명령 기록(최근)

    def open(self):
        self.res = open_resource(self.addr)
        self.idn_text = self.query("*IDN?")
        return self.idn_text

    def _log(self, c):
        self.cmds.append(c)
        if len(self.cmds) > 20000:
            del self.cmds[:5000]

    def write(self, cmd):
        if self.res is None:
            raise VisaError("장비 연결 안 됨")
        with self.lock:
            self._log(cmd)
            try:
                self.res.write(cmd)
            except Exception as e:                      # noqa: BLE001
                raise VisaError("장비 통신 끊김 · %s" % str(e)[:40]) from e

    def query(self, cmd):
        if self.res is None:
            raise VisaError("장비 연결 안 됨")
        with self.lock:
            self._log(cmd)
            try:
                return str(self.res.query(cmd)).strip()
            except Exception as e:                      # noqa: BLE001
                raise VisaError("장비 통신 끊김 · %s" % str(e)[:40]) from e

    def close(self):
        r, self.res = self.res, None
        if r is not None:
            try:
                r.close()
            except Exception:                           # noqa: BLE001
                pass

    @property
    def is_open(self):
        return self.res is not None


def _drain_errors(dev):
    """장비 오류 큐(SYST:ERR?)를 비우며 읽는다. 오류 문구 목록."""
    out = []
    for _ in range(8):
        try:
            r = dev.query("SYST:ERR?")
        except VisaError:
            break
        if r.startswith("0") or not r:
            break
        out.append(r)
    return out


class K2110(_Dev):
    """Keithley 2110 5½ 자리 멀티미터 - K형 열전대 온도(°C, 기준점 내부)."""

    def configure(self, tc_type="K", rjun="REAL"):
        self.write("*RST")
        self.write("SENS:FUNC \"TCO\"")
        self.write("SENS:TCO:TYPE %s" % tc_type)
        self.write("SENS:TCO:RJUN:RSEL %s" % rjun)
        self.write("UNIT Cel")
        self.check_errors()

    def check_errors(self):
        errs = _drain_errors(self)
        if errs:
            raise VisaError("K2110 설정 오류 · " + errs[0][:40])

    def read_temp(self):
        """1회 읽기(°C). 열전대 끊김 · 범위 넘음이면 None."""
        return parse_float(self.query("READ?"))


class K2450(_Dev):
    """Keithley 2450 - 전압 소스, 전류 측정. 전류 한도(ILIMit)를 항상 건다. 실제 배선은 뒷면 3축 단자(FORCE HI/LO · SENSE HI/LO) = REAR."""

    out_state = False

    def check_scpi(self):  # noqa: D102 - K2450
        """명령 세트 확인. SCPI 가 아니면 (False, 안내)."""
        try:
            lang = self.query("*LANG?").upper()
        except VisaError:
            return True, ""                              # 일부 펌웨어는 *LANG? 가 없다 - 이때는 SCPI 로 본다
        if "SCPI" not in lang and "2450" not in lang:
            return False, "K2450 명령 세트를 SCPI 로 · 장비 메뉴"
        return True, ""

    def configure(self, p):
        """기존 프로그램 순서: 초기화 → 단자 → 원격 감지 → 전압 소스(수준) → 전류 측정(자동 범위 끔 · 시작 범위) → 적분 → 필터 → 자동 영점.
        출력 켬은 예비 냉각이 끝난 뒤(output)."""
        if self.out_state:                                  # 출력이 켜진 채로는 단자를 바꾸지 않는다(매뉴얼: 바꾸면 출력이 꺼짐) - 먼저 끈다
            self.output(False)
        self.write("*RST")                                  # 출력도 꺼진다
        want = "REAR" if str(p.get("terminal", "REAR")).upper().startswith("R") else "FRON"
        self.write(":ROUT:TERM %s" % want)                  # 단자는 *RST 바로 뒤, 다른 설정보다 먼저
        got = self.terminal()
        if got != want:
            raise VisaError("K2450 단자 설정 실패 · %s 로 읽힘" % (got or "?"))
        self.write(":SENS:CURR:RSEN %s" % ("ON" if p.get("four_wire", True) else "OFF"))
        self.write(":SOUR:FUNC VOLT")
        self.write(":SOUR:VOLT:ILIM %.6g" % float(p["ilim_a"]))
        self.write(":SOUR:VOLT %.6g" % float(p["volt"]))
        self.write(":SENS:FUNC \"CURR\"")
        auto = p.get("range_mode") == "auto"
        self.write(":SENS:CURR:RANG:AUTO %s" % ("ON" if auto else "OFF"))
        if not auto:
            self.write(":SENS:CURR:RANG %.6g" % clamp_range(float(p["start_range"]), float(p["ilim_a"])))
        self.write(":SENS:CURR:NPLC %.6g" % float(p["nplc"]))
        self.write(":SENS:CURR:AVER:TCON REP")
        self.write(":SENS:CURR:AVER:COUN %d" % int(p["filter_n"]))
        self.write(":SENS:CURR:AVER ON")
        self.write(":SENS:AZER:ONCE")
        errs = _drain_errors(self)
        if errs:
            raise VisaError("K2450 설정 오류 · " + errs[0][:40])

    def terminal(self):
        """:ROUT:TERM? 로 지금 단자. "REAR" | "FRON" | "" (읽지 못함)."""
        try:
            t = self.query(":ROUT:TERM?").strip().upper()
        except VisaError:
            return ""
        return "REAR" if t.startswith("REAR") else "FRON" if t.startswith("FRON") else ""

    def set_range(self, r):
        self.write(":SENS:CURR:RANG %.6g" % float(r))

    def output(self, on):
        self.write(":OUTP %s" % ("ON" if on else "OFF"))
        self.out_state = bool(on)

    def read(self):
        """(전류 A | None, 전류 한도 걸림 bool)."""
        cur = parse_float(self.query(":READ?"))
        trip = False
        try:
            trip = self.query(":SOUR:VOLT:ILIM:TRIP?").strip() in ("1", "+1")
        except VisaError:
            trip = False
        return cur, trip


__all__ = ["K2110", "K2450", "VisaError", "VisaUnavailable", "list_resources", "resistance", "is_open_circuit", "visa_kind", "next_range", "clamp_range", "limit_ok", "NO_DRIVER", "RANGES"]
