"""e2e_rt.py - R–T 측정 e2e 시나리오 31~ (tools/e2e.py 가 불러온다). dry · fake-serial 두 모드에서 가짜 측정 장비(tools/fake_visa.py) + 시간 압축.

매번 확인: 히트건 꺼짐 · K2450 출력 꺼짐(가짜 장비의 out) · (오류 · 중지) 쿨러 꺼짐.
"""
import json
import os
import shutil
import tempfile
import time

import e2e
from e2e import case, check, full_setup
from e2e_cases import after_estop, serial_cmds, recover, start_run, wait_run, follow_band

RT = {"max_temp": 45.0, "break_temp": 30.0, "repeat": 3, "precool_c": 26.0, "cooler_target": "all", "file_path": ""}
FAST = {"sim": {"time_scale": 120.0}}


def patch(rt=None, sim=None, **kw):
    d = {"rt": dict(RT, **(rt or {})), "sim": sim or {"time_scale": 120.0}}
    d.update(kw)
    return d


def lab(c, **kw):
    ok, r = c.dry("lab", **kw)
    return r if ok else {}


def at_contact(c, x=60.0, y=60.0):
    ok1, r1 = c.send("inst_connect")
    ok2, r2 = c.send("move_xy", x=x, y=y)
    ok3, r3 = c.send("contact_try", free=True)
    check(ok1 and ok2 and ok3, "장비 연결 · 이동 · 접촉 높이로 내림 (%s %s %s)" % (r1, r2, r3))
    c.pump(1.2)


def wait_rt(c, timeout=120):
    return c.wait(lambda s: s["rt"]["run"] and s["rt"]["run"]["state"] not in ("running", "stopping", "ready"), timeout, "R–T 측정 끝")


def wait_phase(c, ph, timeout=60):
    return c.wait(lambda s: s["rt"]["run"] and s["rt"]["run"]["phase"] == ph and s["rt"]["run"]["state"] == "running", timeout, ph)


def safe_off(c, what, check_out=True):
    c.pump(0.8)
    st = lab(c)
    check(not c.state["relays"]["heater_on"], what + " · 히트건 꺼짐")
    if check_out:
        check(st.get("out") is False, what + " · K2450 출력 꺼짐")


def last_heater_cmd_is_off(c):
    cmds = [x for x in serial_cmds(c) if x.startswith("r 10 ") or x.startswith("! (abort)")]
    return bool(cmds) and (cmds[-1] == "r 10 0" or cmds[-1].startswith("! (abort)"))


@case("31 R–T 이 위치 측정 3회 · 파일 · 제어 기록 · NAS · 결과 탭")
def c31(c):
    nas = os.path.join(c.tmp, "nas", "VO2 data")
    # 이 시나리오는 patch 로 NAS 경로를 받을 수 없어(임시 폴더가 Client 안에서 정해짐) 설정 저장으로 넣는다
    ok, r = c.send("settings_save", patch={"rt.file_path": nas, "rt.cooler_target": "sample"})
    check(ok, "NAS 경로 · 쿨러 샘플 구역 (%s)" % (r,))
    t = full_setup(c)
    c.send("capture")
    s = c.state["samples"][3]
    ok, r = c.send("goto_sample", no=s["no"])
    check(ok, "샘플 위로 (%s)" % (r,))
    at_contact(c, x=c.state["stage"]["x"], y=c.state["stage"]["y"])
    check(c.state["rt"]["block"] == "", "시작 조건 충족 (%s)" % c.state["rt"]["block"])
    ok, r = c.send("rt_start")
    check(ok, "측정 시작 (%s)" % (r,))
    c.pump(1.0)
    # 측정 중에는 이동 · 릴레이 · 순회 거절
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "R–T" in str(r), "측정 중 조그 거절 (%s)" % (r,))
    ok, r = c.send("z_top")
    check(not ok, "측정 중 Z 맨 위 거절 (%s)" % (r,))
    ok, r = c.send("relay", n=3, on=True)
    check(not ok, "측정 중 수동 릴레이 거절 (%s)" % (r,))
    ok, r = c.send("rt_start")
    check(not ok, "이중 시작 거절 (%s)" % (r,))
    check(wait_rt(c, 180), "측정 끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done" and run["status"] == "완료", "완료 (%s %s)" % (run["state"], run["message"]))
    check(run["cycle"] == 3 and run["repeat"] == 3, "3회차")
    d = run["dir"]
    names = sorted(os.listdir(d))
    for n in ("1-1_1.00V_1.dat", "1-1_1.00V_2.dat", "1-1_1.00V_3.dat", "1-1_1.00V_merged.dat", "control.csv", "rt.json"):
        check(n in names, "PC 파일 " + n)
    c1 = open(os.path.join(d, "1-1_1.00V_1.dat"), encoding="utf-8", newline="").read().split("\r\n")
    check(c1[0] == "Temp\tResistance" and len(c1) > 50, "머리글 Temp<탭>Resistance · 행 %d" % len(c1))
    check(all(len(x.split("\t")) == 2 and "E" in x for x in c1[1:] if x), "값 %.7E 두 열")
    n_cyc = sum(len([x for x in open(os.path.join(d, "1-1_1.00V_%d.dat" % i), encoding="utf-8").read().splitlines()[1:] if x]) for i in (1, 2, 3))
    n_mrg = len([x for x in open(os.path.join(d, "1-1_1.00V_merged.dat"), encoding="utf-8").read().splitlines()[1:] if x])
    check(n_cyc == n_mrg, "합친 파일 행 = 회차 합 (%d / %d)" % (n_mrg, n_cyc))
    ctl = open(os.path.join(d, "control.csv"), encoding="utf-8-sig").read().splitlines()
    check(ctl[0] == "시각,경과 s,온도,목표 온도,저항,전류,한도 걸림,범위,히트건,쿨러,단계" and len(ctl) > 100, "제어 기록 열 · 행 %d" % len(ctl))
    check(any(x.split(",")[-1] == "예비 냉각" for x in ctl[1:]) and any(x.split(",")[-1] == "가열" for x in ctl[1:]) and any(x.split(",")[-1] == "냉각" for x in ctl[1:]), "단계 3종 기록")
    check(any(x.split(",")[8] == "1" for x in ctl[1:]) and any(x.split(",")[8] == "0" for x in ctl[1:]), "히트건 켜짐/꺼짐 기록")
    # NAS
    c.pump(0.5)
    nn = sorted(os.listdir(nas)) if os.path.isdir(nas) else []
    check(nn == sorted(["1-1_1.00V_1.dat", "1-1_1.00V_2.dat", "1-1_1.00V_3.dat", "1-1_1.00V_merged.dat"]), "NAS 에는 기존 파일만 (%s)" % nn)
    check("control.csv" not in nn, "제어 기록은 PC 에만")
    check(open(os.path.join(nas, "1-1_1.00V_2.dat"), "rb").read() == open(os.path.join(d, "1-1_1.00V_2.dat"), "rb").read(), "NAS 파일 = PC 파일")
    # 끝 상태
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    st = lab(c)
    check(st.get("out") is False, "K2450 출력 꺼짐")
    check(not [k for k, v in c.state["relays"]["owners"].items()], "쿨러 꺼짐 (%s)" % c.state["relays"]["owners"])
    c.pump(1.5)
    check(abs(c.state["stage"]["z"]) < 0.06, "Z 맨 위 (%s)" % c.state["stage"]["z"])
    # 쿨러는 샘플 구역(그 샘플의 쿨러)이었다
    cells = c.state["settings"]["cooler"]["zones"]["cells"]
    relay = [x["relay"] for x in cells if x.get("cooler") == s["cooler"]][0]
    cmds = serial_cmds(c)
    cool_on = [x for x in cmds if x.startswith("r ") and x.endswith(" 1") and x != "r 10 1"]
    check(len(cool_on) >= 1 and set(cool_on) == {"r %d 1" % relay}, "쿨러 켬 = 샘플 구역(쿨러 %s) 릴레이 R%s 하나 (%s)" % (s["cooler"], relay, sorted(set(cool_on))))
    # 결과 탭 목록
    res = c.state["rt"]["results"]
    check(res and res[0]["name"] == "1-1_" and res[0]["status"] in ("완료", "done") and res[0]["cycle"] == "3 / 3", "결과 목록 (%s)" % (res[:1],))
    code, body = c.http("/rt_data/" + res[0]["dir"])
    j = json.loads(body)
    check(len(j["cycles"]) == 3 and all(len(x["pts"]) > 50 for x in j["cycles"]), "그래프 데이터 3회차")
    code, body = c.http("/rt.json?since=0")
    pj = json.loads(body)
    check(pj["total"] > 100 and pj["points"][0]["ph"] == "예비 냉각", "그래프 점 전달 (%d)" % pj["total"])


@case("32 NAS 경로 없음 → 측정 계속 · 경고 · 다시 복사 / 같은 이름은 _1")
def c32(c):
    blocker = os.path.join(c.tmp, "blocker")
    open(blocker, "w").write("x")
    nas = os.path.join(blocker, "sub")                        # 부모가 파일이라 폴더를 못 만든다
    c.send("settings_save", patch={"rt.file_path": nas, "rt.repeat": 1})
    at_contact(c)
    ok, r = c.send("rt_start")
    check(ok, "시작")
    check(wait_rt(c, 120), "측정 끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done", "NAS 실패에도 측정 완료 (%s %s)" % (run["state"], run["message"]))
    check(any("NAS" in w for w in run["warns"]), "경고 (%s)" % run["warns"])
    check(run["nas"] and run["nas"]["ok"] is False and len(run["nas"]["pending"]) >= 2, "미복사 목록 (%s)" % (run["nas"],))
    pc = sorted(f for f in os.listdir(run["dir"]) if f.endswith(".dat"))
    check(pc == ["1-1_1.00V_1.dat", "1-1_1.00V_merged.dat"], "PC 에는 다 있다 (%s)" % pc)
    ok, r = c.send("rt_nas_retry")
    check(not ok, "경로가 그대로면 다시 복사도 실패 (%s)" % (r,))
    os.remove(blocker)
    os.makedirs(blocker)                                      # 이제 폴더를 만들 수 있다
    os.makedirs(nas)
    open(os.path.join(nas, "1-1_1.00V_merged.dat"), "w").write("남의 파일")       # 같은 이름이 있으면 덮어쓰지 않는다
    ok, r = c.send("rt_nas_retry")
    check(ok, "[NAS 로 다시 복사] 성공 (%s)" % (r,))
    nn = sorted(os.listdir(nas))
    check("1-1_1.00V_1.dat" in nn and "1-1_1.00V_merged_1.dat" in nn and "1-1_1.00V_merged.dat" in nn, "같은 이름은 _1 (%s)" % nn)
    check(open(os.path.join(nas, "1-1_1.00V_merged.dat")).read() == "남의 파일", "남의 파일은 그대로")


def _fault_case(c, fault_fn, expect, what, rt=None, mid_phase="가열", check_out=True):
    at_contact(c)
    ok, r = c.send("rt_start")
    check(ok, what + " · 시작 (%s)" % (r,))
    check(wait_phase(c, mid_phase, 90), what + " · %s 단계" % mid_phase)
    fault_fn()
    check(wait_rt(c, 90), what + " · 끝남")
    run = c.state["rt"]["run"]
    check(run["state"] == "error" and expect in run["message"], what + " · 오류 사유 (%s %s)" % (run["state"], run["message"]))
    safe_off(c, what, check_out)
    check(c.state["band"] and "R–T 측정 오류" in c.state["band"]["text"], what + " · 경보 띠 (%s)" % (c.state["band"],))
    return run


@case("33 오류: 열전대 끊김")
def c33(c):
    _fault_case(c, lambda: lab(c, fault={"tc_open": True}), "온도 값 없음", "열전대 끊김")
    check(last_heater_cmd_is_off(c), "마지막 히트건 명령 = 끄기")


@case("34 오류: 비상 차단 온도")
def c34(c):
    _fault_case(c, lambda: lab(c, set={"T": 80.0, "Ts": 80.0}), "비상 차단", "비상 차단 온도")


@case("35 오류: 장비 연결 끊김")
def c35(c):
    run = _fault_case(c, lambda: lab(c, fault={"disconnect": True}), "통신 끊김", "장비 연결 끊김", check_out=False)
    check(last_heater_cmd_is_off(c), "마지막 히트건 명령 = 끄기")
    check("출력 끄기 실패" in run["message"], "출력을 못 껐다고 알림 (%s)" % run["message"][-40:])
    check(lab(c).get("out") is True, "연결이 끊긴 동안은 K2450 출력을 못 끈다(알림으로 대신)")
    lab(c, fault={"disconnect": False})
    check(c.wait(lambda s: lab(c).get("out") is False, 15, "통신 복구 뒤 출력 끔"), "통신이 돌아오면 앱이 출력을 다시 끈다")


@case("36 오류: 단계 시간 한도")
def c36(c):
    c.send("settings_save", patch={"rt.phase_limit_s": 15.0})
    at_contact(c)
    ok, r = c.send("rt_start")
    check(ok, "시작")
    check(wait_rt(c, 90), "끝남")
    run = c.state["rt"]["run"]
    check(run["state"] == "error" and "시간 한도" in run["message"], "시간 한도 초과 오류 (%s %s)" % (run["state"], run["message"]))
    safe_off(c, "시간 한도")


@case("37 오류: 전류 한도 걸림은 경고만(측정 계속)")
def c37(c):
    at_contact(c)
    lab(c, fault={"ilim": True})
    c.send("settings_save", patch={"rt.repeat": 1})
    ok, r = c.send("rt_start")
    check(wait_rt(c, 120), "끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done" and any("전류 한도" in w for w in run["warns"]), "전류 한도 경고 · 계속 (%s %s)" % (run["state"], run["warns"]))
    safe_off(c, "전류 한도")


@case("38 비상정지 (가열 중)")
def c38(c):
    at_contact(c)
    ok, r = c.send("rt_start")
    check(wait_phase(c, "가열", 90), "가열 단계")
    check(c.state["relays"]["heater_on"], "히트건 켜짐 확인")
    t1 = time.monotonic()
    c.http("/estop", "POST")
    okb, rb = c.dry("bang")
    if okb and rb and rb.get("t"):                              # fake-serial: 비상정지 요청 → '!' 가 시리얼에 써진 시각
        ms = (rb["t"] - t1) * 1000.0
        check(ms <= 100.0, "비상정지 지연(R–T 측정 중 · 히트건 켜짐) %.0f ms (기준 100 ms 이하)" % ms)
        print("  비상정지 지연 R–T 측정 중 %.0f ms" % ms)
    c.pump(1.0)
    check(wait_rt(c, 30), "측정 끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "stopped" and run["status"] == "중지됨", "중단 = 중지됨 (%s %s)" % (run["state"], run["message"]))
    safe_off(c, "비상정지")
    bad, tail = after_estop(c)
    check(not bad, "비상정지 뒤 움직임 · 릴레이 켜기 명령 없음 (%s)" % (bad,))
    check(c.state["stage"]["locked"] and c.state["phase"] == "비상정지", "이동 잠김 (%s)" % c.state["phase"])
    check(abs(c.state["stage"]["z"] - 20.0) < 1.0, "비상정지는 Z 를 움직이지 않음 (%s)" % c.state["stage"]["z"])
    pressed = follow_band(c)
    check(not c.state.get("band") and c.state["stage"]["z_ok"], "띠 버튼만으로 회복 (%s)" % pressed)


c38.patch = patch(sim={"time_scale": 8.0})


@case("39 중지 (가열 중) · 종료는 중지한 뒤")
def c39(c):
    at_contact(c)
    ok, r = c.send("rt_start")
    check(wait_phase(c, "가열", 90), "가열 단계")
    ok, r = c.send("shutdown")
    check(not ok and "R–T" in str(r), "측정 중 종료 거절 (%s)" % (r,))
    ok, r = c.send("rt_stop")
    check(ok, "중지 (%s)" % (r,))
    check(wait_rt(c, 60), "끝남")
    run = c.state["rt"]["run"]
    check(run["state"] == "stopped" and run["status"] == "중지됨", "중지됨 (%s)" % run["state"])
    safe_off(c, "중지")
    c.pump(1.5)
    check(abs(c.state["stage"]["z"]) < 0.06, "중지 뒤 Z 맨 위")
    check(last_heater_cmd_is_off(c), "마지막 히트건 명령 = 끄기")


c39.patch = patch(sim={"time_scale": 8.0})


@case("40 종료 (가열 중이 아니면 허용) · 종료 정리에서 장비 닫기")
def c40(c):
    at_contact(c)
    ok, r = c.send("rt_start")
    check(wait_phase(c, "가열", 90), "가열 단계")
    ok, r = c.send("rt_stop")
    check(wait_rt(c, 60), "중지")
    ok, r = c.send("shutdown")
    check(ok, "중지한 뒤 종료 (%s)" % (r,))
    cmds = serial_cmds(c)
    check(last_heater_cmd_is_off(c), "종료 때 히트건 끄기가 마지막")


c40.patch = patch(sim={"time_scale": 8.0})


@case("41 핀이 들린 상태 · 장비 미연결 · 온도 값 없음에서 시작 거절")
def c41(c):
    ok, r = c.send("rt_start")
    check(not ok and "연결" in str(r), "장비 미연결 거절 (%s)" % (r,))
    ok, r = c.send("inst_connect")
    check(ok, "장비 연결")
    c.send("move_xy", x=60, y=60)
    check("접촉 높이" in c.state["rt"]["block"], "시작 조건 사유 (%s)" % c.state["rt"]["block"])
    ok, r = c.send("rt_start")
    check(not ok and "접촉 높이" in str(r), "핀이 들려 있으면 거절 (%s)" % (r,))
    cmds = serial_cmds(c)
    check(not any(x == "r 10 1" for x in cmds), "히트건은 켜지지 않았다")
    c.send("contact_try", free=True)
    lab(c, fault={"tc_open": True})
    ok, r = c.send("rt_start")
    check(not ok and "온도 값" in str(r), "열전대 끊김 상태에서 거절 (%s)" % (r,))
    lab(c, fault={"tc_open": False})
    c.send("z_top")
    c.pump(1.5)
    check("접촉 높이" in c.state["rt"]["block"], "Z 맨 위로 올리면 다시 거절 (%s)" % c.state["rt"]["block"])
    c.send("settings_save", patch={"contact_z_mm": None})
    c.pump(0.5)
    check("접촉 높이 필요" in c.state["rt"]["block"], "접촉 높이가 비면 사유 (%s)" % c.state["rt"]["block"])


@case("42 측정 장비 [연결 시험] · SCPI 아님이면 안내")
def c42(c):
    ok, r = c.send("inst_list")
    check(ok and "GPIB0::18::INSTR" in r["items"] and any(x.startswith("USB0") for x in r["items"]), "주소 목록 (%s)" % (r,))
    ok, r = c.send("inst_test")
    check(ok, "연결 시험 (%s)" % (r,))
    check("2110" in r["k2110"] and "2450" in r["k2450"], "장비 이름 (%s / %s)" % (r["k2110"], r["k2450"]))
    check(r["temp"] is not None and 20 < r["temp"] < 30, "온도 1회 %.1f" % (r["temp"] or -1))
    check(r["R"] is not None and r["R"] > 1e4 and r["I"] is not None, "저항 1회 %.3g Ω" % (r["R"] or -1))
    st = lab(c)
    check(st["out"] is False, "시험 뒤 출력 꺼짐")
    k = st["cmds"]
    i_on, i_off = max(i for i, x in enumerate(k) if x == ":OUTP ON"), max(i for i, x in enumerate(k) if x == ":OUTP OFF")
    check(i_on < i_off and k[i_on + 1:i_off][0].startswith(":READ"), "출력은 읽기 직전에 켜고 바로 끔 (%s)" % k[i_on:i_off + 1])
    check(c.state["rt"]["inst"]["test"]["R"] == r["R"], "시험 결과가 화면 상태에 남음")
    # SCPI 아님
    lab(c, set={"lang": "TSP"})
    ok, r = c.send("inst_connect")
    check(not ok and "SCPI" in str(r), "명령 세트가 SCPI 가 아님 · 거절 (%s)" % (r,))
    check(c.state["rt"]["inst"]["k2450"]["ok"] is False and "장비 메뉴" in c.state["rt"]["inst"]["k2450"]["err"], "안내 문구")
    lab(c, set={"lang": "SCPI"})
    # 연결 끊김
    lab(c, fault={"disconnect": True})
    ok, r = c.send("inst_connect")
    check(not ok, "연결 끊김 상태 연결 거절 (%s)" % (r,))


@case("43 샘플 순회 + R–T 측정 단계 1개 샘플")
def c43(c):
    c.send("settings_save", patch={"rt.cooler_target": "sample", "rt.repeat": 1, "rt.file_name": "T1-"})
    t = full_setup(c)
    c.send("capture")
    steps = [{"type": "move_sample"}, {"type": "contact"}, {"type": "rt"}, {"type": "z_top"}]
    ok, r = c.send("settings_save", patch={"condition.items": c.state["settings"]["condition"]["items"] + [{"name": "R-T 순회", "steps": steps}]})
    check(ok, "R–T 단계 조건 저장 (%s)" % (r,))
    ok, r = start_run(c, "R-T 순회", nos=[2])
    check(not ok and "연결" in str(r), "장비 미연결이면 시작 거절 (%s)" % (r,))
    c.send("inst_connect")
    ok, r = start_run(c, "R-T 순회", nos=[2])
    check(ok, "시작 (%s)" % (r,))
    check(wait_run(c, 240), "끝")
    run = c.state["run"]
    check(run["state"] == "done" and run["results"]["2"]["status"] == "완료", "샘플 2 완료 (%s %s)" % (run["state"], run["message"]))
    d = os.path.join(run["dir"], "rt_02")
    check(os.path.isdir(d), "샘플 폴더 rt_02")
    names = sorted(os.listdir(d)) if os.path.isdir(d) else []
    check("T1-2_1.00V_1.dat" in names and "T1-2_1.00V_merged.dat" in names, "파일 이름 = File name + 샘플 번호 (%s)" % names)
    check(not c.state["relays"]["heater_on"] and lab(c).get("out") is False, "히트건 · 출력 꺼짐")
    c.pump(1.5)
    check(abs(c.state["stage"]["z"]) < 0.06, "Z 맨 위(조건의 마지막 단계)")
    cmds = serial_cmds(c)
    iz = [i for i, x in enumerate(cmds) if x.startswith("mz") or x.startswith("gz") or x.startswith("jz")]
    check(len(iz) >= 2, "접촉 높이로 내렸다 올림")
    # 중지: 순회 중 R–T 단계에서 중지
    ok, r = start_run(c, "R-T 순회", nos=[3])
    check(ok, "두 번째 시작")
    c.wait(lambda s: s["run"] and s["run"]["state"] == "running" and (s["rt"]["run"] or {}).get("state") == "running" and (s["rt"]["run"] or {}).get("phase") in ("가열", "예비 냉각", "냉각"), 60, "R–T 단계")
    ok, r = c.send("measure_stop")
    check(ok, "중지")
    check(wait_run(c, 60), "끝")
    check(c.state["run"]["state"] == "stopped", "중지됨 (%s)" % c.state["run"]["state"])
    c.pump(1.0)
    check(not c.state["relays"]["heater_on"] and lab(c).get("out") is False, "중지: 히트건 · 출력 꺼짐")


c43.patch = patch(sim={"time_scale": 20.0})


@case("44 최대 속도 재기 · 기본값으로 · 입력 검증")
def c44(c):
    at_contact(c)
    ok, r = c.send("rt_speed", s=20.0)
    check(ok, "속도 측정 시작 (%s)" % (r,))
    check(wait_rt(c, 120), "끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done" and run["speed"] and run["speed"]["heat"] > 10, "가열 속도 %s °C/분" % (run["speed"],))
    check(run["speed"]["cool"] < 1.0, "쿨러만 켠 속도 %s" % run["speed"]["cool"])
    safe_off(c, "속도 측정")
    ok, r = c.send("settings_save", patch={"rt.break_temp": 99.0})
    check(not ok, "최고 온도보다 높은 냉각 끝 온도 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"rt.start_range": 0.003})
    check(not ok, "범위 값 검증 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"rt.max_temp": 70.0, "rt.volt": 2.5, "rt.heat_rate": 3.0})
    check(ok and c.state["settings"]["rt"]["max_temp"] == 70.0, "마지막 값 기억")
    ok, r = c.send("rt_defaults")
    s = c.state["settings"]["rt"]
    check(ok and s["max_temp"] == 85.0 and s["volt"] == 1.0 and s["heat_rate"] == 0.0 and s["precool_c"] == 24.5 and s["repeat"] == 3, "기본값으로 (%s)" % s["max_temp"])
    check(s["k2450_addr"] == "GPIB0::18::INSTR", "장비 주소는 그대로")


@case("45 목표 온도선 추종 (가열 · 냉각 속도 지정)")
def c45(c):
    at_contact(c)
    c.send("settings_save", patch={"rt.heat_rate": 6.0, "rt.cool_rate": 3.0, "rt.max_temp": 40.0, "rt.break_temp": 30.0, "rt.repeat": 1, "rt.precool_c": 26.0, "rt.phase_limit_s": 7200.0})
    ok, r = c.send("rt_start")
    check(ok, "시작 (%s)" % (r,))
    check(wait_rt(c, 240), "끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done", "완료 (%s %s)" % (run["state"], run["message"]))
    code, body = c.http("/rt.json?since=0")
    pts = json.loads(body)["points"]
    heat = [p for p in pts if p["ph"] == "가열"]
    check(len(heat) > 30 and any(p["tgt"] is not None for p in heat), "목표 온도선이 점에 실림")
    on = sum(p["h"] for p in heat) / len(heat)
    check(0.05 < on < 0.8, "속도 지정 가열은 히트건을 켜고 끈다 (켜짐 비율 %.0f %%)" % (100 * on))
    cool = [p for p in pts if p["ph"] == "냉각"]
    check(len(cool) > 10, "냉각 점 %d" % len(cool))
    safe_off(c, "추종")


# 케이스별 설정 패치: 오류 시나리오는 단계가 길게(시간 배율 10), 나머지는 빠르게(120)
for _n, _fn in list(e2e.CASES.items()):
    if _n[:2].isdigit() and 31 <= int(_n[:2]) <= 45 and not hasattr(_fn, "patch"):
        _fn.patch = patch(sim={"time_scale": 10.0 if int(_n[:2]) in (33, 34, 35, 36) else 20.0 if int(_n[:2]) == 31 else 120.0})
