"""core - 장비·검출 핵심 모듈.

서버(backend)와 화면(frontend)을 모른다. 여기 있는 것만으로 촬영·검출·보정·
스테이지 제어가 되고, CLI 로도 직접 쓸 수 있다.

  paths   경로(데이터 폴더는 코드 밖에 둔다)
  imgio   한글 경로 imread/imwrite
  camera  USB 카메라
  detect  샘플 검출 파이프라인
  calib   마커 보정 · 픽셀↔mm      (python -m core.calib fit|samples|check)
  stage   아두이노 스테이지 제어    (python -m core.stage st|mx|my|save)
"""

# cv2 는 카메라를 못 열 때마다 백엔드 진단을 콘솔에 쏟아낸다(장비 없이 띄우면
# 우리 로그가 파묻힌다). 환경변수는 cv2 를 import 하는 순간 읽히므로, core 안
# 어디서 cv2 를 먼저 import 하든 걸리도록 여기 맨 위에서 정한다.
import os as _os

_os.environ.setdefault("OPENCV_LOG_LEVEL", "SILENT")
