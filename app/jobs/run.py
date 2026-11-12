"""run.py - 측정. 고른 샘플을 순서대로: 이동 → 접촉 → 냉각(시작 전) → 가열 → 냉각 → 저장.

샘플 하나의 순서
  1. 이동  : 히트건이 꺼졌는지 확인 → 핀 맨 위(Z 0, st 로 확인) → 샘플 가운데로 X · Y.
  2. 접촉  : 저장한 접촉 높이 − 0.3 mm 까지 내린 뒤 전기로 확인하며 내린다. 실패(안 닿음 · 금속 판 · 4단자)면 핀을 올리고 이 샘플은 "실패", 다음 샘플로.
  3. 냉각  : 쿨러를 켠다. 온도가 시작 온도보다 높으면 내려갈 때까지 기다린다(기록하지 않는다).
  4. 가열  : K2450 출력을 켜고 기록 시작. 히트건을 **한 번** 켜고, 가열 끝 온도에 닿으면 **한 번** 끈다(그 사이에 다시 켜거나 끄지 않는다 - 자주 켜고 끄면 연기가 난다).
  5. 냉각  : 냉각 끝 온도 아래로 내려갈 때까지 기록한다(히트건을 끈 뒤 더 오르는 구간도 그대로 기록).
  6. 반복이 2회 이상이면 4~5 를 되풀이.
  7. 저장  : K2450 출력 끔 → 쿨러 끔 → 핀 맨 위 → 파일 닫기 → NAS 복사.
읽기: 읽는 간격마다 온도(K2110) 한 번 · 저항(K2450) 한 번. 그 온도를 안전 감시에 그대로 넘긴다(온도를 따로 두 번 읽지 않는다).
어떤 끝(끝남 · 실패 · 건너뛰기 · 중지 · 비상정지 · 오류)에서도: 히트건 끔 → K2450 출력 끔 → 쿨러 끔 → (비상정지가 아니면) 핀 맨 위. 받은 데이터는 파일로 남는다.
점 데이터는 state 에 넣지 않는다(화면은 points() 로 새 점만 받아 간다).
"""

import csv
import json
import os
import re
import threading
import time
from pathlib import Path

from app import contact as contact_mod
from app.devices import meters
from app.jobs import analysis, files
from app.jobs.heater import RelayHeater
from app.safety import Rejected
from app.state import ALARM, NOTICE

STAGES = ("이동", "접촉", "냉각", "가열", "냉각", "저장")
WAIT, NOW, DONE, FAIL, SKIPPED, STOPPED, OUT = "대기", "측정 중", "완료", "실패", "건너뜀", "중지", "제외"
T_MISS_N = 3                                      # 온도를 이만큼 연속으로 못 읽으면 측정을 멈춘다(열전대 끊김)
MAX_POINTS = 200000
COND_KEYS = ("meter_volt", "meter_four_wire", "meter_avg_count", "meter_nplc", "meter_ilim_a", "run_start_c", "run_heater_off_c", "run_cool_end_c",
             "run_repeat", "run_interval_s", "run_cooler_mode", "run_cooler_during_heat", "contact_z_mm", "run_no_rise_s", "run_no_rise_c",
             "run_fast_rise_c_s", "run_stall_s", "run_stall_c", "run_phase_limit_s", "run_open_n", "contact_open_a", "nas_dir", "nas_date_folder",
             "heater_max_c", "heater_max_on_s", "plate_size_mm", "cooler_grid")


class SampleFail(Exception):
    """이 샘플만 실패 - 다음 샘플로 간다."""


class RunAbort(Exception):
    """측정 장비 오류 등 - 측정 전체를 멈추고 경보를 낸다."""


class Halt(Exception):
    """사람이 멈춤: kind = stop(측정 중지) · skip(이 샘플 건너뛰기) · estop(비상정지)."""

    def __init__(self, kind):
        super().__init__(kind)
        self.kind = kind


class Runner:
    def __init__(self, ctrl):
        self.c = ctrl
        self.heater = RelayHeater(ctrl)
        self._lock = threading.RLock()
        self._th = None
        self._busy = False                          # 측정 스레드가 도는 중(active)
        self.state = "idle"                         # idle(측정 전) · running · stopped(멈춤 - 이어서 할 수 있다) · done
        self.items = {}                             # 샘플 id → 진행 기록
        self.order = []                             # 측정 순서(샘플 id)
        self.cur = None
        self.stage, self.frac, self.line, self.cycle = None, 0.0, "", 1
        self.pause_after = False
        self._stop = self._skip = self._estop = False
        self.feeding = False                        # 측정이 온도를 읽어 안전 감시에 넘기는 중(따로 읽지 않는다)
        self.folder = None
        self.cond = {}
        self.batch = ""
        self.t0 = None
        self.nas = None
        self.msg = ""
        self._coolers = []
        self._t_miss = 0

    # ------------------------------------------------------------------
    @property
    def active(self):
        """측정 스레드가 도는 중(마지막 파일 저장 · NAS 복사 · 끝난 상태를 화면에 보내기까지). 이것이 False 가 된 뒤에야 다음 측정 · 손 조작을 받는다."""
        return self._busy

    def gate(self):
        """측정을 시작하려면 필요한 것 중 빠진 것 [{"text", "step"(준비 단계 번호)}]. 빈 판 사진은 없어도 된다."""
        c, st, out = self.c, self.c.state, []
        for key, name in (("stage", "스테이지 연결"), ("k2110", "온도계 연결"), ("k2450", "저항계 연결"), ("camera", "카메라 연결")):
            if st.get("devices", key, "state") != "연결":
                out.append({"text": name, "step": 1})
        if c.safety.locked:
            out.append({"text": "비상정지 해제", "step": None})
        homed = st.get("homed")
        if not all(homed.get(a) for a in "xyz"):
            out.append({"text": "세 축 원점", "step": 2})
        if not c.calib.d.get("plate"):
            out.append({"text": "판 영역", "step": 3})
        if not c.calib.coords_ready():
            out.append({"text": "좌표 보정", "step": 5})
        if c.cfg["contact_z_mm"] is None:
            out.append({"text": "접촉 높이", "step": 6})
        if c.prep.pos_block:
            out.append({"text": "헤드 위치 확인", "step": 2})
        return out

    def reset(self):
        """새 사진으로 샘플을 다시 찾았다: 측정 기록을 비운다(파일은 그대로 남는다)."""
        with self._lock:
            if self.active:
                return
            self.state, self.items, self.order, self.cur, self.stage, self.msg = "idle", {}, [], None, None, ""

    # ------------------------------------------------------------------
    # 명령
    # ------------------------------------------------------------------
    def start(self):
        """[측정 시작]: 고른 샘플 전부를 처음부터. 새 저장 폴더를 만든다."""
        c = self.c
        with self._lock:
            if self.active:
                raise Rejected("이미 측정 중")
            miss = self.gate()
            if miss:
                raise Rejected("필요 항목: " + " · ".join(m["text"] for m in miss))
            used = [s for s in c.prep.samples if s["use"]]
            if not used:
                raise Rejected("측정할 샘플 없음")
            if any(s["xy"] is None for s in used):
                raise Rejected("샘플 위치 계산 실패 · 좌표 보정 확인 필요 (준비 5)")
            names = [s["name"] for s in used]
            if len(set(names)) != len(names):
                raise Rejected("샘플 이름 중복 · 이름 수정 필요")
            self.cond = {k: c.cfg[k] for k in COND_KEYS}
            if float(self.cond["run_heater_off_c"]) >= float(self.cond["heater_max_c"]):
                raise Rejected("가열 끝 온도 %g °C 는 차단 온도 %g °C 미만 필요" % (self.cond["run_heater_off_c"], self.cond["heater_max_c"]))
            self.batch = str(c.cfg["batch_name"])
            self.t0 = time.time()
            lt = time.localtime(self.t0)
            self.folder = c.cfg.data_path() / time.strftime("%Y-%m-%d", lt) / ("%s_%s" % (time.strftime("%H%M%S", lt), self.batch))
            try:
                self.folder.mkdir(parents=True, exist_ok=True)
                jpg = c.prep.jpeg("plate")
                if jpg:
                    (self.folder / "plate.jpg").write_bytes(jpg)
            except OSError as e:
                raise Rejected("저장 폴더 생성 실패 · %s" % str(e)[:50])
            self.items = {s["id"]: self._new_item(s) for s in used}
            self.order = [s["id"] for s in used]
            self._start_nas()
            self._launch()
        return {"folder": str(self.folder), "n": len(self.order)}

    def resume(self):
        """[이어서 측정]: 남은 "대기"부터 같은 폴더에 이어서. 멈춘 뒤 새로 고른 샘플은 맨 뒤에 붙는다."""
        with self._lock:
            if self.active:
                raise Rejected("이미 측정 중")
            if self.state not in ("stopped", "done") or self.folder is None:
                raise Rejected("재개할 측정 없음")
            miss = self.gate()
            if miss:
                raise Rejected("필요 항목: " + " · ".join(m["text"] for m in miss))
            for s in self.c.prep.samples:
                if s["use"] and s["id"] not in self.items and s["xy"] is not None:
                    self.items[s["id"]] = self._new_item(s)
                    self.order.append(s["id"])
            if not any(self.items[i]["status"] == WAIT for i in self.order):
                raise Rejected("남은 샘플 없음")
            if self.nas is None or not self.nas.is_alive():
                self._start_nas(keep=True)
            self._launch()
        return {"n": sum(1 for i in self.order if self.items[i]["status"] == WAIT)}

    def stop(self):
        if not self.active:
            raise Rejected("측정 중 아님")
        self._stop = True
        return {}

    def skip(self):
        if not self.active:
            raise Rejected("측정 중 아님")
        self._skip = True
        return {}

    def set_pause_after(self, on):
        self.pause_after = bool(on)
        self.publish()
        return {"pause_after": self.pause_after}

    def requeue(self, id):
        """끝난(또는 실패 · 건너뜀 · 멈춤) 샘플을 다시 측정 목록에 넣는다. [이어서 측정] 이 다시 잰다.
        새 데이터를 쓰기 직전에 예전 파일을 "이전_<HHMMSS>" 폴더로 옮기고 원래 이름으로 쓴다(_retire_old)."""
        with self._lock:
            if self.active:
                raise Rejected("측정 중 실행 불가")
            it = self.items.get(int(id))
            if it is None or it["status"] in (WAIT, NOW):
                raise Rejected("재측정할 수 없는 샘플")
            it.update(status=WAIT, reason="", notes=[])
            self.state = "stopped"
            self.msg = ""
        self.publish()
        return {}

    def on_estop(self):
        self._estop = True

    def points(self, id, frm=0):
        """샘플의 점 [온도, 저항, 단계(0 가열 · 1 냉각)] 중 frm 번째부터. 화면이 새 점만 받아 간다."""
        it = self.items.get(int(id))
        if it is None:
            return {"n": 0, "points": []}
        pts = it["points"]
        frm = max(0, int(frm))
        return {"n": len(pts), "points": pts[frm:]}

    # ------------------------------------------------------------------
    def _new_item(self, s):
        return {"id": s["id"], "name": s["name"], "status": WAIT, "reason": "", "notes": [], "plate": list(s["plate"]), "xy": list(s["xy"]),
                "t0": None, "t1": None, "points": [], "cycles": [], "contact": None, "files": [], "sum": None, "peak": None}

    def _start_nas(self, keep=False):
        """NAS 복사 스레드를 띄운다. keep = 같은 기록을 이어서 한다: 앞 복사기가 NAS 에 쓴 이름을 이어받아(다시 잰 파일은 같은 이름으로 덮어쓴다)
        지난번에 못 쓴 파일부터 다시 보낸다."""
        c = self.c
        nas_dir = files.nas_target(self.cond.get("nas_dir"), self.cond.get("nas_date_folder"), self.batch, self.t0)
        old = self.nas if keep else None
        self.nas = None
        if nas_dir:
            self.nas = files.NasCopier(nas_dir, lambda t: c.state.alarm("nas", t, NOTICE, dismiss=True), lambda t: c.logs.app(t, "warn"))
            if old is not None and old.nas_dir == nas_dir:
                self.nas.names, self.nas.done, self.nas.pending, self.nas.ok = dict(old.names), list(old.done), list(old.pending), old.ok
                for local in old.pending:
                    self.nas.put(local)
            self.nas.start()

    def _launch(self):
        self._stop = self._skip = self._estop = False
        self.msg = ""
        self._busy = True
        self.state = "running"
        self.c.state.clear_alarm("run")
        self._th = threading.Thread(target=self._main, daemon=True, name="run")
        self._th.start()
        self.publish()

    def join(self, timeout=None):
        t = self._th
        if t is not None:
            t.join(timeout)

    # ------------------------------------------------------------------
    # 측정 스레드
    # ------------------------------------------------------------------
    def _main(self):
        c = self.c
        g = c.safety.generation
        c.logs.app("측정 시작 · 묶음 %s · %d개 · %s" % (self.batch, sum(1 for i in self.order if self.items[i]["status"] == WAIT), self.folder))
        halted = False                                              # 중지 · 비상정지 · 오류로 멈췄다(남은 샘플이 없어도 "끝남"이 아니다)
        try:
            for sid in list(self.order):
                it = self.items[sid]
                if it["status"] != WAIT:
                    continue
                if self._stop:
                    raise Halt("stop")
                self._one(it, g)
                if self.pause_after and any(self.items[i]["status"] == WAIT for i in self.order):
                    self.msg = "샘플 완료 후 정지"
                    break
        except Halt as h:
            halted = True
            self.msg = {"stop": "측정 중지", "estop": "비상정지로 중지"}.get(h.kind, "")
        except RunAbort as e:
            halted = True
            self.msg = "측정 중지 · %s" % e
            c.state.alarm("run", "측정 중지 · %s" % e, ALARM, dismiss=True)
            c.logs.app("측정 멈춤(장비 오류) · %s" % e, "error")
        except Exception as e:                                      # noqa: BLE001 - 예상하지 못한 오류도 "끝남"으로 두지 않는다
            halted = True
            self.msg = "측정 중지 · 오류"
            c.state.alarm("run", "측정 중지 · 오류 · %s: %s" % (type(e).__name__, str(e)[:60]), ALARM, dismiss=True)
            c.logs.app("측정 멈춤(예상 못 한 오류) · %s: %s" % (type(e).__name__, e), "error")
        finally:
            self.feeding = False
            self.cur, self.stage = None, None
            left = any(self.items[i]["status"] == WAIT for i in self.order)
            final = "stopped" if left or halted else "done"
            self.pause_after = False
            self.line = "저장 중"
            self.publish()
            try:
                self._save_json(final)
                self._save_summary()
                if self.nas is not None:
                    self.nas.finish()
                    self.nas.join(timeout=30)
                    self._save_json(final)
                n = self.counts()
                c.logs.app("측정 %s · 완료 %d · 실패 %d · 남음 %d" % ("완료" if final == "done" else "중지", n[DONE], n[FAIL], n[WAIT]))
            finally:
                # 순서: 파일 저장 · NAS 복사 → 끝난 상태를 화면에 보냄 → "측정 중이 아님". 그 사이에 새 측정이 시작돼 같은 기록에 섞여 쓰지 않게 한다.
                self.state = final
                self.publish()
                self._busy = False

    def _one(self, it, g):
        c = self.c
        it.update(status=NOW, reason="", notes=[], t0=time.time(), t1=None, points=[], cycles=[], contact=None, sum=None, peak=None)
        self.cur, self.cycle, self._t_miss = it["id"], 1, 0
        halt = abort = None
        try:
            try:
                self._measure(it, g)
                it["status"] = DONE
            except Rejected as e:                                   # 안전 규칙 · 스테이지가 거절했다
                if not c.safety.gen_ok(g) or c.safety.locked or self._estop:
                    raise Halt("estop")
                raise SampleFail(str(e))
        except SampleFail as e:
            it.update(status=FAIL, reason=str(e))
            c.logs.app("샘플 %s 실패 · %s" % (it["name"], e), "warn")
        except Halt as h:
            it.update(status=SKIPPED if h.kind == "skip" else STOPPED, reason={"skip": "", "stop": "측정 중지", "estop": "비상정지"}[h.kind])
            halt = h
        except RunAbort as e:
            it.update(status=FAIL, reason=str(e))
            abort = e
        except Exception as e:                                      # noqa: BLE001
            it.update(status=FAIL, reason="오류")
            abort = RunAbort("오류 · %s: %s" % (type(e).__name__, str(e)[:60]))
        finally:
            self.feeding = False
            self._cleanup(estop=bool(halt and halt.kind == "estop") or c.safety.locked)
            it["t1"] = time.time()
            self._close_files(it)
            it["sum"] = analysis.analyze(self._first_cycle(it))
            self._save_json()
            self.publish()
        if abort is not None:
            raise abort
        if halt is not None and halt.kind != "skip":
            raise halt

    def _check(self, g):
        c = self.c
        if self._estop or not c.safety.gen_ok(g) or c.safety.locked:
            raise Halt("estop")
        if self._stop:
            raise Halt("stop")
        if self._skip:
            self._skip = False
            raise Halt("skip")

    def _set(self, stage, line, frac=0.0):
        self.stage, self.line, self.frac = stage, line, frac
        self.publish()

    def _measure(self, it, g):
        c, cd = self.c, self.cond
        volt = float(cd["meter_volt"])
        off_c, end_c, start_c = float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"]), float(cd["run_start_c"])
        repeat = int(cd["run_repeat"])
        fname = it["name"] + "_"                                    # LabVIEW 의 File name 은 밑줄까지다
        # ---- 1 이동 ----
        self._check(g)
        self._set(0, "샘플 위로 이동")
        if self.heater.is_on:
            self.heater.off()
        c.goto(it["xy"][0], it["xy"][1])
        # ---- 2 접촉 ----
        self._check(g)
        self._set(1, "접촉 탐색")
        try:
            res = c.contact_descend("height", cd["contact_z_mm"], g)
        except contact_mod.ContactFail as e:
            if e.code == "inst":
                raise RunAbort(str(e))
            raise SampleFail(contact_mod.FAIL_TEXT.get(e.code, str(e)))
        it["contact"] = {k: res[k] for k in ("first_z", "final_z", "r2", "r4")}
        # ---- 3 냉각(시작 전) ----
        self._check(g)
        self._coolers = self._coolers_for(it)
        for n in self._coolers:
            c.relay_on(n)
        self.feeding = True
        T = self._read_temp()
        if T is not None and T > start_c:
            self._set(2, "%g °C 까지 냉각" % start_c)
            self._loop(it, g, phase=None, with_r=False, done=lambda t, now: t <= start_c, stall=True,
                       frac=_frac_down(T, start_c), what="시작 전 냉각")
        dev = c.k2450
        if dev is None or not dev.is_open:
            raise RunAbort("저항계 연결 안 됨")
        for cyc in range(1, repeat + 1):
            self.cycle = cyc
            # ---- 4 가열 ----
            self._check(g)
            self._open_cycle(it, fname, volt, cyc)
            self._set(3, "%g °C 까지 가열" % off_c)                  # 진행 글은 지금 하는 일만 쓴다
            if cyc == 1:
                try:
                    dev.configure(volt, cd["meter_ilim_a"], cd["meter_four_wire"], cd["meter_nplc"], cd["meter_avg_count"])
                    dev.output(True)
                except meters.VisaError as e:
                    raise RunAbort("저항계 설정 오류 · %s" % str(e)[:50])
            if not cd["run_cooler_during_heat"]:
                for n in self._coolers:
                    c.relay_off(n)
            T0 = self._wait_heater_ready(g)
            self._check(g)
            self.heater.on()                                        # 한 번 켠다
            reached = self._heat(it, g, T0 if T0 is not None else start_c, off_c)
            if self.heater.is_on:
                self.heater.off()                                   # 한 번 끈다
            if not reached:
                it["notes"].append("가열 끝 온도 미도달")
            # ---- 5 냉각 ----
            if not cd["run_cooler_during_heat"]:
                for n in self._coolers:
                    c.relay_on(n)
            self._set(4, "%g °C 까지 냉각" % end_c)
            peak = {"t": None}
            self._loop(it, g, phase=1, with_r=True, done=lambda t, now: t < end_c, stall=True, peak=peak,
                       frac=lambda t: _frac_down(peak["t"] if peak["t"] is not None else t, end_c)(t), what="냉각")
            it["peak"] = peak["t"] if it["peak"] is None else max(it["peak"], peak["t"] or -1e9)
            self._close_cycle(it)
        # ---- 7 저장 ---- (출력 끔 · 쿨러 끔 · 핀 맨 위 · 파일 닫기 · NAS 복사는 _one 의 정리가 한다)
        self._set(5, "저장 중", 0.5)

    # ---- 읽기 ----
    def _read_temp(self):
        c = self.c
        dev = c.k2110
        if dev is None or not dev.is_open:
            raise RunAbort("온도계 연결 안 됨")
        try:
            T = dev.read_temp()
        except meters.VisaError as e:
            c.state.set_device("k2110", "오류", str(e), dev.addr)
            raise RunAbort("온도계 오류 · %s" % str(e)[:50])
        c.feed_temperature(T)                                       # 이 값을 안전 감시에 그대로 넘긴다
        if T is None:
            self._t_miss += 1
            if self._t_miss >= T_MISS_N:
                raise RunAbort("온도 측정 실패 · 열전대 확인 필요")
        else:
            self._t_miss = 0
        return T

    def _wait_heater_ready(self, g):
        """히트건을 끈 지 얼마 안 됐으면(앞 샘플을 건너뛰거나 멈춘 직후) 다시 켤 수 있을 때까지 기다린다. 마지막으로 읽은 온도를 돌려준다."""
        c = self.c
        line = self.line
        while True:
            T = self._read_temp()
            off_t = c.safety.heater_off_t
            wait = 0.0 if off_t is None else float(c.cfg["heater_min_off_s"]) - (c.clock() - off_t)
            if wait <= 0:
                self.line = line
                return T
            self._check(g)
            self.line = "히트건 대기 %d초" % (int(wait) + 1)
            self.publish()
            c.sleep(min(float(self.cond["run_interval_s"]), wait))

    def _read_r(self):
        c = self.c
        dev = c.k2450
        try:
            cur, trip = dev.read_step()
        except meters.VisaError as e:
            c.state.set_device("k2450", "오류", str(e), dev.addr)
            raise RunAbort("저항계 오류 · %s" % str(e)[:50])
        volt = float(self.cond["meter_volt"])
        R = meters.resistance(volt, cur)
        c.state.update("resistance", ohm=R, current=cur, volt=volt, four_wire=bool(self.cond["meter_four_wire"]), time=time.time())
        return cur, R, trip

    def _row(self, it, T, R, phase):
        line = files.data_line(T, R) + files.CRLF
        try:
            for f in (self._fc, self._fm):
                f.write(line)
                f.flush()
        except OSError as e:
            raise RunAbort("파일 쓰기 실패 · %s" % str(e)[:50])
        if len(it["points"]) < MAX_POINTS:
            it["points"].append([float(files.fmt_e(T)), float(files.fmt_e(R)), phase])    # 파일에 쓴 값 그대로 - 측정 때 낸 요약 · 그래프가 파일에서 다시 낸 것과 똑같다
        cy = it["cycles"][-1]
        cy["rows"] += 1
        if phase == 0:
            cy["heat_rows"] += 1

    def _note_reading(self, it, cur, trip, st):
        """전류가 하한 아래인 읽기가 이어지면 주의(측정은 계속) · 전류 한도 걸림은 한 번 알린다."""
        c, cd = self.c, self.cond
        if cur is not None:
            st["low"] = st["low"] + 1 if abs(cur) < float(cd["contact_open_a"]) else 0
            if st["low"] >= int(cd["run_open_n"]) and not st["open_warned"]:
                st["open_warned"] = True
                it["notes"].append("핀 접촉 불량 의심")
                c.state.alarm("pin_open", "%s · 핀 접촉 불량 의심" % it["name"], NOTICE, dismiss=True)
        if trip and not st["trip_warned"]:
            st["trip_warned"] = True
            it["notes"].append("전류 한도 도달")
            c.state.alarm("ilim", "%s · 전류 한도 도달" % it["name"], NOTICE, dismiss=True)

    def _heat(self, it, g, T_start, off_c):
        """가열: 히트건을 켠 채 끄는 온도에 닿을 때까지 기록한다. 닿으면 True, 안전 규칙(최대 켜짐 시간 등)이 먼저 껐으면 False."""
        c, cd = self.c, self.cond
        interval = float(cd["run_interval_s"])
        no_s, no_c, fast = float(cd["run_no_rise_s"]), float(cd["run_no_rise_c"]), float(cd["run_fast_rise_c_s"])
        t0 = c.clock()
        on_t0 = on_T0 = None
        prev = None
        st = it.setdefault("_st", {"low": 0, "open_warned": False, "trip_warned": False, "fast_warned": False})
        while True:
            self._check(g)
            t_it = c.clock()
            T = self._read_temp()
            cur, R, trip = self._read_r()
            now = c.clock()
            self._note_reading(it, cur, trip, st)
            if T is not None and R is not None and not trip:
                self._row(it, T, R, 0)
            if T is not None:
                if T >= off_c:
                    return True
                if not self.heater.is_on:                           # 안전 규칙이 히트건을 껐다(최대 켜짐 시간 · 차단 온도)
                    c.logs.app("샘플 %s · 히트건이 %g °C 에 닿기 전에 꺼짐 · %s" % (it["name"], off_c, c.heater_trip or ""), "warn")
                    return False
                if prev is not None and now - prev[0] > 0.05 and (T - prev[1]) / (now - prev[0]) > fast and not st["fast_warned"]:
                    st["fast_warned"] = True
                    c.state.alarm("fast_rise", "온도 급상승 · 열전대 확인 필요", NOTICE, dismiss=True)
                prev = (now, T)
                if on_t0 is None:
                    on_t0, on_T0 = now, T
                elif T - on_T0 >= no_c:
                    on_t0, on_T0 = now, T
                elif now - on_t0 >= no_s:                           # 켜 놓았는데 온도가 안 오른다
                    self.heater.off()
                    raise SampleFail("온도 상승 없음")
                self.frac = max(0.0, min(1.0, (T - T_start) / max(1e-6, off_c - T_start)))
            elif not self.heater.is_on:
                return False
            if now - t0 > float(cd["run_phase_limit_s"]):
                self.heater.off()
                raise SampleFail("가열 %g초 초과" % cd["run_phase_limit_s"])
            self.publish()
            c.sleep(max(0.0, interval - (c.clock() - t_it)))

    def _loop(self, it, g, phase, with_r, done, stall, frac, what, peak=None):
        """냉각(시작 전 · 가열 뒤): done(온도, 지금) 이 참이 되거나, 한동안 더 안 내려가면(방 온도에 닿음) 정상으로 끝낸다."""
        c, cd = self.c, self.cond
        interval = float(cd["run_interval_s"])
        span, step = float(cd["run_stall_s"]), float(cd["run_stall_c"])
        t0 = c.clock()
        best = best_t = None
        st = it.setdefault("_st", {"low": 0, "open_warned": False, "trip_warned": False, "fast_warned": False})
        while True:
            self._check(g)
            t_it = c.clock()
            T = self._read_temp()
            cur = R = None
            trip = False
            if with_r:
                cur, R, trip = self._read_r()
                self._note_reading(it, cur, trip, st)
            now = c.clock()
            if T is not None:
                if peak is not None and (peak["t"] is None or T > peak["t"]):
                    peak["t"] = T
                if with_r and R is not None and not trip:
                    self._row(it, T, R, phase)
                if done(T, now):
                    return T
                if stall:
                    if best is None or T < best - step:
                        best, best_t = T, now
                    elif now - best_t >= span:                      # 방 온도에 닿아 더 안 내려간다 - 그 단계를 정상으로 끝낸다
                        it["notes"].append("%s %.1f °C 에서 종료" % (what, T))
                        return T
                self.frac = max(0.0, min(1.0, frac(T)))
            if now - t0 > float(cd["run_phase_limit_s"]):
                raise SampleFail("%s %g초 초과" % (what, cd["run_phase_limit_s"]))
            self.publish()
            c.sleep(max(0.0, interval - (c.clock() - t_it)))

    # ---- 쿨러 · 정리 ----
    def _coolers_for(self, it):
        cd = self.cond
        mode = cd["run_cooler_mode"]
        grid = cd["cooler_grid"]
        if mode == "none":
            return []
        if mode == "all":
            return [n for r in grid for n in r]
        cell = float(cd["plate_size_mm"]) / 3.0                      # 샘플 아래 구역만
        col = min(2, max(0, int(it["plate"][0] // cell)))
        row = min(2, max(0, int(it["plate"][1] // cell)))
        return [grid[row][col]]

    def _cleanup(self, estop):
        """어떤 끝에서도: 히트건 끔 → K2450 출력 끔 → 쿨러 끔 → (비상정지가 아니면) 핀 맨 위."""
        c = self.c
        hr = self.heater.relay
        try:
            if c.stage.connected and (self.heater.is_on or c.stage.relays[hr - 1] is not False):
                self.heater.off()
        except Rejected as e:
            c.state.alarm("heater_off_fail", "히트건 끄기 실패 · 전원 직접 차단 필요", ALARM)
            c.logs.app("측정 뒤 히트건 끄기 실패 · %s" % e, "error")
        if c.k2450 is not None and c.k2450.is_open:
            c._k2450_off(c.k2450)
        for n in list(self._coolers):
            try:
                if c.stage.connected and c.stage.relays[n - 1] is not False:
                    c.relay_off(n)
            except Rejected as e:
                c.state.alarm("cooler_off_fail", "쿨러 끄기 실패 · 전원 직접 차단 필요", ALARM)
                c.logs.app("측정 뒤 쿨러 끄기 실패 · %s" % e, "error")
        self._coolers = []
        if not estop and c.stage.connected and not c.safety.locked:
            try:
                if c._mm("z") != 0:
                    c.z_top()
            except Rejected as e:
                c.logs.app("측정 뒤 핀 올리기 실패 · %s" % e, "error")
                c.state.alarm("run_ztop", "핀 상승 실패 · %s" % e, ALARM, dismiss=True)

    # ---- 파일 ----
    _fc = _fm = None

    def _open_cycle(self, it, fname, volt, cyc):
        if self._fm is None:
            self._retire_old(it, fname, volt)                       # 다시 재는 샘플: 예전 파일을 "이전_<HHMMSS>" 로 옮긴다
        try:
            if self._fm is None:
                mp = self.folder / files.merged_name(fname, volt)
                self._fm = open(mp, "w", encoding="ascii", newline="")
                it["files"].append(mp.name)
            cp = self.folder / files.cycle_name(fname, volt, cyc)
            self._fc = open(cp, "w", encoding="ascii", newline="")
            self._fc.write(files.HEADER + files.CRLF)
            self._fc.flush()
        except OSError as e:
            raise RunAbort("파일 생성 실패 · %s" % str(e)[:50])
        it["files"].append(Path(cp).name)
        it["cycles"].append({"file": Path(cp).name, "rows": 0, "heat_rows": 0})
        return Path(cp)

    def _retire_old(self, it, fname, volt):
        """새 데이터를 쓰기 직전: 그 샘플의 예전 파일(.dat · merged)을 같은 기록 폴더의 "이전_<HHMMSS>" 로 옮긴다.
        새 파일은 원래 이름(LabVIEW 규칙 - 뒤에 _1 을 붙이지 않는다)으로 쓴다. 옮긴 것은 run.json 의 샘플 "moved" 에 적는다."""
        pat = re.compile(re.escape(files.file_stem(fname, volt)) + r"(\d+|merged)\.dat$")
        names = [n for n in it["files"] if (self.folder / n).is_file()]
        try:
            names += sorted(p.name for p in self.folder.iterdir() if p.is_file() and pat.match(p.name) and p.name not in names)
        except OSError as e:
            raise RunAbort("저장 폴더 읽기 실패 · %s" % str(e)[:50])
        it["files"] = []
        if not names:
            return
        sub = "이전_" + time.strftime("%H%M%S")
        moved = []
        try:
            (self.folder / sub).mkdir(exist_ok=True)
            for n in names:
                dst, _ch = files.unique_path(str(self.folder / sub / n))
                os.replace(str(self.folder / n), dst)
                moved.append(n)
        except OSError as e:
            self.c.logs.app("샘플 %s · 예전 파일 옮기기 실패 · %s" % (it["name"], e), "error")
            raise SampleFail("예전 파일 이동 실패 · 열린 파일 닫기 필요")
        finally:
            if moved:
                it.setdefault("moved", []).append({"to": sub, "files": moved, "t": time.time()})
                self.c.logs.app("샘플 %s 다시 측정 · 예전 파일 %d개를 %s 로 옮김" % (it["name"], len(moved), sub))

    def _close_cycle(self, it):
        f, self._fc = self._fc, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
            if self.nas is not None:
                self.nas.put(self.folder / it["cycles"][-1]["file"])

    def _close_files(self, it):
        if self._fc is not None:
            self._close_cycle(it)
        f, self._fm = self._fm, None
        if f is not None:
            try:
                f.close()
            except OSError:
                pass
            if self.nas is not None:
                for name in it["files"]:
                    if name.endswith("merged.dat") or "_merged" in name:
                        self.nas.put(self.folder / name)
        it.pop("_st", None)

    @staticmethod
    def _first_cycle(it):
        """요약(전이 온도 등)에 쓰는 점: 첫 회차."""
        if not it["cycles"]:
            return []
        return it["points"][:it["cycles"][0]["rows"]]

    def _json(self, state=None):
        nas = None
        if self.nas is not None:
            nas = {"dir": self.nas.nas_dir, "ok": self.nas.ok, "pending": [Path(p).name for p in self.nas.pending], "done": [Path(p).name for p in self.nas.done],
                   "names": {Path(a).name: os.path.basename(b) for a, b in self.nas.names.items()}}       # PC 파일 이름 → NAS 에 쓴 이름
        elif self.cond.get("nas_dir"):
            nas = {"dir": files.nas_target(self.cond.get("nas_dir"), self.cond.get("nas_date_folder"), self.batch, self.t0), "ok": None, "pending": [], "done": []}
        return {"batch": self.batch, "started": self.t0, "saved": time.time(), "state": state or self.state, "msg": self.msg, "cond": self.cond, "nas": nas,
                "samples": [{k: v for k, v in self.items[i].items() if k not in ("points", "_st")} | {"order": n + 1} for n, i in enumerate(self.order)]}

    def _save_json(self, state=None):
        if self.folder is None:
            return
        try:
            tmp = self.folder / "run.json.tmp"
            tmp.write_text(json.dumps(self._json(state), ensure_ascii=False, indent=1), encoding="utf-8")
            tmp.replace(self.folder / "run.json")
        except OSError as e:
            self.c.logs.app("run.json 저장 실패 · %s" % e, "error")

    def _save_summary(self):
        if self.folder is None:
            return
        try:
            with open(self.folder / "summary.csv", "w", encoding="utf-8-sig", newline="") as f:
                w = csv.writer(f)
                w.writerow(["샘플", "순서", "결과", "사유", "시작", "끝", "걸린 시간(s)", "최고 온도(°C)", "시작 온도 저항(Ω)", "최고 온도 저항(Ω)", "저항 비",
                            "가열 전이 온도(°C)", "냉각 전이 온도(°C)", "차이(°C)", "파일"])
                for n, i in enumerate(self.order):
                    it = self.items[i]
                    s = it.get("sum") or {}
                    ts = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else ""                  # noqa: E731
                    e = lambda v: "" if v is None else "%.6g" % v                                              # noqa: E731
                    w.writerow([it["name"], n + 1, it["status"], " · ".join([it["reason"]] + it["notes"]).strip(" ·"), ts(it["t0"]), ts(it["t1"]),
                                "" if not (it["t0"] and it["t1"]) else "%.0f" % (it["t1"] - it["t0"]), e(s.get("t_max")), e(s.get("r_start")), e(s.get("r_peak")),
                                e(s.get("ratio")), e(s.get("t_heat")), e(s.get("t_cool")), e(s.get("hyst")), " ".join(it["files"])])
        except OSError as e:
            self.c.logs.app("summary.csv 저장 실패 · %s" % e, "error")

    # ------------------------------------------------------------------
    # 화면에 보낼 상태(점 데이터는 넣지 않는다)
    # ------------------------------------------------------------------
    def counts(self):
        n = {k: 0 for k in (WAIT, NOW, DONE, FAIL, SKIPPED, STOPPED)}
        for i in self.order:
            n[self.items[i]["status"]] += 1
        return n

    def view(self):
        c = self.c
        rows = []
        k = 0
        idle = self.state == "idle"
        for s in c.prep.samples:
            it = None if idle else self.items.get(s["id"])
            row = {"id": s["id"], "name": s["name"], "order": None, "status": OUT, "reason": "", "notes": [], "dur": None, "n": 0, "t0": None}
            if it is not None:
                row.update(order=self.order.index(s["id"]) + 1, status=it["status"], reason=it["reason"], notes=list(it["notes"]), n=len(it["points"]), t0=it["t0"],
                           dur=None if not (it["t0"] and it["t1"]) else round(it["t1"] - it["t0"], 1), name=it["name"])
            elif s["use"]:
                k += 1
                row.update(order=(k if idle else None), status=WAIT if idle or self.state != "running" else OUT)
            rows.append(row)
        n = self.counts()
        total = len(self.order)
        summary = ""
        if self.state in ("done", "stopped") and total:
            rest = ["%s %d" % (word, n[key]) for key, word in ((FAIL, "실패"), (SKIPPED, "건너뜀"), (STOPPED, "중지"), (WAIT, "남음")) if n[key]]
            summary = "%d개 중 %d개 완료" % (total, n[DONE]) + (" · " + ", ".join(rest) if rest else "")       # 예: 9개 중 7개 완료 · 실패 1, 남음 1
        rec = None
        if self.folder is not None:
            rec = "%s/%s" % (self.folder.parent.name, self.folder.name)
        return {"state": self.state, "active": self.state == "running", "batch": self.batch or str(c.cfg["batch_name"]), "folder": None if self.folder is None else str(self.folder),
                "record": rec, "cur": self.cur, "stage": self.stage, "frac": round(self.frac, 3), "line": self.line, "cycle": self.cycle,
                "repeat": int(self.cond.get("run_repeat", c.cfg["run_repeat"])), "pause_after": self.pause_after, "rows": rows, "total": total,
                "summary": summary, "msg": self.msg, "gate": [] if self.state == "running" else self.gate(), "volt": float(self.cond.get("meter_volt", c.cfg["meter_volt"]))}

    def publish(self):
        self.c.state.set("run", self.view())


def _frac_down(t_from, t_to):
    span = max(1e-6, float(t_from) - float(t_to))
    return lambda t: (float(t_from) - t) / span
