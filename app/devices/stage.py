"""stage.py - 아두이노 Mega + 펌웨어 V11 드라이버.

규칙(펌웨어 때문에 지킨다)
- 시리얼은 일꾼 스레드 하나가 맡는다. 명령은 한 번에 하나씩 보내고, 응답이 끝난 뒤 다음 명령을 보낸다.
- 예외는 '!' 뿐이다. '!' 는 줄 서지 않고 bang() 이 바로 쓴다. 펌웨어는 이동 중에 '!' 의 첫 글자만 본다.
- 이동 중에는 '!' 말고 아무 줄도 보내지 않는다(수신 버퍼 64 바이트, 이동 중에는 줄을 처리하지 않는다).
  일꾼이 하나라 이동 명령의 응답을 다 받을 때까지 다음 줄이 나가지 않는다.
- 명령을 쓰기 직전(쓰기 잠금 안)에 세대 번호를 다시 비교한다. 비상정지가 세대를 올렸으면 쓰지 않고 버린다.
- 이동 시간 한도를 넘으면 '!' 를 쓰고, 입력을 비운 뒤 st 로 다시 맞춘다.
"""

import queue
import re
import threading
import time
from concurrent.futures import Future
from typing import Callable, Optional

PPMM = 160.0                                       # 모든 축 160 펄스 = 1 mm
FW_DEFAULT_SPEED = (6000, 40000, 300)              # 펌웨어 v · a · b 기본값
JOG_MAX_PULSE = 8000                               # jx/jy/jz 한 번에 최대(50 mm)
FIND_MAX_PULSE = 1600                              # fz 탐색 최대
POLL_S = 0.5                                       # 쉬는 동안 st 주기(통신 감시 신호도 된다)
RS_POLL_S = 2.0
LINE_TIMEOUT_S = 3.0
WD_MARK = "통신 끊김"
REJECT_MARK = "[거부]"
ABORT_MARK = "[!] 중단"


class StageError(Exception):
    """스테이지 오류. 문구는 사람이 읽는 한국어다."""


class StageRejected(StageError):
    """펌웨어가 [거부] 했다."""


class StageTimeout(StageError):
    pass


class StageIOError(StageError):
    """포트 읽기 · 쓰기 실패(USB 빠짐 등)."""


class Discarded(StageError):
    """세대 번호가 달라져(비상정지) 보내지 않고 버렸다."""


class MoveAborted(StageError):
    """'!' 로 이동이 끊겼다."""


class StageReboot(StageError):
    """보드가 다시 시작됐다(배너가 보임)."""


# --------------------------------------------------------------------------
# 순수 함수
# --------------------------------------------------------------------------
def move_time_s(pulses, v, a, b):
    """n 펄스 사다리꼴 이동 시간(s). 펌웨어 가속 · 정속 · 감속과 같은 식."""
    n = abs(float(pulses))
    if n <= 0:
        return 0.0
    v = max(float(v), float(b))
    a = float(a)
    b = min(float(b), v)
    d_acc = (v * v - b * b) / (2.0 * a)
    if n >= 2.0 * d_acc:
        return 2.0 * (v - b) / a + (n - 2.0 * d_acc) / v
    vp = (b * b + a * n) ** 0.5
    return 2.0 * (vp - b) / a


def time_limit_s(pulses, v, a, b):
    """이동 시간 한도 = 계산 시간 × 1.5 + 2 s 여유."""
    return move_time_s(pulses, v, a, b) * 1.5 + 2.0


def _kv(line):
    d = {}
    for tok in line.split():
        if "=" in tok:
            k, v = tok.split("=", 1)
            d[k] = v
    return d


def parse_st(line):
    """'ST X=.. Y=.. X2=.. Z=.. HX=0|1 HY HZ DIRTY=0|1' → dict. 못 읽으면 StageError."""
    d = _kv(line)
    try:
        return {"x": int(d["X"]), "y": int(d["Y"]), "z": int(d["Z"]),
                "hx": d.get("HX") == "1", "hy": d.get("HY") == "1", "hz": d.get("HZ") == "1",
                "dirty": d.get("DIRTY") == "1"}
    except (KeyError, ValueError):
        raise StageError("위치 응답을 읽지 못했습니다: %s" % line.strip()[:60])


def parse_rs(line):
    """'RS R1=0 … R10=0' → [bool × 10]."""
    d = _kv(line)
    try:
        return [d["R%d" % i] == "1" for i in range(1, 11)]
    except KeyError:
        raise StageError("릴레이 응답을 읽지 못했습니다: %s" % line.strip()[:60])


def parse_banner_version(line):
    m = re.search(r"3-AXIS\s+(V\d+)", line or "", re.I)
    return m.group(1).upper() if m else None


def mm_to_pulse(mm):
    return int(round(float(mm) * PPMM))


def is_st(ln):
    return ln.startswith("ST ") and "DIRTY=" in ln


def is_rs(ln):
    return ln.startswith("RS ")


def is_move_done(ln):
    return " | X=" in ln or "이미 그 위치" in ln


def is_status_block_end(ln):
    """p · v/a/b · zx/zy/zz/z/forget 이 출력하는 상태 블록의 마지막 줄(Z=…)."""
    s = ln.strip()
    return s.startswith("Z=") and "맨 위" in s


class _Job:
    __slots__ = ("cmd", "gen", "done", "timeout", "kind", "future", "pulses")

    def __init__(self, cmd, gen, done, timeout, kind, pulses=0):
        self.cmd, self.gen, self.done, self.timeout, self.kind, self.pulses = cmd, gen, done, timeout, kind, pulses
        self.future = Future()


# --------------------------------------------------------------------------
class Stage:
    """open_port(이름) → pyserial 모양 객체. gen_ok(세대) → 이 세대의 명령을 아직 써도 되는가."""

    def __init__(self, open_port: Callable, gen_ok: Optional[Callable] = None, log: Optional[Callable] = None,
                 on_event: Optional[Callable] = None, on_status: Optional[Callable] = None, poll_s: float = POLL_S):
        self.open_port = open_port
        self.gen_ok = gen_ok or (lambda g: True)
        self.log = log or (lambda arrow, text: None)
        self.on_event = on_event or (lambda kind, text: None)      # kind: reboot · wd_trip · lost
        self.on_status = on_status or (lambda: None)
        self.poll_s = poll_s
        self.ser = None
        self.connected = False
        self.port_name = ""
        self.firmware = ""
        self.banner_lines = []
        self.pos = {"x": None, "y": None, "z": None}               # 펄스
        self.homed = {"x": False, "y": False, "z": False}
        self.dirty = None
        self.relays = [None] * 10
        self.speed = FW_DEFAULT_SPEED
        self._q = queue.PriorityQueue()
        self._seq = 0
        self._seq_lock = threading.Lock()
        self._wlock = threading.Lock()                              # 쓰기 잠금: 세대 비교 ~ 쓰기를 한 덩어리로
        self._rbuf = b""
        self._thread = None
        self._alive = False
        self._last_st = 0.0
        self._last_rs = 0.0
        self.moving = False

    # ---- 연결 ----
    def connect(self, port_name: str, speed=FW_DEFAULT_SPEED, wd_ms: int = 2000, banner_wait_s: float = 6.0):
        """포트를 열고(보드가 재시작한다) 배너로 V11 을 확인한 뒤 wd · 속도 · st · rs 를 읽는다. V11 이 아니면 연결을 거절한다."""
        if self.connected:
            raise StageError("이미 연결되어 있습니다")
        try:
            ser = self.open_port(port_name)
        except Exception as e:                                      # noqa: BLE001
            raise StageIOError("포트를 열 수 없습니다 · %s · %s" % (port_name, str(e)[:50]))
        self.ser = ser
        self._rbuf = b""
        self.banner_lines = []
        fw = None
        t_end = time.monotonic() + banner_wait_s
        try:
            while time.monotonic() < t_end:
                ln = self._readline()
                if ln is None:
                    if fw:
                        break                                       # 배너 뒤 조용해졌다
                    continue
                self.banner_lines.append(ln)
                v = parse_banner_version(ln)
                if v:
                    fw = v
        except StageIOError:
            self._close_port()
            raise
        if fw is None:
            self._close_port()
            raise StageError("펌웨어 배너를 받지 못했습니다 · V11 인지 확인할 수 없어 연결하지 않습니다")
        if fw != "V11":
            self._close_port()
            raise StageError("펌웨어가 %s 입니다 · V11 이 필요합니다" % fw)
        self.firmware = fw
        self.port_name = port_name
        self.connected = True
        self._alive = True
        self._thread = threading.Thread(target=self._worker, daemon=True, name="stage-serial")
        self._thread.start()
        try:
            self.call("wd %d" % int(wd_ms), None, lambda ln: "통신 감시" in ln, LINE_TIMEOUT_S)
            if tuple(speed) != FW_DEFAULT_SPEED:
                self.set_speed(*speed)
            self.speed = tuple(speed)
            self.read_status()
            self.read_relays()
        except Exception:
            self.disconnect()
            raise

    def disconnect(self):
        self._alive = False
        self.connected = False
        self._seq_put(0, _Job("", None, None, 0, "stop"))
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        self._close_port()
        self._fail_pending(StageError("스테이지 연결이 끊겼습니다"))

    def _close_port(self):
        ser, self.ser = self.ser, None
        if ser is not None:
            try:
                ser.close()
            except Exception:                                       # noqa: BLE001
                pass

    def _fail_pending(self, exc):
        while True:
            try:
                _, _, job = self._q.get_nowait()
            except queue.Empty:
                return
            if job.kind != "stop" and not job.future.done():
                job.future.set_exception(exc)

    # ---- 비상정지 ----
    def bang(self) -> bool:
        """'!' 를 줄 서지 않고 바로 쓴다(flush 로 기다리지 않는다). 쓸 포트가 없으면 False."""
        ser = self.ser
        if ser is None:
            return False
        with self._wlock:
            self._ser_write(b"!\n", flush=False)
        self.log("->", "!")
        return True

    # ---- 명령 보내기 ----
    def _seq_put(self, prio, job):
        with self._seq_lock:
            self._seq += 1
            self._q.put((prio, self._seq, job))

    def submit(self, cmd, gen, done, timeout, kind="cmd", pulses=0, prio=5) -> Future:
        if not self.connected:
            f = Future()
            f.set_exception(StageError("스테이지가 연결되어 있지 않습니다"))
            return f
        job = _Job(cmd, gen, done, timeout, kind, pulses)
        self._seq_put(prio, job)
        return job.future

    def call(self, cmd, gen, done, timeout, kind="cmd", pulses=0, prio=5):
        """보내고 응답이 끝날 때까지 기다린다. 응답 줄 목록을 돌려준다."""
        return self.submit(cmd, gen, done, timeout, kind, pulses, prio).result()

    # ---- 일꾼 ----
    def _worker(self):
        while self._alive:
            try:
                _, _, job = self._q.get(timeout=0.05)
            except queue.Empty:
                self._idle()
                continue
            if job.kind == "stop":
                break
            self._run(job)

    def _idle(self):
        try:
            self._read_unsolicited()
            if not self._q.empty() or self.ser is None:
                return
            now = time.monotonic()
            if now - self._last_st >= self.poll_s:
                self._run(_Job("st", None, is_st, LINE_TIMEOUT_S, "poll"), quiet=True)
            elif now - self._last_rs >= RS_POLL_S:
                self._run(_Job("rs", None, is_rs, LINE_TIMEOUT_S, "poll"), quiet=True)
        except Exception:                                           # noqa: BLE001 - 쉬는 동안의 읽기 오류는 _run 이 사건으로 올렸다
            pass

    def _read_unsolicited(self):
        """쉬는 동안 먼저 들어온 줄(재시작 배너 · 통신 감시 보고)을 처리한다."""
        ser = self.ser
        if ser is None:
            return
        try:
            n = ser.in_waiting
        except Exception as e:                                      # noqa: BLE001
            self._lost(e)
            return
        if n <= 0:
            return
        for _ in range(20):
            ln = self._readline(short=True)
            if ln is None:
                break
            self._note(ln)

    def _run(self, job, quiet=False):
        try:
            if not self.gen_ok(job.gen):
                raise Discarded("비상정지로 취소됨")
            self._drain_before()
            with self._wlock:
                if not self.gen_ok(job.gen):                        # 쓰기 직전에 한 번 더(비상정지가 그 사이에 세대를 올렸다)
                    raise Discarded("비상정지로 취소됨")
                self._ser_write((job.cmd + "\n").encode("ascii"))
            self.log("->", job.cmd)
            if job.kind == "move":
                self.moving = True
            lines = self._collect(job)
            if job.kind == "poll":
                if job.cmd == "st":
                    self._last_st = time.monotonic()
                else:
                    self._last_rs = time.monotonic()
            job.future.set_result(lines)
        except Exception as e:                                      # noqa: BLE001
            if isinstance(e, StageIOError):
                self._lost(e)
            if job.kind == "poll":
                self._last_st = self._last_rs = time.monotonic()
            if not job.future.done():
                job.future.set_exception(e)
        finally:
            self.moving = False

    def _collect(self, job):
        lines = []
        t_end = time.monotonic() + job.timeout
        while time.monotonic() < t_end:
            ln = self._readline()
            if ln is None:
                continue
            lines.append(ln)
            self._note(ln)
            if "3-AXIS" in ln:
                raise StageReboot("보드가 다시 시작되었습니다")
            if REJECT_MARK in ln:
                raise StageRejected("펌웨어가 거부했습니다: %s" % ln.strip())
            if ABORT_MARK in ln:
                raise MoveAborted("이동이 중단되었습니다")
            if job.done(ln):
                return lines
        if job.kind == "raw":
            return lines
        if job.kind == "move":
            self._recover()
        raise StageTimeout("응답이 %.0f 초 안에 오지 않았습니다 (%s)" % (job.timeout, job.cmd))

    def _recover(self):
        """이동 시간 한도를 넘었다: '!' → 입력 비우기 → st 로 다시 맞춘다."""
        try:
            with self._wlock:
                self._ser_write(b"!\n", flush=False)
            self.log("->", "! (시간 한도)")
            time.sleep(0.3)
            self._drain_before()
            with self._wlock:
                self._ser_write(b"st\n")
            self.log("->", "st")
            t_end = time.monotonic() + 2.0
            while time.monotonic() < t_end:
                ln = self._readline()
                if ln is not None:
                    self._note(ln)
                    if is_st(ln):
                        break
        except StageIOError:
            pass

    def _drain_before(self):
        ser = self.ser
        if ser is None:
            return
        try:
            n = ser.in_waiting
        except Exception as e:                                      # noqa: BLE001
            raise StageIOError("읽기 실패 · 통신 끊김: %s" % str(e)[:40])
        if n <= 0:
            return
        for _ in range(50):
            ln = self._readline(short=True)
            if ln is None:
                break
            self._note(ln, drained=True)

    def _note(self, ln, drained=False):
        """들어온 줄에서 위치 · 릴레이 · 사건을 읽어 둔다."""
        try:
            if "3-AXIS" in ln:
                self.on_event("reboot", ln.strip())
            if WD_MARK in ln and "[!]" in ln:
                self.on_event("wd_trip", ln.strip())
            if is_st(ln):
                s = parse_st(ln)
                self.pos = {"x": s["x"], "y": s["y"], "z": s["z"]}
                self.homed = {"x": s["hx"], "y": s["hy"], "z": s["hz"]}
                self.dirty = s["dirty"]
                self.on_status()
            elif is_rs(ln):
                self.relays = parse_rs(ln)
                self.on_status()
            elif " | X=" in ln:
                m = re.search(r"\|\s*X=(-?\d+)\s+Y=(-?\d+)\s+Z=(-?\d+)", ln)
                if m:
                    self.pos = {"x": int(m.group(1)), "y": int(m.group(2)), "z": int(m.group(3))}
                    self.on_status()
        except StageError:
            pass

    # ---- 시리얼 입출력 ----
    def _ser_write(self, data, flush=True):
        try:
            self.ser.write(data)
            if flush:
                self.ser.flush()
        except Exception as e:                                      # noqa: BLE001
            if e.__class__.__name__ == "SerialTimeoutException":
                raise StageIOError("쓰기 시간 초과 · 통신 끊김")
            raise StageIOError("쓰기 실패 · 통신 끊김: %s" % str(e)[:40])

    def _readline(self, short=False):
        """한 줄(줄바꿈까지). 시간 안에 줄바꿈이 없으면 줄 중간까지만 오므로 이어 붙인다."""
        ser = self.ser
        if ser is None:
            raise StageIOError("포트가 닫혀 있습니다")
        try:
            raw = ser.readline()
        except Exception as e:                                      # noqa: BLE001
            raise StageIOError("읽기 실패 · 통신 끊김: %s" % str(e)[:40])
        if not raw:
            return None
        raw = self._rbuf + raw
        self._rbuf = b""
        if not raw.endswith(b"\n"):
            self._rbuf = raw[-512:]
            return None
        ln = raw.decode("utf-8", errors="replace").rstrip("\r\n")
        self.log("<-", ln)
        return ln if ln.strip() else None

    def _lost(self, e):
        if not self.connected:
            return
        self.connected = False
        self._alive = False
        self._close_port()
        self.relays = [None] * 10
        self.on_event("lost", str(e))

    # ---- 사람이 쓰는 명령 ----
    def read_status(self, gen=None):
        self.call("st", gen, is_st, LINE_TIMEOUT_S)
        return self.snapshot()

    def read_relays(self, gen=None):
        self.call("rs", gen, is_rs, LINE_TIMEOUT_S)
        return list(self.relays)

    def snapshot(self):
        return {"pos": dict(self.pos), "homed": dict(self.homed), "dirty": self.dirty, "relays": list(self.relays)}

    def set_speed(self, v, a, b, gen=None):
        for name, val in (("v", v), ("a", a), ("b", b)):
            self.call("%s %d" % (name, int(val)), gen, is_status_block_end, LINE_TIMEOUT_S)
        self.speed = (int(v), int(a), int(b))

    def set_v(self, v, gen=None):
        """이동 속도만 바꾼다(가속 · 시작 속도는 그대로). 핀을 샘플 가까이 내릴 때 느리게 하려고 쓴다."""
        v = int(v)
        if v == self.speed[0]:
            return
        self.call("v %d" % v, gen, is_status_block_end, LINE_TIMEOUT_S)
        self.speed = (v, self.speed[1], self.speed[2])

    def jog(self, axis, pulses, gen):
        """조그(jx/jy/jz, 원점 없어도 된다). 한 번에 8000 펄스까지라 길면 나눠 보낸다. 조각마다 세대를 다시 본다."""
        left = int(pulses)
        while left != 0:
            n = max(-JOG_MAX_PULSE, min(JOG_MAX_PULSE, left))
            self.call("j%s %d" % (axis, n), gen, is_move_done, time_limit_s(n, *self.speed), kind="move", pulses=n)
            left -= n

    def goto(self, axis, pulse, gen):
        """절대 이동(gx/gy/gz, 원점 필요)."""
        cur = self.pos.get(axis) or 0
        self.call("g%s %d" % (axis, int(pulse)), gen, is_move_done, time_limit_s(int(pulse) - cur, *self.speed), kind="move", pulses=int(pulse) - cur)

    def find_end(self, axis, gen, search_pulse=800):
        """끝단까지 밀어서 원점 잡기(fz). 끝나면 st 줄까지 받는다."""
        n = max(1, min(FIND_MAX_PULSE, int(search_pulse)))
        state = {"ok": False}

        def done(ln):
            if "원점 설정 완료" in ln:
                state["ok"] = True
                return False
            return state["ok"] and is_st(ln)

        limit = n / 300.0 + 0.5 + 320 / 300.0 + 5.0
        self.call("fz %s %d" % (axis, n), gen, done, limit, kind="move")

    def set_here(self, axis, gen):
        """지금 자리를 원점으로(zx · zy · zz · z). axis: x | y | z | xy."""
        cmd = {"x": "zx", "y": "zy", "z": "zz", "xy": "z"}[axis]
        self.call(cmd, gen, is_status_block_end, LINE_TIMEOUT_S)
        self.read_status(gen)

    def save(self, gen):
        self.call("save", gen, lambda ln: "저장됨" in ln, LINE_TIMEOUT_S)

    def relay(self, n, on, gen, prio=5):
        self.call("r %d %d" % (n, 1 if on else 0), gen, is_rs, LINE_TIMEOUT_S, prio=prio)

    def relays_off(self, gen, prio=0):
        self.call("r all 0", gen, is_rs, LINE_TIMEOUT_S, prio=prio)

    def raw(self, line, gen):
        """직접 명령: 보내고 0.8 초 동안 들어오는 줄을 모아 돌려준다(이동 · 릴레이 켜기는 control 이 걸러 준다)."""
        return self.call(line, gen, lambda ln: False, 0.8, kind="raw")
