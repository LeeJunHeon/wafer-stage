"""conditions.py - 측정 조건(단계 목록)의 정의 · 설명 · 검증 · 예상 시간.

순수 함수만 둔다(state·장비를 모른다). 설정 검증(settings_schema)과 측정 실행(measure)이 같은 정의를 쓴다.
온도 · 계측기 · 접촉 판정은 별도 프로그램이 맡으므로 이 프로그램에는 없다. 남은 것은 이동 · Z · 쿨러 · 히트건 · 대기뿐이다.

측정 조건: {"name": "접촉 시험", "steps": [step, ...]}

  move_sample  샘플 위로 이동: Z 맨 위 확인 뒤 X · Y(공구 오프셋 적용). 첫 단계로 고정 - 삭제 · 이동 불가
  contact      접촉 높이로 내리기: 저장한 접촉 높이(절대 Z)까지. X · Y 가 샘플 위일 때만. 접촉 높이가 비어 있으면 측정 시작을 거절
  cooler       {"action": "on"|"off", "target": "sample"|"all"}  쿨러 켜기(샘플 구역 / 전부) · 끄기
  heat         {"s": 20.0}  히트건 n s - 시간이 되면 자동으로 끈다. n 은 heater.max_on_s 를 넘을 수 없다
  wait         {"s": 5.0}   대기
  confirm      사람이 [다음] 을 누를 때까지 대기(별도 프로그램으로 측정하는 동안 핀을 샘플에 댄 채 기다리는 용도)
  z_top        Z 맨 위
"""

import math

WAIT_MAX_S = 3600.0
FIRST = "move_sample"
DEFAULT_NAME = "접촉 시험"
MANUAL_NAME = "수동 측정"
HEAT_NAME = "가열"
HEAT_MAX_DEFAULT = 120.0

STEP_TYPES = {
    "move_sample": "샘플 위로 이동",
    "contact": "접촉 높이로 내리기",
    "cooler": "쿨러",
    "heat": "히트건",
    "wait": "대기",
    "confirm": "확인 대기",
    "z_top": "Z 맨 위",
}


def default_condition(wait_s=2.0):
    """접촉 시험: 이동 → 접촉 높이 → 대기 2 s → Z 맨 위. 릴레이를 켜지 않는다."""
    return {"name": DEFAULT_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "contact"},
        {"type": "wait", "s": float(wait_s)},
        {"type": "z_top"},
    ]}


def default_manual_condition():
    """수동 측정: 이동 → 쿨러 켜기(구역) → 접촉 높이 → 확인 대기 → 쿨러 끄기 → Z 맨 위."""
    return {"name": MANUAL_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "cooler", "action": "on", "target": "sample"},
        {"type": "contact"},
        {"type": "confirm"},
        {"type": "cooler", "action": "off", "target": "sample"},
        {"type": "z_top"},
    ]}


def default_heat_condition():
    """가열: 값은 전부 예시 - 장비에서 확인한 뒤 [편집]에서 바꾼다."""
    return {"name": HEAT_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "cooler", "action": "on", "target": "sample"},
        {"type": "contact"},
        {"type": "wait", "s": 30.0},
        {"type": "heat", "s": 20.0},
        {"type": "wait", "s": 60.0},
        {"type": "cooler", "action": "off", "target": "sample"},
        {"type": "z_top"},
    ]}


def default_conditions():
    return [default_condition(), default_manual_condition(), default_heat_condition()]


def _num(v):
    if isinstance(v, bool):
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if math.isfinite(f) else None


def _fmt(v):
    return ("%g" % v) if v is not None else "?"


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


def heater_relay_n(values):
    try:
        return int((((values or {}).get("heater")) or {}).get("relay") or 10)
    except (TypeError, ValueError):
        return 10


def heater_max_s(values):
    v = _num((((values or {}).get("heater")) or {}).get("max_on_s"))
    return v if v and v > 0 else HEAT_MAX_DEFAULT


def relay_enabled(n, values):
    items = (((values or {}).get("relay") or {}).get("items")) or []
    try:
        return bool(items[int(n) - 1].get("enabled"))
    except (IndexError, TypeError, ValueError, AttributeError):
        return False


def describe(step, values=None):
    """화면 · 로그용 한 줄(명사형). 서버가 만든 설명 문구 하나로 표시한다."""
    t = step.get("type")
    if t == "move_sample":
        return "샘플 위로 이동"
    if t == "contact":
        z = _num(((values or {}).get("contact_z_mm")))
        return "접촉 높이로 내리기" + ("" if z is None else " · Z %s mm" % _fmt(z))
    if t == "cooler":
        who = "전부" if step.get("target") == "all" else "샘플 구역"
        return "쿨러 %s %s" % (who, "켜기" if step.get("action") == "on" else "끄기")
    if t == "heat":
        return "히트건 %s s" % _fmt(_num(step.get("s")))
    if t == "wait":
        return "대기 %s s" % _fmt(_num(step.get("s")))
    if t == "confirm":
        return "확인 대기"
    if t == "z_top":
        return "Z 맨 위"
    return "알 수 없는 단계"


def validate_step(step, values, z_max_mm=None):
    """단계 하나의 오류 문구 또는 None."""
    if not isinstance(step, dict):
        return "단계 형식 오류"
    t = step.get("type")
    if t not in STEP_TYPES:
        return "알 수 없는 단계 종류 (%s)" % t
    if t == "wait":
        s = _num(step.get("s"))
        if s is None or s < 0 or s > WAIT_MAX_S:
            return "대기 0~%g s" % WAIT_MAX_S
    elif t == "cooler":
        if step.get("action") not in ("on", "off"):
            return "쿨러 켜기/끄기 오류"
        if step.get("target") not in ("sample", "all"):
            return "쿨러 대상은 샘플 구역 또는 전부"
    elif t == "heat":
        s = _num(step.get("s"))
        mx = heater_max_s(values)
        if s is None or s <= 0:
            return "히트건 시간 0 초과"
        if s > mx + 1e-9:
            return "히트건 %g s · 상한 %g s 초과" % (s, mx)
    return None


def validate_condition(r, values, z_max_mm=None, prefix=""):
    """(errors{path: 문구}, warnings[문구])."""
    errs, warns = {}, []
    if not isinstance(r, dict):
        return {prefix or "condition": "측정 조건 형식 오류"}, warns
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
        if e:
            errs["%s.steps[%d]" % (prefix, i)] = e
        elif isinstance(st, dict):
            warns.extend(step_warnings(st, values, name, i))
    if len(steps) > 50:
        errs[prefix + ".steps"] = "단계 50개 이내"
    if not errs:
        w = _order_warning(steps, name)
        if w:
            warns.append(w)
        if not ends_at_top(r):
            warns.append("'%s' 끝에서 Z 가 맨 위가 아님 · 다음 이동 전 자동 상승" % name)
    return errs, warns


def _order_warning(steps, name):
    """내린 채(접촉 높이) 끝나거나 히트건이 접촉 전에 켜지는 순서를 알린다(저장은 된다)."""
    types = [s.get("type") for s in steps if isinstance(s, dict)]
    if "heat" in types and "contact" in types and types.index("heat") < types.index("contact"):
        return "'%s' 히트건이 접촉 높이보다 먼저 켜짐" % name
    return ""


def step_warnings(st, values, name, i):
    """저장은 되지만 실행 전에 알려야 하는 것: 필요한 릴레이가 사용 안 함."""
    out = []
    off = [n for n in step_relays(st, values) if not relay_enabled(n, values)]
    if off:
        out.append("'%s' %d번 단계 · %s 사용 안 함 · 장비 설정 8 에서 사용 지정해야 실행됨"
                   % (name, i + 1, ", ".join("R%d" % n for n in off)))
    return out


def zone_relay_list(values):
    z = (values or {}).get("cooler") or {}
    zz = z.get("zones") if isinstance(z, dict) else None
    from core import zones as zones_mod
    zz, _ch = zones_mod.migrate(zz)
    out = []
    for n in zones_mod.relays_of(zz):
        try:
            out.append(int(n))
        except (TypeError, ValueError):
            pass
    return out


def step_relays(st, values):
    """단계가 켜는 릴레이 번호(쿨러 · 히트건)."""
    t = st.get("type")
    if t == "cooler" and st.get("action") == "on":
        return zone_relay_list(values)
    if t == "heat":
        return [heater_relay_n(values)]
    return []


def expand(steps, cap=5000):
    """실행 순서대로의 (원래 단계 번호 0 기준, 단계) 목록. 반복 단계는 없다 - 그대로 번호를 붙인다."""
    return [(i, st) for i, st in enumerate(steps) if isinstance(st, dict)][:cap]


def ends_at_top(r):
    """측정 조건이 끝날 때 Z 가 맨 위인가: 내리기(contact) 뒤에 Z 맨 위가 있어야 한다."""
    down = False
    for st in r.get("steps") or []:
        if not isinstance(st, dict):
            continue
        if st.get("type") == "contact":
            down = True
        elif st.get("type") == "z_top":
            down = False
    return not down


def uses_contact(r):
    return any(isinstance(s, dict) and s.get("type") == "contact" for s in r.get("steps") or [])


def uses_heat(r):
    return any(isinstance(s, dict) and s.get("type") == "heat" for s in r.get("steps") or [])


def relays_used(r, values=None):
    out = []
    for s in r.get("steps") or []:
        if isinstance(s, dict):
            for n in step_relays(s, values):
                if n not in out:
                    out.append(n)
    return sorted(out)


def find(values, name=None):
    """이름으로 측정 조건을 찾는다(없으면 활성, 그것도 없으면 첫 조건)."""
    rc = (values or {}).get("condition") or {}
    items = [r for r in rc.get("items") or [] if isinstance(r, dict)]
    want = name if name is not None else rc.get("active")
    for r in items:
        if r.get("name") == want:
            return r
    return items[0] if items else default_condition()


def estimate_step_s(step, values, move_s, z_speed_mm_s):
    """단계 하나의 예상 시간(초). 사람 확인은 0 으로 센다."""
    t = step.get("type")
    if t == "move_sample":
        return move_s
    if t in ("contact", "z_top"):
        z = _num((values or {}).get("contact_z_mm")) if t == "contact" else 0.0
        return 0.5 + abs(z or 0.0) / max(z_speed_mm_s, 1e-6)
    if t in ("wait", "heat"):
        return _num(step.get("s")) or 0.0
    if t == "cooler":
        return 0.3
    return 0.0


def estimate_sample_s(r, values, move_s, z_speed_mm_s):
    """샘플 하나에 드는 예상 시간(초)."""
    return sum(estimate_step_s(s, values, move_s, z_speed_mm_s) for _i, s in expand(r.get("steps") or []))
