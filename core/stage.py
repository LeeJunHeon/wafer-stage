"""stage.py - 펌웨어 V6(firmware/stage_v6/stage_v6.ino) 시리얼 드라이버.

프로토콜은 .ino 에서 그대로 따왔다.
  보내는 것   : "st" / "mx 100.0" / "my 50.5" / "save"  (줄바꿈으로 끝)
  상태 한 줄  : "ST X=16000 Y=8000 X2=16000 HX=1 HY=1 DIRTY=0"
  이동 보고   : "  X  + cmd=16000 out=16000 t=2783.53ms | X=16000 Y=8000"
                -> ' | X=' 가 들어 있는 줄이 '이동 끝' 신호다.
  이미 도착   : "  이미 그 위치"          (움직이지 않으므로 보고 줄이 안 온다)
  거부        : "  [거부] 원점 미확정 ..." / "  [거부] 범위 밖  목표=..."
  경고        : 줄 안에 "[!]" (X 틀어짐 / CPU / 중단)
  저장        : "save" -> "  저장됨 (신뢰 확정)"

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
import sys
import time

from . import paths

PPMM = 160.0                  # 160 펄스 = 1mm (.ino 의 PPMM)
X_MAX_PULSE = 39620           # 247.6mm
Y_MAX_PULSE = 39640           # 247.8mm

BAUD = 115200
BOOT_MAX_S = 5.0              # 포트를 열면 DTR 로 보드가 리셋된다. 배너를 이만큼 기다린다
BOOT_QUIET_S = 1.0            # 이만큼 조용하면 배너가 끝난 것으로 본다
MOVE_TIMEOUT_S = 30.0         # 가장 긴 이동 248mm 가 v6000 에서 약 7초
HOME_TIMEOUT_S = 90.0         # 원점 탐색은 스트로크 전체를 훑을 수 있다
LINE_TIMEOUT_S = 5.0
POLL_S = 0.2                  # 한 번 읽기에 기다리는 시간 (포트 설정은 여기서 고정)

DONE_MARK = " | X="           # 이동 보고 줄
ALREADY_MARK = "이미 그 위치"
REJECT_MARK = "[거부]"
WARN_MARK = "[!]"


class StageError(Exception):
    pass


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
        raise StageError("시리얼 포트가 하나도 없습니다. USB 연결을 확인하세요.")
    ard = [p for p in ports if "arduino" in ((p.description or "") + " " +
                                             (p.manufacturer or "")).lower()]
    if len(ard) == 1:
        return ard[0].device
    if len(ports) == 1:
        return ports[0].device
    msg = ["포트를 고를 수 없습니다. --port 로 지정하세요.", "사용 가능한 포트:"]
    for p in ports:
        msg.append("  %-8s %s" % (p.device, p.description))
    raise StageError("\n".join(msg))


# --------------------------------------------------------------------------
class Stage:
    def __init__(self, port=None, dry=False, log_path=None):
        self.port_name = port
        self.dry = bool(dry)
        self.ser = None
        self.banner = ""
        self.needs_home = False        # 배너에 "원점없음" 이 있었나
        self.log_path = log_path or os.path.join(paths.OUT_DIR, "serial.log")
        # 주고받은 줄을 그대로 넘겨받을 곳(서버가 화면 로그로 보낸다). 파일 로그는
        # 이것과 무관하게 항상 남는다 - 화면에서 껐다고 기록이 사라지면 안 된다.
        self.on_line = None
        self.warnings = []
        # 비상정지('!')를 보낸 뒤 상태. 펌웨어는 '명령마다' abortFlag 를 지우므로
        # (.ino 395행) 다음 명령을 그냥 보내면 그대로 움직인다. 그래서 드라이버가
        # 잠가야 한다 - 원점을 다시 잡기 전까지 이동을 보내지 않는다.
        self._aborted = False
        # dry 모드의 가상 위치. 이동을 흉내만 내고 위치가 0 에 머물면 화면 검증이
        # 무의미해진다(포인터·맵이 안 움직인다). 보낸 명령대로 위치를 옮겨 둔다.
        self._dry_xy = [0.0, 0.0]

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
                f.write("%s %s %s\n" % (time.strftime("%H:%M:%S"), arrow,
                                        text.rstrip("\r\n")))
        except OSError:
            pass                       # 로그 실패로 동작을 막지는 않는다

    # ---- 열기/닫기 ------------------------------------------------------
    def open(self):
        if self.dry:
            self.banner = "(dry) 시리얼 없이 실행"
            self._log("--", self.banner)
            return self.banner
        try:
            import serial
        except ImportError:
            raise StageError("pyserial 이 필요합니다:  pip install pyserial")
        self.port_name = pick_port(self.port_name)
        try:
            self.ser = serial.Serial(self.port_name, BAUD, timeout=POLL_S)
        except Exception as e:
            raise StageError("포트를 열 수 없습니다 (%s): %s\n"
                             "아두이노 IDE 의 시리얼 모니터가 열려 있으면 닫아 주세요."
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
        self.needs_home = "원점없음" in self.banner
        return self.banner

    def close(self):
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
    def _write(self, cmd):
        self._log("->", cmd)
        if self.dry:
            print("  (dry) %s" % cmd)
            return
        self.ser.write((cmd + "\n").encode("ascii"))
        self.ser.flush()

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
        raw = self.ser.readline()
        if not raw:
            return None
        ln = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self._log("<-", ln)
        return ln

    def _collect(self, deadline_s, is_done, what):
        """완료 줄이 올 때까지 읽는다. [거부] 면 예외, [!] 는 모아 둔다."""
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
                self._aborted = True
                raise StageError("비상정지로 중단됨: %s" % ln.strip())
            if WARN_MARK in ln and not is_done(ln):
                self.warnings.append(ln.strip())
                print("  경고(펌웨어): %s" % ln.strip())
            if is_done(ln):
                if WARN_MARK in ln:
                    self.warnings.append(ln.strip())
                return ln
        raise StageError("%s 응답이 %.0f초 안에 오지 않았습니다. 마지막 줄: %s"
                         % (what, deadline_s, seen[-1] if seen else "(없음)"))

    # ---- 명령 ----------------------------------------------------------
    def status(self):
        """'st' -> ST 줄을 dict 로. 펄스와 mm 를 함께 담는다."""
        if self.dry:
            x, y = self._dry_xy
            return {"x_pulse": int(x * PPMM), "y_pulse": int(y * PPMM),
                    "x2_pulse": int(x * PPMM), "homed_x": True, "homed_y": True,
                    "dirty": False, "x_mm": x, "y_mm": y, "raw": "(dry)"}
        self._write("st")
        ln = self._collect(LINE_TIMEOUT_S, lambda s: s.startswith("ST "), "st")
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
                "dirty": d.get("DIRTY") == "1", "raw": ln.strip(),
            }
        except (KeyError, ValueError):
            raise StageError("ST 줄을 해석할 수 없습니다: %s" % ln)
        out["x_mm"] = out["x_pulse"] / PPMM
        out["y_mm"] = out["y_pulse"] / PPMM
        return out

    def _move(self, cmd, what):
        if self._aborted:
            raise StageError("비상정지 상태 - 원점잡기(fz) 후 사용")
        self._write(cmd)
        if self.dry:
            try:
                axis, val = cmd.split()
                self._dry_xy[0 if axis == "mx" else 1] = float(val)
            except ValueError:
                pass
            return "(dry) %s" % cmd
        # 완료는 이동 보고 줄, 또는 '이미 그 위치'(움직일 필요가 없던 경우).
        return self._collect(MOVE_TIMEOUT_S,
                             lambda s: (DONE_MARK in s) or (ALREADY_MARK in s), what)

    def move_x_mm(self, mm):
        return self._move("mx %.1f" % float(mm), "X 이동")

    def move_y_mm(self, mm):
        return self._move("my %.1f" % float(mm), "Y 이동")

    def goto_mm(self, x, y):
        """펌웨어에 동시 이동이 없으므로 X 먼저, 그다음 Y."""
        return [self.move_x_mm(x), self.move_y_mm(y)]

    def abort(self):
        """비상정지. 어느 스레드에서든 즉시 "!" 를 쓴다.

        이동은 블로킹이라 워커 스레드가 보고 줄을 기다리고 있다. 큐에 넣어 순서를
        기다리면 '비상' 이 아니므로 여기서만 예외적으로 포트에 직접 쓴다
        (pyserial 의 write 는 스레드 안전하고, 진행 중이던 이동은 보고 줄에
        "[!] 중단" 이 붙어 돌아온다).
        """
        self._log("->", "! (abort)")
        self._aborted = True
        if self.dry or self.ser is None:
            return True
        try:
            self.ser.write(("!" + chr(10)).encode("ascii"))
            self.ser.flush()
            return True
        except Exception as e:             # noqa: BLE001
            raise StageError("비상정지 전송 실패: %s" % e)

    def reset_abort(self):
        """원점을 다시 잡았으므로 잠금을 푼다."""
        self._aborted = False

    def find_zero(self, axis, search_pulses=None):
        """원점 탐색. "fz x [n]" 을 보내고 "원점 설정 완료" 줄까지 기다린다.

        끝까지 밀어붙이는 동작이라 스트로크 전체를 훑을 수 있어 넉넉히 90초를 준다.
        완료 뒤 status() 로 위치·원점 상태를 갱신해 돌려준다.
        """
        ax = str(axis).lower()
        if ax not in ("x", "y", "xy"):
            raise StageError("축은 x, y, xy 중 하나여야 합니다: %s" % axis)
        for one in (["x", "y"] if ax == "xy" else [ax]):
            cmd = "fz %s" % one
            if search_pulses:
                cmd += " %d" % int(search_pulses)
            self._write(cmd)
            if self.dry:
                continue
            done = self._collect(HOME_TIMEOUT_S,
                                 lambda t: ("원점 설정 완료" in t) or ("중단됨" in t),
                                 "원점 탐색(%s)" % one)
            if "원점 설정 완료" in done:
                self.reset_abort()
        if self.dry:
            self.reset_abort()
            # dry 모드의 가상 위치. 이동을 흉내만 내고 위치가 0 에 머물면 화면
            # 검증이 무의미해지므로(포인터·맵이 안 움직인다) 명령대로 옮겨 둔다.
            self._dry_xy = [0.0, 0.0]      # 원점을 잡았으니 0
        return self.status()

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
    ap = argparse.ArgumentParser(description="스테이지 펌웨어 V6 시리얼 드라이버")
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
