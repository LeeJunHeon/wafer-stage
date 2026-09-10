"""imgio.py - 유니코드(한글) 경로에서도 되는 이미지 읽기/쓰기.

OpenCV 의 cv2.imread / cv2.imwrite 는 Windows 에서 경로를 ANSI 코드페이지로
넘기기 때문에, 폴더 이름에 한글이 들어가면 파일을 못 찾거나(None) **조용히
저장에 실패**한다(False 를 돌려주는데 아무도 안 본다). 실제로 프로그램을
"자동 측정 프로그램" 폴더로 옮긴 뒤 raw.png / annotated.png / calib_fit.jpg 가
하나도 안 생겼다.

그래서 파일 입출력은 파이썬이 하고(np.fromfile / ndarray.tofile), 인코딩·디코딩만
OpenCV 에 맡긴다. 이 경로는 코드페이지를 타지 않는다.
"""

import os

import cv2
import numpy as np


def imread_u(path, flags=cv2.IMREAD_COLOR):
    """실패하면 None (cv2.imread 와 같은 규약)."""
    try:
        buf = np.fromfile(path, dtype=np.uint8)
    except OSError:
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, flags)


def imwrite_u(path, img, params=None):
    """저장에 실패하면 IOError 를 던진다. 조용히 넘어가지 않는다."""
    if img is None:
        raise IOError("이미지가 비어 있어 저장할 수 없습니다: %s" % path)
    ext = os.path.splitext(path)[1] or ".png"
    ok, buf = cv2.imencode(ext, img, params or [])
    if not ok:
        raise IOError("이미지 인코딩 실패(%s): %s" % (ext, path))
    d = os.path.dirname(os.path.abspath(path))
    if d:
        os.makedirs(d, exist_ok=True)
    try:
        buf.tofile(path)
    except OSError as e:
        raise IOError("이미지 저장 실패: %s (%s)" % (path, e))
    if not os.path.exists(path) or os.path.getsize(path) == 0:
        raise IOError("이미지 저장 실패(파일이 비어 있음): %s" % path)
    return True
