"""vision.py - 촬영과 검출(executor) + 최근 프레임 JPEG 보관.

촬영·검출은 CPU 작업이라 이벤트 루프에서 돌리면 화면이 멎는다. 전부 executor 에서
돌리고 결과만 받는다. 화면에 그릴 그림은 서버가 좌표만 주고(SVG 는 화면이 그린다)
사진 자체는 /frame/<id>.jpg 로 준다.
"""

import asyncio
import io
import time
from concurrent.futures import ThreadPoolExecutor

import cv2

import calib
import camera
import imgio
import logger

_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")

# 최근 프레임 하나만 메모리에 둔다(id, JPEG bytes). 파일로 쓰지 않는 이유는
# 촬영마다 디스크를 건드리지 않기 위해서다 - 저장은 engine 이 seq 폴더에 따로 한다.
_frame = {"id": None, "jpg": b"", "w": 0, "h": 0, "ts": 0.0}

IMAGE_OVERRIDE = None          # --image 로 준 파일. 있으면 카메라 대신 이 파일을 쓴다


def set_image_override(path):
    global IMAGE_OVERRIDE
    IMAGE_OVERRIDE = path or None


def frame_jpeg(fid):
    if _frame["id"] != fid:
        return None
    return _frame["jpg"]


def _grab(params):
    """프레임 한 장. --image 면 그 파일, 아니면 카메라(가장 선명한 장)."""
    if IMAGE_OVERRIDE:
        bgr = imgio.imread_u(IMAGE_OVERRIDE)
        if bgr is None:
            raise IOError("이미지를 열 수 없습니다: %s" % IMAGE_OVERRIDE)
        return bgr, {"source": IMAGE_OVERRIDE}
    cam = camera.Camera(params).open()
    try:
        frame, stats = cam.capture_best()
        return frame, stats
    finally:
        cam.release()


def _store(bgr):
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise IOError("프레임 인코딩 실패")
    h, w = bgr.shape[:2]
    _frame.update({"id": "%d" % int(time.time() * 1000), "jpg": buf.tobytes(),
                   "w": int(w), "h": int(h), "ts": time.time()})
    return {"id": _frame["id"], "ts": _frame["ts"], "w": _frame["w"], "h": _frame["h"],
            "url": "/frame/%s.jpg" % _frame["id"]}


def _capture_sync(params):
    bgr, stats = _grab(params)
    meta = _store(bgr)
    # 마커 재보정 실패(3개 미만)면 좌표를 만들지 않는다. 틀린 좌표로 스테이지를
    # 움직이는 것보다 멈추는 것이 낫다.
    buf = io.StringIO()
    import contextlib
    with contextlib.redirect_stdout(buf):
        res = calib.sense(bgr, params, allow_fallback=False, save=True)
    for line in buf.getvalue().splitlines():
        if line.strip():
            logger.write("info", line.rstrip())
    return bgr, meta, res, stats


async def capture(params):
    """(bgr, frame_meta, sense 결과 또는 None, 촬영 통계)."""
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ex, _capture_sync, params)


async def run_blocking(fn, *a):
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(_ex, fn, *a)


def shutdown():
    _ex.shutdown(wait=False)
