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
BASE_SETTINGS = {}                  # 저장소 settings.json(사용자 설정) - 케이스 패치의 바탕

# 모든 케이스에 얹는 패치: 기본 레시피의 대기를 0.2 s 로(순회가 빨리 끝나게).
FAST_RECIPE = {"name": "기본", "steps": [
    {"type": "move_sample"}, {"type": "z", "target": "work"}, {"type": "wait", "s": 0.2},
    {"type": "measure"}, {"type": "z", "target": "top"}]}
DEFAULT_PATCH = {"recipe": {"items": [FAST_RECIPE], "active": "기본"}}


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
    global DATA_DIR, BASE_SETTINGS
    DATA_DIR = tempfile.mkdtemp(prefix="wafer_smoke_")
    with open(paths.SETTINGS_PATH, encoding="utf-8") as f:
        BASE_SETTINGS = json.load(f)
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
        self.closed = False            # 서버가 스스로 끝났다(종료 명령)

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

    settings.json 은 케이스가 끝나면 _restore_settings 가 원래대로 돌린다.
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
    check(tabs == ["camera", "stage", "position", "photohome", "tool", "relay", "recipe",
                   "detect", "advanced"], "스키마 탭 9개 (%s)" % tabs)
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
        SETTINGS_P = {"zz_unknown": {"keep": 1}}
        legacy = {k: v for k, v in BASE_SETTINGS.items()
                  if k not in ("tool", "recipe", "relay", "capture_xy", "focus_mode",
                               "focus_sweep", "motion", "flow", "photohome")}
        legacy.update({"park_xy": [10, 20], "z_measure_mm": 33, "dwell_s": 7,
                       "autofocus": False, "focus": "auto", "zz_legacy": "keep",
                       "marker_mm_xy": {"0": [200, 161], "1": [201, 19], "2": [15, 160],
                                        "3": [15, 20]}})
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
            ("사진 호밍", good, photohome_flow, SYNTH, None),
            ("공구 보정", good, tool_calib_flow, None, TOOLCAL_P),
            ("이동 중 종료 거절", good, exit_moving_flow, SLOW, None),
            ("종료", good, exit_flow, None, None)]
        # python tools/e2e_smoke.py 릴레이 레시피  - 이름 일부로 골라 돌린다(개발용)
        only = [a for a in sys.argv[1:] if not a.startswith("-")]
        for name, image, flow, env_extra, patch, *rest in cases:
            if only and not any(o in name for o in only):
                continue
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
                with contextlib.suppress(OSError):
                    os.remove(os.path.join(DATA_DIR, "head_calib.json"))
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
