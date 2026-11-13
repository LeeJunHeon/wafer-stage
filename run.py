"""run.py - 시작점.  python run.py (창을 연다) / --no-window (서버만, 브라우저로 접속) / --port (서버 포트)

서버는 127.0.0.1:8000. 기본은 pywebview 창(최대화, 제목 "VO₂ 자동 측정 프로그램")을 연다. 창을 닫으면 종료 처리
(히트건 · 쿨러 끄기 → K2450 출력 끄기 → 연결 닫기)를 하고 끝난다.
창으로 실행했을 때만 폴더 선택 창([찾아보기])을 쓸 수 있다 - 창을 만든 뒤 그 함수를 Controller 에 넘긴다(--no-window 에는 없다).
"""

import argparse
import sys
import threading
import time

TITLE = "VO₂ 자동 측정 프로그램"


def main(argv=None):
    ap = argparse.ArgumentParser(description=TITLE)
    ap.add_argument("--no-window", action="store_true", help="창 없이 서버만 띄운다(브라우저로 http://127.0.0.1:8000 접속)")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    import uvicorn
    from app.config import Config
    from app.control import Controller
    from app.server import HOST, create_app

    ctrl = Controller(Config().load())
    server = uvicorn.Server(uvicorn.Config(create_app(ctrl), host=HOST, port=a.port, log_level="warning"))
    url = "http://%s:%d/" % (HOST, a.port)
    th = threading.Thread(target=server.run, daemon=True, name="uvicorn")
    th.start()
    for _ in range(100):                                            # 서버가 뜰 때까지(최대 10 초)
        if server.started or not th.is_alive():
            break
        time.sleep(0.1)
    if not server.started:
        print("서버를 시작하지 못했습니다 (포트 %d 가 사용 중일 수 있습니다)" % a.port, flush=True)
        ctrl.shutdown()
        return 1
    print("%s · %s" % (TITLE, url), flush=True)
    threading.Thread(target=ctrl.auto_connect, daemon=True, name="auto-connect").start()    # 실패해도 프로그램은 뜬다

    try:
        if a.no_window:
            while th.is_alive():
                time.sleep(0.5)
        else:
            import webview
            window = webview.create_window(TITLE, url, maximized=True, text_select=True)

            def pick_folder(start):
                """이 컴퓨터의 폴더 선택 창. 고른 경로, 취소하면 None."""
                got = window.create_file_dialog(webview.FileDialog.FOLDER, directory=start)
                if isinstance(got, (list, tuple)):
                    got = got[0] if got else None
                return got or None

            ctrl.set_folder_picker(pick_folder)
            webview.start()                                         # 창을 닫으면 돌아온다
    except KeyboardInterrupt:
        pass
    finally:
        ctrl.shutdown()                                             # 히트건 · 쿨러 끄기 → K2450 출력 끄기 → 닫기
        server.should_exit = True
        th.join(timeout=3)
    return 0


if __name__ == "__main__":
    sys.exit(main())
