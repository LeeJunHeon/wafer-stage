"""e2e_safety.py - 모터 안전 e2e 시나리오 56~ (tools/e2e.py 가 불러온다). dry · fake-serial 두 모드.

  56 이동 중 조그는 줄 서지 않고 즉시 거절(이동 명령 · 샘플 위로)         57 비상정지 직후 1 s 이동 거절 · 회복 조그 한 번 5 mm
  58 Z 조그: 스텝 한도 · Z↓ 연속 없음 · 접촉 높이 + 여유 아래 거절 · 접촉 높이 화면(free) 예외
  59 핀이 내려가 있으면 X · Y 조그 거절                                      60 연속 조그는 스텝 1 mm 이하만
  61 X · Y 이동 명령 앞에는 늘 Z 맨 위(시리얼 순서)                         62 비상정지: WS · HTTP 동시에 와도 한 번만 처리
  63 비상정지 지연 - 상태별(서버: '!' 가 시리얼에 쓰이기까지)               64 쓰기 막힘(USB 멈춤 흉내)에서도 '!' 는 1.6 s 안
화면 쪽(누르는 순간 전송 · 키보드 조그 범위 · 키 · 버튼 누른 채 비상정지 · 서버 끊김)은 tools/safety_ui.py(playwright)가 맡는다.
"지연 <상태> <ms>" 줄은 보고용 표로 그대로 쓴다.
"""
import json
import os
import time

import e2e
from e2e import case, check, full_setup
from e2e_cases import serial_cmds

SLOW_MOVE = {"WAFER_STAGE_DRY_MOVE_S": "1.2", "WAFER2_FAKE_SCALE": "0.3"}      # dry 는 이동마다 1.2 s, fake 는 느린 시간 배율
JOGCFG = {"jog": {"z_max_mm": 1.0}}                                              # 기본값(예전 시험 기본 패치는 50)
FAST_MS = 100.0
LAT = {}


def bang_wall(c):
    ok, r = c.dry("bang")
    return (r or {}).get("wall") or [] if ok else []


def latency(c, name, via="http", limit=FAST_MS, pre=None):
    """요청을 보낸 순간 → '!' 를 시리얼에 쓴 순간(ms). 같은 기계의 벽시계로 잰다."""
    n0 = len(bang_wall(c))
    t0 = time.time()
    if via == "ws":
        c.send_nowait("estop")
    else:
        c.http("/estop", "POST")
    ms = None
    for _ in range(40):
        w = bang_wall(c)
        if len(w) > n0:
            ms = (w[n0] - t0) * 1000.0
            break
        time.sleep(0.05)
    LAT[name] = ms
    print("지연 %s %s ms" % (name, "—" if ms is None else "%.0f" % ms), flush=True)
    check(ms is not None and ms <= limit, "비상정지 지연 · %s · %s ms (한도 %.0f)" % (name, "—" if ms is None else "%.0f" % ms, limit))
    return ms


def serial_n(c, prefix):
    return len([x for x in serial_cmds(c) if x.startswith(prefix)])


@case("56 이동 중 조그는 줄 서지 않고 즉시 거절 (이동 명령 · 샘플 위로)")
def c56(c):
    t = full_setup(c)
    c.send("capture")
    n_jog0 = serial_n(c, "jx") + serial_n(c, "jy") + serial_n(c, "jz")
    i = c.send_nowait("goto_sample", no=c.state["samples"][2]["no"])
    c.pump(0.15)
    t0 = time.time()
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "이동 중" in str(r), "샘플 이동 중 조그 즉시 거절 (%s)" % (r,))
    check(time.time() - t0 < 0.6, "거절은 즉시 (%.2f s)" % (time.time() - t0))
    ok, r = c.wait_ack(i, 60)
    check(ok, "샘플 이동은 끝까지 감 (%s)" % (r,))
    c.pump(0.8)
    n_jog1 = serial_n(c, "jx") + serial_n(c, "jy") + serial_n(c, "jz")
    check(n_jog1 == n_jog0 + 0, "이동이 끝난 뒤에도 거절된 조그가 늦게 실행되지 않음 (조그 명령 %d → %d)" % (n_jog0, n_jog1))
    # 같은 규칙: 좌표 이동 중 좌표 이동 · Z 맨 위 · 대기 위치
    i = c.send_nowait("move_xy", x=200.0, y=200.0)
    c.pump(0.15)
    ids = [(cmd, c.send_nowait(cmd, **args)) for cmd, args in (("move_xy", dict(x=50.0, y=50.0)), ("z_top", {}), ("goto_named", dict(name="park")), ("jog", dict(axis="z", mm=-1.0)), ("origin", dict(axis="x")))]
    for cmd, j in ids:                                      # 한꺼번에 보내 이동이 끝나기 전에 모두 받게 한다
        ok, r = c.wait_ack(j, 30)
        check(not ok, "이동 중 %s 거절 (%s)" % (cmd, r))
    ok, r = c.wait_ack(i, 60)
    check(ok, "좌표 이동은 끝까지 감")
    c.pump(0.8)
    st = c.state["stage"]
    check(abs(st["x"] - 200) < 0.5 and abs(st["y"] - 200) < 0.5, "거절된 명령이 이동을 바꾸지 않음 (%.1f, %.1f)" % (st["x"], st["y"]))


setattr(c56, "env", SLOW_MOVE)


@case("57 비상정지 직후 1 s 이동 거절 · 회복 조그는 한 번 5 mm 이하")
def c57(c):
    c.pump(0.5)
    c.http("/estop", "POST")
    ids = [("조그", c.send_nowait("jog", axis="z", mm=-0.5)), ("좌표 이동", c.send_nowait("move_xy", x=50.0, y=50.0)), ("원점 잡기", c.send_nowait("origin", axis="z"))]
    for name, i in ids:
        ok, r = c.wait_ack(i, 30)
        check(not ok and "비상정지 직후" in str(r), "비상정지 직후 %s 거절 (%s)" % (name, r))
    c.pump(1.0)
    ok, r = c.send("jog", axis="x", mm=6.0)
    check(not ok, "잠금 · 원점 없음 중 X 조그 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=-0.5)
    check(ok, "1 s 뒤 Z 위쪽 조그는 받음 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=-6.0)
    check(not ok, "회복 중 한 번 5 mm 넘는 조그 거절 (%s)" % (r,))
    ok, r = c.send("move_xy", x=50.0, y=50.0)
    check(not ok and ("잠금" in str(r) or "원점" in str(r)), "회복 전에는 좌표 이동 거절 (%s)" % (r,))


@case("58 Z 조그: 스텝 한도 · Z↓ 연속 없음 · 접촉 높이 + 여유 아래 거절 · 접촉 높이 화면 예외")
def c58(c):
    ok, r = c.send("settings_save", patch={"contact_z_mm": 3.0, "jog.z_max_mm": 1.0})
    check(ok, "접촉 높이 3 mm · Z 한도 1 mm (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=2.0)
    check(not ok and "최대" in str(r), "Z 한 번 2 mm 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=1.0, rep=True)
    check(not ok and "한 스텝" in str(r), "Z↓ 연속(rep) 거절 (%s)" % (r,))
    for k in range(3):
        ok, r = c.send("jog", axis="z", mm=1.0)
        check(ok, "Z↓ 1 mm (%d) (%s)" % (k + 1, r))
    c.pump(0.5)
    check(abs(c.state["stage"]["z"] - 3.0) < 0.06, "Z = 접촉 높이 (%.2f)" % c.state["stage"]["z"])
    ok, r = c.send("jog", axis="z", mm=0.5)
    check(not ok and "접촉 높이" in str(r), "접촉 높이 + 0.3 보다 아래(0.5) 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=0.3)
    check(ok, "접촉 높이 + 0.3 까지는 받음 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=0.05)
    check(not ok, "그 아래 0.05 더 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=0.5, free=True)
    check(ok, "접촉 높이 화면(free)은 한도 없음 (%s)" % (r,))
    ok, r = c.send("jog", axis="z", mm=-1.0, rep=True)
    check(ok, "Z↑ 연속은 됨 (%s)" % (r,))
    c.send("jog", axis="z", mm=-1.0)
    c.send("jog", axis="z", mm=-1.0)
    c.pump(0.3)
    c.send("jog", axis="z", mm=-c.state["stage"]["z"])
    c.pump(0.5)
    ok, r = c.send("settings_save", patch={"jog.z_steps": [0.05, 0.1, 0.5, 2]})
    check(not ok, "Z 스텝 목록이 Z 한도를 넘으면 저장 거절 (%s)" % (r,))
    ok, r = c.send("contact_clear")
    ok, r = c.send("jog", axis="z", mm=1.0)
    check(ok, "접촉 높이가 없으면 아래 한도 없음(1 mm 한도만) (%s)" % (r,))


@case("59 핀이 내려가 있으면 X · Y 조그 거절")
def c59(c):
    ok, r = c.send("settings_save", patch={"contact_z_mm": 3.0})
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(ok, "Z 맨 위에서는 X 조그 됨 (%s)" % (r,))
    for _ in range(3):
        c.send("jog", axis="z", mm=1.0)
    c.pump(0.4)
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(not ok and "핀이 내려가" in str(r), "Z = 접촉 높이: X 조그 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="y", mm=1.0)
    check(not ok and "핀이 내려가" in str(r), "Z = 접촉 높이: Y 조그 거절 (%s)" % (r,))
    c.send("jog", axis="z", mm=-1.0)
    c.pump(0.4)
    ok, r = c.send("jog", axis="x", mm=1.0)
    check(ok, "Z 2 mm (접촉 3 − 0.5 보다 얕음)에서는 X 조그 됨 (%s)" % (r,))


@case("60 연속 조그는 스텝 1 mm 이하만")
def c60(c):
    ok, r = c.send("jog", axis="x", mm=10.0, rep=True)
    check(not ok and "연속" in str(r), "10 mm 연속(rep) 거절 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=10.0)
    check(ok, "10 mm 한 번은 됨 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=-1.0, rep=True)
    check(ok, "1 mm 연속은 됨 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=-1.5, rep=True)
    check(not ok, "1.5 mm 연속 거절 (%s)" % (r,))
    ok, r = c.send("settings_save", patch={"jog.repeat_max_mm": 2.0})
    ok, r = c.send("jog", axis="x", mm=-1.5, rep=True)
    check(ok, "연속 한도를 2 mm 로 올리면 됨 (%s)" % (r,))


@case("61 X · Y 이동 명령 앞에는 늘 Z 맨 위 (시리얼 순서)")
def c61(c):
    def seq_after(mark):
        cmds = serial_cmds(c)
        return cmds[mark:]

    def lowered():
        c.send("jog", axis="z", mm=1.0)
        c.send("jog", axis="z", mm=1.0)
        c.pump(0.4)
        return c.state["stage"]["z"]

    def z_before_xy(tail, what):
        iz = next((i for i, x in enumerate(tail) if x.split()[0] in ("mz", "jz", "gz")), None)
        ixy = next((i for i, x in enumerate(tail) if x.split()[0] in ("mx", "my", "gx", "gy", "g", "jx", "jy")), None)
        check(ixy is not None and iz is not None and iz < ixy, "%s: Z 올림 → X · Y 이동 순서 (Z %s · X Y %s) %s" % (what, iz, ixy, tail[:5]))
    for what, cmd, args in (("좌표 이동", "move_xy", dict(x=80.0, y=70.0)), ("대기 위치", "goto_named", dict(name="park")), ("촬영 위치", "goto_named", dict(name="capture"))):
        z = lowered()
        check(z > 1.5, "[%s] Z 를 내려 둠 (%.1f)" % (what, z))
        mark = len(serial_cmds(c))
        ok, r = c.send(cmd, **args)
        check(ok, "[%s] 이동 (%s)" % (what, r))
        z_before_xy(seq_after(mark), what)
        c.pump(0.3)
        check(c.state["stage"]["z"] < 0.06, "[%s] 끝난 뒤 Z 맨 위 (%.2f)" % (what, c.state["stage"]["z"]))
    ok, r = c.send("jog", axis="z", mm=1.0)
    ok, r = c.send("jog", axis="z", mm=1.0)
    mark = len(serial_cmds(c))
    ok, r = c.send("origin", axis="x")
    z_before_xy(seq_after(mark), "X 원점 잡기")


@case("62 비상정지: WS · HTTP 가 동시에 와도 한 번만 처리")
def c62(c):
    n0 = serial_n(c, "! (abort)")
    c.send_nowait("estop")
    c.http("/estop", "POST")
    c.pump(0.6)
    n1 = serial_n(c, "! (abort)")
    check(n1 - n0 == 1, "'!' 한 번 (%d → %d)" % (n0, n1))
    c.pump(0.5)
    st = c.state
    check(st["stage"]["locked"] and st["alarm"] and "비상정지" in st["alarm"]["text"], "잠금 · 경보 (%s)" % (st["alarm"],))
    time.sleep(0.4)
    c.send_nowait("estop")
    c.pump(0.6)
    check(serial_n(c, "! (abort)") - n1 == 1, "0.3 s 뒤 요청은 다시 처리(별도 비상정지)")


@case("63 비상정지 지연 · 상태별 (요청 → '!' 가 시리얼에 쓰이기까지, 한도 100 ms)")
def c63(c):
    c.pump(0.5)
    latency(c, "쉼", "http")
    c.pump(1.2)
    # 회복해서 다음 상태들을 만든다
    def rec():
        c.pump(1.3)
        for ax in ("z", "x", "y"):
            ok, r = c.send("origin", axis=ax)
            if not ok:
                print("  (회복 %s 실패: %s)" % (ax, r))
        c.pump(0.4)
        st = c.state["stage"]
        check(st["z_ok"] and st["xy_ok"] and not st["locked"], "회복 끝 (%s)" % {k: st[k] for k in ("z_ok", "xy_ok", "locked")})
    rec()

    c.send_nowait("move_xy", x=200.0, y=200.0)
    c.pump(0.3)
    latency(c, "X·Y 이동 중", "ws")
    rec()
    c.dry("z", mm=20.0)
    c.pump(0.3)
    c.send_nowait("z_top")
    c.pump(0.3)
    latency(c, "Z 이동 중", "http")
    rec()
    c.send("settings_save", patch={"contact_z_mm": 30.0})
    c.send_nowait("contact_try", free=True)
    c.pump(0.4)
    latency(c, "접촉 높이로 내리기", "ws")
    rec()
    c.send_nowait("jog", axis="x", mm=20.0)
    latency(c, "조그 한 스텝 진행 중", "http")
    rec()
    c.send_nowait("origin", axis="z")
    c.pump(0.3)
    latency(c, "원점 잡기", "http")
    rec()
    c.dry("cpu", s=3.0, n=6)
    time.sleep(0.2)
    latency(c, "CPU 부하", "http")
    c.pump(3.0)
    check(True, "지연표 끝")


setattr(c63, "env", SLOW_MOVE)


@case("64 쓰기 막힘(USB 멈춤 흉내)에서도 '!' 는 1.6 s 안에 나가고 화면 state 갱신은 계속")
def c64(c):
    if e2e.MODE != "fake":
        print("  (fake 모드 전용 - 건너뜀)")
        return
    c.pump(0.5)
    c.dry("block_write", s=1.0)
    v0 = c.state.get("v")
    n0 = len(bang_wall(c))
    t0 = time.time()
    c.send_nowait("estop")
    got = None
    n_state = 0
    while time.time() - t0 < 3.0:
        before = c.state.get("v")
        c.pump(0.1)
        if c.state.get("v") != before:
            n_state += 1
        w = bang_wall(c)
        if len(w) > n0 and got is None:
            got = (w[n0] - t0) * 1000.0
    print("지연 쓰기 막힘 1 s %s ms" % ("—" if got is None else "%.0f" % got), flush=True)
    LAT["쓰기 막힘 1 s"] = got
    check(got is not None and got <= 1600, "'!' 는 1.6 s 안에 나감 (%s ms)" % ("—" if got is None else "%.0f" % got))
    check(n_state >= 1 or c.state.get("v") != v0, "그동안 화면 state 갱신 계속 (%d 번)" % n_state)


for _fn in (c56, c57, c58, c59, c60, c61, c62, c63, c64):
    setattr(_fn, "patch", JOGCFG)
for _fn in (c61, c63):
    setattr(_fn, "env", SLOW_MOVE)
