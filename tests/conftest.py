"""시험 공통 설정: 가짜 장비(tests/fakes/)와 임시 데이터 폴더만 쓴다. 실제 포트 · VISA · 카메라는 열지 않는다.

프로그램(app/)에는 가짜 장비가 없다. 시험이 Controller 의 "장비를 꽂는 자리"에 가짜를 꽂는다:
  open_port(스테이지 포트 여는 함수) → FakeV11 · meters.RM_FACTORY → 가짜 VISA(Lab) · camera.open_capture → 가짜 카메라 · clock · sleep → 시간 배율.
가짜 세계(World) 하나를 가짜 카메라 · 가짜 K2450 · 열 모델이 같이 본다. 시험은 ctrl.fake_fw · ctrl.lab · ctrl.world 로 가짜 장비를 들여다본다.
"""

import time

import pytest

from app import control as control_mod
from app.config import BY_KEY, Config
from app.control import Controller
from app.devices import meters
from app.devices.stage import PPMM
from tests.fakes import fake_visa
from tests.fakes.fake_camera import open_fake
from tests.fakes.fake_heatgun import FakeHeatGun
from tests.fakes.fake_v11 import FakeV11
from tests.fakes.world import World


# 전기 탐색을 시험할 때 명시하는 설정: 접촉 방식의 기본은 고정 높이이고, 탐색 간격의 기본값도 0.1 mm 로 크다.
# 탐색의 정밀도(표면 ± 0.05 mm)를 보는 시험은 v2 에서 쓰던 촘촘한 간격을 직접 준다.
SEARCH = dict(contact_mode="search", contact_step_mm=0.025, contact_search_step_mm=0.05, contact_search_mm=5.0)


class FakeClock:
    """안전 규칙의 시간을 시험이 직접 건너뛰게 한다."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s

    def sleep(self, s):
        """기다림 = 시계를 그만큼 건너뛴다(실제로는 기다리지 않는다)."""
        self.advance(max(0.0, float(s)))
        time.sleep(0)


def wait_for(fn, timeout=5.0, step=0.02):
    t_end = time.monotonic() + timeout
    while time.monotonic() < t_end:
        if fn():
            return True
        time.sleep(step)
    return False


class DeadThermometer:
    """연결은 돼 있지만 온도를 못 읽는 K2110(감시 스레드가 값을 덮어쓰지 않게)."""
    addr = "dead"
    is_open = True

    def read_temp(self):
        raise meters.VisaError("읽기 실패")

    def close(self):
        pass


def with_thermometer(ctrl, clock):
    ctrl.k2110 = DeadThermometer()
    ctrl.feed_temperature(30.0)


# ---------------------------------------------------------------------------
# 시간 배율과 실제 시간 창
# time_scale > 1 이면 열 모델 · 측정 · 안전 시계가 그만큼 빨리 간다. 그런데 "이 시간 안에 다음 일이 와야 한다" 는 창도 같이 줄어든다:
# temp_stale_s 3 초는 배율 60 에서 실제 0.05 초, 배율 200 에서 0.015 초다. 같은 프로세스의 다른 스레드가 GIL 을 잡아 측정 한 회차가 그보다 길어지면
# 안전 감시가 (맞게) "온도 측정 실패 · 히트건 차단" 을 낸다 - 프로그램이 아니라 시험 장치의 문제다(CPU 스레드 2~3개를 같이 돌리면 한 회차가 실제 0.5~0.8 초).
# 그래서 배율을 건 Controller 는 그런 창을 실제 시간 REAL_WINDOW_S 이상으로 정한다(설정 상한 안에서 - 그래도 MIN_REAL_WINDOW_S 아래로는 내리지 않는다).
# 시험이 값을 직접 주면 그대로 쓴다(그 창 자체를 보는 시험 - 판정은 그대로 본다).
# ---------------------------------------------------------------------------
REAL_WINDOW_S = 1.0                 # 배율을 걸어도 지켜 줄 실제 시간 여유(초)
MIN_REAL_WINDOW_S = 0.3             # 설정 상한(temp_stale_s 60 초) 때문에 위를 못 지킬 때의 하한 - 배율 200 에서 60 초 = 실제 0.3 초


def real_time_windows(time_scale, given):
    """배율을 건 Controller 의 "실제 시간 여유가 필요한 창" 기본값 {설정 이름: 값}. given = 시험이 직접 준 설정 이름들(그 값은 건드리지 않는다).
    - temp_stale_s : 측정 한 회차(온도 → 저항 → 측정 간격)가 이 안에 다시 와야 히트건이 꺼지지 않는다 → max(기본값, REAL_WINDOW_S × 배율), 상한 안에서.
    - heater_max_on_s : 가열에 드는 시간(열 모델이 정한다 - 30 → 85 °C 약 60 초) + 끝 온도를 알아채는 한 회차. 한 회차(= 실제 시간 × 배율)가
      부하에 늘어나므로 기본값에 REAL_WINDOW_S × 배율을 더한다.
    그 밖의 시간 값은 부하에 흔들리지 않아 그대로 둔다: 가열 무반응(읽은 온도의 오름으로 판정 - 회차가 늦으면 그만큼 더 올라 있다) ·
    냉각 정체 · 한 단계 한도(열 모델 시간 + 한 회차 - 한도 1800 초에 비해 작다) · 비상정지 직후 2초(해제를 막는 쪽이라 시험이 기다린다)."""
    k = float(time_scale)
    out = {}
    if k <= 1.0:
        return out
    if "temp_stale_s" not in given:
        it = BY_KEY["temp_stale_s"]
        v = min(float(it.max), max(float(it.default), REAL_WINDOW_S * k))
        if v / k < MIN_REAL_WINDOW_S:
            raise ValueError("time_scale %g 에서는 temp_stale_s 를 실제 %.2f 초 이상으로 정할 수 없음 - 배율을 낮추거나 값을 직접 줄 것" % (k, MIN_REAL_WINDOW_S))
        out["temp_stale_s"] = v
    if "heater_max_on_s" not in given:
        it = BY_KEY["heater_max_on_s"]
        out["heater_max_on_s"] = min(float(it.max), float(it.default) + REAL_WINDOW_S * k)
    return out


def alarm_lines(ctrl):
    """경보 목록을 "code: 글" 로(경보 목록을 비교하는 assert 의 실패 글 - 무엇이 떴는지 바로 보이게)."""
    return ["%s: %s" % (a["code"], a["text"]) for a in ctrl.state.snapshot()["alarms"]]


def alarm_codes(ctrl):
    return [a["code"] for a in ctrl.state.snapshot()["alarms"]]


def build_ctrl(cfg, clock=None, scale=0.05, time_scale=1.0, board=None, keep_board=False, firmware="V13"):
    """가짜 장비를 꽂은 Controller. scale = 가짜 스테이지의 이동 시간 배율(작을수록 빠름) · time_scale = 열 모델 · 측정 · 안전 시계의 배율.
    board = 이 시험용 보드를 연다(다시 열면 재시작 - FakeV11.reopen: EEPROM 표시로 위치 · 원점을 정한다). 종료 뒤 다시 실행을 볼 때 앞 Controller 의 보드를 준다.
    keep_board = [연결 끊기] 뒤 다시 [연결] 해도 같은 보드를 다시 연다(기본은 열 때마다 새 보드)."""
    k = float(time_scale)
    base = (lambda: time.monotonic() * k) if k != 1.0 else time.monotonic        # 가짜 열 모델의 시계
    sleep = (lambda s: time.sleep(s / k)) if k != 1.0 else time.sleep
    if hasattr(clock, "advance"):                                   # FakeClock(시험이 시간을 직접 건너뛰는 시계): 기다림(히트건 켜기 대기 등)도 그 시계를 건너뛴다
        sleep = clock.sleep
    world = World()
    box = {"fw": None, "ctrl": None}
    fw = lambda: box["fw"]                                          # noqa: E731
    gun = FakeHeatGun(world.fan_relay, world.element_relay, clock=clock or base)     # 배선은 세계가 정한다(설정을 바꿔도 전선은 그대로)
    lab = fake_visa.Lab(clock=base, sleep=sleep, world=world,
                        heater_fn=gun.power,                    # 열선 출력 %(V13) 의 시간 평균 전력
                        cooler_fn=lambda: bool(fw() and any(fw().relay[n - 1] for n in world.cooler_relays)),
                        relays_fn=lambda: [i + 1 for i, r in enumerate(fw().relay) if r] if fw() else [],
                        pos_fn=lambda: (fw().true_xy() + (fw().real["z"] / PPMM,)) if fw() else None)
    lab.read_dt = 0.02                                              # 가짜 읽기 시간(실장비는 길다)
    lab.addrs = ["ASRL5::INSTR", "ASRL4::INSTR", cfg["k2110_addr"], cfg["k2450_addr"]]
    fake_visa.install(lab)                                          # meters.RM_FACTORY 에 꽂는다

    def open_port(name):                                            # 스테이지 포트 여는 자리: 열 때마다 새 가짜 보드(board · keep_board 면 같은 보드를 다시 연다 = 재시작)
        old = board if box["fw"] is None else (box["fw"] if keep_board else None)
        box["fw"] = old.reopen() if old is not None else FakeV11(time_scale=scale, homed=True, firmware=firmware)
        box["fw"].on_relay = gun.update                             # 히트건은 보드의 릴레이 · 열선 출력 % 를 따라간다
        gun.update(box["fw"].relay, box["fw"].hp_pct)
        box["ctrl"].fake_fw = box["fw"]
        return box["fw"]

    ctrl = Controller(cfg, clock=clock or base, sleep=sleep, open_port=open_port)
    box["ctrl"] = ctrl
    ctrl.camera.open_capture = open_fake(lambda: fw().true_xy() if fw() else (0.0, 0.0), world=world)
    ctrl.fake_fw, ctrl.lab, ctrl.world, ctrl.heatgun = None, lab, world, gun     # 시험이 가짜 장비를 들여다보는 손잡이(프로그램은 쓰지 않는다)
    return ctrl


def fill_prep(ctrl):
    """초기 설정 2~7단계를 하나씩 하지 않고 가짜 세계의 참값으로 초기 설정 값을 채운다(원점 · 촬영 위치 · 판 영역 · 빈 판 사진 · 좌표 · 헤드 마커 · 접촉 높이 · 쿨러)."""
    p, w = ctrl.prep, ctrl.world
    p.calib.set_origin("xyz", "here")
    ctrl._remember(photo_head_xy=[5.0, 125.0], contact_z_mm=round(w.plate_z - 0.6, 3))
    loaded = w.loaded
    w.set_loaded(False)
    try:
        frame = p.capture(move=True, need_head=True)
        fh, fw = frame.shape[:2]
        p.calib.set_plate([[float(x), float(y)] for x, y in w.plate_corners_px(fw, fh)], (fw, fh))
        for n, xy in zip("ABCD", w.corners_stage()):
            p.calib.set_corner_stage(n, xy)
        p.empty_take()
    finally:
        w.set_loaded(loaded)
    p.calib.set_contact({"r2": None, "r4": None, "how": "접촉 탐색"})
    p.calib.set_cooler()
    p.marker_learn()
    p.publish()


@pytest.fixture
def make_ctrl(tmp_path, monkeypatch):
    made = []
    monkeypatch.setattr(control_mod, "serial_ports", lambda: [("COM5", "Arduino Mega 2560 (COM5)")])     # 포트 목록: 실제 포트를 훑지 않는다

    def make(clock=None, scale=0.05, connect=True, meters=True, camera=False, time_scale=1.0, board=None, keep_board=False, firmware="V13", **cfg_over):
        cfg = Config(tmp_path / "settings.json").load()
        # 이동 뒤 사진 대기: 시험용 카메라는 요청한 순간의 장면을 그리므로(늦게 오는 프레임이 없다) 기다리지 않는다 - 화면 멈춤 확인은 그대로 한다
        cfg.update({"data_dir": str(tmp_path / "data"), "camera_width": 320, "camera_height": 240, "cam_settle_wait_s": 0.0,
                    "heater_boot_hold_s": 0.0, **cfg_over})                 # 시험용 히트건은 켜기 직후 열선 점검(H-E)이 없다 - 유지를 보는 시험은 값을 직접 준다
        cfg.update(real_time_windows(time_scale, set(cfg_over)))       # 배율 때문에 실제 시간 창이 수십 ms 로 줄지 않게
        ctrl = build_ctrl(cfg, clock=clock, scale=scale, time_scale=time_scale, board=board, keep_board=keep_board, firmware=firmware)
        made.append(ctrl)
        if connect:
            ctrl.connect_stage()
        if connect and meters:
            ctrl.connect_meter("k2110")
            ctrl.connect_meter("k2450")
        if camera:
            ctrl.connect_camera()
        return ctrl

    yield make
    for c in made:
        c.shutdown()
    fake_visa.uninstall()
    # 히트건을 쓴 모든 시험에 공통: 시험용 히트건에 인터락 오류(송풍 먼저 → 열선)도, 송풍 단독 켜짐도 한 번도 없어야 한다
    bad = ["%s" % p for c in made for p in c.heatgun.problems()]
    if bad:
        pytest.fail("시험용 히트건 켜는 순서 문제: %s" % bad)


@pytest.fixture
def ready_ctrl(make_ctrl):
    """초기 설정이 끝난 가짜 장비: 카메라 1280×960 · 초기 설정 값(판 영역 · 빈 판 사진 · 좌표 · 헤드 마커 · 접촉 높이)을 채운 상태."""
    def make(**kw):
        opts = dict(scale=0.01, camera=True, camera_width=1280, camera_height=960)
        opts.update(kw)
        ctrl = make_ctrl(**opts)
        ctrl.lab.read_dt = 0.002
        fill_prep(ctrl)
        return ctrl
    return make
