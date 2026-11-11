"""logs.py - 로그 파일. <데이터 폴더>/logs/YYYY-MM-DD/{app,serial,visa}.log 에 한 줄씩 남긴다(주고받은 줄 포함).

쓰기에 실패해도 측정 · 제어는 멈추지 않는다.
"""

import threading
import time
from pathlib import Path
from typing import Optional

MAX_BYTES = 20 * 1024 * 1024


def _clip(text, n=240):
    t = str(text).replace("\r", "\\r").replace("\n", "\\n")
    return t if len(t) <= n else t[:n] + "…"


class Logs:
    def __init__(self, data_dir: Optional[Path]):
        self.root = Path(data_dir) / "logs" if data_dir else None
        self._lock = threading.Lock()
        self.recent = []                              # 화면용 최근 줄(app)

    def _write(self, name: str, line: str):
        if self.root is None:
            return
        try:
            with self._lock:
                d = self.root / time.strftime("%Y-%m-%d")
                d.mkdir(parents=True, exist_ok=True)
                fn = d / (name + ".log")
                if fn.exists() and fn.stat().st_size > MAX_BYTES:
                    fn.replace(d / (name + ".1.log"))
                with open(fn, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    @staticmethod
    def _now():
        t = time.time()
        return "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(t)), int(t * 1000) % 1000)

    def serial(self, arrow: str, text: str):
        """arrow: '->' 보낸 줄 · '<-' 받은 줄."""
        self._write("serial", "%s %s %s" % (self._now(), arrow, _clip(text)))

    def visa(self, name: str, addr: str, op: str, cmd: str, resp, dt: float, exc):
        line = "%s %s %s %s %s" % (self._now(), name, addr, op, _clip(cmd))
        if resp is not None:
            line += " -> " + _clip(resp)
        line += " (%.1f ms)" % (dt * 1000.0)
        if exc is not None:
            line += " !! %s: %s" % (type(exc).__name__, _clip(exc))
        self._write("visa", line)

    def app(self, text: str, level: str = "info"):
        line = "%s [%s] %s" % (self._now(), level, text)
        self._write("app", line)
        self.recent.append(line)
        del self.recent[:-200]
