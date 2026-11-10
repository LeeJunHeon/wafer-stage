"""vision.py - 카메라 서비스: 프레임 소스(실제 / 합성 / 사진 한 장) + 미리보기 + 촬영.

- 모든 촬영은 FrameSource.frame_after 를 거쳐 "마지막 이동 끝 + 안정 시간" 이후 프레임만 쓴다.
- 미리보기 스레드와 촬영은 같은 카메라 객체를 lock 으로 공유한다.
- 합성 카메라(dry): WAFER2_DRY_CAMERA=synth - tools/synth_cam.py.
"""

import collections
import os
import sys
import threading
import time

import cv2
import numpy as np

from core import framesrc, imgio

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class ImageCam:
    """--image: 항상 같은 사진."""

    def __init__(self, path):
        self.img = imgio.imread_u(path)
        self.cur = None

    def grab(self):
        self.cur = self.img
        return self.img is not None

    def retrieve(self):
        return None if self.cur is None else self.cur.copy()

    def release(self):
        pass


def jpeg(bgr, max_w=1600, q=80):
    if bgr is None:
        return None
    h, w = bgr.shape[:2]
    if w > max_w:
        bgr = cv2.resize(bgr, (max_w, int(h * max_w / w)), interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [cv2.IMWRITE_JPEG_QUALITY, q])
    return buf.tobytes() if ok else None


def sharpness(bgr, poly=None):
    """초점 점수(라플라시안 분산). poly(프레임 좌표) 가 있으면 그 안에서만 - 판 영역 기준."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    lap = cv2.Laplacian(g, cv2.CV_32F)
    if poly is not None and len(poly) >= 3:
        m = np.zeros(g.shape, np.uint8)
        cv2.fillConvexPoly(m, np.round(np.asarray(poly)).astype(np.int32), 255)
        sel = lap[m > 0]
        return float(sel.var()) if sel.size else 0.0
    return float(lap.var())


FAIL_LIMIT = 8                                                   # 실시간 프레임이 연속으로 이만큼 실패하면 카메라 끊김


class Vision:
    def __init__(self, cfg, true_xy_fn=None, dry=False, image=None, log=None):
        self.cfg = cfg
        self.dry = dry
        self.image = image
        self.true_xy = true_xy_fn or (lambda: (0.0, 0.0))
        self.log = log or (lambda *_: None)
        self.cam = None
        self.fs = None
        self.lock = threading.RLock()
        self.preview_jpg = None
        self.preview_req_t = -1e9                                # 마지막 /preview.jpg 요청 단조 시각 - 최근 2 s 안에 있었으면 미리보기가 켜져 있다(화면이 상태를 따로 기억하지 않는다)
        self.preview_n = 0                                       # 만든 실시간 프레임 수(성능 측정용)
        self.preview_t = 0.0                                     # 마지막으로 만든 프레임을 찍은 벽시계 시각
        self.preview_mono = 0.0
        self._enc_times = collections.deque(maxlen=64)           # 최근 인코딩 시각(실제 갱신 장/초)
        self._cap_req = 0                                        # 촬영이 카메라를 쓰려고 기다리는 중이면 미리보기는 끼어들지 않는다
        self._enc_now = threading.Event()                        # 화면이 그림을 요청했다 - 다음 grab 한 프레임을 바로 압축한다(주기를 기다리지 않음)
        self._th = None
        self._stop = threading.Event()
        self.last_error = ""
        self.synth = None
        self.size = None
        self.missing = False                                     # 시험: 카메라가 없는 상태를 흉내 낸다(열기 실패)
        self.open_delay = 0.0                                    # 시험: 열기가 오래 걸리는 카메라(DSHOW 열기 시도 1~2 s)
        self.opens = 0
        self.attempted = False                                   # 열기를 한 번이라도 해 보았는가(아니면 "확인 안 됨")
        self.frame_ok = False                                    # 실제 프레임을 받았는가(그때만 "연결됨")
        self.frame_t = 0.0
        self.fail_n = 0

    # ---- 열기 ---------------------------------------------------------
    def open(self):
        with self.lock:
            if self.cam is not None:
                return
            self.attempted = True
            self.opens += 1
            if self.open_delay > 0:
                time.sleep(self.open_delay)
            if self.missing:
                self.last_error = "카메라 없음"
                raise RuntimeError("카메라 없음")
            w, h = int(self.cfg.get("width")), int(self.cfg.get("height"))
            if self.image:
                self.cam = ImageCam(self.image)
            elif self.dry:
                if os.environ.get("WAFER2_DRY_CAMERA", "synth") != "synth":
                    raise RuntimeError("dry 카메라 없음")
                for p in (os.path.join(_ROOT, "tools"), _ROOT):
                    if p not in sys.path:
                        sys.path.insert(0, p)
                import synth_cam
                sw = int(os.environ.get("WAFER2_SYNTH_W", "1280"))
                sh = int(os.environ.get("WAFER2_SYNTH_H", "960"))
                self.synth = synth_cam.SynthCamera(self.true_xy, sw, sh, depth=int(os.environ.get("WAFER2_SYNTH_DEPTH", "4")))
                self.cam = self.synth
            else:
                from core import camera as cam_mod
                params = {k: self.cfg.get(k) for k in ("camera_index", "width", "height", "fourcc", "wb_temperature", "exposure",
                                                       "focus_mode", "focus", "focus_sweep", "capture_frames", "cam_first_frame_s")}
                params["autofocus"] = self.cfg.get("focus_mode") == "auto"
                params["auto_exposure"] = float(self.cfg.get("exposure") or 0) == 0
                c = cam_mod.Camera(params)
                c.open()
                self.cam = c
            self.fs = framesrc.FrameSource(self.cam, min_fresh=int(self.cfg.get("capture.fresh_frames")))
            f = self._first()
            if f is None:                                        # 열렸어도 프레임이 안 들어오면 연결이 아니다
                self._release()
                self.last_error = "프레임 없음"
                raise RuntimeError("카메라 프레임 없음")
            self.size = (f.shape[1], f.shape[0])
            self.frame_ok, self.frame_t, self.fail_n, self.last_error = True, time.monotonic(), 0, ""

    def _first(self):
        self.cam.grab()
        return self.cam.retrieve()

    def _release(self):
        with self.lock:
            if self.cam is not None:
                try:
                    self.cam.release()
                except Exception:                                # noqa: BLE001
                    pass
            self.cam = self.fs = self.synth = None
            self.frame_ok = False
            self.preview_jpg = None                              # 옛 프레임을 계속 보이지 않는다

    def close(self):
        self.stop_preview()
        self._release()

    def unplug(self):
        """카메라가 사라졌다(USB 빠짐 · 프레임 끊김): 열린 것을 놓고 "연결 안 됨" 으로. 미리보기 스레드는 그대로 둔다(자동 다시 연결이 다시 연다)."""
        self._release()

    # ---- 미리보기 -----------------------------------------------------
    PREVIEW_ON_S = 2.0                                           # /preview.jpg 요청이 이 시간 안에 있었으면 켜진 것
    PREVIEW_STALE_S = 3.0                                        # 이 시간 넘게 새 프레임이 없으면 옛 그림 대신 204

    @property
    def preview_on(self):
        return time.monotonic() - self.preview_req_t < self.PREVIEW_ON_S

    @preview_on.setter
    def preview_on(self, v):                                     # 옛 호환(시험 · 화면의 preview 명령): 켜면 요청이 있었던 것으로 친다
        if v:
            self.preview_req_t = time.monotonic()

    def want_preview(self):
        """/preview.jpg 를 한 번 요청받았다: 켜고, 마지막 압축이 주기(preview_fps)의 0.9 배보다 오래됐으면 다음에 grab 한 프레임을 바로 압축하라고 알린다
        (화면 요청 주기와 서버 압축 주기가 따로 돌면 지연이 두 주기만큼 쌓인다). 반환: 압축을 요청했는가."""
        now = time.monotonic()
        self.preview_req_t = now
        period = 1.0 / max(0.5, float(self.cfg.get("preview_fps")))
        if now - self.preview_mono >= 0.9 * period:
            self._enc_now.set()
            return True
        return False

    def current_preview(self):
        """지금 그림이 쓸 만하면 (jpg, seq, 찍은 시각) · 아니면(없음 · 3 s 넘게 새 프레임 없음) None."""
        if self.preview_jpg is None or time.monotonic() - self.preview_mono > self.PREVIEW_STALE_S:
            return None
        return self.preview_jpg, self.preview_n, self.preview_t

    def preview_fps(self):
        now = time.monotonic()
        n = sum(1 for t in self._enc_times if now - t <= 2.0)
        return round(n / 2.0, 1)

    def start_preview(self):
        if self._th and self._th.is_alive():
            return
        self._stop.clear()
        self._th = threading.Thread(target=self._loop, name="preview", daemon=True)
        self._th.start()

    def stop_preview(self):
        self._stop.set()
        if self._th:
            self._th.join(2.0)
        self._th = None

    def _loop(self):
        """켜져 있는 동안 쉬지 않고 grab 해서 드라이버 큐를 비우고, 주기(preview_fps)마다 가장 새 프레임 하나만 꺼내(retrieve) 압축한다
        (주기마다 한 번만 grab 하면 큐에 쌓인 옛 프레임부터 나와 장면보다 1~2 초 늦다). 촬영이 카메라를 쓰는 동안에는 끼어들지 않는다."""
        next_enc = 0.0
        while not self._stop.is_set():
            if not self.preview_on or self._cap_req > 0:
                next_enc = 0.0
                time.sleep(0.03)
                continue
            if not self.lock.acquire(timeout=0.05):
                continue
            idle = 0.0
            lost = False
            try:
                if self.cam is not None:
                    t = time.monotonic()
                    wall = time.time()
                    if self.cam.grab():
                        self.fail_n = 0
                        self.frame_t = t
                        if t >= next_enc or self._enc_now.is_set():
                            self._enc_now.clear()
                            f = self.cam.retrieve()
                            if f is not None:
                                period = 1.0 / max(0.5, float(self.cfg.get("preview_fps")))
                                self.preview_jpg = jpeg(f, int(self.cfg.get("preview_max_w")), 70)
                                self.preview_n += 1
                                self.preview_t, self.preview_mono = wall, t
                                self._enc_times.append(t)
                                self.size = (f.shape[1], f.shape[0])
                                next_enc = t + period
                        idle = 0.004
                    else:
                        self.fail_n += 1
                        idle = 0.02
                else:
                    idle = 0.05
            except Exception as e:                               # noqa: BLE001
                self.last_error = str(e)
                self.fail_n += 1
                idle = 0.02
            finally:
                lost = self.cam is not None and self.fail_n >= FAIL_LIMIT
                self.lock.release()
            if lost:                                             # 프레임이 계속 안 오면 카메라가 빠진 것으로 본다
                self.last_error = self.last_error or "프레임 끊김"
                self.unplug()
            time.sleep(idle)

    # ---- 촬영 ---------------------------------------------------------
    def frame_after(self, t_after, settle_s=None, n_best=1, poly=None, timeout_s=30.0):
        """t_after 이후 프레임. n_best > 1 이면 이어서 받은 n 장 중 가장 선명한 한 장. 반환 (bgr, 단조 시각)."""
        self.open()
        settle = float(self.cfg.get("capture.settle_s")) if settle_s is None else settle_s
        self._cap_req += 1                                       # 미리보기가 잠금을 놓고 비켜 준다
        lk = self.lock.acquire(timeout=timeout_s + 5)
        try:
            if not lk:
                raise RuntimeError("카메라가 다른 작업 중")
            st = self.fs.frame_after(t_after, settle, timeout_s)
            best, bt, bs = st.frame, st.t, sharpness(st.frame, poly) if n_best > 1 else 0.0
            for _ in range(max(0, n_best - 1)):
                s2 = self.fs.latest()
                if s2 is None:
                    continue
                sc = sharpness(s2.frame, poly)
                if sc > bs:
                    best, bt, bs = s2.frame, s2.t, sc
            self.size = (best.shape[1], best.shape[0])
            return best.copy(), bt
        finally:
            if lk:
                self.lock.release()
            self._cap_req -= 1

    def set_world(self, **kw):
        if self.synth is not None:
            self.synth.set_world(**kw)
