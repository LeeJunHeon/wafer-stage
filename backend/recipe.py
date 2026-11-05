"""recipe.py - 측정 순서(레시피)의 단계 정의 · 설명 · 검증 · 예상 시간.

순수 함수만 둔다(state·장비를 모른다). 설정 검증(settings_schema)과 순회
엔진(engine)이 같은 정의를 쓴다 - 화면이 받아 주는 단계와 엔진이 실행하는
단계가 어긋나지 않게.

레시피: {"name": "기본", "steps": [step, ...]}
단계(step)는 "type" 과 그 종류의 값만 가진다.

  move_sample  샘플 위로 이동(공구 오프셋 적용). 첫 단계로 고정 - 삭제·이동 불가
  z            {"target": "work"|"top"|"mm", "mm": 10.0}
               work = 활성 공구의 작업 높이(z_work_mm), top = 맨 위(0)
  relay        {"n": 1~10, "on": true|false}
  wait         {"s": 5.0}
  measure      계측기 호출(지금은 더미)
  confirm      사람이 [다음]을 누를 때까지 대기
  cooler       {"action": "on"|"off", "target": "sample"|"all"|"n", "n": 1~10}  쿨러(구역 릴레이)
  contact      Z 접촉 탐색 {"start_ref": "work"|"mm", "start_mm", "step_mm", "max_ref": "work"|"mm", "max_mm",
               "press_mm", "settle_s", "on_fail": "stop"|"skip"|"confirm"} (work 기준 값은 작업 높이에 더하는 mm)
  wait_temp    {"cond": "<="|">=", "target_c", "hold_s", "max_s", "on_timeout": "continue"|"skip"|"stop"|"confirm"}
  heat         히트건 켜기 → 끝 조건 → 끄기 {"end": "temp"|"time"|"either", "target_c", "time_s", "max_s"}
  cool         히트건 끈 채 wait_temp 와 같은 조건으로 대기(기록 구분용 이름)
  repeat       블록 반복 {"from": 시작 단계, "to": 끝 단계(번호는 1부터), "count": 횟수} - 이 단계 앞의 블록을 count 번 돈다
  log          {"action": "start"|"end"} 기록 시작/끝(temp.period_s 마다 온도 · 릴레이 · Z · 측정값을 CSV 로)

새 단계 종류는 STEP_TYPES 에 정의(설명·검증·예상 시간)를 더하고, engine 의
RUNNERS 에 실행 함수를 더하면 된다(예: 나중의 photo).
"""

import math

WAIT_MAX_S = 3600.0
FIRST = "move_sample"
DEFAULT_NAME = "기본"
HEAT_NAME = "가열 측정"
# 온도 · 접촉 · 가열 단계의 입력 범위(검증 · 편집기 공용)
TEMP_MIN_C, TEMP_MAX_C = -50.0, 600.0


def default_recipe(wait_s=5.0):
    """기본 레시피. 릴레이 단계는 넣지 않는다(사용할 릴레이가 아직 정해지지 않았다)."""
    return {"name": DEFAULT_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "z", "target": "work"},
        {"type": "wait", "s": float(wait_s)},
        {"type": "measure"},
        {"type": "z", "target": "top"},
    ]}


def default_heat_recipe():
    """기본 레시피 '가열 측정'. 온도 · 시간 값은 전부 임시(placeholder) - 장비에서 측정한 뒤 바꾼다."""
    return {"name": HEAT_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "cooler", "action": "on", "target": "sample"},
        {"type": "contact", "start_ref": "work", "start_mm": -2.0, "step_mm": 0.1,
         "max_ref": "work", "max_mm": 1.0, "press_mm": 0.1, "settle_s": 0.2, "on_fail": "stop"},
        {"type": "log", "action": "start"},
        {"type": "wait_temp", "cond": "<=", "target_c": 30.0, "hold_s": 5.0, "max_s": 300.0,
         "on_timeout": "confirm"},
        {"type": "heat", "end": "either", "target_c": 150.0, "time_s": 60.0, "max_s": 120.0},
        {"type": "cool", "cond": "<=", "target_c": 40.0, "hold_s": 0.0, "max_s": 300.0,
         "on_timeout": "continue"},
        {"type": "log", "action": "end"},
        {"type": "z", "target": "top"},
    ]}


def _num(v):
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def relay_name(n, values):
    items = (((values or {}).get("relay") or {}).get("items")) or []
    try:
        nm = str(items[int(n) - 1].get("name") or "").strip()
    except (IndexError, TypeError, ValueError, AttributeError):
        nm = ""
    return nm or ("R%s" % n)


def relay_enabled(n, values):
    items = (((values or {}).get("relay") or {}).get("items")) or []
    try:
        return bool(items[int(n) - 1].get("enabled"))
    except (IndexError, TypeError, ValueError, AttributeError):
        return False


def active_tool(values):
    """활성 공구 dict. 없으면 첫 공구(기준 포인터)."""
    tool = (values or {}).get("tool") or {}
    items = tool.get("items") or []
    name = tool.get("active")
    for t in items:
        if isinstance(t, dict) and t.get("name") == name:
            return t
    return items[0] if items and isinstance(items[0], dict) else {
        "name": "기준 포인터", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0,
        "tip_shape": "rect", "tip_w_mm": 10.0, "tip_h_mm": 2.0, "tip_angle_deg": 90.0}


def z_target_mm(step, values):
    """z 단계의 목표 높이(mm). 0 = 맨 위. 읽을 수 없으면 None."""
    t = step.get("target")
    if t == "top":
        return 0.0
    if t == "work":
        return _num(active_tool(values).get("z_work_mm"))
    return _num(step.get("mm"))


def _fmt(v):
    return ("%g" % v) if v is not None else "?"


def describe(step, values=None):
    """화면·로그용 한 줄(명사형)."""
    t = step.get("type")
    if t == "move_sample":
        return "샘플 위로 이동"
    if t == "z":
        tg = step.get("target")
        if tg == "top":
            return "Z 맨 위"
        if tg == "work":
            return "Z 작업 높이 %s mm" % _fmt(z_target_mm(step, values))
        return "Z %s mm" % _fmt(_num(step.get("mm")))
    if t == "relay":
        return "%s %s" % (relay_name(step.get("n"), values), "켜기" if step.get("on") else "끄기")
    if t == "wait":
        return "대기 %s s" % _fmt(_num(step.get("s")))
    if t == "measure":
        return "측정"
    if t == "confirm":
        return "확인 대기"
    if t == "cooler":
        tg = step.get("target")
        who = "전부" if tg == "all" else ("R%s" % step.get("n") if tg == "n" else "이 샘플 구역")
        return "쿨러 %s %s" % (who, "켜기" if step.get("action") == "on" else "끄기")
    if t == "contact":
        return "접촉 탐색 시작 %s · 스텝 %s mm · 최대 %s" % (_ref_text(step, "start", values), _fmt(_num(step.get("step_mm"))),
                                                      _ref_text(step, "max", values))
    if t == "wait_temp":
        return "온도 대기 %s %s °C · 최대 %s s" % (step.get("cond"), _fmt(_num(step.get("target_c"))), _fmt(_num(step.get("max_s"))))
    if t == "cool":
        return "냉각 대기 %s %s °C · 최대 %s s" % (step.get("cond"), _fmt(_num(step.get("target_c"))), _fmt(_num(step.get("max_s"))))
    if t == "heat":
        e = step.get("end")
        cond = ("%s °C" % _fmt(_num(step.get("target_c")))) if e == "temp" else (
            "%s s" % _fmt(_num(step.get("time_s")))) if e == "time" else (
            "%s °C 또는 %s s" % (_fmt(_num(step.get("target_c"))), _fmt(_num(step.get("time_s")))))
        return "가열 %s · 최대 %s s" % (cond, _fmt(_num(step.get("max_s"))))
    if t == "repeat":
        return "반복 %s~%s 단계 × %s" % (step.get("from"), step.get("to"), step.get("count"))
    if t == "log":
        return "기록 %s" % ("시작" if step.get("action") == "start" else "끝")
    return "알 수 없는 단계"


STEP_TYPES = {
    "move_sample": "샘플 위로 이동",
    "z": "Z 이동",
    "relay": "릴레이",
    "wait": "대기",
    "measure": "측정",
    "confirm": "확인 대기",
    "cooler": "쿨러",
    "contact": "접촉 탐색",
    "wait_temp": "온도 대기",
    "heat": "가열",
    "cool": "냉각 대기",
    "repeat": "반복",
    "log": "기록",
}
ON_TIMEOUT = ("continue", "skip", "stop", "confirm")


def _ref_text(step, which, values):
    ref = step.get(which + "_ref")
    v = _num(step.get(which + "_mm"))
    if ref == "work":
        return "작업 높이 %+g mm" % (v or 0.0)
    return "%s mm" % _fmt(v)


def contact_range(step, values):
    """접촉 탐색의 (시작 Z, 최대 Z) 절대값(mm). 읽을 수 없으면 (None, None)."""
    work = _num(active_tool(values).get("z_work_mm")) or 0.0
    out = []
    for w in ("start", "max"):
        v = _num(step.get(w + "_mm"))
        if v is None:
            out.append(None)
        else:
            out.append(work + v if step.get(w + "_ref") == "work" else v)
    return out[0], out[1]


def validate_step(step, values, z_max_mm):
    """단계 하나의 오류 문구 또는 None."""
    if not isinstance(step, dict):
        return "단계 형식 오류"
    t = step.get("type")
    if t not in STEP_TYPES:
        return "알 수 없는 단계 종류 (%s)" % t
    if t == "z":
        tg = step.get("target")
        if tg not in ("work", "top", "mm"):
            return "Z 목표 오류"
        z = z_target_mm(step, values)
        if z is None:
            return "Z 높이 오류"
        if z < 0 or z > z_max_mm + 1e-9:
            return "Z %g mm · 가동범위 0~%g 밖" % (z, z_max_mm)
    elif t == "relay":
        try:
            n = int(step.get("n"))
        except (TypeError, ValueError):
            return "릴레이 번호 오류"
        if not 1 <= n <= 10:
            return "릴레이 번호 1~10"
        if not relay_enabled(n, values):
            return "R%d 사용 안 함 · 릴레이 탭에서 사용 지정" % n
        if not isinstance(step.get("on"), bool):
            return "릴레이 켜기/끄기 오류"
    elif t == "wait":
        s = _num(step.get("s"))
        if s is None or s < 0 or s > WAIT_MAX_S:
            return "대기 0~%g s" % WAIT_MAX_S
    elif t == "cooler":
        if step.get("action") not in ("on", "off"):
            return "쿨러 켜기/끄기 오류"
        if step.get("target") not in ("sample", "all", "n"):
            return "쿨러 대상 오류"
        if step.get("target") == "n":
            try:
                if not 1 <= int(step.get("n")) <= 10:
                    return "릴레이 번호 1~10"
            except (TypeError, ValueError):
                return "릴레이 번호 오류"
    elif t == "contact":
        if step.get("start_ref") not in ("work", "mm") or step.get("max_ref") not in ("work", "mm"):
            return "접촉 탐색 높이 기준 오류"
        a, b = contact_range(step, values)
        if a is None or b is None:
            return "접촉 탐색 높이 오류"
        rel = step.get("start_ref") == "work" or step.get("max_ref") == "work"
        if not rel:                          # 작업 높이 기준은 공구에 따라 달라 경고로(실행 때 다시 검사)
            for nm, z in (("시작", a), ("최대", b)):
                if z < 0 or z > z_max_mm + 1e-9:
                    return "접촉 %s Z %g mm · 가동범위 0~%g 밖" % (nm, z, z_max_mm)
            if a >= b:
                return "접촉 시작 높이는 최대 깊이보다 위여야 함"
        elif a is not None and b is not None and a >= b:
            return "접촉 시작 높이는 최대 깊이보다 위여야 함"
        st = _num(step.get("step_mm"))
        if st is None or st <= 0 or st > 5:
            return "스텝 0 초과 5 mm 이하"
        pr = _num(step.get("press_mm"))
        if pr is None or pr < 0 or pr > 5:
            return "추가 누름 0~5 mm"
        se = _num(step.get("settle_s"))
        if se is None or se < 0 or se > 60:
            return "스텝 간 대기 0~60 s"
        if step.get("on_fail") not in ("stop", "skip", "confirm"):
            return "못 찾으면 처리 오류"
    elif t in ("wait_temp", "cool"):
        if step.get("cond") not in ("<=", ">="):
            return "조건은 ≤ 또는 ≥"
        tc = _num(step.get("target_c"))
        if tc is None or not TEMP_MIN_C <= tc <= TEMP_MAX_C:
            return "목표 %g~%g °C" % (TEMP_MIN_C, TEMP_MAX_C)
        hs, ms = _num(step.get("hold_s")), _num(step.get("max_s"))
        if hs is None or hs < 0 or hs > WAIT_MAX_S:
            return "유지 시간 0~%g s" % WAIT_MAX_S
        if ms is None or ms <= 0 or ms > 86400:
            return "최대 대기 0 초과 86400 s 이하"
        if step.get("on_timeout") not in ON_TIMEOUT:
            return "시간 초과 처리 오류"
    elif t == "heat":
        if step.get("end") not in ("temp", "time", "either"):
            return "가열 끝 조건 오류"
        tc, ts, ms = _num(step.get("target_c")), _num(step.get("time_s")), _num(step.get("max_s"))
        if step.get("end") in ("temp", "either") and (tc is None or not TEMP_MIN_C <= tc <= TEMP_MAX_C):
            return "목표 %g~%g °C" % (TEMP_MIN_C, TEMP_MAX_C)
        if step.get("end") in ("time", "either") and (ts is None or ts <= 0 or ts > 3600):
            return "가열 시간 0 초과 3600 s 이하"
        if ms is None or ms <= 0 or ms > 3600:
            return "최대 시간(필수) 0 초과 3600 s 이하"
    elif t == "log":
        if step.get("action") not in ("start", "end"):
            return "기록 시작/끝 오류"
    return None


def validate_recipe(r, values, z_max_mm, prefix=""):
    """(errors{path: 문구}, warnings[문구])."""
    errs, warns = {}, []
    if not isinstance(r, dict):
        return {prefix or "recipe": "레시피 형식 오류"}, warns
    name = str(r.get("name") or "").strip()
    if not name:
        errs[prefix + ".name"] = "이름 필요"
    elif len(name) > 30:
        errs[prefix + ".name"] = "이름 30자 이내"
    steps = r.get("steps")
    if not isinstance(steps, list) or not steps:
        errs[prefix + ".steps"] = "단계 없음"
        return errs, warns
    if not isinstance(steps[0], dict) or steps[0].get("type") != FIRST:
        errs[prefix + ".steps[0]"] = "첫 단계는 샘플 위로 이동"
    for i, st in enumerate(steps):
        if i > 0 and isinstance(st, dict) and st.get("type") == FIRST:
            errs["%s.steps[%d]" % (prefix, i)] = "샘플 이동은 첫 단계만"
            continue
        e = validate_step(st, values, z_max_mm)
        if not e and isinstance(st, dict) and st.get("type") == "repeat":
            e = _validate_repeat(st, i)
        if e:
            errs["%s.steps[%d]" % (prefix, i)] = e
        elif isinstance(st, dict):
            warns.extend(step_warnings(st, values, name, i))
    if len(steps) > 50:
        errs[prefix + ".steps"] = "단계 50개 이내"
    if not errs and not ends_at_top(r, values):
        warns.append("'%s' 끝에서 Z 가 맨 위가 아님 · 다음 이동 전 자동 상승" % name)
    return errs, warns


def _validate_repeat(st, i):
    """repeat(0 기준 위치 i): 블록은 자기 앞 단계들(2 이상 · 첫 단계 샘플 이동은 제외)이어야 한다."""
    try:
        a, b, c = int(st.get("from")), int(st.get("to")), int(st.get("count"))
    except (TypeError, ValueError):
        return "반복 시작 · 끝 · 횟수 숫자 필요"
    if a < 2 or a > b or b > i:
        return "반복 블록은 2번 단계부터 이 단계 바로 앞까지 안에서"
    if not 1 <= c <= 100:
        return "반복 횟수 1~100"
    return None


def step_warnings(st, values, name, i):
    """저장은 되지만 실행 전에 알려야 하는 것: 필요한 릴레이가 사용 안 함."""
    out = []
    off = [n for n in step_relays(st, values) if not relay_enabled(n, values)]
    if off:
        out.append("'%s' %d번 단계 · %s 사용 안 함 · 릴레이 탭에서 사용 지정해야 실행됨"
                   % (name, i + 1, ", ".join("R%d" % n for n in off)))
    if st.get("type") == "contact":
        a, b = contact_range(st, values)
        zmax = _num((((values or {}).get("limits")) or {}).get("z_max_mm")) or 45.0
        if a is not None and b is not None and (a < 0 or b + (_num(st.get("press_mm")) or 0) > zmax + 1e-9):
            out.append("'%s' %d번 단계 · 활성 공구 기준 접촉 높이 Z %g~%g mm 가 가동범위 0~%g 밖 · 공구 작업 높이 확인"
                       % (name, i + 1, a, b, zmax))
    return out


def zone_relay_list(values):
    z = (values or {}).get("cooler") or {}
    zz = z.get("zones") if isinstance(z, dict) else None
    out = []
    for n in (zz or {}).get("relays") or []:
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            pass
    return out


def heater_relay_n(values):
    try:
        return int((((values or {}).get("heater")) or {}).get("relay") or 10)
    except (TypeError, ValueError):
        return 10


def step_relays(st, values):
    """단계가 켜는 릴레이 번호(쿨러 · 히트건 · 릴레이 단계)."""
    t = st.get("type")
    if t == "relay":
        try:
            return [int(st.get("n"))] if st.get("on") else []
        except (TypeError, ValueError):
            return []
    if t == "cooler" and st.get("action") == "on":
        if st.get("target") == "n":
            try:
                return [int(st.get("n"))]
            except (TypeError, ValueError):
                return []
        return zone_relay_list(values)
    if t == "heat":
        return [heater_relay_n(values)]
    return []


def expand(steps, cap=2000):
    """repeat 를 풀어 실행 순서대로의 (원래 단계 번호 0 기준, 단계) 목록. 예상 시간 계산용(엔진은 같은 규칙으로 돈다)."""
    out, counters, i, guard = [], {}, 0, 0
    steps = [s for s in steps if isinstance(s, dict)]
    while i < len(steps) and guard < cap:
        guard += 1
        st = steps[i]
        if st.get("type") == "repeat":
            try:
                a, c = int(st.get("from")), int(st.get("count"))
            except (TypeError, ValueError):
                i += 1
                continue
            counters[i] = counters.get(i, 0) + 1
            if counters[i] < c and 1 <= a <= i:
                for k in range(a - 1, i):          # 안쪽 반복의 카운터는 다시 시작
                    counters.pop(k, None)
                i = a - 1
                continue
            counters.pop(i, None)
            i += 1
            continue
        out.append((i, st))
        i += 1
    return out


def ends_at_top(r, values):
    """레시피가 끝날 때 Z 가 맨 위(0)인가. z 단계가 없으면 그대로(참)."""
    last = None
    for st in r.get("steps") or []:
        if isinstance(st, dict) and st.get("type") == "z":
            last = z_target_mm(st, values)
    return last is None or abs(last) < 1e-9


def uses_z(r):
    return any(isinstance(s, dict) and s.get("type") == "z" for s in r.get("steps") or [])


def relays_used(r, values=None):
    out = []
    for s in r.get("steps") or []:
        if not isinstance(s, dict):
            continue
        if s.get("type") == "relay":
            try:
                n = int(s.get("n"))
            except (TypeError, ValueError):
                continue
            if n not in out:
                out.append(n)
        elif values is not None:
            for n in step_relays(s, values):
                if n not in out:
                    out.append(n)
    return sorted(out)


def needs_temp(r):
    """온도 센서가 필요한 레시피인가(가열 · 온도 대기 · 냉각 대기)."""
    return any(isinstance(s, dict) and s.get("type") in ("heat", "wait_temp", "cool") for s in r.get("steps") or [])


def find(values, name=None):
    """이름으로 레시피를 찾는다(없으면 활성, 그것도 없으면 첫 레시피)."""
    rc = (values or {}).get("recipe") or {}
    items = [r for r in rc.get("items") or [] if isinstance(r, dict)]
    want = name if name is not None else rc.get("active")
    for r in items:
        if r.get("name") == want:
            return r
    return items[0] if items else default_recipe()


def estimate_step_s(step, values, move_s, z_speed_mm_s):
    """단계 하나의 예상 시간(초). 사람 확인은 0 으로 센다."""
    t = step.get("type")
    if t == "move_sample":
        return move_s
    if t == "z":
        z = z_target_mm(step, values) or 0.0
        return 0.3 + abs(z) / max(z_speed_mm_s, 1e-6)
    if t == "wait":
        return _num(step.get("s")) or 0.0
    if t == "relay":
        return 0.1
    if t == "measure":
        return 0.5
    if t in ("wait_temp", "cool"):
        return (_num(step.get("max_s")) or 0.0)          # 온도 조건은 최대 대기 기준
    if t == "heat":
        return (_num(step.get("max_s")) or 0.0)
    if t == "cooler":
        return 0.3
    if t == "contact":
        a, b = contact_range(step, values)
        st = _num(step.get("step_mm")) or 0.1
        se = _num(step.get("settle_s")) or 0.0
        if a is None or b is None:
            return 0.0
        return max(0.0, (b - a) / max(st, 1e-6)) * (se + 0.15) + 0.5     # 최대 깊이까지 간다고 어림
    return 0.0


def estimate_sample_s(r, values, move_s, z_speed_mm_s):
    """샘플 하나에 드는 예상 시간(초). z 단계 사이의 거리는 '0 에서 목표' 로 어림한다."""
    return sum(estimate_step_s(s, values, move_s, z_speed_mm_s)
               for _i, s in expand(r.get("steps") or []))
