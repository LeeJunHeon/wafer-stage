"""e2e_contact.py - 전기로 접촉 확인하며 핀 내리기 e2e 86~93 (tools/e2e.py 가 불러온다). dry · fake-serial, **실제 속도**, 가짜 K2450 접촉 흉내.

가짜 K2450(tools/fake_visa.py lab.probe)이 스테이지의 실제 X · Y · Z 를 보고 답한다: 샘플마다 표면 높이 · 크기 · 저항, 샘플 밖은 알루미늄 판(단락),
FORCE 두 핀이 표면에서 먼저 닿고 SENSE 두 핀은 lag4 만큼 더 내려가야 닿는다(감지 핀이 떠 있으면 4단자 = 과전압 보호 · 이상값).

  86 샘플 셋 표면 ±0.2 mm(측정 단계 '접촉 높이로 내리기'): 첫 접촉 Z = 표면 ±1 스텝 · 최종 Z = 첫 접촉 + 더 누름 · 결과 · 명령 순서 · 한도 아래 펄스 0
  87 두 핀만 먼저 닿음 → 4단자 확인에서 더 내려 네 핀이 닿은 뒤 성공
  88 안 닿음(표면이 한도 아래): 한도에서 멈춤 · Z 맨 위 · 그 샘플 오류 · 한도 아래로 내려간 펄스 0
  89 샘플 밖(금속 판): 단락으로 멈춤 · Z 맨 위 · 실패
  90 찾는 중 정지 · 비상정지: '!' 가 가장 먼저 · 남은 스텝 안 나감 · K2450 출력 꺼짐
  91 찾는 중 K2450 끊김 · 응답 지연(VISA 시간 초과): 더 내리지 않고 Z 맨 위 · 실패 · 출력 끔(돌아오면)
  92 접촉 확인 끔 = 지금 동작 그대로(저장 높이까지만 · K2450 없어도) / 켬 + K2450 없음 = 내리지 않고 거절
  93 장비 설정 7 [전기로 접촉 찾기]: 접촉 높이 없음 → 찾기 → 저장 값 = 표면 ±1 스텝 · 있으면 같은 범위 · 수동 측정 중 거절
"""
import time

import e2e
from e2e import case, check
from e2e_cases import ready_capture, serial_cmds, start_run, wait_run

REAL = {"WAFER_STAGE_DRY_MOVE_S": "real", "WAFER2_FAKE_SCALE": "1.0"}
PATCH = {"contact": {"sense": True}, "contact_z_mm": 20.0, "jog": {"z_max_mm": 45.0}}
STEP = 0.025
OD = 0.05
FLOOR = 20.0 + 0.5


def setup(fn):
    fn.env = REAL
    fn.patch = PATCH
    return fn


def probe(c, surf, lag4=None, plate_z=21.0, ohm=1.0e6):
    """샘플 표면 · 크기를 가짜 K2450 에 준다. surf: {번호: 표면 Z} (없으면 마지막 값). 반환 {번호: 샘플}."""
    sm = {}
    for i, p in enumerate(c.state["samples"]):
        z = surf.get(p["no"], 30.0)
        sm[p["no"]] = {"x": p["X"], "y": p["Y"], "half": 3.0, "z": z, "ohm": ohm * (1 + 0.5 * i), "lag4": (lag4 or {}).get(p["no"], 0.0)}
    ok, r = c.dry("lab", set={"probe": {"plate_z": plate_z, "rc": 2.0e3, "samples": list(sm.values())}})
    check(ok, "가짜 K2450 접촉 흉내 설정 (%s)" % ((r or {}).get("err") if isinstance(r, dict) else r,))
    return sm


def mz_cmds(cmds):
    out = []
    for x in cmds:
        if x.startswith("mz "):
            try:
                out.append(float(x.split()[1]))
            except ValueError:
                pass
    return out


def prep(c):
    ready_capture(c)
    ok, r = c.send("inst_connect")
    check(ok, "장비 연결 (%s)" % (r,))
    return [p["no"] for p in c.state["samples"]]


def run_one(c, nos):
    ok, r = start_run(c, "접촉 시험", nos=nos)
    check(ok, "측정 시작 (%s)" % (r,))
    check(wait_run(c, 600), "측정 끝")
    c.pump(0.8)
    return c.state["run"]["results"]


def lab(c, **kw):
    ok, r = c.dry("lab", **kw)
    return r if ok and isinstance(r, dict) else {}


def zpos(c):
    c.pump(0.6)
    return c.state["stage"]["z"]


@case("86 접촉 찾기 · 샘플 셋 표면 ±0.2 mm · 첫 접촉 = 표면 ±1 스텝 · 최종 = 첫 접촉 + 더 누름 · 명령 순서")
@setup
def c86(c):
    nos = prep(c)
    use = nos[:3]
    surf = {use[0]: 20.0, use[1]: 20.2, use[2]: 19.8}
    sm = probe(c, surf)
    n0 = len(serial_cmds(c))
    res = run_one(c, use)
    for no in use:
        r = res.get(str(no)) or res.get(no) or {}
        z = surf[no]
        fz, fin = r.get("first_z"), r.get("final_z")
        check(r.get("status") == "완료" and fz is not None and z - 1e-6 <= fz <= z + STEP + 1e-6,
              "샘플 %d · 첫 접촉 Z %s = 표면 %.3f ±1 스텝 (%s)" % (no, fz, z, r.get("reason")))
        check(fin is not None and fz is not None and abs(fin - (fz + OD)) < 1e-6, "샘플 %d · 최종 Z %s = 첫 접촉 + %.2f" % (no, fin, OD))
        check(r.get("r2") and r.get("r4") and r["r4"] < r["r2"] and abs(r["r4"] - sm[no]["ohm"]) < sm[no]["ohm"] * 0.02,
              "샘플 %d · 2단자 %s > 4단자 %s ≈ 시료 %.2E" % (no, r.get("r2"), r.get("r4"), sm[no]["ohm"]))
    zs = mz_cmds(serial_cmds(c)[n0:])
    check(zs and max(zs) <= max(surf.values()) + OD + 1e-6 and max(zs) <= FLOOR + 1e-6, "한도 아래로 내려간 펄스 0 (가장 깊은 Z %.4f)" % (max(zs) if zs else -1))
    cmds = lab(c).get("cmds", [])
    if ":OUTP ON" in cmds:
        pre = cmds[:cmds.index(":OUTP ON")]
        check(":SOUR:VOLT:PROT PROT2" in pre and ":SOUR:VOLT:ILIM 0.0001" in pre and ":SOUR:VOLT 0.1" in pre and ":SENS:CURR:RSEN OFF" in pre,
              "출력 켜기 전에 과전압 보호 2 V · 한도 0.1 mA · 0.1 V · 2단자 (%s)" % pre[-12:])
    outs = [x for x in cmds if x.startswith(":OUTP")]
    check(outs and outs[-1] == ":OUTP OFF" and not lab(c).get("out"), "끝나면 K2450 출력 꺼짐")
    csv = open(c.state["run"]["dir"] + "/samples.csv", encoding="utf-8-sig").read().splitlines()[0]
    check("첫 접촉 Z" in csv and "4단자 Ω" in csv, "결과 CSV 에 첫 접촉 Z · 최종 Z · 2 · 4단자 (%s)" % csv)


@case("87 접촉 찾기 · 두 핀만 먼저 닿음 → 4단자 확인에서 더 내려 네 핀 닿은 뒤 성공")
@setup
def c87(c):
    nos = prep(c)
    no = nos[0]
    probe(c, {no: 20.0}, lag4={no: 0.1})
    res = run_one(c, [no])
    r = res.get(str(no)) or res.get(no) or {}
    check(r.get("status") == "완료", "성공 (%s · %s)" % (r.get("status"), r.get("reason")))
    check(r.get("first_z") is not None and abs(r["first_z"] - 20.0) <= STEP + 1e-6, "첫 접촉 Z %s = 두 핀이 닿은 표면" % r.get("first_z"))
    check(r.get("final_z") is not None and r["final_z"] >= 20.1 - 1e-6, "최종 Z %s ≥ 네 핀이 닿는 20.1" % r.get("final_z"))


@case("88 접촉 찾기 · 안 닿음(표면이 한도 아래) → 한도에서 멈춤 · Z 맨 위 · 오류 · 한도 아래 펄스 0")
@setup
def c88(c):
    nos = prep(c)
    no = nos[0]
    probe(c, {no: 20.8})
    n0 = len(serial_cmds(c))
    res = run_one(c, [no])
    r = res.get(str(no)) or res.get(no) or {}
    check(r.get("status") == "오류" and "접촉 못 함" in (r.get("reason") or ""), "그 샘플 오류 · 접촉 못 함 (%s)" % r.get("reason"))
    zs = mz_cmds(serial_cmds(c)[n0:])
    check(zs and max(zs) <= FLOOR + 1e-6, "한도 Z %.2f 아래로 내려간 펄스 0 (가장 깊은 Z %.4f)" % (FLOOR, max(zs) if zs else -1))
    check(abs(zpos(c)) < 0.01, "Z 맨 위 (%s)" % c.state["stage"]["z"])
    check(not lab(c).get("out"), "K2450 출력 꺼짐")


@case("89 접촉 찾기 · 샘플 밖(금속 판) → 단락으로 멈춤 · Z 맨 위 · 실패")
@setup
def c89(c):
    prep(c)
    probe(c, {}, plate_z=20.1)
    ok, r = c.send("move_xy", x=15.0, y=15.0)
    check(ok, "샘플 밖으로 이동 (%s)" % (r,))
    ok, r = c.send("contact_try", free=True, timeout=120)
    check(not ok and "금속" in str(r), "단락 → 실패 '핀이 샘플 밖 금속에 닿음' (%s)" % (r,))
    check(abs(zpos(c)) < 0.01, "Z 맨 위 (%s)" % c.state["stage"]["z"])
    check(not lab(c).get("out"), "K2450 출력 꺼짐")


def _search_then(c, act):
    """찾기를 시작해 K2450 출력이 켜지고 몇 스텝 내려간 뒤 act() 한다. 반환 (ack, act 직전 명령 수, 그때 Z)."""
    i = c.send_nowait("contact_try", free=True)
    t0 = time.time()
    while time.time() - t0 < 60:
        c.pump(0.15)
        if lab(c).get("out") and len([z for z in mz_cmds(serial_cmds(c)) if z > 19.75]) >= 2:
            break
    n_at = len(serial_cmds(c))
    act()
    ok, r = c.wait_ack(i, 60)
    return ok, r, n_at


@case("90 접촉 찾기 중 정지 · 비상정지 → '!' 가 가장 먼저 · 남은 스텝 안 나감 · K2450 출력 꺼짐")
@setup
def c90(c):
    nos = prep(c)
    no = nos[0]
    probe(c, {no: 20.45})
    lab(c, set={"read_dt": 15.0})                            # 한 번 읽기 = 시뮬레이션 15 s(배율 60 → 0.25 s) - 찾기가 몇 초 걸리게
    s0 = c.state["samples"][0]
    for what in ("stop", "estop"):
        ok, r = c.send("move_xy", x=s0["X"], y=s0["Y"])
        check(ok, "[%s] 샘플 위로 (%s)" % (what, r))
        ok, r, n_at = _search_then(c, lambda: c.http("/%s" % what, "POST"))
        after = serial_cmds(c)[n_at:]
        bang = [k for k, x in enumerate(after) if x.startswith("!")]
        moves_after = [x for x in after[(bang[0] + 1) if bang else 0:] if x.startswith(("mz", "mx", "my", "jz", "gz"))]
        check(not ok and ("정지" in str(r) or "비상정지" in str(r)), "[%s] 찾기가 중단됨 (%s)" % (what, r))
        check(bang and len([x for x in after[:bang[0]] if x.startswith(("mz", "jz"))]) <= 1,
              "[%s] '!' 가 가장 먼저(누른 순간 이미 나가던 한 스텝 말고는 앞에 이동 없음) (%s)" % (what, after[:4]))
        check(not moves_after, "[%s] '!' 뒤 이동 줄 0 (%s)" % (what, moves_after[:3]))
        c.pump(1.5)
        check(not lab(c).get("out"), "[%s] K2450 출력 꺼짐" % what)
        if what == "stop":
            time.sleep(0.6)
    check(c.state["stage"]["locked"], "비상정지 잠금")


@case("91 접촉 찾기 중 K2450 끊김 · 응답 지연 → 더 내리지 않고 Z 맨 위 · 실패 · 출력 끔")
@setup
def c91(c):
    nos = prep(c)
    no = nos[0]
    probe(c, {no: 20.45})
    lab(c, set={"read_dt": 15.0})
    s0 = c.state["samples"][0]
    for name, fault in (("끊김", {"disconnect": True}), ("응답 지연 6 s", {"slow": 6.0})):
        ok, r = c.send("move_xy", x=s0["X"], y=s0["Y"])
        check(ok, "[%s] 샘플 위로 (%s)" % (name, r))
        holder = {}

        def act():
            holder["z"] = c.state["stage"]["z"]
            lab(c, fault=fault)
        ok, r, n_at = _search_then(c, act)
        lab(c, fault={"disconnect": False, "slow": 0.0})
        check(not ok and "K2450" in str(r), "[%s] 실패 (%s)" % (name, r))
        zs = mz_cmds(serial_cmds(c)[n_at:])
        deeper = [z for z in zs if z > 19.75 + 0.3]
        check(not deeper, "[%s] 더 내리지 않음 (%s)" % (name, deeper[:3]))
        check(abs(zpos(c)) < 0.01, "[%s] Z 맨 위 (%s)" % (name, c.state["stage"]["z"]))
        t0 = time.time()
        while time.time() - t0 < 6 and lab(c).get("out"):
            c.pump(0.5)
        check(not lab(c).get("out"), "[%s] K2450 출력 꺼짐(통신이 돌아오면 다시 끔)" % name)
        c.send("inst_connect")


@case("92 접촉 확인 끔 = 지금 동작 그대로 · 켬 + K2450 없음 = 거절")
@setup
def c92(c):
    ready_capture(c)
    s0 = c.state["samples"][0]
    ok, r = c.send("move_xy", x=s0["X"], y=s0["Y"])
    ok, r = c.send("contact_try", free=True)
    check(not ok and "K2450 연결 필요(접촉 확인)" in str(r), "켬 · K2450 없음 → 내리지 않고 거절 (%s)" % (r,))
    check(abs(zpos(c)) < 0.01, "Z 그대로 맨 위")
    ok, r = start_run(c, "접촉 시험", nos=[s0["no"]])
    check(not ok and "K2450" in str(r), "측정 시작도 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"contact.sense": False})
    check(ok and not c.state["contact"]["sense"], "접촉 확인 끔")
    n0 = len(serial_cmds(c))
    ok, r = c.send("contact_try", free=True)
    check(ok and r.get("sense") is False and abs(zpos(c) - 20.0) < 0.01, "끔 → 저장 높이 20.00 까지만 (%s · Z %s)" % (r, c.state["stage"]["z"]))
    zs = mz_cmds(serial_cmds(c)[n0:])
    check(zs and max(zs) <= 20.0 + 1e-6, "저장 높이 아래로 안 내려감 (%s)" % zs[-3:])
    check(c.state["start_block"].get("notes") is not None, "시작 안내")
    c.send("z_top")
    ok, r = start_run(c, "접촉 시험", nos=[s0["no"]])
    check(ok, "끔 · K2450 없이 측정 시작 (%s)" % (r,))
    check(wait_run(c, 300), "측정 끝")
    rr = c.state["run"]["results"]
    r0 = rr.get(str(s0["no"])) or rr.get(s0["no"]) or {}
    check(r0.get("status") == "완료" and r0.get("first_z") is None, "끔 · 완료 · 첫 접촉 기록 없음 (%s)" % r0)


@case("93 장비 설정 7 [전기로 접촉 찾기] · 저장 값 = 표면 ±1 스텝 · 수동 측정 중 거절")
@setup
def c93(c):
    nos = prep(c)
    no = nos[0]
    probe(c, {no: 20.0})
    c.send("contact_clear")
    check(c.state["contact"]["z"] is None, "접촉 높이 비움")
    ok, r = c.send("goto_sample", no=no)
    check(ok, "샘플 위로 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=17.0, free=True)                  # 사람이 표면 근처(5 mm 안)까지 조그로 내린다
    check(ok, "Z 17 mm 까지 조그 (%s)" % (r,))
    ok, r = c.send("contact_find", no=no, timeout=300)
    check(ok and r.get("mode") == "search" and r.get("first_z") is not None and 20.0 - 1e-6 <= r["first_z"] <= 20.0 + STEP + 1e-6,
          "찾기(접촉 높이 없음) · 첫 접촉 Z %s = 표면 ±1 스텝" % (r.get("first_z") if isinstance(r, dict) else r))
    if ok:
        ok2, r2 = c.send("contact_save", z=r["first_z"], no=no)
        check(ok2 and abs(c.state["contact"]["z"] - r["first_z"]) < 1e-6, "저장 값 = 찾은 높이 (%s)" % c.state["contact"]["z"])
        ok3, r3 = c.send("contact_save", z=r["first_z"] + 0.3, no=no)
        check(not ok3, "찾은 높이가 아닌 값은 저장 거절 (%s)" % (r3,))
    c.send("z_top")
    ok, r = c.send("contact_find", no=no, timeout=300)
    check(ok and r.get("mode") == "height" and abs(r["first_z"] - 20.0) <= STEP + 1e-6, "접촉 높이 있음 → 같은 범위로 찾기 (%s)" % (r,))
    c.send("z_top")
    ok, r = c.send("man_cont")
    check(ok, "수동 연속 측정 시작 (%s)" % (r,))
    ok, r = c.send("contact_find", no=no)
    check(not ok and "수동 측정 중" in str(r), "수동 측정 중 접촉 찾기 거절 (%s)" % (r,))
    ok, r = c.send("contact_try", no=no)
    check(not ok and "수동 측정 중" in str(r), "수동 측정 중 접촉 높이로 내리기 거절 (%s)" % (r,))
    c.send("man_stop")
    c.wait(lambda s: s["manual"]["run"] and s["manual"]["run"]["state"] in ("stopped", "done", "error"), 30, "정지")
