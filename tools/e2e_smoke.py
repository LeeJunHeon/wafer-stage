"""e2e_smoke.py - 서버를 실제로 띄워 WebSocket 으로 전 흐름을 확인한다.

설정은 케이스마다 임시 파일(WAFER_STAGE_SETTINGS)로 준다 - 저장소의 settings.json
(사용자 설정)을 바탕으로 케이스별 패치를 얹는다. 순회는 빠른 기본 레시피(대기 0.2 s)로 돈다.

  python tools/e2e_smoke.py

--dry(시리얼 없이) + --image(촬영 대신 사진) 로 서버를 띄우고
  capture -> 샘플 검출 수 확인
  run(auto, dwell 0.2) -> running -> done, done==총 개수, results.csv 생성
  pause/resume/stop 경로
  needs_confirm 경로(마커 3개 + 반사광 사진)
  샘플 편집(클릭·현재 위치 추가 · 삭제 · 되돌리기 · 새 촬영 확인 · 순회 중 거절)
를 차례로 본다. 하드웨어가 없어도 도는 검증이라 커밋 전에 이걸 돌린다.

실제 data 폴더에는 아무것도 쓰지 않는다. 임시 폴더를 만들어 검증용 사진 두 장만
복사해 넣고, WAFER_STAGE_DATA 로 서버에 넘긴다(끝나면 지운다).
"""

import asyncio
import contextlib
import csv
import json
import math
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
BASE_SETTINGS = {}                  # 케이스 설정의 바탕 = 빈 dict(서버가 스키마 기본값을 채운다)

# 모든 케이스에 얹는 패치: 기본 레시피의 대기를 0.2 s 로(순회가 빨리 끝나게).
FAST_RECIPE = {"name": "기본", "steps": [
    {"type": "move_sample"}, {"type": "z", "target": "work"}, {"type": "wait", "s": 0.2},
    {"type": "measure"}, {"type": "z", "target": "top"}]}
DEFAULT_PATCH = {"recipe": {"items": [FAST_RECIPE], "active": "기본"},
                 "tool": {"fit_margin_mm": 50.0},       # 공구 끝 맞음 검사는 전용 케이스만 본다(기존 흐름은 확인 없이 시작)
                 "calib": {"mode": "markers"}}      # 기존 케이스는 판 마커 방식 - 고정 보정은 케이스가 덮는다
SMALL = {"width": 1280, "height": 720}              # 합성 카메라 헤드 마커 케이스의 해상도(헤드 보정 1280x720)      # 판 마커 방식 케이스(기존) - 고정 보정은 케이스가 덮는다


def deep_merge(base, over):
    out = json.loads(json.dumps(base))
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = deep_merge(out[k], v)
        else:
            out[k] = json.loads(json.dumps(v))
    return out


def relays_patch(enabled=(1, 2, 3), names=None, max_on=None):
    names = names or {}
    max_on = max_on or {}
    return [{"enabled": (i + 1) in enabled, "name": names.get(i + 1, "R%d" % (i + 1)),
             "max_on_s": max_on.get(i + 1, 0)} for i in range(10)]


def setup_data():
    """임시 데이터 폴더를 만들고 검증용 사진 두 장을 복사한다.

    서버는 여기에만 쓴다(seq_* 폴더·calib_matrix.json·로그). 실제 data 폴더를
    검증 찌꺼기로 채우지 않기 위한 것이다.
    """
    global DATA_DIR
    DATA_DIR = tempfile.mkdtemp(prefix="wafer_smoke_")
    # 설정은 작업 폴더의 settings.json 을 읽지 않는다 - 케이스마다 임시 파일(WAFER_STAGE_SETTINGS)을
    # 스키마 기본값(빈 파일) 위에 케이스 패치만 얹어 만든다.
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
        self.closed = False            # 서버가 스스로 끝났다(종료 명령)
        self.cap_texts = []            # 촬영 중 받은 진행 문구(순서대로, 연속 중복 제외)
        self.cap_errors = []           # 받은 촬영 실패 한 줄

    async def pump(self, seconds=0.0):
        """들어오는 메시지를 지정 시간 동안(또는 한 번) 처리한다.

        종료 명령을 보내면 서버가 스스로 끝나므로 소켓이 닫힌다. 그것은 오류가
        아니라 기대한 결과다 - closed 로 표시하고 조용히 돌아온다.
        """
        if self.closed:
            return
        end = time.monotonic() + seconds
        while True:
            try:
                raw = await asyncio.wait_for(self.ws.recv(), timeout=max(0.05, seconds))
            except asyncio.TimeoutError:
                if time.monotonic() >= end:
                    return
                continue
            except Exception:              # noqa: BLE001 - 연결 종료
                self.closed = True
                return
            m = json.loads(raw)
            if m.get("type") == "state":
                self.state = m
                cam = m.get("camera") or {}
                t = cam.get("cap_text")
                if cam.get("capturing") and t and (not self.cap_texts or self.cap_texts[-1] != t):
                    self.cap_texts.append(t)
                if cam.get("cap_error") and cam["cap_error"] not in self.cap_errors:
                    self.cap_errors.append(cam["cap_error"])
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


async def wait_log(c, needle, timeout):
    """로그에 그 말이 나올 때까지 기다린다. 걸린 시간(초) 또는 None."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await c.pump(0.1)
        if any(needle in l["msg"] for l in c.logs):
            return time.monotonic() - t0
    return None


async def estop_delay_home_flow(c):
    """긴 이동(끝단 이동) 중에 도착한 비상정지가 기다리지 않고 바로 나가는가.

    실장 사고: 원점 탐색(69초) 중에 누른 비상정지 3번이 탐색이 끝난 뒤에야
    펌웨어로 나갔다. 서버가 WebSocket 을 receive -> await handle_command 로
    한 줄에 돌려, 뒤이어 온 메시지가 소켓에서 기다렸기 때문이다.
    """
    c.logs.clear()
    await c.send(cmd="touch_end", axis="x", mm=10)
    await asyncio.sleep(0.3)               # 이동이 확실히 시작된 뒤
    c.logs.clear()
    await c.send(cmd="estop")
    dt = await wait_log(c, "비상정지", 3.0)
    check(dt is not None and dt < 0.5,
          "끝단 이동 중 비상정지가 0.5초 안에 처리 (%s초)"
          % ("안 옴" if dt is None else round(dt, 2)))
    dt2 = await wait_log(c, "끝단 이동 중단", 10.0)
    check(dt2 is not None, "끝단 이동이 '중단' 으로 끝남")
    await c.pump(1.0)
    check(bool(c.state["stage"]["needs_home"]), "중단 뒤 needs_home=True")


async def estop_delay_move_flow(c):
    """일반 이동 중의 비상정지. 서버를 새로 띄운 케이스라 앞 단계의 밀린 명령이 없다.

    이동이 끊겼는지를 '목표에 도달했는가' 로 본다 - 비상정지가 줄을 서면 이동이
    끝까지 가서 목표에 도달해 버린다.
    """
    await c.send(cmd="goto", x=200, y=200)
    await asyncio.sleep(0.3)               # 이동이 시작된 뒤
    await c.send(cmd="estop")
    # 끊기지 않았다면 이동(3초 × 2축)이 끝나 목표에 도달할 시간을 준다.
    await c.pump(8.0)
    st = c.state["stage"]
    check(not (st["x_mm"] == 200.0 and st["y_mm"] == 200.0),
          "이동 중 비상정지로 목표에 도달하지 않음 (X%s Y%s)" % (st["x_mm"], st["y_mm"]))
    check(bool(st["needs_home"]), "이동 중 비상정지 뒤 이동 잠금")
    # 이동이 실제로 시작은 했어야 한다(시작도 못 했으면 위 검사가 의미 없다).
    check(any("이동" in l["msg"] or "mx" in l["msg"] for l in c.logs),
          "이동이 시작은 했다")


async def origin_flow(c):
    """수동 원점 등록(V8). 원점이 없는 상태에서 시작한다."""
    st = c.state["stage"]
    check(not (st["homed_x"] and st["homed_y"]), "원점 없이 시작")
    check(st["fw"] == "V11", "펌웨어 V11 인식 (%s)" % st["fw"])

    c.acks.clear()
    await c.send(cmd="jog", axis="x", delta_mm=5)
    await c.pump(2.5)
    acks = [a for a in c.acks if a.get("of") == "jog"]
    check(bool(acks) and acks[-1]["ok"],
          "원점 없이 수동 이동 ok (%s)" % (acks[-1] if acks else "없음"))
    check(c.state["stage"]["jog_mode_x"] == "rel",
          "jog_mode_x=rel (%s)" % c.state["stage"]["jog_mode_x"])

    c.logs.clear()
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(1.5)
    check(any("원점 없음" in l["msg"] for l in c.logs), "원점 없으면 goto 는 거절")

    # 두 축 등록
    c.logs.clear()
    await c.send(cmd="set_origin", axis="xy", gap_mm=2)
    await c.pump(4.0)
    st = c.state["stage"]
    check(st["homed_x"] and st["homed_y"], "원점 등록(X·Y) 후 homed")
    check(st["jog_mode_x"] == "abs" and st["jog_mode_y"] == "abs",
          "등록 후 두 축 abs (%s/%s)" % (st["jog_mode_x"], st["jog_mode_y"]))
    check(any("원점 등록" in l["msg"] for l in c.logs), "원점 등록 로그")

    # 비상정지 -> 펌웨어 원점 기록까지 지운다
    c.logs.clear()
    await c.send(cmd="estop")
    await c.pump(3.0)
    st = c.state["stage"]
    check(not st["homed_x"] and not st["homed_y"], "비상정지 뒤 두 축 원점 해제(forget)")
    check(any("원점 기록 삭제" in l["msg"] for l in c.logs), "원점 기록 삭제 로그")

    # 비상정지 잠금이 남아 있어도 끝단까지 미는 복구는 끝까지 가야 한다.
    # (_aborted 를 그대로 보면 두 번째 조각 앞에서 '중단' 으로 끝났다)
    x0 = c.state["stage"]["x_mm"]
    c.logs.clear()
    await c.send(cmd="touch_end", axis="x", mm=120)
    await c.pump(4.0)
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> jx ")]
    check(sent == ["-> jx -8000", "-> jx -8000", "-> jx -3200"],
          "비상정지 뒤에도 조각이 모두 나간다 (%s)" % sent)
    check(c.state["stage"]["x_mm"] == x0 - 120.0,
          "비상정지 뒤 끝단 이동 120mm 완주 (%s -> %s)" % (x0, c.state["stage"]["x_mm"]))
    check(not any("중단" in l["msg"] for l in c.logs),
          "복구 이동이 '중단' 으로 끝나지 않는다")

    # 축 하나만 등록하면 잠금은 그대로
    c.logs.clear()
    await c.send(cmd="set_origin", axis="x", gap_mm=0)   # gap_mm 은 무시된다
    await c.pump(3.0)
    st = c.state["stage"]
    check(any(l["msg"] == "-> jx 320" for l in c.logs),
          "gap_mm 0 을 보내도 2mm(320 펄스) 물러난다")
    check(st["homed_x"] and not st["homed_y"], "X 만 등록됨")
    check(bool(st["needs_home"]), "한 축만 등록하면 잠금 유지")
    await c.send(cmd="set_origin", axis="y", gap_mm=0)
    await c.pump(3.0)
    st = c.state["stage"]
    check(st["homed_x"] and st["homed_y"], "Y 까지 등록됨")
    check(not st["needs_home"], "두 축 등록 후 잠금 해제")
    ph = ((c.state or {}).get("sequence") or {}).get("phase")
    check(ph in ("idle", "ready"), "원점 등록 후 phase (%s)" % ph)


async def touch_end_flow(c):
    """끝단 이동: 원점이 있는 축도 밀 수 있고, 범위를 벗어나면 그 축 원점이 풀린다."""
    st = c.state["stage"]
    check(st["homed_x"] and st["homed_y"], "원점 있는 상태로 시작")

    c.logs.clear()
    await c.send(cmd="touch_end", axis="y", mm=120)
    await c.pump(4.0)
    st = c.state["stage"]
    check(st["y_mm"] == -120.0, "끝단 이동 120mm 로 Y -120 (%s)" % st["y_mm"])
    # 펌웨어 한도는 한 번에 8000 펄스(50mm)다. 드라이버가 나눠 보내야 한다.
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> jy ")]
    check(sent == ["-> jy -8000", "-> jy -8000", "-> jy -3200"],
          "50mm 조각으로 나눠 보냄 (%s)" % sent)
    check(not st["homed_y"], "끝단 이동으로 Y 원점 해제")
    check(st["homed_x"], "X 원점은 그대로")
    check(st["jog_mode_y"] == "rel" and st["jog_mode_x"] == "abs",
          "축별 모드 (x %s · y %s)" % (st["jog_mode_x"], st["jog_mode_y"]))
    check(any("Y 원점 해제" in l["msg"] for l in c.logs), "원점 해제 안내 로그")

    c.logs.clear()
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(1.5)
    check(any("Y 원점 없음" in l["msg"] for l in c.logs),
          "goto 거절 사유가 축별 (%s)" % [l["msg"] for l in c.logs][:2])

    await c.send(cmd="set_origin", axis="y", gap_mm=2)
    await c.pump(3.0)
    check(c.state["stage"]["homed_y"], "Y 원점 다시 등록")
    c.logs.clear()
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(3.0)
    check(any("이동 완료" in l["msg"] for l in c.logs), "등록 뒤 goto 성공")

    c.logs.clear()
    await c.send(cmd="touch_end", axis="x", mm=300)
    await c.pump(1.5)
    check(any("1~247.6 mm" in l["msg"] for l in c.logs), "끝단 이동 300mm 거부")


async def touch_end_abort_flow(c):
    """조각으로 나눈 끝단 이동이 비상정지에서 멈추는가(남은 조각을 보내지 않는다)."""
    c.logs.clear()
    await c.send(cmd="touch_end", axis="x", mm=150)     # 8000 x3 + 나머지
    await asyncio.sleep(0.5)                            # 첫 조각이 도는 중
    await c.send(cmd="estop")
    dt = await wait_log(c, "끝단 이동 중단", 10.0)
    check(dt is not None, "조각 도중 비상정지로 끝단 이동 중단")
    n = len([l for l in c.logs if l["msg"].startswith("-> jx ")])
    check(n == 1, "남은 조각을 보내지 않았다 (jx %d건)" % n)


async def z_flow(c):
    """Z 축(V9): 조그 · 끝단 이동 · 원점 등록, 그리고 복귀·파킹의 Z 선행."""
    st = c.state["stage"]
    check("z_mm" in st and "homed_z" in st and "jog_mode_z" in st,
          "state 에 Z 필드 (z_mm=%s homed_z=%s mode=%s)"
          % (st.get("z_mm"), st.get("homed_z"), st.get("jog_mode_z")))
    check(st["homed_z"] and st["jog_mode_z"] == "abs", "Z 원점 있는 상태로 시작")

    await c.send(cmd="jog", axis="z", delta_mm=5)
    await c.pump(2.5)
    check(c.state["stage"]["z_mm"] == 5.0,
          "jog z +5 -> Z 5 (%s)" % c.state["stage"]["z_mm"])

    # 끝단 이동은 0 쪽 = 위로 민다. 범위를 벗어나면 Z 원점이 풀린다.
    c.logs.clear()
    await c.send(cmd="touch_end", axis="z", mm=10)
    await c.pump(3.0)
    st = c.state["stage"]
    # 앞의 조그로 Z 는 +5 에 있었다. 거기서 10 을 위로 밀면 -5 다(0 쪽 = 위).
    check(st["z_mm"] == -5.0, "끝단 이동 z 10 -> Z -5 (%s)" % st["z_mm"])
    check(not st["homed_z"], "끝단 이동으로 Z 원점 해제")
    check(st["homed_x"] and st["homed_y"], "X·Y 원점은 그대로")

    c.logs.clear()
    await c.send(cmd="set_origin", axis="z")
    await c.pump(3.0)
    st = c.state["stage"]
    check(any(l["msg"] == "-> jz 320" for l in c.logs), "Z 등록 전 2mm 물러남 (jz 320)")
    check(any(l["msg"] == "-> zz" for l in c.logs), "Z 원점 등록 명령 (zz)")
    check(st["homed_z"] and st["z_mm"] == 0.0,
          "Z 원점 등록 후 Z0 (homed=%s z=%s)" % (st["homed_z"], st["z_mm"]))

    # 불변식: X·Y 이동 전에는 Z 를 먼저 맨 위로. 좌표 이동도 마찬가지다.
    await c.send(cmd="jog", axis="z", delta_mm=8)
    await c.pump(2.5)
    c.logs.clear()
    await c.send(cmd="goto", x=30, y=30)
    await c.pump(3.0)
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> m")]
    check(sent == ["-> mz 0.00", "-> mx 30.0", "-> my 30.0"],
          "좌표 이동도 Z 먼저 올림 mz -> mx -> my (%s)" % sent)
    # 원점 복귀는 Z 를 먼저 올리고 X·Y 를 간다. 순서가 전부다.
    await c.send(cmd="jog", axis="z", delta_mm=8)
    await c.pump(2.5)
    c.logs.clear()
    await c.send(cmd="return_origin")
    await c.pump(5.0)
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> m")]
    check(sent == ["-> mz 0.00", "-> mx 0.0", "-> my 0.0"],
          "원점 복귀 순서 mz -> mx -> my (%s)" % sent)
    st = c.state["stage"]
    check((st["x_mm"], st["y_mm"], st["z_mm"]) == (0.0, 0.0, 0.0),
          "원점 복귀 뒤 (0,0,0) (%s)" % [st["x_mm"], st["y_mm"], st["z_mm"]])

    # 이미 Z 가 0 이면 mz 를 보내지 않는다.
    await c.send(cmd="goto", x=30, y=30)
    await c.pump(3.0)
    c.logs.clear()
    await c.send(cmd="return_origin")
    await c.pump(5.0)
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> m")]
    check(sent == ["-> mx 0.0", "-> my 0.0"], "Z 가 0 이면 mz 생략 (%s)" % sent)

    # 파킹도 같은 순서다.
    await c.send(cmd="jog", axis="z", delta_mm=7)
    await c.pump(2.5)
    c.logs.clear()
    await c.send(cmd="park")
    await c.pump(5.0)
    sent = [l["msg"] for l in c.logs if l["msg"].startswith("-> m")]
    check(bool(sent) and sent[0] == "-> mz 0.00" and len(sent) == 3,
          "파킹도 mz 가 먼저 (%s)" % sent)

    # 끝단 이동 상한은 그 축의 가동범위다. Z 는 45mm(실측).
    top = c.state["limits"]["z_max_mm"]
    check(top == 45.0, "limits.z_max_mm = 45 (%s)" % top)
    c.logs.clear()
    await c.send(cmd="touch_end", axis="z", mm=50)
    await c.pump(1.5)
    check(any("1~45 mm" in l["msg"] for l in c.logs),
          "끝단 이동 z 50 거부 (%s)" % [l["msg"] for l in c.logs][:2])
    check(c.state["stage"]["z_mm"] == 0.0, "거부됐으니 Z 는 그대로 0")

    c.logs.clear()
    await c.send(cmd="touch_end", axis="z", mm=45)
    await c.pump(4.0)
    check(c.state["stage"]["z_mm"] == -45.0,
          "끝단 이동 z 45 통과 -> Z -45 (%s)" % c.state["stage"]["z_mm"])


async def limits_flow(c):
    """설정의 limits 가 곧바로 조그 자르기·범위 검사에 반영되는지.

    (서버는 케이스별 임시 설정 파일을 쓰므로 저장소 settings.json 은 건드리지 않는다.)
    """
    lim = c.state["limits"]
    check(lim == {"x_max_mm": 247.6, "y_max_mm": 247.8, "z_max_mm": 45.0},
          "기본 limits (%s)" % lim)
    mk = c.state.get("marker_mm") or {}
    check(sorted(mk.keys()) == ["0", "1", "2", "3"],
          "state.marker_mm 에 id 0~3 (%s)" % sorted(mk.keys()))

    # 히트건 작업 높이(예전 z_measure_mm)가 Z 가동범위보다 크면 저장 전부를 거절한다.
    # 예전 화면이 보내던 최상위 키(limits · z_measure_mm)도 이행해서 받는다.
    c.acks.clear()
    await c.send(cmd="settings_save",
                 limits={"x_max_mm": 200, "y_max_mm": 150, "z_max_mm": 40},
                 z_measure_mm=41)
    a = await wait_ack(c, "settings_save")
    check(bool(a) and not a["ok"] and "tool.items[1].z_work_mm" in a.get("errors", {}),
          "작업 높이 41 > Z 40 저장 거절 · 항목별 사유 (%s)" % (a or {}).get("errors"))
    await c.pump(0.5)
    check(c.state["limits"]["x_max_mm"] == 247.6, "거절됐으니 limits 도 그대로(부분 저장 없음)")

    await c.send(cmd="settings_save",
                 limits={"x_max_mm": 200, "y_max_mm": 150, "z_max_mm": 40},
                 z_measure_mm=39)
    a = await wait_ack(c, "settings_save")
    await c.pump(1.0)
    lim = c.state["limits"]
    check(bool(a) and a["ok"] and lim == {"x_max_mm": 200.0, "y_max_mm": 150.0, "z_max_mm": 40.0},
          "저장 뒤 limits 반영 (%s)" % lim)
    heat = [t for t in c.state["settings"]["tool"]["items"] if t["name"] == "히트건"]
    check(bool(heat) and heat[0]["z_work_mm"] == 39, "z_measure_mm → 히트건 작업 높이 39 (%s)" % heat)

    # 새 상한으로 잘린다: 조그(절대)는 상한에서 멈추고, goto 는 범위 밖을 거절한다.
    await c.send(cmd="set_origin", axis="z")
    await c.pump(3.0)
    await c.send(cmd="jog", axis="z", delta_mm=100)
    await c.pump(4.0)
    check(c.state["stage"]["z_mm"] == 40.0, "Z 조그 100 -> 40 에서 잘림 (%s)"
          % c.state["stage"]["z_mm"])
    await c.send(cmd="goto", x=30, y=30)
    await c.pump(3.0)
    await c.send(cmd="jog", axis="x", delta_mm=300)
    await c.pump(6.0)
    check(c.state["stage"]["x_mm"] == 200.0, "X 조그 300 -> 200 에서 잘림 (%s)"
          % c.state["stage"]["x_mm"])
    c.logs.clear()
    await c.send(cmd="goto", x=10, y=160)
    await c.pump(2.0)
    check(any("가동범위" in l["msg"] and "밖" in l["msg"] for l in c.logs),
          "goto Y160 > 150 거절 (%s)" % [l["msg"] for l in c.logs][:2])
    check(c.state["stage"]["y_mm"] == 30.0, "거절됐으니 Y 그대로 30")

    # 1~1000 mm 밖의 값은 거절된다(전부 통과해야 저장).
    await c.send(cmd="settings_save",
                 limits={"x_max_mm": 5000, "y_max_mm": 0, "z_max_mm": 45},
                 z_measure_mm=44)
    a = await wait_ack(c, "settings_save")
    await c.pump(0.5)
    lim = c.state["limits"]
    check(bool(a) and not a["ok"] and "limits.x_max_mm" in a["errors"]
          and "limits.y_max_mm" in a["errors"]
          and lim == {"x_max_mm": 200.0, "y_max_mm": 150.0, "z_max_mm": 40.0},
          "범위 밖 값은 거절 · 지금 값 유지 (%s · %s)" % (lim, (a or {}).get("errors")))

    # 끝단 이동 상한도 새 값을 따른다.
    c.logs.clear()
    await c.send(cmd="touch_end", axis="y", mm=160)
    await c.pump(1.5)
    check(any("1~150 mm" in l["msg"] for l in c.logs),
          "끝단 이동 y 160 거부 (%s)" % [l["msg"] for l in c.logs][:2])


async def z_alone_flow(c):
    """세 축 모두 원점이 없는 상태에서 Z 만 등록한다.

    연결 직후에는 비상정지가 없었으므로 needs_home 이 서면 안 되고(그러면 화면이
    "비상정지 · 원점 등록 필요" 라고 한다), Z 는 X·Y 와 무관하게 abs 가 되어야 한다.
    """
    st = c.state["stage"]
    check(not st["needs_home"],
          "연결 직후 needs_home=False (%s)" % st["needs_home"])
    check(not st["homed_x"] and not st["homed_y"] and not st["homed_z"],
          "세 축 모두 원점 없음")
    check(st["jog_mode_z"] == "rel", "등록 전 Z 는 rel (%s)" % st["jog_mode_z"])

    c.logs.clear()
    await c.send(cmd="set_origin", axis="z")
    await c.pump(3.0)
    st = c.state["stage"]
    check(st["homed_z"] and st["jog_mode_z"] == "abs",
          "Z 만 등록해도 Z 는 abs (homed_z=%s mode=%s)"
          % (st["homed_z"], st["jog_mode_z"]))
    check(not st["homed_x"] and not st["homed_y"], "X·Y 는 그대로 원점 없음")
    check(not st["needs_home"], "Z 등록이 needs_home 을 건드리지 않는다")

    await c.send(cmd="jog", axis="z", delta_mm=6)
    await c.pump(2.5)
    check(c.state["stage"]["z_mm"] == 6.0,
          "X·Y 원점 없이도 Z 는 움직인다 (%s)" % c.state["stage"]["z_mm"])

    # X·Y 는 여전히 잠겨 있고, 사유는 '비상정지' 가 아니라 '원점 없음' 이다.
    c.logs.clear()
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(1.5)
    msgs = [l["msg"] for l in c.logs]
    check(any("원점 없음" in m for m in msgs), "goto 는 원점 없음으로 거절 (%s)" % msgs[:2])
    check(not any("비상정지" in m for m in msgs), "거절 사유에 '비상정지' 가 없다")


async def no_z_origin_flow(c):
    """Z 원점이 없을 때 파킹·원점 복귀는 경고만 남기고 X·Y 만 간다."""
    await c.send(cmd="set_origin", axis="x")
    await c.pump(3.0)
    await c.send(cmd="set_origin", axis="y")
    await c.pump(3.0)
    st = c.state["stage"]
    check(st["homed_x"] and st["homed_y"] and not st["homed_z"],
          "X·Y 만 원점 있음 (%s)" % [st["homed_x"], st["homed_y"], st["homed_z"]])

    c.logs.clear()
    await c.send(cmd="park")
    await c.pump(5.0)
    msgs = [l["msg"] for l in c.logs]
    check(any("Z 원점 없음 · Z 는 두고 이동" in m for m in msgs),
          "파킹이 Z 원점 없음을 경고 (%s)" % [m for m in msgs if "Z" in m][:2])
    check(not any(m.startswith("-> mz") for m in msgs), "Z 명령을 보내지 않았다")
    px, py = c.state["settings"]["park_xy"]
    check((c.state["stage"]["x_mm"], c.state["stage"]["y_mm"]) == (px, py),
          "경고 뒤에도 파킹은 갔다 (%s)"
          % [c.state["stage"]["x_mm"], c.state["stage"]["y_mm"]])

    c.logs.clear()
    await c.send(cmd="return_origin")
    await c.pump(5.0)
    msgs = [l["msg"] for l in c.logs]
    check(any("Z 원점 없음 · Z 는 두고 이동" in m for m in msgs), "원점 복귀도 같은 경고")
    check((c.state["stage"]["x_mm"], c.state["stage"]["y_mm"]) == (0.0, 0.0),
          "원점 복귀는 (0,0) 까지 갔다 (%s)"
          % [c.state["stage"]["x_mm"], c.state["stage"]["y_mm"]])


async def fw_v8_flow(c):
    """V8 펌웨어에는 Z 가 없다 - Z 만 거부하고 X 는 그대로 된다."""
    check(c.state["stage"]["fw"] == "V8", "펌웨어 V8 인식 (%s)" % c.state["stage"]["fw"])
    c.logs.clear()
    await c.send(cmd="jog", axis="z", delta_mm=5)
    await c.pump(2.0)
    check(any("V9 필요" in l["msg"] for l in c.logs),
          "V8 에서 Z 이동 거부 (%s)" % [l["msg"] for l in c.logs][:3])
    c.acks.clear()
    await c.send(cmd="jog", axis="x", delta_mm=5)
    await c.pump(2.5)
    acks = [a for a in c.acks if a.get("of") == "jog"]
    check(bool(acks) and acks[-1]["ok"], "V8 에서도 X 이동은 된다 (%s)" % (acks[-1] if acks else "없음"))


async def estop_dedup_flow(c):
    """비상정지를 두 경로로 보내도 로그는 한 번만 남는가(WS + HTTP)."""
    c.logs.clear()
    await c.send(cmd="estop")
    await asyncio.sleep(0.2)
    await c.send(cmd="estop")
    await c.pump(3.0)
    n = len([l for l in c.logs if "펌웨어 중단 명령 전송" in l["msg"]])
    check(n == 1, "비상정지 2회에도 로그 1건 (%d건)" % n)


async def fw_old_flow(c):
    """V7 펌웨어에서는 수동 이동(jx/jy)이 범위 밖으로 못 나간다 - 절차를 못 쓴다."""
    st = c.state["stage"]
    check(st["fw"] == "V7", "펌웨어 V7 인식 (%s)" % st["fw"])
    c.logs.clear()
    await c.send(cmd="jog", axis="x", delta_mm=5)
    await c.pump(2.0)
    check(any("V8 필요" in l["msg"] for l in c.logs),
          "V7 에서 수동 이동 거부 (%s)" % [l["msg"] for l in c.logs][:3])


async def exit_moving_flow(c):
    """이동 중에는 종료를 받지 않는다(DRY_MOVE_S=3 으로 이동을 늘려 둔다)."""
    await c.send(cmd="goto", x=100, y=100)
    await asyncio.sleep(0.4)               # 이동이 시작된 뒤
    c.acks.clear()
    await c.send(cmd="exit")
    await c.pump(2.0)
    ex = [a for a in c.acks if a.get("of") == "exit"]
    check(bool(ex) and ex[0]["ok"] is False and ex[0]["reason"] == "moving",
          "이동 중 exit 거절 (%s)" % (ex[0] if ex else "없음"))
    st, _ = http_get(c.port, "/health")
    check(st == 200, "거절 뒤 서버는 살아 있다 (status %s)" % st)


async def exit_flow(c):
    """종료는 위치를 저장만 하고 끝난다 - 스테이지를 움직이지 않는다."""
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(3.5)                      # 이동 완료 + 자동 저장까지
    before = (c.state["stage"]["x_mm"], c.state["stage"]["y_mm"])
    check(before == (50.0, 50.0), "이동 완료 X50 Y50 (%s)" % (before,))

    c.logs.clear()
    c.acks.clear()
    await c.send(cmd="exit")
    await c.pump(3.0)
    ex = [a for a in c.acks if a.get("of") == "exit"]
    check(bool(ex) and ex[0]["ok"] is True, "exit ack ok (%s)" % (ex[0] if ex else "없음"))

    msgs = [l["msg"] for l in c.logs]
    check(any("save" in m for m in msgs), "종료 때 save 를 보낸다 (%s)" % msgs[:6])
    moved = [m for m in msgs if ("파킹" in m) or ("-> mx" in m) or ("-> my" in m)]
    check(not moved, "종료 경로에 이동 명령이 없다 (%s)" % moved)
    after = (c.state["stage"]["x_mm"], c.state["stage"]["y_mm"])
    check(after == before, "종료해도 위치 그대로 (%s -> %s)" % (before, after))


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


def case_settings(port, patch=None, base=None):
    """케이스의 임시 settings.json 경로. base(없으면 사용자 설정) + 기본 패치 + patch."""
    path = os.path.join(DATA_DIR, "settings_%d.json" % port)
    body = deep_merge(BASE_SETTINGS if base is None else base, DEFAULT_PATCH if base is None else {})
    body = deep_merge(body, patch or {})
    with open(path, "w", encoding="utf-8") as f:
        json.dump(body, f, ensure_ascii=False, indent=2)
    return path


def start_server(port, image, env_extra=None, patch=None, base=None):
    env = dict(os.environ)
    env["PYTHONIOENCODING"] = "utf-8"
    env[paths.ENV_DATA_DIR] = DATA_DIR     # 실제 data 폴더 대신 임시 폴더에 쓴다
    env[paths.ENV_SETTINGS] = case_settings(port, patch, base)
    env.update(env_extra or {})
    # 서버 출력은 파일로 받는다. PIPE 로 두고 읽지 않으면 검출 로그가 파이프 버퍼를
    # 채우는 순간 서버가 print 에서 멈춘다(실측: 순회가 '파킹' 에서 영영 안 끝났다).
    log_path = os.path.join(DATA_DIR, "server_%d.log" % port)
    log_f = open(log_path, "wb")
    p = subprocess.Popen(
        [sys.executable, os.path.join(ROOT, "run.py"),
         "--dry", "--no-window", "--port", str(port), "--image", image],
        cwd=ROOT, env=env, stdout=log_f, stderr=subprocess.STDOUT)
    p._smoke_log = log_f                   # 프로세스와 함께 닫는다
    if not wait_port(port):
        p.kill()
        log_f.close()
        with open(log_path, "rb") as f:
            out = f.read(4000).decode("utf-8", "replace")
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
    check(c.state["stage"]["x_mm"] == 0.0, "시작 위치 x=0 (%s)" % c.state["stage"]["x_mm"])

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
    check(c.state["stage"]["x_mm"] == 247.6,
          "jog x +300 -> 상한 247.6 (%s)" % c.state["stage"]["x_mm"])

    # 현재 위치를 파킹으로
    await c.send(cmd="park_here")
    await c.pump(2.0)
    got = c.state["settings"]["park_xy"]
    check(got == [247.6, 0.0], "park_here -> park_xy %s" % got)

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

    # 순회 중에는 종료도 받지 않는다(거절만 하고 순회는 계속된다).
    c.acks.clear()
    await c.send(cmd="exit")
    await c.pump(2.0)
    ex = [a for a in c.acks if a.get("of") == "exit"]
    check(bool(ex) and ex[0]["ok"] is False and ex[0]["reason"] == "busy",
          "순회 중 exit 거절 (%s)" % (ex[0] if ex else "없음"))
    ph = ((c.state or {}).get("sequence") or {}).get("phase")
    check(ph in ("running", "paused"), "거절 뒤에도 순회는 계속 (%s)" % ph)

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
    # 비상정지 뒤에는 forget 으로 두 축 원점이 풀리므로 사유가 "원점 없음 · …"
    # 으로 바뀐다. 어느 쪽이든 원점을 요구하는 잠금이면 된다.
    blocked = [l for l in c.logs if "원점" in l["msg"] and "등록" in l["msg"]]
    check(len(blocked) >= 3, "park/goto/run 이 모두 잠김 (%d건)" % len(blocked))

    await c.send(cmd="set_origin", axis="xy", gap_mm=0)
    await c.pump(3.0)
    check(not c.state["stage"]["needs_home"], "원점 등록 후 잠금 해제")
    q = c.state["sequence"]
    check(q["phase"] == "ready", "원점 등록 후 phase=ready (%s)" % q["phase"])
    check(q["estopped"] is False, "원점 등록 후 estopped=False")
    check(c.state["stage"]["last_error"] == "",
          "원점 등록 후 last_error 비움 (%r)" % c.state["stage"]["last_error"])
    c.logs.clear()
    await c.send(cmd="park")
    await c.pump(2.0)
    ok = any("이동 완료" in l["msg"] for l in c.logs)
    check(ok, "원점 등록 후 이동이 다시 된다")


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


async def wait_ack(c, of, timeout=10.0):
    """그 명령의 ack 를 기다린다(c.acks 에서 꺼낸다). 없으면 None."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        for a in c.acks:
            if a.get("of") == of:
                c.acks.remove(a)
                return a
        await c.pump(0.1)
    return None


def _free_px(st, gap_px=40.0):
    """감지영역 안에서 모든 샘플 (u,v) 와 gap_px 이상 떨어진 점. 가운데부터 찾는다."""
    u0, v0, u1, v1 = st["sensing"]["rect"]
    cu, cv = (u0 + u1) / 2.0, (v0 + v1) / 2.0
    pts = [(u, v) for u in range(int(u0) + 20, int(u1) - 20, 8)
           for v in range(int(v0) + 20, int(v1) - 20, 8)]
    pts.sort(key=lambda p: (p[0] - cu) ** 2 + (p[1] - cv) ** 2)
    for u, v in pts:
        if all(((u - s["u"]) ** 2 + (v - s["v"]) ** 2) ** 0.5 >= gap_px
               for s in st["samples"]):
            return float(u), float(v)
    return None


def _free_mm(st, edge_mm=10.0, gap_mm=10.0):
    """마커 중심 사각형 안쪽(가장자리에서 edge_mm)에서 모든 샘플과 gap_mm 떨어진 점."""
    mk = list(st["marker_mm"].values())
    x0, x1 = min(p[0] for p in mk) + edge_mm, max(p[0] for p in mk) - edge_mm
    y0, y1 = min(p[1] for p in mk) + edge_mm, max(p[1] for p in mk) - edge_mm
    x = x0
    while x <= x1:
        y = y0
        while y <= y1:
            if all(((x - s["X"]) ** 2 + (y - s["Y"]) ** 2) ** 0.5 >= gap_mm
                   for s in st["samples"]):
                return round(x, 1), round(y, 1)
            y += 2.0
        x += 2.0
    return None


def _nos(c):
    return [s["no"] for s in c.state["samples"]]


async def edit_flow(c):
    """샘플 편집: 클릭 추가 · 현재 위치 추가 · 삭제 · 되돌리기 · 새 촬영 확인 · 순회 중 거절."""
    # 1) 촬영
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    check(ph == "ready" and len(c.state["samples"]) == 16,
          "편집 전 촬영 16개 (%s, %d)" % (ph, len(c.state["samples"])))
    check(c.state["edit"]["undo"] == 0, "촬영 직후 edit.undo 0 (%s)" % c.state["edit"])
    out_dir = c.state["sequence"]["out_dir"]
    rect = c.state["sensing"]["rect"]

    # 2) 빈 곳 클릭 추가
    pt = _free_px(c.state)
    check(pt is not None, "감지영역 안 빈 점 (%s)" % (pt,))
    c.acks.clear()
    await c.send(cmd="sample_add", u=pt[0], v=pt[1])
    a = await wait_ack(c, "sample_add")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and a.get("no") == 17, "sample_add ack ok · no 17 (%s)" % a)
    s17 = next((s for s in c.state["samples"] if s["no"] == 17), None)
    check(bool(s17) and s17["manual"] and s17["status"] == "wait" and s17["on"],
          "#17 manual · wait · on (%s)" % s17)
    ed = c.state["edit"]
    check(ed["undo"] == 1 and ed["manual"] == 1, "edit undo 1 · manual 1 (%s)" % ed)
    with open(os.path.join(out_dir, "samples_edit.json"), encoding="utf-8") as f:
        je = json.load(f)
    check(len(je) == 17 and sum(1 for x in je if x["source"] == "manual") == 1
          and set(je[0]) == {"no", "u", "v", "X", "Y", "source"},
          "samples_edit.json 17개 · manual 1 (%d)" % len(je))
    with open(os.path.join(out_dir, "samples.json"), encoding="utf-8") as f:
        check(len(json.load(f)) == 16, "samples.json 은 검출 16개 그대로")

    # 3) 겹침 · 감지영역 밖 · 잘못된 좌표
    s1 = c.state["samples"][0]
    await c.send(cmd="sample_add", u=s1["u"], v=s1["v"])
    a = await wait_ack(c, "sample_add")
    check(bool(a) and not a["ok"] and a["reason"] == "overlap",
          "기존 샘플 자리 -> overlap (%s)" % a)
    await c.send(cmd="sample_add", u=max(0, rect[0] - 15), v=max(0, rect[1] - 15))
    a = await wait_ack(c, "sample_add")
    check(bool(a) and not a["ok"] and a["reason"] == "outside",
          "감지영역 밖 -> outside (%s)" % a)
    await c.send(cmd="sample_add", u="nan", v=10)
    a = await wait_ack(c, "sample_add")
    check(bool(a) and not a["ok"] and a["reason"] == "bad_args", "NaN 좌표 -> bad_args (%s)" % a)
    await c.pump(0.3)
    check(len(c.state["samples"]) == 17, "거절 뒤 개수 그대로 17 (%d)" % len(c.state["samples"]))

    # 4) 삭제 · 되돌리기
    one = dict(next(s for s in c.state["samples"] if s["no"] == 1))
    await c.send(cmd="sample_delete", no=1)
    await c.pump(0.8)
    check(1 not in _nos(c) and c.state["edit"]["deleted"] == 1,
          "sample_delete 1 -> 빠짐 · deleted 1 (%s)" % c.state["edit"])
    await c.send(cmd="sample_undo")
    await c.pump(0.8)
    back = next((s for s in c.state["samples"] if s["no"] == 1), None)
    check(bool(back) and back == one and _nos(c) == sorted(_nos(c)) and _nos(c)[0] == 1,
          "되돌리기 -> #1 같은 X·Y·윤곽으로 번호순 제자리 (%s)" % _nos(c)[:3])
    await c.send(cmd="sample_undo")
    await c.pump(0.8)
    check(17 not in _nos(c) and c.state["edit"]["undo"] == 0,
          "되돌리기 -> #17 빠짐 · undo 0 (%s)" % c.state["edit"])
    c.logs.clear()
    await c.send(cmd="sample_undo")
    await c.pump(0.8)
    check(any("되돌릴 편집 없음" in l["msg"] for l in c.logs)
          and not any(l["level"] == "err" for l in c.logs),
          "빈 스택 되돌리기 -> 로그만 (%s)" % [l["msg"] for l in c.logs])

    # 5) 현재 위치를 샘플로
    xy = _free_mm(c.state)
    check(xy is not None, "마커 사각형 안 빈 자리 (%s)" % (xy,))
    c.logs.clear()
    await c.send(cmd="goto", x=xy[0], y=xy[1])
    await wait_log(c, "이동 완료", 20)
    await c.pump(0.5)
    await c.send(cmd="sample_add_here")
    a = await wait_ack(c, "sample_add")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and a.get("no") == 18,
          "sample_add_here -> no 18 (되돌린 17 재사용 안 함) (%s)" % a)
    s18 = next((s for s in c.state["samples"] if s["no"] == 18), None)
    inside = bool(s18) and rect[0] <= s18["u"] <= rect[2] and rect[1] <= s18["v"] <= rect[3]
    check(inside and s18["X"] == xy[0] and s18["Y"] == xy[1],
          "#18 = 현재 위치 X·Y · (u,v) 감지영역 안 (%s)" % s18)
    if s18:
        await c.send(cmd="sample_add", u=s18["u"], v=s18["v"])
        a = await wait_ack(c, "sample_add")
        check(bool(a) and not a["ok"] and a["reason"] == "overlap",
              "#18 의 (u,v) 클릭 -> overlap (px↔mm 왕복) (%s)" % a)

    # 6) 감지영역 밖 위치
    c.logs.clear()
    await c.send(cmd="goto", x=5, y=5)
    await wait_log(c, "이동 완료", 20)
    await c.pump(0.5)
    await c.send(cmd="sample_add_here")
    a = await wait_ack(c, "sample_add")
    check(bool(a) and not a["ok"] and a["reason"] == "outside",
          "(5,5) 에서 추가 -> outside (%s)" % a)

    # 7) 편집이 남은 채 새 촬영 -> 확인 요구
    n_before = len(c.state["samples"])
    await c.send(cmd="capture")
    a = await wait_ack(c, "capture")
    await c.pump(0.5)
    check(bool(a) and not a["ok"] and a["reason"] == "needs_confirm" and a["needs_confirm"],
          "편집 남은 capture -> needs_confirm (%s)" % a)
    check(len(c.state["samples"]) == n_before and c.state["sequence"]["phase"] == "ready",
          "확인 전에는 찍지 않음 (%d)" % len(c.state["samples"]))
    await c.send(cmd="capture", confirm=True)
    await c.wait_phase(("capturing",), 10)
    ph = await c.wait_phase(("ready", "error"), 90)
    ed = c.state["edit"]
    check(ph == "ready" and len(c.state["samples"]) == 16
          and ed == {"undo": 0, "manual": 0, "deleted": 0},
          "confirm 촬영 -> 16개 · 편집 0 (%s)" % ed)

    # 8) 순회 중 편집 거절 · results.csv source 열
    pt = _free_px(c.state)
    await c.send(cmd="sample_add", u=pt[0], v=pt[1])
    a = await wait_ack(c, "sample_add")
    check(bool(a) and a["ok"] and a.get("no") == 17, "새 촬영 뒤 번호 다시 17 (%s)" % a)
    await c.send(cmd="run", mode="auto", dwell_s=1.0, confirm=True)
    ph = await c.wait_phase(("running",), 20)
    check(ph == "running", "편집 샘플로 run -> running (%s)" % ph)
    c.logs.clear()
    c.acks.clear()
    n_run = len(c.state["samples"])
    await c.send(cmd="sample_add", u=pt[0] + 60, v=pt[1])
    await c.send(cmd="sample_delete", no=1)
    await c.send(cmd="sample_undo")
    await c.pump(1.5)
    blocked = [l for l in c.logs if "순회 중" in l["msg"]]
    a = [x for x in c.acks if x.get("of") == "sample_add"]
    check(len(blocked) >= 3 and bool(a) and a[0]["reason"] == "busy"
          and len(c.state["samples"]) == n_run and c.state["edit"]["undo"] == 1,
          "순회 중 추가·삭제·되돌리기 거절 (로그 %d건 · ack %s)"
          % (len(blocked), a[0]["reason"] if a else "없음"))
    await c.send(cmd="stop")
    await c.wait_phase(("stopped", "done", "error"), 60)
    with open(os.path.join(c.state["sequence"]["out_dir"], "results.csv"),
              encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))
    check(rows[0] == ["no", "shape", "X_mm", "Y_mm", "value", "unit", "status", "time",
                      "source"], "results.csv 기존 열 순서 + source (%s)" % rows[0])
    man = [r for r in rows[1:] if r[-1] == "manual"]
    check(len(man) == 1 and man[0][0] == "17", "results.csv manual 행 #17 (%s)" % man)


def dry_io_check():
    """dry 드라이버(V11 흉내)의 릴레이 · 통신 감시 · sp. 서버 없이 직접 부른다."""
    from core import stage as stage_mod
    print("[dry 펌웨어 V11]", flush=True)
    st = stage_mod.Stage(dry=True, log_path=os.path.join(DATA_DIR, "dry_io.log"))
    st.open()
    check(st.fw_version == "V11", "dry 펌웨어 V11 (%s)" % st.fw_version)
    check(st.relay_status() == [False] * 10, "릴레이 전원 투입 때 전부 꺼짐")
    r = st.relay_set(3, True)
    check(r[2] and sum(r) == 1, "r 3 1 → R3 만 켜짐 (%s)" % r)
    st.abort()
    st.forget()
    check(st.relay_status()[2], "비상정지·forget 뒤에도 릴레이 유지")
    check(st.relay_all_off() == [False] * 10, "r all 0 → 전부 꺼짐")
    try:
        st.relay_set(11, True)
        check(False, "릴레이 11 거부")
    except stage_mod.StageError:
        check(True, "릴레이 11 거부")
    check(not hasattr(st, "homing_status"), "호밍 센서 함수 제거")
    st.reset_abort()
    ev = []
    st.on_event = lambda k, ln: ev.append(k)
    st.relay_set(2, True)
    st.watchdog(300)
    st.dry_stall(2.0)
    time.sleep(0.8)
    check(ev == ["wd_trip"] and st.relay_status() == [False] * 10,
          "dry 통신 감시: 끊기면 릴레이 전부 꺼짐 + 사건 (%s)" % ev)
    st.dry_slip(1.5, 0.0)
    tx = st.dry_true_xy()[0]
    s2 = st.set_position(40.0, 50.0)
    check(s2["x_mm"] == 40.0 and s2["homed_x"] and abs(st.dry_true_xy()[0] - tx) < 1e-9,
          "dry sp: 보고 위치만 바뀌고 실제 위치는 그대로")
    st.close()


# --------------------------------------------------------------------------
# 2026-11 개편 검증: 설정 · 릴레이 · 레시피 · 공구 · 촬영 위치 · 사진 호밍 · 공구 보정
# --------------------------------------------------------------------------
def _sent(c, prefix="-> "):
    """보낸 시리얼 원문(로그) 목록."""
    return [l["msg"][3:] for l in c.logs if l.get("serial") and l["msg"].startswith(prefix)]


async def wait_for(c, pred, timeout=10.0, step=0.2):
    """state 가 조건을 만족할 때까지. 걸린 시간 또는 None."""
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout:
        await c.pump(step)
        try:
            if pred(c.state):
                return time.monotonic() - t0
        except (KeyError, TypeError, IndexError):
            pass
    return None


async def settings_flow(c):
    """설정: 스키마 · 검증 거절(부분 저장 없음) · 저장 · 백업 · 기본값 · 내보내기/가져오기."""
    # 스키마(WS + HTTP)
    await c.send(cmd="settings_schema")
    a = await wait_ack(c, "settings_schema")
    tabs = [t["id"] for t in ((a or {}).get("schema") or {}).get("tabs", [])]
    check(tabs == ["camera", "stage", "position", "photohome", "tool", "relay", "measure", "recipe",
                   "detect", "advanced"], "스키마 탭 10개 (%s)" % tabs)
    keys = [it["key"] for it in a["schema"]["items"]]
    for k in ("relay.items", "tool.items", "recipe.items", "capture_xy", "photohome.marker_id",
              "motion.speed_pps", "preview_fps", "cam_fail_limit", "origin_gap_mm"):
        check(k in keys, "스키마에 %s" % k)
    check(a["values"]["motion"]["speed_pps"] == 6000 and a["defaults"]["relay"]["watchdog_ms"] == 5000,
          "현재값 · 기본값 함께 옴")
    st, body = http_get(c.port, "/settings/schema")
    check(st == 200 and b'"schema"' in body, "GET /settings/schema 200 (%s)" % st)

    # 교차 검증 오류들
    await c.send(cmd="settings_validate", seq=7, values={
        "photohome": {"marker_id": 2}, "stat_grow": 30, "park_xy": [300, 10],
        "recipe": {"items": [{"name": "기본", "steps": [
            {"type": "move_sample"}, {"type": "relay", "n": 4, "on": True}]}]},
        "tool": {"items": [{"name": "기준 포인터", "dx_mm": 0, "dy_mm": 0, "z_work_mm": 0},
                           {"name": "히트건", "dx_mm": 0, "dy_mm": 0, "z_work_mm": 99}]}})
    a = await wait_ack(c, "settings_validate")
    e = (a or {}).get("errors", {})
    check(bool(a) and a["seq"] == 7 and not a["ok"], "settings_validate 거절 · seq 그대로")
    for k in ("photohome.marker_id", "stat_grow", "park_xy", "recipe.items[0].steps[1]",
              "tool.items[1].z_work_mm"):
        check(k in e, "검증 사유 %s (%s)" % (k, e.get(k)))

    # 하나라도 틀리면 아무것도 저장하지 않는다
    c.acks.clear()
    await c.send(cmd="settings_save", values={"stat_seed": 30, "photohome": {"marker_id": 1}})
    a = await wait_ack(c, "settings_save")
    await c.pump(0.5)
    check(bool(a) and not a["ok"] and "photohome.marker_id" in a["errors"],
          "틀린 항목이 있으면 저장 거절 (%s)" % (a or {}).get("errors"))
    check(c.state["settings"]["stat_seed"] == 25, "거절 때 옳은 항목도 저장 안 함(부분 저장 없음)")

    # 저장 · 백업 · 모르는 키 보존
    bdir = os.path.join(DATA_DIR, "settings_backup")
    before = len(os.listdir(bdir)) if os.path.isdir(bdir) else 0
    await c.send(cmd="settings_save", values={"motion": {"speed_pps": 5000}, "stat_seed": 30})
    a = await wait_ack(c, "settings_save")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["settings"]["motion"]["speed_pps"] == 5000,
          "저장 ok · 속도 5000 반영")
    after = len(os.listdir(bdir)) if os.path.isdir(bdir) else 0
    check(after == before + 1, "직전본 백업 1개 생성 (%d → %d)" % (before, after))
    with open(os.path.join(DATA_DIR, "settings_%d.json" % c.port), encoding="utf-8") as f:
        disk = json.load(f)
    check(disk.get("zz_unknown") == {"keep": 1}, "모르는 키 보존 (%s)" % disk.get("zz_unknown"))
    check("de_floor" in disk, "검출 파라미터(파일 편집 키) 보존")
    check(disk["motion"]["speed_pps"] == 5000 and disk["stat_seed"] == 30, "파일에 저장됨")

    # 백업은 최근 20개만
    for i in range(22):
        await c.send(cmd="settings_save", values={"autosave_s": 3.0 + (i % 2) * 0.5})
        await wait_ack(c, "settings_save")
    n = len(os.listdir(bdir))
    check(n == 20, "백업 최근 20개 유지 (%d)" % n)

    # 탭 기본값으로(화면은 스키마 기본값을 채워 저장한다 - 같은 경로)
    await c.send(cmd="settings_schema")
    sc = await wait_ack(c, "settings_schema")
    dflt = sc["defaults"]
    await c.send(cmd="settings_save", values={"motion": dflt["motion"], "stat_seed": dflt["stat_seed"]})
    a = await wait_ack(c, "settings_save")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["settings"]["motion"]["speed_pps"] == 6000
          and c.state["settings"]["stat_seed"] == 25, "기본값 복원 저장")

    # 내보내기 → 고쳐서 가져오기 → 틀린 파일 가져오기 거절
    await c.send(cmd="settings_export")
    a = await wait_ack(c, "settings_export")
    data = (a or {}).get("data") or {}
    check(bool(a) and a["ok"] and data.get("settings", {}).get("stat_seed") == 25
          and a.get("path") and os.path.exists(a["path"]), "내보내기 · 파일 생성 (%s)" % (a or {}).get("path"))
    data["settings"]["stat_seed"] = 31
    await c.send(cmd="settings_import", data=data)
    a = await wait_ack(c, "settings_import")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["settings"]["stat_seed"] == 31, "가져오기 반영 (31)")
    bad = json.loads(json.dumps(data))
    bad["settings"]["photohome"]["marker_id"] = 0
    await c.send(cmd="settings_import", data=bad)
    a = await wait_ack(c, "settings_import")
    await c.pump(0.3)
    check(bool(a) and not a["ok"] and "photohome.marker_id" in a.get("errors", {})
          and c.state["settings"]["photohome"]["marker_id"] == 10, "틀린 설정 가져오기 거절")
    await c.send(cmd="settings_import", data={"oops": "아님"})
    a = await wait_ack(c, "settings_import")
    check(bool(a) and not a["ok"] and a["reason"] == "bad_format",
          "설정 키가 없는 파일 → 형식 오류로 거절 (%s)" % (a or {}).get("reason"))

    # 조그 한 번 한도(jog.max_mm 50)
    c.acks.clear()
    await c.send(cmd="jog", axis="x", delta_mm=60)
    a = await wait_ack(c, "jog")
    check(bool(a) and not a["ok"] and a["reason"] == "over_limit", "조그 한도 초과 거절 (%s)" % a)


async def migrate_flow(c):
    """예전 settings.json(z_measure_mm · dwell_s · focus "auto")을 그대로 읽는다."""
    s = c.state["settings"]
    heat = [t for t in s["tool"]["items"] if t["name"] == "히트건"]
    check(bool(heat) and heat[0]["z_work_mm"] == 33, "z_measure_mm 33 → 히트건 작업 높이 (%s)" % heat)
    rc = [r for r in s["recipe"]["items"] if r["name"] == "기본"]
    waits = [st["s"] for st in (rc[0]["steps"] if rc else []) if st["type"] == "wait"]
    check(waits == [7.0], "dwell_s 7 → 기본 레시피 대기 (%s)" % waits)
    check(s["park_xy"] == [10.0, 20.0] and s["capture_xy"] == [10.0, 20.0],
          "park_xy 유지 · capture_xy 는 park_xy 로 (%s / %s)" % (s["park_xy"], s["capture_xy"]))
    check(s["marker_mm_xy"]["0"] == [200, 161], "marker_mm_xy 유지 (%s)" % s["marker_mm_xy"].get("0"))
    check(s["calib"]["mode"] == "fixed", "calib.mode 없는 예전 설정 → fixed (%s)" % s["calib"]["mode"])
    with open(os.path.join(DATA_DIR, "server_%d.log" % c.port), encoding="utf-8", errors="replace") as f:
        check("calib.mode 없음 → fixed" in f.read(), "이행 로그 한 줄 남김")
    check(s["focus_mode"] == "fixed" and s["focus_sweep"] is True and s["focus"] == -1,
          "focus \"auto\" → 고정 + 스윕 (%s %s %s)" % (s["focus_mode"], s["focus_sweep"], s["focus"]))
    # 저장하면 새 형식으로 쓰이고, 예전 키·모르는 키는 지우지 않는다
    await c.send(cmd="settings_save", values={"autosave_s": 4.0})
    a = await wait_ack(c, "settings_save")
    with open(os.path.join(DATA_DIR, "settings_%d.json" % c.port), encoding="utf-8") as f:
        disk = json.load(f)
    check(bool(a) and a["ok"] and "tool" in disk and "recipe" in disk
          and disk.get("zz_legacy") == "keep" and disk.get("dwell_s") == 7,
          "저장 뒤 새 형식 + 예전·모르는 키 보존")


async def relay_flow(c):
    """릴레이: 켜기/끄기/전체 끄기 · 사용 안 함 · 최대 켜짐 · 통신 감시 · 순회 중 · 정책."""
    st = c.state
    check(len(st["relays"]) == 10 and not any(r["on"] for r in st["relays"]), "연결 직후 10개 전부 꺼짐")
    c.acks.clear()
    await c.send(cmd="relay_set", n=2, on=True)
    a = await wait_ack(c, "relay_set")
    await c.pump(0.3)
    check(bool(a) and a["ok"] and c.state["relays"][1]["on"] and c.state["relays"][1]["since"],
          "R2 켜기 → state.relays[1].on + 켜진 시각")
    await c.send(cmd="relay_set", n=5, on=True)
    a = await wait_ack(c, "relay_set")
    check(bool(a) and not a["ok"] and a["reason"] == "disabled" and not c.state["relays"][4]["on"],
          "사용 안 함 R5 켜기 거절 (%s)" % (a or {}).get("reason"))
    await c.send(cmd="relay_set", n=2, on=False)
    await wait_ack(c, "relay_set")
    await c.pump(0.3)
    check(not c.state["relays"][1]["on"], "R2 끄기")
    await c.send(cmd="relay_set", n=2, on=True)
    await c.send(cmd="relay_set", n=3, on=True)
    await c.pump(0.8)
    await c.send(cmd="relay_all_off")
    a = await wait_ack(c, "relay_all_off")
    await c.pump(0.3)
    check(bool(a) and a["ok"] and not any(r["on"] for r in c.state["relays"]), "전체 끄기")

    # 최대 켜짐 시간(R1 = 1.5 s) 초과 → 앱이 끄고 경보
    await c.send(cmd="relay_set", n=1, on=True)
    await wait_ack(c, "relay_set")
    dt = await wait_for(c, lambda s: not s["relays"][0]["on"], 6.0)
    check(dt is not None and dt > 1.0, "최대 켜짐 1.5 s 넘으면 자동 꺼짐 (%s s)" % (dt and round(dt, 1)))
    check(any("최대 켜짐" in a["text"] for a in c.state["alarms"]), "최대 켜짐 초과 경보 배너")

    # 통신 감시: 드라이 펌웨어가 줄을 못 받은 척(stall) → 감시 시간(600 ms) 뒤 릴레이를 끈다
    await c.send(cmd="relay_set", n=2, on=True)
    await wait_ack(c, "relay_set")
    await c.pump(0.3)
    c.logs.clear()
    await c.send(cmd="dry_stall", s=3)
    dt = await wait_for(c, lambda s: any(a["src"] == "통신 감시" for a in s["alarms"]), 5.0)
    check(dt is not None, "통신 끊김 경보 (%s s)" % (dt and round(dt, 1)))
    check(not c.state["relays"][1]["on"], "통신 끊김 뒤 릴레이 꺼짐(펌웨어)")
    check(any("통신 끊김" in l["msg"] for l in c.logs), "통신 끊김 로그")
    await c.pump(3.5)                      # stall 이 끝나기를 기다린다

    # 순회 중: 수동 켜기 거절 · 전체 끄기 허용 · 정지 정책(끄기)
    await c.send(cmd="relay_set", n=3, on=True)
    await wait_ack(c, "relay_set")
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    await c.send(cmd="relay_set", n=3, on=True)
    await wait_ack(c, "relay_set")
    await c.send(cmd="run", mode="auto", confirm=True)
    await c.wait_phase(("running",), 20)
    c.acks.clear()
    await c.send(cmd="relay_set", n=2, on=True)
    a = await wait_ack(c, "relay_set")
    check(bool(a) and not a["ok"] and a["reason"] == "busy", "순회 중 수동 켜기 거절 (%s)" % (a or {}).get("reason"))
    check(c.state["relays"][2]["on"], "순회 전에 켠 R3 는 그대로 켜짐")
    await c.send(cmd="stop")
    await c.wait_phase(("stopped", "done", "error"), 60)
    await c.pump(0.5)
    check(not any(r["on"] for r in c.state["relays"]), "정지 정책(끄기) → 전부 꺼짐")

    # 비상정지 정책: '!' 와 같은 쓰기에 r all 0
    await c.send(cmd="relay_set", n=3, on=True)
    await wait_ack(c, "relay_set")
    await c.pump(0.3)
    c.logs.clear()
    await c.send(cmd="estop")
    await c.pump(2.0)
    first = [l["msg"] for l in c.logs if l.get("serial") and l["msg"].startswith("->")]
    check(bool(first) and first[0].startswith("-> ! (abort)") and "r all 0" in first[0],
          "비상정지가 맨 먼저 + 같은 쓰기에 r all 0 (%s)" % first[:2])
    check(not any(r["on"] for r in c.state["relays"]), "비상정지 정책(끄기) → 전부 꺼짐")
    await c.send(cmd="set_origin", axis="xy")
    await c.pump(3.0)

    # 연결 해제 정책
    await c.send(cmd="relay_set", n=3, on=True)
    await wait_ack(c, "relay_set")
    c.logs.clear()
    await c.send(cmd="stage_disconnect")
    await c.pump(1.5)
    check(any(l["msg"] == "-> r all 0" for l in c.logs), "연결 해제 정책 → r all 0 전송")
    check(not c.state["stage"]["connected"], "연결 해제됨")


async def recipe_flow(c):
    """레시피: 단계 순서 기록(steps.jsonl) · 릴레이 단계 · Z 불변식 · 일시정지/정지 · Z 원점 거절."""
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    check(ph == "ready", "촬영 (%s)" % ph)
    out_dir = c.state["sequence"]["out_dir"]
    await c.send(cmd="recipe_select", name="가열")
    a = await wait_ack(c, "recipe_select")
    await c.pump(0.3)
    check(bool(a) and a["ok"] and c.state["settings"]["recipe"]["active"] == "가열", "레시피 '가열' 선택")
    for s in c.state["samples"][3:]:
        await c.send(cmd="set_on", no=s["no"], on=False)   # 3개만 돈다
    await c.pump(0.5)
    c.logs.clear()
    await c.send(cmd="run", mode="auto", confirm=True)
    ph = await c.wait_phase(("done", "error", "stopped"), 90)
    check(ph == "done" and c.state["sequence"]["done"] == 3, "가열 레시피 3개 완료 (%s, %s)"
          % (ph, c.state["sequence"]["done"]))
    recs = []
    with open(os.path.join(out_dir, "steps.jsonl"), encoding="utf-8") as f:
        for ln in f:
            recs.append(json.loads(ln))
    start = [r for r in recs if r.get("event") == "run_start"]
    check(bool(start) and start[-1]["recipe"] == "가열" and start[-1]["tool"] == "히트건",
          "steps.jsonl 에 레시피 · 공구 이름 (%s)" % (start[-1] if start else None))
    steps = [r for r in recs if "step" in r]
    first = [r["type"] for r in steps if r["sample_no"] == steps[0]["sample_no"]]
    check(first == ["move_sample", "z", "relay", "wait", "measure", "relay", "z"],
          "샘플 하나의 단계 순서 (%s)" % first)
    check(all(r["result"] == "ok" and r["time_start"] <= r["time_end"] for r in steps),
          "모든 단계 ok · 시작/끝 시각")
    mv = steps[0]["detail"]
    check(abs(mv["stage_xy"][0] - (mv["sample_xy"][0] - 5.0)) < 0.011
          and abs(mv["stage_xy"][1] - (mv["sample_xy"][1] + 3.0)) < 0.011,
          "공구 오프셋 적용 P = S − (5, −3) (%s)" % mv)
    sent = _sent(c)
    check(sent.count("r 1 1") == 3 and sent.count("r 1 0") == 3, "릴레이 단계 켜기·끄기 3번씩")
    # 불변식: X·Y 를 보낼 때 Z 는 늘 맨 위
    z, bad = 0.0, []
    for m in sent:
        if m.startswith("mz "):
            z = float(m.split()[1])
        if (m.startswith("mx ") or m.startswith("my ")) and abs(z) > 1e-9:
            bad.append(m)
    check(not bad and any(m == "mz 10.00" for m in sent), "X·Y 이동 때 Z 는 늘 0 (위반 %s)" % bad)
    check(not any(r["on"] for r in c.state["relays"]), "끝나면 릴레이 정책으로 꺼짐")

    # 대기 단계 중 일시정지(남은 시간 멈춤) · 재개 · 정지(즉시)
    await c.send(cmd="recipe_select", name="긴 대기")
    await wait_ack(c, "recipe_select")
    await c.send(cmd="run", mode="auto", confirm=True)
    dt = await wait_for(c, lambda s: "대기" in (s["sequence"].get("step_desc") or "")
                        and (s["sequence"].get("step_left_s") or 0) > 0, 20)
    check(dt is not None, "대기 단계 진입")
    await c.send(cmd="pause")
    await wait_for(c, lambda s: s["sequence"]["phase"] == "paused", 5)
    await c.pump(0.6)
    l1 = c.state["sequence"]["step_left_s"]
    await c.pump(1.2)
    l2 = c.state["sequence"]["step_left_s"]
    check(c.state["sequence"]["phase"] == "paused" and l1 is not None and l1 == l2,
          "일시정지 중 대기 남은 시간 멈춤 (%s → %s)" % (l1, l2))
    await c.send(cmd="resume")
    await wait_for(c, lambda s: (s["sequence"].get("step_left_s") or 99) < l2 - 0.3, 5)
    check((c.state["sequence"].get("step_left_s") or 99) < l2, "재개 뒤 대기 다시 흐름")
    t0 = time.monotonic()
    await c.send(cmd="stop")
    ph = await c.wait_phase(("stopped",), 15)
    check(ph == "stopped" and time.monotonic() - t0 < 6.0,
          "대기 중 정지 → 즉시 중단 (%.1f s)" % (time.monotonic() - t0))
    with open(os.path.join(out_dir, "steps.jsonl"), encoding="utf-8") as f:
        recs = [json.loads(ln) for ln in f]
    check(any(r.get("result") == "stopped" and r.get("type") == "wait" for r in recs),
          "steps.jsonl 에 대기 단계 stopped")

    # Z 원점이 없으면 z 단계가 있는 레시피는 시작 거절
    await c.send(cmd="touch_end", axis="z", mm=10)
    await c.pump(2.5)
    check(not c.state["stage"]["homed_z"], "끝단 이동으로 Z 원점 해제")
    await c.send(cmd="recipe_select", name="가열")
    await wait_ack(c, "recipe_select")
    c.acks.clear()
    await c.send(cmd="run", mode="auto", confirm=True)
    a = await wait_ack(c, "run")
    check(bool(a) and not a["ok"] and any("Z 원점 없음" in r for r in a.get("needs_confirm", [])),
          "Z 원점 없음 → 시작 거절 (%s)" % (a or {}).get("needs_confirm"))


async def tool_flow(c):
    """공구: 오프셋 적용 목표 좌표 · 가동범위 밖 거절."""
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    s1 = c.state["samples"][0]
    check(c.state["tool"]["name"] == "히트건", "활성 공구 히트건 (%s)" % c.state["tool"]["name"])
    c.logs.clear()
    await c.send(cmd="goto", no=s1["no"])
    await wait_log(c, "이동 완료", 20)
    await c.pump(0.3)
    st = c.state["stage"]
    check(abs(st["x_mm"] - (s1["X"] - 5.0)) < 0.06 and abs(st["y_mm"] - (s1["Y"] + 3.0)) < 0.06,
          "선택 샘플 이동 = S − (5, −3) (%s,%s / %s,%s)" % (st["x_mm"], st["y_mm"], s1["X"], s1["Y"]))
    # 오프셋 -200 이면 X 큰 샘플은 스테이지 자리가 가동범위 밖
    items = c.state["settings"]["tool"]["items"]
    for t in items:
        if t["name"] == "히트건":
            t["dx_mm"] = -200.0
    await c.send(cmd="settings_save", values={"tool": {"items": items, "active": "히트건"}})
    await wait_ack(c, "settings_save")
    await c.pump(0.5)
    far = [s for s in c.state["samples"] if s["X"] + 200 > c.state["limits"]["x_max_mm"]]
    check(bool(far) and all(s["status"] == "skip" for s in far),
          "가동범위 밖 자리 샘플은 제외(skip) (%d개)" % len(far))
    c.logs.clear()
    await c.send(cmd="goto", no=far[0]["no"])
    await c.pump(1.5)
    check(any("가동범위" in l["msg"] and "밖" in l["msg"] for l in c.logs), "가동범위 밖 이동 거절")
    await c.send(cmd="tool_select", name="기준 포인터")
    a = await wait_ack(c, "tool_select")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["tool"]["name"] == "기준 포인터"
          and not any(s["status"] == "skip" for s in c.state["samples"]),
          "기준 포인터로 바꾸면 제외 해제")
    await c.send(cmd="tool_select", name="없는 공구")
    a = await wait_ack(c, "tool_select")
    check(bool(a) and not a["ok"], "목록에 없는 공구 선택 거절")


async def capture_pos_flow(c):
    """촬영 위치: 촬영 전 촬영 위치로 이동 · 순회 후 확인 촬영(after.png) · 순회 후 파킹."""
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    sent = _sent(c)
    check(ph == "ready" and "mx 10.0" in sent and "my 200.0" in sent,
          "촬영 전 촬영 위치(10, 200)로 이동 (%s)" % [m for m in sent if m.startswith("m")])
    st = c.state["stage"]
    check((st["x_mm"], st["y_mm"]) == (10.0, 200.0), "촬영은 촬영 위치에서 (%s)" % [st["x_mm"], st["y_mm"]])
    check(any("헤드 보정 없음" in l["msg"] for l in c.logs), "헤드 보정 없으면 위치 확인 건너뜀(촬영은 함)")
    for s in c.state["samples"][2:]:
        await c.send(cmd="set_on", no=s["no"], on=False)
    await c.pump(0.5)
    await c.send(cmd="run", mode="auto", confirm=True)
    ph = await c.wait_phase(("done", "error", "stopped"), 90)
    out = c.state["sequence"]["out_dir"]
    check(ph == "done" and os.path.exists(os.path.join(out, "after.png")),
          "순회 후 확인 촬영 after.png (%s)" % ph)
    st = c.state["stage"]
    px, py = c.state["settings"]["park_xy"]
    check((st["x_mm"], st["y_mm"]) == (px, py), "순회 후 갈 곳 = 파킹 (%s)" % [st["x_mm"], st["y_mm"]])
    # 현재 위치를 촬영 위치로
    await c.send(cmd="goto", x=20, y=150)
    await wait_log(c, "이동 완료", 20)
    await c.send(cmd="capture_here")
    await c.pump(1.0)
    check(c.state["settings"]["capture_xy"] == [20.0, 150.0], "현재 위치를 촬영 위치로 (%s)"
          % c.state["settings"]["capture_xy"])
    # 새 촬영 요구: 한 번 순회한 촬영으로는 다시 시작하지 않는다
    await c.send(cmd="settings_save", values={"flow": {"require_fresh_capture": True}})
    await wait_ack(c, "settings_save")
    c.acks.clear()
    await c.send(cmd="run", mode="auto", confirm=True)
    a = await wait_ack(c, "run")
    check(bool(a) and not a["ok"] and any("새 촬영" in r for r in a.get("needs_confirm", [])),
          "새 촬영 요구 → 시작 거절 (%s)" % (a or {}).get("needs_confirm"))


async def photohome_flow(c):
    """사진 호밍(합성 카메라): 보정 · 허용 · 자동 보정 · 확인 필요 · 미검출 · 원점 복구 · 보정 무효."""
    ph = c.state["photohome"]
    check(not ph["calib_valid"] and ph["calib_reason"] == "헤드 보정 없음", "처음엔 보정 없음")
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    check(bool(a) and not a["ok"], "보정 없이 사진 호밍 → 실패 (%s)" % (a or {}).get("reason"))

    # 헤드 마커 보정(격자 3×3)
    await c.send(cmd="head_calib_start")
    a = await wait_ack(c, "head_calib", 120)
    res = (a or {}).get("result") or {}
    check(bool(a) and a["ok"] and len(res.get("points", [])) == 9,
          "헤드 보정 9점 수집 (%s)" % (a or {}).get("reason"))
    check(res.get("rms_mm") is not None and res["rms_mm"] < 0.3 and res["ok"],
          "보정 잔차 RMS %.3f mm < 한계" % (res.get("rms_mm") or -1))
    check(not os.path.exists(os.path.join(DATA_DIR, "head_calib.json")), "[적용] 전에는 저장 안 함")
    await c.send(cmd="head_calib_apply")
    a = await wait_ack(c, "head_calib_apply")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["photohome"]["calib_valid"]
          and os.path.exists(os.path.join(DATA_DIR, "head_calib.json")), "적용 → head_calib.json · 유효")
    with open(os.path.join(DATA_DIR, "head_calib.json"), encoding="utf-8") as f:
        hc = json.load(f)
    check(hc["size"] == [1280, 720] and hc["camera_index"] == c.state["settings"]["camera_index"]
          and len(hc["points"]) == 9 and "res_mm" in hc["points"][0], "보정 파일에 해상도·번호·점별 잔차")

    # 허용 오차 안
    await c.send(cmd="goto", x=120, y=110)
    await wait_log(c, "이동 완료", 20)
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    check(bool(a) and a["ok"] and a["reason"] == "ok" and a["d_mm"] < 0.3,
          "허용 오차 안 → 그대로 (d %s)" % (a or {}).get("d_mm"))
    check(c.state["photohome"]["status"] == "ok", "위치 확인 칩 '정상'")

    # 허용~경고 → 자동 보정(sp)
    x0 = c.state["stage"]["x_mm"]
    await c.send(cmd="dry_slip", dx=1.0, dy=0.5)
    c.logs.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    await c.pump(0.5)
    check(bool(a) and a["ok"] and a["reason"] == "corrected", "1.1 mm 어긋남 → 자동 보정 (%s)" % (a or {}).get("reason"))
    check(abs(c.state["stage"]["x_mm"] - (x0 + 1.0)) < 0.25, "sp 뒤 기록 위치 = 실제 (%s → %s)"
          % (x0, c.state["stage"]["x_mm"]))
    check(any(l["msg"].startswith("-> sp ") for l in c.logs) and any(l["msg"] == "-> save" for l in c.logs),
          "sp 뒤 save")
    check(c.state["photohome"]["status"] == "corrected", "칩 '보정됨'")

    # 경고 초과 → 확인 요구 → 적용
    x1 = c.state["stage"]["x_mm"]
    await c.send(cmd="dry_slip", dx=8.0, dy=0.0)
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    check(bool(a) and not a["ok"] and a["reason"] == "needs_confirm" and a["d_mm"] > 5,
          "8 mm → 확인 요구 (d %s)" % (a or {}).get("d_mm"))
    check(abs(c.state["stage"]["x_mm"] - x1) < 0.01, "확인 전에는 바꾸지 않음")
    await c.send(cmd="photo_home_apply")
    a = await wait_ack(c, "photo_home_apply")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and abs(c.state["stage"]["x_mm"] - (x1 + 8.0)) < 0.25,
          "확인 후 적용 (%s → %s)" % (x1, c.state["stage"]["x_mm"]))

    # 미검출(마커가 시야 밖)
    await c.send(cmd="dry_slip", dx=600.0, dy=0.0)
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 20)
    await c.pump(0.3)
    check(bool(a) and not a["ok"] and "미검출" in c.state["photohome"]["msg"],
          "헤드 마커 미검출 (%s)" % c.state["photohome"]["msg"])
    await c.send(cmd="dry_slip", dx=-600.0, dy=0.0)

    # 원점 없음 복구: 비상정지(원점 기록 삭제) → 사진 호밍 → 확인 → 원점 복구
    await c.send(cmd="estop")
    await c.pump(2.0)
    check(not c.state["stage"]["homed_x"] and c.state["stage"]["needs_home"], "비상정지 뒤 원점 없음")
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    check(bool(a) and a["reason"] == "needs_confirm" and a["recorded"] is None,
          "원점 없음 → 확인 요구 (%s)" % (a or {}).get("needs_confirm"))
    await c.send(cmd="photo_home_apply")
    a = await wait_ack(c, "photo_home_apply")
    await c.pump(0.8)
    st = c.state["stage"]
    check(bool(a) and a["ok"] and st["homed_x"] and st["homed_y"] and not st["needs_home"]
          and not c.state["sequence"]["estopped"], "사진 호밍으로 원점 복구 · 잠금 해제")
    c.logs.clear()
    await c.send(cmd="goto", x=60, y=60)
    await c.pump(2.0)
    check(any("이동 완료" in l["msg"] for l in c.logs), "복구 뒤 이동 가능")

    # 보정 무효(해상도가 다름) → 사진 호밍 막힘 + 경보
    await c.send(cmd="settings_save", values={"width": 1024})
    await wait_ack(c, "settings_save")
    await c.pump(0.5)
    ph = c.state["photohome"]
    check(not ph["calib_valid"] and "해상도" in ph["calib_reason"], "해상도 바뀜 → 보정 무효 (%s)" % ph["calib_reason"])
    check(any(a["src"] == "사진 호밍" for a in c.state["alarms"]), "보정 무효 경보 배너")
    c.acks.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 15)
    check(bool(a) and not a["ok"], "보정 무효면 사진 호밍 안 함")


async def tool_calib_flow(c):
    """공구 오프셋 보정: 방법 1(맞추기) · 방법 2(자국)의 계산과 적용."""
    await c.send(cmd="goto", x=100, y=100)
    await wait_log(c, "이동 완료", 20)
    await c.send(cmd="calib_tool_record", which="A")
    a = await wait_ack(c, "calib_tool")
    c.logs.clear()
    await c.send(cmd="jog", axis="x", delta_mm=3)
    await wait_ack(c, "jog")
    await c.send(cmd="jog", axis="y", delta_mm=-1.5)
    await wait_ack(c, "jog")
    await c.send(cmd="calib_tool_record", which="B")
    a = await wait_ack(c, "calib_tool")
    d1 = ((a or {}).get("result") or {}).get("d1")
    check(d1 == [-3.0, 1.5], "방법 1: d = A − B = (−3, 1.5) (%s)" % d1)
    await c.send(cmd="calib_tool_apply", method="1", tool="히트건")
    a = await wait_ack(c, "calib_tool_apply")
    await c.pump(0.5)
    heat = [t for t in c.state["settings"]["tool"]["items"] if t["name"] == "히트건"][0]
    check(bool(a) and a["ok"] and (heat["dx_mm"], heat["dy_mm"]) == (-3.0, 1.5), "방법 1 적용 (%s)" % heat)

    # 방법 2: P0 에서 단독 가열 → 촬영 → 자국 클릭 → d = M − P0
    await c.send(cmd="goto", x=120, y=120)
    await wait_log(c, "이동 완료", 20)
    c.logs.clear()
    await c.send(cmd="calib_tool_heat", n=1, s=0.6, z_mm=5)
    a = await wait_ack(c, "calib_tool_heat", 20)
    p0 = ((a or {}).get("result") or {}).get("P0")
    check(bool(a) and a["ok"] and p0 == [120.0, 120.0], "가열 끝 · P0 (%s)" % p0)
    sent = _sent(c)
    check("mz 5.00" in sent and "r 1 1" in sent and "r 1 0" in sent and sent.index("r 1 0") > sent.index("r 1 1"),
          "가열: Z 내림 → 릴레이 켜기 → 끄기 (%s)" % [m for m in sent if m[:1] in "mr"])
    check(c.state["stage"]["z_mm"] == 0.0 and not any(r["on"] for r in c.state["relays"]),
          "가열 뒤 Z 맨 위 · 릴레이 꺼짐")
    await c.send(cmd="capture")
    await c.wait_phase(("ready", "error"), 90)
    s1 = c.state["samples"][0]
    await c.send(cmd="calib_tool_mark", u=s1["u"], v=s1["v"])
    a = await wait_ack(c, "calib_tool_mark")
    d2 = ((a or {}).get("result") or {}).get("d2")
    check(bool(d2) and abs(d2[0] - (s1["X"] - 120)) < 0.02 and abs(d2[1] - (s1["Y"] - 120)) < 0.02,
          "방법 2: d = M − P0 (%s · 샘플 %s,%s)" % (d2, s1["X"], s1["Y"]))
    await c.send(cmd="calib_tool_apply", method="2", tool="히트건")
    a = await wait_ack(c, "calib_tool_apply")
    await c.pump(0.5)
    heat = [t for t in c.state["settings"]["tool"]["items"] if t["name"] == "히트건"][0]
    check(bool(a) and a["ok"] and abs(heat["dx_mm"] - d2[0]) < 1e-6, "방법 2 적용 (%s)" % heat)


# --------------------------------------------------------------------------
# 고정 보정(calib.mode = fixed): 합성 판 장면(알려진 호모그래피 · 알려진 샘플 위치)
# --------------------------------------------------------------------------
FIXED = {"truth": None, "size": None}


def _synth():
    tools = os.path.join(ROOT, "tools")
    if tools not in sys.path:
        sys.path.insert(0, tools)
    import synth_cam
    return synth_cam


def prep_fixed(w, h, cal_size=None, with_h=True, with_area=True):
    """합성 장면 PNG 와 plate_calib.json 을 임시 데이터 폴더에 만든다. (장면 경로)"""
    import cv2
    from core import plate as plate_mod
    sc = _synth()
    img, truth = sc.render_plate(w, h)
    path = os.path.join(DATA_DIR, "scene_%dx%d.png" % (w, h))
    cv2.imwrite(path, img)
    FIXED["truth"], FIXED["size"] = truth, (w, h)
    H = truth["H"]
    quad = [list(sc.project(H, a, b)) for a, b in sc.PLATE_QUAD_MM]
    pts = []
    for x, y in sc.PLATE_CAL_MM:
        u, v = sc.project(H, x, y)
        pts.append({"x": x, "y": y, "u": round(u, 2), "v": round(v, 2)})
    f = plate_mod.fit(pts)
    for p, r in zip(pts, f["res"]):
        p["res_mm"] = round(r, 4)
    size = cal_size or (w, h)
    d = plate_mod.new_record(1, size)
    q, _why = plate_mod.check_quad(quad, (w, h))
    d.update({"area": q, "points": pts, "H": f["H"], "rms_mm": round(f["rms"], 4),
              "max_mm": round(f["max"], 4), "res_limit_mm": 1.0})
    if not with_h:
        d.update({"points": [], "H": None, "rms_mm": None, "max_mm": None, "res_limit_mm": None})
    if with_area:
        plate_mod.save(d, os.path.join(DATA_DIR, "plate_calib.json"))
    return path


REAL = {}


def real_scene():
    """실장 사진(data/truth + data/out)이 있으면 (사진 경로, 정답 항목) - 없으면 None(케이스 건너뜀)."""
    import glob
    from core import paths
    root = os.path.dirname(paths.OUT_DIR)
    name = "seq_20261105_140835"
    try:
        with open(os.path.join(root, "truth", "truth_20261105.json"), encoding="utf-8") as f:
            item = json.load(f)[name]
    except (OSError, KeyError, ValueError):
        return None
    img = os.path.join(root, "out", name, item["image"])
    return (img, item) if os.path.exists(img) else None


def prep_real_fixed():
    from core import plate as plate_mod
    img, item = REAL["scene"]
    d = plate_mod.new_record(1, item["size"])
    d["area"] = item["area"]
    plate_mod.save(d, os.path.join(DATA_DIR, "plate_calib.json"))
    return img


async def fixed_real_flow(c):
    """실장 사진 · 고정 환경 검출기: 격자 3x5 · source 표시 · 제외 영역 저장 · 빈 판 기준 저장."""
    img, item = REAL["scene"]
    truth = [(s["u"], s["v"]) for s in item["samples"]]
    check(c.state["settings"]["fixed"]["detector"] == "fixed", "검출기 기본 fixed")
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 180)
    st = c.state
    sm = st["samples"]
    check(ph == "ready" and len(sm) == 15, "격자 15칸 (%s · %d개)" % (ph, len(sm)))
    check(all(s.get("source") in ("detected", "local", "predicted") for s in sm), "source 필드")
    hit = sum(1 for t in truth if min(((s["u"] - t[0]) ** 2 + (s["v"] - t[1]) ** 2) ** 0.5 for s in sm) < 17)
    check(hit >= 14, "정답 근처 %d/15" % hit)
    check(sum(1 for s in sm if s["source"] == "detected") >= 12, "검출 12개 이상 (%s)" % [s["source"] for s in sm].count("detected"))
    check(all(s["weak"] for s in sm if s["source"] == "predicted"), "예측은 약함 표시")
    check(any(l["msg"].startswith("소요 ") for l in c.logs), "소요 로그")
    c.logs.clear()
    # 제외 영역: 감지 영역 구석의 작은 다각형 저장 → 상태 · 다음 촬영 반영(샘플 수 불변)
    a = item["area"]
    poly = [[a[2][0] - 60, a[2][1] - 60], [a[2][0] - 10, a[2][1] - 60], [a[2][0] - 10, a[2][1] - 10]]
    c.acks.clear()
    await c.send(cmd="plate_exclude_apply", polys=[poly], size=item["size"])
    ack = await wait_ack(c, "plate_exclude_apply")
    check(bool(ack) and ack["ok"], "제외 영역 저장 ack")
    await c.pump(0.5)
    check(len(c.state["plate"]["data"]["exclude"]) == 1, "state.plate.data.exclude 1개")
    await c.send(cmd="plate_exclude_apply", polys=[[[1, 1], [2, 2]]], size=item["size"])
    ack = await wait_ack(c, "plate_exclude_apply")
    check(bool(ack) and not ack["ok"], "점 3개 미만 거절")
    await c.send(cmd="capture", confirm=True)
    ph = await c.wait_phase(("ready", "error"), 180)
    check(ph == "ready" and len(c.state["samples"]) == 15, "제외 영역 적용 촬영 · 15칸 유지")
    # 빈 판 기준 저장(--image 라 같은 사진) · 상태
    c.acks.clear()
    await c.send(cmd="plate_ref_shot")
    ack = await wait_ack(c, "plate_ref_shot")
    check(bool(ack) and ack["ok"], "빈 판 기준 저장")
    await c.pump(0.5)
    ref = c.state["plate"]["ref"]
    check(ref["exists"] and ref["valid"], "빈 판 기준 유효 (%s)" % ref)

    # --- 번호는 위치 순서(행 우선) · 검출만 모드라 사진 좌표 ---
    sm = c.state["samples"]
    rows5 = [sm[i * 5:(i + 1) * 5] for i in range(3)]
    ok_order = all(sorted(r, key=lambda s: s["u"]) == r for r in rows5) and \
        all(max(s["v"] for s in rows5[i]) < min(s["v"] for s in rows5[i + 1]) for i in range(2))
    check([s["no"] for s in sm] == list(range(1, 16)) and ok_order, "번호 = 위치 순서(행 우선 · 3행 x 5열)")

    # --- 기준 보관 · 되돌리기 · 무효 표시 ---
    import glob
    hist_dir = os.path.join(DATA_DIR, "plate_ref_history")
    check(not glob.glob(os.path.join(hist_dir, "*.png")), "첫 기준: 보관본 없음")
    first_time = c.state["plate"]["ref"]["time"]
    await c.send(cmd="settings_save", values={"exposure": -3.0})        # 카메라 설정이 바뀌면 기준이 무효
    await c.pump(0.8)
    check(not c.state["plate"]["ref"]["valid"] and "노출" in c.state["plate"]["ref"]["reason"],
          "카메라 설정이 바뀌면 기준 무효 표시 (%s)" % c.state["plate"]["ref"]["reason"])
    await asyncio.sleep(1.1)
    c.acks.clear()
    await c.send(cmd="plate_ref_shot")
    ack = await wait_ack(c, "plate_ref_shot")
    await c.pump(0.6)
    ref = c.state["plate"]["ref"]
    check(bool(ack) and ack["ok"] and ref["valid"] and len(ref["history"]) == 1, "새 기준 촬영 → 이전 기준 보관 (%d개)" % len(ref["history"]))
    check(len(glob.glob(os.path.join(hist_dir, "*.png"))) == 1 and len(glob.glob(os.path.join(hist_dir, "*.json"))) == 1,
          "plate_ref_history 에 png + 메타")
    h0 = ref["history"][0]
    check(h0["time"] == first_time and not h0["valid"], "보관본: 찍은 시각 · 지금 설정과 달라 무효 (%s)" % h0)
    code, body = http_get(c.port, "/plate_ref_thumb/%s.jpg" % h0["name"])
    check(code == 200 and body[:2] == b"\xff\xd8", "미리보기 썸네일 (%s)" % code)
    # 무효 기준 사용 → 확인 필요, 확인하면 되돌린다(현재 기준은 보관)
    c.acks.clear()
    await c.send(cmd="plate_ref_use", name=h0["name"])
    ack = await wait_ack(c, "plate_ref_use")
    check(bool(ack) and not ack["ok"] and ack["reason"] == "needs_confirm", "무효 기준 사용은 확인을 받음 (%s)" % (ack or {}).get("reason"))
    c.acks.clear()
    await c.send(cmd="plate_ref_use", name=h0["name"], confirm=True)
    ack = await wait_ack(c, "plate_ref_use")
    await c.pump(0.6)
    ref = c.state["plate"]["ref"]
    check(bool(ack) and ack["ok"] and ref["time"] == first_time and not ref["valid"] and len(ref["history"]) >= 1,
          "이 기준 사용 → 현재 기준이 됨 · 무효 표시 유지 (%s)" % ref["time"])
    # 10개 넘으면 오래된 것부터 삭제
    for _ in range(12):
        await asyncio.sleep(1.05)
        await c.send(cmd="plate_ref_shot")
        await wait_ack(c, "plate_ref_shot")
    await c.pump(0.6)
    check(len(c.state["plate"]["ref"]["history"]) == 10 and len(glob.glob(os.path.join(hist_dir, "*.png"))) == 10,
          "보관은 최근 10개 (%d)" % len(c.state["plate"]["ref"]["history"]))
    await c.send(cmd="settings_save", values={"exposure": -5.0})
    await c.pump(0.5)

    # --- 경계 밖 · 제외 영역에 중심이 있는 검출은 뺀다 ---
    sm0 = c.state["samples"]
    tgt = sm0[7]
    poly = [[tgt["u"] - 60, tgt["v"] - 70], [tgt["u"] + 60, tgt["v"] - 70], [tgt["u"] + 60, tgt["v"] + 70], [tgt["u"] - 60, tgt["v"] + 70]]
    await c.send(cmd="plate_exclude_apply", polys=[poly], size=item["size"])
    await wait_ack(c, "plate_exclude_apply")
    await c.send(cmd="capture", confirm=True)
    ph = await c.wait_phase(("ready", "error"), 180)
    sm1 = c.state["samples"]
    inside = [s for s in sm1 if abs(s["u"] - tgt["u"]) < 60 and abs(s["v"] - tgt["v"]) < 70]
    check(ph == "ready" and len(sm1) == 14 and not inside,
          "제외 영역 안 검출은 결과에서 빠짐 (%d개 · 안 %d)" % (len(sm1), len(inside)))
    check([s["no"] for s in sm1] == list(range(1, 15)), "빠진 뒤 번호는 1부터 위치 순서")
    ann = os.path.join(c.state["sequence"]["out_dir"], "annotated.jpg")
    check(os.path.exists(ann), "annotated.jpg 저장(라벨 한 벌 규칙)")
    # 감지 영역을 줄여 경계 밖이 된 칩도 빠진다(제외 영역 지움 → 사각형 한쪽을 안으로)
    await c.send(cmd="plate_exclude_apply", polys=[], size=item["size"])
    await wait_ack(c, "plate_exclude_apply")
    a_ = item["area"]
    xs = sorted(p[0] for p in a_)
    cut = (xs[0] + xs[-1]) / 2.0 + 60
    quad = [[p[0] if p[0] < cut else cut, p[1]] for p in a_]
    await c.send(cmd="plate_area_apply", quad=quad, size=item["size"])
    await wait_ack(c, "plate_area_apply")
    await c.send(cmd="capture", confirm=True)
    ph = await c.wait_phase(("ready", "error"), 180)
    sm2 = c.state["samples"]
    check(ph == "ready" and 0 < len(sm2) < 15 and all(s["u"] <= cut + 2 for s in sm2),
          "감지 영역 경계 밖 검출은 빠짐 (%d개 · 최대 u %.0f ≤ %.0f)" % (len(sm2), max(s["u"] for s in sm2), cut))
    await c.send(cmd="plate_area_apply", quad=a_, size=item["size"])
    await wait_ack(c, "plate_area_apply")
    await c.send(cmd="capture", confirm=True)
    await c.wait_phase(("ready", "error"), 180)

    # --- 화면: 라벨 한 벌 · 겹침 없음(실장 사진) ---
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return

    def look():
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1920, "height": 1040})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(2500)
            r = pg.evaluate(r"""() => {
              const q = s => Array.from(document.querySelectorAll(s));
              const nums = q('#ovSamples text.ov-num');
              const bgs = q('#ovSamples rect.ov-labbg').map(e => e.getBBox());
              const polys = q('#ovSamples polygon, #ovSamples circle.pred').map(e => e.getBBox());
              const ov = (a, b) => Math.min(a.x + a.width, b.x + b.width) - Math.max(a.x, b.x) > 0.5 &&
                                   Math.min(a.y + a.height, b.y + b.height) - Math.max(a.y, b.y) > 0.5;
              let lab_lab = 0, lab_chip = 0;
              bgs.forEach((a, i) => { bgs.forEach((b, j) => { if (j > i && ov(a, b)) lab_lab++; }); polys.forEach(p => { if (ov(a, p)) lab_chip++; }); });
              const hasCoord = nums.some(t => /[+-]?\d+\.\d+\/[+-]?\d+\.\d+/.test(t.textContent));
              return {nums: nums.length, bgs: bgs.length, lab_lab, lab_chip, hasCoord,
                      leads: q('#ovSamples line.ov-lead').length, big: q('.ov-big, #ovSamples text.big').length};
            }""")
            b.close()
            return r, errs
    r, errs = await asyncio.get_running_loop().run_in_executor(None, look)
    n = len(c.state["samples"])
    check(r["nums"] == n and r["bgs"] == n and r["leads"] == n and not r["hasCoord"] and r["big"] == 0,
          "라벨 한 벌 · 좌표 숫자 없음 · 지시선 (%s)" % r)
    check(r["lab_lab"] == 0 and r["lab_chip"] == 0 and not errs, "라벨이 라벨 · 윤곽과 겹치지 않음 (라벨끼리 %d · 윤곽과 %d · 오류 %s)" % (r["lab_lab"], r["lab_chip"], errs))


# ---------------------------------------------------------------------------
# 측정 과정(쿨러 · 접촉 · 온도 · 가열 · 기록) - 더미 열 모델, 시간 배율 sim.time_scale
# ---------------------------------------------------------------------------
def _steps_jsonl(c):
    p = os.path.join(c.state["sequence"]["out_dir"], "steps.jsonl")
    out = []
    with open(p, encoding="utf-8") as f:
        for ln in f:
            if ln.strip():
                out.append(json.loads(ln))
    return out


async def _set_recipe(c, steps, **extra):
    vals = {"recipe": {"items": [{"name": "T", "steps": [{"type": "move_sample"}] + steps}], "active": "T"}}
    vals.update(extra)
    c.acks.clear()
    await c.send(cmd="settings_save", values=vals)
    a = await wait_ack(c, "settings_save")
    await c.pump(0.4)
    check(bool(a) and a["ok"], "레시피 저장 (%s)" % ((a or {}).get("errors")))


async def _run_only(c, nos, timeout=90, confirm=True):
    c.acks.clear()
    await c.send(cmd="run", mode="pick", only=nos, confirm=confirm)
    a = await wait_ack(c, "run")
    return a


def _relay_on(c, n):
    return bool(c.state["relays"][n - 1]["on"])


async def _wait_state(c, pred, timeout=60.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        await c.pump(0.2)
        if pred(c.state):
            return True
    return False


async def process_flow(c):
    """기본 레시피 한 샘플 전 과정 · 시간 초과 정책 · 과열 · 센서 오류 · 정지/비상정지 · 이동 불변식 · 쿨러 구역."""
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    sm = c.state["samples"]
    check(ph == "ready" and len(sm) == 9, "촬영 · 샘플 9 (%s %d)" % (ph, len(sm)))
    # --- 쿨러 구역 배정: 3x3 칸에 샘플 위치별로 R1~R9(사진 기준 행 우선) ---
    cools = [s["cooler"] for s in sm]
    check(sorted(cools) == list(range(1, 10)), "샘플마다 다른 구역 R1~R9 (%s)" % cools)
    first = min(sm, key=lambda s: s["v"] + s["u"])
    last = max(sm, key=lambda s: s["v"] + s["u"])
    check(first["cooler"] == 1 and last["cooler"] == 9, "왼쪽 위 R1 · 오른쪽 아래 R9 (%s · %s)" % (first["cooler"], last["cooler"]))
    s_mid = sorted(sm, key=lambda s: (round(s["v"] / 60), s["u"]))
    check([s["cooler"] for s in s_mid] == list(range(1, 10)), "행 우선 번호 (%s)" % [s["cooler"] for s in s_mid])
    # 수동 변경
    tgt = sm[0]["no"]
    await c.send(cmd="sample_cooler", no=tgt, n=7)
    await c.pump(0.4)
    s0 = next(s for s in c.state["samples"] if s["no"] == tgt)
    check(s0["cooler"] == 7 and s0["cooler_manual"], "샘플별 수동 쿨러 변경")
    await c.send(cmd="sample_cooler", no=tgt, n=0)
    await c.pump(0.4)
    s0 = next(s for s in c.state["samples"] if s["no"] == tgt)
    check(s0["cooler"] == cools[0] and not s0["cooler_manual"], "자동으로 되돌림")

    # --- 기본(빠른) 레시피 한 샘플: 접촉 탐색 → 냉각 대기 → 가열 → 냉각 → 기록 ---
    c.acks.clear()
    c.logs.clear()
    a = await _run_only(c, [tgt])
    check(bool(a) and a["ok"], "순회 시작 (%s)" % (a or {}).get("needs_confirm"))
    ok = await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error", "stopped"), 120)
    st = c.state
    check(ok and st["sequence"]["phase"] == "done", "순회 완료 (%s · %s)" % (st["sequence"]["phase"], st["sequence"]["message"]))
    recs = [r for r in _steps_jsonl(c) if r.get("sample_no") == tgt and "type" in r]
    types = [r["type"] for r in recs]
    check(types == ["move_sample", "cooler", "contact", "log", "wait_temp", "heat", "cool", "log", "z"],
          "단계 순서 (%s)" % types)
    check(all(r["result"] == "ok" for r in recs), "단계 결과 전부 ok")
    det = {r["type"]: r["detail"] for r in recs}
    cz = (det.get("contact") or {}).get("contact_z")
    check((det["contact"] or {}).get("found") and cz is not None and 11.5 <= cz <= 12.6, "접촉 탐색 · 접촉 Z %s" % cz)
    check(det["wait_temp"].get("timed_out") is False and det["heat"]["end"] in ("temp", "time")
          and det["cool"].get("timed_out") is False, "온도 대기 · 가열 · 냉각 결과 (%s)" % det["heat"])
    csvp = os.path.join(st["sequence"]["out_dir"], "sample_%02d_log.csv" % tgt)
    check(os.path.exists(csvp), "기록 CSV 파일")
    import csv as _csv
    with open(csvp, encoding="utf-8-sig", newline="") as f:
        rows = list(_csv.reader(f))
    check(rows[0] == ["시각", "경과_s", "단계", "온도_C", "히트건", "쿨러", "Z_mm", "접촉", "측정값"] and len(rows) > 6,
          "CSV 머리 · 행 %d" % len(rows))
    temps = [float(r[3]) for r in rows[1:] if r[3]]
    check(min(temps) < 31 and max(temps) > 70 and any(r[4] == "1" for r in rows[1:]), "온도 %.1f~%.1f · 히트건 켜진 행 있음" % (min(temps), max(temps)))
    check(not _relay_on(c, 10), "끝난 뒤 히트건 꺼짐")
    check(not _relay_on(c, 10) and not any(_relay_on(c, n) for n in range(1, 10)),
          "순회 종료: 릴레이 정책(끄기)에 따라 쿨러도 꺼짐")

    # --- 쿨러 모드 · 끝나도 켜 둠 ---
    await _set_recipe(c, [{"type": "cooler", "action": "on", "target": "sample"}, {"type": "z", "target": "top"}],
                      cooler={"mode": "all", "keep_on": False})
    await c.send(cmd="relay_all_off")
    await c.pump(0.5)
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "done" and not any(_relay_on(c, n) for n in range(1, 10)),
          "mode all + keep_on 끔: 샘플 끝나면 쿨러 전부 꺼짐")
    await _set_recipe(c, [{"type": "cooler", "action": "on", "target": "sample"}, {"type": "z", "target": "top"}],
                      cooler={"mode": "all", "keep_on": True})
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    # policy(stop 때 끄기)이 순회 종료에 적용돼 꺼진다 - 순회 도중에는 9개 전부 켜졌었다
    on_log = [l for l in c.logs if "켜짐" in l["msg"] and l["msg"].startswith("쿨러") or ("켜짐" in l["msg"] and "R" in l["msg"])]
    check(c.state["sequence"]["phase"] == "done", "mode all 순회 완료")
    await _set_recipe(c, [{"type": "z", "target": "top"}], cooler={"mode": "sample", "keep_on": True})

    # --- 시간 초과 정책(온도 대기) ---
    wt = lambda pol: [{"type": "cooler", "action": "on", "target": "sample"},          # noqa: E731
                      {"type": "wait_temp", "cond": "<=", "target_c": -40, "hold_s": 0, "max_s": 3, "on_timeout": pol},
                      {"type": "z", "target": "top"}]
    await _set_recipe(c, wt("stop"))
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "error" and "시간 초과" in c.state["sequence"]["message"],
          "시간 초과 → 정지 (%s)" % c.state["sequence"]["message"])
    await _set_recipe(c, wt("skip"))
    await _run_only(c, [sm[0]["no"], sm[1]["no"]])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    sts = [next(s for s in c.state["samples"] if s["no"] == n)["status"] for n in (sm[0]["no"], sm[1]["no"])]
    check(c.state["sequence"]["phase"] == "done" and sts == ["skip", "skip"] and c.state["sequence"]["done"] == 0,
          "시간 초과 → 이 샘플 건너뜀 · 순회는 계속 (%s)" % sts)
    await _set_recipe(c, wt("continue"))
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "done" and c.state["sequence"]["done"] == 1, "시간 초과 → 계속")
    await _set_recipe(c, wt("confirm"))
    await _run_only(c, [tgt])
    okc = await _wait_state(c, lambda st: st["sequence"]["phase"] == "waiting_confirm", 60)
    check(okc, "시간 초과 → 확인 요청(대기)")
    await c.send(cmd="next")
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "done", "확인 뒤 계속")

    # --- 접촉 못 찾으면 ---
    ct = lambda pol: [{"type": "contact", "start_ref": "mm", "start_mm": 5, "step_mm": 1, "max_ref": "mm", "max_mm": 7,
                       "press_mm": 0, "settle_s": 0.1, "on_fail": pol}, {"type": "z", "target": "top"}]
    await _set_recipe(c, ct("stop"))
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "error" and "접촉 못 찾음" in c.state["sequence"]["message"],
          "접촉 못 찾음 → 정지 (%s)" % c.state["sequence"]["message"])
    await _set_recipe(c, ct("skip"))
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error"), 60)
    check(c.state["sequence"]["phase"] == "done" and next(s for s in c.state["samples"] if s["no"] == tgt)["status"] == "skip",
          "접촉 못 찾음 → 이 샘플 건너뜀")

    # --- 과열: 히트건이 켜진 채 max_c 를 넘으면 즉시 끄고 순회 중단 ---
    HEAT_LONG = [{"type": "heat", "end": "temp", "target_c": 280, "time_s": 10, "max_s": 100}, {"type": "z", "target": "top"}]
    await _set_recipe(c, HEAT_LONG, heater={"max_c": 100.0, "relay": 10, "sensor_fail_n": 3})
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error", "stopped"), 60)
    st = c.state
    check(st["sequence"]["phase"] == "error" and "과열" in st["sequence"]["message"] and not _relay_on(c, 10),
          "과열 보호: 히트건 끄고 중단 (%s)" % st["sequence"]["message"])
    check(any(a["src"] == "히트건" for a in st["alarms"]), "과열 경보 배너")
    # --- 센서 오류 연속 → 즉시 끔 ---
    await _set_recipe(c, [{"type": "heat", "end": "time", "time_s": 100, "max_s": 100}, {"type": "z", "target": "top"}],
                      heater={"max_c": 300.0, "relay": 10, "sensor_fail_n": 3})
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: _relay_on(c, 10), 30)
    await c.send(cmd="dry_temp", c=None, fail=True)
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("done", "error", "stopped"), 30)
    check(c.state["sequence"]["phase"] == "error" and "센서 오류" in c.state["sequence"]["message"] and not _relay_on(c, 10),
          "센서 오류 연속: 히트건 끄고 중단 (%s)" % c.state["sequence"]["message"])
    await c.send(cmd="dry_temp", c=None, fail=False)
    # --- 불변식: X·Y 이동 전에는 히트건이 꺼져 있다(엔진이 강제) ---
    c.logs.clear()
    await c.send(cmd="relay_set", n=10, on=True)
    await c.pump(0.4)
    check(_relay_on(c, 10), "(준비) 히트건 수동으로 켬")
    await c.send(cmd="goto", x=50, y=50)
    await c.pump(1.5)
    sent = _sent(c)
    i_off = next((i for i, m in enumerate(sent) if m == "r 10 0"), None)
    i_mv = next((i for i, m in enumerate(sent) if m.split(" ")[0] in _MOVES), None)
    check(i_off is not None and (i_mv is None or i_off < i_mv) and not _relay_on(c, 10),
          "이동 명령 전에 'r 10 0' (끄기 #%s · 이동 #%s)" % (i_off, i_mv))

    # --- 정지 · 비상정지 중 가열 단계: 정책이 keep 이어도 히트건은 끈다 ---
    await _set_recipe(c, [{"type": "heat", "end": "time", "time_s": 100, "max_s": 100}, {"type": "z", "target": "top"}],
                      relay={"on_stop": "keep", "on_estop": "keep", "on_error": "keep"})
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: _relay_on(c, 10), 30)
    check(_relay_on(c, 10), "가열 중 히트건 켜짐")
    await c.send(cmd="stop")
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("stopped", "done", "error"), 30)
    await c.pump(0.5)
    check(not _relay_on(c, 10), "정지(정책 keep)여도 히트건 꺼짐 (phase %s)" % c.state["sequence"]["phase"])
    await _run_only(c, [tgt])
    await _wait_state(c, lambda st: _relay_on(c, 10), 30)
    await c.send(cmd="estop")
    await _wait_state(c, lambda st: st["sequence"]["phase"] in ("stopped", "done", "error"), 30)
    await c.pump(1.0)
    check(not _relay_on(c, 10), "비상정지(정책 keep)여도 히트건 꺼짐")


# ---------------------------------------------------------------------------
# 샘플로 맞추는 판 보정: 근사 → 1점 오프셋 → 2점 닮음 → 4점 호모그래피 · 이상점 · 카메라 이동 의심 · 이력
# ---------------------------------------------------------------------------
PIN_OFF = (7.0, -3.0)           # 알려진 핀 오프셋(스테이지 = 판 장면 mm + 이 값)


def prep_sample_calib():
    """합성 판 장면 + 감지 영역만 있고 축 방향 · 근사 변환이 들어 있는 plate_calib.json(보정점 0)."""
    import numpy as np
    from core import plate as plate_mod
    path = prep_fixed(1280, 960, with_h=False)
    sc = _synth()
    H = np.asarray(FIXED["truth"]["H"], np.float64)
    d = plate_mod.load(os.path.join(DATA_DIR, "plate_calib.json"))
    size = (156.0, 156.0)                                        # 일부러 2.5% 틀린 실제 크기(근사의 배율 오차)
    Hp = plate_mod.plate_homography(d["area"], size)

    def p_at(x, y):
        return np.array(plate_mod.px_to_mm(Hp, *sc.project(H, x, y)))
    Q = plate_mod.axes_matrix(p_at(121, 120) - p_at(120, 120), p_at(120, 121) - p_at(120, 120))
    d["approx_axes"] = {"Q": Q.tolist(), "source": "head", "time": "test"}
    d["approx_t"] = [float(v) for v in (np.array([247.6, 247.8]) / 2.0 - Q.T @ np.array([78.0, 78.0]))]
    plate_mod.solve_record(d, size)
    plate_mod.save(d, os.path.join(DATA_DIR, "plate_calib.json"))
    return path


def _truth_stage(c):
    """샘플 번호 → 참 스테이지 XY(판 장면 mm + 핀 오프셋)."""
    sc = _synth()
    H = FIXED["truth"]["H"]
    out = {}
    for s in c.state["samples"]:
        best = min(FIXED["truth"]["samples"], key=lambda t: (sc.project(H, *t)[0] - s["u"]) ** 2 + (sc.project(H, *t)[1] - s["v"]) ** 2)
        out[s["no"]] = (best[0] + PIN_OFF[0], best[1] + PIN_OFF[1])
    return out


def _max_err(c, tr):
    return max(((s["X"] - tr[s["no"]][0]) ** 2 + (s["Y"] - tr[s["no"]][1]) ** 2) ** 0.5 for s in c.state["samples"])


async def _record_at(c, no, tr, dx=0.0, dy=0.0, confirm=None):
    """사람 역할: 핀을 그 샘플의 참 자리(+dx, dy 어긋남)에 맞춘 뒤 [이 자리로 보정]."""
    await c.send(cmd="goto", x=tr[no][0] + dx, y=tr[no][1] + dy)
    await c.pump(1.2)
    c.acks.clear()
    msg = dict(cmd="plate_sample_calib", no=no)
    if confirm:
        msg["confirm"] = confirm
    await c.send(**msg)
    a = await wait_ack(c, "plate_sample_calib")
    await c.pump(0.5)
    return a


async def sample_calib_flow(c):
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    sm = c.state["samples"]
    check(ph == "ready" and len(sm) == 9, "촬영 · 샘플 9 (%s %d)" % (ph, len(sm)))
    pc = c.state["plate"]["calib"]
    check(pc["state"] == "approx" and pc["n"] == 0 and pc["has_axes"], "근사 상태 · 보정점 0 (%s)" % pc["state"])
    tr = _truth_stage(c)
    err0 = _max_err(c, tr)
    check(err0 > 5.0, "근사 상태 오차가 크다(위치 기준 미정) %.1f mm" % err0)
    # 근사 상태: 순회 금지 · 샘플 이동은 확인 후
    c.acks.clear()
    await c.send(cmd="run", mode="auto", confirm=True)
    a = await wait_ack(c, "run")
    check(bool(a) and not a["ok"] and any("근사" in r for r in a.get("needs_confirm", [])), "근사 상태 순회 금지 (%s)" % (a or {}).get("needs_confirm"))
    c.acks.clear()
    await c.send(cmd="goto", no=1)
    a = await wait_ack(c, "goto")
    check(bool(a) and not a["ok"] and a["reason"] == "needs_confirm", "근사 상태 샘플 이동은 확인을 받음")
    await c.send(cmd="goto", no=1, confirm=True)
    await c.pump(1.5)
    check(c.state["stage"]["z_mm"] in (0, 0.0, None), "확인 뒤 이동 · Z 맨 위")
    # 활성 공구가 핀이 아니면 거절
    await c.send(cmd="tool_select", name="히트건")
    await wait_ack(c, "tool_select")
    c.acks.clear()
    await c.send(cmd="plate_sample_calib", no=1)
    a = await wait_ack(c, "plate_sample_calib")
    check(bool(a) and not a["ok"] and any("핀" in r for r in a.get("needs_confirm", [])), "활성 공구가 핀이 아니면 거절 (%s)" % (a or {}).get("needs_confirm"))
    await c.send(cmd="tool_select", name="기준 포인터")
    await wait_ack(c, "tool_select")
    # 1점(대각선 한 귀퉁이): 오프셋 제거
    order = sorted(sm, key=lambda s: s["u"] + s["v"])
    byd = sorted(sm, key=lambda s: s["u"] - s["v"])
    nA, nB, nC, nD = order[0]["no"], order[-1]["no"], byd[0]["no"], byd[-1]["no"]       # 귀퉁이 네 곳
    a = await _record_at(c, nA, tr)
    check(bool(a) and a["ok"] and a["model"] == "offset", "1점 → 오프셋 맞춤 (%s)" % (a or {}).get("model"))
    err1 = _max_err(c, tr)
    check(err1 < err0 / 2.0, "1점 뒤 오차 %.1f → %.1f mm" % (err0, err1))
    # 2점(대각선): 닮음 변환
    a = await _record_at(c, nB, tr)
    err2 = _max_err(c, tr)
    check(bool(a) and a["model"] == "similarity" and err2 < 1.0, "대각선 2점 뒤 전체 오차 %.2f mm < 1 (%s)" % (err2, (a or {}).get("model")))
    # 4점: 전체 호모그래피
    await _record_at(c, nC, tr)
    a = await _record_at(c, nD, tr)
    err4 = _max_err(c, tr)
    check(bool(a) and a["model"] == "homography" and err4 < 0.5, "4점 뒤 오차 %.2f mm < 0.5 (%s)" % (err4, (a or {}).get("model")))
    check(c.state["plate"]["calib"]["state"] == "calibrated" and c.state["plate"]["calib"]["n"] == 4
          and all(p.get("src") == "sample" for p in c.state["plate"]["data"]["points"]), "보정점 4 · 출처 sample")
    # 이상점 의심: 다섯째 점을 6 mm 어긋난 자리에서 → 확인 필요
    nE = sm[4]["no"] if sm[4]["no"] not in (nA, nB, nC, nD) else order[len(order) // 2]["no"]
    a = await _record_at(c, nE, tr, dx=6.0)
    check(bool(a) and not a["ok"] and a["reason"] == "outlier" and a["res_mm"] > 4, "이상점 의심 확인 (%s · %.1f mm)" % ((a or {}).get("reason"), (a or {}).get("res_mm") or 0))
    a = await _record_at(c, nE, tr, dx=6.0, confirm=True)
    check(bool(a) and a["ok"], "확인하면 기록")
    # 연속 두 번째 큰 어긋남 → 카메라 이동 의심(경보) · 기존 점 무효화 확인
    others = [x["no"] for x in sm if x["no"] not in (nA, nB, nC, nD, nE)]
    nF = others[0]
    a = await _record_at(c, nF, tr, dy=-7.0)
    check(bool(a) and not a["ok"] and a["reason"] in ("camera_move", "outlier"), "연속 큰 어긋남 (%s)" % (a or {}).get("reason"))
    if a and a["reason"] == "outlier":
        a = await _record_at(c, nF, tr, dy=-7.0, confirm=True)
        a = await _record_at(c, others[1], tr, dx=-6.0)
    check(bool(a) and a["reason"] == "camera_move", "카메라 이동 의심 (%s)" % (a or {}).get("reason"))
    await c.pump(0.4)
    check(any(x["src"] == "카메라 이동" for x in c.state["alarms"]), "카메라 이동 의심 경보 배너")
    n_before = c.state["plate"]["calib"]["n"]
    a = await _record_at(c, others[1], tr, dx=-6.0, confirm="invalidate")
    await c.pump(0.4)
    check(bool(a) and a["ok"] and c.state["plate"]["calib"]["n"] == 1 and n_before > 1, "무효화 → 이 점부터 다시 (점 %d → %d)" % (n_before, c.state["plate"]["calib"]["n"]))
    # 이력에서 되돌리기: 4점 상태로
    hist = c.state["plate"]["calib"]["history"]
    h4 = next((h for h in hist if h["n_points"] == 4 and h["model"] == "homography"), None)
    check(h4 is not None and len(hist) <= 20, "이력 보관(최대 20) · 4점 상태 있음 (%d개)" % len(hist))
    c.acks.clear()
    await c.send(cmd="plate_hist_restore", name=h4["name"])
    a = await wait_ack(c, "plate_hist_restore")
    await c.pump(0.5)
    check(bool(a) and a["ok"] and c.state["plate"]["calib"]["n"] == 4 and _max_err(c, tr) < 0.5, "이력 되돌리기 → 4점 · 오차 %.2f mm" % _max_err(c, tr))
    # 점 삭제
    await c.send(cmd="plate_point_delete", i=3)
    a = await wait_ack(c, "plate_point_delete")
    await c.pump(0.4)
    check(bool(a) and a["ok"] and c.state["plate"]["calib"]["n"] == 3, "보정점 개별 삭제 (%d)" % c.state["plate"]["calib"]["n"])


async def _probe_axes(c, rot):
    """헤드 보정이 없는 상태에서 [근사 변환 만들기] → X·Y 를 조금 움직여 축 방향을 자동 측정한다."""
    await c.send(cmd="goto", x=100, y=100)             # 헤드 마커가 시야 가운데에 오는 자리
    await c.pump(1.5)
    c.acks.clear()
    await c.send(cmd="plate_approx_make")
    a = await wait_ack(c, "plate_approx_make")
    check(bool(a) and not a["ok"] and a["reason"] == "needs_confirm", "자동 측정은 이동 전 확인을 받음 (%s)" % (a or {}).get("reason"))
    c.acks.clear()
    await c.send(cmd="plate_approx_make", source="auto", confirm=True)
    a = await wait_ack(c, "plate_approx_make", timeout=60)
    await c.pump(0.5)
    check(bool(a) and a["ok"] and a["source"] == "auto", "축 방향 자동 측정 (%s)" % (a or {}))
    import numpy as np
    from core import plate as plate_mod
    sc = _synth()
    d = plate_mod.load(os.path.join(DATA_DIR, "plate_calib.json"))
    w, h = FIXED["probe_size"]
    Hh = np.asarray(sc.true_homography(h if rot else w, w if rot else h), np.float64)    # 마커 평면 mm → px (회전 전 캔버스)
    if rot:
        Hh = np.array([[0, -1, w - 1], [1, 0, 0], [0, 0, 1.0]]) @ Hh
    Hp = plate_mod.plate_homography(d["area"], (160.0, 160.0))

    def pa(x, y):
        return np.array(plate_mod.px_to_mm(Hp, *sc.project(Hh, x, y)))
    Qt = plate_mod.axes_matrix(pa(101, 100) - pa(100, 100), pa(100, 101) - pa(100, 100))      # 참 축 방향
    Qm = np.asarray(d["approx_axes"]["Q"], np.float64)
    ang = [math.degrees(math.acos(max(-1.0, min(1.0, float(Qt[:, k] @ Qm[:, k]))))) for k in (0, 1)]
    check(max(ang) < 3.0 and d["approx_axes"]["source"] == "auto",
          "자동 측정한 축 방향이 참과 같다(어긋남 %.2f° · %.2f° · rot=%s)" % (ang[0], ang[1], rot))
    # 변환 전체: 오프셋만 다르다(5곳의 차이 벡터 표준편차 - 합성 카메라가 비등방이라 90° 장면은 느슨하게)
    Hi = np.linalg.inv(Hh)
    diffs = []
    for u, v in [(300, 200), (900, 200), (300, 600), (900, 600), (640, 360)]:
        X, Y = plate_mod.px_to_mm(d["H"], u, v)
        x, y = plate_mod.px_to_mm(Hi, u, v)
        diffs.append((X - x, Y - y))
    sd = float(np.std(np.array(diffs), axis=0).max())
    check(sd < (0.6 if not rot else 4.0), "근사 변환은 참과 오프셋만 다름(표준편차 %.2f mm)" % sd)


def axes_flow(rot):
    async def flow(c):
        await _probe_axes(c, rot)
    return flow


def prep_axes(rot):
    """헤드 마커 합성 카메라 + 감지 영역만 있는 plate_calib.json(헤드 보정은 두지 않는다)."""
    import numpy as np
    from core import plate as plate_mod
    sc = _synth()
    w, h = 1280, 720
    Hh = np.asarray(sc.true_homography(h if rot else w, w if rot else h), np.float64)
    if rot:
        Hh = np.array([[0, -1, w - 1], [1, 0, 0], [0, 0, 1.0]]) @ Hh
    quad = [list(sc.project(Hh, a, b)) for a, b in [(40, 40), (40, 200), (200, 200), (200, 40)]]
    d = plate_mod.new_record(1, (w, h))
    q, _why = plate_mod.check_quad(quad, (w, h))
    d["area"] = q
    plate_mod.save(d, os.path.join(DATA_DIR, "plate_calib.json"))
    FIXED["probe_size"] = (w, h)
    return None


def _px_dir_deg(H, x=1.0, y=0.0):
    """스테이지 방향(x, y) 이 사진에서 가리키는 각도(도) - 참 호모그래피 H(mm→px) 의 미분."""
    sc = _synth()
    p0, p1 = sc.project(H, 60.0, 60.0), sc.project(H, 60.0 + x, 60.0 + y)
    return math.degrees(math.atan2(p1[1] - p0[1], p1[0] - p0[0]))


def prep_fixed_rot(rot):
    """합성 판 장면(+판 보정). rot=True 면 카메라를 90° 돌린 것처럼 사진 · 호모그래피를 함께 돌린다."""
    import cv2
    import numpy as np
    from core import plate as plate_mod
    sc = _synth()
    w, h = 1280, 960
    img, truth = sc.render_plate(w, h)
    H = np.asarray(truth["H"], np.float64)
    size = (w, h)
    if rot:
        img = cv2.rotate(img, cv2.ROTATE_90_CLOCKWISE)
        T = np.array([[0, -1, h - 1], [1, 0, 0], [0, 0, 1.0]])
        H = T @ H
        size = (h, w)
    path = os.path.join(DATA_DIR, "scene_rot%d.png" % int(rot))
    cv2.imwrite(path, img)
    quad = [list(sc.project(H, a, b)) for a, b in sc.PLATE_QUAD_MM]
    pts = []
    for x, y in sc.PLATE_CAL_MM:
        u, v = sc.project(H, x, y)
        pts.append({"x": x, "y": y, "u": round(u, 2), "v": round(v, 2)})
    f = plate_mod.fit(pts)
    for p, r in zip(pts, f["res"]):
        p["res_mm"] = round(r, 4)
    d = plate_mod.new_record(1, size)
    q, _w = plate_mod.check_quad(quad, size)
    d.update({"area": q, "points": pts, "H": f["H"], "rms_mm": round(f["rms"], 4), "max_mm": round(f["max"], 4),
              "res_limit_mm": 1.0})
    plate_mod.save(d, os.path.join(DATA_DIR, "plate_calib.json"))
    FIXED["H"] = H
    return path


def tip_dir_flow(rot):
    async def flow(c):
        """공구 끝 방향은 스테이지 기준: 카메라를 90° 돌려도 실물 방향이 같다(사진 각도가 90° 달라진다)."""
        H = FIXED["H"]
        want_x = _px_dir_deg(H, 1, 0)                 # 스테이지 X 축이 사진에서 가리키는 각도
        want_y = _px_dir_deg(H, 0, 1)
        await c.send(cmd="capture")
        ph = await c.wait_phase(("ready", "error"), 90)
        check(ph == "ready", "촬영 (%s)" % ph)
        sm = c.state["samples"]
        tip = c.state["tip"]
        # 핀: 스테이지 기준 각도 0(긴 변이 X 축) → 사진 각도 = X 축의 사진 방향
        diff = (sm[0]["tip_deg"] - want_x + 90) % 180 - 90
        check(abs(diff) < 3, "스테이지 0° → 사진 %.1f° (기대 %.1f°, rot=%s)" % (sm[0]["tip_deg"], want_x, rot))
        # 이행: 사진 예비값 90° 만 있던 공구(히트건) 가 판 보정으로 스테이지 각도로 한 번 채워진다
        tools = {t["name"]: t for t in c.state["settings"]["tool"]["items"]}
        ptr = tools["기준 포인터"]
        check(ptr["tip_angle_deg"] == 0, "스테이지 각도를 정해 둔 공구는 그대로 (%s)" % ptr["tip_angle_deg"])
        mig = tools["이행 핀"]
        exp_stage = (_stage_of_img(H, 90.0)) % 180
        got = (mig["tip_angle_deg"] or -999) % 180
        check(mig["tip_angle_deg"] is not None and min(abs(got - exp_stage), 180 - abs(got - exp_stage)) < 3,
              "사진 90° → 스테이지 %.1f° 이행 (%s)" % (exp_stage, mig["tip_angle_deg"]))
        FIXED.setdefault("dirs", []).append(want_x)
        if len(FIXED["dirs"]) == 2:
            d = (FIXED["dirs"][1] - FIXED["dirs"][0] + 180) % 180
            check(abs(d - 90) < 3, "카메라 90° 회전 → 같은 실물 방향의 사진 각도 차 %.1f°" % d)
    return flow


def _stage_of_img(H, img_deg):
    """사진 방향 img_deg 에 해당하는 스테이지 방향(도, 0~180)."""
    import numpy as np
    sc = _synth()
    p0 = np.array(sc.project(H, 60.0, 60.0))
    J = np.array([np.array(sc.project(H, 61.0, 60.0)) - p0, np.array(sc.project(H, 60.0, 61.0)) - p0]).T   # mm → px
    th = math.radians(img_deg)
    v = np.linalg.solve(J, np.array([math.cos(th), math.sin(th)]))
    return math.degrees(math.atan2(v[1], v[0])) % 180


def _nearest(samples, x, y):
    best = min(samples, key=lambda s: (s["X"] - x) ** 2 + (s["Y"] - y) ** 2)
    return best, ((best["X"] - x) ** 2 + (best["Y"] - y) ** 2) ** 0.5


def _timing_line(c):
    for l in c.logs:
        if l["msg"].startswith("소요 "):
            return l["msg"]
    return None


async def fixed_flow(c):
    """고정 보정: 판 마커 없이 촬영 → 감지 영역 안 샘플만 · 판 보정대로의 좌표 · 미리보기 축소 · 수동 추가."""
    import cv2
    import numpy as np
    sc = _synth()
    truth = FIXED["truth"]
    w, h = FIXED["size"]
    st = c.state
    check(st["settings"]["calib"]["mode"] == "fixed", "calib.mode = fixed")
    check(st["plate"]["valid"] and st["plate"]["mode"] == "fixed", "판 보정 유효 (%s)" % st["plate"]["reason"])
    check(not any(a["src"] == "판 보정" for a in st["alarms"]), "유효하면 판 보정 경보 없음")
    # 미리보기: 원본이 아니라 preview_max_w 로 줄여 보낸다
    code, body = http_get(c.port, "/preview.jpg")
    pw = st["settings"]["preview_max_w"]
    im = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR) if code == 200 else None
    check(im is not None and im.shape[1] == pw and abs(im.shape[0] - round(h * pw / w)) <= 1,
          "미리보기 축소 %dx%d → %s" % (w, h, None if im is None else "%dx%d" % (im.shape[1], im.shape[0])))
    # 촬영
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 120)
    check(ph == "ready", "판 마커 없이 촬영 → ready (%s)" % ph)
    st = c.state
    samples = [s for s in st["samples"] if not s.get("manual")]
    check(len(samples) == len(truth["samples"]),
          "감지 영역 안 샘플만 %d개 (%d개)" % (len(truth["samples"]), len(samples)))
    errs = [_nearest(samples, x, y)[1] for x, y in truth["samples"]] if samples else [99]
    check(max(errs) < 0.3, "좌표가 판 보정대로 · 최대 오차 %.3f mm" % max(errs))
    check(all(min(((s["X"] - x) ** 2 + (s["Y"] - y) ** 2) ** 0.5 for x, y in truth["outside"]) > 10
              for s in samples), "감지 영역 밖 물체 무시")
    cal = st["calib"] or {}
    check(cal.get("mode") == "fixed" and cal.get("used_ids") == [] and cal.get("missing_ids") == [],
          "마커 검출 · 부족 판정 안 함 (%s)" % cal)
    check(bool(st["sensing"] and st["sensing"].get("quad")) and len(st["sensing"]["quad"]) == 4,
          "감지 영역 사각형 전달")
    check(not st["markers"], "판 마커가 화면에 있어도 무시")
    check(not any(a["src"] == "판 보정" for a in st["alarms"]), "촬영 뒤 경보 없음")
    check(any("평탄화" in l["msg"] and "고정 보정" in l["msg"] for l in c.logs), "평탄화 건너뜀 로그")
    check(not any("마커" in l["msg"] and "부족" in l["msg"] for l in c.logs), "마커 부족 로그 없음")
    tl = _timing_line(c)
    check(tl is not None and "촬영" in tl and "융합" in tl and "검출" in tl, "단계별 소요 로그 (%s)" % tl)
    print("  [소요] " + str(tl), flush=True)
    check(bool(st["frame"]) and st["frame"]["w"] == w and st["frame"]["h"] == h,
          "촬영본 원본 해상도 %dx%d" % (w, h))
    check(os.path.exists(os.path.join(st["sequence"]["out_dir"], "annotated.jpg")), "annotated.jpg 저장")
    # 수동 추가(촬영본 클릭): 판 보정으로 변환
    H = truth["H"]
    u, v = sc.project(H, 95.0, 95.0)
    c.acks.clear()
    await c.send(cmd="sample_add", u=round(u, 1), v=round(v, 1))
    a = await wait_ack(c, "sample_add")
    await c.pump(0.4)
    man = [s for s in c.state["samples"] if s.get("manual")]
    check(bool(a) and a["ok"] and bool(man) and abs(man[0]["X"] - 95) < 0.2 and abs(man[0]["Y"] - 95) < 0.2,
          "클릭 추가 → 판 보정 좌표 (%s)" % ([man[0]["X"], man[0]["Y"]] if man else a))
    u2, v2 = sc.project(H, 15.0, 100.0)               # 사각형 밖
    await c.send(cmd="sample_add", u=round(u2, 1), v=round(v2, 1))
    a = await wait_ack(c, "sample_add")
    check(bool(a) and not a["ok"] and a["reason"] == "outside",
          "사각형 밖 클릭 추가 거절 (%s)" % (a or {}).get("reason"))
    # 포인터(스테이지 위치)가 판 보정으로 사진 위에 찍힌다
    await c.send(cmd="goto", x=120, y=120)
    await wait_log(c, "이동 완료", 30)
    await c.pump(0.4)
    s2 = c.state["stage"]
    eu, ev = sc.project(H, s2["x_mm"], s2["y_mm"])
    check(s2["u"] is not None and abs(s2["u"] - eu) < 1.0 and abs(s2["v"] - ev) < 1.0,
          "포인터 픽셀 = 판 보정 역변환 (%s,%s ↔ %.1f,%.1f)" % (s2["u"], s2["v"], eu, ev))


async def fixed_calib_flow(c):
    """판 보정 · 감지 영역 명령: 4점 미만 거절 · 잔차 표시 · 적용 · 볼록 아닌 사각형 · 위치 기록 · 무효."""
    sc = _synth()
    truth = FIXED["truth"]
    w, h = FIXED["size"]
    H = truth["H"]

    def pt(x, y, dx=0.0):
        u, v = sc.project(H, x + dx, y)
        return {"x": x, "y": y, "u": round(u, 2), "v": round(v, 2)}
    good = [pt(40, 40), pt(40, 200), pt(200, 200), pt(200, 40), pt(120, 120)]
    c.acks.clear()
    await c.send(cmd="plate_calib_apply", points=good[:3], size=[w, h])
    a = await wait_ack(c, "plate_calib_apply")
    check(bool(a) and not a["ok"] and a["reason"] == "min_points",
          "점 3개 적용 거절 (%s)" % (a or {}).get("reason"))
    # 잔차: 한 점이 5 mm 어긋나면 한계(1.0) 초과로 표시
    bad = good + [pt(120, 60, dx=5.0)]
    await c.send(cmd="plate_calib_fit", points=bad, size=[w, h])
    a = await wait_ack(c, "plate_calib_fit")
    check(bool(a) and a["ok"] and len(a["res"]) == 6 and a["rms_mm"] > 0.3 and len(a["over"]) >= 1,
          "잔차 · RMS 계산 · 한계 초과 점 표시 (RMS %s · 초과 %s)"
          % ((a or {}).get("rms_mm"), (a or {}).get("over")))
    await c.send(cmd="plate_calib_fit", points=good, size=[w, h])
    a = await wait_ack(c, "plate_calib_fit")
    check(bool(a) and a["ok"] and a["rms_mm"] < 0.01 and not a["over"],
          "정상 점 5개 · 잔차 거의 0 (%s)" % (a or {}).get("rms_mm"))
    path = os.path.join(DATA_DIR, "plate_calib.json")
    await c.send(cmd="plate_calib_apply", points=good, size=[w, h])
    a = await wait_ack(c, "plate_calib_apply")
    await c.pump(0.4)
    with open(path, encoding="utf-8") as f:
        d = json.load(f)
    check(bool(a) and a["ok"] and d["size"] == [w, h] and len(d["points"]) == 5 and d["H"] is not None
          and d["camera_index"] == c.state["settings"]["camera_index"],
          "5점 적용 → plate_calib.json (해상도 · 카메라 번호)")
    check(c.state["plate"]["valid"] and c.state["plate"]["data"]["n"] == 5, "state.plate 유효 · 5점")
    # 감지 영역: 프레임 밖 꼭짓점은 거절
    await c.send(cmd="plate_area_apply", quad=[[10, 10], [w + 50, 10], [w - 10, h - 10], [10, h - 10]],
                 size=[w, h])
    a = await wait_ack(c, "plate_area_apply")
    check(bool(a) and not a["ok"] and a["reason"] == "invalid",
          "프레임 밖 꼭짓점 거절 (%s)" % (a or {}).get("reason"))
    # 점이 한 직선 위이면 계산하지 않는다(퇴화)
    line = [pt(40, 40), pt(60, 60), pt(80, 80), pt(100, 100)]
    await c.send(cmd="plate_calib_apply", points=line, size=[w, h])
    a = await wait_ack(c, "plate_calib_apply")
    check(bool(a) and not a["ok"], "한 직선 위의 점은 적용 거절")
    # 위치 기록
    await c.send(cmd="plate_pos")
    a = await wait_ack(c, "plate_pos")
    s = c.state["stage"]
    check(bool(a) and a["ok"] and (a["x"], a["y"]) == (s["x_mm"], s["y_mm"]),
          "[위치 기록] = 스테이지 XY (%s)" % a)
    # 해상도 설정을 바꾸면 무효 + 경보
    await c.send(cmd="settings_save", values={"width": 1024, "height": 768})
    await wait_ack(c, "settings_save")
    await c.pump(0.5)
    check(not c.state["plate"]["valid"] and "해상도" in c.state["plate"]["reason"],
          "해상도 변경 → 판 보정 무효 (%s)" % c.state["plate"]["reason"])
    check(any(a["src"] == "판 보정" and "판 보정 무효 · 다시 보정" in a["text"] for a in c.state["alarms"]),
          "경보 배너 '판 보정 무효 · 다시 보정'")


async def fixed_area_only_flow(c):
    """감지 영역만 있고 판 보정이 없으면: 검출만(좌표 없음) · 이동/순회/추가 거절 · 판 보정 적용 뒤 좌표."""
    sc = _synth()
    truth = FIXED["truth"]
    w, h = FIXED["size"]
    st = c.state
    check(st["plate"]["area_ok"] and not st["plate"]["valid"], "감지 영역만 유효 (%s)" % st["plate"]["reason"])
    check(not any(a["src"] == "판 보정" for a in st["alarms"]), "경보 배너 없음(주의 수준)")
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 120)
    st = c.state
    check(ph == "ready" and len(st["samples"]) == len(truth["samples"]),
          "검출만 · 샘플 %d개 (판 보정 있을 때와 같음 %d) %s %s" % (len(st["samples"]), len(truth["samples"]), ph, st["sequence"]["message"]))
    check(all(s["X"] is None and s["Y"] is None and s["status"] == "nocoord" for s in st["samples"]),
          "mm 좌표 None · 상태 nocoord")
    check(all(s["u"] > 0 and s["verts"] for s in st["samples"]), "픽셀 좌표 · 윤곽은 정상")
    check((st["calib"] or {}).get("area_only") is True, "state.calib.area_only")
    check(any("판 보정 없음" in x for x in st["warnings"]), "주의 안내 한 줄 (%s)" % st["warnings"])
    check(any("감지 영역 크기로 배율" in l["msg"] for l in c.logs), "배율 로그(감지 영역 크기)")
    check(os.path.exists(os.path.join(st["sequence"]["out_dir"], "annotated.jpg")), "촬영본 · annotated 저장")
    # 이동 · 순회 · 추가 거절
    c.logs.clear()
    await c.send(cmd="goto", no=st["samples"][0]["no"])
    await c.pump(1.0)
    check(any("판 보정 필요" in l["msg"] for l in c.logs) and not any(m.split(" ")[0] in _MOVES for m in _sent(c)),
          "선택 샘플 이동 거절(판 보정 필요)")
    c.acks.clear()
    await c.send(cmd="run", mode="auto", confirm=True)
    a = await wait_ack(c, "run")
    check(bool(a) and not a["ok"] and any("판 보정 필요" in r for r in a.get("needs_confirm", [])),
          "순회 시작 거절 (%s)" % (a or {}).get("needs_confirm"))
    # 검출만 모드에서도 추가 · 이동 · 되돌리기는 된다(픽셀 좌표) - mm · 이동 · 순회만 판 보정 뒤
    pt = _free_px(c.state)
    c.acks.clear()
    await c.send(cmd="sample_add", u=pt[0], v=pt[1])
    a = await wait_ack(c, "sample_add")
    await c.pump(0.4)
    s_new = next((s for s in c.state["samples"] if a and s["no"] == a.get("no")), None)
    check(bool(a) and a["ok"] and bool(s_new) and s_new["X"] is None and s_new["status"] == "nocoord"
          and s_new["manual"], "검출만 모드 클릭 추가 · 픽셀 좌표 · nocoord (%s)" % (a or {}).get("reason"))
    u_old, v_old = s_new["u"], s_new["v"]
    await c.send(cmd="sample_move", no=s_new["no"], u=u_old + 30, v=v_old)
    a = await wait_ack(c, "sample_move")
    await c.pump(0.4)
    s_mv = next(s for s in c.state["samples"] if s["no"] == s_new["no"])
    check(bool(a) and a["ok"] and abs(s_mv["u"] - (u_old + 30)) < 0.2 and s_mv["source"] == "manual"
          and s_mv["X"] is None, "샘플 이동 · source manual · 좌표 없음 (%s)" % (a or {}).get("reason"))
    await c.send(cmd="sample_undo")
    await c.pump(0.6)
    s_bk = next(s for s in c.state["samples"] if s["no"] == s_new["no"])
    check(abs(s_bk["u"] - u_old) < 0.2, "이동 되돌리기 (%.1f → %.1f)" % (s_mv["u"], s_bk["u"]))
    await c.send(cmd="sample_move", no=s_new["no"], u=u_old + 30, v=v_old)
    await wait_ack(c, "sample_move")
    await c.pump(0.3)
    # 판 보정 적용 → 다음 촬영부터 좌표
    H = truth["H"]
    pts = []
    for x, y in sc.PLATE_CAL_MM:
        u, v = sc.project(H, x, y)
        pts.append({"x": x, "y": y, "u": round(u, 2), "v": round(v, 2)})
    await c.send(cmd="plate_calib_apply", points=pts, size=[w, h])
    a = await wait_ack(c, "plate_calib_apply")
    check(bool(a) and a["ok"], "판 보정 적용")
    await c.pump(0.5)
    s_re = next((s for s in c.state["samples"] if s["no"] == s_new["no"]), None)
    check(bool(s_re) and s_re["X"] is not None and s_re["status"] in ("wait", "skip")
          and not c.state["calib"]["area_only"],
          "판 보정 적용 뒤 편집한 샘플의 mm 계산 (%s)" % ((s_re or {}).get("X"),))
    await c.send(cmd="capture", confirm=True)
    ph = await c.wait_phase(("ready", "error"), 120)
    samples = c.state["samples"]
    errs = [_nearest(samples, x, y)[1] for x, y in truth["samples"]] if samples and samples[0]["X"] is not None else [99]
    check(ph == "ready" and len(samples) == 9 and max(errs) < 0.3 and not c.state["calib"]["area_only"],
          "적용 뒤 촬영 · 좌표 생김 (최대 오차 %.3f)" % max(errs))
    # 맵: fixed 에서는 판 마커 자리를 그리지 않고 감지 영역 사각형을 점선으로
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return
    def look():
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1920, "height": 1040})
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(1500)
            r = (pg.locator("#map .m-marker").count(), pg.locator("#map polygon.m-rect").count())
            b.close()
            return r
    nm, nq = await asyncio.get_running_loop().run_in_executor(None, look)
    check(nm == 0 and nq == 1, "맵: 판 마커 미표시(%d) · 감지 영역 점선 %d" % (nm, nq))


async def fixed_nocal_flow(c):
    """감지 영역도 없으면: 검출은 건너뛰되 촬영본은 data/out/<시각>_nocal/ 에 저장하고 화면에 띄운다."""
    st = c.state
    check(not st["plate"]["area_ok"], "감지 영역 없음")
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    st = c.state
    check(ph == "error" and not st["samples"], "검출 건너뜀 (phase %s)" % ph)
    check("감지 영역 없음 · 보정 → 감지 영역 지정" in st["sequence"]["message"], "상태 문구 (%s)" % st["sequence"]["message"])
    check(bool(st["frame"]), "촬영본을 화면에 띄움(state.frame)")
    out = os.path.join(DATA_DIR, "out")
    nd = [n for n in os.listdir(out) if n.endswith("_nocal")]
    check(bool(nd) and os.path.exists(os.path.join(out, nd[-1], "raw.png")), "nocal 폴더에 raw.png (%s)" % nd)
    check(any("촬영본 저장" in l["msg"] for l in c.logs), "로그 한 줄")


async def fixed_invalid_flow(c):
    """저장된 판 보정의 해상도가 촬영과 다르면: 촬영은 하되 좌표를 만들지 않는다."""
    st = c.state
    check(not st["plate"]["valid"], "시작부터 무효 (%s)" % st["plate"]["reason"])
    check(any(a["src"] == "판 보정" and a["text"] == "판 보정 무효 · 다시 보정" for a in st["alarms"]),
          "무효 경보 배너")
    c.logs.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    st = c.state
    check(ph == "error" and not st["samples"], "좌표를 만들지 않음 (phase %s · 샘플 %d)" % (ph, len(st["samples"])))
    check(bool(st["frame"]), "촬영본은 남는다")
    check(any(a["src"] == "판 보정" for a in st["alarms"]), "경보 유지")
    check(any("판 보정 무효" in l["msg"] for l in c.logs), "로그 '판 보정 무효'")
    check("판 보정 무효" in st["sequence"]["message"], "상태 문구 (%s)" % st["sequence"]["message"])


async def capture_progress_flow(c):
    """촬영 진행 문구(서버가 단계별로 푸시) · 고정 노출 기본 · 소요 세분화 · 실패 한 줄 · 노출 다시 찾기."""
    s = c.state["settings"]
    check(s["bracket"] is False and s["capture_frames"] == 3, "기본: 브라케팅 끔 · 프레임 3 (%s · %s)" % (s["bracket"], s["capture_frames"]))
    check(abs(s["calib"]["est_mm_per_px"] - 0.143) < 1e-9, "대략 배율 기본 0.143")
    c.logs.clear()
    c.cap_texts.clear()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    check(ph == "ready", "촬영 완료 (%s)" % ph)
    check("검출 중" in c.cap_texts and any(t in ("저장 중",) for t in c.cap_texts),
          "진행 문구 순서 (%s)" % c.cap_texts)
    check(not c.state["camera"]["capturing"] and not c.state["camera"].get("cap_text"), "완료 뒤 막 조건 해제")
    tl = _timing_line(c)
    check(tl is not None and all(k in tl for k in ("노출", "안정 대기", "프레임", "융합", "검출", "헤드 마커", "저장")),
          "소요 세분화 로그 (%s)" % tl)
    check(any("검출 배율" in l["msg"] and "판 보정값" in l["msg"] for l in c.logs), "배율 로그(판 보정값)")
    c.logs.clear()
    await c.send(cmd="exposure_refind")
    await c.pump(0.6)
    check(any("노출" in l["msg"] for l in c.logs), "노출 다시 찾기 명령")
    # 실패 한 줄: 감지 영역 파일을 지워 무효로 만든 촬영 대신, 해상도 불일치는 별도 케이스가 본다.
    check(c.cap_errors == [], "정상 촬영은 실패 한 줄 없음")



async def tip_flow(c):
    """공구 끝 모양: state.tip · 샘플 mmpx/fit · 핀 벗어남 확인 · 공구 바꾸면 재검사 · 스냅 · 끌기 · 되돌리기 · 화면."""
    sc = _synth()
    st = c.state
    check(st["tip"]["shape"] == "circle" and st["tip"]["w"] == 40 and abs(st["tip"]["margin_mm"] - 0.3) < 1e-9,
          "state.tip (%s)" % st["tip"])
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 90)
    sm = c.state["samples"]
    check(ph == "ready" and len(sm) == 9, "촬영 · 샘플 9개 (%s %d)" % (ph, len(sm)))
    check(all(s["mmpx"] and 0.2 < s["mmpx"] < 0.35 for s in sm), "샘플마다 mmpx (판 보정 국소 배율)")
    check(all(s["fit"] is False for s in sm), "40 mm 원 끝: 전부 윤곽 밖 (%s)" % [s["fit"] for s in sm])
    c.acks.clear()
    await c.send(cmd="run", mode="auto")
    a = await wait_ack(c, "run")
    check(bool(a) and not a["ok"] and any("핀 벗어남" in r for r in a.get("needs_confirm", [])),
          "순회 전 확인: 핀 벗어남 목록 (%s)" % (a or {}).get("needs_confirm"))
    # 공구를 바꾸면 다시 검사
    await c.send(cmd="tool_select", name="작은 핀")
    await wait_ack(c, "tool_select")
    await c.pump(0.5)
    check(c.state["tip"]["w"] == 1.5 and all(s["fit"] is True for s in c.state["samples"]),
          "작은 핀: 전부 안 (%s)" % [s["fit"] for s in c.state["samples"]])
    # 스냅: 칩 하나를 지운 뒤 중심에서 12 px 어긋난 자리를 클릭 → 그 칩 중심에 붙는다
    target = dict(c.state["samples"][4])
    await c.send(cmd="sample_delete", no=target["no"])
    await c.pump(0.6)
    c.acks.clear()
    c.logs.clear()
    await c.send(cmd="sample_add", u=target["u"] + 12, v=target["v"] - 9)
    a = await wait_ack(c, "sample_add")
    await c.pump(0.5)
    sn = next((s for s in c.state["samples"] if a and s["no"] == a.get("no")), None)
    dist = ((sn["u"] - target["u"]) ** 2 + (sn["v"] - target["v"]) ** 2) ** 0.5 if sn else 99
    check(bool(a) and a["ok"] and bool(sn) and dist < 4 and len(sn["verts"]) >= 3 and sn["manual"],
          "스냅: 칩 중심에 붙음 · 윤곽 달림 (%.1f px)" % dist)
    check(any("스냅" in l["msg"] and "mm 이동" in l["msg"] for l in c.logs), "스냅 이동 거리 로그")
    # 스냅 끄면 클릭 자리 그대로
    await c.send(cmd="settings_save", values={"sample_add": {"snap": False}})
    await c.pump(0.6)
    free = _free_px(c.state)
    c.acks.clear()
    await c.send(cmd="sample_add", u=free[0], v=free[1])
    a = await wait_ack(c, "sample_add")
    await c.pump(0.4)
    s2 = next((s for s in c.state["samples"] if a and s["no"] == a.get("no")), None)
    check(bool(a) and a["ok"] and bool(s2) and abs(s2["u"] - free[0]) < 0.2 and not s2["verts"] and s2["fit"] is None,
          "스냅 끔: 클릭한 자리 · 윤곽 없음 · 검사 안 함")
    # 끌기(이동) · 되돌리기: 검출 샘플을 옮기면 source manual, 윤곽은 남아 맞음 검사가 계속된다
    mv = dict(c.state["samples"][0])
    await c.send(cmd="sample_move", no=mv["no"], u=mv["u"] + 4, v=mv["v"] + 3)
    a = await wait_ack(c, "sample_move")
    await c.pump(0.4)
    m1 = next(s for s in c.state["samples"] if s["no"] == mv["no"])
    check(bool(a) and a["ok"] and m1["source"] == "manual" and abs(m1["u"] - mv["u"] - 4) < 0.2
          and len(m1["verts"]) >= 3 and m1["fit"] is True, "이동: source manual · 윤곽 유지 · fit 유지")
    with open(os.path.join(c.state["sequence"]["out_dir"], "samples_edit.json"), encoding="utf-8") as f:
        je = json.load(f)
    check(any(x["no"] == mv["no"] and x["source"] == "manual" for x in je), "samples_edit.json 에 이동 기록")
    await c.send(cmd="sample_undo")
    await c.pump(0.5)
    m0 = next(s for s in c.state["samples"] if s["no"] == mv["no"])
    check(abs(m0["u"] - mv["u"]) < 0.2 and m0["source"] != "manual", "이동 되돌리기")
    # 겹침 · 감지영역 밖 이동 거절
    other = c.state["samples"][1]
    await c.send(cmd="sample_move", no=mv["no"], u=other["u"], v=other["v"])
    a = await wait_ack(c, "sample_move")
    check(bool(a) and not a["ok"] and a["reason"] == "overlap", "겹치는 자리로 이동 거절 (%s)" % (a or {}).get("reason"))
    # 화면: 공구 끝 모양이 그려지고 벗어난 샘플은 경고 색
    await c.send(cmd="tool_select", name="기준 포인터")
    await c.pump(0.6)
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        return

    def look():
        with sync_playwright() as pw:
            b = pw.chromium.launch()
            pg = b.new_page(viewport={"width": 1920, "height": 1040})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(1500)
            n_tip = pg.locator("#ovTips polygon.ov-tip").count()
            n_bad = pg.locator("#ovTips polygon.ov-tip.bad").count()
            pg.screenshot(path=os.path.join(DATA_DIR, "tip_view.png"))
            pg.click("#btnTip")
            n_off = pg.locator("#ovTips polygon.ov-tip").count()
            b.close()
            return n_tip, n_bad, n_off, errs
    n_tip, n_bad, n_off, errs = await asyncio.get_running_loop().run_in_executor(None, look)
    on = sum(1 for s in c.state["samples"] if s["on"] and s.get("mmpx"))
    check(n_tip == on and n_bad >= 1 and n_off == 0 and not errs,
          "화면: 공구 끝 %d/%d · 경고색 %d · 토글 끄면 %d · 오류 %s" % (n_tip, on, n_bad, n_off, errs))


async def fixed_big_flow(c):
    """4080x3060 · detect_max_px 축소 후 좌표 복원 · 소요 시간."""
    truth = FIXED["truth"]
    check(c.state["settings"]["detect_max_px"] == 1000, "detect_max_px 1000")
    c.logs.clear()
    t0 = time.monotonic()
    await c.send(cmd="capture")
    ph = await c.wait_phase(("ready", "error"), 180)
    dt = time.monotonic() - t0
    samples = c.state["samples"]
    check(ph == "ready" and len(samples) == len(truth["samples"]),
          "4080x3060 촬영 · 샘플 %d (%s)" % (len(samples), ph))
    errs = [_nearest(samples, x, y)[1] for x, y in truth["samples"]] if samples else [99]
    check(max(errs) < 0.3, "축소 검출 뒤 원본 좌표 복원 · 최대 오차 %.3f mm" % max(errs))
    f = c.state["frame"] or {"w": 0, "h": 0}
    check(f["w"] == 4080 and f["h"] == 3060, "촬영본 %dx%d" % (f["w"], f["h"]))
    # 축소 배율은 mm/px 에 반영돼 면적(mm²)이 맞는다(칩 7~9 mm → 약 50~80 mm²)
    areas = [s.get("area_mm2") for s in samples if s.get("area_mm2")]
    check(bool(areas) and 30 < sum(areas) / len(areas) < 130,
          "면적 mm² 가 축소 배율을 반영 (평균 %.0f)" % (sum(areas) / max(len(areas), 1)))
    import cv2
    import numpy as np
    code, body = http_get(c.port, "/preview.jpg")
    im = cv2.imdecode(np.frombuffer(body, np.uint8), cv2.IMREAD_COLOR) if code == 200 else None
    check(im is not None and im.shape[1] <= c.state["settings"]["preview_max_w"],
          "미리보기 축소 %s" % str(None if im is None else im.shape[:2]))
    tl = _timing_line(c)
    print("  [소요 4080x3060] %s (명령→ready %.1f s)" % (tl, dt), flush=True)
    check(tl is not None and "4080x3060" in tl, "소요 로그 (%s)" % tl)


def prep_head_calib():
    """합성 카메라의 참 호모그래피로 만든 헤드 보정(head_calib.json) - 격자 촬영 없이 유효한 보정을 둔다."""
    import numpy as np
    sc = _synth()
    w, h = 1280, 720                                   # SMALL 의 해상도
    Hs = sc.true_homography(w, h)                      # 마커 평면 mm → px
    tr = np.array([[1.0, 0, -sc.HEAD_OFFSET_MM[0]], [0, 1.0, -sc.HEAD_OFFSET_MM[1]], [0, 0, 1.0]])
    H = tr @ np.linalg.inv(Hs)                         # px → 스테이지 XY(기준 포인터)
    d = {"created": time.strftime("%Y-%m-%d %H:%M:%S"), "camera_index": int(BASE_SETTINGS.get("camera_index", 1)),
         "size": [w, h], "marker_id": 10, "marker_mm": 40.0, "dict": "4X4_50",
         "H": [[float(a) for a in row] for row in H], "points": [], "rms_mm": 0.01, "max_mm": 0.02,
         "rms_limit_mm": 0.5, "ok": True}
    with open(os.path.join(DATA_DIR, "head_calib.json"), "w", encoding="utf-8") as f:
        json.dump(d, f)
    return None


async def _reconnect(c):
    await c.send(cmd="stage_disconnect")
    await c.pump(0.8)
    c.logs.clear()
    await c.send(cmd="stage_connect")


_MOVES = ("mx", "my", "g", "gx", "gy", "mz", "gz", "jx", "jy", "jz")


async def head_connect_flow(c):
    """헤드 마커 1개로: 연결 직후 위치 판단(이동 없음) · 화면 끝 경고 · 격자 가장자리 점 건너뜀."""
    check(c.state["photohome"]["calib_valid"], "헤드 보정 유효(판 마커 없이)")
    check(c.state["settings"]["calib"]["mode"] == "fixed", "고정 보정 모드")
    await _reconnect(c)
    dt = await wait_log(c, "위치 확인 정상", 40)
    await c.pump(0.5)
    check(dt is not None, "연결 직후 위치 판단 · 정상 (%s)" % [l["msg"] for l in c.logs if "위치" in l["msg"]][:3])
    sent = _sent(c)
    check(not any(m.split(" ")[0] in _MOVES for m in sent), "판단 중 이동 명령 없음 (%s)" % sent[:8])
    check(c.state["photohome"]["status"] == "ok", "위치 확인 칩 '정상' (%s)" % c.state["photohome"]["status"])
    # 켠 설정이 꺼지면 하지 않는다
    await c.send(cmd="settings_save", values={"photohome": {"on_connect": False}})
    await wait_ack(c, "settings_save")
    await _reconnect(c)
    await c.pump(4.0)
    check(not any("위치 확인" in l["msg"] for l in c.logs), "photohome.on_connect 끄면 연결 직후 판단 안 함")
    await c.send(cmd="settings_save", values={"photohome": {"on_connect": True}})
    await wait_ack(c, "settings_save")
    # 기본 여백(짧은 변의 3%): (0, 0) 에서 마커가 아래 끝에 걸친다 → 결과를 쓰지 않는다
    await c.send(cmd="settings_save", values={"photohome": {"edge_margin_px": 0}})
    await wait_ack(c, "settings_save")
    c.acks.clear()
    c.logs.clear()
    await c.send(cmd="photo_home")
    a = await wait_ack(c, "photo_home", 20)
    await c.pump(0.4)
    check(bool(a) and not a["ok"] and "화면 끝" in c.state["photohome"]["msg"],
          "헤드 마커 화면 끝 경고 (%s)" % c.state["photohome"]["msg"])
    check(not any(m.startswith("-> sp ") for m in _sent(c)), "화면 끝이면 결과를 쓰지 않음(sp 없음)")
    # 격자 점 중 가장자리에 걸리는 점은 건너뛰고 보고(X=0 줄 4점)
    await c.send(cmd="settings_save", values={"photohome": {
        "grid_n": 4, "grid_margin_mm": 10, "edge_margin_px": 120, "settle_s": 0.1}})
    await wait_ack(c, "settings_save")
    await c.send(cmd="head_calib_start")
    a = await wait_ack(c, "head_calib", 180)
    res = (a or {}).get("result") or {}
    check(bool(a) and a["ok"] and len(res.get("skipped", [])) == 4 and len(res.get("points", [])) == 12,
          "격자 16점 중 가장자리 4점 건너뜀 (점 %s · 건너뜀 %s · %s)"
          % (len(res.get("points", [])), len(res.get("skipped", [])),
             (a or {}).get("needs_confirm") or (a or {}).get("reason")))


async def head_missing_flow(c):
    """연결 직후 헤드 마커가 안 보이면: 알리기만 하고 아무것도 움직이지 않는다."""
    await _reconnect(c)
    dt = await wait_log(c, "미검출", 40)
    await c.pump(0.5)
    check(dt is not None, "마커 미검출 안내 로그 (%s)" % [l["msg"] for l in c.logs if "위치" in l["msg"] or "마커" in l["msg"]][:3])
    sent = _sent(c)
    check(not any(m.startswith("-> sp ") or m.split(" ")[0] in _MOVES for m in sent),
          "미검출이면 sp · 이동 모두 없음 (%s)" % sent[:8])
    check(c.state["photohome"]["status"] == "failed" and c.state["stage"]["x_mm"] == 0.0
          and c.state["stage"]["y_mm"] == 0.0, "칩 '실패' · 위치 그대로")


def main():
    for f in SRC.values():
        if not os.path.exists(f):
            raise SystemExit("검증용 사진이 없습니다: %s" % f)
    img = setup_data()
    dry_io_check()
    good, glare = img["seq_20260909_150413"], img["seq_20260909_150621"]
    try:
        SLOW = {"WAFER_STAGE_DRY_MOVE_S": "3"}    # dry 이동을 3초로 늘린다
        SYNTH = {"WAFER_STAGE_DRY_CAMERA": "synth"}   # 사진 호밍: 합성 카메라
        BIG_JOG = {"jog": {"max_mm": 1000}}       # 예전 검증의 큰 조그(300 mm)
        # 순회 중 거절을 보는 케이스: 순회가 검사 도중 끝나지 않게 샘플당 1 s(예전 dwell 1.0)
        SLOW_RUN = {"recipe": {"items": [dict(FAST_RECIPE, steps=[
            dict(st, s=1.0) if st["type"] == "wait" else st for st in FAST_RECIPE["steps"]])]}}
        HEAT = {"name": "히트건", "dx_mm": 5.0, "dy_mm": -3.0, "z_work_mm": 10.0, "note": ""}
        POINTER = {"name": "기준 포인터", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0, "note": ""}
        RELAY_P = {"relay": {"items": relays_patch((1, 2, 3), max_on={1: 1.5}),
                             "watchdog_ms": 600}}
        RECIPE_P = {
            "relay": {"items": relays_patch((1,))},
            "tool": {"items": [POINTER, HEAT], "active": "히트건"},
            "recipe": {"active": "기본", "items": [FAST_RECIPE,
                {"name": "가열", "steps": [
                    {"type": "move_sample"}, {"type": "z", "target": "work"},
                    {"type": "relay", "n": 1, "on": True}, {"type": "wait", "s": 0.3},
                    {"type": "measure"}, {"type": "relay", "n": 1, "on": False},
                    {"type": "z", "target": "top"}]},
                {"name": "긴 대기", "steps": [{"type": "move_sample"}, {"type": "wait", "s": 4}]}]}}
        TOOL_P = {"tool": {"items": [POINTER, HEAT], "active": "히트건"}}
        CAPTURE_P = {"capture_xy": [10, 200], "flow": {"after_photo": True}}
        TOOLCAL_P = {"relay": {"items": relays_patch((1,))}}
        SETTINGS_P = {"zz_unknown": {"keep": 1}, "de_floor": 8.0}
        legacy = {k: v for k, v in BASE_SETTINGS.items()
                  if k not in ("tool", "recipe", "relay", "capture_xy", "focus_mode",
                               "focus_sweep", "motion", "flow", "photohome", "calib")}
        legacy.update({"park_xy": [10, 20], "z_measure_mm": 33, "dwell_s": 7,
                       "autofocus": False, "focus": "auto", "zz_legacy": "keep",
                       "marker_mm_xy": {"0": [200, 161], "1": [201, 19], "2": [15, 160],
                                        "3": [15, 20]}})
        FIXED_P = {"calib": {"mode": "fixed"}, "fixed": {"layout": "free"}, "width": 1280, "height": 960, "preview_max_w": 640,
                   "photohome": {"before_capture": False}, "flow": {"capture_goto": False}}
        FIXED_BIG_P = {"calib": {"mode": "fixed"}, "fixed": {"layout": "free"}, "width": 4080, "height": 3060,
                       "detect_max_px": 1000, "preview_max_w": 1280,
                       "photohome": {"before_capture": False}, "flow": {"capture_goto": False}}
        TIP_PATCH = dict(FIXED_P, tool={
            "fit_margin_mm": 0.3, "active": "기준 포인터",
            "items": [{"name": "기준 포인터", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0, "note": "",
                       "tip_shape": "circle", "tip_w_mm": 40.0, "tip_h_mm": 40.0, "tip_angle_deg": 0.0},
                      {"name": "작은 핀", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0, "note": "",
                       "tip_shape": "circle", "tip_w_mm": 1.5, "tip_h_mm": 1.5, "tip_angle_deg": 0.0}]})
        _PTR_Z10 = {"name": "기준 포인터", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 10.0, "note": "",
                    "tip_shape": "rect", "tip_w_mm": 10.0, "tip_h_mm": 2.0, "tip_angle_deg": 0.0, "tip_angle_img_deg": 90.0}
        MEAS_RECIPE = {"name": "T", "steps": [
            {"type": "move_sample"},
            {"type": "cooler", "action": "on", "target": "sample"},
            {"type": "contact", "start_ref": "work", "start_mm": -2.0, "step_mm": 0.5, "max_ref": "work", "max_mm": 5.0,
             "press_mm": 0.1, "settle_s": 0.1, "on_fail": "stop"},
            {"type": "log", "action": "start"},
            {"type": "wait_temp", "cond": "<=", "target_c": 30.0, "hold_s": 1.0, "max_s": 120.0, "on_timeout": "stop"},
            {"type": "heat", "end": "either", "target_c": 80.0, "time_s": 40.0, "max_s": 100.0},
            {"type": "cool", "cond": "<=", "target_c": 40.0, "hold_s": 0.0, "max_s": 200.0, "on_timeout": "stop"},
            {"type": "log", "action": "end"},
            {"type": "z", "target": "top"}]}
        MEAS_P = dict(FIXED_P, relay={"items": relays_patch(tuple(range(1, 11)))},
                      tool={"items": [_PTR_Z10], "active": "기준 포인터", "fit_margin_mm": 50.0},
                      sim={"time_scale": 30.0, "contact_z_mm": 12.0, "noise_c": 0.0},
                      recipe={"active": "T", "items": [MEAS_RECIPE]})
        TIPDIR_P = dict(FIXED_P, tool={"fit_margin_mm": 50.0, "active": "기준 포인터", "items": [
            _PTR_Z10, {"name": "이행 핀", "dx_mm": 0.0, "dy_mm": 0.0, "z_work_mm": 0.0, "note": "", "tip_shape": "rect",
                       "tip_w_mm": 10.0, "tip_h_mm": 2.0, "tip_angle_deg": None, "tip_angle_img_deg": 90.0}]})
        SC_P = dict(FIXED_P, calib={"mode": "fixed", "area_size_mm": [156.0, 156.0]},
                    tool={"fit_margin_mm": 50.0})
        AXES_P = {"calib": {"mode": "fixed", "axis_probe_mm": 30.0, "area_size_mm": [160.0, 160.0]}, "width": 1280, "height": 720,
                  "photohome": {"before_capture": False, "settle_s": 0.1}}
        HEAD_P = {"calib": {"mode": "fixed"}, "width": 1280, "height": 720,
                  "photohome": {"on_connect": True, "settle_s": 0.2, "edge_margin_px": 5}}
        cases = [
            ("정상 흐름", good, main_flow, None, None),
            ("샘플 편집", good, edit_flow, None, None),
            ("수동 이동", good, jog_flow, None, deep_merge(BIG_JOG, SLOW_RUN)),
            ("비상정지 지연(원점)", good, estop_delay_home_flow, SLOW, None),
            ("비상정지 지연(이동)", good, estop_delay_move_flow, SLOW, None),
            ("수동 원점 등록", good, origin_flow, {"WAFER_STAGE_DRY_NO_HOME": "1"}, None),
            ("끝단 이동", good, touch_end_flow, None, None),
            ("끝단 이동 중단", good, touch_end_abort_flow,
             {"WAFER_STAGE_DRY_MOVE_S": "1"}, None),
            ("Z 축", good, z_flow, None, None),
            ("Z 단독 등록", good, z_alone_flow, {"WAFER_STAGE_DRY_NO_HOME": "1"}, None),
            ("가동범위 설정", good, limits_flow, None, BIG_JOG),
            ("Z 원점 없이 파킹·복귀", good, no_z_origin_flow,
             {"WAFER_STAGE_DRY_NO_HOME": "1"}, None),
            ("펌웨어 V8", good, fw_v8_flow, {"WAFER_STAGE_DRY_FW": "V8"}, None),
            ("비상정지 중복", good, estop_dedup_flow, None, None),
            ("펌웨어 V7", good, fw_old_flow,
             {"WAFER_STAGE_DRY_FW": "V7", "WAFER_STAGE_DRY_NO_HOME": "1"}, None),
            ("정지 경로", good, stop_flow, None, None),
            ("비상정지 경로", good, estop_flow, None, None),
            ("확인 필요 경로", glare, confirm_flow, None, None),
            ("설정", good, settings_flow, None, SETTINGS_P),
            ("설정 이행", good, migrate_flow, None, None, legacy),
            ("릴레이", good, relay_flow, None, RELAY_P),
            ("레시피", good, recipe_flow, None, RECIPE_P),
            ("공구 오프셋", good, tool_flow, None, TOOL_P),
            ("촬영 위치", good, capture_pos_flow, None, CAPTURE_P),
            ("사진 호밍", good, photohome_flow, SYNTH, SMALL),
            ("공구 보정", good, tool_calib_flow, None, TOOLCAL_P),
            ("고정 보정 촬영", lambda: prep_fixed(1280, 960), fixed_flow, None, FIXED_P),
            ("고정 보정 판 보정", lambda: prep_fixed(1280, 960), fixed_calib_flow, None, FIXED_P),
            ("고정 보정 검출만", lambda: prep_fixed(1280, 960, with_h=False), fixed_area_only_flow, None,
             dict(FIXED_P, calib={"mode": "fixed", "area_size_mm": [160.0, 160.0]}, sample_add={"snap": False})),
            ("고정 보정 감지영역 없음", lambda: prep_fixed(1280, 960, with_area=False), fixed_nocal_flow, None, FIXED_P),
            ("고정 보정 무효", lambda: prep_fixed(1280, 960, cal_size=(640, 480)), fixed_invalid_flow, None, FIXED_P),
            ("촬영 진행 표시", lambda: prep_fixed(1280, 960), capture_progress_flow, None, FIXED_P),
            *([("고정 환경 실장 사진", prep_real_fixed, fixed_real_flow, None,
                {"calib": {"mode": "fixed", "est_mm_per_px": 0.151}, "width": 4080, "height": 3060,
                 "preview_max_w": 1280, "photohome": {"before_capture": False}, "flow": {"capture_goto": False}}
                )] if REAL.setdefault("scene", real_scene()) else []),
            ("공구 끝 표시", lambda: prep_fixed(1280, 960), tip_flow, None, TIP_PATCH),
            ("측정 과정", lambda: prep_fixed(1280, 960), process_flow, None, MEAS_P),
            ("샘플로 보정", prep_sample_calib, sample_calib_flow, None, SC_P),
            ("축 방향 측정", lambda: (prep_axes(False) or good), axes_flow(False), SYNTH, AXES_P),
            ("축 방향 측정 90", lambda: (prep_axes(True) or good), axes_flow(True), dict(SYNTH, WAFER_STAGE_SYNTH_ROT="1"), AXES_P),
            ("공구 방향 0", lambda: prep_fixed_rot(False), tip_dir_flow(False), None, TIPDIR_P),
            ("공구 방향 90", lambda: prep_fixed_rot(True), tip_dir_flow(True), None,
             dict(TIPDIR_P, width=960, height=1280)),
            ("고정 보정 4080", lambda: prep_fixed(4080, 3060), fixed_big_flow, None, FIXED_BIG_P),
            ("헤드 마커 연결 판단", lambda: (prep_head_calib() or good), head_connect_flow, SYNTH, HEAD_P),
            ("헤드 마커 미검출", lambda: (prep_head_calib() or good), head_missing_flow,
             dict(SYNTH, WAFER_STAGE_SYNTH_HEAD_OFF="1"), HEAD_P),
            ("이동 중 종료 거절", good, exit_moving_flow, SLOW, None),
            ("종료", good, exit_flow, None, None)]
        # python tools/e2e_smoke.py 릴레이 레시피  - 이름 일부로 골라 돌린다(개발용)
        only = [a for a in sys.argv[1:] if not a.startswith("-")]
        for name, image, flow, env_extra, patch, *rest in cases:
            if only and not any(o in name for o in only):
                continue
            if callable(image):
                image = image()            # 고정 보정 케이스: 합성 장면(+ plate_calib.json)을 만든다
            elif image is None:
                image = good
            base = rest[0] if rest else None
            port = free_port()
            print("[%s] 서버 :%d  %s"
                  % (name, port, os.path.basename(os.path.dirname(image))), flush=True)
            p = start_server(port, image, env_extra, patch, base)
            try:
                asyncio.run(run_case(port, image, flow))
            finally:
                # 종료 케이스는 서버가 스스로 끝난다. 이미 죽었어도 정상이다.
                with contextlib.suppress(Exception):
                    p.kill()
                with contextlib.suppress(Exception):
                    p.wait(timeout=10)
                with contextlib.suppress(Exception):
                    p._smoke_log.close()
                # 헤드 보정은 그 케이스에만 - 다음 케이스의 촬영 전 확인에 끼지 않게
                for _f in ("head_calib.json", "plate_calib.json"):
                    with contextlib.suppress(OSError):
                        os.remove(os.path.join(DATA_DIR, _f))
            time.sleep(0.5)
    finally:
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
