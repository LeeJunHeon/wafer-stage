"""motion.py - 이동 계층. 모든 X · Y · Z 이동 · 조그 · 원점 등록이 이 클래스를 지나간다.

불변식 (CLAUDE.md "안전 불변식")
1. 이동 잠금은 하나(`lock`). Z 올림과 X · Y 이동을 한 잠금으로 묶는다.
2. X · Y 이동 전 Z 맨 위 - 캐시가 아니라 펌웨어 ST 로 Z=0 을 확인한 뒤에만 움직인다. 올림이 실패하면 그 흐름을 끝낸다. 설정으로 끌 수 없다.
3. 비상정지 세대 번호(`gen`): 명령(op)이 시작될 때 세대를 잡고, 시리얼에 쓰기 직전마다(Stage.write_guard) 비교한다. 달라졌으면 쓰지 않는다
   (대기 중인 조그 조각 · 원점 잡기 · 이동 포함).
4. 비상정지 잠금은 사람의 회복 동작으로만 풀린다: Z 원점 잡기(fz) -> (위치 확인 -> 적용(sp)) 또는 X · Y 원점 잡기(fz). sp 는 그 사이에 위치가 바뀌었으면 거절.
5. Z 원점이 없으면 X · Y 이동은 거절이다(경고 후 진행이 아니다).
6. 이동 시간 한도는 거리 · 속도 · 가속으로 계산한다. 넘으면 느낌표 정지 + 재동기 뒤 연결은 유지하되 위치를 못 믿는다(위치 확인 요구).
7. 히트건이 꺼졌다고 확인되지 않으면 X · Y 이동을 거절한다(pre_xy 훅).
8. 사람이 보낸 이동 명령(nowait=True)은 이동 잠금을 기다리지 않는다 - 이동 중이면 즉시 거절(줄 세우지 않는다). 비상정지 직후 1 s 는 모두 거절.
9. 조그 한 번의 한도 · Z 아래 한도 · 핀이 내려가 있을 때 X · Y 거절 등은 jog_guard 훅(App)이 정한다. 회복 중 조그는 한 번 5 mm 이하.
다른 모듈은 시리얼에 직접 쓰지 않는다.
"""

import contextlib
import threading
import time

from core import stage as stage_mod
from core.stage import StageError, StageGuard, StageLate, StageStopped, StageTimeout, StageWriteTimeout

Z_TOP_TOL_MM = 0.05
DEADLINE_MARGIN = 1.5
DEADLINE_EXTRA_S = 2.0
STOP_QUIET_S = 0.5                            # 정지 직후 이 시간 안에는 사람이 보낸 이동 명령을 받지 않는다
LATE_START_S = 1.0                            # 사람이 보낸 이동 명령은 받은 뒤 이 시간 안에 첫 줄을 쓰지 못하면 거절
ESTOP_QUIET_S = 1.0                           # 비상정지 직후 이 시간 안에는 사람이 보낸 이동 명령을 받지 않는다
RECOVERY_JOG_MAX_MM = 5.0                     # 잠금 · 원점 없음(회복) 중 조그는 한 번 이 거리 이하만


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
        self.z_ok = False                             # Z 원점이 믿을 만함(기본은 못 믿음 - ST 동기화가 끝난 뒤에만 올린다)
        self.xy_ok = False                            # X · Y 위치가 믿을 만함
        self.stop_t = -1e9                            # 마지막 정지(세대를 올린 것만) 단조 시각
        self.last_estop_gen = 0                       # 가장 최근 비상정지 · 재부팅이 만든 세대 번호
        self.rebooted = False
        self.pre_xy = None                            # fn() - X · Y 이동 전 점검(히트건 꺼짐 확인). 거절하면 MotionError
        self.on_reboot = None                         # fn(line) - 서버가 wd · 속도 · 가속 재전송, ST · RS 재읽기, 측정 중지
        self._tl = threading.local()
        self.xy_done = {"x": False, "y": False}        # 이번 회복에서 축별 원점을 다시 잡았나(비상정지 · 재부팅 · 시간 초과면 False 로)
        self.slow_pps = 1500                          # 접촉 높이 마지막 구간 속도(설정 motion.jog_slow_pps)
        self.moving = False
        self.cur_op = ""                               # 지금 도는 이동의 종류(xy | "") - 정지가 끊은 것이 X · Y 절대 이동인지 가른다
        self.estop_t = -1e9                            # 마지막 비상정지(재부팅 포함) 단조 시각
        self.jog_guard = None                         # fn(axis, mm, status, opts) - 조그 규칙(Z 아래 한도 · 핀이 내려가 있으면 X · Y 거절 ...). 거절하면 MotionError
        self.last_move_end = 0.0                      # 마지막 이동이 끝난 단조 시각 - 촬영은 이 시각 이후 프레임만 쓴다
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
        self.xy_done = {"x": bool(st.get("homed_x")), "y": bool(st.get("homed_y"))}
        return st

    def recovery_steps(self):
        """회복 순서 중 남은 것(띠 · 로그에 보여 준다). 비어 있으면 잠금 없음."""
        out = []
        if not self.z_ok:
            out.append("Z 원점 잡기")
        if not self.xy_ok or self.estop_locked:
            if self.xy_done["x"] != self.xy_done["y"]:
                out.append("%s 원점 잡기" % ("Y" if self.xy_done["x"] else "X"))
            else:
                out.append("위치 확인 · 적용 또는 X · Y 원점 잡기")
        return out

    def _xy_lost(self):
        self.xy_ok = False
        self.xy_done = {"x": False, "y": False}

    # ---- 가드 --------------------------------------------------------
    def _guard(self, cmd):
        tok = getattr(self._tl, "gen", None)
        if tok is not None and tok != self.gen:
            raise StageGuard("%s · 쓰기 취소 (%s)" % ("비상정지" if self.last_estop_gen > tok else "정지", cmd))
        dl = getattr(self._tl, "late", None)
        if dl is not None:                              # 첫 이동 줄 직전: 받은 지 너무 오래됐으면 쓰지 않는다
            self._tl.late = None
            if time.monotonic() > dl:
                raise StageLate("늦은 이동 · 거절 (%s)" % cmd)

    def cancel_err(self, gen=None):
        """세대가 바뀌어 취소된 이유: 그 사이 비상정지가 있었으면 비상정지, 아니면 정지."""
        g = gen if gen is not None else getattr(self._tl, "gen", None)
        if g is not None and self.last_estop_gen > g:
            return MotionError("비상정지로 중단", "estop")
        return MotionError("정지로 중단", "stop")

    class _Op:
        def __init__(self, m, gen=None):
            self.m = m
            self.gen = gen

        def __enter__(self):
            self.prev = getattr(self.m._tl, "gen", None)
            self.m._tl.gen = self.m.gen if self.gen is None else self.gen
            return self

        def __exit__(self, *a):
            self.m._tl.gen = self.prev

    def op(self, gen=None):
        """세대를 잡는다. 이 안의 모든 시리얼 쓰기는 직전에 세대를 비교한다. gen 은 **명령이 들어온 순간** 잡은 값."""
        return Motion._Op(self, gen)

    def _enter(self, gen):
        """잠금을 얻은 직후 부른다: 명령이 들어온 뒤 비상정지가 있었으면 쓰지 않고 중단."""
        if gen is not None and gen != self.gen:
            raise self.cancel_err(gen)

    def _check_gen(self):
        tok = getattr(self._tl, "gen", None)
        if tok is not None and tok != self.gen:
            raise self.cancel_err(tok)

    @contextlib.contextmanager
    def _locked(self, nowait=False):
        """이동 잠금. nowait 이면(사람이 보낸 명령) 기다리지 않고 즉시 거절한다 - 이동이 끝난 뒤 늦게 실행되는 일이 없게."""
        t_in = time.monotonic()
        if nowait:
            if t_in - self.estop_t < ESTOP_QUIET_S:
                raise MotionError("비상정지 직후 · 잠시 뒤", "estop_recent")
            if t_in - self.stop_t < STOP_QUIET_S:
                raise MotionError("정지 직후 · 잠시 뒤", "stop_recent")
            if not self.lock.acquire(blocking=False):
                raise MotionError("이동 중 · 끝난 뒤", "busy")
            self._tl.late = t_in + LATE_START_S
        else:
            self.lock.acquire()
        try:
            yield
        finally:
            self._tl.late = None
            self.lock.release()

    # ---- 비상정지 ----------------------------------------------------
    def estop(self, relays_off=False, off_relays=()):
        """어느 스레드에서든 즉시. 잠금을 기다리지 않는다."""
        self.estop_t = time.monotonic()
        self.gen += 1                                  # 먼저 세대부터 올려 대기 중인 쓰기를 막는다
        self.last_estop_gen = self.gen
        self.estop_locked = True
        self.z_ok = False
        self._xy_lost()
        try:
            return self.stage.abort(relays_off=relays_off, off_relays=off_relays)      # '!' 를 먼저 쓴다
        finally:
            self.log("비상정지 · 세대 %d" % self.gen)

    def stop(self, bump=True):
        """정지: 어느 스레드에서든 즉시. 세대를 올려 뒤따르는 쓰기를 막고 '!' 만 쓴다 - 잠그지 않고 릴레이 · 원점도 건드리지 않는다.
        bump=False(단독 R-T 측정 중): 세대를 올리지 않는다 - 올리면 측정 스레드의 히트건 켜기 쓰기가 막힌다."""
        if bump:
            self.stop_t = time.monotonic()
            self.gen += 1
        try:
            return self.stage.stop()                   # '!' 를 먼저 쓴다
        finally:
            self.log("정지 · 세대 %d" % self.gen)

    def _reboot(self, line):
        self.estop_t = time.monotonic()
        self.gen += 1
        self.last_estop_gen = self.gen
        self.estop_locked = True
        self.z_ok = False
        self._xy_lost()
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
        except StageLate as e:
            raise MotionError("늦은 이동 · 거절 · 다시 누르세요", "late") from e
        except StageGuard as e:
            raise self.cancel_err() from e
        except StageStopped as e:
            raise MotionError("정지로 중단", "stop") from e
        except StageWriteTimeout as e:
            self._xy_lost()                            # 쓰기가 막혔다 - 명령이 늦게 도착해 움직일 수 있다: 위치를 못 믿고 절대 이동을 잠근다
            self.stage._aborted = True
            if str(what).startswith("Z"):
                self.z_ok = False
            self.log("%s 쓰기 시간 초과 · 위치 못 믿음" % what)
            raise MotionError("%s 쓰기 시간 초과 · 통신 끊김 · 위치 확인 필요" % what, "timeout") from e
        except StageTimeout as e:
            ok = e.resynced if hasattr(e, "resynced") else self.stage.resync()
            self._xy_lost()                            # 위치를 못 믿는다 - 위치 확인(또는 X · Y 원점) 뒤 다시
            if str(what).startswith("Z"):
                self.z_ok = False                      # Z 가 내려간 채 멈췄을 수 있다 - Z 원점부터 다시(Z 원점 잡기는 먼저 끝단 앞까지 올린다)
            io = isinstance(e, stage_mod.StageIOError)
            self.log("%s %s · 정지 + 재동기 %s · %s" % (what, "통신 끊김" if io else "시간 초과", "성공" if ok else "실패", e))
            raise MotionError("%s %s · 위치 확인 필요%s" % (what, "통신 끊김" if io else "시간 초과", "" if ok else " · 재동기 실패"), "timeout") from e
        except StageError as e:
            if self.gen != getattr(self._tl, "gen", self.gen):
                raise self.cancel_err() from e
            raise MotionError(_short(e), "stage") from e

    def _need_origin(self, xy=True):
        if self.estop_locked:
            raise MotionError("비상정지 잠금 · 회복 순서: " + " → ".join(self.recovery_steps()), "locked")
        if not self.z_ok:
            raise MotionError("Z 원점 없음 · Z 원점 잡기 먼저", "no_z")
        if xy and not self.xy_ok:
            raise MotionError("위치 못 믿음 · 위치 확인 또는 X · Y 원점 잡기", "no_xy")

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

    def move_xy(self, x, y, gen=None, nowait=False):
        """X · Y 이동 하나. 이동 잠금 안에서 Z 올림 -> ST 로 Z=0 확인 -> X -> Y. 설정으로 끌 수 없다."""
        g = self.gen if gen is None else gen             # 잠금 전에 잡는다
        for v in (x, y):
            if not isinstance(v, (int, float)) or v != v or abs(v) == float("inf"):
                raise MotionError("좌표 값 오류", "range")
        _lm = stage_mod.limits_mm()
        lx, ly = _lm["x_max_mm"], _lm["y_max_mm"]
        if not (0 <= x <= lx and 0 <= y <= ly):
            raise MotionError("가동범위 밖 · X %.1f Y %.1f" % (x, y), "range")
        with self._locked(nowait):
            self._enter(g)
            self._need_origin(xy=True)
            with self.op(g):
                if self.pre_xy:
                    self.pre_xy()                      # 히트건 끄기 전송 + 확인 - 거절하면 여기서 끝
                self.moving = True
                self.cur_op = "xy"
                try:
                    self.lift_z()
                    cur = self.stage.status()
                    self._check_gen()
                    self._run("X 이동", lambda: self.stage.move_x_mm(x, deadline_s=self.deadline_s(abs(x - cur["x_mm"]))))
                    self._run("Y 이동", lambda: self.stage.move_y_mm(y, deadline_s=self.deadline_s(abs(y - cur["y_mm"]))))
                finally:
                    self.moving = False
                    self.cur_op = ""
                    self.last_move_end = time.monotonic()
            return self.stage.status()

    def move_z(self, mm, gen=None, nowait=False):
        g = self.gen if gen is None else gen
        with self._locked(nowait):
            self._enter(g)
            self._need_origin(xy=(mm > Z_TOP_TOL_MM))
            with self.op(g):
                cur = self.stage.status().get("z_mm") or 0.0
                self.moving = True
                try:
                    self._run("Z 이동", lambda: self.stage.move_z_mm(mm, deadline_s=self.deadline_s(abs(mm - cur), stage_mod.Z_PPMM)))
                finally:
                    self.moving = False
                    self.last_move_end = time.monotonic()
            return self.stage.status()

    # ---- 조그 · 원점 (회복 동작은 잠금 상태에서도 허용) ----------------
    def jog(self, axis, mm, gen=None, nowait=False, opts=None, internal=False, speed=None, nohome=False):
        """조그 한 번(펌웨어 jx/jy/jz, 8000 펄스 조각 · 조각마다 비상정지 · 정지 확인 · 쓰기 직전 세대 확인).

        nohome = 사람이 켠 '원점 없이 이동 허용'(App.free_move): 원점 · 위치 신뢰 · 비상정지 잠금(회복 순서) · 회복 중 5 mm 한도 ·
        핀 내려감 판정(jog_guard)을 건너뛴다. 지키는 것: 이동 잠금(이동 중 즉시 거절 · 비상정지 직후 1 s · 정지 직후 0.5 s · 늦은 이동) ·
        세대(비상정지 즉시) · 이동 시간 한도 + 재동기 · 통신 끊김 · X · Y 전 히트건 꺼짐 확인 · 원점이 있는 축의 가동범위."""
        g = self.gen if gen is None else gen
        ax = str(axis).lower()
        with self._locked(nowait):
            self._enter(g)
            if not nohome and not self.z_ok and not (ax == "z" and mm < 0):
                raise MotionError("Z 원점 잡기 먼저 · 지금은 Z 위쪽 조그만", "no_z")
            if not internal and not nohome and (self.estop_locked or not self.z_ok or not self.xy_ok) and abs(float(mm)) > RECOVERY_JOG_MAX_MM + 1e-9:
                raise MotionError("회복 중 조그는 한 번 최대 %g mm" % RECOVERY_JOG_MAX_MM, "recovery_step")
            with self.op(g):
                st = None
                if (ax == "z" and self.z_ok) or (ax in ("x", "y") and self.xy_ok) or self.jog_guard:
                    st = self.stage.status()
                if (ax == "z" and self.z_ok) or (ax in ("x", "y") and self.xy_ok):
                    cur = st.get(ax + "_mm")                         # 원점이 있는 축은 가동범위 밖으로 몰지 않는다(펌웨어가 그 축 원점을 풀어 버린다)
                    hi = stage_mod.limits_mm()[ax + "_max_mm"]
                    if cur is None or not (0.0 <= cur + float(mm) <= hi + 1e-9):
                        raise MotionError("가동범위 밖 · %s 조그 %+g mm 거절" % (ax.upper(), float(mm)), "range")
                if self.jog_guard and not internal:
                    self.jog_guard(ax, float(mm), st, opts or {}, self.z_ok)
                if ax in ("x", "y") and self.pre_xy:
                    self.pre_xy()                                 # X · Y 조그도 히트건 끄기 확인 뒤에(Z 위쪽 조그는 예외)
                self.moving = True
                try:
                    return self._run("조그", lambda: self.stage.jog_rel(axis, mm, speed=speed))
                finally:
                    self.moving = False
                    self.last_move_end = time.monotonic()

    # ---- 원점 잡기 (펌웨어 fz) ------------------------------------------
    def find_origin(self, axis, gen=None, approach_mm=3.0, nowait=False):
        """펌웨어 fz 로 원점을 잡는다: 끝단 쪽으로 최대 10 mm 를 느리게 밀고 -> 2 mm 물러나 0 -> EEPROM 저장.

        펌웨어 위치가 남아 있으면(비상정지 뒤 · 원점 해제 전) 끝단 approach_mm 앞까지 먼저 다가간다.
        없으면 사람이 조그로 끝단 10 mm 안까지 옮겨 둔 것으로 본다.
        X · Y 는 Z 원점이 있고 Z 가 맨 위(ST 로 확인)일 때만. 세대 검사 · 거래 잠금을 거친다.
        """
        ax = str(axis).lower()
        if ax not in ("x", "y", "z"):
            raise MotionError("축은 X · Y · Z", "range")
        g = self.gen if gen is None else gen
        info = {"approach": False}
        with self._locked(nowait):
            self._enter(g)
            if ax != "z" and not self.z_ok:
                raise MotionError("Z 원점 먼저 · Z 원점 잡기", "no_z")
            with self.op(g):
                st = self.stage.status()
                if ax != "z":
                    z = st.get("z_mm")
                    if z is None:
                        raise MotionError("Z 위치를 못 읽음 · 원점 잡기 거절", "z_unknown")
                    if abs(z) > Z_TOP_TOL_MM:                              # 내려가 있으면 상대 조그로 먼저 올린다(위로만 - 절대 이동이 잠겨 있어도 된다)
                        self._run("Z 올림", lambda: self.stage.jog_rel("z", -float(z)))
                        z = self.stage.status().get("z_mm")
                        if z is None or abs(z) > Z_TOP_TOL_MM:
                            raise MotionError("Z 가 맨 위 아님 · Z↑ 먼저", "z_lift")
                homed = bool(st.get("homed_" + ax))
                pos = float(st.get(ax + "_mm") or 0.0)
                hi = stage_mod.limits_mm()[ax + "_max_mm"]
                if ax == "z" and not (homed and self.z_ok and 0.0 <= pos <= hi):
                    # Z 위치를 모른다(펌웨어 원점 없음 · 믿을 수 없는 카운터): 위쪽 끝단까지 전체 스트로크 + 3 mm 를 느리게 먼저 민다.
                    # 위로 미는 것은 언제나 안전하다(끝단에서 헛돈다). 0 등록은 그 뒤 fz 한 번뿐 - 중간에 끊겨도 틀린 0 이 저장되지 않는다.
                    sp = min(max(300, int(self.slow_pps)), 1200)
                    self.jog("z", -(hi + 3.0), gen=g, internal=True, speed=sp)
                    info["full_stroke"] = True
                elif homed and 0.0 <= pos <= hi:
                    gap = pos - approach_mm
                    if gap > 0.05:
                        self.jog(ax, -gap, gen=g, internal=True)
                        info["approach"] = True
                    info["from_mm"] = round(pos, 1)
                self._enter(g)
                if ax != "z" and self.pre_xy:
                    self.pre_xy()
                # fz 를 쓰기 직전에 그 축의 원점 신뢰를 내린다 - 어떻게 끝나든(시간 초과 · 정지 · 비상정지 · 통신 오류 · 거부)
                # "원점 설정 완료" 를 받은 뒤에만 올린다(fz 가 끊기면 펌웨어는 "원점 있음" 이라고 답하지만 위치 카운터는 틀리다).
                if ax == "z":
                    self.z_ok = False
                else:
                    self.xy_done[ax] = False
                    self.xy_ok = False
                self.moving = True
                try:
                    self._run("원점 잡기", lambda: self.stage.find_zero(ax))
                finally:
                    self.moving = False
                    self.last_move_end = time.monotonic()
            self._enter(g)
            if ax == "z":
                self.z_ok = True
            else:
                self.xy_done[ax] = True
                self.xy_ok = bool(self.xy_done["x"] and self.xy_done["y"])
            if self.z_ok and self.xy_ok:
                self.estop_locked = False
                self.rebooted = False
                self.stage.reset_abort()
            else:
                self.stage._aborted = True                  # 나머지 축이 끝날 때까지 절대 이동은 잠근다
            info["status"] = self.stage.status()
            return info

    # ---- 접촉 높이 -----------------------------------------------------
    def descend_contact(self, z_mm, approach_mm, gen=None, check_xy=None, nowait=False):
        """저장한 접촉 높이(절대 Z mm)까지 내린다. approach_mm 앞까지는 보통 속도, 남은 거리는 느린 속도(slow_pps).
        check_xy(status) 가 MotionError 를 던지면 내리지 않는다(X · Y 가 샘플 위일 때만)."""
        g = self.gen if gen is None else gen
        zmax = stage_mod.limits_mm()["z_max_mm"]
        if not isinstance(z_mm, (int, float)) or z_mm != z_mm or not (0.0 < z_mm <= zmax):
            raise MotionError("접촉 높이 오류 · Z 0~%g 밖" % zmax, "range")
        with self._locked(nowait):
            self._enter(g)
            self._need_origin(xy=True)
            with self.op(g):
                st = self.stage.status()
                if check_xy is not None:
                    check_xy(st)
                cur = float(st.get("z_mm") or 0.0)
                pre = max(0.0, float(z_mm) - max(0.0, float(approach_mm)))
                self.moving = True
                try:
                    if cur < pre - 0.01:
                        self._run("Z 접근", lambda: self.stage.move_z_mm(pre, deadline_s=self.deadline_s(abs(pre - cur), stage_mod.Z_PPMM)))
                        cur = pre
                    slow = max(300, int(self.slow_pps))
                    dist = abs(float(z_mm) - cur)
                    t = dist * stage_mod.Z_PPMM / slow + 2.0 * slow / max(1.0, float(self.accel))
                    self._run("Z 접촉 높이", lambda: self.stage.move_z_mm(float(z_mm), speed=slow, deadline_s=t * DEADLINE_MARGIN + DEADLINE_EXTRA_S))
                finally:
                    self.moving = False
                    self.last_move_end = time.monotonic()
            return self.stage.status()

    def apply_position(self, x, y, expect_gen=None, expect_xy=None, tol_mm=0.5, gen=None):
        """위치 확인 결과 적용(sp). 회복 3~4단계.

        expect_gen: 촬영을 시작할 때의 세대 - 그 사이 비상정지가 있었으면 거절.
        expect_xy: 촬영 전 펌웨어 위치 (x, y) - 그 사이 위치가 바뀌었으면 거절.
        """
        g = self.gen if gen is None else gen
        with self.lock:
            self._enter(g)
            if expect_gen is not None and expect_gen != self.gen:
                if self.last_estop_gen > expect_gen:
                    raise MotionError("촬영 중 비상정지 · 위치 적용 안 함", "estop")
                raise MotionError("촬영 중 정지 · 위치 적용 안 함", "stop")
            if not self.z_ok:
                raise MotionError("Z 원점 없음 · Z 원점 잡기 먼저", "no_z")
            with self.op(g):
                if expect_xy is not None:
                    st = self.stage.status()
                    if abs(st["x_mm"] - expect_xy[0]) > tol_mm or abs(st["y_mm"] - expect_xy[1]) > tol_mm:
                        raise MotionError("촬영 사이 위치가 바뀜 · 위치 적용 안 함", "moved")
                self._run("위치 적용", lambda: self.stage.set_position(x, y))
                self._enter(g)                                    # sp 뒤 · 플래그 변경 직전 세대 재확인
                self.xy_ok = True
                self.xy_done = {"x": True, "y": True}
                if self.z_ok:
                    self.stage.reset_abort()
                    self.estop_locked = False
                    self.rebooted = False
            return self.stage.status()


def _short(e, n=58):
    t = str(e).replace("\n", " ").strip()
    return t if len(t) <= n else t[:n - 1] + "…"
