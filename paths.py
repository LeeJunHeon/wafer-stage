"""paths.py - 이 프로그램이 쓰는 경로를 한곳에서 정한다.

원칙: 코드(wafer_stage)와 데이터(촬영 결과·보정값)를 분리한다. 코드 폴더는 git
저장소이고, 데이터는 그 밖에 둔다. 옆에 measure\\ 같은 다른 프로그램이 생겨도
서로의 폴더를 몰라도 되도록, wafer_stage 는 자기 폴더와 data 폴더만 안다.

  <상위 폴더>\\
    wafer_stage\\   <- PROJECT_ROOT (여기서 python main.py 를 실행한다)
    data\\          <- DATA_DIR (settings.json 의 "data_dir", 기본 "../data")
      out\\         <- OUT_DIR   (촬영 결과 폴더 + log.jsonl)
      calib_matrix.json          <- CALIB_PATH

data_dir 이 상대경로면 PROJECT_ROOT 기준으로 푼다. 현재 작업 디렉터리에 의존하지
않으므로 어디서 실행해도 같은 곳을 가리킨다.
"""

import json
import os

PROJECT_ROOT = os.path.dirname(os.path.abspath(__file__))
SETTINGS_PATH = os.path.join(PROJECT_ROOT, "settings.json")

DEFAULT_DATA_DIR = "../data"


def _read_data_dir():
    d = DEFAULT_DATA_DIR
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f).get("data_dir", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR
    except Exception:
        pass                       # settings.json 이 없거나 깨져도 기본값으로 돈다
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(PROJECT_ROOT, d))


DATA_DIR = _read_data_dir()
OUT_DIR = os.path.join(DATA_DIR, "out")
LOG_PATH = os.path.join(OUT_DIR, "log.jsonl")
CALIB_PATH = os.path.join(DATA_DIR, "calib_matrix.json")


def resolve_out(out_dir):
    """settings 의 out_dir 를 실제 경로로. 상대경로면 DATA_DIR 밑으로 둔다.

    (--set out_dir=out_test 처럼 임시 폴더를 지정해도 데이터 폴더 안에 생긴다.)
    """
    out_dir = str(out_dir or "out")
    if os.path.isabs(out_dir):
        return out_dir
    return os.path.normpath(os.path.join(DATA_DIR, out_dir))


def ensure_data_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(OUT_DIR, exist_ok=True)
