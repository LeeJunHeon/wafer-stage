r"""paths.py - 경로를 한곳에서 정한다. 코드(wafer_stage_v2, git)와 데이터(..\data_v2, git 밖)를 분리한다.

  <상위 폴더>\
    wafer_stage_v2\   <- PROJECT_ROOT (python run.py)
    data_v2\          <- DATA_DIR (설정 data_dir, 기본 ../data_v2)
      calib.json            좌표 보정 · 판 영역 · 제외 영역 (기준 프레임)
      head_calib.json       헤드 위치 보정 (기준 프레임)
      plate_ref.png/.json   빈 판 사진 + 메타
      plate_ref_history\   이전 빈 판 사진 (10개)
      shots\<시각>\       촬영 폴더 (raw.png · samples.json · annotated.jpg)
      runs\<시각>_<측정 조건>\   측정 한 번 = 폴더 하나
      logs\  settings_backup\  truth\

환경변수(이전 프로그램과 이름이 다르다): WAFER2_DATA(데이터 폴더) · WAFER2_SETTINGS(설정 파일) - 검증이 임시 폴더로 돌기 위한 통로.
이전 프로그램의 ..\data 는 읽기만 한다(정답 사진 복사 · 설정 가져오기).
"""

import json
import os

ENV_DATA_DIR = "WAFER2_DATA"
ENV_SETTINGS = "WAFER2_SETTINGS"
MUTEX_NAME = "VANAM_WaferStageV2_SingleInstance"
OLD_MUTEX_NAME = "VANAM_WaferStage_SingleInstance"          # 이전 프로그램 - 잡혀 있으면 시작을 거절한다
LOG_NAME = "wafer_stage_v2.log"

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETTINGS_PATH = os.environ.get(ENV_SETTINGS) or os.path.join(PROJECT_ROOT, "settings.json")
DEFAULT_DATA_DIR = "../data_v2"


def resolve(d):
    return d if os.path.isabs(d) else os.path.normpath(os.path.join(PROJECT_ROOT, d))


def _read_data_dir():
    env = os.environ.get(ENV_DATA_DIR)
    if env:
        return resolve(env)
    d = DEFAULT_DATA_DIR
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            d = json.load(f).get("data_dir", DEFAULT_DATA_DIR) or DEFAULT_DATA_DIR
    except Exception:
        pass
    return resolve(d)


DATA_DIR = _read_data_dir()
SHOTS_DIR = os.path.join(DATA_DIR, "shots")
RUNS_DIR = os.path.join(DATA_DIR, "runs")
LOGS_DIR = os.path.join(DATA_DIR, "logs")
SETTINGS_BACKUP_DIR = os.path.join(DATA_DIR, "settings_backup")
CALIB_PATH = os.path.join(DATA_DIR, "calib.json")
HEAD_CALIB_PATH = os.path.join(DATA_DIR, "head_calib.json")
REF_PATH = os.path.join(DATA_DIR, "plate_ref.png")
REF_META_PATH = os.path.join(DATA_DIR, "plate_ref.json")
REF_HISTORY_DIR = os.path.join(DATA_DIR, "plate_ref_history")
OLD_DATA_DIR = resolve("../data")                           # 읽기 전용
