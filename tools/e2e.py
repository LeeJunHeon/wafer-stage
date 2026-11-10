"""e2e.py - 전체 흐름 시험(dry + 합성 카메라). 케이스마다 서버를 띄운다: 임시 데이터 폴더(WAFER2_DATA) · 임시 설정 파일(WAFER2_SETTINGS).
실제 data_v2 · settings.json · COM 포트 · 카메라는 건드리지 않는다.

  python tools/e2e.py [케이스 번호(예: 65 · 09b) 또는 이름 일부 ...]
"""
import json
import math
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import traceback

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
from websockets.sync.client import connect  # noqa: E402

W, H = 1280, 960
MODE = os.environ.get("E2E_MODE", "dry")          # dry | fake (fake-serial: tools/fake_v11.py 를 시리얼 자리에)
FAILS = []
CHECKS = [0]


def check(cond, msg):
    CHECKS[0] += 1
    print(("  ok   " if cond else "  FAIL ") + msg, flush=True)
    if not cond:
        FAILS.append(msg)
    return cond


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _merge(a, b):
    out = dict(a)
    for k, v in b.items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


class Client:
    def __init__(self, patch=None, env=None, precreate=None):
        self.tmp = tempfile.mkdtemp(prefix="wafer2_e2e_")
        self.data = os.path.join(self.tmp, "data")
        if precreate:                                    # 서버를 띄우기 전에 데이터 폴더에 파일을 미리 둔다(예: estop.lock)
            os.makedirs(self.data, exist_ok=True)
            precreate(self.data)
        self.settings = os.path.join(self.tmp, "settings.json")
        base = {"width": W, "height": H, "capture_frames": 2, "preview_fps": 2, "status_poll_s": 0.5,
                "capture": {"settle_s": 0.2, "fresh_frames": 5},
                "contact_z_mm": 20.0, "approach_mm": 2.0,
                "sim": {"time_scale": 60.0}, "autosave_s": 60,
                "relay": {"items": [{"enabled": True, "name": "R%d" % (i + 1), "max_on_s": 0} for i in range(10)],                           "on_estop": "off", "on_stop": "off", "on_error": "off", "on_exit": "off"},
                "photohome": {"grid_n": 3, "grid_margin_mm": 20.0}, "capture_xy": [10.0, 90.0], "park_xy": [10.0, 20.0],
                "calib": {"area_size_mm": [160.0, 160.0]}, "heater": {"relay": 10, "max_on_s": 120.0},
                "jog": {"z_max_mm": 45.0},                       # 예전 시험은 Z 를 크게 조그한다 - 모터 안전 시험(56~)은 이 값을 기본(1 mm)으로 되돌린다
                "rt": {"file_path": ""}}                         # 시험은 실제 NAS(기본 폴더)에 쓰지 않는다 - NAS 시험은 임시 폴더를 준다
        base = _merge(base, patch or {})
        with open(self.settings, "w", encoding="utf-8") as f:
            json.dump(base, f, ensure_ascii=False)
        self.port = free_port()
        e = dict(os.environ, WAFER2_DATA=self.data, WAFER2_SETTINGS=self.settings, PYTHONUTF8="1", WAFER2_SYNTH_W=str(W),
                 WAFER2_SYNTH_H=str(H), WAFER2_NO_SHELL="1", WAFER_STAGE_DRY_MOVE_S="0")
        e.update(env or {})
        self.log = open(os.path.join(self.tmp, "server.log"), "w", encoding="utf-8")
        self.proc = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py"), "--fake-serial" if MODE == "fake" else "--dry", "--no-window", "--port", str(self.port)],
                                     env=e, stdout=self.log, stderr=subprocess.STDOUT, cwd=self.tmp)     # 상대 경로로 쓰는 것이 있어도 저장소 밖(임시 폴더)
        self.ws = None
        self.state = {}
        self.id = 0
        self.acks = {}
        t0 = time.time()
        while time.time() - t0 < 40:
            try:
                self.ws = connect("ws://127.0.0.1:%d/ws" % self.port, max_size=None, ping_interval=None)
                break
            except OSError:
                time.sleep(0.3)
        if self.ws is None:
            raise RuntimeError("서버 연결 실패")
        self.pump(2.0)

    def pump(self, t=0.3):
        end = time.time() + t
        while time.time() < end:
            try:
                m = json.loads(self.ws.recv(timeout=max(0.01, end - time.time())))
            except TimeoutError:
                break
            except Exception:
                break
            if m.get("type") == "state":
                self.state = m["state"]
            elif m.get("type") == "ack":
                self.acks[m["id"]] = m
        return self.state

    def send(self, cmd, timeout=120, **args):
        """명령을 보내고 ack 를 기다린다. 반환 (ok, res | 사유)."""
        i = self.send_nowait(cmd, **args)
        return self.wait_ack(i, timeout)

    def send_nowait(self, cmd, **args):
        self.id += 1
        self.ws.send(json.dumps({"id": self.id, "cmd": cmd, "args": args}))
        return self.id

    def wait_ack(self, i, timeout=60):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.pump(0.2)
            if i in self.acks:
                a = self.acks.pop(i)
                self.pump(0.35)                       # ack 뒤 갱신된 state 를 받는다
                return a.get("ok"), (a.get("res") if a.get("ok") else a.get("msg"))
        return False, "시간 초과"

    def wait(self, fn, timeout=60, what=""):
        t0 = time.time()
        while time.time() - t0 < timeout:
            self.pump(0.2)
            try:
                if fn(self.state):
                    return True
            except Exception:
                pass
        print("  (대기 시간 초과 %s)" % what)
        return False

    def http(self, path, method="GET"):
        import urllib.request
        req = urllib.request.Request("http://127.0.0.1:%d%s" % (self.port, path), method=method)
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()

    def dry(self, op, **kw):
        return self.send("dry", op=op, **kw)

    def close(self):
        try:
            if self.ws:
                self.ws.close()
        except Exception:
            pass
        self.proc.terminate()
        try:
            self.proc.wait(8)
        except Exception:
            self.proc.kill()
        self.log.close()
        shutil.rmtree(self.tmp, ignore_errors=True)


# ---------------------------------------------------------------------------
# 공통 준비: 판 영역 -> 빈 판 사진 -> 좌표 보정 5점 -> 헤드 위치 보정
# ---------------------------------------------------------------------------
def truth_px(c):
    ok, t = c.dry("truth")
    return t


def mm2px(t, x, y):
    m = t["H"]
    p = [m[0][0] * x + m[0][1] * y + m[0][2], m[1][0] * x + m[1][1] * y + m[1][2], m[2][0] * x + m[2][1] * y + m[2][2]]
    return p[0] / p[2], p[1] / p[2]


def setup_area_ref(c, t):
    quad = [mm2px(t, x, y) for x, y in t["quad_mm"]]
    ok, r = c.send("area_set", points=[list(p) for p in quad], frame="ref", size=[W, H])
    check(ok, "판 영역 지정 (%s)" % (r,))
    c.dry("world", kw={"empty": True})
    ok, r = c.send("ref_capture")
    check(ok, "빈 판 사진 촬영 · 촬영 위치로 자동 이동 (%s)" % (r,))
    c.dry("world", kw={"empty": False})
    return quad


def setup_calib(c, t, n_extra=1):
    pts = [(40.0, 40.0), (40.0, 200.0), (200.0, 200.0), (200.0, 40.0), (120.0, 120.0), (70.0, 70.0)][:4 + n_extra]
    for x, y in pts:
        ok, r = c.send("move_xy", x=x, y=y)
        ok2, r2 = c.send("calib_record")
        u, v = mm2px(t, x, y)
        ok3, r3 = c.send("calib_click", u=u, v=v, frame="ref")
        if not (ok and ok2 and ok3):
            check(False, "기준점 기록 실패 %s %s %s" % (r, r2, r3))
    return c.send("calib_apply")


def setup_head(c):
    ok, r = c.send("head_calib", timeout=240)
    check(ok, "헤드 위치 보정 격자 (%s)" % (r,))
    ok2, r2 = c.send("head_apply")
    check(ok2, "헤드 위치 보정 적용 (%s)" % (r2,))


def full_setup(c):
    t = truth_px(c)
    setup_area_ref(c, t)
    ok, r = setup_calib(c, t)
    check(ok, "좌표 보정 적용 5점 (%s)" % (r,))
    setup_head(c)
    return t


def chips_err(c, t):
    """샘플 X · Y 와 참 칩 위치의 최대 오차."""
    errs = []
    for s in c.state["samples"]:
        if s["X"] is None:
            return 99.0
        errs.append(min(math.hypot(s["X"] - x, s["Y"] - y) for x, y in t["chips"]))
    return max(errs) if errs else 99.0


# ---------------------------------------------------------------------------
CASES = {}


def case(name):
    def deco(fn):
        CASES[name] = fn
        return fn
    return deco


@case("01 연결 · 원점 · 상태")
def c01(c):
    st = c.state
    check(st["stage"] and st["stage"]["connected"], "스테이지 연결")
    check(st["stage"]["z_ok"] and st["stage"]["xy_ok"] and not st["stage"]["locked"], "원점 복원 · 잠금 없음")
    check(st["phase"] == "대기", "상태 필 = 대기 (%s)" % st["phase"])
    check(st["calib"]["status"] == "need", "좌표 보정 필요 표시")


@case("02 장비 설정 전 과정")
def c02(c):
    full_setup(c)
    st = c.state
    check(st["calib"]["status"] == "done" and st["calib"]["head"]["status"] == "done", "좌표 보정 · 헤드 위치 보정 완료 (%s)" % st["calib"]["text"])
    sz = st["calib"]["size_mm"]
    check(sz and abs(sz[0] - 160) < 2 and abs(sz[1] - 160) < 2, "판 실측 크기 %s" % (sz,))
    ok, r = c.send("move_xy", x=100, y=100)
    check(ok, "이동 (%s)" % (r,))
    ok, r = c.send("jog", axis="x", mm=3.0)
    check(ok, "조그")


@case("03 촬영 · 인식 · 샘플 편집")
def c03(c):
    t = full_setup(c)
    ok, r = c.send("capture")
    check(ok, "촬영 (%s)" % (r,))
    st = c.state
    check(len(st["samples"]) == len(t["chips"]), "인식 %d / %d" % (len(st["samples"]), len(t["chips"])))
    check(chips_err(c, t) < 0.8, "좌표 최대 오차 %.2f mm" % chips_err(c, t))
    n0 = len(st["samples"])
    ok, _ = c.send("sample_add", u=300.0, v=300.0, snap=False)
    check(ok and len(c.state["samples"]) == n0 + 1, "샘플 추가")
    no = c.state["samples"][-1]["no"]
    ok, _ = c.send("sample_move", no=no, u=320.0, v=310.0)
    check(ok and abs([s for s in c.state["samples"] if s["no"] == no][0]["u"] - 320) < 0.2, "위치 수정")
    c.send("sample_exclude", no=1, on=True)
    check([s for s in c.state["samples"] if s["no"] == 1][0]["excluded"], "측정 제외")
    c.send("sample_delete", no=no)
    check(len(c.state["samples"]) == n0, "삭제")
    c.send("undo_edit")
    check(len(c.state["samples"]) == n0 + 1, "되돌리기(삭제 취소)")
    ok, _ = c.send("sample_here")
    check(ok, "현재 위치를 샘플로")


def run_cases(names):
    import e2e_cases  # noqa: F401  (시나리오 04~30 등록)
    import e2e_rt  # noqa: F401  (R–T 측정 시나리오 31~ 등록)
    import e2e_conn  # noqa: F401  (장치 연결 상태 시나리오 48~ 등록)
    import e2e_safety  # noqa: F401  (모터 안전 시나리오 56~ 등록)
    import e2e_stop  # noqa: F401  (정지 · 원점 · 연결 · 설정 시나리오 65~ 등록)
    def hit(k, n):                                       # "65" · "09b" 처럼 번호만 주면 그 번호의 케이스만(이름 안의 숫자와 섞이지 않게), 아니면 이름 일부
        return n.split()[0] == k if k.replace("-", "").isalnum() and k[:2].isdigit() and " " not in k else k in n
    sel = [n for n in CASES if not names or any(hit(k, n) for k in names)]
    for n in sel:
        print("== %s" % n, flush=True)
        c = None
        try:
            c = Client(patch=getattr(CASES[n], "patch", None), env=getattr(CASES[n], "env", None), precreate=getattr(CASES[n], "precreate", None))
            CASES[n](c)
        except Exception:
            traceback.print_exc()
            check(False, "예외 · " + n)
        finally:
            if c:
                c.close()
    print("\n검사 %d · 실패 %d" % (CHECKS[0], len(FAILS)))
    for f in FAILS:
        print("  - " + f)
    return 1 if FAILS else 0


if __name__ == "__main__":
    import e2e as _m                    # __main__ 과 다른 모듈 복사본이 되지 않게
    sys.exit(_m.run_cases(sys.argv[1:]))
