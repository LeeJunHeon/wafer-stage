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


def app_log(c):
    """서버의 파일 로그 줄(임시 데이터 폴더의 logs)."""
    d = os.path.join(c.data, "logs")
    out = []
    try:
        for fn in sorted(os.listdir(d)):
            if fn.endswith(".log") and fn != "serial.log":
                with open(os.path.join(d, fn), encoding="utf-8", errors="replace") as f:
                    out += f.read().splitlines()
    except OSError:
        pass
    return out


def press_stop(c, via="http"):
    """정지를 누른다. 반환 (누른 벽시계 시각)."""
    t0 = time.time()
    if via == "ws":
        c.send_nowait("stop")
    else:
        c.http("/stop", "POST")
    return t0


def stop_latency(c, t0, n0, name, limit=FAST_MS):
    """누름 → '!' 쓰기(ms) ≤ limit. fake 면 펌웨어가 그 '!' 로 멈춘 시각(cuts.t_halt)까지도 잰다(보고 · '!' 가 가려지면 이동이 끝까지 가서
    check_cut 의 '!' 뒤 펄스 검사가 실패한다)."""
    ms = None
    for _ in range(60):
        w = fw(c).get("stop_wall", [])
        if len(w) > n0:
            ms = (w[n0] - t0) * 1000.0
            break
        time.sleep(0.03)
    LATS[name] = ms
    print("지연 정지 %s %s ms" % (name, "—" if ms is None else "%.0f" % ms), flush=True)
    check(ms is not None and ms <= limit, "정지 지연 · %s · 누름→'!' %s ms (한도 %.0f)" % (name, "—" if ms is None else "%.0f" % ms, limit))
    return ms


def halt_ms(c, t0, ncut0):
    """fake: 누름 → 펌웨어가 멈춘 시각(ms). 끊긴 이동이 없으면 None."""
    for _ in range(40):
        f = fw(c)
        cuts = f.get("cuts") or []
        if len(cuts) > ncut0 and cuts[-1].get("t_halt") is not None:
            t_halt_wall = f["wall"] - (f["mono"] - cuts[-1]["t_halt"])
            return (t_halt_wall - t0) * 1000.0
        if f.get("cuts") is None:
            return None
        time.sleep(0.05)
    return None


def wait_cmd(c, mark, pred, timeout=10.0):
    """mark 뒤 서버가 시리얼에 쓴 줄 중 pred 를 만족하는 것이 나올 때까지(이동이 실제로 시작될 때까지) 기다린다."""
    end = time.time() + timeout
    while time.time() < end:
        if any(pred(x) for x in serial_cmds(c)[mark:]):
            return True
        c.pump(0.05)
    return False


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


def scenario(c, name, start_fn, wait_s, lo_hi=None, expect_fail=True, rest=5.0, started=None):
    """start_fn() -> ack id (send_nowait). wait_s 뒤 정지(started(줄) 가 있으면 그 줄이 시리얼에 나간 뒤부터 wait_s).
    lo_hi = [(축, 출발, 목표)] - 끝난 위치가 출발과 목표 사이."""
    mark = len(serial_cmds(c))
    f0 = fw(c)
    n0, ncut0 = len(f0.get("stop_wall", [])), len(f0.get("cuts", []) or [])
    i = start_fn()
    if started is not None:
        check(wait_cmd(c, mark, started), "%s · 이동 줄이 나감" % name)
    c.pump(wait_s)
    t0 = press_stop(c)
    stop_latency(c, t0, n0, name)
    hm = halt_ms(c, t0, ncut0)
    if hm is not None:
        print("  (%s · 누름→펌웨어 멈춤 %.0f ms)" % (name, hm), flush=True)
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
    scenario(c, "접촉 느린 구간", lambda: c.send_nowait("contact_try", free=True), 0.4, [("z", 10.0, 20.0)],
             started=lambda x: x.startswith("mz ") and abs(float(x.split()[1]) - 20.0) < 0.01)                         # 느린 구간(mz 20.0)이 실제로 나간 뒤에 누른다(앞 구간과 사이에 누르면 끊을 이동이 없다)
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
    ok, r = c.send("inst_connect")
    check(ok, "측정 장비 연결 (%s)" % (r,))
    ok, lab0 = c.dry("lab", set={"out_on": True})                  # K2450 출력이 켜진 상태(측정 장비 쪽)
    n_cmd0 = len(lab0.get("cmds") or [])
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
    if f.get("relay") is not None:
        check(f["relay"][0], "펌웨어의 릴레이 1 도 켜진 채 (%s)" % f["relay"][:3])
    ok, lab1 = c.dry("lab")
    check(lab1.get("out") is True, "K2450 출력 그대로 켜짐 (%s)" % lab1.get("out"))
    check(not any(":OUTP" in x for x in (lab1.get("cmds") or [])[n_cmd0:]), "정지가 측정 장비에 아무것도 보내지 않음")
    if f.get("counter"):
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


@case("68 샘플 측정 중 정지 (이동 중 · 대기 단계 · 일시정지 · 확인 대기 · R–T 단계)")
def c68(c):
    import e2e_rt
    ready_capture(c)
    c.send("inst_connect")
    steps = [{"type": "move_sample"}, {"type": "contact"}, {"type": "rt"}, {"type": "z_top"}]
    ok, r = c.send("settings_save", patch={"condition.items": c.state["settings"]["condition"]["items"] + [{"name": "R-T 순회", "steps": steps}]})
    check(ok, "R–T 단계 측정 조건 저장 (%s)" % (r,))
    for what in ("이동 중", "대기 단계", "일시정지", "확인 대기", "R–T 단계"):
        cond = {"확인 대기": "수동 측정", "R–T 단계": "R-T 순회"}.get(what, HEAT)
        c.send("z_top")
        c.send("move_xy", x=200.0, y=200.0)                          # 샘플에서 먼 자리 - 첫 이동(샘플 위로)이 길다
        c.pump(0.6)
        mark = len(serial_cmds(c))
        ok, r = start_run(c, cond, nos=[1])
        check(ok, "%s · 측정 시작 (%s)" % (what, r))
        if what == "이동 중":
            check(wait_cmd(c, mark, lambda x: x.startswith("mx ")), "%s · 샘플 위로 X 이동이 나감" % what)
            c.pump(0.3)
        elif what == "확인 대기":
            c.wait(lambda s: s["run"] and s["run"]["waiting"] == "확인 대기", 90, "확인 대기")
        elif what == "R–T 단계":
            check(c.wait(lambda s: (s["rt"]["run"] or {}).get("state") == "running" and (s["rt"]["run"] or {}).get("phase") in ("예비 냉각", "가열", "냉각"), 120, "R–T"),
                  "%s · R–T 측정 도는 중" % what)
            c.pump(1.0)
        elif what == "대기 단계":
            check(c.wait(lambda s: s["run"] and any(x["state"] == "active" and x["text"].startswith("대기") for x in (s["run"]["steps"] or [])), 90, "대기"),
                  "%s · 대기 단계 도는 중" % what)
            c.pump(0.5)
        else:
            check(c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 90, "히트건"), "%s · 가열 단계" % what)
        if what == "일시정지":
            c.send("measure_pause")
            c.pump(1.0)
        mark = len(serial_cmds(c))
        nlog0 = len(app_log(c))
        z0 = c.state["stage"]["z"]
        press_stop(c)
        check(wait_run(c, 60), "%s · 측정이 끝남" % what)
        run = c.state["run"]
        check(run["state"] == "stopped" and run["message"] == "정지로 중단", "%s · 중지됨 · 정지로 중단 (%s · %s)" % (what, run["state"], run["message"]))
        check(run["results"].get("1", {}).get("status") == "중지됨", "%s · 샘플 결과 = 중지됨 (%s)" % (what, run["results"].get("1")))
        c.pump(3.0)
        check(not moves_after_mark(c, mark), "%s · 끝난 뒤 이동 줄 0 (%s)" % (what, moves_after_mark(c, mark)[:3]))
        check(not c.state["relays"]["heater_on"], "%s · 히트건 꺼짐" % what)
        f = fw(c)
        if f.get("relay") is not None:
            check(not f["relay"][9], "%s · 펌웨어의 히트건 릴레이 꺼짐" % what)
        check(e2e_rt.lab(c).get("out") is not True, "%s · K2450 출력 꺼짐" % what)
        check(c.state["alarm"] is None and not c.state["stage"]["locked"], "%s · 오류 경보 · 잠금 없음 (%s)" % (what, c.state["alarm"]))
        logs = [x for x in app_log(c)[nlog0:] if "측정 오류" in x or "오류 ·" in x]
        check(not logs, "%s · '측정 오류' 로그 없음 (%s)" % (what, logs[:2]))
        if what != "이동 중":
            check(abs(c.state["stage"]["z"] - z0) < 0.05, "%s · Z 를 다시 움직이지 않음 (Z %.2f → %.2f)" % (what, z0, c.state["stage"]["z"]))
        c.pump(0.6)


setattr(c68, "env", REAL)
setattr(c68, "patch", {"sim": {"time_scale": 2.0}, "rt": {"max_temp": 45.0, "break_temp": 30.0, "repeat": 1, "precool_c": 26.0, "cooler_target": "sample",
                                                            "file_path": "", "date_folder": False, "volt": 1.0, "ilim_a": 0.01}})


@case("70 멈춰 있을 때 정지 20 번 → st · rs 100 번의 응답이 전부 제 순서")
def c70(c):
    c.pump(0.5)
    for _ in range(20):
        press_stop(c)
        time.sleep(0.35)
    c.pump(0.6)
    bad = []
    for k in range(100):                                             # st(연결 확인)와 rs(릴레이 끄기 → RS) 번갈아 100 번
        r1 = c.send("link_check") if k % 2 == 0 else c.send("relay", n=1 + (k // 2) % 9, on=False)
        if not r1[0]:
            bad.append(r1[1])
    check(not bad, "정지 20 번 뒤 st · rs 100 번 · 응답 어긋남 %d (%s)" % (len(bad), bad[:2]))
    check(c.state["alarm"] is None and not c.state["stage"]["locked"], "경보 · 잠금 없음")
    f = fw(c)
    if f.get("log"):
        sts = [x for x in f["log"] if x == "st"]
        rss = [x for x in f["log"] if x.startswith("r ") and x.endswith(" 0")]
        check(len(sts) >= 50 and len(rss) >= 50, "펌웨어가 st %d · r n 0 %d 개를 모두 받음" % (len(sts), len(rss)))


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
            c.send("relay", n=1, on=True)                            # 정책(비상정지 때 끄기)이 적용되는지 본다
            i = c.send_nowait("move_xy", x=190.0, y=100.0)
            c.pump(0.6)
            first, second = ("/stop", "/estop") if order.startswith("정지") else ("/estop", "/stop")
            c.http(first, "POST")
            time.sleep(gap)
            c.http(second, "POST")
            c.wait_ack(i, 30)
            c.pump(1.5)
            st = c.state["stage"]
            check(st["locked"] and not st["z_ok"] and not st["xy_ok"], "%s · 간격 %d ms · 결과 = 비상정지 (잠금 %s · 원점 버림)" % (order, gap * 1000, st["locked"]))
            rl = c.state["relays"]["owners"]
            check(not (rl.get("1") or rl.get(1)), "%s · 간격 %d ms · 릴레이 정책(끄기) 적용" % (order, gap * 1000))
            b = c.state["band"]
            check(b and b["err"] and "비상정지" in b["text"], "%s · 간격 %d ms · 띠 = 비상정지(빨강) (%s)" % (order, gap * 1000, b and b["text"]))
            ok, r = c.send("origin", axis="z")
            c.pump(0.3)
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
    t0 = time.time()
    ok, r = c.send("relay_test", n=10)                                     # 재현(D1): move_xy 중 히트건 [2 s 켜 보기] → X 와 Y 사이에 r 10 1 이 끼었다
    check(not ok and time.time() - t0 < 1.0, "이동 중 히트건 시험 즉시 거절 (%s)" % (r,))
    c.wait_ack(i, 40)
    c.pump(0.8)
    cmds = serial_cmds(c)
    check(not any(x in ("r 1 1", "r 2 1", "r 10 1") for x in cmds), "거절된 릴레이 켜기 줄이 나중에도 나가지 않음")
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


def _recover_all(c):
    c.pump(1.1)
    for ax in "zxy":
        c.send("origin", axis=ax)
    if not c.state["stage"]["xy_ok"]:
        c.send("apply_position")
    c.pump(0.4)


@case("77 원점: fz 실패(시간 초과 · 정지 · 비상정지 · 통신 끊김) 뒤 그 축 원점 없음 · Z 위치를 모를 때 전체 스트로크 + fz 한 번(실제 위치)")
def c77(c):
    # (1) fz 실패 네 가지 → 그 축 원점 없음(펌웨어는 "원점 있음" 이라고 답해도 위치 카운터가 틀리다)
    for how in ("시간 초과", "정지", "비상정지", "통신 끊김"):
        _recover_all(c)
        c.send("move_xy", x=3.0, y=60.0)
        mark = len(serial_cmds(c))
        if how == "시간 초과":
            c.dry("hang", on=True)                                  # 다음 이동(fz 의 미는 구간)이 끝나지 않는다
        i = c.send_nowait("origin", axis="x")
        check(wait_cmd(c, mark, lambda x: x.startswith("fz x")), "X fz · %s · fz 가 나감" % how)
        c.pump(0.4)
        if how == "정지":
            press_stop(c)
        elif how == "비상정지":
            c.http("/estop", "POST")
        elif how == "통신 끊김":
            c.dry("cut", s=30.0) if MODE == "fake" else c.dry("unplug", device="stage")
        ok, r = c.wait_ack(i, 60)
        c.pump(1.0)
        st = c.state["stage"]
        check(not ok, "X fz · %s · 실패로 끝남 (%s)" % (how, r))
        check(st is None or (not st["xy_ok"] and not st["done"]["x"]), "X fz · %s · X 원점 없음 (xy_ok %s · done %s)" % (how, st and st["xy_ok"], st and st["done"]))
        if how == "통신 끊김":
            if MODE == "fake":
                c.dry("cut", s=0.0)
            else:
                c.dry("replug", device="stage")
            c.wait(lambda s: s["stage"] is not None and s["dev"]["stage"]["state"] == "ok", 40, "다시 연결")
            c.send("link_check")
            c.pump(1.0)
        elif how == "시간 초과":
            c.dry("hang", on=False)
    # (1b) 재현(C1): Z 원점 잡기(fz z)가 시간 초과로 끝나면 z_ok 가 내려가고 띠 = 원점 다시 잡기(주황) + [Z 원점 잡기]
    _recover_all(c)
    c.dry("hang", on=True)
    ok, r = c.send("origin", axis="z", timeout=90)
    c.dry("hang", on=False)
    c.pump(1.0)
    st, b = c.state["stage"], c.state["band"]
    check(not ok and not st["z_ok"], "Z fz 시간 초과 → Z 원점 없음 (z_ok %s · %s)" % (st["z_ok"], r))
    check(b and not b["err"] and b["text"] in ("원점 다시 잡기", "원점 없음") and any(a["act"] == "origin" and a["args"].get("axis") == "z" for a in b["actions"]) or (b and "시간 초과" in b["text"]),
          "띠 = 원점 다시 잡기 + Z 버튼 (%s · %s)" % (b and b["text"], b and [a["label"] for a in b["actions"]]))
    _recover_all(c)
    check(c.state["stage"]["z_ok"] and c.state["stage"]["xy_ok"], "회복")
    # (2) 펌웨어 Z 원점 없음 + 실제 Z 30 mm 아래 → 위쪽 전체 스트로크 + fz 한 번 → 0 이 끝단 2 mm 아래(실제 위치로 검사)
    if MODE != "fake":
        mark = len(serial_cmds(c))
        c.http("/estop", "POST")
        c.pump(1.3)
        ok, r = c.send("origin", axis="z")
        cmds = serial_cmds(c)[mark:]
        check(ok and any(x.startswith("jz -") for x in cmds) and cmds.count("fz z 1600") == 1, "(dry) Z 위치 못 믿음 → 위쪽 전체 스트로크 조그 뒤 fz 한 번 (%s)" % [x for x in cmds if x[:2] in ("jz", "fz")])
        return
    c.dry("set_real", axis="z", mm=30.0)
    c.dry("forget_z")
    c.http("/estop", "POST")
    c.pump(1.3)
    mark = len(serial_cmds(c))
    ok, r = c.send("origin", axis="z")
    check(ok, "Z 원점 잡기 (%s)" % (r,))
    cmds = serial_cmds(c)[mark:]
    jz = [x for x in cmds if x.startswith("jz")]
    check(any(x.startswith("jz -") and abs(int(x.split()[1])) >= int((45.0 + 3.0) * 160) - 1 for x in jz), "먼저 위쪽으로 전체 스트로크 + 3 mm 를 민다 (%s)" % jz[:3])
    check(cmds.count("fz z 1600") == 1, "fz 는 한 번 (0 등록은 그 한 번뿐)")
    check(cmds.index("fz z 1600") > max(i for i, x in enumerate(cmds) if x.startswith("jz")), "fz 앞에 전체 스트로크")
    f = fw(c)
    check(abs(f["real"]["z"]) < 0.05, "0 이 끝단 2 mm 아래에 잡힘 (실제 Z %.2f mm · 끝단 -2.00)" % f["real"]["z"])
    vs = [x for x in cmds if x.startswith("v ")]
    check(vs and int(vs[0].split()[1]) <= 1200, "전체 스트로크는 느린 속도 (%s)" % vs[:2])
    # (3) 그 도중 정지 → 0 등록 없음
    c.dry("set_real", axis="z", mm=30.0)
    c.dry("forget_z")
    c.http("/estop", "POST")
    c.pump(1.3)
    mark = len(serial_cmds(c))
    i = c.send_nowait("origin", axis="z")
    c.pump(1.5)
    press_stop(c)
    ok, r = c.wait_ack(i, 30)
    c.pump(1.0)
    f = fw(c)
    cmds = serial_cmds(c)[mark:]
    check(not ok and not f["hom"][2] and not any(x.startswith("fz") for x in cmds), "전체 스트로크 도중 정지 · fz 없음 · 0 등록 없음 (%s)" % (r,))
    check(not c.state["stage"]["z_ok"], "Z 원점 없음 그대로")


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
    mark = len(serial_cmds(c))
    c.pump(5.0)
    mv = [x for x in serial_cmds(c)[mark:] if x.split()[0] in MOVE_CMDS]
    check(not mv, "그 뒤에도 몇 초 뒤 갑자기 움직이지 않음 (%s)" % mv[:3])
    # 거래 잠금이 2 s 잡힌 상태의 조그 → 1 s 안에 첫 줄을 못 써 거절, 그 뒤에도 움직이지 않음
    c.wait(lambda s: s["dev"]["camera"]["state"] == "ok", 20, "카메라")
    mark = len(serial_cmds(c))
    c.dry("hold_txn", s=2.0)
    t0 = time.time()
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "늦은" in str(r), "거래 잠금 2 s 동안의 조그 → 거절 (%s · %.1f s)" % (r, time.time() - t0))
    c.pump(3.0)
    mv = [x for x in serial_cmds(c)[mark:] if x.split()[0] in MOVE_CMDS]
    check(not mv, "그 뒤에도 움직이지 않음 (%s)" % mv[:3])
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(ok, "잠금이 풀린 뒤 조그는 됨 (%s)" % (r,))


setattr(c78, "env", REAL)


@case("79 시리얼: 읽기 예외 → 통신 끊김 · 위치 못 믿음 / 잘린 ST 줄은 받아들이지 않음")
def c79(c):
    # (1) 잘린 ST: 핀이 내려간(Z 20) 상태에서 ST 를 줄 중간에서 나눠 보낸다("… Z=3" | "200 …") → 이어 붙여 읽고 X · Y 이동 전에 Z 를 올린다
    if MODE == "fake":
        for trunc in (False, True):
            c.send("z_top")
            c.send("move_xy", x=100.0, y=100.0)
            ok, r = c.send("contact_try", free=True)
            check(ok and c.state["stage"]["z"] > 19.0, "핀을 접촉 높이로 (%s · Z %.1f)" % (r, c.state["stage"]["z"]))
            c.dry("split_st", n=30, trunc=trunc)
            mark = len(serial_cmds(c))
            ok, r = c.send("move_xy", x=60.0, y=60.0)
            cmds = serial_cmds(c)[mark:]
            iz = next((k for k, x in enumerate(cmds) if x.startswith("mz ") and float(x.split()[1]) == 0.0), None)
            ix = next((k for k, x in enumerate(cmds) if x.startswith("mx")), None)
            what = "잘린 ST 줄(줄바꿈 있음)" if trunc else "줄 중간에서 나뉜 ST"
            check(ok and iz is not None and ix is not None and iz < ix, "%s · Z 를 먼저 올린 뒤 X (%s)" % (what, [x for x in cmds if x[:2] in ("mz", "mx", "my")][:4]))
            f = fw(c)
            check(f["counter"]["z"] == 0 and abs(c.state["stage"]["z"]) < 0.05, "%s · Z 맨 위 (펌웨어 %d · 화면 %.2f)" % (what, f["counter"]["z"], c.state["stage"]["z"]))
            c.dry("split_st", n=0)
            c.pump(0.5)
    # (2) 읽기 예외(USB 뽑힘): 이동 중 뽑힘 → 그 이동은 통신 끊김으로 끝나고 위치를 못 믿는다
    c.send("move_xy", x=40.0, y=100.0)
    i = c.send_nowait("move_xy", x=220.0, y=100.0)
    mark = len(serial_cmds(c))
    wait_cmd(c, mark - 3, lambda x: x.startswith("mx 220"))
    c.pump(0.5)
    c.dry("unplug", device="stage")
    ok, r = c.wait_ack(i, 40)
    check(not ok and "통신 끊김" in str(r), "이동 중 읽기 예외 → 이동이 통신 끊김으로 끝남 (%s)" % (r,))
    c.pump(2.5)
    s = c.state
    check(s["stage"] is None or not s["stage"]["xy_ok"], "위치 못 믿음 (xy_ok %s)" % (s["stage"] and s["stage"]["xy_ok"]))
    check(s["alarm"] is not None and "통신 끊김" in s["alarm"]["text"] or s["dev"]["stage"]["state"] in ("down", "retry"), "통신 끊김 경보 또는 연결 안 됨 (%s · %s)" % (s["alarm"], s["dev"]["stage"]["state"]))
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "뽑힌 뒤 이동 거절 (%s)" % (r,))


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
    for kind in ("정지", "비상정지"):
        # (1) 쓰기 1 s 막힘: 풀리면 '!' 가 가장 먼저 도착해 진행 중인 이동을 끊는다(1.6 s 이내)
        if c.state["stage"]["locked"] or not c.state["stage"]["xy_ok"]:
            c.pump(1.1)
            for ax in "zxy":
                c.send("origin", axis=ax)
        c.send("move_xy", x=40.0, y=100.0)
        i = c.send_nowait("move_xy", x=220.0, y=100.0)
        c.pump(0.5)
        ncut0 = len(fw(c).get("cuts") or [])
        nw0 = len(fw(c).get("stop_wall" if kind == "정지" else "bang_wall", []))
        c.dry("block_write", s=1.0)
        t0 = time.time()
        c.http("/stop" if kind == "정지" else "/estop", "POST")
        w = []
        for _ in range(80):
            w = fw(c).get("stop_wall" if kind == "정지" else "bang_wall", [])
            if len(w) > nw0:
                break
            time.sleep(0.05)
        ms = (w[nw0] - t0) * 1000 if len(w) > nw0 else None
        check(ms is not None and ms <= 1600, "%s · 쓰기 1 s 막힘 · '!' 가 1.6 s 안에 나감 (%s ms)" % (kind, None if ms is None else int(ms)))
        c.wait_ack(i, 30)
        c.pump(0.8)
        f = fw(c)
        cuts = f.get("cuts") or []
        check(len(cuts) > ncut0 and cuts[-1]["out"] < cuts[-1]["cmd"], "%s · 풀린 뒤 '!' 가 가장 먼저 도착해 이동을 끊음 (끊김 %d → %d)" % (kind, ncut0, len(cuts)))
        if len(cuts) > ncut0 and cuts[-1].get("p_at_bang") is not None:
            check(cuts[-1]["out"] - cuts[-1]["p_at_bang"] <= 65, "%s · '!' 뒤 나간 펄스 %.0f ≤ 64" % (kind, cuts[-1]["out"] - cuts[-1]["p_at_bang"]))
        # (2) 쓰기가 계속 실패(3 s): 경보 = 전송 실패 + 재전송, 풀리면 '!' 도착 + 경보 문구 바뀜
        c.pump(1.2)
        if c.state["stage"]["locked"] or not c.state["stage"]["xy_ok"]:
            for ax in "zxy":
                c.send("origin", axis=ax)
        c.send("move_xy", x=40.0, y=100.0)
        i = c.send_nowait("move_xy", x=220.0, y=100.0)
        c.pump(0.5)
        nw0 = len(fw(c).get("stop_wall" if kind == "정지" else "bang_wall", []))
        ncut0 = len(fw(c).get("cuts") or [])
        c.dry("block_write", s=3.0)
        t0 = time.time()
        c.http("/stop" if kind == "정지" else "/estop", "POST")         # 첫 쓰기가 1 s 뒤 시간 초과로 실패하고 돌아온다
        want = "정지 전송 실패 · 비상정지를 누르십시오" if kind == "정지" else "비상정지 전송 실패 · 모터 전원을 끄십시오"
        seen = []
        while time.time() - t0 < 2.2:                                    # 막혀 있는 동안(3 s) 경보를 본다
            c.pump(0.1)
            al = c.state["alarm"]
            seen.append(al and al["text"])
        bad = [x for x in seen[-8:] if x != want]
        check(not bad, "%s · 쓰기 실패 중 경보 = '%s' (%s)" % (kind, want, sorted(set(x for x in seen if x))))
        ok_after = "비상정지 · 이동 잠김" if kind == "비상정지" else None
        check(c.wait(lambda s: (s["alarm"] and s["alarm"]["text"]) == ok_after if ok_after else not (s["alarm"] and "전송" in s["alarm"]["text"]), 15, "경보 바뀜"),
              "%s · 풀리면 경보 문구가 바뀜 (%s)" % (kind, c.state["alarm"] and c.state["alarm"]["text"]))
        f = fw(c)
        check(len(f.get("stop_wall" if kind == "정지" else "bang_wall", [])) > nw0 and len(f.get("cuts") or []) > ncut0, "%s · 재전송한 '!' 가 결국 도착해 이동을 끊음" % kind)
        c.wait_ack(i, 30)
        c.pump(1.2)


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
    bad_inflight, bad_relay, bad_after, bad_locked = [], [], [], []
    inflight = False
    last_bang = -1e9
    last_estop = -1e9
    estop_open = False
    rec = set()                                                 # 비상정지 뒤 회복 동작: z(fz z) · xy(sp 또는 fz x + fz y)
    for t, arrow, text in rows:
        first = text.split()[0] if text.split() else ""
        if arrow == "->":
            if text.startswith("!"):
                last_bang = t
                if "(abort)" in text:
                    estop_open = True
                    last_estop = t
                    rec = set()
                continue
            if inflight and not (text.startswith("r ") and text.endswith(" 0")):
                bad_inflight.append((t, text))
            if first in MOVE_CMDS:
                if t - last_bang < 0.5 or t - last_estop < 1.0:          # 정지 뒤 0.5 s · 비상정지 뒤 1 s
                    bad_after.append((t, text))
                if estop_open and first in ("mx", "my", "mz", "gx", "gy", "gz") and not (first == "mz" and text.split()[1:] == ["0.0"] and "z" in rec):
                    bad_locked.append((t, text))                         # 회복 전 절대 이동(회복 조그 · fz · sp 만 허용)
                inflight = first != "sp"
                if first == "fz":
                    rec.add(text.split()[1] if len(text.split()) > 1 else "")
                if first == "sp":
                    rec.add("sp")
                if "z" in rec and ("sp" in rec or ("x" in rec and "y" in rec)):
                    estop_open = False
            if estop_open and text.startswith("r ") and text.endswith(" 1"):
                bad_relay.append((t, text))
        elif arrow == "<-":
            if inflight and (" | X=" in text or "이미 그 위치" in text or "[거부]" in text or "원점 설정 완료" in text or "중단됨" in text or "PC 위치로" in text):
                inflight = False
    check(not bad_inflight, "이동 줄과 완료 보고 사이에 '!' 아닌 줄 0 (%d: %s)" % (len(bad_inflight), bad_inflight[:3]))
    check(not bad_relay, "비상정지 뒤 릴레이 켜기 줄 0 (%d: %s)" % (len(bad_relay), bad_relay[:3]))
    check(not bad_after, "정지 뒤 0.5 s · 비상정지 뒤 1 s 안의 이동 줄 0 (%d: %s)" % (len(bad_after), bad_after[:3]))
    check(not bad_locked, "비상정지 뒤 회복 전 절대 이동 줄 0 (%d: %s)" % (len(bad_locked), bad_locked[:3]))
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
    f = fw(c)
    check(f.get("last_alive") is False, "실패한 연결의 포트를 닫음 (열린 채 %s)" % (f.get("last_alive"),))


setattr(c75b, "env", {"WAFER2_FAKE_SILENT_ST": "1"})


@case("75c 연결 중 비상정지 → 연결 뒤 잠금으로 시작")
def c75c(c):
    c.dry("open_delay", stage=3.0)
    c.dry("unplug", device="stage")
    c.pump(0.5)
    c.dry("replug", device="stage")
    i = c.send_nowait("reconnect")
    c.pump(1.0)
    check(c.state["stage"] is None, "연결 중(이동 계층이 아직 공개되지 않음)")
    c.http("/estop", "POST")
    c.wait_ack(i, 40)
    check(c.wait(lambda s: s["stage"] is not None, 40, "연결"), "연결이 끝남")
    c.pump(1.0)
    st = c.state["stage"]
    check(st and st["locked"] and not st["z_ok"], "연결이 끝난 뒤 잠금 상태 (%s)" % (st and st["locked"]))
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok, "잠금 · 이동 거절 (%s)" % (r,))
    check(os.path.exists(os.path.join(c.data, "estop.lock")), "estop.lock 이 남음")
