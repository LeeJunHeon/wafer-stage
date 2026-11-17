"""contact.py - 전기로 접촉을 확인하며 핀 내리기("전기 탐색"). v2 backend/contact.py 의 방식을 옮겼다. 값은 전부 settings contact_*.

접촉 방식(contact_mode)의 기본은 "고정 높이"(저장한 접촉 높이까지만 내린다 - Controller.contact_fixed)이고, 이 파일의 전기 탐색은 선택 사항이다.
측정의 "접촉" 단계(전기 탐색일 때) · 초기 설정 6 · 수동 제어 탭 [접촉 탐색] 이 이 함수 하나를 쓴다.

  1) K2450 을 접촉 확인용으로 설정(2단자 · 0.1 V · 전류 한도 0.1 mA · 과전압 보호 가장 낮게 · 자동 범위 · 평균 끔)하고 출력을 켠다.
  2) 높이 모드(측정): 저장한 접촉 높이 − start_above_mm 까지 내린 뒤 step_mm 씩 내리며 멈출 때마다 한 번 잰다.
     찾기 모드(초기 설정 6 · 수동): 지금 높이부터 search_step_mm 씩 최대 search_mm 까지 → 닿으면 search_back_mm 올렸다가 step_mm 씩 다시.
  3) 열린 회로(전류 < open_a)도 단락도 아닌 값이 confirm_n 번 연속이면 닿음(첫 접촉 Z = 그 연속의 첫 자리).
  4) 첫 접촉 + overdrive_mm 까지 누른다 - 이것도 step_mm 이하로 나눠 내리고 멈출 때마다 잰다. 그다음 4단자로 한 번 더 잰다 -
     정상(과전압 보호 안 걸림 · 양수 · 2단자보다 작고 2단자 × four_min_ratio 보다 큼)이면 네 핀 모두 닿음.
     아니면 step_mm 씩 더 내리며 4단자를 다시 잰다(한도 안에서만).
  5) 단락(전류 한도 걸림 · 2단자 < short_ohm) = 핀이 샘플 밖 금속 판에 닿음 → 멈추고 핀 맨 위 · 실패.
  6) 한도(높이 모드: 저장 높이 + max_below_mm · 찾기 모드: 시작 + search_mm · Z 가동 범위)까지 내려도 안 닿으면 더 내리지 않고 핀 맨 위 · 실패.
  7) 어떤 끝에서도 K2450 출력을 끈다. 비상정지면 핀을 움직이지 않는다(출력만 끈다).

걸음 크기: 아래로 가는 한 걸음은 그 단계의 간격(첫 하강 search_step_mm · 그 밖 step_mm)을 넘지 않는다. 간격은 펄스 단위로 **내림**해서 쓴다
(0.03 mm → 4 펄스 0.025 mm). 더 누름도 나눠서 내린다. 위로 가는 되돌림만 한 번에 간다(샘플에서 멀어지는 쪽).
움직이는 쪽(Controller._probe_down)이 걸음마다 이 한도와 내려갈 수 있는 한도(floor)를 한 번 더 확인한다.
"""

import math

from app.devices import meters

KEYS = ("volt", "ilim_a", "nplc", "step_mm", "start_above_mm", "max_below_mm", "confirm_n", "overdrive_mm", "short_ohm", "search_mm", "search_step_mm", "open_a",
        "four_min_ratio", "search_back_mm", "max_reads")
FAIL_TEXT = {"none": "미접촉", "short": "금속 판 접촉", "four": "4핀 접촉 실패", "inst": "저항계 오류", "step": "하강 간격 초과"}


class ContactFail(Exception):
    """접촉 찾기 실패. code: none(안 닿음) · short(금속 판 · 단락) · four(4단자 확인 실패) · inst(K2450 오류) · step(걸음 한도를 넘는 이동 요청).
    문구는 사람이 읽는 한 문장."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def params(cfg):
    return {k: cfg["contact_" + k] for k in KEYS}


def step_floor(mm, one_mm):
    """간격(mm)을 펄스 단위로 내림한 값(mm). 한 펄스보다 작으면 한 펄스. 실제 이동이 설정한 간격을 넘지 않게 한다."""
    return max(1, int(math.floor(float(mm) / one_mm + 1e-9))) * one_mm


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


def four_ok(r4, r2, ovp, min_ratio):
    """4단자 값이 정상인가: 과전압 보호가 안 걸렸고 양수이며 2단자보다 작고 2단자 × min_ratio 보다 크다
    (그보다 작으면 감지 핀이 떠 출력이 올라간 것으로 본다 - 열린 감지선)."""
    return (not ovp) and r4 is not None and r2 is not None and 0 < r4 < r2 and r4 >= float(min_ratio) * r2


def limits(mode, z_ref, p, z_max, one_mm):
    """(시작 Z, 내려갈 수 있는 한도 Z). one_mm = 한 펄스(mm)."""
    if mode == "height":
        z_ref = float(z_ref)
        return max(one_mm, z_ref - float(p["start_above_mm"])), min(float(z_max), z_ref + float(p["max_below_mm"]))
    z0 = max(0.0, float(z_ref or 0.0))
    return max(one_mm, z0), min(float(z_max), z0 + float(p["search_mm"]))


def make_sense(k2450, p, mode, floor, one_mm, on_step=None):
    """멈출 때마다 부르는 판단 함수 sense(z) → 더 움직일 거리 dz(mm, 양수 = 아래) | None(끝) 과 진행 상태 st 를 만든다.
    st["max_down"]() = 지금 단계에서 아래로 한 번에 갈 수 있는 최대 거리(mm) - 움직이는 쪽이 걸음마다 확인한다."""
    st = {"phase": "coarse" if mode == "search" else "2w", "streak": 0, "first": None, "first_z": None, "r2": None, "r4": None, "n": 0, "four": False,
          "points": [], "target": None}
    volt = float(p["volt"])
    step = step_floor(p["step_mm"], one_mm)                        # 촘촘한 걸음(펄스 단위로 내림)
    coarse = step_floor(p["search_step_mm"], one_mm)               # 찾기 모드의 첫 하강 걸음
    st["max_down"] = lambda: coarse if st["phase"] == "coarse" else step

    def read():
        try:
            return k2450.read_strict()
        except meters.VisaError as e:
            raise ContactFail("저항계 읽기 실패 · %s" % str(e)[:40], "inst")

    def down(z, d):
        """d 만큼 더 내리는 요청 - 한도를 넘으면 움직이지 않고 실패."""
        if z + d > floor + 1e-9:
            if st["phase"] == "4w":
                raise ContactFail("4핀 접촉 실패 · Z %.2f mm" % floor, "four")
            raise ContactFail("미접촉 · Z %.2f mm" % floor, "none")
        return d

    def note(z, kind, ohm):
        st["points"].append([round(z, 4), kind, ohm])
        if on_step:
            on_step(z, kind, ohm)

    def press(z):
        """더 누름: 목표(첫 접촉 + overdrive, 한도 안)까지 남았으면 한 걸음 이하로 내리고, 다 왔으면 4단자 확인으로 넘어간다."""
        left = st["target"] - z
        if left > one_mm / 2.0:
            return min(step, left)
        st["phase"] = "4w"
        return 0.0

    def sense(z):
        st["n"] += 1
        if st["n"] > int(p["max_reads"]):
            raise ContactFail("미접촉 · 측정 횟수 초과", "none")
        if st["phase"] in ("coarse", "2w", "press"):
            cur, trip = read()
            kind, r = classify(cur, trip, volt, p)
            note(z, kind, r)
            if kind == "bad":
                raise ContactFail("저항계 값 이상", "inst")
            if kind == "short":
                raise ContactFail("금속 판 접촉 · Z %.2f mm" % z, "short")
            if st["phase"] == "press":                       # 더 누르는 중: 걸음마다 단락만 확인하며 목표까지
                if kind == "contact":
                    st["r2"] = r
                return press(z)
            d = coarse if st["phase"] == "coarse" else step
            if kind == "open":
                st["streak"], st["first"] = 0, None
                return down(z, d)
            if st["streak"] == 0:
                st["first"] = z
            st["streak"] += 1
            st["r2"] = r
            if st["streak"] < int(p["confirm_n"]):
                return down(z, d)
            if st["phase"] == "coarse":                      # 처음 찾기: 닿은 자리에서 올렸다가(위로 가는 이동) 촘촘히 다시
                st["phase"], st["streak"], st["first"] = "2w", 0, None
                back = min(float(p["search_back_mm"]), z - one_mm)
                return -back if back > 0 else 0.0
            st["first_z"] = st["first"]
            st["target"] = min(st["first"] + float(p["overdrive_mm"]), floor)      # 최종 Z = 첫 접촉 + 더 누름(한도 안). 이미 그보다 내려와 있으면 그대로
            st["phase"] = "press"
            return press(z)
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
            raise ContactFail("저항계 읽기 실패 · %s" % str(e)[:40], "inst")
        kind, r4 = classify(cur, trip, volt, p)
        note(z, "4w:" + kind + (":ovp" if ovp else ""), r4)
        if kind == "short":
            raise ContactFail("금속 판 접촉 · Z %.2f mm" % z, "short")
        if kind == "bad":
            raise ContactFail("저항계 값 이상", "inst")
        if kind == "contact" and four_ok(r4, st["r2"], ovp, p["four_min_ratio"]):
            st["r4"] = r4
            return None
        return down(z, step)

    return sense, st
