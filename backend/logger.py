"""logger.py - 화면 로그(링 버퍼) + 날짜별 파일 로그. 화면에 가는 오류는 short() 를 거치고 원문은 파일에만 남긴다."""

import collections
import os
import threading
import time

from core import paths

_RING = collections.deque(maxlen=600)
_LOCK = threading.Lock()
_SEQ = [0]
_SUBS = []                       # fn(entry) - 서버가 ws 로 보낸다


def short(e, n=58):
    """한 줄 60자 이내 사유."""
    t = str(e).replace("\r", " ").replace("\n", " ").strip()
    return t if len(t) <= n else t[:n - 1] + "…"


def _file_line(entry):
    try:
        os.makedirs(paths.LOGS_DIR, exist_ok=True)
        fn = os.path.join(paths.LOGS_DIR, time.strftime("%Y%m%d_") + paths.LOG_NAME)
        with open(fn, "a", encoding="utf-8") as f:
            f.write("%s [%s] %s\n" % (entry["t"], entry["lv"], entry["msg"]))
    except OSError:
        pass


def log(msg, lv="info", raw=None):
    """lv: info | warn | err | tx | rx. raw 가 있으면 파일에만 원문을 남긴다."""
    with _LOCK:
        _SEQ[0] += 1
        e = {"id": _SEQ[0], "t": time.strftime("%H:%M:%S"), "lv": lv, "msg": str(msg)}
        _RING.append(e)
    _file_line(dict(e, msg=str(msg) + ((" | " + str(raw)) if raw else "")))
    for fn in list(_SUBS):
        try:
            fn(e)
        except Exception:                                    # noqa: BLE001
            pass
    return e


def recent(n=600):
    with _LOCK:
        return list(_RING)[-n:]


def subscribe(fn):
    _SUBS.append(fn)
