"""run.py - 시작점.  python run.py (실장비) / python run.py --fake (가짜 장비)

서버는 127.0.0.1:8000. 화면(pywebview 창)은 2단계에서 붙인다.
"""

import argparse
import sys


def main(argv=None):
    ap = argparse.ArgumentParser(description="샘플 자동 측정 v3")
    ap.add_argument("--fake", action="store_true", help="가짜 장비(스테이지 · K2110 · K2450)로 실행 - 실제 포트 · VISA 를 열지 않는다")
    ap.add_argument("--fake-scale", type=float, default=0.2, help="가짜 이동 시간 배율(1 = 실제 속도, 작을수록 빠름)")
    ap.add_argument("--port", type=int, default=8000)
    a = ap.parse_args(argv)

    import uvicorn
    from app.config import Config
    from app.control import Controller
    from app.server import HOST, create_app

    ctrl = Controller(Config().load(), fake=a.fake, fake_scale=a.fake_scale)
    print("샘플 자동 측정 v3 · http://%s:%d/ %s" % (HOST, a.port, "(가짜 장비)" if a.fake else ""), flush=True)
    uvicorn.run(create_app(ctrl), host=HOST, port=a.port, log_level="warning")
    return 0


if __name__ == "__main__":
    sys.exit(main())
