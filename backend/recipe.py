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

새 단계 종류는 STEP_TYPES 에 정의(설명·검증·예상 시간)를 더하고, engine 의
RUNNERS 에 실행 함수를 더하면 된다(예: 나중의 photo).
"""

import math

WAIT_MAX_S = 3600.0
FIRST = "move_sample"
DEFAULT_NAME = "기본"


def default_recipe(wait_s=5.0):
    """기본 레시피. 릴레이 단계는 넣지 않는다(사용할 릴레이가 아직 정해지지 않았다)."""
    return {"name": DEFAULT_NAME, "steps": [
        {"type": "move_sample"},
        {"type": "z", "target": "work"},
        {"type": "wait", "s": float(wait_s)},
        {"type": "measure"},
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
        "name": "기준 포인터", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0}


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
    return "알 수 없는 단계"


STEP_TYPES = {
    "move_sample": "샘플 위로 이동",
    "z": "Z 이동",
    "relay": "릴레이",
    "wait": "대기",
    "measure": "측정",
    "confirm": "확인 대기",
}


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
        if e:
            errs["%s.steps[%d]" % (prefix, i)] = e
    if len(steps) > 50:
        errs[prefix + ".steps"] = "단계 50개 이내"
    if not errs and not ends_at_top(r, values):
        warns.append("'%s' 끝에서 Z 가 맨 위가 아님 · 다음 이동 전 자동 상승" % name)
    return errs, warns


def ends_at_top(r, values):
    """레시피가 끝날 때 Z 가 맨 위(0)인가. z 단계가 없으면 그대로(참)."""
    last = None
    for st in r.get("steps") or []:
        if isinstance(st, dict) and st.get("type") == "z":
            last = z_target_mm(st, values)
    return last is None or abs(last) < 1e-9


def uses_z(r):
    return any(isinstance(s, dict) and s.get("type") == "z" for s in r.get("steps") or [])


def relays_used(r):
    out = []
    for s in r.get("steps") or []:
        if isinstance(s, dict) and s.get("type") == "relay":
            try:
                n = int(s.get("n"))
            except (TypeError, ValueError):
                continue
            if n not in out:
                out.append(n)
    return sorted(out)


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
    return 0.0


def estimate_sample_s(r, values, move_s, z_speed_mm_s):
    """샘플 하나에 드는 예상 시간(초). z 단계 사이의 거리는 '0 에서 목표' 로 어림한다."""
    return sum(estimate_step_s(s, values, move_s, z_speed_mm_s)
               for s in r.get("steps") or [] if isinstance(s, dict))
