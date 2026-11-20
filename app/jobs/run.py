"""run.py - 측정. 고른 샘플을 순서대로: 이동 → 접촉 → 냉각(시작 전) → 가열 → 냉각 → 저장.

샘플 하나의 순서
  1. 이동  : 히트건(송풍 · 열선)이 꺼질 때까지 기다림 → 핀 맨 위(Z 0, st 로 확인) → 샘플 가운데로 X · Y.
  2. 접촉  : 접촉 방식(contact_mode)에 따라
             고정 높이(기본) - 저장한 접촉 높이까지 정확히 내리고 끝낸다. 내린 뒤 저항을 한 번 재서 기록만 한다(판정 없음 · 실패 없음).
             전기 탐색(선택) - 저장한 접촉 높이 − 0.3 mm 까지 내린 뒤 전기로 확인하며 내린다. 실패(안 닿음 · 금속 판 · 4단자)면 핀을 올리고 이 샘플은 "실패".
  3. 냉각  : 쿨러를 켠다. 온도가 시작 온도보다 높으면 내려갈 때까지 기다린다(기록하지 않는다). 회차마다 가열 전에 같은 조건을 본다.
             정체로 시작 온도에 못 닿으면 가열하지 않고 측정 전체를 멈춘다 - 그 샘플은 "대기" 로 남아 [측정 재개] 로 다시 시작한다.
  4. 가열  : K2450 출력을 켜고 기록 시작. 히트건을 켠다(열선 → 송풍 · 송풍이 켜져 있으면 열선만).
가열 방식(run_heat_mode) "ramp"(기본) - 송풍을 켜 둔 채 열선 출력 %(heater.LineControl - PI + 앞먹임 · 펌웨어 V13 hp)로 목표선(시작 온도 + 가열 속도 × 경과)을 따른다.
                                        가열 끝 온도에 닿으면 유지 시간(run_heat_hold_s) 동안 같은 방식으로 유지한 뒤 끝.
                                "full" - 열선을 계속 켜 두고 가열 끝 온도에서 끈다.
             점마다 목표 온도 · 열선 · 송풍 상태를 <이름>_<전압>V_control.csv 에 남긴다(LabVIEW .dat 는 그대로).
5. 냉각  : 열선 끔. 냉각 방식(run_cool_mode) "ramp"(기본) - 송풍을 켜 둔 채 같은 출력 % 제어로 목표선(가열 끝 순간 온도 − 냉각 속도 × 경과)을 따른다.
                                              "free" - 자연 냉각. 송풍은 run_fan_during_cool 이면 냉각 동안 켜 두고, 아니면 heater_fan_after_s 뒤 끈다.
             냉각 끝 온도 아래로 내려갈 때까지 기록한다(열선을 끈 뒤 더 오르는 구간도 그대로 기록). 정체로 끝나면 "냉각 끝 온도 미도달" 주의.
             목표선(가열 · 냉각)은 온도가 heater_lead_c 넘게 뒤처지면 기다린다(heater.LineControl - 온도 시험과 같은 것).
  6. 반복이 2회 이상이면 4~5 를 되풀이(송풍이 켜져 있으면 열선만 다시 켠다).
  7. 저장  : K2450 출력 끔 → 쿨러 끔 → 핀 맨 위 → 파일 닫기 → NAS 복사.
읽기: 읽는 간격마다 온도(K2110) 한 번 · 저항(K2450) 한 번. 그 온도를 안전 감시에 그대로 넘긴다(온도를 따로 두 번 읽지 않는다).
어떤 끝(끝남 · 실패 · 건너뛰기 · 중지 · 비상정지 · 오류)에서도: 히트건 끔(열선 바로 · 송풍은 열선 끈 뒤 heater_fan_after_s) → K2450 출력 끔 → 쿨러 끔 →
(비상정지가 아니면) 핀 맨 위. 받은 데이터는 파일로 남는다.
점 데이터는 state 에 넣지 않는다(화면은 points() 로 새 점만 받아 간다).
"""

import csv
import json
import os
import re
import threading
import time
from pathlib import Path

from app import config as config_mod
from app import geometry as geo
from app import contact as contact_mod
from app import imgio
from app.devices import meters
from app.jobs import analysis, files
from app.jobs import heater as heater_mod
from app.jobs.heater import LineControl, RelayHeater, Stall
from app.devices.stage import FW_NEED
from app.safety import Rejected
from app.state import ALARM, NOTICE

STAGES = ("이동", "접촉", "냉각", "가열", "냉각", "저장")
WAIT, NOW, DONE, FAIL, SKIPPED, STOPPED, OUT = "대기", "측정 중", "완료", "실패", "건너뜀", "중지", "제외"
T_MISS_N = 3                                      # 온도를 이만큼 연속으로 못 읽으면 측정을 멈춘다(열전대 끊김)
MAX_POINTS = 200000
COND_KEYS = ("meter_volt", "meter_four_wire", "meter_avg_count", "meter_nplc", "meter_ilim_a", "run_start_c", "run_heater_off_c", "run_cool_end_c",
             "run_repeat", "run_interval_s", "run_cooler_mode", "run_cooler_during_heat", "contact_mode", "contact_fixed_read", "contact_z_mm",
             "run_no_rise_s", "run_no_rise_c",
             "run_fast_rise_c_s", "run_stall_s", "run_stall_c", "run_phase_limit_s", "run_open_n", "contact_open_a", "nas_dir", "nas_date_folder",
             "heater_max_c", "heater_max_on_s", "plate_size_mm", "cooler_grid", "cooler_relays", "heater_fan_relay", "heater_element_relay",
             "run_heat_mode", "run_point_step_c", "cool_rate_separate", "run_heat_hold_s",
             "run_fan_during_cool", "heater_start_gap_s", "heater_fan_after_s", "run_cool_mode", "run_cool_rate_c_min", "heater_lead_c",
             "heater_lead_filter_s", "heater_lead_hold_s", "heater_max_pct", "heater_full_pct", "heater_pwm_period_ms", "heater_timing",
             "heat_model_k", "heat_model_tau_s", "heat_model_theta_s", "heat_curve", "heater_tc_s", "heater_pi_filter_s", "heater_jump_c", "heater_turn_s", "reversal_c")
CONTROL_HEADER = ("회차", "단계", "경과(s)", "온도(°C)", "저항(Ω)", "목표 온도(°C)", "열선 출력(%)", "송풍")     # <이름>_<전압>V_control.csv 의 첫 줄


class SampleFail(Exception):
    """이 샘플만 실패 - 다음 샘플로 간다."""


class RunAbort(Exception):
    """측정 장비 오류 등 - 측정 전체를 멈추고 경보를 낸다."""


class StartTempAbort(RunAbort):
    """시작 온도까지 못 식혔다(정체) - 가열하지 않고 측정 전체를 멈춘다. 그 샘플은 "대기" 로 남는다(주위 온도 · 쿨러 문제면 다음 샘플도 같다)."""


START_TEMP_TEXT = "시작 온도 미도달 · 쿨러 · 주위 온도 확인 필요"


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
        self._slow_warned = False                   # 읽기 한 회차가 온도 못 읽음 허용 시간을 넘었다는 경보(측정마다 한 번)
        self.heat = None                            # 가열 중 화면 한 줄: {"sp" 목표 온도, "element", "fan", "mode"}. 가열 중이 아니면 None
        self._t_rec = None                          # 이 샘플 기록을 시작한 시각(clock) - control.csv 의 경과
        self._last_T = None                         # 마지막으로 읽은 온도(냉각 목표선의 시작점 = 가열이 끝난 순간 온도)
        self._T_amb = None                          # 이번 회차 가열 직전 온도(앞먹임의 시작 주위 온도)
        self._pub_lock = threading.RLock()          # state.run 을 쓰는 두 길(publish · refresh_gate)이 서로 앞지르지 않게

    # ------------------------------------------------------------------
    @property
    def active(self):
        """측정 스레드가 도는 중(마지막 파일 저장 · NAS 복사 · 끝난 상태를 화면에 보내기까지). 이것이 False 가 된 뒤에야 다음 측정 · 손 조작을 받는다."""
        return self._busy

    def gate(self):
        """측정을 시작하려면 필요한 것 중 빠진 것 [{"text", "step"(초기 설정 단계 번호)}]. 빈 판 사진은 없어도 된다.
        카메라는 요구하지 않는다 - 측정 · 재개는 카메라를 쓰지 않는다(촬영 · 샘플 검출 · 헤드 위치 확인만 쓴다)."""
        c, st, out = self.c, self.c.state, []
        for key, name in (("stage", "스테이지 연결"), ("k2110", "온도계 연결"), ("k2450", "저항계 연결")):
            if st.get("devices", key, "state") != "연결":
                out.append({"text": name, "step": 1})
        if c.safety.locked:
            out.append({"text": "비상정지 해제", "step": None})
        homed = st.get("homed")
        if not all(homed.get(a) for a in "xyz"):
            out.append({"text": "세 축 원점", "step": 2})
        if not c.calib.d.get("plate"):
            out.append({"text": "판 영역", "step": 3})
        elif not c.calib.plate_ok():                                 # 초점이 바뀌었다(calib.focus_changed): 꼭짓점은 남아 있지만 다시 확정 필요
            out.append({"text": "판 영역 다시 확정", "step": 3})
        if not c.calib.coords_ready():
            out.append({"text": "좌표 보정", "step": 5})
        if c.cfg["contact_z_mm"] is None:
            out.append({"text": "접촉 높이", "step": 6})
        if c.prep.pos_block:
            out.append({"text": "헤드 위치 확인", "step": 2})
        if self.empty_cell_names(c.cfg.values, [s for s in c.prep.samples if s["use"]]):
            out.append({"text": "쿨러 배치 빈 칸", "step": 7})
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
            if c.temptest.active:                                   # 온도 시험이 히트건 · 쿨러를 쓰는 중(온도 시험 시작도 이 잠금 안에서 측정을 본다)
                raise Rejected("온도 시험 중 실행 불가")
            if c.stage.connected and not c.stage.heat_capable:      # 열선 출력 % 제어가 필요하다
                raise Rejected(FW_NEED)
            miss = self.gate()
            if miss:
                raise Rejected("필요 항목: " + " · ".join(m["text"] for m in miss))
            used = [s for s in c.prep.samples if s["use"]]
            if not used:
                raise Rejected("측정할 샘플 없음")
            if any(s["xy"] is None for s in used):
                raise Rejected("샘플 위치 계산 실패 · 좌표 보정 확인 필요 (초기 설정 5)")
            names = [s["name"] for s in used]
            if len(set(names)) != len(names):
                raise Rejected("샘플 이름 중복 · 이름 수정 필요")
            bad = config_mod.problems(c.cfg.values)                 # 설정 조합 검사(저장 때와 같은 함수): 문제가 있으면 시작하지 않는다
            if bad:
                raise Rejected(bad[0])
            c.k2450_begin_run()                                     # 연속 측정을 끄고 K2450 사용권을 얻는다(측정이 끝나면 _main 이 놓는다)
            try:
                self.cond = {k: c.cfg[k] for k in COND_KEYS}
                if c.stage.heat_capable:
                    c.heater_method_restore()                       # 설정의 열선 출력 방식으로(맞추기 · 수동이 바꿨을 수 있다)
                heat_notices(c, self.cond, "run_interval_s", "측정")
                self.batch = str(c.cfg["batch_name"])
                self.t0 = time.time()
                lt = time.localtime(self.t0)
                self.folder = c.cfg.data_path() / time.strftime("%Y-%m-%d", lt) / ("%s_%s" % (time.strftime("%H%M%S", lt), self.batch))
                try:
                    self.folder.mkdir(parents=True, exist_ok=True)
                    jpg = c.prep.jpeg("plate")
                    if jpg:
                        imgio.write_bytes(self.folder / "plate.jpg", jpg)     # 이미지 파일은 imgio 로만(한글 경로 · 원자적 쓰기)
                except (OSError, imgio.ImageIOError) as e:
                    raise Rejected("저장 폴더 생성 실패 · %s" % str(e)[:50])
                self.items = {s["id"]: self._new_item(s) for s in used}
                self.order = [s["id"] for s in used]
                self._start_nas()
                self._launch()
            except BaseException:
                c.k2450_end_run()
                raise
        return {"folder": str(self.folder), "n": len(self.order)}

    def resume(self):
        """[이어서 측정]: 남은 "대기"부터 같은 폴더에 이어서. 멈춘 뒤 새로 고른 샘플은 맨 뒤에 붙는다."""
        with self._lock:
            if self.active:
                raise Rejected("이미 측정 중")
            if self.state not in ("stopped", "done") or self.folder is None:
                raise Rejected("재개할 측정 없음")
            if self.c.temptest.active:
                raise Rejected("온도 시험 중 실행 불가")
            if self.c.stage.connected and not self.c.stage.heat_capable:
                raise Rejected(FW_NEED)
            miss = self.gate()
            if miss:
                raise Rejected("필요 항목: " + " · ".join(m["text"] for m in miss))
            for s in self.c.prep.samples:
                if s["use"] and s["id"] not in self.items and s["xy"] is not None:
                    self.items[s["id"]] = self._new_item(s)
                    self.order.append(s["id"])
            if not any(self.items[i]["status"] == WAIT for i in self.order):
                raise Rejected("남은 샘플 없음")
            if self.empty_cell_names(self.cond, [self.items[i] for i in self.order if self.items[i]["status"] == WAIT]):
                raise Rejected("쿨러 배치 빈 칸 · 초기 설정 7")              # 재개는 이 측정의 조건(cond)의 배치를 쓴다
            bad = config_mod.problems(dict(self.c.cfg.values, **self.cond))    # 이 측정이 쓰는 조건(cond) + 지금의 안전 설정으로 다시 검사한다
            if bad:
                raise Rejected(bad[0])
            self.c.k2450_begin_run()
            try:
                if self.nas is None or not self.nas.is_alive():
                    self._start_nas(keep=True)
                self._launch()
            except BaseException:
                self.c.k2450_end_run()
                raise
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
        self._slow_warned = False
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
                c.save_position("측정 끝")                          # 정리(핀 맨 위)까지 끝난 위치를 저장 - 측정 뒤 PC · 프로그램이 죽어도 원점을 믿을 수 있게(비상정지 잠금이면 건너뜀)
            finally:
                # 순서: 파일 저장 · NAS 복사 → 끝난 상태를 화면에 보냄 → K2450 사용권 놓음 → "측정 중이 아님".
                # 그 사이에 새 측정이 시작돼 같은 기록에 섞여 쓰지 않게 한다.
                self.state = final
                self.publish()
                c.k2450_end_run()
                self._busy = False

    def _one(self, it, g):
        c = self.c
        it.update(status=NOW, reason="", notes=[], t0=time.time(), t1=None, points=[], cycles=[], contact=None, sum=None, peak=None, element_log=[])
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
        except StartTempAbort as e:
            it.update(status=WAIT, reason="시작 온도 미도달")          # 끝나지 않은 채 남긴다 - [측정 재개] 가 이 샘플부터 다시 한다
            c.logs.app("샘플 %s · %s · %.1f °C" % (it["name"], e, self._last_T if self._last_T is not None else float("nan")), "warn")
            abort = e
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
            it["sum"]["curve"] = curve_summary(it.pop("_tl", []), cd_of(self.cond, c.cfg.values))
            it["sum"]["jumps"] = [{"phase": ph, "sp": spi, "d": round(j["d"], 1)} for ph, spi, j in it.pop("_jumps", [])]     # 온도 급변(d = 끝난 뒤의 최대 폭)
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
        self._wait_heater_off(g)                                    # 히트건이 켜진 동안은 X · Y 이동 불가(송풍은 열선 끈 뒤 heater_fan_after_s 에 꺼진다)
        c.goto(it["xy"][0], it["xy"][1])
        # ---- 2 접촉 ----
        self._check(g)
        if cd.get("contact_mode", "fixed") == "search":             # 전기 탐색(선택): 전기로 확인하며 내린다. 실패하면 이 샘플만 실패
            self._set(1, "접촉 탐색")
            try:
                res = c.contact_descend("height", cd["contact_z_mm"], g)
            except contact_mod.ContactFail as e:
                if e.code == "inst":
                    raise RunAbort(str(e))
                raise SampleFail(contact_mod.FAIL_TEXT.get(e.code, str(e)))
            it["contact"] = dict({k: res[k] for k in ("first_z", "final_z", "r2", "r4")}, mode="search")
        else:                                                       # 고정 높이(기본): 저장한 접촉 높이까지만 내린다. 저항은 기록만(판정 없음)
            self._set(1, "접촉 높이로 하강")
            it["contact"] = c.contact_fixed(cd["contact_z_mm"], g, read=bool(cd.get("contact_fixed_read", True)), meter=cd)
        # ---- 3 냉각(시작 전) ----
        self._check(g)
        self._coolers = self._coolers_for(it)
        for n in self._coolers:
            c.relay_on(n)
        self.feeding = True
        dev = c.k2450
        if dev is None or not dev.is_open:
            raise RunAbort("저항계 연결 안 됨")
        for cyc in range(1, repeat + 1):
            self.cycle = cyc
            self._check(g)
            self._precool(it, g, start_c)                           # 회차마다: 시작 온도 이하가 아니면 쿨러로 더 식힌다(정체면 측정 전체를 멈춘다)
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
            T0 = self._read_temp()                                  # 켜기 직전 온도(측정이 켤 때는 방금 읽은 온도가 있어야 한다 · 목표선과 진행률의 시작점)
            self._T_amb = T0 if T0 is not None else start_c         # 앞먹임의 "시작 주위 온도"(가열 · 냉각이 같이 쓴다)
            self._check(g)
            self.heater.on()                                        # 송풍이 꺼져 있으면 열선 → 송풍, 켜져 있으면 열선만(끈 뒤 기다리는 시간은 없다)
            try:
                reached = self._heat(it, g, T0 if T0 is not None else start_c, off_c)
            finally:
                self.heat = None
            if self.heater.element_on:
                self.heater.element(False)                          # 가열 끝: 열선 끔(출력 0)
                self._elog(it, c.clock(), 0)
            ramp_cool = cd.get("run_cool_mode", "free") == "ramp" and reached     # 안전 규칙이 가열을 끊었으면 그 회차는 자연 냉각(열선을 다시 켜지 않는다)
            if not ramp_cool and not cd.get("run_fan_during_cool", True):
                self.heater.off()                                   # 송풍은 열선 끈 뒤 heater_fan_after_s 에 꺼진다
            if not reached:
                it["notes"].append("가열 끝 온도 미도달")
            # ---- 5 냉각 ----
            if not cd["run_cooler_during_heat"]:
                for n in self._coolers:
                    c.relay_on(n)
            self._set(4, "%g °C 까지 냉각" % end_c)
            peak = {"t": None}
            now = c.clock()
            ctl = None
            if ramp_cool:                                           # 냉각 속도 제어: 가열이 끝난 순간 온도에서 냉각 속도로 내려가는 목표선(송풍은 켜 둔 채 · 같은 출력 % 제어)
                s0 = (config_mod.heat_rate_c_min(cd) / 60.0 if cd.get("run_heat_mode", "full") == "ramp" and float(cd.get("run_heat_hold_s", 0.0)) <= 0
                      else 0.0)                                     # 방향 전환: 바로 앞 가열 목표선의 기울기에서
                ctl = LineControl(self.heater, cd, self._last_T if self._last_T is not None else off_c, end_c, -1, now,
                                  config_mod.cool_rate_c_min(cd), T_amb=self._T_amb, on_change=lambda t, pct: self._elog(it, t, pct), slope0=s0)
            _T, stalled = self._loop(it, g, phase=1, with_r=True, done=lambda t, now: t < end_c, stall=True, peak=peak,
                                     frac=lambda t: _frac_down(peak["t"] if peak["t"] is not None else t, end_c)(t), what="냉각", ctl=ctl)
            self.heat = None
            if self.heater.element_on:
                self.heater.element(False)                          # 냉각 끝: 열선 끔(송풍은 정리 · 다음 회차가 정한다)
                self._elog(it, c.clock(), 0)
            if stalled and "냉각 끝 온도 미도달" not in it["notes"]:
                it["notes"].append("냉각 끝 온도 미도달")
            it["peak"] = peak["t"] if it["peak"] is None else max(it["peak"], peak["t"] or -1e9)
            self._close_cycle(it)
        # ---- 7 저장 ---- (출력 끔 · 쿨러 끔 · 핀 맨 위 · 파일 닫기 · NAS 복사는 _one 의 정리가 한다)
        self._set(5, "저장 중", 0.5)

    def _precool(self, it, g, start_c):
        """가열 전: 온도가 시작 온도 이하인지 본다. 아니면 쿨러로 더 식힌다(기록하지 않는다). 정체로 못 닿으면 StartTempAbort(측정 전체를 멈춘다)."""
        T = self._read_temp()
        if T is not None and T <= start_c:
            return
        self._set(2, "%g °C 까지 냉각" % start_c)
        T, stalled = self._loop(it, g, phase=None, with_r=False, done=lambda t, now: t <= start_c, stall=True,
                                frac=_frac_down(T if T is not None else start_c, start_c), what="시작 전 냉각")
        if stalled:
            raise StartTempAbort("%s · 현재 %.1f °C" % (START_TEMP_TEXT, T))

    def _elog(self, it, now, pct):
        """열선 출력을 바꾼 기록(run.json element_log): [경과 s(control.csv 와 같은 기준), 출력 %(0 = 끔), 회차]."""
        it.setdefault("element_log", []).append([round(now - (self._t_rec if self._t_rec is not None else now), 3), round(float(pct), 1), self.cycle])

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
            self._last_T = T
        return T

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

    def _row(self, it, T, R, phase, sp=None):
        """한 점: 회차 .dat · merged(LabVIEW 형식 그대로) + control.csv(경과 · 목표 온도 · 열선 · 송풍 - 가열 제어 값을 맞추는 데 쓴다)."""
        line = files.data_line(T, R) + files.CRLF
        c = self.c
        t_rel = c.clock() - (self._t_rec or c.clock())
        ctl = "%d,%s,%.2f,%s,%s,%s,%.1f,%d\r\n" % (self.cycle, "가열" if phase == 0 else "냉각", t_rel, files.fmt_e(T), files.fmt_e(R),
                                              "" if sp is None else "%.3f" % sp, c.heater_pct(), 1 if c._fan_on() else 0)
        if len(it.setdefault("_tl", [])) < MAX_POINTS:
            it["_tl"].append((round(t_rel, 3), float(T), None if sp is None else float(sp), phase, self.cycle))     # 요약(점 수 · 속도 · 역전 · 목표선 차이)용
        try:
            for f in (self._fc, self._fm):
                f.write(line)
                f.flush()
            if self._fx is not None:
                self._fx.write(ctl)
                self._fx.flush()
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

    def _note_cycle(self, it, t_prev, t_now):
        """실제 한 회차(앞 회차 시작 → 이번 회차 시작: 읽기 + 측정 간격)가 온도 못 읽음 허용 시간을 넘으면 경보 한 번(측정마다).
        이렇게 되면 안전 감시가 히트건을 꺼서 "가열 끝 온도 미도달" 이 된다 - 원인(측정 간격 · 읽기 시간)을 글에 적는다."""
        if t_prev is None or self._slow_warned:
            return
        c = self.c
        dt, stale = t_now - t_prev, float(c.cfg["temp_stale_s"])
        if dt > stale:
            self._slow_warned = True
            it["notes"].append("읽기 회차 지연")
            c.state.alarm("run_slow", "읽기 한 회차 %.1f초 · 허용 %g초 초과 · 측정 간격과 평균 횟수 확인" % (dt, stale), ALARM, dismiss=True)
            c.logs.app("샘플 %s · 읽기 한 회차 %.2f초 · 온도 못 읽음 허용 %g초 초과(측정 간격 %g초 · NPLC %g · 평균 %d회)" % (
                it["name"], dt, stale, self.cond["run_interval_s"], self.cond["meter_nplc"], self.cond["meter_avg_count"]), "warn")

    def _heat(self, it, g, T_start, off_c):
        """가열: 히트건을 켠 뒤(부르는 쪽이 켠다) 가열 끝 온도에 닿을 때까지 기록한다. 닿으면 True, 안전 규칙이 먼저 열선을 껐으면 False.
        ramp - 송풍을 켜 둔 채 열선 출력 % 로 목표선 SP(t) = T_start + 가열 속도 × 경과(상한 off_c)를 따른다(LineControl · PowerControl).
               off_c 에 닿으면 run_heat_hold_s 동안 같은 방식으로 유지한 뒤 끝.
        full - 열선을 계속 켜 두고 off_c 에서 끝(부르는 쪽이 끈다).
        가열 무반응(run_no_rise_s · run_no_rise_c)은 열선이 켜져 있던 시간만 센다. 한 단계 시간 한도(run_phase_limit_s)는 가열 단계 전체를 본다."""
        c, cd = self.c, self.cond
        interval = float(cd["run_interval_s"])
        no_s, no_c, fast = float(cd["run_no_rise_s"]), float(cd["run_no_rise_c"]), float(cd["run_fast_rise_c_s"])
        ramp = cd.get("run_heat_mode", "full") == "ramp"
        hold_s = float(cd.get("run_heat_hold_s", 0.0))
        t0 = c.clock()
        trips0 = c.heater_trips
        if not ramp and int(cd["heater_max_pct"]) < 100:
            self.heater.power(cd["heater_max_pct"])                 # 계속 가열: 최대 출력
        self._elog(it, t0, self.heater.pct)                         # 열선 출력을 바꾼 때 - run.json element_log
        acc = {"on": 0.0, "last": (t0, self.heater.pct)}            # 최대 출력으로 환산한 열선 켜진 시간의 합 · (온도를 읽은 시각, 그때 출력 %)

        def changed(now, pct):                                      # 목표선 제어가 출력을 바꿨다: 그때까지의 출력 × 시간(다음 온도에 나타난다)을 더한다
            acc["on"] += acc["last"][1] / 100.0 * (now - acc["last"][0])
            acc["last"] = (now, pct)
            self._elog(it, now, pct)

        ctl = LineControl(self.heater, cd, T_start, off_c, 1, t0, config_mod.heat_rate_c_min(cd), T_amb=self._T_amb, on_change=changed) if ramp else None
        ref_on = 0.0                                                # 무반응 판정의 기준(그때까지 열선이 켜져 있던 시간)
        ref_T = None
        prev = None
        hold_t0 = None
        st = it.setdefault("_st", {"low": 0, "open_warned": False, "trip_warned": False, "fast_warned": False})
        t_prev = None
        while True:
            self._check(g)
            t_it = c.clock()
            self._note_cycle(it, t_prev, t_it)
            t_prev = t_it
            T = self._read_temp()
            t_T, p_T = c.clock(), self.heater.pct                  # 온도를 읽은 시각 · 그때 열선 출력
            cur, R, trip = self._read_r()
            now = c.clock()
            el_on = self.heater.element_on
            acc["on"] += acc["last"][1] / 100.0 * (t_T - acc["last"][0])     # 켜진 시간은 온도를 읽은 시각까지만 센다 - 그 뒤(저항 읽기 동안)의 가열은
            acc["last"] = (t_T, p_T)                                # 아직 이 온도에 없다(저항 읽기가 길면 오른 것을 못 본 채 시간만 쌓여 "온도 상승 없음" 이 된다)
            on_time = acc["on"]
            if ctl is None:
                sp = float(off_c)
            elif T is not None:
                sp = ctl.target(T, now)                             # 목표선(온도가 heater_lead_c 넘게 아래면 그 자리에서 기다린다)
            else:
                sp = ctl.sp
            self.heat = {"sp": round(sp, 2), "element": el_on, "fan": self.heater.fan_on, "mode": "ramp" if ramp else "full",
                         "wait": bool(ctl is not None and ctl.waiting), "pct": self.heater.pct}
            self._note_reading(it, cur, trip, st)
            if T is not None and R is not None and not trip:
                self._row(it, T, R, 0, sp)
            if c.heater_trips != trips0:                            # 안전 규칙이 열선을 껐다(차단 온도 · 열선 연속 최대 시간 · 온도 못 읽음)
                c.logs.app("샘플 %s · 열선이 %g °C 에 닿기 전에 꺼짐 · %s" % (it["name"], off_c, c.heater_trip or ""), "warn")
                return False
            if T is not None:
                if hold_t0 is None and T >= off_c:
                    if not ramp or hold_s <= 0:
                        return True
                    hold_t0 = now                                   # 가열 끝 온도 도달 → 유지 시작
                if hold_t0 is not None and now - hold_t0 >= hold_s:
                    return True
                if prev is not None and now - prev[0] > 0.05 and (T - prev[1]) / (now - prev[0]) > fast and not st["fast_warned"]:
                    st["fast_warned"] = True
                    c.state.alarm("fast_rise", "온도 급상승 · 열전대 확인 필요", NOTICE, dismiss=True)
                prev = (now, T)
                if hold_t0 is None:                                 # 무반응: 열선이 켜져 있던 시간만 센다(유지 중에는 오르지 않는 것이 맞다)
                    if ramp and ref_T is not None and T < ref_T:
                        ref_T = T                                   # 가열 속도 제어: 열선이 꺼진 동안 식은 만큼은 기준을 낮춘다(끈 동안 내려간 것을 "안 오름" 으로 세지 않게)
                    if ref_T is None or T - ref_T >= no_c:
                        ref_T, ref_on = T, on_time
                    elif on_time - ref_on >= no_s:
                        raise SampleFail("온도 상승 없음")
                if ctl is not None:
                    try:
                        ctl.decide(T, sp, now)                      # 출력 %(PI + 앞먹임) - 바꾸면 changed 가 기록한다
                    except Rejected as e:                           # 안전 규칙이 켜기를 거절했다(온도 못 읽음 · 차단 온도 …) - 비상정지면 _check 가 멈춘다
                        self._check(g)
                        c.logs.app("샘플 %s · 열선 켜기 거절 · %s" % (it["name"], e), "warn")
                        return False
                    self._note_jumps(it, ctl, "가열")
                self.frac = max(0.0, min(1.0, (T - T_start) / max(1e-6, off_c - T_start)))
            if now - t0 > float(cd["run_phase_limit_s"]):
                raise SampleFail("가열 %g초 초과" % cd["run_phase_limit_s"])
            self.publish()
            c.sleep(max(0.0, interval - (c.clock() - t_it)))

    def _loop(self, it, g, phase, with_r, done, stall, frac, what, peak=None, ctl=None):
        """냉각(시작 전 · 가열 뒤): done(온도, 지금) 이 참이 되거나, 한동안 더 안 내려가면(정체) 끝낸다. 반환 (마지막 온도, 정체였나).
        ctl = 냉각 속도 제어(LineControl): 목표선을 정해 점마다 기록하고 열선을 켜고 끈다. 안전 규칙이 열선을 끄거나 켜기를 거절하면
        그 회차는 자연 냉각으로 이어 간다(열선을 다시 켜지 않는다)."""
        c, cd = self.c, self.cond
        interval = float(cd["run_interval_s"])
        stall_j = Stall(cd["run_stall_s"], cd["run_stall_c"])
        trips0 = c.heater_trips
        t0 = c.clock()
        st = it.setdefault("_st", {"low": 0, "open_warned": False, "trip_warned": False, "fast_warned": False})
        t_prev = None
        while True:
            self._check(g)
            t_it = c.clock()
            self._note_cycle(it, t_prev, t_it)
            t_prev = t_it
            T = self._read_temp()
            cur = R = None
            trip = False
            if with_r:
                cur, R, trip = self._read_r()
                self._note_reading(it, cur, trip, st)
            now = c.clock()
            if ctl is not None and c.heater_trips != trips0:        # 안전 규칙이 열선을 껐다 - 자연 냉각으로 이어 간다
                ctl = self._ctl_off(it, "냉각 중 열선 차단 · %s" % (c.heater_trip or ""))
            if T is not None:
                sp = ctl.target(T, now) if ctl is not None else None
                if ctl is not None:
                    self.heat = {"sp": round(sp, 2), "element": self.heater.element_on, "fan": self.heater.fan_on, "mode": "ramp", "wait": ctl.waiting,
                                 "pct": self.heater.pct}
                if peak is not None and (peak["t"] is None or T > peak["t"]):
                    peak["t"] = T
                if with_r and R is not None and not trip:
                    self._row(it, T, R, phase, sp)
                if done(T, now):
                    return T, False
                if stall and stall_j.update(T, now):                # 정체: 더 안 내려간다 - 그 단계를 끝낸다(부르는 쪽이 판단)
                    return T, True
                if ctl is not None:
                    try:
                        ctl.decide(T, sp, now)
                    except Rejected as e:
                        self._check(g)
                        ctl = self._ctl_off(it, "냉각 중 열선 켜기 거절 · %s" % e)
                    else:
                        self._note_jumps(it, ctl, "냉각")
                self.frac = max(0.0, min(1.0, frac(T)))
            if now - t0 > float(cd["run_phase_limit_s"]):
                raise SampleFail("%s %g초 초과" % (what, cd["run_phase_limit_s"]))
            self.publish()
            c.sleep(max(0.0, interval - (c.clock() - t_it)))

    def _note_jumps(self, it, ctl, phase):
        """제어기에 새로 생긴 온도 급변: 샘플 메모(같은 글은 한 번) · 로그 · 요약용으로 모음(it["_jumps"]). 측정은 멈추지 않는다."""
        seen = it.setdefault("_jumps", [])
        known = {id(j) for _ph, _spi, j in seen}
        for j in ctl.jumps:
            if id(j) in known:
                continue
            spi = int(round(j["sp"]))                               # 목표 온도 정수는 여기서 한 번만(메모 · run.json 이 같은 수)
            seen.append((phase, spi, j))
            txt = "온도 급변 · %s %d °C" % (phase, spi)
            if txt not in it["notes"]:
                it["notes"].append(txt)
            self.c.logs.app("샘플 %s · 온도 급변 · %s %.1f °C · 목표선에서 %+.1f °C" % (it["name"], phase, j["sp"], j["d"]), "warn")

    def _ctl_off(self, it, why):
        """냉각 속도 제어를 그만두고 자연 냉각으로(열선은 끈 채). 기록 · 로그 한 줄."""
        c = self.c
        if self.heater.element_on:
            try:
                self.heater.element(False)
                self._elog(it, c.clock(), False)
            except Rejected as e:
                c.logs.app("열선 끄기 실패 · %s" % e, "error")
        it["notes"].append("냉각 중 열선 차단")
        c.logs.app("샘플 %s · %s · 자연 냉각으로 이어 감" % (it["name"], why), "warn")
        self.heat = None
        return None

    # ---- 쿨러 · 정리 ----
    def _wait_heater_off(self, g):
        """히트건(송풍 · 열선)이 모두 꺼질 때까지 기다린다: 열선을 끄고, 송풍은 열선 끈 뒤 heater_fan_after_s 에 감시가 끈다.
        끄기를 기다리는 것도 없는데 켜져 있으면(끄기 실패) 측정을 멈춘다 - 히트건이 켜진 채 X · Y 로 움직이지 않는다."""
        c = self.c
        if not self.heater.is_on:
            return
        self.heater.off()
        interval = float(self.cond["run_interval_s"])
        while self.heater.is_on:
            self._check(g)
            if not self.heater.fan_pending:
                raise RunAbort("히트건 끄기 실패 · 전원 직접 차단 필요")
            left = max(0.0, (c._fan_off_at or c.clock()) - c.clock())
            self._set(0, "송풍 끄는 중 · %.0f초" % left)
            c.check_heater()
            c.sleep(min(interval, max(0.0, left)) if left > 0 else 0.0)

    def _coolers_for(self, it):
        cd = self.cond
        mode = cd["run_cooler_mode"]
        grid = cd["cooler_grid"]
        if mode == "none":
            return []
        if mode == "all":
            return [int(n) for n in cd["cooler_relays"]]
        row, col = geo.cooler_cell(it["plate"], cd["plate_size_mm"])     # 샘플 아래 구역만(빈 칸의 샘플은 시작 · 재개가 거절한다 - empty_cell_names)
        return [grid[row][col]]

    @staticmethod
    def empty_cell_names(v, samples):
        """쿨러 "샘플 아래 구역" 인데 쿨러 배치의 빈 칸에 있는 샘플 이름들. 그 밖의 쿨러 방식이면 []."""
        if v["run_cooler_mode"] != "zone":
            return []
        grid = v["cooler_grid"]
        out = []
        for s in samples:
            r, c = geo.cooler_cell(s["plate"], v["plate_size_mm"])
            if grid[r][c] is None:
                out.append(s["name"])
        return out

    def _cleanup(self, estop):
        """어떤 끝에서도: 히트건 끔(열선 바로 · 송풍은 열선 끈 뒤 heater_fan_after_s) → K2450 출력 끔 → 쿨러 끔 → (비상정지가 아니면) 핀 맨 위.
        송풍이 늦게 꺼지는 것은 기다리지 않는다(Controller 감시가 끈다 - 다음 샘플의 이동이 꺼질 때까지 기다린다)."""
        c = self.c
        self.heat = None
        try:
            if c.stage.connected:
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
    _fc = _fm = _fx = None

    def _open_cycle(self, it, fname, volt, cyc):
        if self._fm is None:
            self._retire_old(it, fname, volt)                       # 다시 재는 샘플: 예전 파일을 "이전_<HHMMSS>" 로 옮긴다
        try:
            if self._fm is None:
                mp = self.folder / files.merged_name(fname, volt)
                self._fm = open(mp, "w", encoding="ascii", newline="")
                it["files"].append(mp.name)
                xp = self.folder / files.control_name(fname, volt)
                self._fx = open(xp, "w", encoding="utf-8-sig", newline="")
                self._fx.write(",".join(CONTROL_HEADER) + "\r\n")
                self._t_rec = self.c.clock()
                it["files"].append(xp.name)
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
        pat = re.compile(re.escape(files.file_stem(fname, volt)) + r"((\d+|merged)\.dat|control\.csv)$")
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
        fx, self._fx = self._fx, None
        if fx is not None:
            try:
                fx.close()
            except OSError:
                pass
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
                "samples": [{k: v for k, v in self.items[i].items() if k not in ("points", "_st", "_tl", "_jumps")} | {"order": n + 1} for n, i in enumerate(self.order)]}

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
                            "가열 전이 온도(°C)", "냉각 전이 온도(°C)", "차이(°C)", "파일", "가열 점 간격(°C)", "냉각 점 간격(°C)",
                            "가열 점 수", "냉각 점 수", "가열 속도(°C/분)", "냉각 속도(°C/분)", "가열 점 간격 흔들림(°C)", "냉각 점 간격 흔들림(°C)",
                            "가열 역전(회)", "냉각 역전(회)", "가열 역전 최대(°C)", "냉각 역전 최대(°C)", "가열 목표선 차이 평균(°C)", "냉각 목표선 차이 평균(°C)",
                            "가열 목표선 차이 최대(°C)", "냉각 목표선 차이 최대(°C)"])
                for n, i in enumerate(self.order):
                    it = self.items[i]
                    s = it.get("sum") or {}
                    ts = lambda t: time.strftime("%H:%M:%S", time.localtime(t)) if t else ""                  # noqa: E731
                    e = lambda v: "" if v is None else "%.6g" % v                                              # noqa: E731
                    w.writerow([it["name"], n + 1, it["status"], " · ".join([it["reason"]] + it["notes"]).strip(" ·"), ts(it["t0"]), ts(it["t1"]),
                                "" if not (it["t0"] and it["t1"]) else "%.0f" % (it["t1"] - it["t0"]), e(s.get("t_max")), e(s.get("r_start")), e(s.get("r_peak")),
                                e(s.get("ratio")), e(s.get("t_heat")), e(s.get("t_cool")), e(s.get("hyst")), " ".join(it["files"]),
                                e(((s.get("spacing") or {}).get("heat") or {}).get("dc")), e(((s.get("spacing") or {}).get("cool") or {}).get("dc"))]
                               + [e(((s.get("curve") or {}).get(ph) or {}).get(k)) for k in ("n", "rate_c_min", "step_sd", "rev_n", "rev_max", "sp_err_mean", "sp_err_max")
                                  for ph in ("heat", "cool")])
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
                           dur=None if not (it["t0"] and it["t1"]) else round(it["t1"] - it["t0"], 1), name=it["name"],
                           spacing=analysis.point_spacing(self._first_cycle(it)) if it["points"] else None,      # 점 간격(가열 · 냉각) - 첫 회차
                           curve=(it.get("sum") or {}).get("curve"))
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
                "summary": summary, "msg": self.msg, "gate": [] if self.state == "running" else self.gate(), "volt": float(self.cond.get("meter_volt", c.cfg["meter_volt"])),
                "heat": None if self.heat is None else dict(self.heat)}

    def publish(self):
        with self._pub_lock:
            self.c.state.set("run", self.view())

    def refresh_gate(self):
        """측정 시작 조건(gate)의 입력이 바뀐 순간 화면용 state.run.gate 만 다시 계산해 보낸다(view 전체는 만들지 않는다 - 가볍게).
        입력: 장비 연결 · 세 축 원점 · 비상정지 잠금 · 판 영역 · 좌표 보정 · 접촉 높이 · 헤드 위치 확인 · 쿨러 배치 빈 칸(고른 샘플). 부르는 곳은 Controller.gate_inputs_changed 하나다.
        publish 와 같은 잠금 안에서 계산하고 쓴다 - 먼저 계산한 예전 목록이 나중에 써져 새 목록을 덮지 않게."""
        with self._pub_lock:
            if self.c.state.get("run") is None:
                return
            self.c.state.set("run", "gate", [] if self.state == "running" else self.gate())


def _frac_down(t_from, t_to):
    span = max(1e-6, float(t_from) - float(t_to))
    return lambda t: (float(t_from) - t) / span


def cd_of(cond, cfg):
    """요약에 쓸 조건: 이 측정의 cond(없는 키는 지금 설정)."""
    return dict(cfg, **(cond or {}))


def curve_summary(tl, cd):
    """첫 회차 기록 [(경과, 온도, 목표, 단계, 회차)] → {"heat": curve_stats, "cool": curve_stats}(가열 = 시작 ~ 가열 끝 온도 · 냉각 = 냉각 끝 ~ 가열 끝 온도)."""
    rows = [r for r in tl if r[4] == 1]
    rc = float(cd.get("reversal_c", 0.3))
    off_c, start_c, end_c = float(cd["run_heater_off_c"]), float(cd["run_start_c"]), float(cd["run_cool_end_c"])
    return {"heat": analysis.curve_stats([(r[0], r[1], r[2]) for r in rows if r[3] == 0], start_c, off_c, 1, rc),
            "cool": analysis.curve_stats([(r[0], r[1], r[2]) for r in rows if r[3] == 1], end_c, off_c, -1, rc)}


def heat_notices(c, cd, interval_key, who):
    """측정 · 온도 시험 시작 때 주의(막지 않는다): 열 모델이나 출력별 온도 표가 없으면 "[히트건 맞추기] 필요" · 설정 속도가 최대 속도 표의 그 구간 최대보다 빠르면 그 구간."""
    m = heater_mod.ThermalModel(cd)
    if not m.measured or not m.has_curve:
        c.state.alarm("heat_model", "열 모델 없음 · 온도 시험 [히트건 맞추기] 필요", NOTICE, dismiss=True)
    else:
        c.state.clear_alarm("heat_model")
    sp = c.calib.d.get("speed_max") or {}
    warn = heater_mod.speed_warnings(sp.get("rows") or [], config_mod.heat_rate_c_min(cd, interval_key), config_mod.cool_rate_c_min(cd, interval_key),
                                     float(cd["run_start_c"]), float(cd["run_heater_off_c"]), float(cd["run_cool_end_c"]))
    if warn:
        c.state.alarm("speed_max", "%s · 설정 속도가 최대보다 빠름 · %s%s" % (who, warn[0], " 외 %d곳" % (len(warn) - 1) if len(warn) > 1 else ""), NOTICE, dismiss=True)
        c.logs.app("%s 시작 · 속도 주의 · %s" % (who, " / ".join(warn)), "warn")
    else:
        c.state.clear_alarm("speed_max")
    c.logs.app("%s 열 제어 · %s · 가열 %.2f · 냉각 %.2f °C/분" % (who, m.text(), config_mod.heat_rate_c_min(cd, interval_key), config_mod.cool_rate_c_min(cd, interval_key)))
