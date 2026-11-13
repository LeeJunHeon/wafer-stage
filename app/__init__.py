"""app - 샘플 측정 프로그램. 이 파일은 app 아래 어떤 모듈보다 먼저 읽힌다(cv2 를 import 하기 전).

OpenCV 는 없는 카메라 번호를 열어 볼 때 콘솔에 경고를 쏟는다(VIDEOIO … can't be used to capture by index · Camera index out of range).
카메라 목록을 만들 때 0~3 번을 열어 보므로 이 글이 매번 나온다 → OpenCV 자체 로그를 끈다. 열기 실패는 프로그램 로그(app.log)에 한 줄로 남긴다.
사람이 환경 변수를 직접 정해 두었으면 그 값을 쓴다.
"""

import os

os.environ.setdefault("OPENCV_LOG_LEVEL", "OFF")
