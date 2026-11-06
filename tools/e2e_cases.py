"""e2e_cases.py - e2e 시나리오 04~ (tools/e2e.py 가 불러온다). 측정 조건: 접촉 시험 · 수동 측정 · 가열."""
import os
import time

import e2e
from e2e import case, check, connect, full_setup, chips_err

HEAT = "가열"
TOUCH = "접촉 시험"
MANUAL = "수동 측정"


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def serial_cmds(c):
    """스테이지가 받은 명령 목록(시리얼 로그의 '->' 줄)."""
    fn = os.path.join(c.data, "logs", "serial.log")
    out = []
    try:
        for ln in open(fn, encoding="utf-8"):
            parts = ln.rstrip("\n").split(" ", 2)
            if len(parts) == 3 and parts[1] == "->":
                out.append(parts[2])
    except OSError:
        pass
    return out


def after_estop(c):
    """마지막 정지 명령 뒤에 나간 명령 중 움직임 · 위치 세팅 · 속도 · 릴레이 켜기."""
    cmds = serial_cmds(c)
    idx = max([i for i, x in enumerate(cmds) if x.startswith("! (abort)")] or [-1])
    tail = cmds[idx + 1:]
    bad = [x for x in tail if x.split()[0] in ("mx", "my", "mz", "jx", "jy", "jz", "sp", "gx", "gy", "gz", "g", "v", "fz")
           or (x.startswith("r ") and x.endswith(" 1"))]
    return bad, tail


def recover(c):
    """회복 순서(명령): Z 원점 잡기 -> 위치 확인 -> 적용."""
    ok1, r1 = c.send("origin", axis="z")
    ok2, r = c.send("verify_position")
    ok3, r3 = c.send("apply_position")
    if not (ok1 and ok2 and ok3):
        print("  (회복 실패: 원점 %s · 위치 확인 %s · 적용 %s)" % (r1, r, r3))
    return ok1 and ok2 and ok3, (r, r3)


def follow_band(c, max_steps=8):
    """띠 버튼만 따라 회복: 첫 번째 버튼을 누르는 것을 띠가 없어질 때까지 되풀이(화면이 하는 것과 같은 명령). 누른 버튼 라벨 목록을 돌려준다."""
    pressed = []
    for _ in range(max_steps):
        c.pump(0.5)
        b = c.state.get("band")
        if not b:
            break
        acts = b.get("actions") or []
        if not acts:
            pressed.append("(버튼 없음!)")
            break
        a = acts[0]
        ok, r = c.send(a["act"], **(a.get("args") or {}))
        pressed.append(a["label"] + ("" if ok else " 실패:" + str(r)))
        if not ok:
            break
    c.pump(0.5)
    return pressed


def start_run(c, cond=TOUCH, nos=None, mode=None):
    return c.send("measure_start", condition=cond, nos=nos, ok_weak=True)


def wait_run(c, timeout=240):
    return c.wait(lambda s: s["run"] and s["run"]["state"] in ("done", "stopped", "error"), timeout, "측정 끝")


def ready_capture(c):
    t = full_setup(c)
    ok, r = c.send("capture")
    check(ok and len(c.state["samples"]) == len(t["chips"]), "촬영 · 인식 %d" % len(c.state["samples"]))
    return t


def estop_http(c):
    return c.http("/estop", "POST")


# ---------------------------------------------------------------------------
@case("04 샘플 이동 · Z 맨 위 확인")
def c04(c):
    ready_capture(c)
    c.dry("z", mm=12.0)
    s = c.state["samples"][4]
    ok, r = c.send("goto_sample", no=s["no"])
    check(ok, "샘플 이동 (%s)" % (r,))
    cmds = serial_cmds(c)
    iz = max(i for i, x in enumerate(cmds) if x.startswith("mz 0.00"))
    ix = max(i for i, x in enumerate(cmds) if x.startswith("mx"))
    check(iz < ix, "X 이동 전에 Z 올림 (mz 0 → mx)")
    c.pump(1.2)
    st = c.state["stage"]
    check(st["z"] is not None and abs(st["z"]) < 0.06, "Z = 0 (%s)" % st["z"])
    check(abs(st["x"] - s["X"]) < 0.2 and abs(st["y"] - s["Y"]) < 0.2, "도착 X %.1f Y %.1f" % (st["x"], st["y"]))


@case("05 가열 측정 전체 · 결과 파일")
def c05(c):
    ready_capture(c)
    ok, r = start_run(c, HEAT, nos=[1, 2])
    check(ok, "측정 시작 (%s)" % (r,))
    check(wait_run(c, 280), "측정 끝")
    run = c.state["run"]
    check(run["state"] == "done", "상태 done (%s %s)" % (run["state"], run["message"]))
    res = run["results"]
    check(sorted(map(int, res)) == [1, 2], "결과 2개")
    for k, v in res.items():
        check(v["status"] == "완료" and abs((v["contact_z"] or 0) - 20.0) < 0.1 and v["start"] and v["end"],
              "샘플 %s %s · 접촉 Z %s · %s ~ %s" % (k, v["status"], v["contact_z"], v["start"], v["end"]))
    d = run["dir"]
    for fn in ("run.json", "samples.csv"):
        check(os.path.exists(os.path.join(d, fn)), "결과 파일 " + fn)
    check(not [f for f in os.listdir(d) if f.startswith("sample_")], "온도 기록 파일 없음")
    check(open(os.path.join(d, "samples.csv"), "rb").read(3) == b"\xef\xbb\xbf", "CSV UTF-8 BOM")
    hdr = open(os.path.join(d, "samples.csv"), encoding="utf-8-sig").readline().strip()
    check(hdr == "번호,X,Y,쿨러,접촉 Z,시작,끝,결과,사유", "CSV 열 (%s)" % hdr)
    c.pump(1.0)
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    check(c.state["phase"] == "대기", "상태 대기 (%s)" % c.state["phase"])
    check(abs(c.state["stage"]["z"]) < 0.06, "Z 맨 위")
    cmds = serial_cmds(c)
    check(any(x == "r 10 1" for x in cmds) and any(x == "r 10 0" for x in cmds), "히트건 켬 · 끔 명령")
    check("save" in cmds[-12:], "측정 끝 · 대기 위치 도착 뒤 위치 저장(save)")


@case("06 확인 대기 [다음]")
def c06(c):
    ready_capture(c)
    ok, r = start_run(c, MANUAL, nos=[2, 5, 7])
    check(ok, "시작 (%s)" % (r,))
    for k in range(3):
        c.wait(lambda s: s["run"] and s["run"]["waiting"] == "확인 대기", 120, "확인 대기")
        st = c.state
        check(st["run"]["waiting"] == "확인 대기" and st["phase"] == "확인 대기", "샘플 %d · 확인 대기 (%s)" % (k + 1, st["phase"]))
        c.pump(1.6)                                           # 상태 읽기 주기(1 s)가 지나 Z 가 갱신되길 기다린다
        st = c.state
        check(abs(st["stage"]["z"] - 20.0) < 0.1, "핀을 샘플에 댄 채 기다림 (Z %.2f)" % st["stage"]["z"])
        c.pump(1.0)
        check(c.state["run"]["waiting"] == "확인 대기", "[다음] 전에는 계속 기다림")
        ok, r = c.send("measure_answer", value="next")
        check(ok, "[다음] (%s)" % (r,))
    wait_run(c, 120)
    check(sorted(map(int, c.state["run"]["results"])) == [2, 5, 7], "선택한 3개만 측정 %s" % list(c.state["run"]["results"]))
    check(c.state["run"]["state"] == "done", "끝")
    ok, r = c.send("measure_answer", value="next")
    check(not ok, "확인 대기가 아닐 때 [다음] 거절 (%s)" % (r,))


@case("07 일시정지 → 조그 → 기준점 기록 → 다시 시작")
def c07(c):
    ready_capture(c)
    start_run(c, HEAT, nos=[1])
    c.wait(lambda s: s["run"] and s["run"]["cur"] is not None, 60)
    c.send("measure_pause")
    check(c.wait(lambda s: s["phase"] == "일시정지", 20), "일시정지 표시")
    time.sleep(0.8)
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(ok, "일시정지 중 조그 (%s)" % (r,))
    ok, r = c.send("calib_record")
    check(ok, "기준점 위치 기록 (%s)" % (r,))
    c.send("measure_resume")
    check(wait_run(c, 200) and c.state["run"]["state"] == "done", "다시 시작 뒤 끝")


@case("08 히트건 중 중지")
def c08(c):
    ready_capture(c)
    start_run(c, HEAT, nos=[3])
    check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 120, "히트건 시작"), "히트건 시작")
    c.send("measure_stop")
    check(wait_run(c, 60) and c.state["run"]["state"] == "stopped", "중지됨")
    c.pump(1.5)
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    st = c.state["stage"]
    check(abs(st["z"]) < 0.06, "Z 맨 위")
    check(abs(st["x"] - 10.0) < 0.2 and abs(st["y"] - 20.0) < 0.2, "대기 위치 (%.1f, %.1f)" % (st["x"], st["y"]))
    check(c.state["run"]["results"].get("3", {}).get("status") == "중지됨", "샘플 결과 = 중지됨")


c08.patch = {"sim": {"time_scale": 3.0}}


@case("09 비상정지 다섯 시점 + 재현 3개")
def c09(c):
    ready_capture(c)
    # (이동 중) + (재현 1) 잠금 대기 중이던 조그
    c.dry("slow", s=0.5)
    i1 = c.send_nowait("goto_sample", no=3)
    time.sleep(0.2)
    i2 = c.send_nowait("jog", axis="x", mm=5.0)
    time.sleep(0.2)
    estop_http(c)
    c.wait_ack(i1, 30)
    ok2, r2 = c.wait_ack(i2, 30)
    c.dry("slow", s=0.0)
    bad, tail = after_estop(c)
    check(not bad, "(이동 중) 비상정지 뒤 이동 · 조그 · 속도 명령 없음 %s" % bad)
    check(not ok2, "(재현 1) 잠금 대기 중이던 조그 거절 (%s)" % (r2,))
    check(c.state["stage"]["locked"], "잠금 유지")
    ok, r = c.send("move_xy", x=50, y=50)
    check(not ok, "잠금 중 이동 거절 (%s)" % (r,))
    # (재현 2)
    ok, r = c.send("jog", axis="x", mm=5.0)
    check(not ok and "Z 원점" in str(r), "(재현 2) Z 원점 없음 · X 조그 거절 (%s)" % (r,))
    # (재현 3) 원점 잡기는 fz - 이격 이동 + 등록이 펌웨어 한 명령
    ok, r = recover(c)
    cmds = serial_cmds(c)
    check(ok, "회복 순서 완료 (Z 원점 잡기 → 위치 확인 → 적용)")
    check(any(x.startswith("fz z") for x in cmds), "(재현 3) fz z 로 원점 잡기")
    check(not c.state["stage"]["locked"], "회복 뒤 잠금 해제")
    # (위치 확인 촬영 중)
    c.dry("cam_delay", s=0.4)
    n_sp = len([x for x in serial_cmds(c) if x.startswith("sp")])
    i = c.send_nowait("verify_position")
    time.sleep(0.6)
    estop_http(c)
    ok, r = c.wait_ack(i, 60)
    c.dry("cam_delay", s=0.0)
    check(not ok, "(위치 확인 촬영 중) 결과 버림 (%s)" % (r,))
    check(len([x for x in serial_cmds(c) if x.startswith("sp")]) == n_sp, "sp 안 보냄")
    ok, r = c.send("apply_position")
    check(not ok, "적용 거절 (%s)" % (r,))
    recover(c)
    # (조그 조각 대기 중)
    c.dry("slow", s=0.4)
    i = c.send_nowait("jog", axis="x", mm=-120.0)
    time.sleep(0.6)
    estop_http(c)
    c.wait_ack(i, 30)
    c.dry("slow", s=0.0)
    bad, tail = after_estop(c)
    check(not bad, "(조그 조각 대기 중) 남은 조각 안 나감 %s" % bad)
    recover(c)
    # (히트건 중)
    ok, r = start_run(c, HEAT, nos=[4])
    check(ok, "(히트건 중) 측정 시작 (%s)" % (r,))
    c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 120)
    estop_http(c)
    c.pump(1.0)
    bad, tail = after_estop(c)
    check(not c.state["relays"]["heater_on"], "(히트건 중) 히트건 꺼짐")
    wait_run(c, 60)
    check(c.state["run"]["state"] in ("stopped", "error") and not bad, "(히트건 중) 측정 중단 · 움직임 없음 %s" % bad)
    recover(c)
    # (촬영 중)
    c.dry("cam_delay", s=0.4)
    i = c.send_nowait("capture")
    time.sleep(0.8)
    estop_http(c)
    ok, r = c.wait_ack(i, 60)
    c.dry("cam_delay", s=0.0)
    check(not ok, "(촬영 중) 촬영 중단 (%s)" % (r,))
    bad, tail = after_estop(c)
    check(not bad, "(촬영 중) 움직임 없음 %s" % bad)
    check(c.state["stage"]["locked"], "(촬영 중) 잠금 유지")


c09.patch = {"sim": {"time_scale": 3.0}}


@case("09b 조그는 원점 있는 축을 가동범위 밖으로 안 몬다")
def c09b(c):
    ready_capture(c)
    ok, r = c.send("jog", axis="z", mm=-5.0)
    check(not ok and "가동범위" in str(r), "맨 위에서 Z↑ 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=-500.0)
    check(not ok and "가동범위" in str(r), "X 가동범위 밖 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=5.0)
    check(ok, "Z↓ 는 됨 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=-5.0)
    check(ok, "Z↑ (내려간 만큼) 는 됨 (%s)" % (r,))


@case("10 이동 시간 초과 · 재동기")
def c10(c):
    ready_capture(c)
    c.dry("hang", on=True, s=9.0)
    t0 = time.time()
    ok, r = c.send("move_xy", x=60, y=60, timeout=90)
    check(not ok and "시간 초과" in str(r), "시간 초과 보고 (%s) %.1f s" % (r, time.time() - t0))
    check(any(x.startswith("! ") for x in serial_cmds(c)), "정지 명령 전송")
    c.pump(1.0)
    check(c.state["stage"]["connected"], "연결 유지")
    check(not c.state["stage"]["xy_ok"], "위치 못 믿음 표시")
    ok, r = c.send("move_xy", x=60, y=60)
    check(not ok, "위치 확인 전 이동 거절 (%s)" % (r,))
    b = c.state.get("band") or {}
    check(b.get("actions"), "띠에 다음 동작 버튼 (%s)" % [a["label"] for a in b.get("actions", [])])
    ok, r = c.send("verify_position")
    ok2, r2 = c.send("apply_position")
    check(ok and ok2, "위치 확인 · 적용 뒤 재개")
    ok, r = c.send("move_xy", x=60, y=60)
    check(ok, "이동 재개 (%s)" % (r,))


@case("11 보드 재부팅 감지")
def c11(c):
    ready_capture(c)
    start_run(c, HEAT, nos=[1])
    c.wait(lambda s: s["run"] and s["run"]["cur"] is not None, 60)
    c.dry("reboot")
    c.pump(1.0)
    check("재부팅" in ((c.state["alarm"] or {}).get("text") or ""), "경보 · 재부팅 (%s)" % (c.state["alarm"],))
    check(c.state["stage"]["locked"], "원점 확인 요구 · 잠금")
    b = c.state.get("band") or {}
    check([a["act"] for a in b.get("actions", [])][:1] == ["origin"], "띠 버튼 = Z 원점 잡기 (%s)" % [a["label"] for a in b.get("actions", [])])
    wait_run(c, 60)
    check(c.state["run"]["state"] in ("stopped", "error"), "측정 중지")
    check(any(x.startswith("wd ") for x in serial_cmds(c)[-15:]), "wd 재전송")


c11.patch = {"sim": {"time_scale": 3.0}}


@case("12 결과 파일 잠김 · 단계 예외")
def c12(c):
    ready_capture(c)
    c.dry("fault", name="csv_lock", on=True)
    start_run(c, HEAT, nos=[1])
    wait_run(c, 200)
    run = c.state["run"]
    check(run["state"] == "done" and any("열려 있음" in w for w in run["warns"]), "CSV 잠김 → _1 이름 · 경고 (%s)" % (run["warns"],))
    c.dry("fault", name="csv_lock", on=False)
    c.dry("fault", name="step_exc", on=True)
    start_run(c, TOUCH, nos=[2])
    wait_run(c, 60)
    run = c.state["run"]
    check(run["state"] == "error" and run["results"]["2"]["status"] == "오류", "예외 → 오류 결과 (%s)" % (run["results"].get("2", {}).get("status"),))
    check("측정 오류" in ((c.state["alarm"] or {}).get("text") or ""), "화면에 사유")
    check([a["act"] for a in (c.state.get("band") or {}).get("actions", [])] == ["alarm_clear"], "띠에 [확인] 버튼")
    check(not c.state["relays"]["heater_on"], "오류 정책 · 히트건 꺼짐")


@case("13 조건 단계 · 히트건 릴레이 거절")
def c13(c):
    ok, r = c.send("relay", n=10, on=True)
    check(not ok and "히트건" in str(r), "수동 켜기 거절 (%s)" % (r,))
    for typ in ("relay", "measure", "wait_temp", "repeat", "log", "z"):
        cond = [{"name": "x", "steps": [{"type": "move_sample"}, {"type": typ, "n": 10, "on": True}]}]
        ok, r = c.send("settings_save", patch={"condition.items": cond, "condition.active": "x"})
        check(not ok and "알 수 없는 단계" in str(r), "지운 단계 '%s' 거절 (%s)" % (typ, r))
    cond = [{"name": "x", "steps": [{"type": "move_sample"}, {"type": "cooler", "action": "on", "target": "n"}]}]
    ok, r = c.send("settings_save", patch={"condition.items": cond, "condition.active": "x"})
    check(not ok and "쿨러" in str(r), "쿨러 대상 거절 (%s)" % (r,))
    ok, r = c.send("relay", n=2, on=True)
    check(ok, "다른 릴레이는 켜짐")


@case("14 카메라 이동 · 기울기")
def c14(c):
    t = full_setup(c)
    c.dry("world", kw={"shift_px": [8.0, -5.0], "rot_deg": 0.4})
    ok, r = c.send("capture")
    st = c.state
    check(ok and st["shot"]["coords_ok"] and len(st["samples"]) == len(t["chips"]),
          "작은 이동 보정 · 인식 %d · 이동 %.2f mm" % (len(st["samples"]), st["shot"]["cam"]["shift_mm"]))
    check(chips_err(c, t) < 0.8, "좌표 오차 %.2f mm" % chips_err(c, t))
    c.dry("world", kw={"shift_px": [0.0, 0.0], "rot_deg": 0.0, "tilt": [2.0e-5, -1.5e-5]})
    ok, r = c.send("capture")
    check(ok and c.state["shot"]["coords_ok"], "기울기 보정 (%s · 잔차 %s)" % (c.state["shot"]["cam"]["model"], c.state["shot"]["cam"]["resid"]))
    check(chips_err(c, t) < 1.0, "기울기 좌표 오차 %.2f mm" % chips_err(c, t))
    c.dry("world", kw={"shift_px": [150.0, 60.0], "rot_deg": 0.0, "tilt": [0.0, 0.0]})
    ok, r = c.send("capture")
    st = c.state
    check(ok and len(st["samples"]) > 0, "큰 이동에도 검출은 함 (%d개)" % len(st["samples"]))
    check(not st["shot"]["coords_ok"] and all(s["X"] is None for s in st["samples"]), "좌표 안 만듦")
    check("카메라 위치 확인" in ((st["alarm"] or {}).get("text") or ""), "경보 (%s)" % (st["alarm"],))
    check([a["act"] for a in (st.get("band") or {}).get("actions", [])] == ["capture"], "띠에 [다시 촬영] 버튼")
    ok, r = start_run(c, TOUCH)
    check(not ok, "측정 시작 거절 (%s)" % (r,))
    ok, r = c.send("goto_sample", no=1)
    check(not ok, "이동 거절 (%s)" % (r,))


@case("15 카메라 이동 뒤 빈 판 사진 교체")
def c15(c):
    t = full_setup(c)
    c.dry("world", kw={"shift_px": [14.0, 9.0], "rot_deg": 0.3, "empty": True})
    ok, r = c.send("ref_capture")
    check(ok and r["moved"] is not None, "교체 · 기하 옮김 (%s)" % (r,))
    c.dry("world", kw={"empty": False})
    check(c.state["calib"]["status"] == "done", "좌표 보정 유지 (%s)" % c.state["calib"]["text"])
    ok, r = c.send("capture")
    check(ok and len(c.state["samples"]) == len(t["chips"]), "새 촬영 인식 %d" % len(c.state["samples"]))
    check(chips_err(c, t) < 0.8, "교체 뒤 좌표 오차 %.2f mm" % chips_err(c, t))
    ok, r = c.send("verify_position")
    check(ok and r["d"] < 0.8, "교체 뒤 위치 확인 차이 %s mm" % (r.get("d") if isinstance(r, dict) else r))
    c.dry("world", kw={"shift_px": [400.0, 300.0], "rot_deg": 20.0, "empty": True})
    ok, r = c.send("ref_capture")
    check(not ok, "정렬 실패 시 교체 거절 (%s)" % (r,))


@case("16 소켓 끊김 · 비상정지 HTTP")
def c16(c):
    ready_capture(c)
    c.ws.close()
    code, body = c.http("/estop", "POST")
    check(code == 200, "HTTP 비상정지 응답")
    c.ws = connect("ws://127.0.0.1:%d/ws" % c.port, max_size=None)
    c.pump(1.5)
    check(c.state["stage"]["locked"], "새 연결에서 잠금 확인")
    check(c.state["band"] and c.state["band"]["actions"], "새 연결에서도 띠 버튼")


@case("17 동시 촬영 · 촬영 중 측정 시작")
def c17(c):
    ready_capture(c)
    c.dry("cam_delay", s=0.5)
    i = c.send_nowait("capture")
    time.sleep(0.6)
    ok, r = c.send("capture")
    check(not ok and "촬영" in str(r), "동시 촬영 거절 (%s)" % (r,))
    ok, r = start_run(c, TOUCH)
    check(not ok, "촬영 중 측정 시작 거절 (%s)" % (r,))
    c.dry("cam_delay", s=0.0)
    c.wait_ack(i, 60)


@case("18 큐 잔류 프레임 · 이동 뒤 새 프레임")
def c18(c):
    full_setup(c)
    c.send("move_xy", x=150, y=150)
    ok, r = c.send("verify_position")
    check(ok and r["d"] < 0.8, "이동 직후 위치 확인 차이 %.2f mm (새 프레임만 사용)" % r["d"])


_ = e2e


# ---------------------------------------------------------------------------
# 통신 감시 유지 신호 · 링크 · 줄 잃음 · 비상정지 지연
# ---------------------------------------------------------------------------
HOT12 = [{"name": "hot12", "steps": [{"type": "move_sample"}, {"type": "heat", "s": 12.0}, {"type": "z_top"}]}]


@case("19 히트건 12 s · 통신 감시 5000 ms")
def c19(c):
    ready_capture(c)
    ok, r = c.send("settings_save", patch={"condition.items": HOT12, "condition.active": "hot12"})
    check(ok, "12 s 히트건 측정 조건 저장 (%s)" % (r,))
    check(c.state["settings"]["relay"]["watchdog_ms"] == 5000, "통신 감시 기본값 5000 ms")
    ok, r = start_run(c, "hot12", nos=[1])
    check(ok, "측정 시작 (%s)" % (r,))
    check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 30, "히트건"), "히트건 켜짐")
    t0 = time.time()
    check(wait_run(c, 60) and c.state["run"]["state"] == "done", "12 s 히트건이 끊기지 않고 끝남 (%.1f s)" % (time.time() - t0))
    c.pump(1.0)
    check(not c.state["alarm"], "통신 끊김 경보 없음 (%s)" % (c.state["alarm"],))
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    check(time.time() - t0 >= 11.0, "히트건 시간 ≈ 12 s 만큼 켜 둠")


c19.patch = {"sim": {"time_scale": 1.0}}


@case("20 링크 멈춤 · 경보 · 오류")
def c20(c):
    if e2e.MODE != "fake":
        print("  (fake-serial 전용)")
        return
    ready_capture(c)
    ok, r = start_run(c, HEAT, nos=[1])
    check(ok, "측정 시작")
    check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 60, "히트건"), "히트건 중")
    c.dry("cut", s=14.0)
    check(wait_run(c, 90) and c.state["run"]["state"] == "error", "측정이 오류로 끝남 (%s)" % (c.state["run"] or {}).get("state"))
    check("통신 끊김" in ((c.state["alarm"] or {}).get("text") or ""), "경보 · 통신 끊김 (%s)" % (c.state["alarm"],))
    check(c.state["run"]["results"].get("1", {}).get("status") == "오류", "샘플 결과 = 오류")
    time.sleep(10)
    c.pump(2.0)
    check(not c.state["relays"]["heater_on"], "링크가 돌아온 뒤 히트건 꺼짐")
    ok, r = c.send("link_check")
    check(ok and not c.state["alarm"], "[연결 확인] 뒤 경보 닫힘 (%s)" % (r,))


c20.patch = {"sim": {"time_scale": 3.0}}


@case("21 줄 하나 잃음")
def c21(c):
    if e2e.MODE != "fake":
        print("  (fake-serial 전용)")
        return
    ready_capture(c)
    c.dry("drop", n=1)
    time.sleep(4.0)
    c.pump(1.0)
    check(c.state["stage"]["connected"] and not c.state["alarm"], "한 줄을 잃어도 연결 유지 · 경보 없음 (%s)" % (c.state["alarm"],))
    ok, r = c.send("move_xy", x=50, y=50)
    check(ok, "그 뒤 이동 정상 (%s)" % (r,))


@case("22 비상정지 지연")
def c22(c):
    if e2e.MODE != "fake":
        print("  (fake-serial 전용)")
        return
    import json as _json
    import time as _t
    ready_capture(c)
    runs = os.path.join(c.data, "runs")
    for i in range(200):                                    # 측정 폴더 200개
        d = os.path.join(runs, "20250101_%06d_더미" % i)
        os.makedirs(d, exist_ok=True)
        _json.dump({"state": "done", "results": {str(k): {"status": "완료"} for k in range(1, 10)}, "summary": {"완료": 9}}, open(os.path.join(d, "run.json"), "w", encoding="utf-8"))
    lat = {}

    def estop_ms():
        t0 = _t.monotonic()
        c.http("/estop", "POST")
        ok, r = c.dry("bang")
        return (r["t"] - t0) * 1000.0 if r and r.get("t") else 9999.0

    ok, r = start_run(c, HEAT, nos=[1, 2, 3])
    c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 60)
    lat["측정 중"] = estop_ms()
    wait_run(c, 60)
    recover(c)
    c.dry("cam_delay", s=0.3)
    i = c.send_nowait("capture")
    time.sleep(1.0)
    lat["촬영 · 검출 중"] = estop_ms()
    c.wait_ack(i, 60)
    c.dry("cam_delay", s=0.0)
    recover(c)
    lat["결과 목록 200개 · 대기"] = estop_ms()
    c.dry("slow", s=0.5)
    c.dry("cpu", s=6.0, n=6)                                # 서버 안에서 파이썬 스레드 6개가 CPU 를 태운다(GIL 경쟁)
    i = c.send_nowait("jog", axis="x", mm=60.0)
    time.sleep(0.4)
    lat["이동 중 + CPU 부하 스레드 6개"] = estop_ms()
    c.wait_ack(i, 30)
    c.dry("slow", s=0.0)
    for k, v in lat.items():
        check(v <= 100.0, "비상정지 지연 %s = %.0f ms (기준 100 ms 이하)" % (k, v))
    print("  비상정지 지연(ms): " + ", ".join("%s %.0f" % kv for kv in lat.items()))


# ---------------------------------------------------------------------------
# 실장비 시험용 새 시나리오
# ---------------------------------------------------------------------------
@case("23 위치 확인 50곳 스윕")
def c23(c):
    """스테이지를 50곳(X 10–190 × Y 20–160)으로 옮기며 위치 확인. 헤드 마커가 보이는 자리는 전부 통과해야 한다(판을 가려도)."""
    full_setup(c)
    xs = [10, 55, 100, 145, 190]
    ys = [20 + 140 * k / 9.0 for k in range(10)]
    n_ok = n_hidden = 0
    bad = []
    for x in xs:
        for y in ys:
            ok0, _ = c.send("move_xy", x=float(x), y=float(y))
            if not ok0:
                bad.append(("이동", x, y))
                continue
            ok, r = c.send("verify_position")
            if ok:
                n_ok += 1
                if r["d"] > 0.8:
                    bad.append(("차이 %.2f" % r["d"], x, y))
            else:
                msg = str(r)
                if "안 보임" in msg or "가장자리" in msg:
                    n_hidden += 1                        # 마커가 사진에 안 보이는 자리는 허용(통과 대상이 아님)
                else:
                    bad.append((msg[:50], x, y))
    check(not bad, "50곳 중 마커가 보이는 자리 전부 통과 · 통과 %d · 마커 안 보임 %d · 실패 %s" % (n_ok, n_hidden, bad[:5]))
    check(n_ok + n_hidden == 50, "통과 %d / 50 (마커 안 보임 %d)" % (n_ok, n_hidden))


@case("24 첫 실행 · 원점 없음 → 버튼으로 Z · X · Y 원점 → 설정 → 촬영 → 측정")
def c24(c):
    st = c.state["stage"]
    check(not st["z_ok"] and not st["xy_ok"], "원점 없음으로 시작 (Z %s · XY %s)" % (st["z_ok"], st["xy_ok"]))
    check(c.state["eeprom"] in ("none", "unsaved"), "연결 시 EEPROM 상태 = %s" % c.state["eeprom"])
    b = c.state["band"]
    check(b and b["text"] == "원점 없음" and [a["act"] for a in b["actions"]] == ["origin"] and b["actions"][0]["args"] == {"axis": "z"}, "띠: 원점 없음 · [Z 원점 잡기] (%s)" % (b,))
    ok, r = c.send("origin", axis="x")
    check(not ok and "Z 원점" in str(r), "Z 전에 X 거절 (%s)" % (r,))
    pressed = follow_band(c, 6)
    check(pressed == ["Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"], "띠 버튼 순서 %s" % pressed)
    st = c.state["stage"]
    check(st["z_ok"] and st["xy_ok"] and not st["locked"] and not c.state["band"], "원점 완료 · 띠 사라짐")
    cmds = serial_cmds(c)
    check([x for x in cmds if x.startswith("fz")] == ["fz z 1600", "fz x 1600", "fz y 1600"], "fz 명령 %s" % [x for x in cmds if x.startswith("fz")])
    t = full_setup(c)
    ok, r = c.send("capture")
    check(ok and len(c.state["samples"]) == len(t["chips"]), "촬영 · 인식 %d" % len(c.state["samples"]))
    no = c.state["samples"][0]["no"]
    ok, r = c.send("goto_sample", no=no)
    ok2, r2 = c.send("jog", axis="z", mm=15.0)
    ok3, r3 = c.send("contact_save", no=no)
    check(ok and ok2 and ok3, "접촉 높이 저장 (%s)" % (r3,))
    ok, r = start_run(c, TOUCH, nos=[1])
    check(ok and wait_run(c, 120) and c.state["run"]["state"] == "done", "측정 시작 · 끝 (%s)" % (r,))


c24.env = {"WAFER_STAGE_DRY_NO_HOME": "1", "WAFER2_FAKE_EEPROM": "none"}


@case("25 비상정지 회복 · 띠 버튼만 (헤드 위치 보정 있음)")
def c25(c):
    ready_capture(c)
    estop_http(c)
    c.pump(1.0)
    b = c.state["band"]
    check(b and b["err"] and "비상정지" in b["text"], "띠 · 비상정지 (%s)" % (b and b["text"]))
    pressed = follow_band(c, 6)
    check(pressed[:3] == ["Z 원점 잡기", "위치 확인", "적용"], "버튼 순서 %s" % pressed)
    check(not c.state["stage"]["locked"] and not c.state["band"], "회복 뒤 잠금 · 띠 없음")
    ok, r = c.send("move_xy", x=60, y=60)
    check(ok, "이동 재개 (%s)" % (r,))


@case("26 비상정지 회복 · 띠 버튼만 (헤드 위치 보정 없음)")
def c26(c):
    t = e2e.truth_px(c)
    e2e.setup_area_ref(c, t)
    ok, r = e2e.setup_calib(c, t)
    check(ok, "좌표 보정만 완료")
    check(c.state["calib"]["head"]["status"] != "done", "헤드 위치 보정 없음")
    estop_http(c)
    c.pump(1.0)
    pressed = follow_band(c, 6)
    check(pressed == ["Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"], "버튼 순서 %s" % pressed)
    check(not c.state["stage"]["locked"] and not c.state["band"], "회복 뒤 잠금 · 띠 없음")


@case("27 접촉 높이 저장 · 내려 보기 · 비어 있으면 거절")
def c27(c):
    c.dry("fault", name="x", on=False)
    ready_capture(c)
    ok, r = c.send("settings_save", patch={"contact_z_mm": None})
    ok, r = start_run(c, TOUCH, nos=[1])
    check(not ok and "접촉 높이 필요" in str(r), "접촉 높이가 비면 시작 거절 (%s)" % (r,))
    ok, r = c.send("contact_try", no=1)
    check(not ok and "접촉 높이" in str(r), "비어 있으면 내려 보기 거절 (%s)" % (r,))
    ok, r = c.send("contact_save", no=1)
    check(not ok, "샘플 위가 아니면 저장 거절 (%s)" % (r,))
    c.send("goto_sample", no=1)
    c.send("jog", axis="z", mm=17.5)
    ok, r = c.send("contact_save", no=1)
    check(ok and abs(r["z"] - 17.5) < 0.1 and abs(c.state["contact"]["z"] - 17.5) < 0.1, "접촉 높이 저장 (%s)" % (r,))
    c.send("z_top")
    ok, r = c.send("contact_try", no=1)
    check(ok and abs(c.state["stage"]["z"] - 17.5) < 0.1, "내려 보기 → Z %.2f" % c.state["stage"]["z"])
    mz = [x for x in serial_cmds(c) if x.startswith("mz")]
    check(len(mz) >= 2 and mz[-2] == "mz 15.50" and mz[-1] == "mz 17.50", "접근 앞까지 보통 속도 → 남은 2 mm 느리게 (%s)" % mz[-3:])
    check("v 1500" in serial_cmds(c)[-6:], "마지막 구간 느린 속도 v 1500")
    c.send("z_top")
    c.send("move_xy", x=150, y=150)
    ok, r = c.send("contact_try", no=1)
    check(not ok and "샘플" in str(r), "X · Y 가 샘플 위가 아니면 거절 (%s)" % (r,))
    c.dry("z", mm=0.0)
    ok, r = start_run(c, TOUCH, nos=[1, 2])
    check(ok and wait_run(c, 120) and c.state["run"]["state"] == "done", "측정 (%s)" % (r,))
    check(all(abs((v["contact_z"] or 0) - 17.5) < 0.1 for v in c.state["run"]["results"].values()), "접촉 Z = 17.5 (%s)" % [v["contact_z"] for v in c.state["run"]["results"].values()])
    ok, r = c.send("settings_save", patch={"contact_z_mm": 99.0})
    check(not ok, "가동범위 밖 접촉 높이 거절 (%s)" % (r,))


@case("28 히트건 n s 상한 · 시간 되면 자동으로 끔")
def c28(c):
    ok, r = c.send("settings_save", patch={"condition.items": [{"name": "x", "steps": [{"type": "move_sample"}, {"type": "heat", "s": 31.0}]}], "condition.active": "x"})
    check(not ok and "상한" in str(r), "상한(30 s)을 넘는 히트건 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"condition.items": [{"name": "x", "steps": [{"type": "move_sample"}, {"type": "heat", "s": 0.0}]}], "condition.active": "x"})
    check(not ok, "0 s 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"condition.items": [{"name": "h6", "steps": [{"type": "move_sample"}, {"type": "heat", "s": 6.0}, {"type": "z_top"}]}], "condition.active": "h6"})
    check(ok, "상한 이내 저장 (%s)" % (r,))
    ready_capture(c)
    t0 = time.time()
    ok, r = start_run(c, "h6", nos=[1])
    check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 30, "히트건"), "히트건 켜짐")
    t1 = time.time()
    check(wait_run(c, 60) and c.state["run"]["state"] == "done", "측정 끝")
    c.pump(0.8)
    check(not c.state["relays"]["heater_on"], "시간이 되자 자동으로 꺼짐")
    check(5.0 <= time.time() - t1 <= 12.0, "켠 시간 ≈ 6 s (%.1f)" % (time.time() - t1))
    ok, r = c.send("settings_save", patch={"heater.max_on_s": 5.0})
    check(not ok, "조건보다 작은 상한 거절 (%s)" % (r,))


c28.patch = {"sim": {"time_scale": 1.0}, "heater": {"relay": 10, "max_on_s": 30.0}}


@case("29 쓰기 막힘")
def c29(c):
    if e2e.MODE != "fake":
        print("  (fake-serial 전용)")
        return
    ready_capture(c)
    c.dry("block_write", s=4.0)
    t0 = time.time()
    c.http("/estop", "POST")
    dt = time.time() - t0
    check(dt < 3.0, "쓰기가 막혀도 비상정지 요청이 영원히 막히지 않음 (%.2f s)" % dt)
    c.pump(1.5)
    check("통신 끊김" in ((c.state["alarm"] or {}).get("text") or "") or c.state["stage"]["locked"], "통신 끊김 경보 또는 잠금 (%s)" % (c.state["alarm"],))
    time.sleep(4.0)
    ok, r = c.send("link_check")
    check(ok, "막힘이 풀린 뒤 연결 확인 (%s)" % (r,))
    b = c.state.get("band")
    check(b and b["actions"], "회복 버튼이 있음")


@case("30 이전 프로그램 설정 가져오기 (읽기 전용 사본)")
def c30(c):
    import hashlib
    import json as _json
    import shutil
    import stat
    old = os.path.join(e2e.ROOT, "..", "wafer_stage", "settings.json")
    if not os.path.exists(old):
        print("  (이전 설정 파일 없음 - 건너뜀)")
        return
    cp = os.path.join(c.tmp, "old_settings.json")
    shutil.copy2(old, cp)
    os.chmod(cp, stat.S_IREAD)                                    # 읽기 전용 사본
    h0 = hashlib.md5(open(cp, "rb").read()).hexdigest()
    h_orig0 = hashlib.md5(open(old, "rb").read()).hexdigest()
    raw = _json.load(open(cp, encoding="utf-8"))
    ok, r = c.send("settings_import_old", path=cp)
    check(ok, "가져오기 (%s)" % (r if not ok else sorted(r["imported"])))
    c.pump(2.5)
    st = c.state["settings"]
    for k in ("camera_index", "exposure", "wb_temperature"):
        check(st[k] == raw[k], "%s = %s" % (k, st[k]))
    check(st["limits"] == raw["limits"], "가동범위 %s" % st["limits"])
    check(st["motion"]["speed_pps"] == raw["motion"]["speed_pps"] and st["motion"]["accel"] == raw["motion"]["accel"], "속도 · 가속")
    check(st["capture_xy"] == raw["capture_xy"] and st["park_xy"] == raw["park_xy"], "촬영 위치 %s · 대기 위치 %s" % (st["capture_xy"], st["park_xy"]))
    check([r_["enabled"] for r_ in st["relay"]["items"]] == [r_["enabled"] for r_ in raw["relay"]["items"]], "릴레이 사용 여부")
    check(st["cooler"]["zones"]["rows"] == raw["cooler"]["zones"]["rows"] and [x["relay"] for x in st["cooler"]["zones"]["cells"]] == [x["relay"] for x in raw["cooler"]["zones"]["cells"]], "쿨러 구역 릴레이")
    check(st["serial_port"] == raw["serial_port"], "포트 %s" % st["serial_port"])
    check("heater" in st and st["heater"]["relay"] == raw["heater"]["relay"], "히트건 릴레이 %s" % st["heater"]["relay"])
    check(c.state["stage"] and c.state["stage"]["connected"], "가져온 뒤 스테이지 재연결 (%s)" % c.state["phase"])
    check(hashlib.md5(open(cp, "rb").read()).hexdigest() == h0 and hashlib.md5(open(old, "rb").read()).hexdigest() == h_orig0, "원본 · 사본 파일 그대로")
