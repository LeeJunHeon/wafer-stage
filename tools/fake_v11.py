"""fake_v11.py - 펌웨어 V11 흉내(pyserial 모양: write · read · readline · in_waiting · timeout · close · reset_input_buffer · flush).

firmware/stage_v11/stage_v11.ino 의 앱이 쓰는 명령과 출력 형식을 그대로 흉내 낸다. 시간도 흉내 낸다 - 펌웨어는 단일 스레드라
블로킹 이동 중에는 다른 줄을 처리하지 않고('!' 만 이동 도중에 본다), 응답은 별도 스레드에서 비동기로 시리얼 쪽에 들어온다.

  명령: st · p · mx/my/mz · gx/gy/gz · jx/jy/jz · zx/zy/zz/z · sp · v · a · b · w · wd · r N 0/1 · r all 0 · rs · save · forget · !
  출력: 시작 배너 · 이동 완료 줄(" | X=") · "  이미 그 위치" · "  [거부] …" · "  [!] 중단" · ST · RS · "  [!] 통신 끊김 · 릴레이 전부 꺼짐"
  통신 감시: 받은 줄마다 lastRx 갱신 · 블로킹 이동 중에는 판정 안 함 · 이동이 끝나는 순간 타이머 재시작.

시험용 조작: latency(응답 지연) · drop_next(줄 하나 잃음) · reboot() · freeze(초) · slip(dx, dy) · time_scale(이동 시간 배율).
"""

import collections
import threading
import time

PPMM = 160.0
X_MAX, Y_MAX, Z_MAX = 39620, 39640, 7200
N_RELAY = 10


class FakeV11:
    def __init__(self, time_scale=0.05, homed=True, banner=True):
        self.timeout = 0.2
        self.time_scale = float(time_scale)         # 1 = 실시간 이동 시간, 0.05 = 20배 빠르게
        self.latency = 0.0                          # 응답 한 줄이 시리얼에 나타나기까지 지연(s)
        self.hang_move = False                      # 다음 이동이 끝나지 않는다('!' 가 올 때까지 - 모터 멈춤 흉내)
        self.cut_until = 0.0                        # 이 시각까지 케이블이 끊김: 보낸 줄은 사라지고 응답도 호스트에 안 닿는다(펌웨어는 계속 돈다)
        self.bang_times = []                        # '!' 가 도착한 단조 시각(비상정지 지연 측정)
        self.freeze_until = 0.0                     # 이 시각까지 아무것도 처리 · 응답하지 않음(링크 멈춤)
        self._drop = 0
        self.lock = threading.Lock()
        self.inbuf = bytearray()
        self.out = collections.deque()              # (나타날 시각, bytes)
        self.cv = threading.Condition()
        self.alive = True
        self.true_off = [0.0, 0.0]                  # 탈조 - 실제 위치 = 보고 + off (mm)
        self._init_state(homed)
        self.wd_ms = 0
        self.last_rx = time.monotonic()
        self.wd_tripped = False
        self.busy = False
        self.abort_flag = False
        self.log = []                               # 받은 명령(검증용)
        if banner:
            self._banner()
        self.th = threading.Thread(target=self._run, daemon=True, name="fake-fw")
        self.th.start()

    # ---- pyserial 모양 ------------------------------------------------
    def write(self, data):
        if time.monotonic() < self.cut_until:
            return len(data)
        if b"!" in data:
            self.bang_times.append(time.monotonic())
        with self.cv:
            self.inbuf += bytes(data)
            self.cv.notify_all()
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        now = time.monotonic()
        with self.cv:
            return sum(len(b) for t, b in self.out if t <= now)

    def reset_input_buffer(self):
        now = time.monotonic()
        with self.cv:
            while self.out and self.out[0][0] <= now:
                self.out.popleft()

    def read(self, n=1):
        end = time.monotonic() + (self.timeout or 0)
        buf = bytearray()
        while len(buf) < n:
            now = time.monotonic()
            with self.cv:
                while self.out and self.out[0][0] <= now and len(buf) < n:
                    t, b = self.out.popleft()
                    take = b[: n - len(buf)]
                    buf += take
                    if len(b) > len(take):
                        self.out.appendleft((t, b[len(take):]))
            if len(buf) >= n or now >= end:
                break
            time.sleep(0.003)
        return bytes(buf)

    def readline(self):
        end = time.monotonic() + (self.timeout or 0)
        while True:
            now = time.monotonic()
            with self.cv:
                if self.out and self.out[0][0] <= now:
                    t, b = self.out.popleft()
                    return b
            if now >= end:
                return b""
            time.sleep(0.003)

    def close(self):
        self.alive = False
        with self.cv:
            self.cv.notify_all()

    # ---- 시험용 조작 ----------------------------------------------------
    def cut_link(self, sec):
        self.cut_until = time.monotonic() + sec

    def freeze(self, sec):
        self.freeze_until = time.monotonic() + sec

    def drop_next(self, n=1):
        self._drop += n

    def slip(self, dx_mm, dy_mm):
        self.true_off[0] += dx_mm
        self.true_off[1] += dy_mm

    def reboot(self):
        """보드 재부팅: 릴레이 꺼짐 · 속도 기본값 · 통신 감시 꺼짐 · 배너를 다시 보낸다(EEPROM 위치 복원)."""
        self.abort_flag = True
        self.relay = [False] * N_RELAY
        self.v, self.a, self.b = 6000, 40000, 300
        self.wd_ms = 0
        self.wd_tripped = False
        self._banner()

    def true_xy(self):
        return (self.px / PPMM + self.true_off[0], self.py / PPMM + self.true_off[1])

    def set_z_mm(self, mm):
        self.pz = int(mm * PPMM)

    # ---- 출력 ---------------------------------------------------------
    def _emit(self, text):
        if time.monotonic() < self.cut_until:
            return
        if self._drop > 0:
            self._drop -= 1
            return
        with self.cv:
            self.out.append((time.monotonic() + self.latency, (text + "\n").encode("utf-8")))

    def _init_state(self, homed):
        self.px = self.py = self.pz = 0
        self.hx = self.hy = self.hz = bool(homed)
        self.dirty = False
        self.relay = [False] * N_RELAY
        self.v, self.a, self.b = 6000, 40000, 300

    def _banner(self):
        self._emit("=== 3-AXIS V11 ===")
        self._emit("  X 0~%d  Y 0~%d  Z 0~%d   (160 pulse = 1mm, Z: 0 = 맨 위)" % (X_MAX, Y_MAX, Z_MAX))
        self._emit("  EEPROM: 마지막 저장 위치 복원  (꺼진 동안 손으로 밀었다면 fz 로 재등록)")

    def _rs(self):
        self._emit("RS " + " ".join("R%d=%d" % (i + 1, 1 if on else 0) for i, on in enumerate(self.relay)))

    def _st(self):
        self._emit("ST X=%d Y=%d X2=%d Z=%d HX=%d HY=%d HZ=%d DIRTY=%d" % (self.px, self.py, self.px, self.pz, self.hx, self.hy, self.hz, self.dirty))

    def _status(self):
        self._emit("  v=%d a=%d b=%d w=0ms" % (self.v, self.a, self.b))
        self._emit("  X=%d (%.2fmm) %s   Y=%d (%.2fmm) %s   (저장됨)" % (self.px, self.px / PPMM, "[원점OK]" if self.hx else "[원점없음]", self.py, self.py / PPMM, "[원점OK]" if self.hy else "[원점없음]"))
        self._emit("  Z=%d (%.2fmm) %s   (0 = 맨 위, + = 아래)" % (self.pz, self.pz / PPMM, "[원점OK]" if self.hz else "[원점없음]"))

    # ---- 펌웨어 루프 ---------------------------------------------------
    def _pop_line(self):
        with self.cv:
            i = self.inbuf.find(b"\n")
            if i < 0:
                return None
            ln = bytes(self.inbuf[:i]).decode("ascii", "replace").strip()
            del self.inbuf[: i + 1]
            return ln

    def _peek_abort(self):
        """블로킹 이동 중 '!' 가 들어왔는지(그 줄만 지운다)."""
        with self.cv:
            lines = bytes(self.inbuf).split(b"\n")
            for k, ln in enumerate(lines[:-1]):
                if ln.strip() == b"!":
                    off = sum(len(x) + 1 for x in lines[:k])
                    del self.inbuf[off: off + len(ln) + 1]
                    return True
        return False

    def _run(self):
        while self.alive:
            now = time.monotonic()
            if now < self.freeze_until:
                time.sleep(0.01)
                continue
            self._wd_check()
            ln = self._pop_line()
            if ln is None:
                with self.cv:
                    self.cv.wait(0.01)
                continue
            if ln == "":
                continue
            self.last_rx = time.monotonic()
            self.wd_tripped = False
            self.log.append(ln)
            try:
                self._cmd(ln)
            except Exception as e:                       # noqa: BLE001
                self._emit("  [거부] fake 오류 %s" % e)
            self.last_rx = time.monotonic()               # 명령이 끝나는 순간 타이머 재시작

    def _wd_check(self):
        if self.wd_ms == 0 or self.wd_tripped or not any(self.relay):
            return
        if (time.monotonic() - self.last_rx) * 1000.0 <= self.wd_ms:
            return
        self.relay = [False] * N_RELAY
        self.wd_tripped = True
        self._emit("  [!] 통신 끊김 · 릴레이 전부 꺼짐")
        self._rs()

    # ---- 명령 ---------------------------------------------------------
    def _cmd(self, ln):
        tok = ln.split()
        c = tok[0]
        self.abort_flag = False                           # 펌웨어는 명령마다 abortFlag 를 지운다
        num = lambda i, d=0: float(tok[i]) if len(tok) > i else d       # noqa: E731
        if c == "!":
            self._emit("  (이동 중에만 유효)")
        elif c == "st":
            self._st()
        elif c == "p":
            self._status()
        elif c in ("v", "a", "b"):
            if num(1) > 0:
                setattr(self, c, int(num(1)))
            self._status()
        elif c == "w":
            self._status()
        elif c == "rs":
            self._rs()
        elif c == "wd":
            v = int(num(1, -1)) if len(tok) > 1 else -1
            if v < 0 or v > 600000:
                self._emit("  [거부] wd 는 0~600000 ms")
            else:
                self.wd_ms = v
                self.last_rx = time.monotonic()
                self.wd_tripped = False
                self._emit("  통신 감시 %dms%s" % (v, "" if v else " (끔)"))
        elif c == "r":
            self._relay(tok)
        elif c == "save":
            self.dirty = False
            self._emit("  저장됨 (신뢰 확정)")
        elif c == "forget":
            self.hx = self.hy = self.hz = False
            self._emit("  기록 삭제")
            self._status()
        elif c == "zz":
            self.pz, self.hz = 0, True
            self._emit("  Z 여기를 0 으로 등록 (저장됨)")
            self._status()
        elif c == "zx":
            self.px, self.hx = 0, True
            self._emit("  X 여기를 0 으로 등록 (저장됨)")
            self._status()
        elif c == "zy":
            self.py, self.hy = 0, True
            self._emit("  Y 여기를 0 으로 등록 (저장됨)")
            self._status()
        elif c == "z":
            self.px = self.py = 0
            self.hx = self.hy = True
            self._emit("  X, Y 여기를 0 으로 등록 (저장됨)")
            self._status()
        elif c == "sp":
            x, y = int(num(1)), int(num(2))
            if len(tok) < 3:
                self._emit("  ? sp <x> <y>")
            elif x < 0 or x > X_MAX or y < 0 or y > Y_MAX:
                self._emit("  [거부] sp 값이 범위 밖")
            else:
                self.px, self.py, self.hx, self.hy = x, y, True, True
                self._emit("  PC 위치로 세팅 (저장됨)")
                self._status()
        elif c in ("mx", "my", "mz", "gx", "gy", "gz"):
            ax = c[1]
            mm = c[0] == "m"
            tgt = int(num(1) * PPMM + 0.5) if mm else int(num(1))
            self._goto(ax, tgt)
        elif c in ("jx", "jy", "jz"):
            self._jog(c[1], int(num(1)))
        else:
            self._emit("  ? fz z zx zy zz sp home gx gy gz g mx my mz jx jy jz x y x1 x2 rx ry fx fy save forget st p v a b w e !")

    def _relay(self, tok):
        if len(tok) < 3 or tok[2] not in ("0", "1"):
            self._emit("  [거부] r <1~10> <1|0>  또는  r all 0")
            return
        on = tok[2] == "1"
        if tok[1] == "all":
            if on:
                self._emit("  [거부] r all 은 0 만 (켜기는 하나씩)")
                return
            self.relay = [False] * N_RELAY
            self._emit("  릴레이 전부 꺼짐")
            self._rs()
            return
        n = int(tok[1]) if tok[1].isdigit() else 0
        if not 1 <= n <= N_RELAY:
            self._emit("  [거부] 릴레이 번호는 1~10")
            return
        self.relay[n - 1] = on
        self._emit("  릴레이 R%d %s" % (n, "켜짐" if on else "꺼짐"))
        self._rs()

    def _pos(self, ax):
        return {"x": self.px, "y": self.py, "z": self.pz}[ax]

    def _setpos(self, ax, v):
        if ax == "x":
            self.px = int(v)
        elif ax == "y":
            self.py = int(v)
        else:
            self.pz = int(v)

    def _homed(self, ax):
        return {"x": self.hx, "y": self.hy, "z": self.hz}[ax]

    def _goto(self, ax, tgt):
        if not self._homed(ax):
            self._emit("  [거부] 원점 미확정 → fz %s 또는 sp x y" % ax)
            return
        hi = {"x": X_MAX, "y": Y_MAX, "z": Z_MAX}[ax]
        if tgt < 0 or tgt > hi:
            self._emit("  [거부] 범위 밖  목표=%d  허용=0~%d" % (tgt, hi))
            return
        d = tgt - self._pos(ax)
        if d == 0:
            self._emit("  이미 그 위치")
            return
        self._move(ax, d)

    def _jog(self, ax, d):
        if abs(d) > 8000:
            self._emit("  [거부] jx/jy/jz 는 한 번에 8000 펄스까지")
            return
        if d == 0:
            self._emit("  이미 그 위치")
            return
        nxt = self._pos(ax) + d
        hi = {"x": X_MAX, "y": Y_MAX, "z": Z_MAX}[ax]
        out = self._move(ax, d, report=False)
        if self._homed(ax) and (nxt < 0 or nxt > hi):
            setattr(self, "h" + ax, False)
            self._emit("  [!] %s 가동범위 밖 → 원점 해제 (다시 등록 필요)" % ax.upper())
        self._report(ax, d, out)

    def _move(self, ax, d, report=True):
        """블로킹 이동. 시간이 걸리고 그동안 '!' 만 본다. 반환: 실제 움직인 펄스."""
        total = abs(d) / max(1.0, float(self.v)) * self.time_scale
        if self.hang_move:
            total = 1e9
            self.hang_move = False
        t0 = time.monotonic()
        self.busy = True
        moved = d
        while True:
            el = time.monotonic() - t0
            if self._peek_abort() or self.abort_flag:
                frac = min(1.0, el / total) if total > 0 else 1.0
                moved = int(d * frac)
                self.abort_flag = True
                break
            if el >= total:
                break
            time.sleep(0.004)
        self.busy = False
        self._setpos(ax, self._pos(ax) + moved)
        if report:
            self._report(ax, d, moved, t0)
        return moved

    def _report(self, ax, d, moved, t0=None):
        dt = (time.monotonic() - t0) * 1000.0 if t0 else 0.0
        extra = "  [!] 중단" if self.abort_flag else ""
        self._emit("  %s %s cmd=%d out=%d t=%.2fms | X=%d Y=%d Z=%d%s" % (ax.upper() + " ", "+" if d >= 0 else "-", abs(d), abs(moved), dt, self.px, self.py, self.pz, extra))
