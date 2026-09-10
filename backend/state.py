"""state.py - 서버 상태의 단일 주인 + settings.json 로드/저장.

서버가 상태의 주인이다. 화면은 여기서 만든 snapshot() 을 받아 그리기만 한다.
화면이 스스로 값을 지어내지 않으므로 "화면은 바뀌었는데 장비는 그대로" 가 없다.
"""

import time

from core import calib, detect, paths
from core import stage as stage_mod

import logger
import version
from storage import atomic_write_json, safe_read_json

# 앱이 편집하는 설정 키만 여기에 기본값을 둔다. 검출 파라미터(detect.DEFAULTS)는
# settings.json 에 그대로 있고 화면에서 건드리지 않는다.
APP_KEYS = ("serial_port", "camera_index", "park_xy", "dwell_s", "marker_mm_xy", "measure")

DEFAULT_APP = {
    "serial_port": None,
    "camera_index": 1,
    "park_xy": [0, 90],
    "dwell_s": 5,
    "marker_mm_xy": {"3": [15, 20], "2": [15, 160], "1": [201, 19], "0": [201, 160]},
    "measure": {"driver": "dummy"},
}


class State:
    def __init__(self):
        self.settings = {}
        self.params = {}                   # detect/calib 에 넘기는 전체 파라미터
        self.load_settings()

        self.stage = {"connected": False, "port": None, "homed_x": False, "homed_y": False,
                      "x_mm": None, "y_mm": None, "u": None, "v": None,
                      "moving": False, "dirty": False, "needs_home": False,
                      "last_error": ""}
        self.camera = {"index": self.settings.get("camera_index", 1), "ok": None,
                       "last_error": "", "capturing": False}
        self.frame = None                  # {"id","ts","w","h","url"}
        self.calib = None                  # {"refit","used_ids",...}
        self.sensing = None                # {"rect":[u0,v0,u1,v1]}
        self.wafer = None                  # {"found","cx","cy","r_px","center_mm"}
        self.markers = {}                  # {id: [[u,v] x4]}
        self.samples = []                  # 화면용 샘플 목록
        self.sequence = {"phase": "idle", "mode": "auto",
                         "dwell_s": float(self.settings.get("dwell_s", 5)),
                         "cur_no": None, "done": 0, "total": 0, "elapsed_s": 0,
                         "out_dir": None, "message": ""}
        self.warnings = []
        self.startup_notices = []

    # ---------------- settings ----------------
    def load_settings(self):
        """settings.json 하나가 설정의 단일 출처. 없으면 기본값으로 만든다."""
        p = dict(detect.DEFAULTS)
        p.update(DEFAULT_APP)
        got = safe_read_json(paths.SETTINGS_PATH)
        if got:
            p.update(got)
        else:
            try:
                atomic_write_json(paths.SETTINGS_PATH, p)
                logger.early("info", "settings.json 을 기본값으로 만들었습니다")
            except OSError as e:
                logger.early("warn", "settings.json 을 만들지 못했습니다: %s" % e)
        self.params = p
        self.settings = {k: p.get(k, DEFAULT_APP[k]) for k in APP_KEYS}
        calib.set_marker_mm(self.settings.get("marker_mm_xy"))
        return p

    def save_settings(self, patch):
        """앱이 편집하는 키만 반영해 저장한다 (검출 파라미터는 건드리지 않는다)."""
        cur = safe_read_json(paths.SETTINGS_PATH) or dict(self.params)
        for k in APP_KEYS:
            if k in patch:
                cur[k] = patch[k]
        atomic_write_json(paths.SETTINGS_PATH, cur)
        self.load_settings()
        self.camera["index"] = self.settings.get("camera_index", 1)
        self.sequence["dwell_s"] = float(self.settings.get("dwell_s", 5))

    # ---------------- 편의 ----------------
    def park_xy(self):
        xy = self.settings.get("park_xy") or [0, 90]
        return [float(xy[0]), float(xy[1])]

    def sample(self, no):
        for s in self.samples:
            if s["no"] == no:
                return s
        return None

    def can_move(self):
        """이동 명령을 받아도 되는 상태인가. 사유 문자열 또는 None."""
        if not self.stage["connected"]:
            return "스테이지 미연결 - 연결 후 사용하세요"
        if not (self.stage["homed_x"] and self.stage["homed_y"]):
            return "원점 없음 - 원점잡기(fz) 후 사용하세요"
        if self.stage.get("needs_home"):
            # 비상정지 뒤에는 펌웨어가 위치를 안다고 해도 믿을 수 없다.
            return "비상정지 후 위치를 신뢰할 수 없습니다 - 원점잡기(fz) 후 사용하세요"
        return None

    def set_warnings(self, ws):
        self.warnings = list(ws or [])

    # ---------------- snapshot ----------------
    def snapshot(self):
        return {
            "type": "state",
            "version": {"name": version.APP_NAME, "version": version.APP_VERSION,
                        "build": version.BUILD_DATE},
            "stage": dict(self.stage),
            "camera": dict(self.camera),
            "frame": dict(self.frame) if self.frame else None,
            "calib": dict(self.calib) if self.calib else None,
            "sensing": dict(self.sensing) if self.sensing else None,
            "wafer": dict(self.wafer) if self.wafer else None,
            "markers": {str(k): v for k, v in (self.markers or {}).items()},
            # 가동범위. 화면의 스테이지 맵이 축척을 잡는 데 쓴다(펌웨어 상수에서 계산).
            "limits": {"x_max_mm": round(stage_mod.X_MAX_PULSE / stage_mod.PPMM, 1),
                       "y_max_mm": round(stage_mod.Y_MAX_PULSE / stage_mod.PPMM, 1)},
            "samples": [dict(s) for s in self.samples],
            "sequence": dict(self.sequence, estopped=_estopped()),
            "warnings": list(self.warnings),
            "settings": dict(self.settings),
            "data_dir": paths.DATA_DIR,
            "ts": time.time(),
        }


def _estopped():
    """engine 은 state 를 import 하므로(계층) 여기서만 지연 import 한다."""
    try:
        import engine
        return bool(engine.estopped())
    except Exception:                      # noqa: BLE001
        return False


state = State()
