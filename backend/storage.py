"""storage.py - 파일 I/O (설정 저장·읽기).

atomic_write_json: 임시 파일에 쓰고 rename. 저장 중에 죽어도 settings.json 이
반쯤 쓰인 상태로 남지 않는다 (설정이 깨지면 다음 실행이 기본값으로 돌아간다).
"""

import json
import os

import logger
from core import paths

paths.ensure_data_dirs()


def atomic_write_json(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=2)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)


def safe_read_json(path):
    """읽기 실패로 죽지 않는다. 없으면 None."""
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except Exception as e:                 # noqa: BLE001
        logger.early("warn", "JSON 읽기 실패: %s (%s)" % (path, e))
        return None


def append_jsonl(path, obj):
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "a", encoding="utf-8") as f:
            f.write(json.dumps(obj, ensure_ascii=False) + "\n")
    except OSError:
        pass
