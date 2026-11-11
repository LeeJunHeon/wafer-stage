"""contact.py - 전기로 접촉을 확인하며 핀 내리기(측정 단계 '접촉 높이로 내리기' · 측정 탭 [접촉 높이로 내리기] · 장비 설정 7 [전기로 접촉 찾기] 공용).

저장한 접촉 높이(contact_z_mm)는 "대략 여기쯤" 이다. 판이 기울었거나 샘플 두께가 다르면 안 닿거나 너무 눌린다. 그래서
  1) 저장 높이 - start_above_mm 까지는 지금처럼 내리고(보통 속도 → 느린 속도),
  2) K2450 을 접촉 확인용으로 설정(2단자 · contact.volt · contact.ilim_a · 과전압 보호 가장 낮게 · 자동 범위 · 평균 끔)하고 출력을 켠 뒤,
  3) step_mm 씩 느리게 내리며 멈출 때마다 한 번 잰다. 열린 회로도 단락도 아닌 값이 confirm_n 번 연속이면 접촉(첫 접촉 Z = 그 연속의 첫 자리),
  4) 첫 접촉 + overdrive_mm 까지 누르고 4단자로 한 번 더 잰다 - 정상(과전압 보호 안 걸림 · 양수 · 2단자보다 작고 1/10 보다 큼 · 열린 회로 아님)이면 네 핀 모두 닿음.
     아니면 step_mm 씩 더 내리며 4단자를 다시 잰다(한도 안에서만).
  5) 단락(전류 한도 걸림 · 2단자 < short_ohm) = 핀이 샘플 밖 금속 판에 닿음 → 멈추고 Z 맨 위 · 실패.
  6) 저장 높이 + max_below_mm 까지 내려도 접촉이 없으면 더 내리지 않고 Z 맨 위 · 실패.
  7) 어떤 끝에서도 K2450 출력 끔. 측정 단계는 자기 설정을 다시 한다(*RST 부터).
Z 이동은 전부 Motion.probe_down(이동 잠금 하나 · 쓰기 직전 세대 · 시간 한도 · jog_guard · 한도 아래로 안 내림)을 지난다.
"""

import time

import logger
from core import instruments as inst
from core.motion import MotionError

KEYS = ["sense", "volt", "ilim_a", "nplc", "step_mm", "start_above_mm", "max_below_mm", "confirm_n", "overdrive_mm", "short_ohm", "search_mm"]
SEARCH_STEP_MM = 0.05            # 장비 설정 7 · 접촉 높이가 없을 때 처음 찾는 간격
SEARCH_BACK_MM = 0.3             # 처음 찾기에서 닿으면 이만큼 올렸다가 step_mm 간격으로 다시
FOUR_MIN_RATIO = 0.1             # 4단자가 2단자의 1/10 보다 작으면 감지 핀이 떠 출력이 올라간 것으로 본다(열린 감지선 - 정상 아님)
MAX_READS = 2000                 # 한 번 찾기의 읽기 상한(한도 계산이 틀려도 끝없이 돌지 않게)
NO_SENSE = "K2450 연결 필요(접촉 확인)"


class ContactFail(Exception):
    """접촉 찾기 실패. code: none(접촉 못 함) · short(금속 판 · 단락) · four(4단자 확인 실패) · inst(K2450 오류)."""

    def __init__(self, msg, code):
        super().__init__(msg)
        self.code = code


def params(cfg):
    p = {k: cfg.get("contact." + k) for k in KEYS}
    p["open_a"] = cfg.get("rt.open_a")
    p["terminal"] = cfg.get("rt.terminal")
    return p


def sense_on(cfg):
    return bool(cfg.get("contact.sense"))


def block(app):
    """접촉 확인이 켜져 있는데 K2450 이 없으면 사유(내리지 않는다). 꺼져 있으면 ""."""
    if not sense_on(app.cfg):
        return ""
    k = app.k2450
    if k is None or not k.is_open:
        return NO_SENSE
    return ""


def classify(I, trip, volt, p):
    """(kind, 저항 Ω | None). kind: open(전류가 rt.open_a 아래) · short(전류 한도 걸림 · 저항 < short_ohm) · contact · bad(값 없음)."""
    if trip:
        return "short", None
    if I is None:
        return "bad", None
    a = abs(float(I))
    if a < float(p["open_a"]):
        return "open", None
    R = abs(float(volt)) / a
    if R < float(p["short_ohm"]):
        return "short", R
    return "contact", R


def four_ok(R4, r2, ovp):
    """4단자 값이 정상인가: 과전압 보호가 안 걸렸고 양수이며 2단자보다 작고 2단자의 1/10 보다 크다."""
    return (not ovp) and R4 is not None and r2 is not None and 0 < R4 < r2 and R4 >= FOUR_MIN_RATIO * r2


def find(app, gen, mode="height", z_ref=None, check_xy=None, nowait=False):
    """접촉 찾기. mode "height": z_ref = 저장한 접촉 높이(시작 z_ref - start_above · 한도 z_ref + max_below).
    mode "search": z_ref = 시작 Z(지금 Z) · 0.05 mm 간격으로 z_ref + search_mm 까지 → 닿으면 0.3 mm 올렸다가 step_mm 간격으로 다시(같은 한도).
    반환 {mode, first_z, final_z, r2, r4, points[(z, kind, Ω)], start_z, floor_z}. 실패는 ContactFail(이때 Z 맨 위 · 출력 끔).
    정지 · 비상정지 · 이동 시간 초과는 MotionError 그대로(출력은 끄고 Z 는 움직이지 않는다)."""
    p = params(app.cfg)
    k = app.k2450
    if k is None or not k.is_open:
        raise ContactFail(NO_SENSE, "inst")
    from core import stage as stage_mod
    zmax = float(stage_mod.limits_mm()["z_max_mm"])
    step = float(p["step_mm"])
    one = 1.0 / stage_mod.Z_PPMM
    if mode == "height":
        z_ref = float(z_ref)
        z_start = max(one, z_ref - float(p["start_above_mm"]))
        floor = min(zmax, z_ref + float(p["max_below_mm"]))
    else:
        z0 = max(0.0, float(z_ref or 0.0))
        z_start = max(one, z0)
        floor = min(zmax, z0 + float(p["search_mm"]))
    st = {"phase": "coarse" if mode == "search" else "2w", "streak": 0, "first": None, "r2": None, "r4": None, "n": 0, "four": False,
          "first_z": None, "points": []}
    volt = float(p["volt"])

    def read():
        try:
            return k.read_strict()
        except inst.VisaError as e:
            raise ContactFail("K2450 읽기 오류 · " + logger.short(e, 40), "inst")

    def down(z, d):
        """d 만큼 더 내리는 요청 - 한도를 넘으면 실패(움직이지 않는다)."""
        if z + d > floor + 1e-9:
            if st["phase"] == "4w":
                raise ContactFail("네 핀이 다 닿지 않음 · 4단자 확인 실패 · 한도 Z %.3f" % floor, "four")
            raise ContactFail("접촉 못 함 · Z %.3f 까지 내려도 전류 없음" % floor, "none")
        return d

    def sense(z):
        st["n"] += 1
        if st["n"] > MAX_READS:
            raise ContactFail("접촉 못 함 · 읽기 횟수 넘음", "none")
        if st["phase"] in ("coarse", "2w"):
            I, trip = read()
            kind, R = classify(I, trip, volt, p)
            st["points"].append([round(z, 4), kind, R])
            if kind == "bad":
                raise ContactFail("K2450 값 이상(범위 넘음) · 내리지 않음", "inst")
            if kind == "short":
                raise ContactFail("핀이 샘플 밖 금속에 닿음 · 단락 Z %.3f" % z, "short")
            d = SEARCH_STEP_MM if st["phase"] == "coarse" else step
            if kind == "open":
                st["streak"], st["first"] = 0, None
                return down(z, d)
            if st["streak"] == 0:
                st["first"] = z
            st["streak"] += 1
            st["r2"] = R
            if st["streak"] < int(p["confirm_n"]):
                return down(z, d)
            if st["phase"] == "coarse":                      # 처음 찾기: 닿은 자리에서 0.3 mm 올렸다가 촘촘히 다시
                st["phase"], st["streak"], st["first"] = "2w", 0, None
                back = min(SEARCH_BACK_MM, z - one)
                return -back if back > 0 else 0.0
            st["first_z"] = st["first"]
            st["phase"] = "4w"
            od = min(st["first"] + float(p["overdrive_mm"]), floor) - z          # 최종 Z = 첫 접촉 + 더 누름(이미 확인하며 내려온 만큼은 빼고 · 한도 안)
            return od if od > 1e-9 else 0.0
        # 4단자 확인
        if not st["four"]:
            try:
                k.set_four_wire(True)
            except inst.VisaError as e:
                raise ContactFail("K2450 4단자 설정 오류 · " + logger.short(e, 40), "inst")
            st["four"] = True
        I, trip = read()
        try:
            ovp = k.ovp_tripped(strict=True)
        except inst.VisaError as e:
            raise ContactFail("K2450 읽기 오류(과전압 보호) · " + logger.short(e, 40), "inst")
        kind, R4 = classify(I, trip, volt, p)
        st["points"].append([round(z, 4), "4w:" + kind + (":ovp" if ovp else ""), R4])
        if kind == "short":
            raise ContactFail("핀이 샘플 밖 금속에 닿음 · 단락(4단자) Z %.3f" % z, "short")
        if kind == "bad":
            raise ContactFail("K2450 값 이상(4단자) · 내리지 않음", "inst")
        if kind == "contact" and four_ok(R4, st["r2"], ovp):
            st["r4"] = R4
            return None
        return down(z, step)

    t0 = time.time()
    res = None
    fail = None
    try:
        try:
            k.configure_contact({"terminal": p["terminal"], "volt": volt, "ilim_a": p["ilim_a"], "nplc": p["nplc"]})
            k.output(True)
        except inst.VisaError as e:
            raise ContactFail("K2450 접촉 확인 설정 오류 · " + logger.short(e, 40), "inst")
        status, zs = app.motion.probe_down(z_start, floor, sense, gen=gen, check_xy=check_xy, nowait=nowait,
                                           approach_mm=float(app.cfg.get("approach_mm")))
        res = {"mode": mode, "first_z": round(st["first_z"], 4), "final_z": round(zs[-1], 4), "r2": st["r2"], "r4": st["r4"],
               "start_z": round(z_start, 4), "floor_z": round(floor, 4), "points": st["points"], "s": round(time.time() - t0, 1),
               "x": status.get("x_mm"), "y": status.get("y_mm"), "gen": gen}
        return res
    except ContactFail as e:
        fail = e
        raise
    except MotionError as e:
        if e.code == "floor":                                # 한도를 넘는 요청 - 이미 down() 이 막지만 혹시 다른 길로 오면 같은 실패로
            fail = ContactFail("접촉 못 함 · 한도 Z %.3f" % floor, "none")
            raise fail from e
        raise
    finally:
        _out_off(app, k)
        if fail is not None:
            logger.log("접촉 찾기 실패 · %s" % fail, "warn")
            m = app.motion
            if m is not None and m.gen == gen and m.z_ok and not m.estop_locked:   # 정지 · 비상정지로 끝난 것은 움직이지 않는다
                try:
                    m.move_z(0.0, gen=gen)
                except Exception as e2:                          # noqa: BLE001
                    logger.log("접촉 찾기 실패 뒤 Z 올림 실패 · " + logger.short(e2), "err")
        elif res is not None:
            logger.log("접촉 찾기 · 첫 접촉 Z %.3f · 최종 Z %.3f · 2단자 %s · 4단자 %s" % (res["first_z"], res["final_z"], fmt_ohm(res["r2"]), fmt_ohm(res["r4"])))


def _out_off(app, k):
    """K2450 출력 끄기(두 번까지). 못 끄면 통신이 돌아올 때까지 앱이 다시 끈다."""
    for i in range(2):
        try:
            k.output(False)
            return
        except Exception:                                    # noqa: BLE001
            time.sleep(0.2)
    try:
        import threading
        threading.Thread(target=app._inst_off_retry, name="contact-out-off", daemon=True).start()
    except Exception:                                        # noqa: BLE001
        pass


def fmt_ohm(r):
    return "—" if r is None else "%.3E Ω" % r
