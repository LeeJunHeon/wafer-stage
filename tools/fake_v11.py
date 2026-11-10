"""fake_v11.py - 펌웨어 V11 흉내(pyserial 모양: write · read · readline · in_waiting · timeout · close · reset_input_buffer · flush).

firmware/stage_v11/stage_v11.ino 의 앱이 쓰는 명령과 출력 형식을 그대로 흉내 낸다. 시간도 흉내 낸다 - 펌웨어는 단일 스레드라
블로킹 이동 중에는 다른 줄을 처리하지 않고('!' 만 이동 도중에 본다), 응답은 별도 스레드에서 비동기로 시리얼 쪽에 들어온다.

  명령: st · p · mx/my/mz · gx/gy/gz · jx/jy/jz · fz x/y/z [펄스] · zx/zy/zz/z · sp · v · a · b · w · wd · r N 0/1 · r all 0 · rs · save · forget · !
  출력: 시작 배너 · 이동 완료 줄(" | X=") · "  이미 그 위치" · "  [거부] …" · "  [!] 중단" · ST · RS · "  [!] 통신 끊김 · 릴레이 전부 꺼짐"
  통신 감시: 받은 줄마다 lastRx 갱신 · 블로킹 이동 중에는 판정 안 함 · 이동이 끝나는 순간 타이머 재시작.

.ino 에 맞춘 것(시험이 실제와 다른 조건에서 통과하지 않게):
  - 이동 시간 = 사다리꼴(가속 · 정속 · 감속). time_scale 1.0 이면 .ino 와 같은 시간.
  - '!' 확인은 64 펄스마다(그 순간 속도로 걸리는 시간 간격, 첫 펄스 전 포함). 끊긴 이동의 out 은 그때까지 나간 펄스(64 의 배수).
  - 축마다 "실제 위치(real)" 와 끝단(하드스톱)이 있다: 끝단을 넘는 펄스는 실제 위치를 바꾸지 않는다(헛돈다 - 카운터만 바뀐다).
  - dirty: 움직이면 dirty, save 하면 해제. 재부팅 때 dirty 면 "save 하지 않고 꺼짐" + 원점 없음 + 위치 0, 아니면 마지막 save 위치를 복원.
  - 수신 버퍼 64 바이트(넘치면 버린다). fz 의 0.5 s 대기 중에 온 '!' 는 물러나는 이동의 첫 펄스에서 끊긴다.
  - cuts: 끊긴 이동마다 {cmd, out, p_at_bang, t_bang, t_halt} - 정지 · 비상정지 지연 시험은 '!' 를 쓴 시각이 아니라 펌웨어가 멈춘 시각 · 펄스 수로 잰다.

시험용 조작: set_real_mm(축, mm) · latency(응답 지연) · drop_next(줄 하나 잃음) · reboot(eeprom=) · freeze(초) · slip(dx, dy) · time_scale(이동 시간 배율) · block_write(초: 쓰기 막힘).
EEPROM 상태(부팅 배너): restored(마지막 저장 위치 복원 - 원점 있음) · unsaved(save 없이 꺼짐 - 원점 없음) · none(기록 없음 - 원점 없음).
"""

import collections
import os
import threading
import time

PPMM = 160.0
X_MAX, Y_MAX, Z_MAX = 39620, 39640, 7200
N_RELAY = 10


class FakeV11:
    def __init__(self, time_scale=0.05, homed=True, banner=True, eeprom=None):
        self.timeout = 0.2
        self.write_timeout = None                   # pyserial 모양: 쓰기가 이 시간 안에 못 끝나면 SerialTimeoutException
        self.block_until = 0.0                      # 이 시각까지 쓰기가 막힘(USB 막힘 흉내)
        self.eeprom = eeprom or ("restored" if homed else "none")
        self.time_scale = float(time_scale)         # 1 = 실시간 이동 시간, 0.05 = 20배 빠르게
        self.latency = 0.0                          # 응답 한 줄이 시리얼에 나타나기까지 지연(s)
        self.hang_move = False                      # 다음 이동이 끝나지 않는다('!' 가 올 때까지 - 모터 멈춤 흉내)
        self.cut_until = 0.0                        # 이 시각까지 케이블이 끊김: 보낸 줄은 사라지고 응답도 호스트에 안 닿는다(펌웨어는 계속 돈다)
        self.bang_times = []                        # '!' 가 도착한 단조 시각(쓰기 시각이라 '!' 가 가려져도 남는다 - 지연 시험은 cuts 를 쓴다)
        self.cuts = []                              # 끊긴 이동: {cmd, out, p_at_bang, t_bang, t_halt, ax}
        self.overflow = 0                           # 수신 버퍼(64 바이트)가 넘쳐 버려진 바이트 수
        self.freeze_until = 0.0                     # 이 시각까지 아무것도 처리 · 응답하지 않음(링크 멈춤)
        self._drop = 0
        self.lock = threading.Lock()
        self.inbuf = bytearray()
        self.out = collections.deque()              # (나타날 시각, bytes)
        self.cv = threading.Condition()
        self.alive = True
        self.unplugged = False                      # USB 가 뽑혔다: 읽기 · 쓰기가 serial.SerialException (다시 꽂아도 이 객체는 계속 오류 - 새로 열어야 새 보드)
        self.true_off = [0.0, 0.0]                  # 탈조 - 실제 위치 = real + off (mm)
        self._init_state(bool(homed) and self.eeprom == "restored")
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
    def _chk(self):
        if self.unplugged:
            import serial
            raise serial.SerialException("ClearCommError failed (PermissionError(13, '장치가 시스템에 부착되어 있지 않습니다.', None, 22))")

    def unplug(self):
        self.unplugged = True
        self.abort_flag = True
        with self.cv:
            self.cv.notify_all()

    def write(self, data):
        self._chk()
        if time.monotonic() < self.block_until:
            wt = self.write_timeout
            wait = self.block_until - time.monotonic()
            if wt is None:
                time.sleep(max(0.0, wait))               # write_timeout 이 없으면 풀릴 때까지 영원히 막힌다
            else:
                time.sleep(min(wt, max(0.0, wait)))
                if time.monotonic() < self.block_until:
                    import serial
                    raise serial.SerialTimeoutException("Write timeout")
        if time.monotonic() < self.cut_until:
            return len(data)
        if b"!" in data:
            self.bang_times.append(time.monotonic())
        with self.cv:
            room = max(0, 64 - len(self.inbuf))             # 아두이노 수신 버퍼 64 바이트 - 넘치면 버린다
            if len(data) > room:
                self.overflow += len(data) - room
            self.inbuf += bytes(data)[:room]
            self.cv.notify_all()
        return len(data)

    def flush(self):
        pass

    @property
    def in_waiting(self):
        self._chk()
        now = time.monotonic()
        with self.cv:
            return sum(len(b) for t, b in self.out if t <= now)

    def reset_input_buffer(self):
        now = time.monotonic()
        with self.cv:
            while self.out and self.out[0][0] <= now:
                self.out.popleft()

    def read(self, n=1):
        self._chk()
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
        self._chk()
        end = time.monotonic() + (self.timeout or 0)
        while True:
            self._chk()
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
    def block_write(self, sec):
        self.block_until = time.monotonic() + sec

    def cut_link(self, sec):
        self.cut_until = time.monotonic() + sec

    def freeze(self, sec):
        self.freeze_until = time.monotonic() + sec

    def drop_next(self, n=1):
        self._drop += n

    def slip(self, dx_mm, dy_mm):
        self.true_off[0] += dx_mm
        self.true_off[1] += dy_mm

    def reboot(self, eeprom=None):
        """보드 재부팅: 릴레이 꺼짐 · 속도 기본값 · 통신 감시 꺼짐 · 배너를 다시 보낸다(eeprom: restored | unsaved | none)."""
        self.abort_flag = True
        if eeprom:
            self.eeprom = eeprom
        elif self.dirty:                                      # save 없이 움직였다 - 부팅 때 "save 하지 않고 꺼짐" + 원점 없음 + 위치 0
            self.eeprom = "unsaved"
        if self.eeprom == "restored":
            self.px, self.py, self.pz = self.saved
        else:
            self.px = self.py = self.pz = 0
        self.dirty = False
        self.hx = self.hy = self.hz = (self.eeprom == "restored")
        self.relay = [False] * N_RELAY
        self.v, self.a, self.b = 6000, 40000, 300
        self.wd_ms = 0
        self.wd_tripped = False
        self._banner()

    def true_xy(self):
        return (self.real["x"] / PPMM + self.true_off[0], self.real["y"] / PPMM + self.true_off[1])

    def set_z_mm(self, mm):
        self.pz = int(mm * PPMM)
        self.real["z"] = self.pz

    def set_real_mm(self, ax, mm):
        """시험용: 그 축의 실제(물리) 위치를 정한다(카운터는 그대로 - 펌웨어는 모른다)."""
        self.real[ax] = int(round(mm * PPMM))

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
        self.real = {"x": 0, "y": 0, "z": 0}
        self.saved = (0, 0, 0)
        self.hx = self.hy = self.hz = bool(homed)
        self.dirty = False
        self.relay = [False] * N_RELAY
        self.v, self.a, self.b = 6000, 40000, 300

    def _banner(self):
        self._emit("=== 3-AXIS V11 ===")
        self._emit("  X 0~%d  Y 0~%d  Z 0~%d   (160 pulse = 1mm, Z: 0 = 맨 위)" % (X_MAX, Y_MAX, Z_MAX))
        self._emit({"restored": "  EEPROM: 마지막 저장 위치 복원  (꺼진 동안 손으로 밀었다면 fz 로 재등록)",
                    "unsaved": "  EEPROM: save 하지 않고 꺼짐 → 위치 불신",
                    "none": "  EEPROM: 기록 없음"}.get(self.eeprom, "  EEPROM: 기록 없음"))
        self._status()

    def _rs(self):
        self._emit("RS " + " ".join("R%d=%d" % (i + 1, 1 if on else 0) for i, on in enumerate(self.relay)))

    split_st = 0                                            # 시험: 다음 ST 몇 개를 줄 중간에서 둘로 나눠 보낸다(앞 조각은 줄바꿈 없음 - pyserial 이 0.2 s 에 줄 중간까지만 주는 경우)
    trunc_st = False                                        # 시험: 나눈 앞 조각을 줄바꿈으로 끝낸다(잘린 ST 줄) - 뒤에 바른 줄을 따로 보낸다

    def _st(self):
        if os.environ.get("WAFER2_FAKE_SILENT_ST"):                # 시험: ST 에 답하지 않는 보드(연결 중 ST 무응답)
            return
        line = "ST X=%d Y=%d X2=%d Z=%d HX=%d HY=%d HZ=%d DIRTY=%d" % (self.px, self.py, self.px, self.pz, self.hx, self.hy, self.hz, self.dirty)
        if self.split_st > 0 and time.monotonic() >= self.cut_until:
            self.split_st -= 1
            cut = line.index(" Z=") + 4                             # "… Z=3" 까지(Z 의 첫 자리만) - 그대로 읽으면 Z 가 맨 위로 보인다
            now = time.monotonic() + self.latency
            with self.cv:
                if self.trunc_st:
                    self.out.append((now, (line[:cut] + "\n").encode("utf-8")))
                    self.out.append((now + 0.3, (line + "\n").encode("utf-8")))
                else:
                    self.out.append((now, line[:cut].encode("utf-8")))
                    self.out.append((now + 0.45, (line[cut:] + "\n").encode("utf-8")))
            return
        self._emit(line)

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
        """블로킹 이동 중 '!' 를 본다. **버퍼 맨 앞 바이트만** 본다(.ino 의 Serial.peek()) - 앞에 다른 줄이 있으면 이동이 끝날 때까지 무시된다."""
        with self.cv:
            if self.inbuf[:1] == b"!":
                i = self.inbuf.find(b"\n")
                del self.inbuf[: (i + 1) if i >= 0 else 1]
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
            self.eeprom = "restored"
            self.saved = (self.px, self.py, self.pz)
            self._emit("  저장됨 (신뢰 확정)")
        elif c == "fz":
            self._fz(tok)
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

    def _fz(self, tok):
        """.ino findZero: 끝단 쪽으로 searchLen(기본 800, 상한 1600) 펄스를 느리게(v1200) 밀고 → 0.5 s → 320 펄스(2 mm) 물러나 0 → 저장."""
        ax = tok[1].lower() if len(tok) > 1 else ""
        if ax not in ("x", "y", "z"):
            self._emit("  ? fz x [탐색펄스]  (x / y / z. 기본 800 = 5mm, 상한 1600)")
            return
        n = int(float(tok[2])) if len(tok) > 2 else 0
        if n <= 0:
            n = 800
        if n > 1600:
            self._emit("  [거부] fz 탐색은 1600 펄스(10mm)까지")
            return
        self._emit("--- 원점 탐색 (탐색 %d 펄스) --- 끝에 닿으면 드르륵 소리 (정상)" % n)
        sv, sa = self.v, self.a
        hx, hy, hz = self.hx, self.hy, self.hz
        self.hx = self.hy = self.hz = True                # 탐색 중에는 범위 검사를 하지 않는다
        self.v, self.a = 1200, 20000                       # .ino findZero: vMax = 1200 · accel = 20000
        self._move(ax, -n)
        time.sleep(0.5 * self.time_scale)
        if not self.abort_flag:
            self._move(ax, 320)
        self.v, self.a = sv, sa
        self.hx, self.hy, self.hz = hx, hy, hz
        if self.abort_flag:
            self._emit("--- 중단됨. 원점 설정 안 됨 ---")            # 위치 카운터는 이미 바뀌어 있다(헛돈 펄스 포함) - 원점 플래그만 fz 전 값
            return
        self._setpos(ax, 0)
        setattr(self, "h" + ax, True)
        self.dirty = False
        self.eeprom = "restored"
        self.saved = (self.px, self.py, self.pz)
        self._emit("--- 원점 설정 완료 (저장됨) ---")
        self._st()

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

    def _profile(self, n):
        """n 펄스 사다리꼴: (총 시간(s, 배율 전), pulses_at(t))."""
        v = max(float(self.v), float(self.b))
        a, b = float(self.a), min(float(self.b), v)
        d_acc = (v * v - b * b) / (2.0 * a)
        if n >= 2.0 * d_acc:
            t_acc = (v - b) / a
            t_cru = (n - 2.0 * d_acc) / v
            T = 2.0 * t_acc + t_cru

            def pa(t):
                if t <= 0:
                    return 0.0
                if t < t_acc:
                    return b * t + a * t * t / 2.0
                if t < t_acc + t_cru:
                    return d_acc + v * (t - t_acc)
                if t < T:
                    r = T - t
                    return n - (b * r + a * r * r / 2.0)
                return float(n)
        else:
            vp = (b * b + a * n) ** 0.5
            th = (vp - b) / a
            T = 2.0 * th

            def pa(t):
                if t <= 0:
                    return 0.0
                if t < th:
                    return b * t + a * t * t / 2.0
                if t < T:
                    r = T - t
                    return n - (b * r + a * r * r / 2.0)
                return float(n)
        return T, pa

    def _time_of(self, n, pa, T):
        """pulses_at 의 역함수(이분법): n 펄스째가 나가는 시각."""
        lo, hi = 0.0, T
        for _ in range(40):
            mid = (lo + hi) / 2.0
            if pa(mid) < n:
                lo = mid
            else:
                hi = mid
        return hi

    def _move(self, ax, d, report=True):
        """블로킹 이동(사다리꼴). 64 펄스마다 버퍼 맨 앞 바이트가 '!' 인지만 본다(첫 펄스 전 포함). 반환: 실제 움직인 펄스."""
        n = abs(int(d))
        sign = 1 if d >= 0 else -1
        sc = self.time_scale
        if self.hang_move:
            T, pa = 1e9, (lambda t: self.v * t)
            self.hang_move = False
        else:
            T, pa = self._profile(n)
        t0 = time.monotonic()
        self.busy = True
        out = 0
        aborted = False
        p_at_bang = None
        t_bang = None
        while True:
            el = (time.monotonic() - t0) / sc
            if self._peek_abort() or self.abort_flag:                 # 64 펄스 경계(또는 첫 펄스 전)에서만 보인다
                aborted = True
                p_el = pa(el)
                t_arr = None
                if self.bang_times and self.bang_times[-1] >= t0:
                    t_bang = self.bang_times[-1]
                    t_arr = max(0.0, (t_bang - t0) / sc)                # '!' 바이트가 버퍼에 들어온 시각(이동 시작 뒤)
                    p_at_bang = pa(t_arr)
                p_chk = p_at_bang if p_at_bang is not None else p_el
                out = min(n, int(-(-p_chk // 64)) * 64) if (el > 0 and p_chk > 0) else 0     # 그 뒤 첫 64 펄스 경계(첫 펄스 전이면 0)
                # 경계 펄스까지 마저 나간 뒤 멈춘다
                if out > p_el and out > 0:
                    t_b = self._time_of(out, pa, T) * sc
                    wait = t0 + t_b - time.monotonic()
                    if wait > 0:
                        time.sleep(wait)
                self.abort_flag = True
                break
            if el >= T:
                out = n
                break
            time.sleep(0.002 if sc >= 0.5 else 0.001)
        self.busy = False
        moved = out * sign
        self._setpos(ax, self._pos(ax) + moved)
        lo, hi = -320, {"x": X_MAX + 320, "y": Y_MAX + 320, "z": Z_MAX}[ax]
        self.real[ax] = max(lo, min(hi, self.real[ax] + moved))      # 끝단을 넘는 펄스는 실제 위치를 바꾸지 않는다(헛돈다)
        if out:
            self.dirty = True
        if aborted:
            self.cuts.append({"ax": ax, "cmd": n, "out": out, "p_at_bang": p_at_bang, "t_bang": t_bang, "t_halt": time.monotonic()})
        if report:
            self._report(ax, d, moved, t0)
        return moved

    def _report(self, ax, d, moved, t0=None):
        dt = (time.monotonic() - t0) * 1000.0 if t0 else 0.0
        extra = "  [!] 중단" if self.abort_flag else ""
        self._emit("  %s %s cmd=%d out=%d t=%.2fms | X=%d Y=%d Z=%d%s" % (ax.upper() + " ", "+" if d >= 0 else "-", abs(d), abs(moved), dt, self.px, self.py, self.pz, extra))
