"""window.py - 창(pywebview) · 단일 실행 뮤텍스 · 종료.

- 뮤텍스 이름은 이전 프로그램과 다르다(VANAM_WaferStageV2_SingleInstance). 이전 프로그램 뮤텍스가 잡혀 있으면 시작을 거절한다
  (COM 포트 · 카메라를 함께 쓸 수 없다).
- 우리 뮤텍스가 잡혀 있는데 보이는 창이 없으면(창 없이 남은 프로세스) 그 PID 를 알리고 --cleanup 으로 정리할 수 있게 한다.
- 종료 순서: 1 측정 · 이동 중이면 거절 -> 2 히트건 · 릴레이 정책 -> 3 save -> 4 시리얼 닫기 -> 5 카메라 놓기 -> 6 서버 정지 -> 7 창 닫기 -> 8 프로세스 끝.
  창은 UI 스레드가 webview.start() 에서 돌아온 뒤 정리한다(다른 스레드에서 프로세스를 죽이지 않는다 - WebView2 가 창 클래스를
  해제하기 전에 죽으면 "Failed to unregister class Chrome_WidgetWin_0 (1411)" 가 남고 다음 실행이 막힌다).
  정해진 시간(HARD_EXIT_S) 안에 끝나지 않으면 마지막 수단으로 프로세스 트리를 강제 종료한다. 뮤텍스는 명시적으로 놓는다.
"""

import contextlib
import os
import socket
import subprocess
import sys
import threading
import time

import logger
from core import paths

HARD_EXIT_S = 12.0
WINDOW = None
_MUTEX = None
_exiting = False
ALLOW_CLOSE = False                # 종료 순서 1~6 이 끝난 뒤에만 창 닫기를 허용한다(그 전의 X 는 화면 확인으로)


def _k32():
    import ctypes
    return ctypes.windll.kernel32


def old_program_running():
    if sys.platform != "win32":
        return False
    try:
        k = _k32()
        h = k.OpenMutexW(0x00100000, False, paths.OLD_MUTEX_NAME)      # SYNCHRONIZE
        if h:
            k.CloseHandle(h)
            return True
    except Exception:                                                  # noqa: BLE001
        pass
    return False


def acquire_mutex():
    """True = 잡았다. 이미 잡혀 있으면 False."""
    global _MUTEX
    if sys.platform != "win32":
        return True
    k = _k32()
    h = k.CreateMutexW(None, False, paths.MUTEX_NAME)
    if k.GetLastError() == 183:
        with contextlib.suppress(Exception):
            k.CloseHandle(h)
        return False
    _MUTEX = h
    return True


def release_mutex():
    global _MUTEX
    if _MUTEX is not None:
        with contextlib.suppress(Exception):
            k = _k32()
            k.ReleaseMutex(_MUTEX)
            k.CloseHandle(_MUTEX)
        _MUTEX = None


def other_instances():
    """같은 프로그램(run.py)을 돌리는 다른 파이썬 프로세스 [(pid, 명령줄)]."""
    if sys.platform != "win32":
        return []
    cmd = ("Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
           "Where-Object { $_.CommandLine -like '*wafer_stage_v2*run.py*' -or $_.CommandLine -like '*run.py*' } | "
           "ForEach-Object { \"$($_.ProcessId)|$($_.CommandLine)\" }")
    try:
        out = subprocess.run(["powershell", "-NoProfile", "-Command", cmd], capture_output=True, text=True, timeout=15).stdout
    except Exception:                                                  # noqa: BLE001
        return []
    res = []
    for ln in out.splitlines():
        if "|" in ln:
            pid, cl = ln.split("|", 1)
            if pid.strip().isdigit() and int(pid) != os.getpid():
                res.append((int(pid), cl.strip()))
    return res


def kill_tree(pid):
    with contextlib.suppress(Exception):
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(pid)], capture_output=True, timeout=15)


def cleanup_others():
    n = 0
    for pid, _cl in other_instances():
        kill_tree(pid)
        n += 1
    return n


def find_free_port(host, start, tries=10):
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


def _wait_ready(host, port, timeout_s=20.0):
    end = time.monotonic() + timeout_s
    while time.monotonic() < end:
        with contextlib.suppress(OSError):
            with socket.create_connection((host, port), 0.3):
                return True
        time.sleep(0.1)
    return False


def _hard_exit_timer():
    def run():
        time.sleep(HARD_EXIT_S)
        logger.log("종료 시간 초과 · 강제 종료", "err")
        kill_tree(os.getpid())
        os._exit(1)
    threading.Thread(target=run, daemon=True, name="hard-exit").start()


def request_exit(server=None):
    """종료 순서 6~7: 서버 정지 -> 창 닫기. (1~5 는 App.shutdown_cleanup 이 먼저 한다.)"""
    global _exiting, ALLOW_CLOSE
    if _exiting:
        return
    _exiting = True
    ALLOW_CLOSE = True
    print("[exit] 종료 요청", flush=True)
    _hard_exit_timer()
    if server is not None:
        server.should_exit = True
    if WINDOW is not None:
        with contextlib.suppress(Exception):
            WINDOW.destroy()


def run(fastapi_app, host, port, no_window, server_holder):
    """서버 스레드 + 창. 창이 닫혀 webview.start() 가 돌아오면 서버를 마저 정리하고 뮤텍스를 놓는다. 반환: 종료 코드."""
    global WINDOW
    import uvicorn

    if sys.platform == "win32" and not no_window:
        if old_program_running():
            _fail("이전 프로그램 실행 중 · 종료 후 실행", no_window)
            return 1
        if not acquire_mutex():
            others = other_instances()
            alive = ", ".join(str(p) for p, _ in others) or "확인 못 함"
            _fail("이미 실행 중 · 창 없이 남은 프로세스 PID %s · python run.py --cleanup 으로 정리" % alive, no_window)
            return 1
    port = find_free_port(host, port) or port
    cfg = uvicorn.Config(fastapi_app, host=host, port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    server.install_signal_handlers = lambda: None
    server_holder["server"] = server
    th = threading.Thread(target=server.run, name="uvicorn", daemon=True)
    th.start()
    if not _wait_ready(host, port):
        _fail("서버 시작 실패", no_window)
        release_mutex()
        return 1
    url = "http://%s:%d/" % (host, port)
    server_holder["url"] = url
    print("서버 준비 완료 %s" % url, flush=True)
    try:
        if no_window:
            while th.is_alive() and not _exiting:
                time.sleep(0.3)
        else:
            import webview
            WINDOW = webview.create_window("샘플 자동 측정", url, width=1440, height=900, maximized=True, text_select=True)
            WINDOW.events.closing += lambda: True if ALLOW_CLOSE else server_holder["on_closing"]()
            webview.start()
            print("[exit] 창 닫힘 · webview.start 반환", flush=True)
        server.should_exit = True
        th.join(8.0)
        print("[exit] 서버 스레드 끝 %s" % (not th.is_alive()), flush=True)
    finally:
        release_mutex()
    return 0


def _fail(msg, no_window):
    print("[error] " + msg, flush=True)
    logger.log(msg, "err")
    if not no_window and sys.platform == "win32":
        with contextlib.suppress(Exception):
            import ctypes
            ctypes.windll.user32.MessageBoxW(0, msg, "샘플 자동 측정", 0x10)
