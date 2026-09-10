"""e2e_smoke.py - 서버를 실제로 띄워 WebSocket 으로 전 흐름을 확인한다.

  python tools/e2e_smoke.py

--dry(시리얼 없이) + --image(촬영 대신 사진) 로 서버를 띄우고
  capture -> 샘플 검출 수 확인
  run(auto, dwell 0.2) -> running -> done, done==총 개수, results.csv 생성
  pause/resume/stop 경로
  needs_confirm 경로(마커 3개 + 반사광 사진)
를 차례로 본다. 하드웨어가 없어도 도는 검증이라 커밋 전에 이걸 돌린다.

실제 data 폴더에는 아무것도 쓰지 않는다. 임시 폴더를 만들어 검증용 사진 두 장만
복사해 넣고, WAFER_STAGE_DATA 로 서버에 넘긴다(끝나면 지운다).
"""

import asyncio
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from core import paths                                                        # noqa: E402

# 원본(검증용 사진)은 실제 데이터 폴더에서 읽기만 한다.
SRC = {"seq_20260909_150413": os.path.join(paths.OUT_DIR, "seq_20260909_150413", "raw.png"),
       "seq_20260909_150621": os.path.join(paths.OUT_DIR, "seq_20260909_150621", "raw.png")}
DATA_DIR = None                     # 임시 데이터 폴더 (setup_data 가 채운다)


_SETTINGS_BAK = None


def _backup_settings():
    global _SETTINGS_BAK
    with open(paths.SETTINGS_PATH, "rb") as f:
        _SETTINGS_BAK = f.read()


def _restore_settings():
    """검증이 바꾼 settings.json 을 원래대로 되돌린다(저장소를 더럽히지 않는다)."""
    if _SETTINGS_BAK is None:
        return
    with open(paths.SETTINGS_PATH, "wb") as f:
        f.write(_SETTINGS_BAK)


def setup_data():
    """임시 데이터 폴더를 만들고 검증용 사진 두 장을 복사한다.

    서버는 여기에만 쓴다(seq_* 폴더·calib_matrix.json·로그). 실제 data 폴더를
    검증 찌꺼기로 채우지 않기 위한 것이다.
    """
    global DATA_DIR
    DATA_DIR = tempfile.mkdtemp(prefix="wafer_smoke_")
    # settings.json 은 저장소 안에 있어 WAFER_STAGE_DATA 로 격리되지 않는다.
    # settings_save·park_here 를 거치는 검증이 원본을 고치므로 백업해 둔다.
    _backup_settings()
    out = os.path.join(DATA_DIR, "out")
    for name, src in SRC.items():
        d = os.path.join(out, name)
        os.makedirs(d, exist_ok=True)
        shutil.copy2(src, os.path.join(d, "raw.png"))
    return {name: os.path.join(out, name, "raw.png") for name in SRC}


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
        self.port = None
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
        c.port = port
        await c.pump(1.0)
        await fn(c)


def http_get(port, path):
    """(status, bytes). 연결 자체가 안 되면 (0, b"")."""
    try:
        with urllib.request.urlopen("http://127.0.0.1:%d%s" % (port, path), timeout=5) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, b""
    except Exception:                      # noqa: BLE001
        return 0, b""


def start_server(port, image):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env[paths.ENV_DATA_DIR] = DATA_DIR     # 실제 data 폴더 대신 임시 폴더에 쓴다
    p = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "run.py"),
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
    # 미리보기: --image 모드에서는 그 사진이 미리보기로 나온다.
    await c.send(cmd="list_ports")
    await c.pump(2.0)
    acks = [a for a in c.acks if a.get("of") == "list_ports"]
    check(bool(acks) and isinstance(acks[0].get("ports"), list),
          "list_ports ack (%s)" % (acks[0].get("ports") if acks else "없음"))
    check(bool(c.state["camera"].get("preview")), "기동과 함께 미리보기 on")
    st, body = http_get(c.port, "/preview.jpg")
    check(st == 200 and body[:2] == b"\xff\xd8",
          "GET /preview.jpg 200 + JPEG (status %s, %d bytes)" % (st, len(body)))
    await c.send(cmd="preview_stop")
    await c.pump(1.5)
    st2, _ = http_get(c.port, "/preview.jpg")
    check(st2 == 204, "preview_stop 뒤 204 (status %s)" % st2)
    check(not c.state["camera"].get("preview"), "preview_stop 뒤 state.camera.preview=false")
    await c.send(cmd="preview_start")
    await c.pump(1.5)

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


async def jog_flow(c):
    """수동 이동: 목표는 서버가 만들고 가동범위로 자른다."""
    await c.send(cmd="stage_home", axis="xy")
    await c.pump(3.0)
    check(c.state["stage"]["x_mm"] == 0.0, "원점 직후 x=0 (%s)" % c.state["stage"]["x_mm"])

    c.acks.clear()
    await c.send(cmd="jog", axis="x", delta_mm=10)
    await c.pump(3.0)
    check(c.state["stage"]["x_mm"] == 10.0, "jog x +10 -> 10.0 (%s)" % c.state["stage"]["x_mm"])
    acks = [a for a in c.acks if a.get("of") == "jog"]
    check(bool(acks) and acks[-1]["ok"], "jog ack ok (%s)" % (acks[-1] if acks else "없음"))

    await c.send(cmd="jog", axis="y", delta_mm=-5)
    await c.pump(3.0)
    check(c.state["stage"]["y_mm"] == 0.0,
          "jog y -5 -> 하한 0.0 (%s)" % c.state["stage"]["y_mm"])

    await c.send(cmd="jog", axis="x", delta_mm=300)
    await c.pump(4.0)
    check(c.state["stage"]["x_mm"] == 247.0,
          "jog x +300 -> 상한 247.0 (%s)" % c.state["stage"]["x_mm"])

    # 현재 위치를 파킹으로
    await c.send(cmd="park_here")
    await c.pump(2.0)
    got = c.state["settings"]["park_xy"]
    check(got == [247.0, 0.0], "park_here -> park_xy %s" % got)

    # 순회 중에는 거절한다
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    await c.send(cmd="run", mode="auto", dwell_s=1.0, confirm=True)
    await c.wait_phase(("running",), 20)
    c.logs.clear()
    c.acks.clear()
    await c.send(cmd="jog", axis="x", delta_mm=1)
    await c.pump(2.0)
    rej = [a for a in c.acks if a.get("of") == "jog" and a["ok"] is False]
    blocked = [l for l in c.logs if "순회 중" in l["msg"]]
    check(bool(rej) and bool(blocked),
          "순회 중 jog 거절 (ack %s · 로그 %d건)" % (rej[0]["reason"] if rej else "없음",
                                              len(blocked)))
    await c.send(cmd="stop")
    await c.wait_phase(("stopped", "done", "error"), 60)


async def stop_flow(c):
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    await c.send(cmd="run", mode="auto", dwell_s=1.0, confirm=True)
    await c.wait_phase(("running",), 20)
    await asyncio.sleep(1.0)
    await c.send(cmd="stop")
    ph = await c.wait_phase(("stopped", "done", "error"), 60)
    check(ph == "stopped", "stop -> stopped (%s)" % ph)

    # 연결을 끊으면 위치는 모르는 값이 된다 - 마지막 숫자를 남기지 않는다.
    await c.send(cmd="stage_disconnect")
    await c.pump(2.0)
    st = c.state["stage"]
    check(st["connected"] is False, "stage_disconnect -> connected=False")
    check(all(st[k] is None for k in ("x_mm", "y_mm", "u", "v")),
          "끊긴 뒤 x_mm/y_mm/u/v 가 None (%s)"
          % {k: st[k] for k in ("x_mm", "y_mm", "u", "v")})


async def estop_flow(c):
    """순회 중 비상정지 → 파킹·save 없이 멈추고, 원점을 다시 잡기 전까지 잠긴다."""
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    await c.send(cmd="run", mode="auto", dwell_s=1.0, confirm=True)
    await c.wait_phase(("running",), 20)
    await asyncio.sleep(0.5)
    c.logs.clear()
    await c.send(cmd="estop")
    ph = await c.wait_phase(("stopped",), 30)
    check(ph == "stopped", "estop -> stopped (%s)" % ph)
    await c.pump(2.0)
    txt = " ".join(l["msg"] for l in c.logs)
    check("파킹" not in txt, "비상정지 뒤 파킹하지 않음")
    check("저장됨" not in txt and "save" not in txt.lower(), "비상정지 뒤 save 하지 않음")
    check(bool(c.state["stage"]["needs_home"]), "needs_home=True 로 이동 잠금")

    c.logs.clear()
    await c.send(cmd="park")
    await c.send(cmd="goto", no=1)
    await c.send(cmd="run", mode="auto", dwell_s=0.1, confirm=True)
    await c.pump(2.5)
    blocked = [l for l in c.logs if "원점 설정 후 사용" in l["msg"]]
    check(len(blocked) >= 3, "park/goto/run 이 모두 잠김 (%d건)" % len(blocked))

    await c.send(cmd="stage_home", axis="xy")
    await c.pump(3.0)
    check(not c.state["stage"]["needs_home"], "원점잡기 후 잠금 해제")
    q = c.state["sequence"]
    check(q["phase"] == "ready", "원점잡기 후 phase=ready (%s)" % q["phase"])
    check(q["estopped"] is False, "원점잡기 후 estopped=False")
    check(c.state["stage"]["last_error"] == "",
          "원점잡기 후 last_error 비움 (%r)" % c.state["stage"]["last_error"])
    c.logs.clear()
    await c.send(cmd="park")
    await c.pump(2.0)
    ok = any("이동 완료" in l["msg"] for l in c.logs)
    check(ok, "원점잡기 후 이동이 다시 된다")


async def confirm_flow(c):
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    check(ph == "ready", "반사광 사진도 검출은 된다 (%s)" % ph)
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
    for f in SRC.values():
        if not os.path.exists(f):
            raise SystemExit("검증용 사진이 없습니다: %s" % f)
    img = setup_data()
    good, glare = img["seq_20260909_150413"], img["seq_20260909_150621"]
    try:
        for name, image, flow in (("정상 흐름", good, main_flow),
                                  ("수동 이동", good, jog_flow),
                                  ("정지 경로", good, stop_flow),
                                  ("비상정지 경로", good, estop_flow),
                                  ("확인 필요 경로", glare, confirm_flow)):
            port = free_port()
            print("[%s] 서버 :%d  %s"
                  % (name, port, os.path.basename(os.path.dirname(image))), flush=True)
            p = start_server(port, image)
            try:
                asyncio.run(run_case(port, image, flow))
            finally:
                p.kill()
                p.wait(timeout=10)
            time.sleep(0.5)
    finally:
        _restore_settings()
        shutil.rmtree(DATA_DIR, ignore_errors=True)
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
