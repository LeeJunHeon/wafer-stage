"""vision.py - 카메라 서비스: 프레임 소스(실제 / 합성 / 사진 한 장) + 미리보기 + 촬영.

- 모든 촬영은 FrameSource.frame_after 를 거쳐 "마지막 이동 끝 + 안정 시간" 이후 프레임만 쓴다.
- 미리보기 스레드와 촬영은 같은 카메라 객체를 lock 으로 공유한다.
- 합성 카메라(dry): WAFER2_DRY_CAMERA=synth - tools/synth_cam.py.
"""

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
        self.preview_on = False
        self._th = None
        self._stop = threading.Event()
        self.last_error = ""
        self.synth = None
        self.size = None

    # ---- 열기 ---------------------------------------------------------
    def open(self):
        with self.lock:
            if self.cam is not None:
                return
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
            self.size = (f.shape[1], f.shape[0]) if f is not None else (w, h)

    def _first(self):
        self.cam.grab()
        return self.cam.retrieve()

    def close(self):
        self.stop_preview()
        with self.lock:
            if self.cam is not None:
                try:
                    self.cam.release()
                except Exception:                                # noqa: BLE001
                    pass
            self.cam = self.fs = self.synth = None

    # ---- 미리보기 -----------------------------------------------------
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
        while not self._stop.is_set():
            period = 1.0 / max(0.5, float(self.cfg.get("preview_fps")))
            t0 = time.monotonic()
            if self.preview_on and self.lock.acquire(timeout=0.2):
                try:
                    if self.cam is not None:
                        st = self.fs.latest()
                        if st is not None:
                            self.preview_jpg = jpeg(st.frame, int(self.cfg.get("preview_max_w")), 70)
                            self.size = (st.frame.shape[1], st.frame.shape[0])
                except Exception as e:                           # noqa: BLE001
                    self.last_error = str(e)
                finally:
                    self.lock.release()
            time.sleep(max(0.02, period - (time.monotonic() - t0)))

    # ---- 촬영 ---------------------------------------------------------
    def frame_after(self, t_after, settle_s=None, n_best=1, poly=None, timeout_s=30.0):
        """t_after 이후 프레임. n_best > 1 이면 이어서 받은 n 장 중 가장 선명한 한 장. 반환 (bgr, 단조 시각)."""
        self.open()
        settle = float(self.cfg.get("capture.settle_s")) if settle_s is None else settle_s
        with self.lock:
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

    def set_world(self, **kw):
        if self.synth is not None:
            self.synth.set_world(**kw)
