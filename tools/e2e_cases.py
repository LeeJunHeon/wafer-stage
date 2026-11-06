"""e2e_cases.py - e2e 시나리오 04~18 (tools/e2e.py 가 불러온다)."""
import os
import time

import e2e
from e2e import case, check, connect, full_setup, chips_err


# ---------------------------------------------------------------------------
# 헬퍼
# ---------------------------------------------------------------------------
def serial_cmds(c):
    """dry 스테이지가 받은 명령 목록(시리얼 로그의 '->' 줄)."""
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
    bad = [x for x in tail if x.split()[0] in ("mx", "my", "mz", "jx", "jy", "jz", "sp", "gx", "gy", "gz", "g", "v")
           or (x.startswith("r ") and x.endswith(" 1"))]
    return bad, tail


def recover(c):
    """회복 순서: Z 끝단 -> Z 원점 등록 -> 위치 확인 -> 적용."""
    c.send("jog", axis="z", mm=-2.0)
    ok1, _ = c.send("register_z")
    ok2, r = c.send("verify_position")
    ok3, r3 = c.send("apply_position")
    return ok1 and ok2 and ok3, (r, r3)


def start_run(c, cond="기본", nos=None, mode="continuous"):
    return c.send("measure_start", condition=cond, mode=mode, nos=nos, ok_weak=True)


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
    ok, r = start_run(c, "가열 측정", nos=[1, 2])
    check(ok, "측정 시작 (%s)" % (r,))
    check(wait_run(c, 280), "측정 끝")
    run = c.state["run"]
    check(run["state"] == "done", "상태 done (%s %s)" % (run["state"], run["message"]))
    res = run["results"]
    check(sorted(map(int, res)) == [1, 2], "결과 2개")
    for k, v in res.items():
        check(v["status"] in ("완료", "확인 필요") and v["t_max"] and v["t_max"] > 100,
              "샘플 %s %s · 최고 %s °C · 가열 %s s" % (k, v["status"], v["t_max"], v["heat_s"]))
    d = run["dir"]
    for fn in ("run.json", "samples.csv", "sample_01.csv", "sample_02.csv"):
        check(os.path.exists(os.path.join(d, fn)), "결과 파일 " + fn)
    check(open(os.path.join(d, "samples.csv"), "rb").read(3) == b"\xef\xbb\xbf", "CSV UTF-8 BOM")
    c.pump(1.0)
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    check(c.state["phase"] == "대기", "상태 대기 (%s)" % c.state["phase"])
    check(abs(c.state["stage"]["z"]) < 0.06, "Z 맨 위")


@case("06 샘플마다 확인 · 선택 샘플만")
def c06(c):
    ready_capture(c)
    ok, r = start_run(c, "기본", nos=[2, 5, 7], mode="confirm")
    check(ok, "시작 (%s)" % (r,))
    for k in range(2):
        c.wait(lambda s: s["run"] and s["run"]["waiting"] == "다음 샘플 확인", 120, "확인 대기")
        check(c.state["run"]["waiting"] == "다음 샘플 확인" and c.state["phase"] == "일시정지", "샘플 %d 끝 · 확인 대기" % (k + 1))
        c.send("measure_answer", value="next")
    wait_run(c, 120)
    check(sorted(map(int, c.state["run"]["results"])) == [2, 5, 7], "선택한 3개만 측정 %s" % list(c.state["run"]["results"]))
    check(c.state["run"]["state"] == "done", "끝")


@case("07 일시정지 → 조그 → 기준점 기록 → 다시 시작")
def c07(c):
    ready_capture(c)
    start_run(c, "기본")
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


@case("08 가열 중 중지")
def c08(c):
    ready_capture(c)
    start_run(c, "가열 측정", nos=[3])
    check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 120, "가열 시작"), "가열 시작")
    c.send("measure_stop")
    check(wait_run(c, 60) and c.state["run"]["state"] == "stopped", "중지됨")
    c.pump(1.5)
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")
    st = c.state["stage"]
    check(abs(st["z"]) < 0.06, "Z 맨 위")
    check(abs(st["x"] - 10.0) < 0.2 and abs(st["y"] - 20.0) < 0.2, "대기 위치 (%.1f, %.1f)" % (st["x"], st["y"]))


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
    # (재현 3)
    ok, r = recover(c)
    cmds = serial_cmds(c)
    check(ok, "회복 순서 완료")
    iz = max(i for i, x in enumerate(cmds) if x == "zz")
    check(cmds[iz - 1].startswith("jz 320"), "(재현 3) 원점 등록 전 끝단 이격 jz 320 (%s)" % cmds[iz - 1])
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
    i = c.send_nowait("jog", axis="x", mm=120.0)
    time.sleep(0.6)
    estop_http(c)
    c.wait_ack(i, 30)
    c.dry("slow", s=0.0)
    bad, tail = after_estop(c)
    check(not bad, "(조그 조각 대기 중) 남은 조각 안 나감 %s" % bad)
    recover(c)
    # (가열 중)
    ok, r = start_run(c, "가열 측정", nos=[4])
    check(ok, "(가열 중) 측정 시작 (%s)" % (r,))
    c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 120)
    estop_http(c)
    c.pump(1.0)
    bad, tail = after_estop(c)
    check(not c.state["relays"]["heater_on"], "(가열 중) 히트건 꺼짐")
    wait_run(c, 60)
    check(c.state["run"]["state"] in ("stopped", "error") and not bad, "(가열 중) 측정 중단 · 움직임 없음 %s" % bad)
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


@case("10 이동 시간 초과 · 재동기")
def c10(c):
    ready_capture(c)
    c.dry("hang", on=True)
    t0 = time.time()
    ok, r = c.send("move_xy", x=60, y=60, timeout=90)
    check(not ok and "시간 초과" in str(r), "시간 초과 보고 (%s) %.1f s" % (r, time.time() - t0))
    check(any(x.startswith("! ") for x in serial_cmds(c)), "정지 명령 전송")
    c.pump(1.0)
    check(c.state["stage"]["connected"], "연결 유지")
    check(not c.state["stage"]["xy_ok"], "위치 못 믿음 표시")
    ok, r = c.send("move_xy", x=60, y=60)
    check(not ok, "위치 확인 전 이동 거절 (%s)" % (r,))
    ok, r = recover(c)
    check(ok, "위치 확인 · 적용 뒤 재개")
    ok, r = c.send("move_xy", x=60, y=60)
    check(ok, "이동 재개 (%s)" % (r,))


@case("11 보드 재부팅 감지")
def c11(c):
    ready_capture(c)
    start_run(c, "가열 측정", nos=[1])
    c.wait(lambda s: s["run"] and s["run"]["cur"] is not None, 60)
    c.dry("reboot")
    c.pump(1.0)
    check("재부팅" in ((c.state["alarm"] or {}).get("text") or ""), "경보 · 재부팅 (%s)" % (c.state["alarm"],))
    check(c.state["stage"]["locked"], "원점 확인 요구 · 잠금")
    wait_run(c, 60)
    check(c.state["run"]["state"] in ("stopped", "error"), "측정 중지")
    check(any(x.startswith("wd ") for x in serial_cmds(c)[-15:]), "wd 재전송")


c11.patch = {"sim": {"time_scale": 3.0}}


@case("12 결과 파일 잠김 · 영상 예외")
def c12(c):
    ready_capture(c)
    c.dry("fault", name="csv_lock", on=True)
    start_run(c, "가열 측정", nos=[1])
    wait_run(c, 200)
    run = c.state["run"]
    check(run["state"] == "done" and any("열려 있음" in w for w in run["warns"]), "CSV 잠김 → _1 이름 · 경고 (%s)" % (run["warns"],))
    c.dry("fault", name="csv_lock", on=False)
    c.dry("fault", name="step_exc", on=True)
    start_run(c, "기본", nos=[2])
    wait_run(c, 60)
    run = c.state["run"]
    check(run["state"] == "error" and run["results"]["2"]["status"] == "오류", "예외 → 오류 결과 (%s)" % (run["results"].get("2", {}).get("status"),))
    check("측정 오류" in ((c.state["alarm"] or {}).get("text") or ""), "화면에 사유")
    check(not c.state["relays"]["heater_on"], "오류 정책 · 히트건 꺼짐")


@case("13 히트건 릴레이 거절")
def c13(c):
    ok, r = c.send("relay", n=10, on=True)
    check(not ok and "히트건" in str(r), "수동 켜기 거절 (%s)" % (r,))
    cond = [{"name": "x", "steps": [{"type": "move_sample"}, {"type": "relay", "n": 10, "on": True}]}]
    ok, r = c.send("settings_save", patch={"condition.items": cond, "condition.active": "x"})
    check(not ok and "히트건" in str(r), "릴레이 단계 거절 (%s)" % (r,))
    cond = [{"name": "x", "steps": [{"type": "move_sample"}, {"type": "cooler", "action": "on", "target": "n", "n": 10}]}]
    ok, r = c.send("settings_save", patch={"condition.items": cond, "condition.active": "x"})
    check(not ok and "히트건" in str(r), "쿨러 n 거절 (%s)" % (r,))
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
    ok, r = start_run(c, "기본")
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


@case("17 동시 촬영 · 촬영 중 측정 시작")
def c17(c):
    ready_capture(c)
    c.dry("cam_delay", s=0.5)
    i = c.send_nowait("capture")
    time.sleep(0.6)
    ok, r = c.send("capture")
    check(not ok and "촬영" in str(r), "동시 촬영 거절 (%s)" % (r,))
    ok, r = start_run(c, "기본")
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
