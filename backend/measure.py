"""measure.py - 측정 실행: 시계 + 히트건 끄기 재시도(Hub) · 측정 한 번(Run) · 결과 저장.

측정 한 번 = 폴더 하나: <runs>/<YYYYMMDD_HHMMSS>_<측정 조건>/ (run.json · samples.csv · plate_fit.jpg).
CSV 는 UTF-8 BOM(엑셀). 파일이 열려 있어 못 쓰면 측정을 멈추지 않고 _1 이름으로 쓰고 경고한다.
결과 판정: 완료 · 오류 · 중지됨 · 측정 제외.
온도 · 계측기 · 접촉 판정은 이 프로그램에 없다(별도 프로그램). 단계는 conditions.py 의 일곱 가지뿐이고, 엔진은 expand() 목록을 그대로 실행한다.
"""

import csv
import os
import threading
import time

import conditions
import logger
import store
from core import motion as motion_mod
from core.power import PowerError, HEAT
from devices import Clock

OK, ERROR, STOPPED, SKIP = "완료", "오류", "중지됨", "측정 제외"
XY_TOL_MM = 0.5                  # 접촉 높이로 내리기 전 X · Y 가 샘플 위라고 보는 허용 오차


class StopRun(Exception):
    pass


class FailStep(Exception):
    pass


class FailLink(Exception):
    def __init__(self):
        super().__init__("통신 끊김 · 릴레이 꺼짐")


# --------------------------------------------------------------------------
class Hub:
    """시계(시뮬레이션에서만 배율)와 히트건 끄기 재시도 스레드. 온도는 없다."""

    def __init__(self, cfg, power, motion, sim):
        self.cfg, self.power, self.motion, self.sim = cfg, power, motion, sim
        self.clock = Clock(float(cfg.get("sim.time_scale")) if sim else 1.0)
        self._th = None
        self._stop = threading.Event()

    def configure(self):
        if self.sim:
            self.clock.set_scale(float(self.cfg.get("sim.time_scale")))

    def start(self):
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="heater-guard", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(2.0)

    def _loop(self):
        """히트건 끄기가 확인되지 않았으면(heater_ok False) 확인될 때까지 다시 보낸다."""
        while not self._stop.wait(0.5):
            hs = self.power.heat_since
            mx = conditions.heater_max_s(self.cfg.values)
            if hs is not None and time.monotonic() - hs > mx + 10.0:       # 독립 감시: 어떤 이유로든 상한 + 여유를 넘겨 켜져 있으면 끈다
                logger.log("히트건 시간 상한 초과 · 강제로 끔", "err")
                try:
                    self.power.heater_off()
                except PowerError:
                    pass
            if not self.power.heater_ok:
                try:
                    self.power.heater_off()
                except PowerError:
                    pass


# --------------------------------------------------------------------------
FAULT = {}                      # 검증 전용: csv_lock(samples.csv 가 열려 있음) · step_exc(대기 단계 예외)


def csv_write(path, header, rows):
    """UTF-8 BOM CSV. 못 쓰면(엑셀이 열고 있음) _1, _2 … 이름으로. 반환: (실제 경로, 경고 문구)."""
    base, ext = os.path.splitext(path)
    cand, n, warn = path, 0, ""
    while True:
        try:
            if FAULT.get("csv_lock") and n == 0 and os.path.basename(path).startswith("samples"):
                raise PermissionError("열려 있음")
            os.makedirs(os.path.dirname(cand), exist_ok=True)
            with open(cand, "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(header)
                w.writerows(rows)
            return cand, warn
        except PermissionError:
            n += 1
            cand = "%s_%d%s" % (base, n, ext)
            warn = "파일이 열려 있음 · %s 로 저장" % os.path.basename(cand)
            if n > 20:
                raise


class Run:
    """측정 한 번. app 은 backend/app.py 의 App."""

    def __init__(self, app, cond, samples, mode="continuous", run_dir=None, prior=None):
        self.app, self.cond, self.mode = app, cond, mode
        self.samples = samples                         # [{no, X, Y, cooler, ...}] 측정할 것만
        self.hub = app.hub
        self.cancel = threading.Event()
        self.pause_ev = threading.Event()
        self.pause_ev.set()                            # set = 진행
        self.paused = False
        self.confirm_ev = threading.Event()
        self.confirm_val = None
        self.waiting = ""                              # 사람 확인 대기 사유
        self.results = dict(prior or {})
        self.cur = None
        self.steps_view = []
        self.t0 = time.time()
        self.t_end = None
        self.state = "ready"                           # ready | running | paused | stopping | done | stopped | error
        self.message = ""
        self.warns = []
        self.flat = conditions.expand(cond["steps"])
        self.run_dir = run_dir or os.path.join(store_runs(), time.strftime("%Y%m%d_%H%M%S_") + _safe(cond["name"]))
        self.stamp = os.path.basename(self.run_dir)
        self.thread = None
        self.sample_times = []
        self.gen = app.motion.gen
        self.link0 = app.link_lost_count
        self.live_text = ""
        self.k = -1                                    # 지금 단계 번호(steps_view 기준)

    # ---- 제어 --------------------------------------------------------
    def start(self):
        self.state = "running"
        os.makedirs(self.run_dir, exist_ok=True)
        self.thread = threading.Thread(target=self._main, name="run", daemon=True)
        self.thread.start()

    def pause(self):
        if self.state == "running":
            self.pause_ev.clear()
            self.paused = True

    def resume(self):
        self.pause_ev.set()
        self.paused = False

    def stop(self):
        self.cancel.set()
        self.pause_ev.set()
        self.confirm_val = "stop"
        self.confirm_ev.set()
        if self.state in ("running", "paused"):
            self.state = "stopping"

    def answer(self, val):
        """확인 대기의 [다음] (val = next) 또는 중지(stop)."""
        if not self.waiting:
            raise ValueError("확인 대기 아님")
        self.confirm_val = val
        self.confirm_ev.set()

    def _cancelled(self):
        return self.cancel.is_set() or self.app.motion.gen != self.gen

    def _wait_if_paused(self):
        while not self.pause_ev.is_set():
            if self._cancelled():
                raise StopRun()
            if self.app.link_lost_count != self.link0:
                raise FailLink()
            time.sleep(0.05)

    def _sleep(self, s, label=None, view=None):
        """대기 s(시계 기준). 남은 시간을 단계 칸에 보여 준다."""
        end = self.hub.clock.now() + s
        while True:
            now = self.hub.clock.now()
            if now >= end:
                break
            self._check()
            if view is not None:
                view["value"] = "남은 %d s" % max(0, int(end - now + 0.999))
            if label:
                self.live_text = "%s · 남은 %d s" % (label, max(0, int(end - now + 0.999)))
            time.sleep(0.03)

    def _check(self):
        if self.app.link_lost_count != self.link0:
            raise FailLink()
        if self._cancelled():
            raise StopRun()
        self._wait_if_paused()

    def _check_heat(self):
        """히트건 단계용 검사: 통신 끊김 · 중지 · 비상정지만 본다(일시정지는 무시 - 일시정지 중에 히트건이 켜진 채 남지 않게)."""
        if self.app.link_lost_count != self.link0:
            raise FailLink()
        if self._cancelled():
            raise StopRun()

    # ---- 본체 --------------------------------------------------------
    def _main(self):
        with self.app.motion.op(self.gen):                 # 측정 스레드 전체가 시작 시점의 비상정지 세대 안에서 돈다(릴레이 쓰기도 가드)
            self._main_inner()

    def _main_inner(self):
        try:
            for s in self.samples:
                if self._cancelled():
                    break
                self.cur = s
                res = self._sample(s)
                self.results[s["no"]] = res
                self._write_results()
            self.state = "stopped" if self._cancelled() else "done"
        except StopRun:
            self.state = "stopped"
        except Exception as e:                              # noqa: BLE001
            logger.log("측정 오류 · " + logger.short(e), "err", raw=repr(e))
            self.state = "error"
            self.message = logger.short(e)
            if self.cur:
                self.results[self.cur["no"]] = self._res(self.cur, ERROR, logger.short(e), self._ctx)
        finally:
            self._finish()

    def _finish(self):
        app = self.app
        self.t_end = time.time()
        bad = self.state in ("error", "stopped") or self._cancelled()
        policy = app.cfg.get("relay.on_error" if self.state == "error" else "relay.on_stop")
        try:
            if bad or policy == "off":
                app.power.all_off(owners=("engine", "cooler", "heat", "tool_heat", "manual"))
            else:
                app.power.all_off(owners=("heat", "tool_heat"))
        except Exception:                                    # noqa: BLE001
            pass
        if app.motion.gen == self.gen and app.motion.z_ok and not app.motion.estop_locked:
            try:
                app.motion.move_z(0.0)
                if self.state != "error" and app.cfg.get("after_run") == "park" and app.motion.xy_ok:
                    px, py = app.cfg.get("park_xy")
                    app.motion.move_xy(px, py, gen=self.gen)
                    app.save_position()                       # 대기 위치에 도착 - 다음 부팅에 원점이 복원되게 EEPROM 에 저장
            except Exception as e:                           # noqa: BLE001
                logger.log("측정 뒤 복귀 실패 · " + logger.short(e), "warn")
        elif app.motion.gen != self.gen:
            self.state = "stopped"
            self.message = "비상정지로 중단"
        self._write_results(final=True)
        app.on_run_end(self)

    # ---- 샘플 하나 -----------------------------------------------------
    _ctx = None

    def _res(self, s, status, reason="", ctx=None):
        c = ctx or {}
        return {"no": s["no"], "X": s["X"], "Y": s["Y"], "cooler": s.get("cooler"), "contact_z": c.get("contact_z"),
                "start": c.get("start"), "end": c.get("end") or (time.strftime("%H:%M:%S") if c.get("start") else None),
                "status": status, "reason": reason}

    def _sample(self, s):
        app = self.app
        ctx = {"contact_z": None, "start": None, "end": None}
        self._ctx = ctx
        if s.get("excluded"):
            return self._res(s, SKIP, "측정 제외", ctx)
        self.steps_view = [{"i": i, "text": conditions.describe(st, app.cfg.values), "state": "wait", "value": ""}
                           for i, st in enumerate(x for _j, x in self.flat)]
        t_s = time.time()
        ctx["start"] = time.strftime("%H:%M:%S")
        status, reason = OK, ""
        try:
            for k, (_orig, st) in enumerate(self.flat):
                self._check()
                self.k = k
                self.steps_view[k]["state"] = "active"
                self._step(st, s, ctx, k)
                self.steps_view[k]["state"] = "done"
                self.steps_view[k]["value"] = self.steps_view[k]["value"] if self.steps_view[k]["value"].startswith("Z") else ""
        except StopRun:
            ctx["end"] = time.strftime("%H:%M:%S")
            self.results[s["no"]] = self._res(s, STOPPED, "중지됨", ctx)
            self._write_results()
            self.sample_times.append(time.time() - t_s)
            raise
        except FailStep as e:
            status, reason = ERROR, str(e)
            if 0 <= self.k < len(self.steps_view):
                self.steps_view[self.k]["state"] = "error"
        ctx["end"] = time.strftime("%H:%M:%S")
        self.sample_times.append(time.time() - t_s)
        return self._res(s, status, reason, ctx)

    # ---- 단계 ---------------------------------------------------------
    def _target_xy(self, s):
        tool = conditions.active_tool(self.app.cfg.values)
        return s["X"] - float(tool.get("dx_mm") or 0.0), s["Y"] - float(tool.get("dy_mm") or 0.0)

    def _step(self, st, s, ctx, k):
        t = st["type"]
        app, cfg = self.app, self.app.cfg
        view = self.steps_view[k]
        if FAULT.get("step_exc") and t == "wait":
            raise RuntimeError("영상 예외 흉내")
        if t == "move_sample":
            px, py = self._target_xy(s)
            lx, ly = cfg.get("limits.x_max_mm"), cfg.get("limits.y_max_mm")
            if not (0 <= px <= lx and 0 <= py <= ly):
                raise FailStep("공구 위치 가동범위 밖 (X %.1f · Y %.1f)" % (px, py))
            app.motion.move_xy(px, py, gen=self.gen)
        elif t == "contact":
            z = cfg.get("contact_z_mm")
            if z is None:
                raise FailStep("접촉 높이 필요 · 장비 설정 7")
            px, py = self._target_xy(s)

            def over_sample(stt):
                if abs(stt["x_mm"] - px) > XY_TOL_MM or abs(stt["y_mm"] - py) > XY_TOL_MM:
                    raise motion_mod.MotionError("샘플 위가 아님 · 내리지 않음", "not_over")
            try:
                r = app.motion.descend_contact(float(z), float(cfg.get("approach_mm")), gen=self.gen, check_xy=over_sample)
            except motion_mod.MotionError as e:
                if e.code == "not_over":
                    raise FailStep(str(e))
                raise
            ctx["contact_z"] = round(float(r.get("z_mm") if r.get("z_mm") is not None else z), 2)
            view["value"] = "Z %.2f mm" % ctx["contact_z"]
        elif t == "z_top":
            app.motion.move_z(0.0, gen=self.gen)
        elif t == "wait":
            self._sleep(float(st["s"]), "대기", view)
        elif t == "confirm":
            self._confirm(view)
        elif t == "cooler":
            self._cooler(st, s)
        elif t == "heat":
            self._heat(st, view)
        elif t == "rt":
            self._rt(s, view)
        else:
            raise FailStep("알 수 없는 단계")

    def _rt_dir(self, no):
        """샘플의 R–T 폴더. 다시 측정이면 이전 폴더를 덮지 않고 _2, _3 …"""
        base = os.path.join(self.run_dir, "rt_%02d" % no)
        d, n = base, 1
        while os.path.exists(d):
            n += 1
            d = "%s_%d" % (base, n)
        return d

    def _rt(self, s, view):
        """R–T 측정 한 번(측정 탭의 '이 위치 측정' 과 같은 코드). 핀은 접촉 높이에 있어야 한다. 파일 이름 = File name + 샘플 번호 + _.
        일시정지는 이 단계가 끝난 뒤에 듣는다(히트건 제어를 멈추지 않는다)."""
        import rt as rt_mod
        app = self.app
        why = rt_mod.RTRun.start_block(app, "rt", check_pin=True, fresh=True)
        if why:
            raise FailStep(why)
        if not s.get("cooler_relay"):
            raise FailStep("샘플 구역에 쿨러 없음")
        p = rt_mod.params(app.cfg)
        rr = rt_mod.RTRun(app, p, "%s%s_" % (p["file_name"], s["no"]), run_dir=self._rt_dir(int(s["no"])),
                          cancel_fn=self._cancelled, relays=[int(s["cooler_relay"])], z_top_end=False, gen=self.gen)
        app.rt = rr
        th = threading.Thread(target=rr.run, name="rt-run", daemon=True)
        th.start()
        while not rr.finished.wait(0.2):
            if not th.is_alive():
                break
            lt = "%s %d/%d" % (rr.phase, rr.cycle, int(p["repeat"]))
            if rr.last["T"] is not None:
                lt += " · %.1f °C" % rr.last["T"]
            view["value"] = lt
            self.live_text = "R–T 측정 · " + lt
            if self._cancelled() or self.app.link_lost_count != self.link0:
                rr.stop()
        th.join(2.0)
        view["value"] = ""
        if rr.status == rt_mod.STOPPED or (not rr.status and self._cancelled()):
            raise StopRun()
        if rr.status != rt_mod.OK:
            raise RuntimeError(rr.message or "R–T 측정 오류")      # 안전 사유(온도 · 통신 · 히트건)는 샘플 하나가 아니라 순회 전체를 멈춘다
        s["rt_dir"] = rr.run_dir

    def _confirm(self, view):
        """사람이 [다음] 을 누를 때까지(핀은 샘플에 댄 채). 중지 · 비상정지 · 통신 끊김이면 끝낸다."""
        self.waiting = "확인 대기"
        self.live_text = "확인 대기 · [다음] 을 누르면 이어서"
        view["value"] = "[다음] 대기"
        self.confirm_val = None
        self.confirm_ev.clear()
        try:
            while not self.confirm_ev.wait(0.1):
                if self._cancelled():
                    raise StopRun()
                if self.app.link_lost_count != self.link0:
                    raise FailLink()
            if self.confirm_val == "stop":
                raise StopRun()
        finally:
            self.waiting = ""
        view["value"] = ""

    def _zone_relays(self, s, st):
        cells = self.app.cfg.get("cooler.zones").get("cells", [])
        if st.get("target") == "all":
            return sorted({int(c["relay"]) for c in cells if c.get("relay")})
        z = s.get("cooler_relay")
        return [int(z)] if z else []

    def _cooler(self, st, s):
        relays = self._zone_relays(s, st)
        if st["action"] == "on" and not relays:
            raise FailStep("샘플 구역에 쿨러 없음")
        for n in relays:
            self.app.power.set(n, st["action"] == "on", "cooler")

    def _heat(self, st, view):
        """히트건 n s - 시간이 되면 자동으로 끈다. 상한 heater.max_on_s 를 넘지 못한다. 끝나면(예외 · 취소 포함) 반드시 끄고 RS 로 확인한다."""
        app, hub = self.app, self.hub
        mx = conditions.heater_max_s(app.cfg.values)
        n = min(float(st["s"]), mx)
        t0, real0 = hub.clock.now(), time.monotonic()
        try:
            app.power.set(int(app.cfg.get("heater.relay")), True, HEAT)
            while True:
                self._check_heat()                                # 히트건 중에는 일시정지를 보지 않는다 - 시간이 되면 끄고, 일시정지는 이 단계가 끝난 뒤
                el = hub.clock.now() - t0
                if el >= n:
                    break
                if time.monotonic() - real0 > n + 5.0:       # 시계가 이상해도 실제 시간으로 한 번 더 막는다
                    break
                view["value"] = "남은 %d s" % max(0, int(n - el + 0.999))
                self.live_text = "히트건 · 남은 %d s" % max(0, int(n - el + 0.999))
                time.sleep(0.03)
        finally:
            try:
                app.power.heater_off()
            except PowerError as e:
                logger.log(str(e), "err")
                raise

    # ---- 결과 ---------------------------------------------------------
    def summary(self):
        c = {OK: 0, ERROR: 0, STOPPED: 0, SKIP: 0}
        for r in self.results.values():
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c

    def _write_results(self, final=False):
        hdr = ["번호", "X", "Y", "쿨러", "접촉 Z", "시작", "끝", "결과", "사유"]
        rows = []
        for no in sorted(self.results):
            r = self.results[no]
            rows.append([no, r["X"], r["Y"], r["cooler"], r["contact_z"], r["start"], r["end"], r["status"], r["reason"]])
        try:
            path, warn = csv_write(os.path.join(self.run_dir, "samples.csv"), hdr, rows)
            if warn and warn not in self.warns:
                self.warns.append(warn)
                logger.log(warn, "warn")
        except OSError as e:
            logger.log("결과 저장 실패 · " + logger.short(e), "err")
        meta = {"name": self.stamp, "condition": self.cond, "settings": self.app.cfg.values, "mode": self.mode,
                "ids": self.app.ids(), "shot": self.app.shot_dir_name(), "start": self.t0, "end": self.t_end, "state": self.state,
                "results": self.results, "warns": self.warns, "summary": self.summary(), "message": self.message}
        try:
            store.write_json(os.path.join(self.run_dir, "run.json"), meta)
        except OSError as e:
            logger.log("측정 기록 저장 실패 · " + logger.short(e), "err")

    # ---- 화면용 -------------------------------------------------------
    def view(self):
        done = len([r for r in self.results.values() if r["status"] != STOPPED])
        n = len(self.samples)
        avg = (sum(self.sample_times) / len(self.sample_times)) if self.sample_times else None
        remain = None if avg is None else max(0.0, (n - done) * avg)
        return {"text": self.live_text, "state": self.state, "paused": self.paused, "waiting": self.waiting, "cond": self.cond["name"],
                "n": n, "done": done, "cur": None if self.cur is None else self.cur["no"], "steps": self.steps_view,
                "cur_sample": None if self.cur is None else {"no": self.cur["no"], "X": self.cur["X"], "Y": self.cur["Y"], "cooler": self.cur.get("cooler")},
                "elapsed": round((self.t_end or time.time()) - self.t0, 1), "remain": None if remain is None else round(remain, 0),
                "results": self.results, "message": self.message, "dir": self.run_dir, "warns": self.warns,
                "order": [s["no"] for s in self.samples]}


def store_runs():
    from core import paths
    return paths.RUNS_DIR


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")


__all__ = ["Hub", "Run", "csv_write"]
