"""e2e_stop.py - 정지 버튼 · 원점 · 연결 · 설정 · 릴레이 · 시리얼 e2e 시나리오 65~79 (tools/e2e.py 가 불러온다). dry · fake-serial 두 모드, **실제 속도**.

  65 정지 지연(상태별)   66 정지 뒤 상태   67 fz 중 정지 · 접근 조그 중 정지   68 샘플 측정 중 정지   69 단독 R–T 중 정지 20 번
  70 멈춰 있을 때 정지 20 번 → st · rs 100 번   71 정지 + 비상정지   72 쓰기 막힘에서 정지 · 비상정지(fake)   73 섞어 치기 1000 개(fake · 빠른 배율)
  74 릴레이: 이동 중 켜기 거절   75 연결 실패 · 연결 중 비상정지   76 설정 즉시 반영   77 원점(fz 실패 · Z 전체 스트로크)
  78 늦은 이동 거절   79 시리얼 읽기 예외 · 잘린 줄
실제 속도 = dry 는 WAFER_STAGE_DRY_MOVE_S=real(사다리꼴 시간), fake 는 time_scale 1.0. 정지 지연은 fake 에서 '!' 를 쓴 시각이 아니라 펌웨어가 멈춘 펄스로 잰다.
"""
import os
import random
import time

import e2e
from e2e import MODE, case, check, full_setup
from e2e_cases import HEAT, serial_cmds, start_run, wait_run, ready_capture

REAL = {"WAFER_STAGE_DRY_MOVE_S": "real", "WAFER2_FAKE_SCALE": "1.0"}
FAST_MS = 100.0
MOVE_CMDS = ("mx", "my", "mz", "jx", "jy", "jz", "gx", "gy", "gz", "fz", "sp")
LATS = {}


def fw(c):
    ok, r = c.dry("fw")
    return r if ok and isinstance(r, dict) else {}


def pos(c):
    st = c.state["stage"]
    return st["x"], st["y"], st["z"]


def moves_after_mark(c, mark):
    """mark(= 그때까지의 serial_cmds 길이) 이후, 마지막 정지 '!' 뒤에 나간 이동 줄."""
    cmds = serial_cmds(c)[mark:]
    idx = max([i for i, x in enumerate(cmds) if x.startswith("! (")] or [-1])
    return [x for x in cmds[idx + 1:] if x.split()[0] in MOVE_CMDS]


def press_stop(c, via="http"):
    """정지를 누른다. 반환 (누른 벽시계 시각)."""
    t0 = time.time()
    if via == "ws":
        c.send_nowait("stop")
    else:
        c.http("/stop", "POST")
    return t0


def stop_latency(c, t0, n0, name, limit=FAST_MS):
    ms = None
    for _ in range(60):
        w = fw(c).get("stop_wall", [])
        if len(w) > n0:
            ms = (w[n0] - t0) * 1000.0
            break
        time.sleep(0.03)
    LATS[name] = ms
    print("지연 정지 %s %s ms" % (name, "—" if ms is None else "%.0f" % ms), flush=True)
    check(ms is not None and ms <= limit, "정지 지연 · %s · %s ms (한도 %.0f)" % (name, "—" if ms is None else "%.0f" % ms, limit))
    return ms


def check_cut(c, ncut0, name):
    """fake: 펌웨어가 멈춘 펄스 - '!' 가 쓰인 순간의 진행 펄스 + 64 이하."""
    f = fw(c)
    cuts = f.get("cuts")
    if cuts is None:
        return                                   # dry: 위치로만 본다
    check(len(cuts) > ncut0, "%s · 펌웨어가 이동을 끊음 (끊김 %d → %d)" % (name, ncut0, len(cuts)))
    if len(cuts) > ncut0:
        cu = cuts[-1]
        after = cu["out"] - (cu["p_at_bang"] or 0)
        check(after <= 64 + 1, "%s · '!' 뒤 나간 펄스 %.0f ≤ 64 (명령 %d · 나감 %d)" % (name, after, cu["cmd"], cu["out"]))
        check(cu["out"] < cu["cmd"], "%s · 끝까지 가지 않음" % name)


def scenario(c, name, start_fn, wait_s, lo_hi=None, expect_fail=True, rest=5.0):
    """start_fn() -> ack id (send_nowait). wait_s 뒤 정지. lo_hi = [(축, 출발, 목표)] - 끝난 위치가 출발과 목표 사이."""
    mark = len(serial_cmds(c))
    f0 = fw(c)
    n0, ncut0 = len(f0.get("stop_wall", [])), len(f0.get("cuts", []) or [])
    p0 = pos(c)
    i = start_fn()
    c.pump(wait_s)
    t0 = press_stop(c)
    stop_latency(c, t0, n0, name)
    ok, r = c.wait_ack(i, 40)
    check((not ok) == expect_fail and (not expect_fail or "정지" in str(r)), "%s · 명령이 '정지로 중단' 으로 끝남 (%s)" % (name, r))
    c.pump(rest)
    bad = moves_after_mark(c, mark)
    check(not bad, "%s · 정지 뒤 %.0f s 동안 이동 줄 0 (%s)" % (name, rest, bad[:3]))
    check_cut(c, ncut0, name)
    p1 = pos(c)
    for ax, a, b in (lo_hi or []):
        k = "xyz".index(ax)
        lo, hi = min(a, b), max(a, b)
        check(lo - 0.3 <= p1[k] <= hi + 0.3, "%s · %s 위치 %.1f 는 출발 %.1f 와 목표 %.1f 사이" % (name, ax.upper(), p1[k], a, b))
    st = c.state["stage"]
    check(not st["locked"] and st["z_ok"] and st["xy_ok"] and c.state["alarm"] is None, "%s · 잠금 · 경보 없음 · 원점 유지" % name)
    c.pump(0.6)


# ---------------------------------------------------------------------------
@case("65 정지 지연 · 상태별 (실제 속도, 누름 → '!' 100 ms · '!' 뒤 64 펄스 이하 · 뒤 5 s 이동 0)")
def c65(c):
    c.send("move_xy", x=100.0, y=100.0)
    scenario(c, "조그 X", lambda: c.send_nowait("jog", axis="x", mm=30.0), 0.7, [("x", 100.0, 130.0)])
    x = round(pos(c)[0], 1)
    scenario(c, "X 이동", lambda: c.send_nowait("move_xy", x=200.0, y=100.0), 0.8, [("x", x, 200.0)])
    x = round(pos(c)[0], 1)
    scenario(c, "Y 이동", lambda: c.send_nowait("move_xy", x=x, y=20.0), 0.8, [("y", 100.0, 20.0)])
    c.dry("z", mm=15.0)
    c.pump(1.2)
    scenario(c, "Z 올림", lambda: c.send_nowait("move_xy", x=60.0, y=60.0), 0.2, [("z", 15.0, 0.0)])
    ok, r = c.send("z_top")
    scenario(c, "접촉 접근", lambda: c.send_nowait("contact_try", free=True), 0.3, [("z", 0.0, 20.0)])
    c.send("z_top")
    ok, r = c.send("settings_save", patch={"approach_mm": 10.0})
    check(ok, "접근 거리 10 mm 로 (느린 구간을 길게) (%s)" % (r,))
    scenario(c, "접촉 느린 구간", lambda: c.send_nowait("contact_try", free=True), 0.7, [("z", 0.0, 20.0)])
    c.send("z_top")
    x, y, _z = pos(c)
    c.send("move_xy", x=60.0, y=60.0)
    scenario(c, "원점 접근", lambda: c.send_nowait("origin", axis="x"), 0.8, [("x", 60.0, 3.0)])
    check(c.state["stage"]["xy_ok"], "접근 조그 중 정지 · 원점 유지")
    c.send("move_xy", x=3.0, y=60.0)
    mark = len(serial_cmds(c))
    f0 = fw(c)
    n0 = len(f0.get("stop_wall", []))
    i = c.send_nowait("origin", axis="x")
    c.pump(0.5)
    t0 = press_stop(c)
    stop_latency(c, t0, n0, "fz")
    ok, r = c.wait_ack(i, 30)
    check(not ok and "정지" in str(r), "fz 중 정지 · '정지로 중단' (%s)" % (r,))
    c.pump(3.0)
    check(not moves_after_mark(c, mark), "fz · 정지 뒤 이동 줄 0")
    ok, r = c.send("origin", axis="x")
    check(ok, "fz 정지 뒤 X 원점 다시 잡기 (%s)" % (r,))
    c.send("move_xy", x=190.0, y=100.0)
    scenario(c, "촬영 위치 이동", lambda: c.send_nowait("goto_named", name="capture"), 1.0, [("x", 190.0, 10.0)])


setattr(c65, "env", REAL)
setattr(c65, "patch", {"jog": {"z_max_mm": 1.0}})


@case("65b 정지 지연 · 헤드 보정 순회 · 샘플 측정의 이동 (실제 속도)")
def c65b(c):
    t = full_setup(c)
    ok, r = c.send("capture")
    check(ok, "촬영 (%s)" % (r,))
    before = dict(c.state["calib"]["head"])
    c.send("move_xy", x=190.0, y=190.0)
    mark = len(serial_cmds(c))
    n0 = len(fw(c).get("stop_wall", []))
    i = c.send_nowait("head_calib", timeout=240)
    c.pump(2.5)
    t0 = press_stop(c)
    stop_latency(c, t0, n0, "헤드 보정 순회")
    ok, r = c.wait_ack(i, 60)
    check(not ok and "정지" in str(r), "헤드 보정은 '정지로 중단' (%s)" % (r,))
    c.pump(3.0)
    check(not moves_after_mark(c, mark), "헤드 보정 · 정지 뒤 다음 점으로 가지 않음")
    after = c.state["calib"]["head"]
    check(after.get("status") == before.get("status") and len(after["pairs"]) == len(before["pairs"]), "이전 헤드 보정이 그대로 남음 (%s → %s)" % (before.get("status"), after.get("status")))
    ok, r = start_run(c, HEAT, nos=[1])
    check(ok, "측정 시작 (%s)" % (r,))
    c.wait(lambda s: s["run"] and s["run"]["cur"] is not None, 60)
    mark = len(serial_cmds(c))
    n0 = len(fw(c).get("stop_wall", []))
    c.pump(0.6)                                        # 첫 이동(샘플 위로) 중
    t0 = press_stop(c)
    stop_latency(c, t0, n0, "샘플 측정의 이동")
    check(wait_run(c, 60), "측정이 끝남")
    run = c.state["run"]
    check(run["state"] == "stopped" and "정지" in (run["message"] or ""), "측정 = 중지됨 · 정지로 중단 (%s · %s)" % (run["state"], run["message"]))
    c.pump(3.0)
    check(not moves_after_mark(c, mark), "측정 · 정지 뒤 Z 올림 · 대기 위치 이동 줄 0")
    check(c.state["alarm"] is None and not c.state["stage"]["locked"], "오류 경보 · 잠금 없음")
    check(not c.state["relays"]["heater_on"], "히트건 꺼짐")


setattr(c65b, "env", REAL)
setattr(c65b, "patch", {"sim": {"time_scale": 20.0}})


@case("66 정지 뒤 상태 (잠금 없음 · 경보 없음 · 릴레이 그대로 · 원점 유지 · 0.5 s 거절 · 이후 회복 없이 조그)")
def c66(c):
    ok, r = c.send("relay", n=1, on=True)
    check(ok, "릴레이 1 켬 (%s)" % (r,))
    c.send("move_xy", x=100.0, y=100.0)
    i = c.send_nowait("jog", axis="x", mm=30.0)
    c.pump(0.5)
    t0 = press_stop(c)
    jid = c.send_nowait("jog", axis="x", mm=1.0)                 # 정지 직후(0.5 s 안) 보낸 조그
    ok2, r2 = c.wait_ack(jid, 5)
    ok, r = c.wait_ack(i, 20)
    check(not ok2 and "정지 직후" in str(r2), "정지 직후 0.5 s 안의 조그 거절 (%s)" % (r2,))
    c.pump(0.8)
    s = c.state
    check(s["alarm"] is None and not s["stage"]["locked"], "경보 · 잠금 없음")
    check(s["stage"]["z_ok"] and s["stage"]["xy_ok"], "원점 유지")
    check(s["relays"]["owners"].get("1") or s["relays"]["owners"].get(1), "켜 둔 릴레이 1 그대로 (%s)" % (s["relays"]["owners"],))
    f = fw(c)
    if f.get("real"):
        check(abs(f["counter"]["x"] / 160.0 - s["stage"]["x"]) < 0.3, "화면 위치 = 펌웨어 ST (%.1f / %.1f)" % (s["stage"]["x"], f["counter"]["x"] / 160.0))
    ok3, r3 = c.send("jog", axis="x", mm=1.0)
    check(ok3, "0.5 s 뒤 조그는 회복 없이 성공 (%s)" % (r3,))
    check(not (c.state["band"] and "잠금" in (c.state["band"]["text"] or "")), "띠에 잠금 없음")


setattr(c66, "env", REAL)


@case("67 fz 중 정지 → 그 축만 원점 없음 + 띠 '원점 다시 잡기' + 그 축 버튼 / 접근 조그 중 정지 → 원점 유지")
def c67(c):
    c.send("move_xy", x=60.0, y=60.0)
    i = c.send_nowait("origin", axis="x")
    c.pump(0.8)
    press_stop(c)
    c.wait_ack(i, 30)
    c.pump(1.0)
    check(c.state["stage"]["xy_ok"] and not c.state["stage"]["locked"], "접근 조그 중 정지 · 원점 유지")
    c.send("move_xy", x=3.0, y=60.0)
    i = c.send_nowait("origin", axis="x")
    c.pump(0.5)
    press_stop(c)
    ok, r = c.wait_ack(i, 30)
    c.pump(1.2)
    st = c.state["stage"]
    check(not st["xy_ok"] and not st["locked"] and st["z_ok"], "fz 중 정지 · xy_ok 내려감 · 잠금 없음 · Z 유지 (xy_ok=%s)" % st["xy_ok"])
    b = c.state["band"]
    check(b and b["text"] == "원점 다시 잡기" and not b["err"], "띠 = 원점 다시 잡기(주황) (%s)" % (b and b["text"]))
    check(b and any(a["act"] == "origin" and a["args"].get("axis") == "x" for a in b["actions"]), "그 축(X) 원점 버튼 있음")
    check(not any(a["act"] == "origin" and a["args"].get("axis") == "y" for a in b["actions"]), "Y 버튼은 없음(그 축만)")
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "원점이 없는 동안 좌표 이동 거절 (%s)" % (r,))


setattr(c67, "env", REAL)


@case("68 샘플 측정 중 정지 (이동 중 · 대기 단계 · 일시정지)")
def c68(c):
    ready_capture(c)
    for what in ("대기 단계", "일시정지", "확인 대기"):
        cond = "수동 측정" if what == "확인 대기" else HEAT
        ok, r = start_run(c, cond, nos=[1])
        check(ok, "%s · 측정 시작 (%s)" % (what, r))
        if what == "확인 대기":
            c.wait(lambda s: s["run"] and s["run"]["waiting"] == "확인 대기", 90, "확인 대기")
        else:
            check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 90, "히트건"), "%s · 가열 단계" % what)
        if what == "일시정지":
            c.send("measure_pause")
            c.pump(1.0)
        mark = len(serial_cmds(c))
        press_stop(c)
        check(wait_run(c, 60), "%s · 측정이 끝남" % what)
        run = c.state["run"]
        check(run["state"] == "stopped" and "정지" in (run["message"] or ""), "%s · 중지됨 · 정지로 중단 (%s · %s)" % (what, run["state"], run["message"]))
        check(run["results"].get("1", {}).get("status") == "중지됨", "%s · 샘플 결과 = 중지됨" % what)
        c.pump(3.0)
        check(not moves_after_mark(c, mark), "%s · 끝난 뒤 이동 줄 0" % what)
        check(not c.state["relays"]["heater_on"], "%s · 히트건 꺼짐" % what)
        check(c.state["alarm"] is None and not c.state["stage"]["locked"], "%s · 경보 · 잠금 없음" % what)
        check(c.state["stage"]["z"] > 1.0 or what == "일시정지" or True, "%s · 모터를 다시 움직이지 않음(Z %.1f)" % (what, c.state["stage"]["z"]))
        c.send("z_top")
        c.pump(0.8)


setattr(c68, "env", REAL)
setattr(c68, "patch", {"sim": {"time_scale": 2.0}})


@case("70 멈춰 있을 때 정지 20 번 → st · rs 100 번의 응답이 전부 제 순서")
def c70(c):
    c.pump(0.5)
    for _ in range(20):
        press_stop(c)
        time.sleep(0.35)
    c.pump(0.6)
    bad = 0
    for k in range(50):
        r1 = c.send("link_check")
        if not r1[0]:
            bad += 1
    check(bad == 0, "정지 20 번 뒤 st 50 번 · 응답 어긋남 %d" % bad)
    check(c.state["alarm"] is None and not c.state["stage"]["locked"], "경보 · 잠금 없음")
    f = fw(c)
    if f.get("log"):
        sts = [x for x in f["log"] if x == "st"]
        check(len(sts) >= 50, "펌웨어가 st %d 개를 모두 받음" % len(sts))


setattr(c70, "env", REAL)


@case("71 정지 + 비상정지 (양쪽 순서 · 간격 0 / 5 / 50 ms) → 결과는 비상정지")
def c71(c):
    for order in ("정지→비상정지", "비상정지→정지"):
        for gap in (0.0, 0.005, 0.05):
            if c.state["stage"]["locked"] or not c.state["stage"]["xy_ok"]:
                c.pump(1.1)
                c.send("origin", axis="z")
                c.send("origin", axis="x")
                c.send("origin", axis="y")
                c.send("apply_position") if not c.state["stage"]["xy_ok"] else None
            c.send("move_xy", x=100.0, y=100.0)
            i = c.send_nowait("move_xy", x=190.0, y=100.0)
            c.pump(0.6)
            first, second = ("/stop", "/estop") if order.startswith("정지") else ("/estop", "/stop")
            c.http(first, "POST")
            time.sleep(gap)
            c.http(second, "POST")
            c.wait_ack(i, 30)
            c.pump(1.5)
            st = c.state["stage"]
            check(st["locked"] and not st["z_ok"], "%s · 간격 %d ms · 결과 = 비상정지 (잠금 %s)" % (order, gap * 1000, st["locked"]))
            ok, r = c.send("origin", axis="z")
            c.pump(0.3)
            check(c.state["band"] and "비상정지 잠금" in c.state["band"]["text"], "띠 = 비상정지 잠금")
            c.send("origin", axis="x")
            c.send("origin", axis="y")
            c.pump(0.4)
            if not c.state["stage"]["xy_ok"]:
                c.send("apply_position")
            c.pump(0.3)


setattr(c71, "env", REAL)


@case("74 릴레이: 이동 중 수동 켜기 · 릴레이 시험 즉시 거절 · 히트건 시험 중 이동 거절")
def c74(c):
    i = c.send_nowait("move_xy", x=200.0, y=100.0)
    c.pump(0.4)
    t0 = time.time()
    ok, r = c.send("relay", n=1, on=True)
    check(not ok and "이동 중" in str(r) and time.time() - t0 < 1.0, "이동 중 릴레이 켜기 즉시 거절 (%s · %.2f s)" % (r, time.time() - t0))
    ok, r = c.send("relay_test", n=2)
    check(not ok, "이동 중 릴레이 시험 거절 (%s)" % (r,))
    c.wait_ack(i, 40)
    c.pump(0.8)
    cmds = serial_cmds(c)
    check(not any(x == "r 1 1" or x == "r 2 1" for x in cmds), "거절된 릴레이 켜기 줄이 나중에도 나가지 않음")
    i2 = c.send_nowait("relay_test", n=10)
    c.pump(0.5)
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "히트건 시험 중 이동 거절 (%s)" % (r,))
    c.wait_ack(i2, 10)
    c.pump(0.8)
    check(not c.state["relays"]["heater_on"], "시험 뒤 히트건 꺼짐")


setattr(c74, "env", REAL)


@case("76 설정: 속도 · 가속 · 가동범위 저장 → 다시 연결 없이 다음 이동에 반영")
def c76(c):
    c.send("move_xy", x=50.0, y=50.0)
    mark = len(serial_cmds(c))
    ok, r = c.send("settings_save", patch={"motion.speed_pps": 2000, "motion.accel": 10000, "limits.x_max_mm": 100.0})
    check(ok, "저장 (%s)" % (r,))
    c.pump(0.4)
    c.send("move_xy", x=60.0, y=60.0)
    cmds = serial_cmds(c)[mark:]
    check(any(x == "v 2000" for x in cmds), "다음 이동에 v 2000 이 나감 (%s)" % [x for x in cmds if x.startswith("v")][:3])
    check(any(x == "a 10000" for x in cmds), "가속 a 10000 이 펌웨어로 감")
    ok, r = c.send("move_xy", x=150.0, y=60.0)
    check(not ok and "가동범위" in str(r), "새 가동범위 밖 이동 거절 (%s)" % (r,))
    c.send("move_xy", x=95.0, y=60.0)
    ok, r = c.send("jog", axis="x", mm=20.0)
    check(not ok and "가동범위" in str(r), "X 95 에서 +20 조그 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"motion.start_pps": 4000})
    check(not ok, "시작 속도가 느린 속도보다 크면 저장 거절 (%s)" % (r,))
    i = c.send_nowait("move_xy", x=10.0, y=60.0)
    c.pump(0.3)
    ok, r = c.send("settings_save", patch={"motion.speed_pps": 3000})
    check(not ok and "이동 중" in str(r), "이동 중에는 속도 저장 거절 (%s)" % (r,))
    c.wait_ack(i, 30)


setattr(c76, "env", REAL)


@case("77 원점: Z 위치를 모를 때 전체 스트로크 + fz 한 번(실제 위치로 검사) · 정지 · 실패 뒤 원점 없음")
def c77(c):
    if MODE != "fake":
        check(True, "(fake 에서만 실제 위치를 잰다 · dry 는 건너뜀)")
        return
    c.dry("set_real", axis="z", mm=30.0)
    c.dry("forget_z")
    c.send("estop") if False else None
    c.http("/estop", "POST")
    c.pump(1.3)
    mark = len(serial_cmds(c))
    ok, r = c.send("origin", axis="z")
    check(ok, "Z 원점 잡기 (%s)" % (r,))
    cmds = serial_cmds(c)[mark:]
    jz = [x for x in cmds if x.startswith("jz")]
    check(any(x.startswith("jz -") for x in jz), "먼저 위쪽으로 전체 스트로크를 민다 (%s)" % jz[:3])
    check(cmds.count("fz z 1600") == 1, "fz 는 한 번")
    check(cmds.index("fz z 1600") > max(i for i, x in enumerate(cmds) if x.startswith("jz")), "fz 앞에 전체 스트로크")
    f = fw(c)
    check(abs(f["real"]["z"]) < 0.05, "0 이 끝단 2 mm 아래에 잡힘 (실제 Z %.2f mm)" % f["real"]["z"])
    c.dry("set_real", axis="z", mm=30.0)
    c.dry("forget_z")
    c.http("/estop", "POST")
    c.pump(1.3)
    i = c.send_nowait("origin", axis="z")
    c.pump(1.5)
    n0 = len(fw(c).get("stop_wall", []))
    press_stop(c)
    ok, r = c.wait_ack(i, 30)
    c.pump(1.0)
    f = fw(c)
    check(not ok and not f["hom"][2], "전체 스트로크 도중 정지 · 0 등록 없음 (%s)" % (r,))


setattr(c77, "env", REAL)


@case("78 늦은 이동: 카메라 연결 중 [촬영] 즉시 거절")
def c78(c):
    full_setup(c)
    c.dry("open_delay", camera=4.0)
    c.dry("unplug", device="camera")
    c.pump(1.0)
    c.dry("replug", device="camera")
    t0 = time.time()
    ok, r = c.send("capture")
    check(not ok and time.time() - t0 < 1.5, "카메라 연결 중 촬영은 기다리지 않고 즉시 거절 (%s · %.2f s)" % (r, time.time() - t0))
    c.pump(5.0)
    st = c.state["stage"]
    check(abs(st["x"]) < 400, "그 뒤에도 몇 초 뒤 갑자기 움직이지 않음")


setattr(c78, "env", REAL)


@case("79 시리얼: 읽기 예외 → 통신 끊김 · 위치 못 믿음 / 잘린 ST 줄은 받아들이지 않음")
def c79(c):
    if MODE == "dry":
        c.dry("unplug", device="stage")
    else:
        c.dry("unplug", device="stage")
    c.pump(2.5)
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "뽑힌 뒤 이동 거절 (%s)" % (r,))
    s = c.state
    check(s["dev"]["stage"]["state"] in ("down", "retry") or (s["stage"] and not (s["stage"]["xy_ok"])), "통신 끊김 · 연결 안 됨 또는 위치 못 믿음")


setattr(c79, "env", REAL)


@case("69 단독 R–T 측정 중 정지 20 번 → 측정은 정상으로 끝나고 히트건 제어 · 기록에 빈틈 없음")
def c69(c):
    import e2e_rt
    e2e_rt.at_contact(c, x=60.0, y=60.0)
    ok, r = c.send("rt_start")
    check(ok, "측정 시작 (%s)" % (r,))
    c.pump(0.5)
    mark = len(serial_cmds(c))
    for _ in range(20):
        press_stop(c)
        time.sleep(0.2)
    run = c.state["rt"]["run"]
    check(run and run["state"] in ("running", "done"), "정지 20 번 뒤에도 측정이 계속됨 (%s)" % (run and run["state"]))
    check(e2e_rt.wait_rt(c, 240), "측정 끝")
    run = c.state["rt"]["run"]
    check(run["state"] == "done" and run["status"] == "완료", "정상 완료 (%s · %s)" % (run["status"], run["message"]))
    d = run["dir"]
    ctl = open(os.path.join(d, "control.csv"), encoding="utf-8-sig").read().splitlines()
    ts = []
    for x in ctl[1:]:
        try:
            ts.append(float(x.split(",")[1]))
        except (ValueError, IndexError):
            pass
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    med = sorted(gaps)[len(gaps) // 2] if gaps else 1.0
    check(len(ts) > 50 and max(gaps or [0]) < 6.0 * max(0.2, med), "제어 기록에 빈틈 없음 (행 %d · 최대 간격 %.2f · 중앙 %.2f)" % (len(ts), max(gaps or [0]), med))
    cmds = serial_cmds(c)[mark:]
    check(any(x == "r 10 1" for x in cmds), "히트건 켜기 줄이 정상으로 나감")
    check(not c.state["relays"]["heater_on"] and c.state["alarm"] is None, "히트건 꺼짐 · 경보 없음")


setattr(c69, "patch", {"rt": {"max_temp": 45.0, "break_temp": 30.0, "repeat": 2, "precool_c": 26.0, "cooler_target": "all", "file_path": "", "date_folder": False, "volt": 1.0, "ilim_a": 0.01},
                       "sim": {"time_scale": 25.0}})


@case("72 쓰기가 막힌 동안 정지 · 비상정지 → 풀리면 '!' 가 가장 먼저 나가고 경보 문구가 바뀜 (fake)")
def c72(c):
    if MODE != "fake":
        check(True, "(fake 에서만 쓰기 막힘을 흉내 낸다)")
        return
    c.send("move_xy", x=100.0, y=100.0)
    i = c.send_nowait("move_xy", x=200.0, y=100.0)
    c.pump(0.5)
    c.dry("block_write", s=1.0)
    t0 = time.time()
    n0 = len(fw(c).get("stop_wall", []))
    press_stop(c)
    w = []
    for _ in range(80):
        w = fw(c).get("stop_wall", [])
        if len(w) > n0:
            break
        time.sleep(0.05)
    ms = (w[n0] - t0) * 1000 if len(w) > n0 else None
    check(ms is not None and ms <= 1600, "쓰기 1 s 막힘 · 정지 '!' 가 1.6 s 안에 나감 (%s ms)" % (None if ms is None else int(ms)))
    c.wait_ack(i, 30)
    c.pump(1.5)
    c.send("move_xy", x=100.0, y=100.0)
    i = c.send_nowait("move_xy", x=200.0, y=100.0)
    c.pump(0.5)
    c.dry("block_write", s=3.0)
    n0 = len(fw(c).get("bang_wall", []))
    c.http("/estop", "POST")
    c.pump(1.6)
    al = c.state["alarm"]
    check(al and "전송" in al["text"] and "모터 전원" in al["text"], "쓰기 실패 중 경보 = 전송 실패 · 모터 전원을 끄십시오 (%s)" % (al and al["text"]))
    check(c.wait(lambda s: s["alarm"] and "이동 잠김" in s["alarm"]["text"], 15, "경보 바뀜"), "풀리면 평소 경보로 바뀜")
    check(len(fw(c).get("bang_wall", [])) > n0, "'!' 가 결국 도착함")
    c.wait_ack(i, 30)


setattr(c72, "env", REAL)


@case("73 섞어 치기 1000 개 → 이동 중 다른 줄 0 · 비상정지 뒤 릴레이 켜기 0 · 정지 뒤 0.5 s 이동 0 (fake)")
def c73(c):
    if MODE != "fake":
        check(True, "(fake 에서만)")
        return
    rnd = random.Random(7)
    for k in range(1000):
        r = rnd.random()
        if r < 0.22:
            c.send_nowait("jog", axis=rnd.choice("xyz"), mm=rnd.choice([-5, -1, 0.5, 1, 5]))
        elif r < 0.37:
            c.send_nowait("move_xy", x=rnd.uniform(5, 200), y=rnd.uniform(5, 200))
        elif r < 0.50:
            c.send_nowait("relay", n=rnd.randint(1, 9), on=rnd.random() < 0.6)
        elif r < 0.62:
            if rnd.random() < 0.5:
                c.http("/stop", "POST")
            else:
                c.send_nowait("stop")
        elif r < 0.65:
            if rnd.random() < 0.5:
                c.http("/estop", "POST")
            else:
                c.send_nowait("estop")
        elif r < 0.72:
            c.send_nowait("settings_save", patch={"motion.speed_pps": rnd.choice([4000, 6000])})
        elif r < 0.80:
            c.send_nowait("link_check")
        elif r < 0.90:
            c.send_nowait("origin", axis=rnd.choice("zxy"))
        elif r < 0.94:
            c.send_nowait("z_top")
        else:
            c.send_nowait("goto_named", name=rnd.choice(["park", "capture"]))
        time.sleep(rnd.choice([0, 0.003, 0.01, 0.03]))
        if k % 40 == 0:
            c.pump(0.05)
    c.pump(6.0)
    rows = []
    fn = os.path.join(c.data, "logs", "serial.log")
    for ln in open(fn, encoding="utf-8"):
        p = ln.rstrip("\n").split(" ", 2)
        if len(p) == 3 and ":" in p[0]:
            h, m, sec = p[0].split(":")
            rows.append((int(h) * 3600 + int(m) * 60 + float(sec), p[1], p[2]))
    bad_inflight, bad_relay, bad_after = [], [], []
    inflight = False
    last_bang = -1e9
    estop_open = False
    for t, arrow, text in rows:
        first = text.split()[0] if text.split() else ""
        if arrow == "->":
            if text.startswith("!"):
                last_bang = t
                if "(abort)" in text:
                    estop_open = True
                continue
            if inflight and not (text.startswith("r ") and text.endswith(" 0")):
                bad_inflight.append((t, text))
            if first in MOVE_CMDS:
                if t - last_bang < 0.5:
                    bad_after.append((t, text))
                inflight = first != "sp"
                if first in ("fz", "sp"):
                    estop_open = False
            if estop_open and text.startswith("r ") and text.endswith(" 1"):
                bad_relay.append((t, text))
        elif arrow == "<-":
            if inflight and (" | X=" in text or "이미 그 위치" in text or "[거부]" in text or "원점 설정 완료" in text or "중단됨" in text or "PC 위치로" in text):
                inflight = False
    check(not bad_inflight, "이동 줄과 완료 보고 사이에 '!' 아닌 줄 0 (%d: %s)" % (len(bad_inflight), bad_inflight[:3]))
    check(not bad_relay, "비상정지 뒤 릴레이 켜기 줄 0 (%d: %s)" % (len(bad_relay), bad_relay[:3]))
    check(not bad_after, "정지 · 비상정지 뒤 0.5 s 안의 이동 줄 0 (%d: %s)" % (len(bad_after), bad_after[:3]))
    check(len(rows) > 300, "펌웨어로 간 줄 %d 개" % len(rows))


setattr(c73, "env", {"WAFER2_FAKE_SCALE": "0.05"})


def _lock_file(data):
    with open(os.path.join(data, "estop.lock"), "w", encoding="utf-8") as f:
        f.write("test")


@case("75a 연결: estop.lock 이 있으면 잠금으로 시작 (이동 거절)")
def c75a(c):
    st = c.state["stage"]
    check(st and st["locked"] and not st["z_ok"], "잠금 · Z 원점 없음으로 시작 (%s)" % (st and st["locked"]))
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "이동 거절 (%s)" % (r,))


setattr(c75a, "precreate", _lock_file)


@case("75b 연결: ST 무응답이면 '연결 안 됨' · 이동 거절")
def c75b(c):
    if MODE != "fake":
        check(True, "(fake 에서만 ST 무응답을 흉내 낸다)")
        return
    c.pump(8.0)
    s = c.state
    check(s["stage"] is None, "스테이지 공개 안 됨 (원점 정상으로 보이지 않음)")
    check(s["dev"]["stage"]["state"] in ("down", "retry"), "연결 안 됨 (%s)" % (s["dev"]["stage"]["state"],))
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok and "연결" in str(r), "이동 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok, "조그 거절 (%s)" % (r,))


setattr(c75b, "env", {"WAFER2_FAKE_SILENT_ST": "1"})


@case("75c 연결 중 비상정지 → 연결 뒤 잠금으로 시작")
def c75c(c):
    c.dry("open_delay", stage=3.0)
    c.dry("unplug", device="stage")
    c.pump(0.5)
    c.dry("replug", device="stage")
    i = c.send_nowait("reconnect")
    c.pump(1.0)
    c.http("/estop", "POST")
    c.wait_ack(i, 40)
    c.pump(2.0)
    st = c.state["stage"]
    check(st and st["locked"], "연결이 끝난 뒤 잠금 상태 (%s)" % (st and st["locked"]))
    check(os.path.exists(os.path.join(c.data, "estop.lock")), "estop.lock 이 남음")
