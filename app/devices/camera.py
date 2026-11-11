"""camera.py - 카메라(OpenCV). 열기 · 미리보기 · 마지막 한 장.

- 기본값은 settings(장치 1 · 4080×3060 · MJPG · 초점 고정). v2 에서 실장비로 쓰던 열기 방식(백엔드 순서 · 포맷 설정 순서 바꿔 가며 시도 ·
  열린 뒤 프레임이 실제로 나오는지 확인)을 그대로 쓴다.
- 읽기 스레드: 계속 grab() 해서 버퍼를 비우고(오래된 프레임을 피한다), 1 초에 한 번 retrieve() 해서
  원본 프레임(전체 해상도) 마지막 한 장과 가로 preview_width 로 줄인 JPEG 를 갱신한다.
- 카메라가 없거나 끊겨도 프로그램은 계속 돈다(on_event("lost")).
"""

import threading
import time
from typing import Callable, Optional

import cv2

BACKENDS = [("DSHOW", getattr(cv2, "CAP_DSHOW", 0)), ("MSMF", getattr(cv2, "CAP_MSMF", 0)), ("ANY", cv2.CAP_ANY)]
FAIL_LIMIT = 30                                  # 연속으로 프레임을 못 받으면 끊김으로 본다
PERIOD_S = 1.0


class CameraError(Exception):
    pass


def fourcc_str(v):
    try:
        n = int(v)
        return "".join(chr((n >> (8 * i)) & 0xFF) for i in range(4)).strip("\x00 ")
    except (TypeError, ValueError):
        return ""


class Camera:
    """open_capture(index, backend_flag) → cv2.VideoCapture 모양 객체(시험 · --fake 는 가짜를 꽂는다)."""

    def __init__(self, open_capture: Optional[Callable] = None, log: Optional[Callable] = None,
                 on_event: Optional[Callable] = None, period_s: float = PERIOD_S):
        self.open_capture = open_capture or (lambda idx, flag: cv2.VideoCapture(idx, flag))
        self.log = log or (lambda text: None)
        self.on_event = on_event or (lambda kind, text: None)
        self.period_s = period_s
        self.cap = None
        self.connected = False
        self.info = {}                                # requested · actual 해상도 · 포맷 · 백엔드
        self._lock = threading.Lock()
        self._frame = None                            # 원본 마지막 한 장
        self._jpeg = None
        self._frame_t = 0.0
        self._thread = None
        self._alive = False
        self._preview_w = 1280

    # ---- 열기 ----
    def open(self, index, width, height, fourcc="MJPG", focus_fixed=True, focus_value=-1, preview_width=1280):
        if self.connected:
            raise CameraError("이미 연결되어 있습니다")
        self._preview_w = int(preview_width)
        errors = []
        for name, flag in BACKENDS:
            try:
                cap = self.open_capture(int(index), flag)
            except Exception as e:                                  # noqa: BLE001
                errors.append("%s: %s" % (name, str(e)[:30]))
                continue
            if cap is None or not cap.isOpened():
                self._release(cap)
                errors.append("%s: 열기 실패" % name)
                continue
            self._configure(cap, name, width, height, fourcc, focus_fixed, focus_value)
            frame = self._first_frame(cap, width * height)
            if frame is None:
                self._release(cap)
                errors.append("%s: 열렸지만 프레임이 없음" % name)
                continue
            self.cap = cap
            self.info = {"backend": name, "requested": [int(width), int(height)], "requested_fourcc": str(fourcc),
                         "actual": [int(frame.shape[1]), int(frame.shape[0])], "actual_fourcc": fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))}
            self._store(frame)
            self.connected = True
            self._alive = True
            self._thread = threading.Thread(target=self._loop, daemon=True, name="camera")
            self._thread.start()
            self.log("카메라 열림 · 요청 %dx%d %s → 실제 %dx%d %s (%s)" % (width, height, fourcc, self.info["actual"][0], self.info["actual"][1],
                                                                       self.info["actual_fourcc"] or "?", name))
            return dict(self.info)
        raise CameraError("카메라 %d 번을 열 수 없습니다 (%s). 번호와 USB 연결을 확인하십시오. 0 번은 노트북 내장 카메라입니다" % (index, ", ".join(errors)))

    @staticmethod
    def _release(cap):
        try:
            if cap is not None:
                cap.release()
        except Exception:                                           # noqa: BLE001
            pass

    def _configure(self, cap, backend, w, h, fourcc, focus_fixed, focus_value):
        code = cv2.VideoWriter_fourcc(*fourcc) if len(fourcc) == 4 else 0

        def set_fourcc():
            if code:
                cap.set(cv2.CAP_PROP_FOURCC, code)

        def set_size():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, int(w))
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, int(h))

        def set_fps():
            cap.set(cv2.CAP_PROP_FPS, 30)

        # 드라이버마다 설정을 먹는 순서가 달라 한 번에는 실패한다 - 순서를 바꿔 가며 시도하고 매번 포맷을 되읽는다
        for seq in ((set_fourcc, set_size, set_fps), (set_size, set_fourcc, set_fps), (set_fourcc, set_fps, set_size), (set_fps, set_fourcc, set_size)):
            for fn in seq:
                fn()
            if fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)) == fourcc:
                break
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0 if focus_fixed else 1)
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        if focus_fixed and int(focus_value) >= 0:
            cap.set(cv2.CAP_PROP_FOCUS, int(focus_value))

    def _first_frame(self, cap, pixels):
        """열린 뒤 프레임이 실제로 나오는지 확인한다(고해상도는 첫 장이 늦다 - 4백만 화소 넘으면 두 배로 기다린다)."""
        wait_s = 5.0 * (2.0 if pixels > 4_000_000 else 1.0)
        t_end = time.monotonic() + wait_s
        n = 0
        while n < 10 or time.monotonic() < t_end:
            n += 1
            ok, f = cap.read()
            if ok and f is not None and getattr(f, "size", 0):
                return f
            if time.monotonic() >= t_end and n >= 10:
                break
            time.sleep(0.05)
        return None

    def mismatch(self):
        """요청과 다르게 열렸으면 사람이 읽을 문장, 같으면 ""."""
        i = self.info
        if not i:
            return ""
        if i["actual"] != i["requested"] or (i["actual_fourcc"] and i["actual_fourcc"] != i["requested_fourcc"]):
            return "카메라가 요청한 %dx%d %s 가 아니라 %dx%d %s 로 열렸습니다" % (
                i["requested"][0], i["requested"][1], i["requested_fourcc"], i["actual"][0], i["actual"][1], i["actual_fourcc"] or "?")
        return ""

    # ---- 읽기 스레드 ----
    def _loop(self):
        fails = 0
        last = 0.0
        while self._alive:
            cap = self.cap
            if cap is None:
                break
            try:
                ok = cap.grab()
                now = time.monotonic()
                if ok and now - last >= self.period_s:
                    ok, frame = cap.retrieve()
                    if ok and frame is not None:
                        self._store(frame)
                        last = now
            except Exception as e:                                  # noqa: BLE001
                ok = False
                self.log("카메라 읽기 오류 · %s" % str(e)[:40])
            if ok:
                fails = 0
                time.sleep(0.005)
            else:
                fails += 1
                if fails >= FAIL_LIMIT:
                    self._lost("프레임을 받지 못함")
                    return
                time.sleep(0.1)

    def _lost(self, why):
        if not self.connected:
            return
        self.connected = False
        self._alive = False
        cap, self.cap = self.cap, None
        self._release(cap)
        with self._lock:
            self._jpeg = None
        self.on_event("lost", why)

    def _store(self, frame):
        w = self._preview_w
        small = frame
        if frame.shape[1] > w:
            small = cv2.resize(frame, (w, max(1, int(round(frame.shape[0] * w / frame.shape[1])))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", small, [cv2.IMWRITE_JPEG_QUALITY, 80])
        with self._lock:
            self._frame = frame
            self._frame_t = time.time()
            if ok:
                self._jpeg = buf.tobytes()

    # ---- 가져가기 ----
    def preview_jpeg(self) -> Optional[bytes]:
        with self._lock:
            return self._jpeg

    def last_frame(self):
        with self._lock:
            return self._frame

    def close(self):
        self._alive = False
        self.connected = False
        t = self._thread
        if t is not None and t is not threading.current_thread():
            t.join(timeout=2.0)
        self._thread = None
        cap, self.cap = self.cap, None
        self._release(cap)
        with self._lock:
            self._frame = None
            self._jpeg = None
