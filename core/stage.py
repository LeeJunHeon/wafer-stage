"""stage.py - 펌웨어 V11(firmware/stage_v11/stage_v11.ino) 시리얼 드라이버.

프로토콜은 .ino 에서 그대로 따왔다.
  보내는 것   : "st" / "mx 100.0" / "my 50.5" / "save"  (줄바꿈으로 끝)
  상태 한 줄  : "ST X=16000 Y=8000 X2=16000 HX=1 HY=1 DIRTY=0"
  이동 보고   : "  X  + cmd=16000 out=16000 t=2783.53ms | X=16000 Y=8000"
                -> ' | X=' 가 들어 있는 줄이 '이동 끝' 신호다.
  이미 도착   : "  이미 그 위치"          (움직이지 않으므로 보고 줄이 안 온다)
  거부        : "  [거부] 원점 미확정 ..." / "  [거부] 범위 밖  목표=..."
  경고        : 줄 안에 "[!]" (X 틀어짐 / CPU / 중단)
  저장        : "save" -> "  저장됨 (신뢰 확정)"
  릴레이(V10) : "r 3 1" / "r all 0" / "rs" -> "RS R1=0 ... R10=0"
  통신 감시   : "wd 5000"(V11) -> 끊기면 펌웨어가 "[!] 통신 끊김 · 릴레이 전부 꺼짐"
  위치 세팅   : "sp <x펄스> <y펄스>" -> "  PC 위치로 세팅 (저장됨)" (사진 호밍)

--dry 는 시리얼 없이 펌웨어를 흉내 낸다(이동 · 원점 · 릴레이 · sp · 통신 감시).
통신 감시는 펌웨어처럼 스레드가 시간을 보고 판정한다. 검증용으로 '실제 위치'를
보고 위치와 어긋나게(탈조) 만들 수 있다(dry_slip) - 사진 호밍 검증이 쓴다.

펌웨어가 한글을 찍으므로 디코딩은 UTF-8 + errors="replace" 로 한다 (깨져도
파싱은 ASCII 부분만 보므로 문제 없다).

수동 확인:
  python -m core.stage --list-ports
  python -m core.stage st
  python -m core.stage mx 100
  python -m core.stage my 50
  python -m core.stage save
"""

import argparse
import json
import os
import re
import sys
import threading
import time

from . import paths

# --dry 이동을 실제처럼 오래 걸리게 만든다(초). 기본 0 = 즉시.
# 비상정지가 '이동 중' 에 도착하는 상황은 이 시간이 없으면 재현되지 않는다.
_DMS = os.environ.get("WAFER_STAGE_DRY_MOVE_S", "0") or "0"
DRY_REAL = _DMS.lower() == "real"                  # "real" = 속도 · 가속으로 계산한 사다리꼴 시간(펌웨어와 같은 시간)
DRY_MOVE_S = 0.0 if DRY_REAL else float(_DMS)
DRY_TIME_SCALE = float(os.environ.get("WAFER_STAGE_DRY_TIME_SCALE", "1") or 1)
DRY_TICK_S = 0.05                  # 자는 동안 이만큼마다 중단을 확인한다
# --dry 가 흉내 낼 펌웨어 버전(검증용). 실제 연결에서는 배너에서 읽는다.
DRY_FW = os.environ.get("WAFER_STAGE_DRY_FW", "V11").upper()
# --dry 는 늘 "원점 있음" 으로 답해 왔다. 원점이 없는 상태의 동작(수동 이동만
# 허용, goto 거절)을 검증하려면 그것도 흉내 낼 수 있어야 한다.
DRY_NO_HOME = os.environ.get("WAFER_STAGE_DRY_NO_HOME", "") not in ("", "0")

JOG_MAX_PULSE = 8000               # 펌웨어 V8 의 jx/jy 한도와 같은 값
# 원점(0)은 끝단에서 이만큼 물러난 자리다. 펌웨어의 HOME_GAP(320 펄스)과 같은
# 값이어야 한다. settings.json 의 마커 좌표가 이 원점 기준으로 실측되어 있어서,
# 이격 거리는 취향이 아니라 좌표계의 일부다 - 바꾸면 모든 샘플이 그만큼 어긋난다.
# 값은 설정(origin_gap_mm, 고급)이 configure() 로 넣는다 - 아래는 그 전의 초기값이다.
ORIGIN_GAP_MM = 2.0

PPMM = 160.0                  # 160 펄스 = 1mm (.ino 의 PPMM)
# 속도·시간은 설정(motion.* · serial.*)이 configure() 로 넣는다. 아래는 초기값일 뿐이다
# (단독 CLI 실행용). 펌웨어 펄스 폭 같은 타이밍은 펌웨어 상수라 여기서 다루지 않는다.
DEFAULT_SPEED_PPS = 6000      # 절대 이동
JOG_PPS = 6000                # 원점이 있을 때 조그
JOG_SLOW_PPS = 1500           # 원점 없이 수동으로 몰 때 - 끝단에 닿아도 살살
# 가동범위(기본값). 펌웨어의 X_MAX/Y_MAX/Z_MAX 는 기계 한계이고, 앱은 그 안에서
# 실사용 범위를 settings.json 의 "limits" 로 정한다 - set_limits() 가 아래 값을
# 런타임에 바꾼다. 다른 모듈은 stage.Z_MAX_MM 처럼 모듈 속성으로 읽으므로 값을
# 복사해 두지 말고 매번 stage_mod.X 로 읽어야 바뀐 값을 본다.
X_MAX_PULSE = 39620           # 247.6mm
Y_MAX_PULSE = 39640           # 247.8mm
# 펌웨어 V11(V9 와 같은 값)의 Z_PPMM · Z_MAX 와 같은 값이어야 한다.
# 실측: 위 끝단에서 2mm 이격한 0 에서 아래 끝단까지 45mm (2026-09-16 재실측).
Z_PPMM      = 160.0           # 실측 확인 2026-09-14 (1mm 명령 = 1mm 이동)
Z_MAX_PULSE = 7200            # 45mm - 실측값
Z_MAX_MM    = Z_MAX_PULSE / Z_PPMM
LIMIT_MIN_MM, LIMIT_MAX_MM = 1.0, 1000.0   # set_limits 가 받는 값의 범위


def set_limits(x_max_mm=None, y_max_mm=None, z_max_mm=None):
    """축 상한(mm)을 바꾼다. 펄스로 환산해(반올림) 그 축의 상한에 반영한다.

    None 이거나 1~1000 mm 를 벗어난 값은 무시하고 지금 값을 유지한다.
    calib.set_marker_mm 과 같은 방식 - 설정을 저장하면 다음 이동부터 적용된다.
    """
    global X_MAX_PULSE, Y_MAX_PULSE, Z_MAX_PULSE, Z_MAX_MM

    def _ok(v):
        try:
            v = float(v)
        except (TypeError, ValueError):
            return None
        return v if LIMIT_MIN_MM <= v <= LIMIT_MAX_MM else None

    x, y, z = _ok(x_max_mm), _ok(y_max_mm), _ok(z_max_mm)
    if x is not None:
        X_MAX_PULSE = int(round(x * PPMM))
    if y is not None:
        Y_MAX_PULSE = int(round(y * PPMM))
    if z is not None:
        Z_MAX_PULSE = int(round(z * Z_PPMM))
        Z_MAX_MM = Z_MAX_PULSE / Z_PPMM
    return limits_mm()


def configure(speed_pps=None, jog_pps=None, jog_slow_pps=None, line_timeout_s=None,
              move_timeout_s=None, boot_wait_s=None, origin_gap_mm=None):
    """설정(settings_schema)의 속도·시간을 반영한다. None 은 지금 값 유지."""
    global DEFAULT_SPEED_PPS, JOG_PPS, JOG_SLOW_PPS, LINE_TIMEOUT_S, MOVE_TIMEOUT_S
    global BOOT_MAX_S, ORIGIN_GAP_MM
    if speed_pps:
        DEFAULT_SPEED_PPS = int(speed_pps)
    if jog_pps:
        JOG_PPS = int(jog_pps)
    if jog_slow_pps:
        JOG_SLOW_PPS = int(jog_slow_pps)
    if line_timeout_s:
        LINE_TIMEOUT_S = float(line_timeout_s)
    if move_timeout_s:
        MOVE_TIMEOUT_S = float(move_timeout_s)
    if boot_wait_s:
        BOOT_MAX_S = float(boot_wait_s)
    if origin_gap_mm is not None:
        ORIGIN_GAP_MM = float(origin_gap_mm)


def limits_mm():
    """지금 쓰는 축 상한(mm). 화면의 limits 와 조그 자르기가 이 값을 쓴다."""
    return {"x_max_mm": X_MAX_PULSE / PPMM, "y_max_mm": Y_MAX_PULSE / PPMM,
            "z_max_mm": Z_MAX_PULSE / Z_PPMM}


def fw_limits_pulse(banner):
    """부팅 배너의 "X 0~39620  Y 0~39640  Z 0~7200" 에서 펌웨어 한계(펄스)를 읽는다.

    못 읽은 축은 빠진다. 연결 때 앱 설정이 이 한계보다 크면 경고를 남기는 데 쓴다.
    """
    out = {}
    for ax in ("x", "y", "z"):
        m = re.search(r"(?<![A-Z])%s\s+0~(\d+)" % ax.upper(), banner or "")
        if m:
            out[ax] = int(m.group(1))
    return out

# Z=0 은 맨 위(들어 올린 자리)다. 값이 커질수록 아래로 내려가고 펄스 부호도 같다.
# 원점은 위쪽 하드스톱으로 밀어서 잡는다 - 끝단 이동(touch_end)은 Z 에서 '위로' 다.

BAUD = 115200
BOOT_MAX_S = 5.0              # 포트를 열면 DTR 로 보드가 리셋된다. 배너를 이만큼 기다린다
BOOT_QUIET_S = 1.0            # 이만큼 조용하면 배너가 끝난 것으로 본다
MOVE_TIMEOUT_S = 30.0         # 가장 긴 이동 248mm 가 v6000 에서 약 7초
HOME_TIMEOUT_S = 90.0         # 원점 탐색은 스트로크 전체를 훑을 수 있다
LINE_TIMEOUT_S = 5.0
WRITE_TIMEOUT_S = 1.0         # serial.Serial write_timeout - USB 가 막혀 쓰기가 영원히 안 끝나는 일을 막는다(비상정지 '!' 포함)
FZ_SEARCH_PULSE = 1600        # fz 탐색 상한 10 mm(펌웨어 HOME_SEARCH_MAX)
FZ_TIMEOUT_S = 25.0
POLL_S = 0.2                  # 한 번 읽기에 기다리는 시간 (포트 설정은 여기서 고정)

DONE_MARK = " | X="           # 이동 보고 줄
ALREADY_MARK = "이미 그 위치"
REJECT_MARK = "[거부]"
N_RELAY = 10                  # 펌웨어 V10+ 의 릴레이 R1~R10
WD_MARK = "통신 끊김"          # 펌웨어 V11 통신 감시가 릴레이를 끈 줄
WARN_MARK = "[!]"


import functools  # noqa: E402

SERIAL_FACTORY = None               # 시험용(tools/fake_v11.py): fn(port, baud, timeout) -> pyserial 모양 객체. 설정되면 진짜 시리얼 대신 쓴다


def _tx(fn):
    """거래 잠금: 명령 쓰기부터 그 응답(완료 줄 · ST · RS) 수집까지를 한 덩어리로. 입력 비우기(_drain)는 이 안에서만 일어나므로 다른 거래의 응답을 지우지 않는다."""
    @functools.wraps(fn)
    def w(self, *a, **k):
        with self._txn:
            return fn(self, *a, **k)
    return w


class StageError(Exception):
    pass


class StageTimeout(StageError):
    """응답이 시간 안에 오지 않음. 호출 쪽(core/motion.py)이 '!' + 재동기를 한다."""


class StageWriteTimeout(StageError):
    """시리얼 쓰기가 write_timeout 안에 끝나지 않음 - 통신 끊김으로 다룬다."""


class StageGuard(StageError):
    """쓰기 직전 검사(write_guard)가 거절함 - 비상정지 · 정지 세대가 바뀌었다."""


class StageLate(StageGuard):
    """사람이 보낸 이동 명령이 받은 지 한도(1 s) 안에 첫 줄을 못 썼다 - 늦게 움직이지 않는다."""


class StageIOError(StageTimeout):
    """읽기 · 쓰기의 입출력 예외(USB 뽑힘 등) - 시간 초과와 같이 다룬다(통신 끊김 · 위치 못 믿음)."""


class StageStopped(StageError):
    """[정지] 로 끊긴 이동(비상정지가 아니다 - 잠그지 않고 위치 카운터를 믿는다)."""


# --------------------------------------------------------------------------
# 포트 찾기
# --------------------------------------------------------------------------
def list_ports():
    from serial.tools import list_ports as lp
    return list(lp.comports())


def pick_port(want=None):
    """want 가 있으면 그대로. 없으면 자동으로 하나만 고른다.

    'Arduino' 가 설명에 들어간 포트가 정확히 1개거나, COM 포트가 하나뿐일 때만
    자동으로 고른다. 애매하면 목록을 보여주고 --port 를 요구한다.
    """
    if want:
        return want
    ports = list_ports()
    if not ports:
        raise StageError("시리얼 포트 없음 (USB 연결 확인)")
    ard = [p for p in ports if "arduino" in ((p.description or "") + " " +
                                             (p.manufacturer or "")).lower()]
    if len(ard) == 1:
        return ard[0].device
    if len(ports) == 1:
        return ports[0].device
    # 첫 줄만 화면에 나간다. 목록은 파일 로그에서 본다.
    msg = ["포트 자동 선택 불가 (설정에서 포트 지정)"]
    for p in ports:
        msg.append("  %-8s %s" % (p.device, p.description))
    raise StageError("\n".join(msg))


def _major_of(fw):
    """"V10" -> 10. 읽을 수 없으면 0(검사하지 않는다)."""
    m = re.match(r"V(\d+)$", (fw or "").strip(), re.I)
    return int(m.group(1)) if m else 0


def _ppmm(ax):
    return Z_PPMM if ax == "z" else PPMM


def _max_mm(ax):
    """그 축의 상한(mm). set_limits 가 바꾼 뒤에도 늘 지금 값에서 계산한다."""
    top = {"x": X_MAX_PULSE, "y": Y_MAX_PULSE, "z": Z_MAX_PULSE}[ax]
    return top / _ppmm(ax)


def _fw_of(banner):
    """배너에서 펌웨어 버전을 읽는다. "=== 3-AXIS V7 ===" -> "V7"."""
    m = re.search(r"3-AXIS\s+(V\d+)", banner or "", re.I)
    return m.group(1).upper() if m else ""


# --------------------------------------------------------------------------
class Stage:
    def __init__(self, port=None, dry=False, log_path=None):
        self.port_name = port
        self.dry = bool(dry)
        self.dry_unplugged = False     # 시험: dry 에서 USB 가 뽑힌 척(읽기 · 쓰기 오류)
        self.ser = None
        self.banner = ""
        self.fw_version = ""           # 배너에서 읽은 "V8" … "V11"
        self.needs_home = False        # 배너에 "원점없음" 이 있었나
        self.log_path = log_path or os.path.join(paths.LOGS_DIR, "serial.log")
        # 주고받은 줄을 그대로 넘겨받을 곳(서버가 화면 로그로 보낸다). 파일 로그는
        # 이것과 무관하게 항상 남는다 - 화면에서 껐다고 기록이 사라지면 안 된다.
        self.on_line = None
        self.warnings = []
        # 비상정지('!')를 보낸 뒤 상태. 펌웨어는 '명령마다' abortFlag 를 지우므로
        # (.ino 395행) 다음 명령을 그냥 보내면 그대로 움직인다. 그래서 드라이버가
        # 잠가야 한다 - 원점을 다시 잡기 전까지 이동을 보내지 않는다.
        self._aborted = False
        self.abort_seq = 0                          # 비상정지 횟수 - 거래가 시작할 때 기억해 두고 끊긴 보고를 받았을 때 비교한다
        self.stop_seq = 0                           # 정지 횟수
        self.stop_wall = []                         # 시험용: 정지 '!' 를 쓴 벽시계 시각
        self._rbuf = b""                            # 줄바꿈으로 끝나지 않은 조각(다음 읽기에 이어 붙인다)
        self.cut_info = {}                          # 마지막으로 끊긴 이동: {axis, cmd, out, wall}
        self.bang_wall = []                         # 시험용: 비상정지 "!" 를 쓴 벽시계 시각들
        # dry 모드의 가상 위치. 이동을 흉내만 내고 위치가 0 에 머물면 화면 검증이
        # 무의미해진다(포인터·맵이 안 움직인다). 보낸 명령대로 위치를 옮겨 둔다.
        self._dry_xy = {"x": 0.0, "y": 0.0, "z": 0.0}
        # 원점은 축마다 따로다(펌웨어의 HX/HY/HZ 가 진실). dry 도 같게 흉내 낸다.
        self._dry_homed = {"x": not DRY_NO_HOME, "y": not DRY_NO_HOME,
                           "z": not DRY_NO_HOME}
        self.speed_pps = None          # 아직 펌웨어에 속도를 보낸 적이 없다
        # dry 의 릴레이(V10). 전원 투입 때 전부 꺼짐.
        self._dry_relay = [False] * N_RELAY
        # dry 통신 감시(V11): 펌웨어처럼 마지막 줄을 받은 뒤 시간이 지나면 릴레이를 끈다.
        self._dry_lock = threading.RLock()
        self._dry_wd_ms = 0
        self._dry_last_rx = time.monotonic()
        self._dry_tripped = False
        self._dry_busy = False         # 블로킹 이동 중(펌웨어는 이때 판정하지 않는다)
        self._dry_stall_until = 0.0    # 검증용: 이 시각까지 받은 줄을 '못 받은 것' 으로
        self._dry_alive = False
        # 검증용 '실제 위치' 오프셋(탈조). 실제 = 보고 + off. sp 는 보고만 바꾼다.
        self._dry_off = {"x": 0.0, "y": 0.0}
        # 펌웨어가 스스로 보낸 사건(통신 끊김 등). fn(kind, line) - 워커·감시 스레드에서 불린다.
        self.on_event = None
        # 쓰기 직전 검사. fn(cmd) 가 StageGuard 를 던지면 시리얼에 쓰지 않는다('!' 와 읽기 전용 명령은 검사하지 않는다).
        self.write_guard = None
        self._wlock = threading.RLock()                    # 쓰기 잠금(짧다): '!' 도 이것만 잡는다
        self._txn = threading.RLock()                      # 거래 잠금: 명령 + 응답 수집
        self.dry_hang = False          # 검증용: 이동 완료가 영영 오지 않는 척(이동 시간 한도 시험)
        self.wd_tripped_at = None
        self.profile = None            # 마지막으로 보낸 (가속도, 시작 속도)
        self.eeprom_line = ""          # 부팅 배너의 EEPROM 줄

    # ---- 로그 ----------------------------------------------------------
    def _log(self, arrow, text):
        if self.on_line is not None:
            try:
                self.on_line(arrow, text.rstrip("\r\n"))
            except Exception:          # noqa: BLE001
                pass                   # 화면 전달 실패로 시리얼을 막지 않는다
        try:
            os.makedirs(os.path.dirname(self.log_path), exist_ok=True)
            with open(self.log_path, "a", encoding="utf-8") as f:
                now = time.time()
                f.write("%s.%03d %s %s\n" % (time.strftime("%H:%M:%S", time.localtime(now)), int((now % 1) * 1000), arrow,
                                             text.rstrip("\r\n")))
        except OSError:
            pass                       # 로그 실패로 동작을 막지는 않는다

    # ---- 열기/닫기 ------------------------------------------------------
    def open(self):
        if self.dry:
            self.eeprom_line = "  EEPROM: " + ("기록 없음" if DRY_NO_HOME else "마지막 저장 위치 복원")
            self.banner = ("=== 3-AXIS %s ===\n  X 0~%d  Y 0~%d  Z 0~%d\n%s\n(dry) 시리얼 없이 실행"
                           % (DRY_FW, 39620, 39640, 7200, self.eeprom_line))
            self.fw_version = DRY_FW
            self._log("--", self.banner)
            self._dry_alive = True
            threading.Thread(target=self._dry_watch, name="dry-wd", daemon=True).start()
            return self.banner
        try:
            import serial
        except ImportError:
            raise StageError("pyserial 이 필요합니다:  pip install pyserial")
        if SERIAL_FACTORY is None:
            self.port_name = pick_port(self.port_name)
        else:
            self.port_name = self.port_name or "FAKE"
        try:
            if SERIAL_FACTORY is None:
                self.ser = serial.Serial(self.port_name, BAUD, timeout=POLL_S, write_timeout=WRITE_TIMEOUT_S)
            else:
                self.ser = SERIAL_FACTORY(self.port_name, BAUD, POLL_S)
                self.ser.write_timeout = WRITE_TIMEOUT_S
        except Exception as e:
            raise StageError("%s 열기 실패 (다른 프로그램이 사용 중인지 확인)\n"
                             "  %s\n"
                             "  아두이노 IDE 의 시리얼 모니터가 열려 있으면 닫습니다."
                             % (self.port_name, e))
        self._log("--", "open %s @%d" % (self.port_name, BAUD))
        # 포트를 열면 보드가 리셋되어 부팅 배너가 나온다. 조용해질 때까지 읽는다.
        lines = []
        t0 = time.time()
        last = time.time()
        while time.time() - t0 < BOOT_MAX_S:
            ln = self._readline()
            if ln is None:
                if lines and time.time() - last >= BOOT_QUIET_S:
                    break
                continue
            lines.append(ln)
            last = time.time()
        self.banner = "\n".join(lines)
        self.fw_version = _fw_of(self.banner)
        self.needs_home = "원점없음" in self.banner
        return self.banner

    def eeprom_state(self):
        """연결 시 EEPROM 상태: restored(마지막 저장 위치 복원) | unsaved(save 없이 꺼짐) | none(기록 없음) | ''(모름)."""
        t = self.eeprom_line or self.banner or ""
        if "복원" in t:
            return "restored"
        if "save 하지 않고" in t or "위치 불신" in t:
            return "unsaved"
        if "기록 없음" in t:
            return "none"
        return ""

    def close(self):
        self._dry_alive = False
        if self.ser is not None:
            self._log("--", "close")
            try:
                self.ser.close()
            except Exception:
                pass
            self.ser = None

    def __enter__(self):
        self.open()
        return self

    def __exit__(self, *a):
        self.close()

    # ---- 저수준 송수신 --------------------------------------------------
    SAFE_CMDS = ("!", "st", "rs", "p", "r all 0")           # 가드 없이 나갈 수 있는 명령(읽기 전용 · 정지 · 전부 끄기)

    def _write(self, cmd):
        with self._wlock:                                   # 가드 검사 ~ 쓰기 ~ flush 를 한 덩어리로('!' 쓰기와 섞이지 않게)
            g = self.write_guard
            if g is not None and cmd not in self.SAFE_CMDS and not (cmd.startswith("r ") and cmd.endswith(" 0")):
                g(cmd)                                      # 거절하면 StageGuard - 아래 쓰기에 닿지 않는다
            if not self.dry:
                self._drain()
            if self.dry:
                if self.dry_unplugged:
                    raise StageIOError("쓰기 실패: 포트 없음 (dry 뽑힘)")
                self._log("->", cmd)
                print("  (dry) %s" % cmd)
                self._dry_rx()
                return
            self._ser_write((cmd + chr(10)).encode("ascii"))
            self._log("->", cmd)

    def _ser_write(self, data, flush=True):
        """쓰기 한 번. write_timeout 을 넘으면 StageWriteTimeout + 통신 끊김 사건(경보 · 링크 끊김 처리).
        flush=False: '!' 는 flush 로 기다리지 않는다(Windows pyserial 의 flush 는 상한 없이 돈다)."""
        try:
            self.ser.write(data)
            if flush:
                self.ser.flush()
        except Exception as e:                              # noqa: BLE001
            if e.__class__.__name__ == "SerialTimeoutException":
                self._event("write_timeout", "쓰기 시간 초과")
                raise StageWriteTimeout("쓰기 시간 초과 · 통신 끊김") from e
            raise StageIOError("쓰기 실패 · 통신 끊김: %s" % e) from e

    def _drain(self):
        """보내기 전에 들어와 있던 줄을 비운다 - 이전 명령의 완료 보고 · RS 가 이번 명령의 응답으로 읽히지 않게(응답은 요청한 명령에 맞춘다)."""
        ser = self.ser
        if ser is None:
            return
        try:
            n = ser.in_waiting
        except Exception:                                   # noqa: BLE001
            return
        if n <= 0:
            return
        try:
            raw = ser.read(n)
        except Exception:                                   # noqa: BLE001
            return
        for ln in raw.decode("utf-8", errors="replace").splitlines():
            if not ln.strip():
                continue
            self._log("<-", "(버림) " + ln)
            if "EEPROM:" in ln:
                self.eeprom_line = ln.strip()
            if "3-AXIS" in ln:
                self.speed_pps = None
                self.profile = None
                self._event("reboot", ln)
            if WD_MARK in ln:
                self._event("wd_trip", ln)

    def _readline(self, timeout=POLL_S):
        """한 줄 읽기. 타임아웃은 포트를 열 때 한 번만 정한다.

        pyserial 은 timeout 에 값을 대입할 때마다 포트를 재설정(Windows 에서는
        SetCommTimeouts + 버퍼 정리)한다. 매 읽기마다 대입했더니 마침 들어오던
        부팅 배너와 ST 응답이 통째로 사라져 '응답 없음' 이 됐다. 그래서 값이
        실제로 바뀔 때만 대입한다.
        """
        if self.dry or self.ser is None:
            return None
        if timeout is not None and self.ser.timeout != timeout:
            self.ser.timeout = timeout
        try:
            raw = self.ser.readline()
        except Exception as e:                              # noqa: BLE001  - SerialException 등: 통신 끊김
            raise StageIOError("읽기 실패 · 통신 끊김: %s" % e) from e
        if not raw:
            return None
        raw = self._rbuf + raw
        self._rbuf = b""
        if not raw.endswith(b"\n"):                          # pyserial 은 시간 안에 줄바꿈이 없으면 줄 중간까지만 준다 - 이어 붙인다
            self._rbuf = raw[-512:]
            return None
        ln = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self._log("<-", ln)
        # 연결 중에 배너가 다시 보이면 보드가 리셋된 것이다(USB 흔들림·워치독).
        # 펌웨어 속도는 기본값으로 돌아갔는데 우리는 마지막으로 보낸 값을 믿고
        # 있어서 v 를 다시 보내지 않는다. 믿음을 버려 다음 이동에서 보내게 한다.
        if "3-AXIS" in ln:
            self.speed_pps = None
            self.profile = None
            self._event("reboot", ln)
        if WD_MARK in ln:
            self._event("wd_trip", ln)
        return ln

    def _event(self, kind, line):
        if kind == "wd_trip":
            self.wd_tripped_at = time.time()
        fn = self.on_event
        if fn is not None:
            try:
                fn(kind, line)
            except Exception:              # noqa: BLE001
                pass

    # ---- dry 펌웨어 흉내: 통신 감시 ------------------------------------
    def _dry_rx(self):
        """dry 가 줄 하나를 받았다(펌웨어의 lastRx 갱신)."""
        with self._dry_lock:
            if time.monotonic() < self._dry_stall_until:
                return                     # 검증용: 링크가 끊긴 척
            self._dry_last_rx = time.monotonic()
            self._dry_tripped = False

    def _dry_watch(self):
        """펌웨어 loop() 의 통신 감시. 블로킹 이동 중에는 판정하지 않는다."""
        while self._dry_alive:
            time.sleep(0.05)
            line = None
            with self._dry_lock:
                if (self._dry_wd_ms > 0 and not self._dry_busy and not self._dry_tripped
                        and any(self._dry_relay)
                        and (time.monotonic() - self._dry_last_rx) * 1000.0 > self._dry_wd_ms):
                    self._dry_relay = [False] * N_RELAY
                    self._dry_tripped = True
                    line = "  [!] 통신 끊김 · 릴레이 전부 꺼짐"
            if line:
                self._log("<-", line)
                self._event("wd_trip", line)

    def dry_stall(self, seconds):
        """검증용: seconds 동안 dry 펌웨어가 받은 줄을 무시한다(통신 끊김 흉내)."""
        with self._dry_lock:
            self._dry_stall_until = time.monotonic() + float(seconds)

    def dry_slip(self, dx_mm, dy_mm):
        """검증용: 실제 위치를 보고 위치에서 (dx, dy) 만큼 어긋나게 한다(탈조)."""
        self._dry_off["x"] += float(dx_mm)
        self._dry_off["y"] += float(dy_mm)

    def dry_true_xy(self):
        """dry 의 실제(물리) 위치 mm. 합성 카메라가 헤드 마커를 그리는 자리다."""
        return (self._dry_xy["x"] + self._dry_off["x"],
                self._dry_xy["y"] + self._dry_off["y"])

    def _tok(self):
        """거래 시작 때 기억할 (비상정지 횟수, 정지 횟수)."""
        return (self.abort_seq, self.stop_seq)

    def _collect(self, deadline_s, is_done, what, tok=None):
        """완료 줄이 올 때까지 읽는다. [거부] 면 예외, [!] 는 모아 둔다. tok: 거래 시작 때의 _tok() - 끊긴 보고가 정지인지 비상정지인지 가른다."""
        t0 = time.time()
        seen = []
        while time.time() - t0 < deadline_s:
            ln = self._readline()
            if ln is None:
                continue
            seen.append(ln)
            if REJECT_MARK in ln:
                raise StageError("펌웨어 거부: %s" % ln.strip())
            if "[!] 중단" in ln:
                # '!' 로 끊긴 이동이다. 보고 줄 형식은 정상 완료와 같아서 그냥
                # 두면 '완료' 로 읽히고, goto_mm 이 이어서 Y 를 보내 버린다.
                self.cut_info = {"line": ln.strip(), "wall": time.time()}
                if tok is not None and self.abort_seq == tok[0]:
                    raise StageStopped("정지로 중단됨: %s" % ln.strip())      # 비상정지가 아니다 - 잠그지 않는다
                self._aborted = True
                raise StageError("비상정지로 중단됨: %s" % ln.strip())
            if WARN_MARK in ln and not is_done(ln):
                self.warnings.append(ln.strip())
                print("  경고(펌웨어): %s" % ln.strip())
            if is_done(ln):
                if WARN_MARK in ln:
                    self.warnings.append(ln.strip())
                return ln
        raise StageTimeout("%s 응답이 %.0f초 안에 오지 않았습니다. 마지막 줄: %s"
                           % (what, deadline_s, seen[-1] if seen else "(없음)"))

    # ---- 명령 ----------------------------------------------------------
    @_tx
    def status(self, timeout=None):
        """'st' -> ST 줄을 dict 로. 펄스와 mm 를 함께 담는다."""
        if self.dry:
            if self.dry_unplugged:
                raise StageIOError("읽기 실패: 포트 없음 (dry 뽑힘)")
            self._dry_rx()                                  # 진짜는 st 를 쓰므로 펌웨어가 줄을 받는다(통신 감시 갱신)
            x, y, z = self._dry_xy["x"], self._dry_xy["y"], self._dry_xy["z"]
            return {"x_pulse": int(x * PPMM), "y_pulse": int(y * PPMM),
                    "x2_pulse": int(x * PPMM), "z_pulse": int(z * Z_PPMM),
                    "homed_x": self._dry_homed["x"], "homed_y": self._dry_homed["y"],
                    "homed_z": self._dry_homed["z"],
                    "dirty": False, "x_mm": x, "y_mm": y, "z_mm": z, "raw": "(dry)"}
        self._write("st")
        ln = self._collect(timeout or LINE_TIMEOUT_S, lambda s: s.startswith("ST ") and "DIRTY=" in s, "st")
        d = {}
        for tok in ln.split():
            if "=" in tok:
                k, v = tok.split("=", 1)
                d[k] = v
        try:
            out = {
                "x_pulse": int(d["X"]), "y_pulse": int(d["Y"]),
                "x2_pulse": int(d.get("X2", d["X"])),
                "homed_x": d.get("HX") == "1", "homed_y": d.get("HY") == "1",
                "homed_z": d.get("HZ") == "1",
                "dirty": d.get("DIRTY") == "1", "raw": ln.strip(),
            }
        except (KeyError, ValueError):
            raise StageError("ST 줄을 해석할 수 없습니다: %s" % ln)
        out["x_mm"] = out["x_pulse"] / PPMM
        out["y_mm"] = out["y_pulse"] / PPMM
        # V8 이하에는 Z 가 없다. 그때는 z 를 모른다고 답한다(죽지 않는다).
        if "Z" in d:
            out["z_pulse"] = int(d["Z"])
            out["z_mm"] = out["z_pulse"] / Z_PPMM
        else:
            out["z_pulse"] = None
            out["z_mm"] = None
        return out

    def _dry_duration(self, pulses):
        """dry 이동 시간(초). real 이면 펌웨어와 같은 사다리꼴(가속 · 정속 · 감속), 아니면 고정 DRY_MOVE_S."""
        if not DRY_REAL:
            return DRY_MOVE_S
        v = float(self.speed_pps or DEFAULT_SPEED_PPS)
        a, b = (self.profile or (40000, 300))
        a, b = float(a), min(float(b), v)
        n = abs(float(pulses))
        d_acc = (v * v - b * b) / (2.0 * a)
        if n >= 2.0 * d_acc:
            t = 2.0 * (v - b) / a + (n - 2.0 * d_acc) / v
        else:
            vp = (b * b + a * n) ** 0.5
            t = 2.0 * (vp - b) / a
        return t / max(1e-6, DRY_TIME_SCALE)

    def _dry_wait(self, what, deadline_s=None, tok=None, pulses=None, on_cut=None, dur=None):
        """dry 이동이 걸리는 시간을 흉내 낸다. 자는 동안 중단을 확인한다.

        조각으로 나눠 자면서 비상정지 · 정지 횟수를 보기 때문에, 이동 중에 들어온 '!' 가
        실제 펌웨어처럼 이동을 끊는다(on_cut(진행 비율) 로 끊긴 자리를 남긴다).
        tok 는 거래 시작 때의 _tok() - 이미 잠겨 있던 상태(비상정지 뒤 수동 이동)는 중단이 아니다.
        """
        tok = tok or self._tok()
        total = dur if dur is not None else self._dry_duration(pulses if pulses is not None else 0)
        t0 = time.time()
        if self.dry_hang and deadline_s:
            end = t0 + deadline_s + 0.01              # 완료가 안 온다 → 한도에서 시간 초과
            hang = True
        else:
            end = t0 + total
            hang = False
        if total <= 0 and not hang:
            return
        self._dry_busy = True
        try:
            while time.time() < end:
                cut = None
                if self.abort_seq != tok[0]:
                    cut = StageError("비상정지로 중단됨 · (dry) [!] 중단")
                elif self.stop_seq != tok[1]:
                    cut = StageStopped("정지로 중단됨 · (dry) [!] 중단")
                if cut is not None:
                    frac = min(1.0, max(0.0, (time.time() - t0) / total)) if total > 0 else 1.0
                    if on_cut:
                        on_cut(frac)
                    self.cut_info = {"frac": frac, "wall": time.time()}
                    raise cut
                time.sleep(min(DRY_TICK_S, max(0.0, end - time.time())))
            if hang:
                raise StageTimeout("%s 응답이 %.0f초 안에 오지 않았습니다. 마지막 줄: (dry 멈춤)" % (what, deadline_s))
        finally:
            self._dry_busy = False
            self._dry_rx()                 # 펌웨어는 이동이 끝나는 순간 타이머를 다시 시작한다

    @_tx
    def _move(self, cmd, what, deadline_s=None):
        if self._aborted:
            raise StageError("비상정지 상태 · 원점 등록 후 사용")
        tok = self._tok()
        self._write(cmd)
        if self.dry:
            try:
                axis, val = cmd.split()
                ax, tgt = axis[1], float(val)
                a0 = self._dry_xy[ax]
            except (ValueError, KeyError):
                ax = tgt = a0 = None

            def cut(frac):
                if ax is not None:
                    self._dry_xy[ax] = a0 + (tgt - a0) * frac
            pulses = abs((tgt - a0) * _ppmm(ax)) if ax is not None else 0
            self._dry_wait(what, deadline_s, tok, pulses=pulses, on_cut=cut)
            if ax is not None:
                self._dry_xy[ax] = tgt
            return "(dry) %s" % cmd
        # 완료는 이동 보고 줄, 또는 '이미 그 위치'(움직일 필요가 없던 경우).
        try:
            return self._collect(deadline_s or MOVE_TIMEOUT_S,
                                 lambda s: (DONE_MARK in s) or (ALREADY_MARK in s), what, tok)
        except StageTimeout as e:
            e.resynced = self.resync()                      # 같은 거래 안에서 '!' + 재동기(대기 스레드가 끼어들지 못한다)
            raise

    def move_x_mm(self, mm, speed=None, deadline_s=None):
        self._ensure_speed(speed or DEFAULT_SPEED_PPS)
        return self._move("mx %.1f" % float(mm), "X 이동", deadline_s)

    def move_y_mm(self, mm, speed=None, deadline_s=None):
        self._ensure_speed(speed or DEFAULT_SPEED_PPS)
        return self._move("my %.1f" % float(mm), "Y 이동", deadline_s)

    def move_z_mm(self, mm, speed=None, deadline_s=None):
        """Z 절대 이동. 0 이 맨 위이고 값이 커질수록 내려간다."""
        self._need_fw(9, "Z 이동")
        self._ensure_speed(speed or DEFAULT_SPEED_PPS)
        return self._move("mz %.2f" % float(mm), "Z 이동", deadline_s)

    @_tx
    def resync(self, quiet_s=0.6, max_s=4.0):
        """시간 초과 뒤 재동기: '!' 로 이동을 끊고 들어오는 줄을 조용해질 때까지 버린 뒤 st 로 응답이 맞는지 확인한다. 성공하면 True."""
        self._aborted = True                                # 위치를 못 믿는다 - Motion 이 잠금 상태를 정한다
        try:
            self.abort()
        except StageError:
            return False
        if self.dry:
            self.dry_hang = False
            return True
        t0 = time.time()
        last = time.time()
        while time.time() - t0 < max_s:
            ln = self._readline(POLL_S)
            if ln is None:
                if time.time() - last >= quiet_s:
                    break
                continue
            last = time.time()
        try:
            self._write("st")
            self._collect(LINE_TIMEOUT_S, lambda t: t.startswith("ST ") and "DIRTY=" in t, "재동기")
            return True
        except StageError:
            return False

    # ---- 수동 원점 잡기(V7) --------------------------------------------
    def _need_fw(self, min_major, what):
        """이 기능에 필요한 최소 펌웨어 버전. 같은지가 아니라 이상인지를 본다.

        같은지로 보면 펌웨어를 올릴 때마다 멀쩡한 기능이 전부 막힌다.
        """
        cur = _major_of(self.fw_version)
        if cur and cur < int(min_major):
            raise StageError("펌웨어 V%d 필요 (%s · 지금 %s)"
                             % (int(min_major), what, self.fw_version))

    @_tx
    def _ensure_speed(self, pps):
        """속도가 다를 때만 v 를 보낸다.

        조그는 스텝마다 부르므로, 매번 보내고 응답을 읽으면 스텝당 0.4~0.5초가
        그냥 사라진다(실측). 바뀔 때만 보낸다.
        """
        pps = int(pps)
        if self.speed_pps == pps:
            return
        self._write("v %d" % pps)
        if not self.dry:
            # v 는 printStatus 한 덩어리를 뱉는다. 조용해질 때까지만 읽어 버린다.
            t0 = time.time()
            while time.time() - t0 < 1.0:
                if self._readline() is None:
                    break
        self.speed_pps = pps

    @_tx
    def set_profile(self, accel, start_pps):
        """가속도(a)·시작 속도(b)를 펌웨어로 보낸다. 같으면 보내지 않는다."""
        want = (int(accel), int(start_pps))
        if self.profile == want:
            return
        for cmd in ("a %d" % want[0], "b %d" % want[1]):
            self._write(cmd)
            if not self.dry:
                t0 = time.time()           # printStatus 한 덩어리를 읽어 버린다
                while time.time() - t0 < 1.0:
                    if self._readline() is None:
                        break
        self.profile = want

    @_tx
    def set_position(self, x_mm, y_mm):
        """지금 위치를 (x, y) 로 세팅한다(sp). 사진 호밍이 잰 실제 위치로 맞출 때 쓴다.

        펌웨어는 X·Y 원점을 '있음' 으로 만들고 EEPROM 에 저장한다. Z 는 건드리지 않는다.
        """
        xp, yp = int(round(float(x_mm) * PPMM)), int(round(float(y_mm) * PPMM))
        self._write("sp %d %d" % (xp, yp))
        if self.dry:
            if not (0 <= xp <= X_MAX_PULSE and 0 <= yp <= Y_MAX_PULSE):
                raise StageError("펌웨어 거부: [거부] sp 값이 범위 밖")
            tx, ty = self.dry_true_xy()
            self._dry_xy["x"], self._dry_xy["y"] = xp / PPMM, yp / PPMM
            self._dry_off = {"x": tx - self._dry_xy["x"], "y": ty - self._dry_xy["y"]}
            self._dry_homed["x"] = self._dry_homed["y"] = True
        else:
            self._collect(LINE_TIMEOUT_S, lambda t: "PC 위치로 세팅" in t, "위치 세팅")
        return self.status()

    @_tx
    def watchdog(self, ms):
        """펌웨어 통신 감시 시간(V11). 0 = 끔."""
        self._need_fw(11, "통신 감시")
        ms = max(0, int(ms))
        self._write("wd %d" % ms)
        if self.dry:
            with self._dry_lock:
                self._dry_wd_ms = ms
            return ms
        self._collect(LINE_TIMEOUT_S, lambda t: "통신 감시" in t, "통신 감시")
        return ms

    @_tx
    def forget(self):
        """펌웨어의 원점 기록을 지운다(비상정지 뒤).

        지우지 않으면 다음 부팅에서 EEPROM 의 '원점OK' 가 복원되어, 믿을 수 없는
        좌표를 가진 채로 앱이 절대 모드로 켜진다(2026-09-11 실장).
        """
        self._write("forget")
        if self.dry:
            # 펌웨어 V9·V10 의 forget 은 세 축을 모두 지운다. 키를 빠뜨리면 다음
            # status() 가 KeyError 로 죽어 위치가 통째로 사라진다.
            self._dry_homed = {k: False for k in self._dry_homed}
        else:
            self._collect(LINE_TIMEOUT_S, lambda t: "기록 삭제" in t, "원점 기록 삭제")
        return self.status()

    def jog_rel(self, axis, mm, speed=None):
        """원점이 없어도 되는 상대 이동. 사람이 보면서 끝단까지 몰 때 쓴다.

        펌웨어는 jx/jy 를 한 번에 8000 펄스(50mm)까지만 받는다. 그보다 먼 거리는
        여기서 조각으로 나눠 차례로 보낸다 - 실장에서 원점이 수십 mm 치우쳐
        있을 때 10mm 씩 아홉 번 누르게 하지 않으려는 것이다(2026-09-11).
        조각 사이마다 중단을 확인한다.
        """
        ax = str(axis).lower()
        if ax not in ("x", "y", "z"):
            raise StageError("축은 x, y, z 중 하나여야 합니다: %s" % axis)
        self._need_fw(9 if ax == "z" else 8, "수동 이동")
        total = int(round(float(mm) * _ppmm(ax)))
        if total == 0:
            return "  이미 그 위치"
        # 비상정지 뒤에도 막지 않는다. 위치는 못 믿지만 상대 이동은 사람이 보면서
        # 끝단까지 몰아 원점을 다시 등록하는 복구 경로다(절대 이동은 그대로 잠긴다).
        self._ensure_speed(speed or JOG_SLOW_PPS)
        # 이미 잠겨 있던 상태(비상정지 뒤 복구)는 중단이 아니다. _aborted 는 원점을
        # 다시 등록할 때까지 유지되는 잠금이라, 그대로 보면 비상정지 뒤 끝단까지
        # 미는 복구 절차가 늘 첫 조각에서 멈춘다 - 이 명령 도중에 새로 들어온 '!'
        # 만 중단으로 본다(_dry_wait 와 같은 방식).
        tok = self._tok()
        sign = 1 if total > 0 else -1
        left = abs(total)
        out = "  이미 그 위치"
        while left > 0:
            if self.abort_seq != tok[0]:
                # 앞 조각 도중에 비상정지가 왔다. 남은 조각을 보내면 멈춘 줄 아는
                # 사람 앞에서 스테이지가 다시 움직인다.
                raise StageError("비상정지로 중단됨")
            if self.stop_seq != tok[1]:
                raise StageStopped("정지로 중단됨")
            chunk = min(left, JOG_MAX_PULSE) * sign
            left -= abs(chunk)
            out = self._jog_chunk(ax, chunk, tok)
        return out

    @_tx
    def _jog_chunk(self, ax, pulse, tok=None):
        """jx/jy 한 번(8000 펄스 이하). 보고 줄을 기다려 돌려준다."""
        cmd = "j%s %d" % (ax, pulse)
        tok = tok or self._tok()
        self._write(cmd)
        if self.dry:
            a0 = self._dry_xy[ax]

            def cut(frac):
                self._dry_xy[ax] = a0 + frac * pulse / _ppmm(ax)
            self._dry_wait("수동 이동(%s)" % ax, None, tok, pulses=pulse, on_cut=cut)
            nxt = self._dry_xy[ax] + pulse / _ppmm(ax)
            # 펌웨어 V8 과 같은 규칙: 원점이 있는 축이 가동범위를 벗어나면 그 축의
            # 원점을 해제한다. 원점이 없으면 음수 좌표도 말이 된다(기준이 없다).
            if self._dry_homed[ax]:
                if nxt < 0 or nxt > _max_mm(ax):
                    self._dry_homed[ax] = False
            self._dry_xy[ax] = nxt
            return "(dry) %s" % cmd
        try:
            return self._collect(MOVE_TIMEOUT_S,
                                 lambda s: (DONE_MARK in s) or (ALREADY_MARK in s),
                                 "수동 이동(%s)" % ax, tok)
        except StageTimeout as e:
            e.resynced = self.resync()
            raise

    @_tx
    def find_zero(self, axis, search_pulse=None):
        """펌웨어 fz: 끝단 쪽으로 최대 search_pulse(기본 10 mm)를 느리게 밀고 → 2 mm 물러나 0 으로 등록 → EEPROM 저장.
        응답 "--- 원점 설정 완료 (저장됨) ---" 이면 성공, "--- 중단됨 ... ---" 이면 실패. 세대 검사는 쓰기 직전(write_guard)이 한다."""
        ax = str(axis).lower()
        if ax not in ("x", "y", "z"):
            raise StageError("축은 x, y, z 중 하나여야 합니다: %s" % axis)
        n = int(search_pulse or FZ_SEARCH_PULSE)
        if not 1 <= n <= FZ_SEARCH_PULSE:
            raise StageError("탐색은 1~%d 펄스" % FZ_SEARCH_PULSE)
        cmd = "fz %s %d" % (ax, n)
        tok = self._tok()
        self._write(cmd)
        if self.dry:
            prev = (self._dry_xy[ax], self._dry_homed[ax])

            def cut(frac):                                  # 펌웨어: 끊기면 위치 카운터는 이미 바뀌었고(틀린 값) 원점 플래그는 fz 전 값
                self._dry_xy[ax] = prev[0] - frac * n / _ppmm(ax)
                self._dry_homed[ax] = prev[1]
            self._dry_wait("원점 잡기", None, tok, dur=self._dry_duration(n) + 0.5 + self._dry_duration(320), on_cut=cut)
            self._dry_xy[ax] = 0.0
            self._dry_homed[ax] = True
            if ax in self._dry_off:
                self._dry_off[ax] = 0.0                     # 끝단에 닿아 다시 잡았으니 실제 위치 = 보고 위치
            return "(dry) %s" % cmd
        try:
            ln = self._collect(FZ_TIMEOUT_S, lambda t: ("원점 설정 완료" in t) or ("중단됨" in t), "원점 잡기", tok)
        except StageTimeout as e:
            e.resynced = self.resync()
            raise
        if "중단됨" in ln:
            self.cut_info = {"line": ln.strip(), "wall": time.time()}
            if self.abort_seq == tok[0]:
                raise StageStopped("정지로 중단됨 · 원점 설정 안 됨")
            self._aborted = True
            raise StageError("비상정지로 중단됨 · 원점 설정 안 됨")
        return ln

    @_tx
    def set_zero(self, axis="xy"):
        """지금 서 있는 자리를 0 으로 등록한다(zx / zy / z)."""
        ax = str(axis).lower()
        cmd = {"x": "zx", "y": "zy", "z": "zz", "xy": "z"}.get(ax)
        if cmd is None:
            raise StageError("축은 x, y, z, xy 중 하나여야 합니다: %s" % axis)
        if ax == "z":
            self._need_fw(9, "원점 등록")
        self._write(cmd)
        if not self.dry:
            self._collect(LINE_TIMEOUT_S, lambda t: "0 으로 등록" in t, "원점 등록")
        else:
            for one in (["x", "y"] if ax == "xy" else [ax]):
                self._dry_xy[one] = 0.0
                self._dry_homed[one] = True
        self.reset_abort()
        return self.status()

    def goto_mm(self, x, y):
        """펌웨어에 동시 이동이 없으므로 X 먼저, 그다음 Y."""
        return [self.move_x_mm(x), self.move_y_mm(y)]

    def abort(self, relays_off=False, off_relays=()):
        """비상정지. 어느 스레드에서든 즉시 "!" 를 쓴다.

        relays_off 면 같은 쓰기에 "r all 0" 을 바로 붙인다(정책이 '끄기' 일 때).
        "!" 가 먼저 나가고 펌웨어는 이동을 끊은 뒤 다음 줄로 릴레이를 끈다 - 비상정지
        자체가 늦어지지 않는다.

        이동은 블로킹이라 워커 스레드가 보고 줄을 기다리고 있다. 큐에 넣어 순서를
        기다리면 '비상' 이 아니므로 여기서만 예외적으로 포트에 직접 쓴다
        (pyserial 의 write 는 스레드 안전하고, 진행 중이던 이동은 보고 줄에
        "[!] 중단" 이 붙어 돌아온다).
        """
        self.abort_seq += 1
        self._aborted = True
        if self.dry:
            self.bang_wall.append(time.time())
            self._log("->", "! (abort)" + (" + r all 0" if relays_off else ""))
            self._dry_rx()
            if relays_off:
                self._dry_relay = [False] * N_RELAY
            else:
                for n in off_relays or ():      # 정책과 무관하게 끌 릴레이(히트건)
                    if 1 <= int(n) <= N_RELAY:
                        self._dry_relay[int(n) - 1] = False
            return True
        if self.ser is None:
            return True
        cmds = ["r all 0"] if relays_off else ["r %d 0" % int(n) for n in (off_relays or ())]
        data = "!" + chr(10) + "".join(c + chr(10) for c in cmds)
        got = self._wlock.acquire(timeout=1.5)              # 쓰기 잠금도 영원히 기다리지 않는다(다른 쓰기가 막혀 있어도 '!' 는 나간다)
        try:
            self._ser_write(data.encode("ascii"), flush=False)  # 거래 잠금은 기다리지 않는다 - '!' 와 릴레이 끄기가 한 번의 쓰기로 나간다
        except StageError as e:
            raise StageError("비상정지 전송 실패: %s" % e)
        finally:
            if got:
                self._wlock.release()
        self.bang_wall.append(time.time())                  # 시험용: '!' 를 쓴 벽시계 시각(비상정지 지연 측정)
        self._log("->", "! (abort)" + (" + " + " + ".join(cmds) if cmds else ""))     # 로그는 쓴 뒤에(파일 I/O 가 '!' 를 늦추지 않게)
        threading.Thread(target=self._after_abort, daemon=True, name="abort-drain").start()
        return True

    def stop(self):
        """정지: 지금 움직이는 모터를 '!' 로 즉시 멈춘다. 비상정지와 달리 잠그지 않고(_aborted 불변) 릴레이도 건드리지 않는다.
        멈춰 있어도 쓴다(펌웨어가 한 줄만 찍는다)."""
        self.stop_seq += 1
        if self.dry:
            self.stop_wall.append(time.time())
            self._log("->", "! (stop)")
            self._dry_rx()
            return True
        if self.ser is None:
            return True
        got = self._wlock.acquire(timeout=1.5)
        try:
            self._ser_write(("!" + chr(10)).encode("ascii"), flush=False)
        except StageError as e:
            raise StageError("정지 전송 실패: %s" % e)
        finally:
            if got:
                self._wlock.release()
        self.stop_wall.append(time.time())
        self._log("->", "! (stop)")
        threading.Thread(target=self._after_abort, daemon=True, name="stop-drain").start()
        return True

    def _after_abort(self):
        """'!' 뒤 들어오는 응답(중단 보고 · RS)을 거래 잠금이 풀린 뒤 비워 다음 거래와 엇갈리지 않게 한다."""
        if not self._txn.acquire(timeout=3.0):
            return
        try:
            t0 = time.time()
            while time.time() - t0 < 0.4:
                if self._readline() is None:
                    break
        finally:
            self._txn.release()

    def txn_try(self):
        return self._txn.acquire(blocking=False)

    def txn_release(self):
        self._txn.release()

    def ping(self):
        """통신 감시 유지 신호: 거래 잠금을 **기다리지 않고** 'st' 한 번. 블로킹 이동 중이면(잠금을 못 얻으면) 보내지 않는다(펌웨어도 그때는 판정 안 함). 보냈으면 True."""
        if not self._txn.acquire(blocking=False):
            return False
        try:
            self.status()
            return True
        finally:
            self._txn.release()

    def reset_abort(self):
        """원점을 다시 잡았으므로 잠금을 푼다."""
        self._aborted = False

    # ---- 릴레이 · 호밍 센서(V10) ----------------------------------------
    def _read_bits(self, cmd, head, key, n, what):
        """"RS R1=0 ..." / "HS H1=0 ..." 한 줄을 [bool]*n 으로."""
        ln = self._collect(LINE_TIMEOUT_S, lambda s: s.startswith(head + " "), what)
        d = dict(t.split("=", 1) for t in ln.split()[1:] if "=" in t)
        try:
            return [d["%s%d" % (key, i + 1)] == "1" for i in range(n)]
        except KeyError:
            raise StageError("%s 줄을 해석할 수 없습니다: %s" % (head, ln))

    @_tx
    def relay_set(self, n, on):
        """릴레이 n(1~10)을 켜거나 끈다. 펌웨어가 돌려주는 RS 상태를 돌려준다.

        비상정지·forget 은 릴레이를 건드리지 않는다(펌웨어 정책과 같다).
        """
        self._need_fw(10, "릴레이")
        n = int(n)
        if not 1 <= n <= N_RELAY:
            raise StageError("릴레이 번호는 1~%d: %s" % (N_RELAY, n))
        cmd = "r %d %d" % (n, 1 if on else 0)
        self._write(cmd)
        if self.dry:
            self._dry_relay[n - 1] = bool(on)
            return list(self._dry_relay)
        return self._read_bits(cmd, "RS", "R", N_RELAY, "릴레이")

    @_tx
    def relay_all_off(self):
        self._need_fw(10, "릴레이")
        self._write("r all 0")
        if self.dry:
            self._dry_relay = [False] * N_RELAY
            return list(self._dry_relay)
        return self._read_bits("r all 0", "RS", "R", N_RELAY, "릴레이")

    @_tx
    def relay_status(self):
        """'rs' -> [bool]*10 (True = 켜짐, 극성과 무관한 논리 상태)."""
        self._need_fw(10, "릴레이")
        self._write("rs")
        if self.dry:
            return list(self._dry_relay)
        return self._read_bits("rs", "RS", "R", N_RELAY, "릴레이 상태")

    @_tx
    def save(self):
        if self._aborted:
            # 비상정지 뒤의 위치는 신뢰할 수 없다. EEPROM 에 굳히면 다음 전원에서
            # 그 틀린 위치를 '복원' 해 버린다.
            return ["  (비상정지 상태 - save 생략)"]
        self._write("save")
        if self.dry:
            return ["(dry) save"]
        lines = []
        t0 = time.time()
        while time.time() - t0 < LINE_TIMEOUT_S:
            ln = self._readline()
            if ln is None:
                if lines:
                    break
                continue
            lines.append(ln)
            if "저장됨" in ln:
                break
        return lines


# --------------------------------------------------------------------------
def load_port_setting():
    try:
        with open(paths.SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f).get("serial_port")
    except Exception:
        return None


def main(argv=None):
    ap = argparse.ArgumentParser(description="스테이지 펌웨어 V11 시리얼 드라이버")
    ap.add_argument("cmd", nargs="?", choices=["st", "mx", "my", "save"],
                    help="보낼 명령")
    ap.add_argument("value", nargs="?", type=float, help="mx / my 의 mm 값")
    ap.add_argument("--port", help="시리얼 포트 (예: COM5)")
    ap.add_argument("--list-ports", action="store_true", help="포트 목록만 출력")
    a = ap.parse_args(argv)

    if a.list_ports:
        ps = list_ports()
        if not ps:
            print("시리얼 포트가 없습니다.")
            return 1
        for p in ps:
            print("%-8s %s" % (p.device, p.description))
        return 0

    if not a.cmd:
        ap.print_help()
        return 2

    st = Stage(a.port or load_port_setting())
    try:
        banner = st.open()
    except StageError as e:
        print(str(e))
        return 1
    try:
        print("포트      : %s" % st.port_name)
        if banner:
            print("배너      :")
            for ln in banner.splitlines():
                print("  " + ln)
        if st.needs_home:
            print("경고      : 원점 없음. fz x / fz y (또는 sp x y) 후 save 하세요.")
        if a.cmd == "st":
            s = st.status()
            print("상태      : X=%d (%.2f mm)  Y=%d (%.2f mm)  X2=%d"
                  % (s["x_pulse"], s["x_mm"], s["y_pulse"], s["y_mm"], s["x2_pulse"]))
            print("            원점 X=%s Y=%s   저장안됨=%s"
                  % (s["homed_x"], s["homed_y"], s["dirty"]))
        elif a.cmd in ("mx", "my"):
            if a.value is None:
                print("mm 값이 필요합니다: python -m core.stage %s 100" % a.cmd)
                return 2
            ln = st.move_x_mm(a.value) if a.cmd == "mx" else st.move_y_mm(a.value)
            print("보고      : %s" % ln.strip())
        elif a.cmd == "save":
            for ln in st.save():
                print("응답      : %s" % ln.strip())
    except StageError as e:
        print("실패      : %s" % e)
        return 1
    finally:
        st.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
