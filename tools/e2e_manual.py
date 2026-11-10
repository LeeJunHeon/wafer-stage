"""e2e_manual.py - 측정 장비 주소 · 수동 측정 탭 · 원점 없이 이동 허용 e2e 시나리오 80~85 (tools/e2e.py 가 불러온다). dry · fake-serial, 가짜 장비(tools/fake_visa.py).

  80 장비 연결: 시리얼(ASRL) 주소는 목록에도 없고 열지도 않음 · 일련번호 없는 USB 주소 → 목록의 주소로 열고 저장 · visa.log
  81 수동 측정: 온도 1번 · 저항 1번(출력은 읽는 동안만) · 연속 10 초 → control.csv 만
  82 수동 R–T 1회 끝까지(쿨러 R2 · 히트건 R1) · 모터 안 움직임 · 파일 형식 · 끝나면 전부 꺼짐 · 결과 목록 "수동"
  83 수동 R–T 중 [정지] · 비상정지 → 같은 정리 경로 · 서로 막기(이동 · 조그 · 촬영 · 자동 R–T · 장비 연결 · 릴레이 켜기)
  84 명령 직접 보내기: 응답 · 오류 큐 · K2450 출력 켜기 거절 · 측정 중 거절 · visa.log
  85 원점 없이 이동 허용(원점 없음 · 실제 속도): 끔 → X 조그 거절 · 켬 → jx/jy/jz · 60 mm = 8000 + 1600 펄스 · 이동 중 비상정지 → '!' · 남은 조각 없음 ·
     좌표 이동 · 대기 위치 · 측정 거절 · 다시 연결 → 꺼짐 · 세 축 원점 → 꺼짐 (E2E_MODE=fake 가 기준)
"""
import json
import os
import time

import e2e
from e2e import case, check


def with_patch(patch):
    def deco(fn):
        fn.patch = patch
        return fn
    return deco

# 새 기본값 배치(히트건 R1 · 쿨러 R2~R10) - 장비 PC 와 같다
NEW_RELAYS = {"heater": {"relay": 1}, "cooler": {"zones": {"rows": 3, "cols": 3, "row_edges": None, "col_edges": None,
                                                           "cells": [{"cooler": i + 1, "relay": i + 2, "name": "쿨러 %d" % (i + 1)} for i in range(9)]}},
              "manual": {"cooler_relays": "2"}, "rt": {"file_path": "", "max_temp": 60.0, "break_temp": 35.0, "repeat": 1, "file_name": "m-1_"}}


def _man(c):
    return c.state.get("manual") or {}


def _wait_end(c, timeout=180):
    return c.wait(lambda s: s["manual"]["run"] and s["manual"]["run"]["state"] in ("done", "stopped", "error"), timeout, "수동 측정 끝")


def _lab(c, **kw):
    ok, r = c.dry("lab", **kw)
    return r if ok else {}


def _rs(c):
    ok, r = c.dry("fw")
    if ok and isinstance(r, dict) and r.get("relay") is not None:
        return [i + 1 for i, b in enumerate(r["relay"]) if b]
    st = c.state.get("manual", {}).get("relays_on")
    return st


def _visa_log(c):
    d = os.path.join(c.data, "logs")
    fs = sorted(f for f in os.listdir(d) if f.endswith("_visa.log")) if os.path.isdir(d) else []
    return open(os.path.join(d, fs[-1]), encoding="utf-8").read() if fs else ""


@case("80 장비 연결 · 시리얼 주소 제외 · 일련번호 없는 USB → 목록 주소 · visa.log")
def c80(c):
    ok, r = c.send("inst_list")
    items = (r or {}).get("items") or []
    check(ok and items and not any(i.upper().startswith("ASRL") for i in items), "목록에 시리얼(ASRL) 없음 (%s)" % items)
    ok, r = c.send("inst_connect")
    check(ok, "장비 연결 (%s)" % (r,))
    ri = c.state["rt"]["inst"]
    check(ri["k2110"]["ok"] and "SIM0002" in ri["k2110"]["addr"], "K2110 = 목록의 일련번호 있는 주소 (%s)" % ri["k2110"].get("addr"))
    check(c.state["settings"]["rt"]["k2110_addr"] == ri["k2110"]["addr"], "찾은 주소를 저장")
    lab = _lab(c)
    check(not any(a.upper().startswith("ASRL") for a in lab.get("opened", [])), "시리얼 주소를 열지 않음 (%s)" % lab.get("opened"))
    ok, r = c.send("inst_test")
    check(ok, "연결 시험 통과 · 예전에는 '+0, \"No error\"' 를 오류로 읽어 실패 (%s)" % (r,))
    log = _visa_log(c)
    check('SYST:ERR? -> +0, "No error"' in log and "K2450 GPIB0::18::INSTR" in log, "visa.log 에 명령 · 응답 · 시간")
    # 설정 주소가 열리지 않으면 목록에서 찾아 다시
    c.send("inst_disconnect")
    c.send("settings_save", patch={"rt.k2110_addr": "USB0::0x05E6::0x2110::WRONG::INSTR"})
    ok, r = c.send("inst_connect")
    check(ok and "SIM0002" in c.state["rt"]["inst"]["k2110"]["addr"], "열기 실패 → 목록 주소로 다시 (%s)" % (r,))


@case("81 수동 측정 · 온도 1번 · 저항 1번 · 연속 10 초")
@with_patch(NEW_RELAYS)
def c81(c):
    ok, r = c.send("man_temp")
    check(ok and r and abs(r["T"] - 25.0) < 3.0, "온도 1번 (%s)" % (r,))
    ok, r = c.send("man_res")
    check(ok and r and r["R"] and 1e5 < r["R"] < 1e6, "저항 1번 · 1 V 기본 (%s)" % (r,))
    check(not _lab(c)["out"], "저항 1번 뒤 K2450 출력 꺼짐")
    cmds = _lab(c)["cmds"]
    check(":OUTP ON" in cmds and cmds[-1] == ":OUTP OFF" or ":OUTP OFF" in cmds[cmds.index(":OUTP ON"):], "출력은 읽는 동안만 켬")
    z0 = c.state["stage"]["z"]
    ok, r = c.send("man_cont")
    check(ok, "연속 측정 시작 (%s)" % (r,))
    time.sleep(10.0)
    c.pump(0.5)
    run = _man(c)["run"]
    check(run and run["state"] == "running" and run["n_points"] > 5, "연속 측정 중 · 점 %s" % (run and run["n_points"]))
    ok2, r2 = c.send("jog", axis="z", mm=-0.1)
    check(not ok2 and "수동 측정 중" in str(r2), "연속 측정 중 조그 거절 (%s)" % (r2,))
    c.send("man_stop")
    _wait_end(c)
    run = _man(c)["run"]
    check(run["status"] == "중지됨", "정지 → 중지됨 (%s · %s)" % (run["status"], run["message"]))
    files = sorted(os.listdir(run["dir"]))
    check(files == ["control.csv", "rt.json"], "연속 측정은 control.csv 만 (%s)" % files)
    check(os.path.dirname(run["dir"]).endswith("manual"), "저장 위치 = 데이터 폴더/manual (%s)" % run["dir"])
    check(c.state["stage"]["z"] == z0, "모터 안 움직임 (Z %s → %s)" % (z0, c.state["stage"]["z"]))
    check(not _lab(c)["out"], "K2450 출력 꺼짐")


@case("82 수동 R–T 1회 끝까지 · 쿨러 R2 · 히트건 R1 · 파일 형식 · 결과 목록")
@with_patch(NEW_RELAYS)
def c82(c):
    _lab(c, set={"tc_on_plate": True})
    st0 = dict(c.state["stage"])
    ok, r = c.send("man_rt")
    check(ok, "수동 R–T 시작 (%s)" % (r,))
    seen_heat = seen_cool = False
    t0 = time.time()
    while time.time() - t0 < 120:
        c.pump(0.3)
        run = _man(c)["run"]
        on = _man(c).get("relays_on") or []
        seen_heat |= 1 in on
        seen_cool |= 2 in on
        if run and run["state"] not in ("running", "stopping", "ready"):
            break
    run = _man(c)["run"]
    check(run["status"] == "완료", "수동 R–T 완료 (%s · %s)" % (run["status"], run["message"]))
    check(seen_heat and seen_cool, "히트건 R1 · 쿨러 R2 켜졌었음")
    check((_man(c).get("relays_on") or []) == [], "끝 · 릴레이 전부 꺼짐 (%s)" % _man(c).get("relays_on"))
    check(not _lab(c)["out"], "끝 · K2450 출력 꺼짐")
    st1 = c.state["stage"]
    check((st1["x"], st1["y"], st1["z"]) == (st0["x"], st0["y"], st0["z"]), "모터 안 움직임 · Z 올림 없음")
    d = run["dir"]
    names = sorted(os.listdir(d))
    check("m-1_1.00V_0.dat" in names and "m-1_1.00V_merged.dat" in names and "control.csv" in names, "파일 (%s)" % names)
    raw = open(os.path.join(d, "m-1_1.00V_0.dat"), "rb").read()
    check(raw.startswith(b"Temp   Resistance   \r\n") and raw.endswith(b"\r\n") and b"\t" in raw, "회차 파일 머리줄 · CRLF · 탭")
    rows = raw.split(b"\r\n")[1:-1]
    import re
    check(rows and all(re.match(rb"^-?\d\.\d{7}E[+-]\d{1,3}\t-?\d\.\d{7}E[+-]\d{1,3}$", x) for x in rows), "숫자 형식 %.7E (지수 0 채우지 않음)")
    mrg = open(os.path.join(d, "m-1_1.00V_merged.dat"), "rb").read().split(b"\r\n")[:-1]
    check(len(mrg) == len(rows), "merged 줄 수 = 회차 줄 수 (%d · %d)" % (len(mrg), len(rows)))
    ctl = open(os.path.join(d, "control.csv"), encoding="utf-8-sig").read().splitlines()
    check(ctl[0] == "시각,경과 s,회차,단계,온도,전류,저항,한도 걸림,히트건,쿨러,전압", "control.csv 머리줄 (%s)" % ctl[0])
    c.pump(1.0)
    rl = c.state["rt"]["results"]
    check(any(x.get("kind") == "수동" and x["dir"] == os.path.basename(d) for x in rl), "결과 목록에 종류 '수동'")
    j = json.loads(c.http("/rt_data/%s?root=manual" % os.path.basename(d))[1])
    check(j["cycles"] and len(j["cycles"][0]["pts"]) == len(rows), "결과 탭 그래프 데이터")


@case("83 수동 R–T 중 정지 · 비상정지 · 서로 막기")
@with_patch(NEW_RELAYS)
def c83(c):
    _lab(c, set={"tc_on_plate": True})
    c.send("settings_save", patch={"rt.max_temp": 90.0, "sim.time_scale": 4.0})
    ok, r = c.send("man_rt")
    check(ok, "수동 R–T 시작 (%s)" % (r,))
    c.wait(lambda s: s["manual"]["run"] and s["manual"]["run"]["phase"] == "가열" and 1 in (s["manual"]["relays_on"] or []), 30, "가열")
    for cmd, args in (("jog", {"axis": "x", "mm": 1.0}), ("move_xy", {"x": 50.0, "y": 50.0}), ("z_top", {}), ("capture", {}), ("rt_start", {}),
                      ("inst_connect", {}), ("relay", {"n": 3, "on": True}), ("relay_test", {"n": 3}), ("head_calib", {}), ("man_temp", {})):
        ok2, r2 = c.send(cmd, **args)
        check(not ok2, "수동 측정 중 %s 거절 (%s)" % (cmd, r2))
    ok, r = c.send("man_stop")
    check(ok, "정지 (%s)" % (r,))
    _wait_end(c, 60)
    run = _man(c)["run"]
    check(run["status"] == "중지됨", "정지 → 중지됨 (%s)" % run["status"])
    check((_man(c).get("relays_on") or []) == [] and not _lab(c)["out"], "정지 · 히트건 → 출력 → 쿨러 꺼짐")
    # 비상정지
    time.sleep(0.6)
    ok, r = c.send("man_rt")
    check(ok, "다시 시작 (%s)" % (r,))
    c.wait(lambda s: s["manual"]["run"] and s["manual"]["run"]["phase"] == "가열" and 1 in (s["manual"]["relays_on"] or []), 30, "가열")
    c.http("/estop", "POST")
    _wait_end(c, 60)
    run = _man(c)["run"]
    check(run["status"] in ("중지됨",), "비상정지 → 중지됨 (%s · %s)" % (run["status"], run["message"]))
    c.pump(2.0)
    check(not (1 in (_man(c).get("relays_on") or [])) and not _lab(c)["out"], "비상정지 · 히트건 꺼짐 · 출력 꺼짐")
    check(_man(c)["block_rt"].startswith("비상정지"), "비상정지 잠금 중 수동 R–T 시작 거절 (%s)" % _man(c)["block_rt"])


@case("84 명령 직접 보내기 · 오류 큐 · 출력 켜기 거절")
@with_patch(NEW_RELAYS)
def c84(c):
    ok, r = c.send("man_scpi", dev="k2110", line="*IDN?")
    check(ok and "2110" in (r.get("resp") or "") and r.get("errs") == [], "K2110 *IDN? 응답 (%s)" % (r,))
    ok, r = c.send("man_scpi", dev="k2450", line=":FOO:BAR 1")
    check(ok and r["errs"] and r["errs"][0].startswith("-113"), "잘못된 명령 → 오류 큐 (%s)" % (r,))
    for bad in (":OUTP ON", "OUTP:STAT 1", ":SOUR:VOLT 1;:OUTP ON", ":outp on"):
        ok, r = c.send("man_scpi", dev="k2450", line=bad)
        check(not ok and "출력" in str(r), "%s 거절 (%s)" % (bad, r))
    check(not _lab(c)["out"], "출력 꺼진 채")
    log = _visa_log(c)
    check(":FOO:BAR 1" in log and "*IDN?" in log, "visa.log 에 직접 보낸 명령")
    ok, r = c.send("man_cont")
    ok2, r2 = c.send("man_scpi", dev="k2110", line="*IDN?")
    check(not ok2 and "수동 측정 중" in str(r2), "측정 중 명령 직접 거절 (%s)" % (r2,))
    c.send("man_stop")
    _wait_end(c, 30)


NOHOME = {"WAFER_STAGE_DRY_NO_HOME": "1", "WAFER2_FAKE_EEPROM": "none", "WAFER_STAGE_DRY_MOVE_S": "real", "WAFER2_FAKE_SCALE": "1.0"}


def _cmds_after(c, n0):
    from e2e_cases import serial_cmds
    return serial_cmds(c)[n0:]


@case("85 원점 없이 이동 허용 · 조그만 · 8000 펄스 조각 · 비상정지 · 다시 연결 · 원점 뒤 꺼짐")
@with_patch({"jog": {"max_mm": 100.0, "steps": [0.1, 1, 10, 50]}})
def c85(c):
    from e2e_cases import serial_cmds
    st = c.state["stage"]
    check(st and not st["z_ok"], "원점 없음으로 시작 (z_ok=%s)" % (st and st["z_ok"]))
    check(not st["free_move"], "체크 꺼짐으로 시작")
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "원점" in str(r), "체크 끔 · X 조그 거절 (%s)" % (r,))
    ok, r = c.send("free_move", on=True)
    check(ok and c.state["stage"]["free_move"], "체크 켬 (%s)" % (r,))
    bands = c.state.get("bands") or []
    check(bands and bands[0]["id"] == "free" and bands[0]["err"] and "Z 높이 모름" in bands[0]["text"], "맨 위 붉은 띠 '원점 없이 이동 중 · Z 높이 모름'")
    for ax, mm, fw in (("z", -0.5, "jz"), ("x", 2.0, "jx"), ("y", -2.0, "jy")):
        n0 = len(serial_cmds(c))
        ok, r = c.send("jog", axis=ax, mm=mm)
        new = _cmds_after(c, n0)
        check(ok and any(x.startswith(fw + " ") for x in new), "%s 조그 → %s (%s · %s)" % (ax.upper(), fw, r, [x for x in new if x.startswith("j")]))
        time.sleep(0.6)
    # 60 mm = 9600 펄스 → 8000 + 1600 두 조각
    n0 = len(serial_cmds(c))
    ok, r = c.send("jog", axis="x", mm=60.0, timeout=60)
    js = [x for x in _cmds_after(c, n0) if x.startswith("jx ")]
    check(ok and js == ["jx 8000", "jx 1600"], "60 mm → 8000 펄스 이하 조각 (%s · %s)" % (js, r))
    time.sleep(0.6)
    # 이동 중 비상정지: '!' 가 가장 먼저 · 남은 조각 안 나감
    n0 = len(serial_cmds(c))
    i = c.send_nowait("jog", axis="x", mm=-60.0)
    time.sleep(0.5)
    c.http("/estop", "POST")
    ok, r = c.wait_ack(i, 30)
    new = _cmds_after(c, n0)
    js = [x for x in new if x.startswith("jx ")]
    bang = [k for k, x in enumerate(new) if x.startswith("!")]
    check(not ok and js == ["jx -8000"] and bang, "이동 중 비상정지 → 남은 조각 안 나감 (%s · %s)" % (new[:6], r))
    check(c.state["stage"]["locked"], "비상정지 잠금")
    time.sleep(1.2)
    ok, r = c.send("jog", axis="z", mm=-0.5)
    check(ok, "비상정지 잠금이어도 체크가 켜져 있으면 조그 (%s)" % (r,))
    time.sleep(0.6)
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "체크 켬 · 좌표 이동은 원점 요구 (%s)" % (r,))
    ok, r = c.send("goto_named", name="park")
    check(not ok, "체크 켬 · 대기 위치 이동 거절 (%s)" % (r,))
    ok, r = c.send("rt_start")
    check(not ok, "체크 켬 · 측정 시작 거절 (%s)" % (r,))
    # 다시 연결 → 꺼짐
    ok, r = c.send("reconnect", timeout=60)
    c.wait(lambda s: s["stage"] and s["stage"]["connected"], 30, "다시 연결")
    check(not c.state["stage"]["free_move"], "다시 연결 → 체크 꺼짐 (%s)" % (r,))
    # 세 축 원점 → 꺼짐
    c.send("free_move", on=True)
    check(c.state["stage"]["free_move"], "다시 켬")
    for ax in ("z", "x", "y"):
        if MODE_FAKE():
            c.dry("set_real", axis=ax, mm=1.0) if ax != "z" else None
        ok, r = c.send("origin", axis=ax, timeout=90)
        check(ok, "%s 원점 잡기 (%s)" % (ax.upper(), r))
        time.sleep(0.6)
    c.pump(1.0)
    st = c.state["stage"]
    check(st["z_ok"] and st["xy_ok"] and not st["free_move"], "세 축 원점 정상 → 체크 저절로 꺼짐 (%s)" % st["free_move"])
    ok, r = c.send("free_move", on=True)
    check(not ok, "원점 정상이면 켜지 않음 (%s)" % (r,))


def MODE_FAKE():
    return e2e.MODE == "fake"


c85.env = NOHOME
