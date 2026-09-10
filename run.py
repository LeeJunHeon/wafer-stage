"""run.py - 진입점.

  python run.py                 # 창(pywebview)
  python run.py --no-window     # 브라우저로 접속
  python run.py --dry           # 시리얼 없이(이동은 흉내)
  python run.py --image PATH    # 촬영 대신 그 사진

실제 서버는 backend/server.py 에 있다. 여기서는 backend 를 경로에 넣고 부르기만 한다.
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

import server                                                      # noqa: E402

if __name__ == "__main__":
    server.main()
