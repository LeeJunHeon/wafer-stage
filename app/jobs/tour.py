"""tour.py - 수동 제어 탭 [자동 순회]: 히트건 · 쿨러 · 온도계 없이 "샘플 인식 → 이동 → 접촉" 만 차례로 본다.

대상 = 샘플 목록(prep.samples) 중 포함(use)이고 스테이지 X · Y 가 있는 것(표 순서).
샘플마다: 이동(Controller.goto - 핀 맨 위 → st 로 Z 0 확인 → X → Y) → 위치 확인 → 접촉 높이까지 내림(Controller.contact_fixed -
마지막 approach_mm 는 느리게) → tour_dwell_s 머묾 → 다음 샘플(goto 가 핀을 맨 위로 올린다). 이동 · 내리기는 prep.go_and_lower 하나를
[샘플 이동] 과 같이 쓴다. 저항계가 연결돼 있고 contact_fixed_read 면 내린 뒤 저항을 한 번 재서 기록한다(측정 작업과 같이 K2450 사용권을 쥔다) ·
contact_check 가 off 가 아니면 측정과 같은 판정(contact.judge_fixed)을 결과 줄 note 에 "안 닿음 · …" / "금속 판 의심 · …"(순회는 멈추지 않는다).
끝: 완료 · [순회 정지] · 이동 패드 [정지](세대가 바뀜) · 거절된 단계 → 핀 맨 위로 올리고 끝 / 비상정지 → 핀을 움직이지 않고 끝.
순회 중에는 Controller.run_command 가 TOUR_ALLOWED 밖의 명령(손으로 하는 이동 · 릴레이 · 측정 · 설정)을 막는다.
"""

import threading

from app import contact as contact_mod
from app.devices.stage import PPMM
from app.safety import Rejected

USE_TOUR = "자동 순회"                              # K2450 사용권 이름(거절 글 "저항계 사용 중 · 자동 순회")
STEP_MOVE, STEP_DOWN, STEP_DWELL, STEP_UP = "이동", "하강", "접촉 높이", "핀 상승"
DONE, STOPPED, ESTOP = "완료", "정지", "비상정지"


class _Stop(Exception):
    """[순회 정지] · [정지] · 비상정지(세대가 바뀜)."""


class Tour:
    def __init__(self, ctrl):
        self.c = ctrl
        self._lock = threading.Lock()
        self._busy = False
        self._stop = False
        self._th = None
        self._k2450 = False                         # 이번 순회가 K2450 사용권을 쥐었다(저항 기록)
        self.i, self.n, self.name, self.step = 0, 0, "", ""
        self.rows = []                              # [{"name", "x", "y", "z", "ohm", "note"}]
        self.result = ""

    @property
    def active(self):
        """순회 스레드가 도는 중(핀을 맨 위로 올리고 끝난 상태를 화면에 보낼 때까지)."""
        return self._busy

    def targets(self):
        return [s for s in self.c.prep.samples if s["use"] and s["xy"] is not None]

    def block_reason(self):
        """시작할 수 없는 까닭(없으면 "")."""
        c = self.c
        if c.run_active():
            return "측정 중 실행 불가"
        if c.temptest.active:
            return "온도 시험 중 실행 불가"
        if c.safety.locked:
            return "비상정지 상태"
        if not c.stage.connected:
            return "스테이지 연결 안 됨"
        if c._heater_on():
            return "히트건 켜짐 · 끈 뒤 시작"
        miss = [a.upper() for a in "xyz" if not c.stage.homed.get(a)]
        if miss:
            return "%s 원점 필요 (초기 설정 2)" % " · ".join(miss)
        if not c.calib.coords_ready():
            return "좌표 보정 필요 (초기 설정 5)"
        if c.cfg["contact_z_mm"] is None:
            return "접촉 높이 필요 (초기 설정 6)"
        if not self.targets():
            return "순회할 샘플 없음 · 샘플 검출 필요"
        return ""

    def start(self):
        c = self.c
        with c.runner._lock:                                        # 자동 측정 · 온도 시험 시작과 엇갈리지 않게(둘 다 이 잠금 안에서 시작한다)
            with self._lock:
                if self._busy:
                    raise Rejected("이미 순회 중")
                why = self.block_reason()
                if why:
                    raise Rejected(why)
                todo = [{"id": s["id"], "name": s["name"], "xy": [float(s["xy"][0]), float(s["xy"][1])]} for s in self.targets()]
                dev = c.k2450
                read = bool(c.cfg["contact_fixed_read"]) and dev is not None and dev.is_open
                if read:
                    c._k2450_acquire(USE_TOUR, stop_monitor=True)   # 측정 작업과 같이: 연속 측정을 먼저 끄고 끝날 때까지 쥔다
                self._k2450 = read
                self._stop = False
                self.i, self.n, self.name, self.step = 0, len(todo), "", ""
                self.rows = []
                self.result = ""
                self._busy = True
                self._th = threading.Thread(target=self._main, args=(todo, read), daemon=True, name="tour")
                self._th.start()
        self.publish()
        return {"n": len(todo), "read": read}

    def stop(self):
        """[순회 정지]: 남은 단계를 버린다 - 이동 정지와 같은 길(세대를 올리고 '!' · st)로 지금 이동을 끊는다. 스레드가 핀을 맨 위로 올리고 끝낸다."""
        if not self._busy:
            return {"active": False}
        try:                                                        # 세대가 먼저 바뀐다(정지 잠금 안) → 스레드는 정지가 끝난 뒤에 핀을 올린다(_park)
            self.c.motion_stop("순회 정지")
        except Rejected as e:
            self.c.logs.app("순회 정지 · 이동 정지 실패 · %s" % e, "warn")
        finally:
            self._stop = True
        return {"active": True}

    def join(self, timeout=None):
        t = self._th
        if t is not None:
            t.join(timeout)

    # ------------------------------------------------------------------
    def _set_step(self, step):
        self.step = step
        self.publish()

    def _check(self, g):
        if self._stop or not self.c.safety.gen_ok(g) or self.c.safety.locked:
            raise _Stop()

    def _dwell(self, g, seconds):
        """접촉 높이에서 머문다(그 사이 [순회 정지] · [정지] · 비상정지를 본다)."""
        c = self.c
        t_end = c.clock() + max(0.0, float(seconds))
        while True:
            self._check(g)
            left = t_end - c.clock()
            if left <= 0:
                return
            c.sleep(min(0.05, left))

    def _main(self, todo, read):
        c = self.c
        g = c.safety.generation
        cz, dwell = float(c.cfg["contact_z_mm"]), float(c.cfg["tour_dwell_s"])
        c.logs.app("샘플 순회 시작 · %d개 · 접촉 높이 Z %.3f · 머묾 %g s · 저항 %s" % (len(todo), cz, dwell, "기록" if read else "안 잼"))
        result = DONE
        try:
            for k, s in enumerate(todo, 1):
                self._check(g)
                self.i, self.name = k, s["name"]
                row = {"name": s["name"], "x": None, "y": None, "z": None, "ohm": None, "note": ""}
                self.rows.append(row)
                self._set_step(STEP_MOVE)
                try:
                    res = c.prep.go_and_lower(s["xy"][0], s["xy"][1], True, read=read, g=g, on_lower=lambda: self._set_step(STEP_DOWN), name=s["name"])
                except Rejected as e:
                    row["note"] = str(e)
                    raise
                x, y = c._mm("x"), c._mm("y")
                row.update(x=None if x is None else round(x, 3), y=None if y is None else round(y, 3), z=res["z"], ohm=res["ohm"])
                if res.get("four_ok") is False:                     # 4단자 값이 2단자에 비해 너무 작다(로그는 contact_fixed) - 순회는 멈추지 않는다
                    rt = res.get("ratio")
                    row["note"] = "4단자 값 이상 · 2단자의 %s" % ("—" if rt is None else "%.1f %%" % (rt * 100.0))
                if read and c.cfg["contact_check"] != "off":           # 접촉 확인(측정과 같은 판정) - 순회는 멈추지 않고 결과 줄에 적기만
                    j = contact_mod.judge_fixed(res["ohm"], float(c.cfg["contact_max_ohm"]), c.cfg["contact_min_ohm"], tripped=bool(res.get("tripped")))
                    if j is not None:
                        row["note"] = j[1]
                c.logs.app("샘플 순회 · %s · X %s · Y %s · Z %.3f · 저항 %s" % (s["name"], "—" if x is None else "%.3f" % x, "—" if y is None else "%.3f" % y,
                                                                         res["z"], "—" if res["ohm"] is None else "%.6g Ω" % res["ohm"]))
                self._set_step(STEP_DWELL)
                self._dwell(g, dwell)
        except _Stop:
            result = ESTOP if c.safety.locked else STOPPED
        except Rejected as e:
            result = ESTOP if c.safety.locked else STOPPED if (self._stop or not c.safety.gen_ok(g)) else "중단 · %s" % e
        except Exception as e:                                      # noqa: BLE001 - 예상하지 못한 오류도 핀을 올리고 끝낸다
            result = "중단 · 오류 · %s" % type(e).__name__
            c.logs.app("샘플 순회 오류 · %s: %s" % (type(e).__name__, e), "error")
            c.logs.exc("자동 순회")
        finally:
            if self._k2450:
                c._k2450_release(USE_TOUR)
                self._k2450 = False
            if result == ESTOP:
                c.logs.app("샘플 순회 · 비상정지 · 핀 그대로", "warn")
            else:
                self._park()
            self.result = result
            self.step = ""
            c.logs.app("샘플 순회 끝 · %s · %d/%d" % (result, sum(1 for r in self.rows if r["z"] is not None), len(todo)),
                       "info" if result == DONE else "warn")
            self._busy = False
            self.publish()

    def _park(self):
        """핀 맨 위(Z 0). 비상정지 잠금이면 움직이지 않는다 · 실패하면 로그만."""
        c = self.c
        with c._halt_lock:                                          # 이동 정지('!' · st)가 진행 중이면 끝나기를 기다린다 - 그 '!' 가 핀 올리기를 끊지 않게
            pass
        if not c.stage.connected or c.safety.locked or not c.stage.homed.get("z"):
            return
        z = c.stage.pos.get("z")
        if z == 0:
            return
        self._set_step(STEP_UP)
        try:
            c.z_top()
            c.logs.app("샘플 순회 · 핀 맨 위 · Z %.3f → 0 mm" % ((z or 0) / PPMM))
        except Rejected as e:
            c.logs.app("샘플 순회 · 핀 올리기 실패 · %s" % e, "error")

    # ------------------------------------------------------------------
    def view(self):
        return {"active": self._busy, "i": self.i, "n": self.n, "name": self.name, "step": self.step,
                "rows": [dict(r) for r in self.rows], "result": self.result}

    def publish(self):
        self.c.state.set("tour", self.view())
