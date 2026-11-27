"""logs.py - 로그 파일. <데이터 폴더>/logs/YYYY-MM-DD/ 에 한 줄씩 남긴다(설정과 상관없이 항상):
  app.log      프로그램이 한 일 · 경보 · 측정 시작 · 조건 · 샘플 끝 요약 · 설정 변경(파일을 새로 만들 때 첫 줄 = 프로그램 버전)
  serial.log   스테이지와 주고받은 줄
  visa.log     K2110 · K2450 과 주고받은 줄
  command.log  run_command 를 지난 명령 한 줄씩(이름 · 인자 · 완료 / 거절 · 문구 / 오류 · 종류 · 걸린 ms) · PUT /api/settings
  error.log    예상 못 한 예외의 traceback 전체(시각 · 어디서) - app.log 의 한 줄은 그대로 남긴다

20 MB 를 넘으면 비어 있는 다음 번호(<이름>.1.log · .2.log …)로 옮긴다(지우지 않는다).
쓰기에 실패해도 측정 · 제어는 멈추지 않는다 - 실패 횟수 · 마지막 까닭을 기억하고 on_health(ok, 까닭) 로 알린다(Controller 가 경보를 올리고 내린다).
시각은 잠금 안에서 만든다(파일의 줄 순서와 시각 순서가 같게).
"""

import os
import platform
import re
import subprocess
import sys
import threading
import time
import traceback
from pathlib import Path
from typing import Callable, Optional

MAX_BYTES = 20 * 1024 * 1024
ROOT = Path(__file__).resolve().parent.parent                       # 프로그램 폴더(git 저장소)
GIT_TIMEOUT_S = 2.0

_version_lock = threading.Lock()
_version = None


def _clip(text, n=240):
    t = str(text).replace("\r", "\\r").replace("\n", "\\n")
    return t if len(t) <= n else t[:n] + "…"


def _git(*args):
    """git 명령 한 번(시간 제한 2초 · 콘솔 창 안 뜨게). 실패하면 None."""
    kw = {}
    if os.name == "nt":
        kw["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
    try:
        r = subprocess.run(["git", "-C", str(ROOT)] + list(args), capture_output=True, text=True, timeout=GIT_TIMEOUT_S, **kw)
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if r.returncode != 0:
        return None
    return r.stdout.strip()


def _git_files():
    """git 을 못 쓸 때: .git 을 직접 읽어 커밋 해시(짧게). 못 읽으면 None."""
    g = ROOT / ".git"
    try:
        head = (g / "HEAD").read_text(encoding="utf-8").strip()
        if head.startswith("ref:"):
            ref = head.split(":", 1)[1].strip()
            p = g / ref
            if p.is_file():
                return p.read_text(encoding="utf-8").strip()[:7]
            packed = g / "packed-refs"
            if packed.is_file():
                for ln in packed.read_text(encoding="utf-8").splitlines():
                    if ln.endswith(" " + ref):
                        return ln.split()[0][:7]
            return None
        return head[:7] if re.match(r"^[0-9a-f]{7,}$", head) else None
    except OSError:
        return None


def version_text() -> str:
    """프로그램 버전 "15ec76d · 2026-11-26 · 고친 파일 있음" - git → .git 직접 읽기 → "버전 모름". 한 번 구해 기억한다."""
    global _version
    with _version_lock:
        if _version is not None:
            return _version
        h = _git("rev-parse", "--short", "HEAD")
        if h:
            date = _git("log", "-1", "--format=%cs") or ""
            st = _git("status", "--porcelain", "--untracked-files=no")
            parts = [h] + ([date] if date else []) + (["고친 파일 있음"] if st else [] if st is not None else ["고친 파일 확인 못 함"])
            _version = " · ".join(parts)
        else:
            h = _git_files()
            _version = "%s · 날짜 모름" % h if h else "버전 모름"
        return _version


class Logs:
    def __init__(self, data_dir: Optional[Path]):
        self.root = Path(data_dir) / "logs" if data_dir else None
        self._lock = threading.Lock()
        self.recent = []                              # 화면용 최근 줄(app)
        self.fail_n = 0                               # 쓰기 실패 횟수
        self.fail_why = ""                            # 마지막 쓰기 실패 까닭
        self._failing = False
        self.on_health: Optional[Callable] = None     # on_health(ok: bool, 까닭) - 실패가 시작될 때 · 다시 써질 때 한 번씩

    # ---- 쓰기 ----
    @staticmethod
    def _stamp(t):
        return "%s.%03d" % (time.strftime("%H:%M:%S", time.localtime(t)), int(t * 1000) % 1000)

    @staticmethod
    def _rotate(d, name):
        """<이름>.log → 비어 있는 다음 번호 <이름>.<n>.log(이전 번호 파일을 덮어쓰지 않는다)."""
        n = 1
        while (d / ("%s.%d.log" % (name, n))).exists():
            n += 1
        (d / (name + ".log")).replace(d / ("%s.%d.log" % (name, n)))

    def _write(self, name: str, fmt, mirror=None):
        """fmt(시각 글) → 쓸 줄. 시각은 잠금 안에서 만든다. mirror = 줄을 받아 더 할 일(app 의 recent)."""
        if self.root is None:
            return None
        ok, why, line = True, "", None
        with self._lock:
            t = time.time()
            line = fmt(self._stamp(t))
            if mirror is not None:
                mirror(line)
            try:
                d = self.root / time.strftime("%Y-%m-%d", time.localtime(t))
                d.mkdir(parents=True, exist_ok=True)
                fn = d / (name + ".log")
                if fn.exists() and fn.stat().st_size > MAX_BYTES:
                    self._rotate(d, name)
                new = not fn.exists()
                with open(fn, "a", encoding="utf-8") as f:
                    if new and name == "app":                       # 그날 첫 줄 · 번호 넘김 뒤: 프로그램 버전
                        f.write("%s [info] 프로그램 버전 · %s\n" % (self._stamp(t), version_text()))
                    f.write(line + "\n")
            except OSError as e:
                ok, why = False, "%s: %s" % (type(e).__name__, str(e)[:80])
                self.fail_n += 1
                self.fail_why = why
            changed = ok == self._failing                           # 실패가 시작됐다 · 다시 써졌다
            self._failing = not ok
        if changed and self.on_health is not None:
            try:
                self.on_health(ok, why)
            except Exception:                                       # noqa: BLE001 - 알림이 로그 쓰기를 막지 않는다
                pass
        return line

    def serial(self, arrow: str, text: str):
        """arrow: '->' 보낸 줄 · '<-' 받은 줄."""
        self._write("serial", lambda ts: "%s %s %s" % (ts, arrow, _clip(text)))

    def visa(self, name: str, addr: str, op: str, cmd: str, resp, dt: float, exc):
        tail = ""
        if resp is not None:
            tail += " -> " + _clip(resp)
        tail += " (%.1f ms)" % (dt * 1000.0)
        if exc is not None:
            tail += " !! %s: %s" % (type(exc).__name__, _clip(exc))
        self._write("visa", lambda ts: "%s %s %s %s %s%s" % (ts, name, addr, op, _clip(cmd), tail))

    def app(self, text: str, level: str = "info"):
        def keep(line):
            self.recent.append(line)
            del self.recent[:-200]
        if self.root is None:
            keep("%s [%s] %s" % (self._stamp(time.time()), level, text))
            return
        self._write("app", lambda ts: "%s [%s] %s" % (ts, level, text), mirror=keep)

    def command(self, name: str, args, result: str, ms: float):
        """command.log 한 줄: 명령 이름 · 인자(240자에서 자름) · 결과(완료 / 거절 · 문구 / 오류 · 종류) · 걸린 ms."""
        a = "" if not args else _clip(args if isinstance(args, str) else repr(args))
        self._write("command", lambda ts: "%s %s %s · %s · %.0f ms" % (ts, name, a or "-", _clip(result, 160), ms))

    def exc(self, where: str, e: Optional[BaseException] = None):
        """error.log: 예상 못 한 예외의 traceback 전체(시각 · 어디서). e 가 없으면 지금 처리 중인 예외."""
        if e is None:
            tb = traceback.format_exc()
        else:
            tb = "".join(traceback.format_exception(type(e), e, e.__traceback__))
        self._write("error", lambda ts: "%s [%s]\n%s" % (ts, where, tb.rstrip("\n")))

    def start_line(self, data_dir):
        """프로그램 시작 한 줄(app.log): 버전 · Python · OS · 데이터 폴더."""
        self.app("프로그램 시작 · 버전 %s · Python %s · %s · 데이터 폴더 %s" % (
            version_text(), platform.python_version(), platform.platform(terse=True), data_dir))

    def install_hooks(self):
        """sys.excepthook · threading.excepthook 를 error.log 로도 보낸다(원래 hook 도 그대로 부른다)."""
        old_sys, old_th = sys.excepthook, threading.excepthook

        def sys_hook(tp, val, tb):
            try:
                self.exc("처리 안 된 예외 · 주 스레드", val)
            finally:
                old_sys(tp, val, tb)

        def th_hook(a):
            try:
                if a.exc_value is not None:
                    self.exc("처리 안 된 예외 · 스레드 %s" % (a.thread.name if a.thread else "?"), a.exc_value)
            finally:
                old_th(a)

        sys.excepthook = sys_hook
        threading.excepthook = th_hook
