"""window.py - pywebview 창 생성·종료 처리.

server.py 의 진입점에서만 쓴다. server.py 를 import 하지 않는다(순환 방지) -
FastAPI 앱과 호스트/포트는 인자로 받는다.

납품 대응:
  - 이중 실행 방지(뮤텍스). 인스턴스가 둘이면 둘 다 한 시리얼에 붙으려다 실패한다.
  - 8000 포트가 이미 쓰이면 8001~ 로 대체(빈 창 방지).
  - 창 X → 앱 종료 확인 모달로 되묻는다(파킹·save 없이 끊기면 위치를 잃는다).
"""

import contextlib
import os
import socket
import sys
import threading
import time

import logger

WINDOW = None
_allow_close = False
_MUTEX_HANDLE = None
_SERVER_ERROR = ""


def _msgbox(title, msg):
    try:
        import ctypes
        ctypes.windll.user32.MessageBoxW(0, msg, title, 0x10)
    except Exception:                      # noqa: BLE001
        print("[%s] %s" % (title, msg))


def _acquire_single_instance():
    """이미 떠 있으면 False. 비Windows(개발)는 항상 True.
    방지 장치 자체가 이유가 되어 실행을 막으면 안 되므로 예외 시 True."""
    if sys.platform != "win32":
        return True
    global _MUTEX_HANDLE
    if _MUTEX_HANDLE is not None:
        return True                        # 내가 이미 잡았다(재진입)
    try:
        import ctypes
        k32 = ctypes.windll.kernel32
        h = k32.CreateMutexW(None, False, "VANAM_WaferStage_SingleInstance")
        if k32.GetLastError() == 183:      # ERROR_ALREADY_EXISTS
            with contextlib.suppress(Exception):
                k32.CloseHandle(h)
            _MUTEX_HANDLE = None
            return False
        _MUTEX_HANDLE = h
        return True
    except Exception:                      # noqa: BLE001
        return True


def find_free_port(host, start, tries=10):
    for p in range(start, start + tries):
        with socket.socket() as s:
            try:
                s.bind((host, p))
                return p
            except OSError:
                continue
    return None


def _wait_server_ready(host, port, timeout_s=20.0):
    """소켓이 열릴 때까지 기다린다. 창이 먼저 뜨면 WebView2 가 '연결 거부' 화면을
    띄우고 다시 시도하지 않는다."""
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        with contextlib.suppress(OSError):
            with socket.create_connection((host, port), 0.3):
                return True
        time.sleep(0.1)
    return False


def request_shutdown():
    """종료 확인을 통과했다 - 확실히 프로세스를 끝낸다."""
    global _allow_close
    _allow_close = True

    def _force_exit():
        time.sleep(0.3)
        os._exit(0)

    threading.Thread(target=_force_exit, daemon=True).start()
    if WINDOW is not None:
        try:
            WINDOW.destroy()
        except Exception as e:             # noqa: BLE001
            print("[warn] 창 종료 실패: %s" % e)


class _JsBridge:
    """서버가 죽어도 창을 닫을 수 있는 직통 경로(WebView2 <-> 파이썬)."""

    def force_close(self):
        request_shutdown()
        return True


def _on_closing():
    """창 X → 앱 종료 확인 모달로 되묻는다.

    evaluate_js 를 이 핸들러에서 동기로 부르면 WebView2 가 재진입 데드락에 빠진다.
    반드시 별도 스레드에서 부르고 즉시 반환한다. 화면이 응답하지 않으면(오류 페이지 등)
    5초 뒤 강제 종료 - 닫히지 않는 창은 제품에서 허용하지 않는다.
    """
    if _allow_close:
        return True
    asked = []

    def _ask():
        try:
            ok = WINDOW.evaluate_js(
                "(function(){ if(window.requestExitConfirm){window.requestExitConfirm();"
                " return true;} return false; })()")
            if ok:
                asked.append(True)
        except Exception as e:             # noqa: BLE001
            print("[warn] 종료확인 모달 호출 실패: %s" % e)

    def _confirm_or_close():
        t = threading.Thread(target=_ask, daemon=True)
        t.start()
        t.join(5.0)
        if not asked:
            logger.write("warn", "창 닫기: 화면 무응답 → 강제 종료")
            request_shutdown()

    threading.Thread(target=_confirm_or_close, daemon=True).start()
    return False


def run(app, host, port, no_window=False):
    """서버를 스레드로 띄우고 창을 연다. no_window 면 서버만 띄우고 기다린다."""
    global WINDOW, _SERVER_ERROR
    import uvicorn

    got = _acquire_single_instance()
    deadline = time.monotonic() + 3.0
    while not got and time.monotonic() < deadline:
        time.sleep(0.25)                   # 직전 인스턴스 정리(~1초)를 기다린다
        got = _acquire_single_instance()
    if not got:
        _msgbox("Sample Auto Measurement",
                "프로그램이 이미 실행 중입니다.\n작업 표시줄에서 기존 창을 확인하세요.")
        return

    free = find_free_port(host, port)
    if free is None:
        _msgbox("Sample Auto Measurement",
                "사용 가능한 포트를 찾지 못했습니다 (%d~%d)." % (port, port + 9))
        return
    if free != port:
        logger.early("info", "포트 %d 사용 중 → %d 사용" % (port, free))
    port = free

    def run_server():
        global _SERVER_ERROR
        try:
            uvicorn.run(app, host=host, port=port, log_level="warning")
        except Exception as e:             # noqa: BLE001
            _SERVER_ERROR = "%s: %s" % (type(e).__name__, e)
            logger.write("err", "서버 스레드 종료: %s" % _SERVER_ERROR)

    th = threading.Thread(target=run_server, daemon=True)
    th.start()
    if not _wait_server_ready(host, port):
        _msgbox("Sample Auto Measurement",
                "서버를 시작하지 못했습니다.\n%s" % (_SERVER_ERROR or "원인 불명"))
        return

    url = "http://%s:%d/" % (host, port)
    if no_window:
        print("서버 준비 완료 - 브라우저에서 %s 를 여세요 (Ctrl+C 로 종료)" % url, flush=True)
        try:
            while th.is_alive():
                time.sleep(0.5)
        except KeyboardInterrupt:
            pass
        return

    try:
        import webview
    except ImportError:
        _msgbox("Sample Auto Measurement",
                "pywebview 가 없습니다.\n%s 를 브라우저에서 여세요." % url)
        return
    WINDOW = webview.create_window("Sample Auto Measurement", url,
                                   width=1440, height=900, js_api=_JsBridge())
    WINDOW.events.closing += _on_closing
    webview.start()
