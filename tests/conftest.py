"""시험 공통 준비: 가짜 장비(--fake)와 임시 데이터 폴더만 쓴다. 실제 포트 · VISA 는 열지 않는다."""

import time

import pytest

from app.config import Config
from app.control import Controller


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


@pytest.fixture
def make_ctrl(tmp_path):
    made = []

    def make(clock=None, scale=0.05, connect=True, meters=True, camera=False, **cfg_over):
        cfg = Config(tmp_path / "settings.json").load()
        cfg.update({"data_dir": str(tmp_path / "data"), "camera_width": 320, "camera_height": 240, **cfg_over})
        kw = {"clock": clock} if clock else {}
        ctrl = Controller(cfg, fake=True, fake_scale=scale, **kw)
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
