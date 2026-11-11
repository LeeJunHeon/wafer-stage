"""config.py - settings.json 읽기 · 쓰기 · 기본값 · 검증.

설정은 ITEMS 표 한 곳에 모은다(이름 · 기본값 · 한국어 이름 · 단위 · 범위). 코드 다른 곳에 숫자를 박지 않는다.
settings.json 은 장비마다 다른 로컬 파일이라 git 에 넣지 않는다. 없으면 기본값으로 시작한다.
기본값 중 "실장비 확인 필요" 표시는 측정 PC 에서 확인하기 전의 값이다.
"""

import copy
import json
import os
import tempfile
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Optional

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "settings.json"
FAKE_PATH = ROOT / "settings_fake.json"            # --fake 는 실제 값과 섞이지 않게 따로 쓴다
FAKE_DATA_DIR = "../data_v3_fake"


@dataclass(frozen=True)
class Item:
    key: str
    default: Any
    kind: str                       # str | int | float | bool | grid | xy(비어 있을 수 있는 [x, y] mm)
    label: str                      # 화면에 보일 한국어 이름
    unit: str = ""
    min: Optional[float] = None
    max: Optional[float] = None


ITEMS = [
    # --- 연결 ---
    Item("stage_port", "COM5", "str", "스테이지 포트", ""),                      # 측정 PC 는 COM5. 자동으로 찾지 않는다
    Item("k2110_addr", "USB0::0x05E6::0x2110::8017862::INSTR", "str", "K2110 주소"),
    Item("k2450_addr", "GPIB0::18::INSTR", "str", "K2450 주소"),
    Item("data_dir", "../data_v3", "str", "데이터 폴더"),
    Item("auto_connect", True, "bool", "시작할 때 자동 연결"),                          # 저장된 포트 · 주소 하나씩만 연다(찾아다니지 않는다)
    # --- 카메라(실장비 기본값: 장치 1 · 4080×3060 · MJPG · 초점 고정) ---
    Item("camera_index", 1, "int", "카메라 장치 번호", "번", 0, 9),                      # 0 번은 노트북 내장 카메라
    Item("camera_width", 4080, "int", "카메라 가로", "px", 160, 10000),
    Item("camera_height", 3060, "int", "카메라 세로", "px", 120, 10000),
    Item("camera_fourcc", "MJPG", "str", "카메라 영상 포맷"),
    Item("camera_focus_fixed", True, "bool", "초점 고정"),
    Item("camera_focus_value", -1, "int", "고정 초점 값(-1 = 카메라에 맡김)", "", -1, 1000),    # 실장비 확인 필요
    Item("preview_width", 1280, "int", "미리보기 가로", "px", 160, 4096),
    # 사진 찍을 때 헤드가 서는 자리. 준비 3단계에서 정한다(비어 있으면 None)
    Item("photo_head_xy", None, "xy", "사진 찍을 때 헤드 자리", "mm"),
    # --- 이동 범위(0 = 모터 쪽 끝) ---
    Item("x_max_mm", 247.6, "float", "X 가동 범위", "mm", 1, 400),
    Item("y_max_mm", 247.8, "float", "Y 가동 범위", "mm", 1, 400),
    Item("z_max_mm", 45.0, "float", "Z 가동 범위", "mm", 1, 100),
    # --- 속도(펌웨어 기본값과 같다 - 다르면 연결할 때 보낸다) ---
    Item("move_speed_pps", 6000, "int", "이동 속도", "펄스/s", 100, 20000),
    Item("move_accel_pps2", 40000, "int", "가속", "펄스/s²", 1000, 200000),
    Item("move_start_pps", 300, "int", "시작 속도", "펄스/s", 50, 5000),
    Item("watchdog_ms", 2000, "int", "통신 감시 시간", "ms", 500, 60000),
    # --- 히트건(R1) ---
    Item("heater_relay", 1, "int", "히트건 릴레이", "번", 1, 10),
    Item("heater_max_c", 110.0, "float", "히트건 차단 온도", "°C", 30, 300),
    Item("heater_max_on_s", 180.0, "float", "히트건 최대 켜짐 시간", "s", 1, 3600),
    Item("heater_min_off_s", 10.0, "float", "히트건 다시 켜기까지 쉬는 시간", "s", 0, 600),
    Item("temp_stale_s", 3.0, "float", "온도 못 읽고 버티는 시간(켜진 동안)", "s", 0.5, 60),
    # 쿨러 3x3 자리 배치(판 사각형 위에서 본 칸 → 릴레이 번호). 실제 배치는 아직 모른다 → 번호순 기본값(실장비 확인 필요)
    Item("cooler_grid", [[2, 3, 4], [5, 6, 7], [8, 9, 10]], "grid", "쿨러 자리 배치(릴레이 번호)"),
    # --- K2450 저항 측정 ---
    Item("meter_volt", 1.0, "float", "측정 전압", "V", 0.01, 20),
    Item("meter_four_wire", True, "bool", "4단자 측정"),
    Item("meter_avg_count", 10, "int", "평균 횟수", "회", 1, 100),
    Item("meter_nplc", 1.0, "float", "적분 시간(NPLC)", "", 0.01, 10),
    Item("meter_ilim_a", 0.01, "float", "전류 한도", "A", 1e-8, 1.0),                 # 실장비 확인 필요
    Item("contact_volt", 0.1, "float", "접촉 확인 전압", "V", 0.01, 5),
    Item("contact_ilim_a", 1e-4, "float", "접촉 확인 전류 한도", "A", 1e-8, 1.0),
]
BY_KEY = {i.key: i for i in ITEMS}


class ConfigError(ValueError):
    """잘못된 설정 값. 문구는 사람이 읽는 한국어 한 문장이다."""


def check(item: Item, value: Any) -> Any:
    """한 항목 검증. 통과한 값(형 변환 후)을 돌려주고 못 쓰면 ConfigError."""
    name = item.label
    if item.kind == "str":
        if not isinstance(value, str):
            raise ConfigError("%s: 글자여야 합니다" % name)
        return value.strip()
    if item.kind == "bool":
        if not isinstance(value, bool):
            raise ConfigError("%s: 켬/끔 값이어야 합니다" % name)
        return value
    if item.kind == "grid":
        ok = isinstance(value, list) and len(value) == 3 and all(isinstance(r, list) and len(r) == 3 for r in value)
        nums = [n for r in value for n in r] if ok else []
        if not ok or not all(isinstance(n, int) and not isinstance(n, bool) and 2 <= n <= 10 for n in nums) or len(set(nums)) != 9:
            raise ConfigError("%s: 3×3 칸에 릴레이 번호 2~10 을 겹치지 않게 넣어야 합니다" % name)
        return [list(r) for r in value]
    if item.kind == "xy":
        if value is None:
            return None
        if not (isinstance(value, list) and len(value) == 2 and all(isinstance(n, (int, float)) and not isinstance(n, bool) for n in value)):
            raise ConfigError("%s: [X, Y] 두 숫자이거나 비워 두어야 합니다" % name)
        return [float(value[0]), float(value[1])]
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError("%s: 숫자여야 합니다" % name)
    v = float(value)
    if v != v or v in (float("inf"), float("-inf")):
        raise ConfigError("%s: 숫자여야 합니다" % name)
    if item.min is not None and v < item.min:
        raise ConfigError("%s: %g%s 이상이어야 합니다" % (name, item.min, item.unit))
    if item.max is not None and v > item.max:
        raise ConfigError("%s: %g%s 이하여야 합니다" % (name, item.max, item.unit))
    return int(v) if item.kind == "int" else v


def defaults() -> dict:
    return {i.key: copy.deepcopy(i.default) for i in ITEMS}


class Config:
    def __init__(self, path: Optional[os.PathLike] = None, fake: bool = False):
        """fake = 가짜 장비용: 파일은 settings_fake.json, 데이터 폴더 기본값은 ../data_v3_fake (실제 값과 섞이지 않는다)."""
        self.fake = bool(fake)
        self.path = Path(path) if path else (FAKE_PATH if fake else DEFAULT_PATH)
        self.values = self._defaults()
        self.loaded_notes = []                       # 읽을 때 무시한 값(화면 · 로그용)

    def _defaults(self):
        d = defaults()
        if self.fake:
            d["data_dir"] = FAKE_DATA_DIR
        return d

    # ---- 읽기 · 쓰기 ----
    def load(self):
        self.values = self._defaults()
        self.loaded_notes = []
        if not self.path.exists():
            return self
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as e:
            self.loaded_notes.append("설정 파일을 읽지 못해 기본값으로 시작합니다 (%s)" % str(e)[:40])
            return self
        if not isinstance(raw, dict):
            self.loaded_notes.append("설정 파일 모양이 달라 기본값으로 시작합니다")
            return self
        for k, v in raw.items():
            item = BY_KEY.get(k)
            if item is None:
                continue                              # 모르는 키(예전 설정)는 무시
            try:
                self.values[k] = check(item, v)
            except ConfigError as e:
                self.loaded_notes.append("%s · 기본값 사용" % e)
        return self

    def save(self):
        """원자적으로 쓴다(임시 파일 → 바꿔치기)."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(self.path.parent), prefix=".settings.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(self.values, f, ensure_ascii=False, indent=2)
            os.replace(tmp, self.path)
        finally:
            if os.path.exists(tmp):
                os.unlink(tmp)

    # ---- 값 ----
    def get(self, key: str):
        return self.values[key]

    def __getitem__(self, key: str):
        return self.values[key]

    def update(self, patch: dict):
        """전부 검증을 통과해야 바뀐다(일부만 바뀌지 않는다)."""
        new = {}
        for k, v in patch.items():
            item = BY_KEY.get(k)
            if item is None:
                raise ConfigError("알 수 없는 설정: %s" % k)
            new[k] = check(item, v)
        self.values.update(new)

    def data_path(self) -> Path:
        p = Path(self.values["data_dir"])
        return p if p.is_absolute() else (ROOT / p).resolve()

    def describe(self):
        """화면용 설정 표."""
        return [{"key": i.key, "label": i.label, "unit": i.unit, "value": self.values[i.key], "default": i.default} for i in ITEMS]
