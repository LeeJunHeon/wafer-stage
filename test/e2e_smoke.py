"""e2e_smoke.py - 서버를 실제로 띄워 WebSocket 으로 전 흐름을 확인한다.

  python test/e2e_smoke.py

--dry(시리얼 없이) + --image(촬영 대신 사진) 로 서버를 띄우고
  capture -> 샘플 검출 수 확인
  run(auto, dwell 0.2) -> running -> done, done==총 개수, results.csv 생성
  pause/resume/stop 경로
  needs_confirm 경로(마커 3개 + 글레어 사진)
를 차례로 본다. 하드웨어가 없어도 도는 검증이라 커밋 전에 이걸 돌린다.
"""

import asyncio
import json
import os
import socket
import subprocess
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

import paths                                                        # noqa: E402

GOOD = os.path.join(paths.OUT_DIR, "seq_20260909_150413", "raw.png")
GLARE = os.path.join(paths.OUT_DIR, "seq_20260909_150621", "raw.png")


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_port(port, timeout=30.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        try:
            with socket.create_connection(("127.0.0.1", port), 0.3):
                return True
        except OSError:
            time.sleep(0.2)
    return False


class Client:
    def __init__(self, ws):
        self.ws = ws
        self.state = None
        self.acks = []
        self.logs = []

    async def pump(self, seconds=0.0):
        """들어오는 메시지를 지정 시간 동안(또는 한 번) 처리한다."""
        end = time.monotonic() + seconds
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.05, seconds))
            except asyncio.TimeoutError:
                if time.monotonic() >= end:
                    return
                continue
            m = json.loads(raw)
            if m.get("type") == "state":
                self.state = m
            elif m.get("type") == "ack":
                self.acks.append(m)
            elif m.get("type") == "log":
                self.logs.append(m)
            if time.monotonic() >= end:
                return

    async def send(self, **kw):
        await self.ws.send(json.dumps(kw))

    async def wait_phase(self, phases, timeout=60.0):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            await self.pump(0.3)
            ph = ((self.state or {}).get("sequence") or {}).get("phase")
            if ph in phases:
                return ph
        return ((self.state or {}).get("sequence") or {}).get("phase")


async def run_case(port, image, fn):
    import websockets
    async with websockets.connect("ws://127.0.0.1:%d/ws" % port) as ws:
        c = Client(ws)
        await c.pump(1.0)
        await fn(c)


def start_server(port, image):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    p = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "backend", "server.py"),
         "--dry", "--no-window", "--port", str(port), "--image", image],
        cwd=ROOT, env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    if not wait_port(port):
        out = p.stdout.read(4000).decode("utf-8", "replace") if p.stdout else ""
        p.kill()
        raise SystemExit("서버가 뜨지 않았습니다:\n" + out)
    return p


FAIL = []


def check(cond, msg):
    print(("  OK   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAIL.append(msg)


async def main_flow(c):
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    n = len(c.state["samples"])
    check(ph == "ready", "capture -> phase=ready (%s)" % ph)
    check(n == 16, "샘플 16개 검출 (%d개)" % n)
    check(c.state["calib"] and c.state["calib"]["used_ids"] == [0, 1, 2, 3],
          "마커 4개로 재보정")
    check(bool(c.state["sensing"]), "감지영역 rect 전달")
    out_dir = c.state["sequence"]["out_dir"]

    # 이 사진은 웨이퍼가 감지영역(마커 사각형)에 걸쳐 있어 서버가 확인을 요구한다.
    # 화면이 확인 모달을 띄우고 confirm:true 로 다시 보내는 것과 같은 경로다.
    await c.send(cmd="run", mode="auto", dwell_s=0.2, confirm=True)
    ph = await c.wait_phase(("running",), 20)
    check(ph == "running", "run -> running (%s)" % ph)

    await c.send(cmd="pause")
    ph = await c.wait_phase(("paused",), 20)
    check(ph == "paused", "pause -> paused (%s)" % ph)
    await c.send(cmd="resume")
    ph = await c.wait_phase(("running",), 20)
    check(ph == "running", "resume -> running (%s)" % ph)

    ph = await c.wait_phase(("done", "error", "stopped"), 180)
    check(ph == "done", "순회 완료 phase=done (%s)" % ph)
    check(c.state["sequence"]["done"] == 16,
          "done=16 (%s)" % c.state["sequence"]["done"])
    csv_p = os.path.join(out_dir, "results.csv")
    check(os.path.exists(csv_p), "results.csv 생성 (%s)" % csv_p)
    check(all(s["status"] == "done" for s in c.state["samples"]), "모든 샘플 done")


async def stop_flow(c):
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    await c.send(cmd="run", mode="auto", dwell_s=1.0, confirm=True)
    await c.wait_phase(("running",), 20)
    await asyncio.sleep(1.0)
    await c.send(cmd="stop")
    ph = await c.wait_phase(("stopped", "done", "error"), 60)
    check(ph == "stopped", "stop -> stopped (%s)" % ph)


async def confirm_flow(c):
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    check(ph == "ready", "글레어 사진도 검출은 된다 (%s)" % ph)
    c.acks.clear()
    await c.send(cmd="run", mode="auto", dwell_s=0.1)
    await c.pump(3.0)
    acks = [a for a in c.acks if a.get("of") == "run"]
    ok = acks and acks[0]["ok"] is False and acks[0]["reason"] == "needs_confirm"
    check(bool(ok), "needs_confirm ack (%s)" % (acks[0] if acks else "없음"))
    if ok:
        print("       사유: " + " / ".join(acks[0]["needs_confirm"]))
    await c.send(cmd="run", mode="auto", dwell_s=0.1, confirm=True)
    ph = await c.wait_phase(("running", "done"), 30)
    check(ph in ("running", "done"), "confirm:true 로 시작됨 (%s)" % ph)
    await c.send(cmd="stop")
    await c.wait_phase(("stopped", "done", "error"), 60)


def main():
    for f in (GOOD, GLARE):
        if not os.path.exists(f):
            raise SystemExit("검증용 사진이 없습니다: %s" % f)
    for name, image, flow in (("정상 흐름", GOOD, main_flow),
                              ("정지 경로", GOOD, stop_flow),
                              ("확인 필요 경로", GLARE, confirm_flow)):
        port = free_port()
        print("[%s] 서버 :%d  %s" % (name, port, os.path.basename(os.path.dirname(image))),
              flush=True)
        p = start_server(port, image)
        try:
            asyncio.run(run_case(port, image, flow))
        finally:
            p.kill()
            p.wait(timeout=10)
        time.sleep(0.5)
    print("")
    if FAIL:
        print("실패 %d건:" % len(FAIL))
        for m in FAIL:
            print("  - " + m)
        return 1
    print("전부 통과")
    return 0


if __name__ == "__main__":
    sys.exit(main())
