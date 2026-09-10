"""vision.py - 카메라 한 대를 미리보기와 촬영이 나눠 쓴다 + 최근 프레임 보관.

촬영·검출은 CPU 작업이라 이벤트 루프에서 돌리면 화면이 멎는다. 전부 executor 에서
돌리고 결과만 받는다. 화면에 그릴 그림은 서버가 좌표만 주고(SVG 는 화면이 그린다)
사진 자체는 /frame/<id>.jpg 로 준다.

미리보기:
  카메라 장치는 한 번에 한 곳만 열 수 있다. 그래서 CameraHolder 가 객체 하나를
  들고 lock 으로 나눠 쓴다 - 미리보기 스레드가 4fps 로 읽고, 촬영은 같은 객체로
  lock 안에서 capture_best() 를 부른다. 촬영 때마다 닫았다 다시 열면 노출·초점이
  매번 처음부터 잡혀 첫 장이 흐리고, 재개방 자체가 1~2초 걸린다.
"""

import asyncio
import io
import threading
import time
from concurrent.futures import ThreadPoolExecutor

import cv2

from core import calib, camera, imgio

import logger

_ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="vision")

# 최근 프레임 하나만 메모리에 둔다(id, JPEG bytes). 파일로 쓰지 않는 이유는
# 촬영마다 디스크를 건드리지 않기 위해서다 - 저장은 engine 이 seq 폴더에 따로 한다.
_frame = {"id": None, "jpg": b"", "w": 0, "h": 0, "ts": 0.0}

IMAGE_OVERRIDE = None          # --image 로 준 파일. 있으면 카메라 대신 이 파일을 쓴다

PREVIEW_FPS = 4.0
PREVIEW_W = 640
PREVIEW_H = 360
PREVIEW_Q = 70
RETRY_S = 5.0                  # 카메라를 못 열었을 때 다시 시도하는 간격
FAIL_LIMIT = 3                 # 이만큼 연속 실패해야 카메라를 닫는다


def set_image_override(path):
    global IMAGE_OVERRIDE
    IMAGE_OVERRIDE = path or None


def frame_jpeg(fid):
    if _frame["id"] != fid:
        return None
    return _frame["jpg"]


# --------------------------------------------------------------------------
# 카메라 공유
# --------------------------------------------------------------------------
class CameraHolder:
    """카메라 객체 하나를 미리보기 스레드와 촬영이 나눠 쓴다.

    상태(연결·오류)는 서버가 주인이므로 여기서는 콜백으로 알리기만 한다.
    """

    def __init__(self):
        self.lock = threading.RLock()
        self.cam = None
        self.params = {}
        self.error = ""
        self._jpg = b""                    # 최신 미리보기 한 장
        self._jpg_ts = 0.0
        self._thread = None
        self._stop = threading.Event()
        self._reopen = threading.Event()
        self.on_status = None              # fn(ok: bool|None, error: str)
        self._last_status = None

    # ---- 상태 ----
    def _status(self, ok, err=""):
        """상태가 바뀔 때만 알린다(같은 오류를 5초마다 로그로 도배하지 않는다)."""
        if (ok, err) == getattr(self, "_last_status", None):
            return
        self._last_status = (ok, err)
        self.error = err
        if self.on_status is not None:
            try:
                self.on_status(ok, err)
            except Exception:              # noqa: BLE001
                pass

    # ---- 열기/닫기 ----
    def _open_locked(self):
        if self.cam is not None:
            return self.cam
        self.cam = camera.Camera(self.params).open()
        return self.cam

    def _close_locked(self):
        cam, self.cam = self.cam, None
        if cam is not None:
            try:
                cam.release()
            except Exception:              # noqa: BLE001
                pass

    def reopen(self, params):
        """설정이 바뀌었다 - 다음 루프에서 다시 연다(카메라 번호 변경 등)."""
        self.params = dict(params or {})
        self._reopen.set()

    # ---- 미리보기 ----
    @property
    def running(self):
        return self._thread is not None and self._thread.is_alive()

    def start(self, params):
        self.params = dict(params or {})
        if self.running:
            self._reopen.set()
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="preview", daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()
        t = self._thread
        if t is not None:
            t.join(timeout=2.0)
        self._thread = None
        with self.lock:
            self._close_locked()
        self._jpg = b""
        self._last_status = None

    def preview_jpeg(self):
        return self._jpg or None

    def _encode(self, bgr):
        h, w = bgr.shape[:2]
        if w > PREVIEW_W:
            small = cv2.resize(bgr, (PREVIEW_W, PREVIEW_H), interpolation=cv2.INTER_AREA)
        else:
            small = bgr
        ok, buf = cv2.imencode(".jpg", small, [int(cv2.IMWRITE_JPEG_QUALITY), PREVIEW_Q])
        if ok:
            self._jpg = buf.tobytes()
            self._jpg_ts = time.time()

    def _loop(self):
        """grab() 은 쉬지 않고, 인코딩은 PREVIEW_FPS 로.

        grab() 은 카메라가 다음 프레임을 낼 때까지 블록하므로 이 루프의 속도는
        장치 fps 가 정한다(바쁜 대기가 아니다). 드라이버 큐를 계속 비워 두기
        때문에 250ms 마다 꺼내는 그림이 '지금 장면' 이다.
        """
        period = 1.0 / PREVIEW_FPS
        next_shot = 0.0
        fails = 0
        while not self._stop.is_set():
            # --image 모드는 카메라를 열지 않는다. 그 사진을 미리보기로 낸다.
            if IMAGE_OVERRIDE:
                if not self._jpg:
                    bgr = imgio.imread_u(IMAGE_OVERRIDE)
                    if bgr is None:
                        self._status(False, "이미지를 열 수 없음: %s" % IMAGE_OVERRIDE)
                    else:
                        self._encode(bgr)
                        self._status(True)
                self._stop.wait(period)
                continue
            if self._reopen.is_set():
                self._reopen.clear()
                with self.lock:
                    self._close_locked()
                fails = 0
            try:
                want_shot = time.monotonic() >= next_shot
                with self.lock:
                    if self.cam is None:
                        self._open_locked()
                        want_shot = True
                    if not self.cam.grab():
                        raise camera.CameraError("프레임을 받지 못함")
                    bgr = self.cam.retrieve() if want_shot else None
                if want_shot:
                    if bgr is None:
                        raise camera.CameraError("프레임을 읽지 못함")
                    self._encode(bgr)
                    next_shot = time.monotonic() + period
                fails = 0
                self._status(True)
            except Exception as e:         # noqa: BLE001
                # 한 장 빠지는 것은 흔하다. 연달아 실패할 때만 장치를 다시 연다.
                fails += 1
                if fails < FAIL_LIMIT and self.cam is not None:
                    self._stop.wait(period)
                    continue
                # 실패가 이어지면 장치를 닫고 RETRY_S 뒤에 다시 연다.
                with self.lock:
                    self._close_locked()
                self._jpg = b""
                fails = 0
                self._status(False, str(e).splitlines()[0] if str(e) else type(e).__name__)
                self._stop.wait(RETRY_S)

    # ---- 촬영 ----
    def capture_best(self, params):
        """미리보기와 같은 카메라 객체로 한 장. 미리보기가 꺼져 있으면 여기서 연다."""
        self.params = dict(params or {})
        with self.lock:
            cam = self._open_locked()
            try:
                return cam.capture_best()
            except Exception:
                self._close_locked()       # 촬영이 깨진 장치는 다음에 다시 연다
                raise


holder = CameraHolder()


def _store(bgr):
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), 85])
    if not ok:
        raise IOError("프레임 인코딩 실패")
    h, w = bgr.shape[:2]
    _frame.update({"id": "%d" % int(time.time() * 1000), "jpg": buf.tobytes(),
                   "w": int(w), "h": int(h), "ts": time.time()})
    return {"id": _frame["id"], "ts": _frame["ts"], "w": _frame["w"], "h": _frame["h"],
            "url": "/frame/%s.jpg" % _frame["id"]}


def _grab(params):
    """프레임 한 장. --image 면 그 파일, 아니면 카메라(가장 선명한 장)."""
    if IMAGE_OVERRIDE:
        bgr = imgio.imread_u(IMAGE_OVERRIDE)
        if bgr is None:
            raise IOError("이미지를 열 수 없습니다: %s" % IMAGE_OVERRIDE)
        return bgr, {"source": IMAGE_OVERRIDE}
    return holder.capture_best(params)


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
    holder.stop()
    _ex.shutdown(wait=False)
