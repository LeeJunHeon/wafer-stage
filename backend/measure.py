"""measure.py - 측정 엔진: 장비 허브(온도 읽기 · 과열 보호 · 기록) + 측정 한 번(Run) + 결과 저장.

측정 한 번 = 폴더 하나: <runs>/<YYYYMMDD_HHMMSS>_<측정 조건>/ (run.json · samples.csv · sample_NN.csv · 판 맞춤 주석 사진).
CSV 는 UTF-8 BOM(엑셀). 파일이 열려 있어 못 쓰면 측정을 멈추지 않고 _1 이름으로 쓰고 경고한다.
결과 판정: 완료 · 확인 필요 · 실패 · 측정 제외 · 중지됨 (+ 오류).
엔진은 conditions.expand() 가 펼친 목록을 그대로 실행한다(구간 반복이 검증과 같은 구현).
"""

import csv
import os
import threading
import time

import conditions
import logger
import store
from core import motion as motion_mod
from core import stage as stage_mod
from core.power import PowerError, HEAT
from devices import Clock

OK, CHECK, FAIL, SKIP, STOPPED, ERROR = "완료", "확인 필요", "실패", "측정 제외", "중지됨", "오류"


class StopRun(Exception):
    pass


class SkipSample(Exception):
    pass


# --------------------------------------------------------------------------
class Hub:
    """온도 · 접촉 · 계측기 드라이버와 시계, 온도 읽기 스레드(과열 보호 포함), 기록."""

    def __init__(self, cfg, power, motion, dry):
        self.cfg, self.power, self.motion, self.dry = cfg, power, motion, dry
        self.clock = Clock(float(cfg.get("sim.time_scale")) if dry else 1.0)
        self.temp = self.contact = self.meter = None
        self.latest = (None, None)                    # (가상 시각, °C)
        self.fail_n = 0
        self.trip = ""                                # 과열 보호가 건 사유
        self._th = None
        self._stop = threading.Event()
        self.rec = None                               # 기록 중이면 list
        self.rec_t0 = 0.0
        self.lock = threading.Lock()
        self.extra = {}

    # ---- 드라이버 ----------------------------------------------------
    def configure(self):
        import devices as dv
        cfg = self.cfg
        if self.dry:
            self.clock.set_scale(float(cfg.get("sim.time_scale")))
        sim = {k: cfg.get("sim." + k) for k in ("ambient_c", "heater_c", "cooler_c", "tau_heat_s", "tau_cool_s", "tau_amb_s", "noise_c")}
        cooler_relays = [c.get("relay") for c in (cfg.get("cooler.zones") or {}).get("cells", []) if c.get("relay")]
        heater_n = int(cfg.get("heater.relay"))

        def heater_on():
            return bool(self.power.owners.get(heater_n))

        def cooler_on():
            return any(self.power.owners.get(int(n)) for n in cooler_relays)

        drv = cfg.get("temp.driver")
        self.temp = dv.DummyTemp(self.clock, sim, heater_on, cooler_on) if (drv == "dummy" and self.dry) else (
            dv.NoTemp() if drv in ("none", "dummy") else dv.NoTemp())
        self.temp.is_dummy_fake = drv == "dummy" and not self.dry
        cd = cfg.get("contact.driver")
        z_fn = lambda: (self.motion.stage.status().get("z_mm") if self.motion.stage else None)          # noqa: E731
        self.contact = dv.DummyContact(z_fn, cfg.get("sim.contact_z_mm")) if (cd == "dummy" and self.dry) else None
        md = cfg.get("meter.driver")
        self.meter = dv.DummyMeter(cfg.get("meter.channels"), lambda: self.latest[1]) if (md == "dummy" and self.dry) else dv.NoMeter(cfg.get("meter.channels"))

    def temp_kind(self):
        d = self.cfg.get("temp.driver")
        if d == "none":
            return "none"
        return "dummy" if d == "dummy" else "real"

    def start(self):
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="temp", daemon=True)
        self._th.start()

    def stop(self):
        self._stop.set()
        if self._th:
            self._th.join(2.0)

    def _loop(self):
        heater_n = int(self.cfg.get("heater.relay"))
        while not self._stop.is_set():
            period = max(0.05, float(self.cfg.get("temp.period_s")))
            t = None
            try:
                t = self.temp.read() if self.temp else None
            except Exception:                               # noqa: BLE001
                t = None
            now = self.clock.now()
            on = bool(self.power.owners.get(heater_n))
            if t is None:
                self.fail_n += 1
            else:
                self.fail_n = 0
                self.latest = (now, t)
            # 과열 보호: 레시피와 무관
            if on:
                over = t is not None and t > float(self.cfg.get("heater.max_c"))
                bad = self.fail_n >= int(self.cfg.get("heater.sensor_fail_n")) and self.temp_kind() != "none"
                if over or bad:
                    self.trip = ("과열 %.0f °C" % t) if over else "온도 센서 오류"
                    try:
                        self.power.heater_off()
                    except PowerError:
                        pass
                    logger.log("히트건 끔 · " + self.trip, "err")
            if self.rec is not None:
                self._record(now, t)
            time.sleep(period / max(1.0, self.clock.scale if self.dry else 1.0) if self.dry else period)

    # ---- 기록 --------------------------------------------------------
    def rec_start(self):
        with self.lock:
            self.rec = []
            self.rec_t0 = self.clock.now()

    def rec_stop(self):
        with self.lock:
            r, self.rec = self.rec, None
            return r or []

    def _record(self, now, t):
        heater_n = int(self.cfg.get("heater.relay"))
        row = {"t": round(now - self.rec_t0, 3), "temp": None if t is None else round(t, 2),
               "heater": int(bool(self.power.owners.get(heater_n))),
               "cooler": int(any(o for n, o in self.power.owners.items() if n != heater_n)),
               "z": self._z()}
        row.update(self.extra)
        with self.lock:
            if self.rec is not None:
                self.rec.append(row)

    def _z(self):
        try:
            return self.motion.stage.status().get("z_mm")
        except Exception:                                   # noqa: BLE001
            return None


# --------------------------------------------------------------------------
FAULT = {}                      # 검증 전용: csv_lock(sample_NN.csv 가 열려 있음) · step_exc(측정 단계 예외)


def csv_write(path, header, rows):
    """UTF-8 BOM CSV. 못 쓰면(엑셀이 열고 있음) _1, _2 … 이름으로. 반환: (실제 경로, 경고 문구)."""
    base, ext = os.path.splitext(path)
    cand, n, warn = path, 0, ""
    while True:
        try:
            if FAULT.get("csv_lock") and n == 0 and os.path.basename(path).startswith("sample_"):
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

    def __init__(self, app, cond, samples, mode, run_dir=None, prior=None):
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
        self.est_sample_s = None
        self.sample_times = []
        self.live = {}                                 # 현재 단계 실시간 값
        self.gen = app.motion.gen
        self.csv_names = {}
        self.live_text = ""
        self.bands = []                                # 현재 샘플의 단계 구간 [{name, t0, t1}] (기록 시각 기준)
        self.ctx_cur = None

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
        self.confirm_val = val
        self.confirm_ev.set()

    def _cancelled(self):
        return self.cancel.is_set() or self.app.motion.gen != self.gen

    def _wait_if_paused(self):
        while not self.pause_ev.is_set():
            if self._cancelled():
                raise StopRun()
            time.sleep(0.05)

    def _sleep(self, s):
        end = self.hub.clock.now() + s
        while self.hub.clock.now() < end:
            self._check()
            time.sleep(0.03)

    def _check(self):
        if self._cancelled():
            raise StopRun()
        self._wait_if_paused()
        if self.hub.trip:
            raise StopRun()

    # ---- 본체 --------------------------------------------------------
    def _main(self):
        app = self.app
        try:
            self.hub.trip = ""
            for k, s in enumerate(self.samples):
                if self._cancelled():
                    break
                self.cur = s
                res = self._sample(s)
                self.results[s["no"]] = res
                self._write_results()
                if self.mode == "confirm" and k < len(self.samples) - 1 and not self._cancelled():
                    self.waiting = "다음 샘플 확인"
                    self.confirm_ev.clear()
                    self.state = "paused"
                    self.confirm_ev.wait()
                    self.state = "running"
                    self.waiting = ""
                    v = self.confirm_val
                    if v == "stop":
                        break
                    if v == "redo":
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
                self.results[self.cur["no"]] = self._res(self.cur, ERROR, logger.short(e))
        finally:
            self._finish()

    def _finish(self):
        app = self.app
        self.t_end = time.time()
        self.hub.rec_stop()
        bad = self.state in ("error", "stopped") or self._cancelled()
        # 정책: 히트건 끄기 · Z 맨 위 · 대기 위치
        try:
            app.power.all_off(owners=("engine", "cooler", "heat", "tool_heat") if bad else ("heat", "tool_heat"))
        except Exception:                                    # noqa: BLE001
            pass
        if not bad and not app.cfg.get("cooler.keep_on"):
            try:
                app.power.all_off(owners=("cooler",))
            except Exception:                                # noqa: BLE001
                pass
        if app.motion.gen == self.gen and app.motion.z_ok and not app.motion.estop_locked:
            try:
                app.motion.move_z(0.0)
                if self.state != "error" and app.cfg.get("after_run") == "park" and app.motion.xy_ok:
                    px, py = app.cfg.get("park_xy")
                    app.motion.move_xy(px, py)
            except Exception as e:                           # noqa: BLE001
                logger.log("측정 뒤 복귀 실패 · " + logger.short(e), "warn")
        elif app.motion.gen != self.gen:
            self.state = "stopped"
            self.message = "비상정지로 중단"
        self._write_results(final=True)
        app.on_run_end(self)

    # ---- 샘플 하나 -----------------------------------------------------
    def _res(self, s, status, reason="", ctx=None):
        c = ctx or {}
        return {"no": s["no"], "X": s["X"], "Y": s["Y"], "cooler": s.get("cooler"), "contact_z": c.get("contact_z"),
                "t_max": c.get("t_max"), "heat_s": c.get("heat_s"), "cool_s": c.get("cool_s"), "values": c.get("values") or {},
                "status": status, "reason": reason, "log": c.get("log")}

    def _sample(self, s):
        app = self.app
        ctx = {"contact_z": None, "t_max": None, "heat_s": None, "cool_s": None, "values": {}, "log": None, "reasons": [],
               "rows": [], "heat_t0": None}
        if s.get("excluded"):
            return self._res(s, SKIP, "측정 제외")
        self.steps_view = [{"i": i, "text": conditions.describe(st, app.cfg.values), "state": "wait", "value": ""}
                           for i, st in enumerate(x for _j, x in self.flat)]
        t_s = time.time()
        self.bands = []
        self.ctx_cur = ctx
        status, reason = OK, ""
        try:
            for k, (_orig, st) in enumerate(self.flat):
                self._check()
                self.steps_view[k]["state"] = "active"
                try:
                    self._step(st, s, ctx, k)
                finally:
                    pass
                self.steps_view[k]["state"] = "done"
                if ctx["reasons"]:
                    status = CHECK
        except SkipSample as e:
            status, reason = CHECK, "단계 건너뜀 · " + str(e)
        except StopRun:
            status, reason = STOPPED, "중지됨"
            self._write_sample_csv(s, ctx)
            raise_after = True
            self.sample_done(t_s)
            self.results[s["no"]] = self._res(s, status, reason, ctx)
            self._write_results()
            if raise_after:
                raise
        except FailStep as e:
            status, reason = FAIL, str(e)
        except motion_mod.MotionError as e:
            status, reason = ERROR, logger.short(e)
            raise
        if status == OK and ctx["reasons"]:
            status = CHECK
        if ctx["reasons"]:
            reason = "; ".join(ctx["reasons"])
        if self.hub.rec is not None:
            ctx["rows"] = self.hub.rec_stop()
        self._write_sample_csv(s, ctx)
        self.sample_done(t_s)
        return self._res(s, status, reason, ctx)

    def sample_done(self, t_s):
        self.sample_times.append(time.time() - t_s)

    def _write_sample_csv(self, s, ctx):
        rows = ctx.get("rows") or (self.hub.rec_stop() if self.hub.rec is not None else [])
        if not rows:
            return
        chans = [c["name"] for c in (self.app.cfg.get("meter.channels") or [])]
        hdr = ["시각 s", "온도 °C", "히트건", "쿨러", "Z mm"] + chans
        data = [[r["t"], r["temp"], r["heater"], r["cooler"], r["z"]] + [r.get(c) for c in chans] for r in rows]
        fn = os.path.join(self.run_dir, "sample_%02d.csv" % s["no"])
        if os.path.exists(fn) and s["no"] in self.results:                  # 다시 측정: 이전 기록 보존
            k = 1
            while os.path.exists(os.path.join(self.run_dir, "sample_%02d_retry%d.csv" % (s["no"], k))):
                k += 1
            fn = os.path.join(self.run_dir, "sample_%02d_retry%d.csv" % (s["no"], k))
        path, warn = csv_write(fn, hdr, data)
        ctx["log"] = os.path.basename(path)
        if warn:
            self.warns.append(warn)
            logger.log(warn, "warn")

    # ---- 단계 ---------------------------------------------------------
    def _step(self, st, s, ctx, k):
        t = st["type"]
        app, cfg, hub = self.app, self.app.cfg, self.hub
        view = self.steps_view[k]
        if FAULT.get("step_exc") and t == "measure":
            raise RuntimeError("영상 예외 흉내")
        if t == "move_sample":
            tool = conditions.active_tool(cfg.values)
            px, py = s["X"] - float(tool.get("dx_mm") or 0.0), s["Y"] - float(tool.get("dy_mm") or 0.0)
            lx, ly = cfg.get("limits.x_max_mm"), cfg.get("limits.y_max_mm")
            if not (0 <= px <= lx and 0 <= py <= ly):
                raise FailStep("공구 위치 가동범위 밖 (X %.1f · Y %.1f)" % (px, py))
            app.motion.move_xy(px, py)
            hub.extra = {}
        elif t == "z":
            mm = conditions.z_target_mm(st, cfg.values)
            app.motion.move_z(mm)
            view["value"] = "%.1f mm" % mm
        elif t == "relay":
            app.power.set(int(st["n"]), bool(st["on"]), "engine")
        elif t == "wait":
            self._sleep(float(st["s"]))
        elif t == "confirm":
            self.waiting = "확인 대기"
            self.confirm_ev.clear()
            self.state = "paused"
            while not self.confirm_ev.wait(0.1):
                if self._cancelled():
                    raise StopRun()
            self.state = "running"
            self.waiting = ""
            if self.confirm_val == "stop":
                raise StopRun()
        elif t == "cooler":
            self._cooler(st, s)
        elif t == "contact":
            self._contact(st, ctx, view)
        elif t in ("wait_temp", "cool"):
            self._wait_temp(st, ctx, view, cool=(t == "cool"))
        elif t == "heat":
            self._heat(st, ctx, view)
        elif t == "measure":
            self._measure(st, ctx, view)
        elif t == "log":
            if st["action"] == "start":
                hub.rec_start()
            else:
                ctx["rows"] = hub.rec_stop()
        else:
            raise FailStep("알 수 없는 단계")

    def _zone_relays(self, s, st):
        tg = st.get("target")
        cells = self.app.cfg.get("cooler.zones").get("cells", [])
        if tg == "n":
            return [int(st.get("n"))]
        if tg == "all":
            return [int(c["relay"]) for c in cells if c.get("relay")]
        z = s.get("cooler_relay")
        return [int(z)] if z else []

    def _cooler(self, st, s):
        for n in self._zone_relays(s, st):
            self.app.power.set(n, st["action"] == "on", "cooler")

    def _contact(self, st, ctx, view):
        app, hub = self.app, self.hub
        a, b = conditions.contact_range(st, app.cfg.values)
        step, press, settle = float(st["step_mm"]), float(st["press_mm"]), float(st["settle_s"])
        zmax = float(app.cfg.get("limits.z_max_mm"))
        if a < 0 or b > zmax:
            raise FailStep("접촉 탐색 높이가 가동범위 밖")
        z = a
        while z <= b + 1e-9:
            self._check()
            app.motion.move_z(z)
            self._sleep(settle)
            c = hub.contact.is_contact() if hub.contact else None
            view["value"] = "Z %.2f mm" % z
            if c:
                ctx["contact_z"] = round(z, 3)
                if press > 0:
                    app.motion.move_z(min(z + press, zmax))
                return
            if c is None:
                raise FailStep("접촉 읽기 실패")
            z += step
        pol = st.get("on_fail", "stop")
        if pol == "stop":
            raise FailStep("접촉 실패")
        if pol == "skip":
            raise SkipSample("접촉 못 찾음")
        ctx["reasons"].append("접촉 못 찾음 · 계속")

    def _t(self):
        return self.hub.latest[1]

    def _wait_temp(self, st, ctx, view, cool):
        hub = self.hub
        cond, target = st["cond"], float(st["target_c"])
        hold, mx = float(st.get("hold_s") or 0.0), float(st["max_s"])
        t0 = hub.clock.now()
        ok_since = None
        band = self._band_open("냉각" if cool else "온도 대기")
        while True:
            self._check()
            now = hub.clock.now()
            T = self._t()
            self.live_text = "%s %d s · %g °C %s %g °C 또는 %g s에 종료" % ("냉각" if cool else "온도 대기", now - t0, T if T is not None else float("nan"),
                                                                    "이하" if cond == "<=" else "이상", target, mx)
            if T is not None:
                if ctx["t_max"] is None or T > ctx["t_max"]:
                    ctx["t_max"] = round(T, 2)
                good = (T <= target) if cond == "<=" else (T >= target)
                ok_since = (ok_since or now) if good else None
                view["value"] = "%.1f °C · %d s" % (T, now - t0)
                if good and now - ok_since >= hold:
                    break
            if now - t0 >= mx:
                if cool:
                    ctx["cool_s"] = round(now - t0, 1)
                ctx["reasons"].append(("냉각 시간 초과" if cool else "온도 대기 시간 초과") + " · 계속")
                pol = st.get("on_timeout", "continue")
                if pol == "stop":
                    raise FailStep("온도 대기 시간 초과")
                if pol == "skip":
                    raise SkipSample("시간 초과")
                if pol == "confirm":
                    self.waiting = "시간 초과 · 계속 / 중지"
                    self.confirm_ev.clear()
                    self.state = "paused"
                    while not self.confirm_ev.wait(0.1):
                        if self._cancelled():
                            raise StopRun()
                    self.state = "running"
                    self.waiting = ""
                    if self.confirm_val == "stop":
                        raise StopRun()
                return
            time.sleep(0.05)
        if cool:
            ctx["cool_s"] = round(hub.clock.now() - t0, 1)
        self._band_close(band)

    def _band_open(self, name):
        b = {"name": name, "t0": self._rec_t(), "t1": None}
        self.bands.append(b)
        return b

    def _band_close(self, b):
        b["t1"] = self._rec_t()

    def _rec_t(self):
        h = self.hub
        return round(h.clock.now() - h.rec_t0, 2) if h.rec is not None else None

    def _heat(self, st, ctx, view):
        app, hub = self.app, self.hub
        end, mx = st["end"], float(st["max_s"])
        tgt = float(st.get("target_c") or 0.0)
        ts = float(st.get("time_s") or 0.0)
        app.power.set(int(app.cfg.get("heater.relay")), True, HEAT)
        t0 = hub.clock.now()
        ctx["heat_t0"] = t0
        band = self._band_open("가열")
        try:
            while True:
                self._check()
                now = hub.clock.now()
                T = self._t()
                if T is not None and (ctx["t_max"] is None or T > ctx["t_max"]):
                    ctx["t_max"] = round(T, 2)
                el = now - t0
                view["value"] = "%.1f °C · %d s" % (T if T is not None else float("nan"), el)
                self.live_text = "가열 %d s · %s" % (el, ("%g °C 도달 또는 %g s에 종료" % (tgt, ts if end == "either" else mx)) if end == "either" else
                                                    ("%g °C 도달에 종료" % tgt) if end == "temp" else ("%g s에 종료" % ts))
                if end in ("temp", "either") and T is not None and T >= tgt:
                    break
                if end in ("time", "either") and el >= ts:
                    break
                if el >= mx:
                    ctx["reasons"].append("가열 최대 시간 도달")
                    break
                time.sleep(0.05)
        finally:
            try:
                app.power.heater_off()
            except PowerError as e:
                logger.log(str(e), "err")
        ctx["heat_s"] = round(hub.clock.now() - t0, 1)
        self._band_close(band)

    def _measure(self, st, ctx, view):
        hub = self.hub
        n, iv = int(st["n"]), float(st["interval_s"])
        chans = [c["name"] for c in (self.app.cfg.get("meter.channels") or [])]
        acc = {c: [] for c in chans}
        for i in range(n):
            self._check()
            r = hub.meter.read() if hub.meter else {}
            for c in chans:
                if r.get(c) is not None:
                    acc[c].append(float(r[c]))
            if i < n - 1:
                self._sleep(iv)
        if not any(acc.values()):
            ctx["values"] = {}
            view["value"] = "계측기 미연결"
            return
        ctx["values"] = {c: round(sum(v) / len(v), 4) for c, v in acc.items() if v}
        hub.extra = dict(ctx["values"])
        view["value"] = " · ".join("%s %g" % kv for kv in ctx["values"].items())

    # ---- 결과 ---------------------------------------------------------
    def summary(self):
        c = {OK: 0, CHECK: 0, FAIL: 0, SKIP: 0, STOPPED: 0, ERROR: 0}
        for r in self.results.values():
            c[r["status"]] = c.get(r["status"], 0) + 1
        return c

    def _write_results(self, final=False):
        chans = [c["name"] for c in (self.app.cfg.get("meter.channels") or [])]
        hdr = ["번호", "X", "Y", "쿨러", "접촉 Z", "최고 온도", "가열 s", "냉각 s"] + chans + ["결과", "사유"]
        rows = []
        for no in sorted(self.results):
            r = self.results[no]
            rows.append([no, r["X"], r["Y"], r["cooler"], r["contact_z"], r["t_max"], r["heat_s"], r["cool_s"]]
                        + [r["values"].get(c) for c in chans] + [r["status"], r["reason"]])
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
        series, targets = [], {}
        with self.hub.lock:
            rows = list(self.hub.rec or [])
        if rows:
            step = max(1, len(rows) // 400)
            series = [[r["t"], r["temp"]] for r in rows[::step]]
        for _o, st in self.flat:
            if st["type"] == "heat" and st.get("target_c") is not None:
                targets["heat"] = st["target_c"]
            elif st["type"] == "wait_temp":
                targets["start"] = st["target_c"]
            elif st["type"] == "cool":
                targets["cool"] = st["target_c"]
        return {"series": series, "targets": targets, "bands": list(self.bands), "t_now": self._rec_t(), "text": self.live_text,
                "cur_temp": self.hub.latest[1], "state": self.state, "paused": self.paused, "waiting": self.waiting, "mode": self.mode, "cond": self.cond["name"],
                "n": n, "done": done, "cur": None if self.cur is None else self.cur["no"], "steps": self.steps_view,
                "elapsed": round((self.t_end or time.time()) - self.t0, 1), "remain": None if remain is None else round(remain, 0),
                "results": self.results, "message": self.message, "dir": self.run_dir, "warns": self.warns,
                "order": [s["no"] for s in self.samples]}


class FailStep(Exception):
    pass


def store_runs():
    from core import paths
    return paths.RUNS_DIR


def _safe(name):
    return "".join(c if c.isalnum() or c in "-_ " else "_" for c in name).strip().replace(" ", "_")


__all__ = ["Hub", "Run", "csv_write", "stage_mod"]
