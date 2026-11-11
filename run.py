"""run.py - 시작점.  python run.py (실장비) / python run.py --fake (가짜 장비) / --no-window (서버만, 브라우저로 접속)

서버는 127.0.0.1:8000. 기본은 pywebview 창(최대화, 제목 "VO₂ 온도–저항 측정")을 연다. 창을 닫으면 종료 처리
(히트건 · 쿨러 끄기 → K2450 출력 끄기 → 연결 닫기)를 하고 끝난다.
"""

import argparse
import sys
import threading
import time

TITLE = "VO₂ 온도–저항 측정"


def main(argv=None):
    ap = argparse.ArgumentParser(description="샘플 자동 측정 v3")
    ap.add_argument("--fake", action="store_true", help="가짜 장비(스테이지 · K2110 · K2450 · 카메라)로 실행 - 실제 포트 · VISA · 카메라를 열지 않는다")
    ap.add_argument("--fake-scale", type=float, default=0.2, help="가짜 이동 시간 배율(1 = 실제 속도, 작을수록 빠름)")
    ap.add_argument("--no-window", action="store_true", help="창 없이 서버만 띄운다(브라우저로 http://127.0.0.1:8000 접속)")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    import uvicorn
    from app.config import Config
    from app.control import Controller
    from app.server import HOST, create_app

    ctrl = Controller(Config().load(), fake=a.fake, fake_scale=a.fake_scale)
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
    print("샘플 자동 측정 v3 · %s %s" % (url, "(가짜 장비)" if a.fake else ""), flush=True)
    threading.Thread(target=ctrl.auto_connect, daemon=True, name="auto-connect").start()    # 실패해도 프로그램은 뜬다

    try:
        if a.no_window:
            while th.is_alive():
                time.sleep(0.5)
        else:
            import webview
            webview.create_window(TITLE, url, maximized=True, text_select=True)
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
