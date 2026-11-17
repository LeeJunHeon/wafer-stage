"""imgio.py - 이미지 파일 읽기 · 쓰기(한 곳). app/ 의 이미지 파일 입출력은 모두 여기를 지난다 - cv2.imread · cv2.imwrite 를 쓰지 않는다.

까닭: Windows 의 OpenCV 는 파일 경로를 ANSI 코드 페이지로 바꿔 넘긴다. 경로에 한글이 있으면(예: Desktop\\01_장비\\자동 측정\\data_v3)
cv2.imread 는 오류 없이 None 을, cv2.imwrite 는 False 를 돌려준다(v2 에서 겪고 core/imgio.py 로 고친 일).
그래서 파일은 파이썬이 바이트로 읽고 쓰고(유니코드 경로를 그대로 쓴다), OpenCV 는 메모리에서 해석(imdecode) · 만들기(imencode)만 한다.
쓰기는 같은 폴더의 임시 파일에 쓴 뒤 os.replace 로 바꾼다(원자적 - 쓰다 멈춰도 반쪽 파일이 남지 않는다).
"""

import os
import tempfile
from pathlib import Path

import cv2
import numpy as np


class ImageIOError(Exception):
    """이미지 파일을 못 읽거나 못 썼다. 글은 까닭 한 줄(경로는 부르는 쪽이 로그에 남긴다)."""


def encode(img, ext=".png", params=None) -> bytes:
    """영상 → 파일 바이트(ext: ".png" · ".jpg"). 못 만들면 ImageIOError."""
    if img is None or getattr(img, "size", 0) == 0:
        raise ImageIOError("빈 영상")
    ok, buf = cv2.imencode(ext, img, list(params or []))
    if not ok:
        raise ImageIOError("영상 변환 실패 · %s" % ext)
    return buf.tobytes()


def decode(data, flags=cv2.IMREAD_COLOR):
    """파일 바이트 → 영상. 해석하지 못하면(깨진 파일 · 이미지가 아님) ImageIOError."""
    if not data:
        raise ImageIOError("빈 파일")
    img = cv2.imdecode(np.frombuffer(data, dtype=np.uint8), flags)
    if img is None:
        raise ImageIOError("이미지 해석 실패")
    return img


def read(path, flags=cv2.IMREAD_COLOR):
    """이미지 파일 읽기. 없거나 못 읽거나 깨졌으면 ImageIOError(None 을 돌려주지 않는다 - 실패를 말없이 넘기지 않게)."""
    try:
        data = Path(path).read_bytes()
    except FileNotFoundError:
        raise ImageIOError("파일 없음")
    except OSError as e:
        raise ImageIOError("파일 읽기 실패 · %s" % e)
    return decode(data, flags)


def write_bytes(path, data: bytes):
    """바이트를 파일로(원자적): 같은 폴더의 임시 파일에 다 쓴 뒤 os.replace. 폴더가 없으면 만든다. 실패하면 ImageIOError."""
    path = Path(path)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix="." + path.stem + ".", suffix=".tmp")
    except OSError as e:
        raise ImageIOError("파일 쓰기 실패 · %s" % e)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, str(path))
    except OSError as e:
        raise ImageIOError("파일 쓰기 실패 · %s" % e)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def write(path, img, params=None):
    """영상을 이미지 파일로(형식은 확장자로 정한다 - .png · .jpg). 원자적. 실패하면 ImageIOError."""
    write_bytes(path, encode(img, Path(path).suffix.lower() or ".png", params))
