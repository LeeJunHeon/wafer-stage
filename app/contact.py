"""contact.py - 전기로 접촉을 확인하며 핀 내리기. v2 backend/contact.py 의 방식과 값을 그대로 옮겼다(값은 전부 settings contact_*).

준비 6 · 수동 탭 [접촉 탐색] · 측정의 "접촉" 단계가 이 함수 하나를 쓴다.

  1) K2450 을 접촉 확인용으로 설정(2단자 · 0.1 V · 전류 한도 0.1 mA · 과전압 보호 가장 낮게 · 자동 범위 · 평균 끔)하고 출력을 켠다.
  2) 높이 모드(측정): 저장한 접촉 높이 − 0.3 mm 까지 내린 뒤 0.025 mm 씩 내리며 멈출 때마다 한 번 잰다.
     찾기 모드(준비 6단계): 지금 높이부터 0.05 mm 씩 최대 5 mm 까지 → 닿으면 0.3 mm 올렸다가 0.025 mm 씩 다시.
  3) 열린 회로(전류 < 1 nA)도 단락도 아닌 값이 2번 연속이면 닿음(첫 접촉 Z = 그 연속의 첫 자리).
  4) 첫 접촉 + 0.05 mm 까지 누르고 4단자로 한 번 더 잰다 - 정상(과전압 보호 안 걸림 · 양수 · 2단자보다 작고 1/10 보다 큼)이면 네 핀 모두 닿음.
     아니면 0.025 mm 씩 더 내리며 4단자를 다시 잰다(한도 안에서만).
  5) 단락(전류 한도 걸림 · 2단자 < 1 kΩ) = 핀이 샘플 밖 금속 판에 닿음 → 멈추고 핀 맨 위 · 실패.
  6) 한도(높이 모드: 저장 높이 + 0.5 mm · 찾기 모드: 시작 + 5 mm · Z 가동 범위)까지 내려도 안 닿으면 더 내리지 않고 핀 맨 위 · 실패.
  7) 어떤 끝에서도 K2450 출력을 끈다. 비상정지면 핀을 움직이지 않는다(출력만 끈다).
"""

from app.devices import meters

FOUR_MIN_RATIO = 0.1             # 4단자가 2단자의 1/10 보다 작으면 감지 핀이 떠 출력이 올라간 것으로 본다(열린 감지선 - 정상 아님)
SEARCH_BACK_MM = 0.3             # 찾기 모드: 닿으면 이만큼 올렸다가 촘촘히 다시
MAX_READS = 2000                 # 한 번 찾기의 읽기 상한(한도 계산이 틀려도 끝없이 돌지 않게)
KEYS = ("volt", "ilim_a", "nplc", "step_mm", "start_above_mm", "max_below_mm", "confirm_n", "overdrive_mm", "short_ohm", "search_mm", "search_step_mm", "open_a")
FAIL_TEXT = {"none": "닿지 않았습니다", "short": "금속 판에 닿았습니다", "four": "네 핀이 다 닿지 않았습니다", "inst": "저항계 오류"}


class ContactFail(Exception):
    """접촉 찾기 실패. code: none(안 닿음) · short(금속 판 · 단락) · four(4단자 확인 실패) · inst(K2450 오류). 문구는 사람이 읽는 한 문장."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def params(cfg):
    return {k: cfg["contact_" + k] for k in KEYS}


def classify(current, trip, volt, p):
    """(종류, 저항 Ω | None). 종류: open(전류가 하한 아래) · short(전류 한도 걸림 · 저항 < short_ohm) · contact · bad(값 없음)."""
    if trip:
        return "short", None
    if current is None:
        return "bad", None
    a = abs(float(current))
    if a < float(p["open_a"]):
        return "open", None
    r = abs(float(volt)) / a
    if r < float(p["short_ohm"]):
        return "short", r
    return "contact", r


def four_ok(r4, r2, ovp):
    """4단자 값이 정상인가: 과전압 보호가 안 걸렸고 양수이며 2단자보다 작고 2단자의 1/10 보다 크다."""
    return (not ovp) and r4 is not None and r2 is not None and 0 < r4 < r2 and r4 >= FOUR_MIN_RATIO * r2


def limits(mode, z_ref, p, z_max, one_mm):
    """(시작 Z, 내려갈 수 있는 한도 Z). one_mm = 한 펄스(mm)."""
    if mode == "height":
        z_ref = float(z_ref)
        return max(one_mm, z_ref - float(p["start_above_mm"])), min(float(z_max), z_ref + float(p["max_below_mm"]))
    z0 = max(0.0, float(z_ref or 0.0))
    return max(one_mm, z0), min(float(z_max), z0 + float(p["search_mm"]))


def make_sense(k2450, p, mode, floor, one_mm, on_step=None):
    """멈출 때마다 부르는 판단 함수 sense(z) → 더 움직일 거리 dz(mm, 양수 = 아래) | None(끝) 과 진행 상태 st 를 만든다."""
    st = {"phase": "coarse" if mode == "search" else "2w", "streak": 0, "first": None, "first_z": None, "r2": None, "r4": None, "n": 0, "four": False, "points": []}
    volt, step = float(p["volt"]), float(p["step_mm"])

    def read():
        try:
            return k2450.read_strict()
        except meters.VisaError as e:
            raise ContactFail("저항계 값을 읽지 못했습니다 · %s" % str(e)[:40], "inst")

    def down(z, d):
        """d 만큼 더 내리는 요청 - 한도를 넘으면 움직이지 않고 실패."""
        if z + d > floor + 1e-9:
            if st["phase"] == "4w":
                raise ContactFail("네 핀이 다 닿지 않았습니다 · Z %.2f mm" % floor, "four")
            raise ContactFail("닿지 않았습니다 · Z %.2f mm" % floor, "none")
        return d

    def note(z, kind, ohm):
        st["points"].append([round(z, 4), kind, ohm])
        if on_step:
            on_step(z, kind, ohm)

    def sense(z):
        st["n"] += 1
        if st["n"] > MAX_READS:
            raise ContactFail("닿지 않았습니다 · 읽기 횟수를 넘었습니다", "none")
        if st["phase"] in ("coarse", "2w"):
            cur, trip = read()
            kind, r = classify(cur, trip, volt, p)
            note(z, kind, r)
            if kind == "bad":
                raise ContactFail("저항계 값이 이상합니다", "inst")
            if kind == "short":
                raise ContactFail("금속 판에 닿았습니다 · Z %.2f mm" % z, "short")
            d = float(p["search_step_mm"]) if st["phase"] == "coarse" else step
            if kind == "open":
                st["streak"], st["first"] = 0, None
                return down(z, d)
            if st["streak"] == 0:
                st["first"] = z
            st["streak"] += 1
            st["r2"] = r
            if st["streak"] < int(p["confirm_n"]):
                return down(z, d)
            if st["phase"] == "coarse":                      # 처음 찾기: 닿은 자리에서 0.3 mm 올렸다가 촘촘히 다시
                st["phase"], st["streak"], st["first"] = "2w", 0, None
                back = min(SEARCH_BACK_MM, z - one_mm)
                return -back if back > 0 else 0.0
            st["first_z"] = st["first"]
            st["phase"] = "4w"
            od = min(st["first"] + float(p["overdrive_mm"]), floor) - z          # 최종 Z = 첫 접촉 + 더 누름(이미 내려온 만큼은 빼고 · 한도 안)
            return od if od > 1e-9 else 0.0
        # 4단자 확인
        if not st["four"]:
            try:
                k2450.set_four_wire(True)
            except meters.VisaError as e:
                raise ContactFail("저항계 4단자 설정 오류 · %s" % str(e)[:40], "inst")
            st["four"] = True
        cur, trip = read()
        try:
            ovp = k2450.ovp_tripped()
        except meters.VisaError as e:
            raise ContactFail("저항계 값을 읽지 못했습니다 · %s" % str(e)[:40], "inst")
        kind, r4 = classify(cur, trip, volt, p)
        note(z, "4w:" + kind + (":ovp" if ovp else ""), r4)
        if kind == "short":
            raise ContactFail("금속 판에 닿았습니다 · Z %.2f mm" % z, "short")
        if kind == "bad":
            raise ContactFail("저항계 값이 이상합니다", "inst")
        if kind == "contact" and four_ok(r4, st["r2"], ovp):
            st["r4"] = r4
            return None
        return down(z, step)

    return sense, st
