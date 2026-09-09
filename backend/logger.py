"""logger.py - 화면 로그와 함께 남기는 날짜별 파일 로그.

화면(WebSocket) 로그는 창을 닫으면 사라진다. 현장에서 "어제 그 오류" 를 다시 볼 수
있어야 하므로 같은 문장을 data/logs/YYYY-MM-DD.log 에도 남긴다.

import 단계(설정 로드 등)에서 나는 진단은 아직 파일 경로가 준비되기 전일 수 있어
early() 버퍼에 모았다가 configure() 때 한 번에 내보낸다.
"""

import os
import sys
import time

import paths

_early = []          # [(level, msg)] - configure 전에 쌓인 진단
_enabled = True
_dir = paths.LOGS_DIR

LEVELS = ("info", "ok", "warn", "err")


def early(level, msg):
    _early.append((level, str(msg)))


def drain_early():
    out = list(_early)
    _early.clear()
    return out


def configure(settings=None):
    """설정을 반영하고 early 버퍼를 파일로 flush 한다."""
    global _enabled, _dir
    s = settings or {}
    _enabled = bool(s.get("file_log", True))
    _dir = paths.LOGS_DIR
    try:
        os.makedirs(_dir, exist_ok=True)
    except OSError:
        _enabled = False
    for lv, msg in drain_early():
        write(lv, msg)


def _path():
    return os.path.join(_dir, time.strftime("%Y-%m-%d") + ".log")


def write(level, msg):
    """파일에만 쓴다. 화면으로 보내는 것은 connection.push_log 의 몫."""
    line = "%s [%s] %s" % (time.strftime("%H:%M:%S"), level, msg)
    try:
        print(line, flush=True)
    except Exception:                      # noqa: BLE001
        pass                               # 콘솔 없는 창 모드
    if not _enabled:
        return
    try:
        with open(_path(), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass                               # 로그 실패로 동작을 막지 않는다


def exc(prefix, e):
    write("err", "%s: %s: %s" % (prefix, type(e).__name__, e))
