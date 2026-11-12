"""시험 공통 준비: 가짜 장비(tests/fakes/)와 임시 데이터 폴더만 쓴다. 실제 포트 · VISA · 카메라는 열지 않는다.

프로그램(app/)에는 가짜 장비가 없다. 시험이 Controller 의 "장비를 꽂는 자리"에 가짜를 꽂는다:
  open_port(스테이지 포트 여는 함수) → FakeV11 · meters.RM_FACTORY → 가짜 VISA(Lab) · camera.open_capture → 가짜 카메라 · clock · sleep → 시간 배율.
가짜 세계(World) 하나를 가짜 카메라 · 가짜 K2450 · 열 모델이 같이 본다. 시험은 ctrl.fake_fw · ctrl.lab · ctrl.world 로 가짜 장비를 들여다본다.
"""

import time

import pytest

from app import control as control_mod
from app.config import Config
from app.control import Controller
from app.devices import meters
from app.devices.stage import PPMM
from tests.fakes import fake_visa
from tests.fakes.fake_camera import open_fake
from tests.fakes.fake_v11 import FakeV11
from tests.fakes.world import World


class FakeClock:
    """안전 규칙의 시간을 시험이 직접 건너뛰게 한다."""

    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, s):
        self.t += s


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


def build_ctrl(cfg, clock=None, scale=0.05, time_scale=1.0):
    """가짜 장비를 꽂은 Controller. scale = 가짜 스테이지의 이동 시간 배율(작을수록 빠름) · time_scale = 열 모델 · 측정 · 안전 시계의 배율."""
    k = float(time_scale)
    base = (lambda: time.monotonic() * k) if k != 1.0 else time.monotonic        # 가짜 열 모델의 시계
    sleep = (lambda s: time.sleep(s / k)) if k != 1.0 else time.sleep
    world = World()
    box = {"fw": None, "ctrl": None}
    fw = lambda: box["fw"]                                          # noqa: E731
    heater = int(cfg["heater_relay"]) - 1
    lab = fake_visa.Lab(clock=base, sleep=sleep, world=world,
                        heater_fn=lambda: bool(fw() and fw().relay[heater]),
                        cooler_fn=lambda: bool(fw() and any(r for i, r in enumerate(fw().relay) if i != heater)),
                        relays_fn=lambda: [i + 1 for i, r in enumerate(fw().relay) if r] if fw() else [],
                        pos_fn=lambda: (fw().true_xy() + (fw().real["z"] / PPMM,)) if fw() else None)
    lab.read_dt = 0.02                                              # 가짜 읽기 시간(실장비는 길다)
    lab.addrs = ["ASRL5::INSTR", "ASRL4::INSTR", cfg["k2110_addr"], cfg["k2450_addr"]]
    fake_visa.install(lab)                                          # meters.RM_FACTORY 에 꽂는다

    def open_port(name):                                            # 스테이지 포트 여는 자리: 열 때마다 새 가짜 보드
        box["fw"] = FakeV11(time_scale=scale, homed=True)
        box["ctrl"].fake_fw = box["fw"]
        return box["fw"]

    ctrl = Controller(cfg, clock=clock or base, sleep=sleep, open_port=open_port)
    box["ctrl"] = ctrl
    ctrl.camera.open_capture = open_fake(lambda: fw().true_xy() if fw() else (0.0, 0.0), world=world)
    ctrl.fake_fw, ctrl.lab, ctrl.world = None, lab, world            # 시험이 가짜 장비를 들여다보는 손잡이(프로그램은 쓰지 않는다)
    return ctrl


def fill_prep(ctrl):
    """준비 2~7단계를 하나씩 하지 않고 가짜 세계의 참값으로 준비 값을 채운다(원점 · 촬영 위치 · 판 영역 · 빈 판 사진 · 좌표 · 헤드 마커 · 접촉 높이 · 쿨러)."""
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
    monkeypatch.setattr(control_mod, "serial_ports", lambda: ["COM5"])       # 포트 목록: 실제 포트를 훑지 않는다

    def make(clock=None, scale=0.05, connect=True, meters=True, camera=False, time_scale=1.0, **cfg_over):
        cfg = Config(tmp_path / "settings.json").load()
        cfg.update({"data_dir": str(tmp_path / "data"), "camera_width": 320, "camera_height": 240, **cfg_over})
        ctrl = build_ctrl(cfg, clock=clock, scale=scale, time_scale=time_scale)
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


@pytest.fixture
def ready_ctrl(make_ctrl):
    """준비가 끝난 가짜 장비: 카메라 1280×960 · 준비 값(판 영역 · 빈 판 사진 · 좌표 · 헤드 마커 · 접촉 높이)을 채운 상태."""
    def make(**kw):
        opts = dict(scale=0.02, camera=True, camera_width=1280, camera_height=960)
        opts.update(kw)
        ctrl = make_ctrl(**opts)
        ctrl.lab.read_dt = 0.002
        fill_prep(ctrl)
        return ctrl
    return make
