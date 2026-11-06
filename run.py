"""run.py - 진입점.

  python run.py                 # 창(pywebview)
  python run.py --no-window     # 브라우저로 접속
  python run.py --dry           # 시리얼 없이(이동은 흉내) + 합성 카메라
  python run.py --image PATH    # 촬영 대신 그 사진
  python run.py --cleanup       # 창 없이 남은 이전 실행 정리
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "backend"))

import server                                                      # noqa: E402

if __name__ == "__main__":
    sys.exit(server.main())
