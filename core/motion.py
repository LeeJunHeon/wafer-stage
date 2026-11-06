"""motion.py - 이동 계층. 모든 X · Y · Z 이동 · 조그 · 원점 등록이 이 클래스를 지나간다.

불변식 (CLAUDE.md "안전 불변식")
1. 이동 잠금은 하나(`lock`). Z 올림과 X · Y 이동을 한 잠금으로 묶는다.
2. X · Y 이동 전 Z 맨 위 - 캐시가 아니라 펌웨어 ST 로 Z=0 을 확인한 뒤에만 움직인다. 올림이 실패하면 그 흐름을 끝낸다. 설정으로 끌 수 없다.
3. 비상정지 세대 번호(`gen`): 명령(op)이 시작될 때 세대를 잡고, 시리얼에 쓰기 직전마다(Stage.write_guard) 비교한다. 달라졌으면 쓰지 않는다
   (대기 중인 조그 조각 · 원점 잡기 · 이동 포함).
4. 비상정지 잠금은 사람의 회복 동작으로만 풀린다: Z 끝단 -> Z 원점 등록 -> 위치 확인 -> 적용(apply_position). sp 는 그 사이에 위치가 바뀌었으면 거절.
5. Z 원점이 없으면 X · Y 이동은 거절이다(경고 후 진행이 아니다).
6. 이동 시간 한도는 거리 · 속도 · 가속으로 계산한다. 넘으면 느낌표 정지 + 재동기 뒤 연결은 유지하되 위치를 못 믿는다(위치 확인 요구).
7. 히트건이 꺼졌다고 확인되지 않으면 X · Y 이동을 거절한다(pre_xy 훅).
다른 모듈은 시리얼에 직접 쓰지 않는다.
"""

import threading

from core import stage as stage_mod
from core.stage import StageError, StageGuard, StageTimeout

Z_TOP_TOL_MM = 0.05
DEADLINE_MARGIN = 1.5
DEADLINE_EXTRA_S = 2.0


class MotionError(Exception):
    """화면에 가는 한 줄(60자 이내) 사유. code 는 시험 · 분기용."""

    def __init__(self, msg, code="reject"):
        super().__init__(msg)
        self.code = code


class Motion:
    def __init__(self, stage, log=None):
        self.stage = stage
        self.log = log or (lambda *_: None)
        self.lock = threading.RLock()                 # 이동 잠금 - 하나
        self.gen = 0                                  # 비상정지 세대
        self.estop_locked = False
        self.z_ok = True                              # Z 원점이 믿을 만함
        self.xy_ok = True                             # X · Y 위치가 믿을 만함
        self.rebooted = False
        self.pre_xy = None                            # fn() - X · Y 이동 전 점검(히트건 꺼짐 확인). 거절하면 MotionError
        self.on_reboot = None                         # fn(line) - 서버가 wd · 속도 · 가속 재전송, ST · RS 재읽기, 측정 중지
        self._tl = threading.local()
        self.moving = False
        self.speed_pps = stage_mod.DEFAULT_SPEED_PPS
        self.accel = 40000
        stage.write_guard = self._guard
        prev = stage.on_event

        def _ev(kind, line):
            if kind == "reboot":
                self._reboot(line)
            if prev:
                prev(kind, line)
        stage.on_event = _ev

    # ---- 상태 --------------------------------------------------------
    def sync_from_status(self):
        """펌웨어 ST 의 원점 플래그로 z_ok · xy_ok 를 정한다(연결 직후 · 재부팅 뒤)."""
        st = self.stage.status()
        self.z_ok = bool(st.get("homed_z"))
        self.xy_ok = bool(st.get("homed_x") and st.get("homed_y"))
        return st

    def recovery_steps(self):
        """회복 순서 중 남은 것(띠에 보여 준다). 비어 있으면 잠금 없음."""
        out = []
        if not self.z_ok:
            out += ["Z 끝단", "Z 원점 등록"]
        if not self.xy_ok or self.estop_locked:
            out += ["위치 확인", "적용"]
        return out

    # ---- 가드 --------------------------------------------------------
    def _guard(self, cmd):
        tok = getattr(self._tl, "gen", None)
        if tok is not None and tok != self.gen:
            raise StageGuard("비상정지 · 쓰기 취소 (%s)" % cmd)

    class _Op:
        def __init__(self, m):
            self.m = m

        def __enter__(self):
            self.prev = getattr(self.m._tl, "gen", None)
            self.m._tl.gen = self.m.gen
            return self

        def __exit__(self, *a):
            self.m._tl.gen = self.prev

    def op(self):
        """세대를 잡는다. 이 안의 모든 시리얼 쓰기는 직전에 세대를 비교한다."""
        return Motion._Op(self)

    def _check_gen(self):
        tok = getattr(self._tl, "gen", None)
        if tok is not None and tok != self.gen:
            raise MotionError("비상정지로 중단", "estop")

    # ---- 비상정지 ----------------------------------------------------
    def estop(self, relays_off=False, off_relays=()):
        """어느 스레드에서든 즉시. 잠금을 기다리지 않는다."""
        self.gen += 1                                  # 먼저 세대부터 올려 대기 중인 쓰기를 막는다
        self.estop_locked = True
        self.z_ok = False
        self.xy_ok = False
        self.log("비상정지 · 세대 %d" % self.gen)
        return self.stage.abort(relays_off=relays_off, off_relays=off_relays)

    def _reboot(self, line):
        self.gen += 1
        self.estop_locked = True
        self.z_ok = False
        self.xy_ok = False
        self.rebooted = True
        self.log("보드 재부팅 감지 · %s" % line)
        fn = self.on_reboot
        if fn:
            try:
                fn(line)
            except Exception:                          # noqa: BLE001
                pass

    # ---- 시간 한도 ---------------------------------------------------
    def deadline_s(self, dist_mm, ppmm=None):
        v = max(1.0, float(self.speed_pps))
        a = max(1.0, float(self.accel))
        pulses = abs(dist_mm) * (ppmm or stage_mod.PPMM)
        t = pulses / v + 2.0 * v / a                   # 정속 구간 + 가감속
        return t * DEADLINE_MARGIN + DEADLINE_EXTRA_S

    # ---- 읽기 --------------------------------------------------------
    def status(self):
        with self.op():
            return self.stage.status()

    # ---- 이동 --------------------------------------------------------
    def _run(self, what, fn):
        """이동 한 번. 시간 초과 -> 정지 + 재동기, 세대 변경 -> 중단 보고."""
        try:
            return fn()
        except StageGuard as e:
            raise MotionError("비상정지로 중단", "estop") from e
        except StageTimeout as e:
            ok = self.stage.resync()
            self.xy_ok = False                         # 위치를 못 믿는다 - 위치 확인 뒤 다시
            self.log("%s 시간 초과 · 정지 + 재동기 %s · %s" % (what, "성공" if ok else "실패", e))
            raise MotionError("%s 시간 초과 · 위치 확인 필요%s" % (what, "" if ok else " · 재동기 실패"), "timeout") from e
        except StageError as e:
            if self.gen != getattr(self._tl, "gen", self.gen):
                raise MotionError("비상정지로 중단", "estop") from e
            raise MotionError(_short(e), "stage") from e

    def _need_origin(self, xy=True):
        if self.estop_locked:
            raise MotionError("비상정지 잠금 · 회복 순서: " + " → ".join(self.recovery_steps()), "locked")
        if not self.z_ok:
            raise MotionError("Z 원점 없음 · Z 끝단 → Z 원점 등록", "no_z")
        if xy and not self.xy_ok:
            raise MotionError("위치 못 믿음 · 위치 확인 필요", "no_xy")

    def lift_z(self):
        """Z 를 맨 위로(ST 로 확인). 이동 잠금 안에서만 부른다."""
        st = self.stage.status()
        z = st.get("z_mm")
        if z is None:
            raise MotionError("Z 위치를 못 읽음 · 이동 거절", "z_unknown")
        if abs(z) > Z_TOP_TOL_MM:
            self._run("Z 올림", lambda: self.stage.move_z_mm(0.0, deadline_s=self.deadline_s(abs(z), stage_mod.Z_PPMM)))
            z = self.stage.status().get("z_mm")
            if z is None or abs(z) > Z_TOP_TOL_MM:
                raise MotionError("Z 올림 실패 · X Y 이동 안 함", "z_lift")
        return True

    def move_xy(self, x, y):
        """X · Y 이동 하나. 이동 잠금 안에서 Z 올림 -> ST 로 Z=0 확인 -> X -> Y. 설정으로 끌 수 없다."""
        with self.lock:
            self._need_origin(xy=True)
            if self.pre_xy:
                self.pre_xy()                          # 히트건 꺼짐 확인 - 거절하면 여기서 끝
            with self.op():
                self.moving = True
                try:
                    self.lift_z()
                    cur = self.stage.status()
                    self._check_gen()
                    self._run("X 이동", lambda: self.stage.move_x_mm(x, deadline_s=self.deadline_s(abs(x - cur["x_mm"]))))
                    self._run("Y 이동", lambda: self.stage.move_y_mm(y, deadline_s=self.deadline_s(abs(y - cur["y_mm"]))))
                finally:
                    self.moving = False
            return self.stage.status()

    def move_z(self, mm):
        with self.lock:
            self._need_origin(xy=(mm > Z_TOP_TOL_MM))
            with self.op():
                cur = self.stage.status().get("z_mm") or 0.0
                self._run("Z 이동", lambda: self.stage.move_z_mm(mm, deadline_s=self.deadline_s(abs(mm - cur), stage_mod.Z_PPMM)))
            return self.stage.status()

    # ---- 조그 · 원점 (회복 동작은 잠금 상태에서도 허용) ----------------
    def jog(self, axis, mm):
        with self.lock:
            with self.op():
                self.moving = True
                try:
                    return self._run("조그", lambda: self.stage.jog_rel(axis, mm))
                finally:
                    self.moving = False

    def register_z_origin(self):
        """Z 원점 등록(회복 2단계). 지금 자리를 Z=0 으로. X · Y 는 위치 확인이 끝나야 믿는다."""
        with self.lock:
            with self.op():
                self.stage.set_zero("z")
                self.stage._aborted = not self.xy_ok            # 드라이버 잠금은 X · Y 가 믿을 만할 때만 푼다
                self.z_ok = True
                if self.xy_ok:
                    self.estop_locked = False
            return self.stage.status()

    def register_xy_origin(self):
        """수동 X · Y 원점 등록(보조)."""
        with self.lock:
            with self.op():
                self.stage.set_zero("xy")
                self.xy_ok = True
                if self.z_ok:
                    self.estop_locked = False
            return self.stage.status()

    def apply_position(self, x, y, expect_gen=None, expect_xy=None, tol_mm=0.5):
        """위치 확인 결과 적용(sp). 회복 3~4단계.

        expect_gen: 촬영을 시작할 때의 세대 - 그 사이 비상정지가 있었으면 거절.
        expect_xy: 촬영 전 펌웨어 위치 (x, y) - 그 사이 위치가 바뀌었으면 거절.
        """
        with self.lock:
            if expect_gen is not None and expect_gen != self.gen:
                raise MotionError("촬영 중 비상정지 · 위치 적용 안 함", "estop")
            if not self.z_ok:
                raise MotionError("Z 원점 없음 · Z 끝단 → Z 원점 등록 먼저", "no_z")
            with self.op():
                if expect_xy is not None:
                    st = self.stage.status()
                    if abs(st["x_mm"] - expect_xy[0]) > tol_mm or abs(st["y_mm"] - expect_xy[1]) > tol_mm:
                        raise MotionError("촬영 사이 위치가 바뀜 · 위치 적용 안 함", "moved")
                self._run("위치 적용", lambda: self.stage.set_position(x, y))
                self.stage.reset_abort()
                self.xy_ok = True
                self.estop_locked = False
                self.rebooted = False
            return self.stage.status()


def _short(e, n=58):
    t = str(e).replace("\n", " ").strip()
    return t if len(t) <= n else t[:n - 1] + "…"
