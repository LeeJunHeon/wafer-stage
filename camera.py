"""camera.py - 카메라 열기 / 포맷 협상 / 단발 촬영.

핵심 두 가지:
  1) 자동초점을 반드시 끈다. 웨이퍼 위 샘플은 납작하고 대비가 약해 AF 가
     계속 헌팅하고, 흐린 프레임에서는 검출이 0개가 나온다.
  2) 촬영은 연속 프레임 중 '가장 선명한 한 장' 을 고르는 것이다.
"""

import time

import cv2
import numpy as np

BACKENDS = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY)]


def focus_score(bgr_or_gray):
    """라플라시안 분산. 클수록 선명. 절대값은 의미 없고 상대 비교용."""
    if bgr_or_gray is None:
        return 0.0
    g = bgr_or_gray
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def _fourcc_str(v):
    v = int(v)
    if v <= 0:
        return "NONE"
    s = "".join(chr((v >> (8 * i)) & 0xFF) for i in range(4))
    return "".join(c if 32 <= ord(c) < 127 else "?" for c in s)


class CameraError(Exception):
    pass


class Camera:
    def __init__(self, params):
        self.p = params
        self.cap = None
        self.info = {
            "index": int(params.get("camera_index", 1)),
            "backend": None,
            "requested_fourcc": str(params.get("fourcc", "MJPG")),
            "actual_fourcc": None,
            "requested_size": [int(params.get("width", 1280)), int(params.get("height", 720))],
            "autofocus": bool(params.get("autofocus", True)),
            "auto_exposure": bool(params.get("auto_exposure", True)),
            "autofocus_off": False,
            "focus": params.get("focus", -1),
            "exposure": params.get("exposure", 0),
        }

    # ------------------------------------------------------------------
    def open(self):
        idx = int(self.p.get("camera_index", 1))   # 기본 1: 0번은 노트북 내장 카메라
        want = str(self.p.get("backend", "auto")).upper()
        order = BACKENDS if want in ("AUTO", "") else \
            [b for b in BACKENDS if b[0] == want] + [b for b in BACKENDS if b[0] != want]

        errors = []
        for name, flag in order:
            cap = cv2.VideoCapture(idx, flag)
            # isOpened() 만 믿으면 안 된다. 열리기는 했는데 프레임이 안 나오는
            # 카메라가 실제로 있으므로 read() 까지 확인한다.
            if not cap.isOpened():
                cap.release()
                errors.append("%s: open failed" % name)
                continue
            self.cap = cap
            self.info["backend"] = name
            self._configure(name)
            ok = False
            for _ in range(10):
                r, f = cap.read()
                if r and f is not None and f.size:
                    ok = True
                    break
            if ok:
                self.info["actual_fourcc"] = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))
                return self
            cap.release()
            self.cap = None
            errors.append("%s: opened but no frames" % name)

        raise CameraError(
            "카메라 인덱스 %d 를 열지 못했습니다.\n  %s\n"
            "settings.json 의 camera_index 를 확인하세요 (0번은 노트북 내장 카메라)."
            % (idx, "\n  ".join(errors)))

    # ------------------------------------------------------------------
    def _configure(self, backend_name):
        cap = self.cap
        w = int(self.p.get("width", 1280))
        h = int(self.p.get("height", 720))
        fcc = str(self.p.get("fourcc", "MJPG"))
        code = cv2.VideoWriter_fourcc(*fcc) if len(fcc) == 4 else 0

        def set_fourcc():
            if code:
                cap.set(cv2.CAP_PROP_FOURCC, code)

        def set_size():
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)

        def set_fps():
            cap.set(cv2.CAP_PROP_FPS, 30)

        # 드라이버마다 설정을 먹는 순서가 달라 한 번에는 반드시 실패한다.
        # 순서를 바꿔가며 시도하고 매번 FOURCC 를 되읽어 확인한다.
        orders = [
            (set_fourcc, set_size, set_fps),
            (set_size, set_fourcc, set_fps),
            (set_fourcc, set_fps, set_size),
            (set_fps, set_fourcc, set_size),
        ]
        for seq in orders:
            for fn in seq:
                fn()
            if _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)) == fcc:
                break

        # --- 자동초점 ---
        # 기본은 켜기. AF 를 끄면 렌즈가 '마지막에 있던 자리' 에 그대로 멈춰
        # 모든 프레임이 고르게 흐려지는 사고가 실제로 났다. AF 헌팅은 촬영 때
        # 15프레임 중 가장 선명한 한 장을 고르는 것으로 흡수한다.
        use_af = bool(self.p.get("autofocus", True))
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 1 if use_af else 0)
        cap.set(cv2.CAP_PROP_AUTO_WB, 0)
        # 자동노출: 끄기만 하고 값을 안 주면 마지막 노출에 얼어붙는다 (초점과 같은 함정).
        # 기본은 켜기. 끄려면 settings.json 의 exposure 에 실제 값을 함께 지정할 것.
        use_ae = bool(self.p.get("auto_exposure", True))
        if use_ae:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75 if backend_name == "DSHOW" else 1)
        else:
            cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if backend_name == "DSHOW" else 0)
        self.info["auto_exposure"] = use_ae
        self.info["autofocus"] = use_af
        self.info["autofocus_off"] = not use_af

        focus = self.p.get("focus", -1)
        if isinstance(focus, str):           # "auto" = 열고 나서 스윕으로 찾는다
            focus = -1
        focus = int(focus)
        if focus >= 0 and not use_af:        # -1 이거나 AF 사용 중이면 건드리지 않는다
            cap.set(cv2.CAP_PROP_FOCUS, focus)
        exp = self.p.get("exposure", 0)
        if exp not in (None, "", 0) and not use_ae:
            cap.set(cv2.CAP_PROP_EXPOSURE, float(exp))

        self.info["actual_fourcc"] = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))

    # ------------------------------------------------------------------
    def read(self):
        if self.cap is None:
            return None
        ok, f = self.cap.read()
        return f if ok and f is not None and f.size else None

    def capture_best(self, n=None, retry=True):
        """연속 n 프레임을 받아 가장 선명한 한 장을 고른다.

        AF 를 껐어도 흔들림/노출 변동이 남아 있으므로 단발 촬영에서도 필요하다.
        """
        n = int(n or self.p.get("capture_frames", 15))
        if self.p.get("autofocus", True):
            self.wait_autofocus()
        frames, scores = [], []
        for _ in range(n):
            f = self.read()
            if f is None:
                continue
            frames.append(f)
            scores.append(focus_score(f))
        if not frames:
            raise CameraError("촬영 중 프레임을 한 장도 받지 못했습니다.")
        # 가장 선명한 장이 '마지막 몇 장' 안에 있고 편차가 크면, AF 가 아직
        # 올라오는 중이었다는 뜻이다. 그 상태로 확정하면 최고점을 놓친다.
        if (retry and len(scores) >= 6
                and int(np.argmax(scores)) >= len(scores) - 3
                and max(scores) / max(min(scores), 1e-6) >= 3.0):
            return self.capture_best(n, retry=False)
        i = int(np.argmax(scores))
        stats = {
            "frames_grabbed": len(frames),
            "chosen_focus_score": round(scores[i], 1),
            "focus_min": round(float(np.min(scores)), 1),
            "focus_max": round(float(np.max(scores)), 1),
            "focus_median": round(float(np.median(scores)), 1),
        }
        return frames[i], stats

    def wait_autofocus(self, progress=None):
        """AF 가 수렴할 때까지 기다린다.

        1차 시도는 '최고점이 patience 동안 안 오르면 끝' 이었는데, 렌즈가 흐린
        구간을 천천히 지나갈 때 그 정체를 수렴으로 오인하고 빠져나왔다. 그래서
        선명도가 7~1819 (중앙값 20) 로 벌어진 촬영이 나왔다.

        지금은 '최근 프레임들의 선명도가 서로 비슷한가' 를 본다. 헌팅 중에는
        연속 프레임 점수가 크게 출렁이고, 초점이 잠기면 잔잔해진다. 더불어
        그 안정 구간이 지금까지 본 최고점 근처인지도 함께 본다 (흐린 채로
        잠깐 잔잔한 구간과 구분하기 위해).
        """
        timeout = float(self.p.get("af_settle_s", 5.0))
        min_wait = float(self.p.get("af_min_wait_s", 0.5))
        window = []
        best = 0.0
        t0 = time.time()
        while True:
            el = time.time() - t0
            if el >= timeout:
                break
            f = self.read()
            if f is None:
                continue
            sc = focus_score(f)
            best = max(best, sc)
            window.append(sc)
            if len(window) > 5:
                window.pop(0)
            if progress:
                progress(sc)
            if el >= min_wait and len(window) == 5:
                lo, hi = min(window), max(window)
                stable = hi / max(lo, 1e-6) < 1.25          # 출렁임이 멎었는가
                at_peak = hi >= 0.85 * best                 # 그리고 최고점 근처인가
                if stable and at_peak:
                    break
        return best

    # ------------------------------------------------------------------
    def focus_sweep(self, progress=None):
        """CAP_PROP_FOCUS 를 훑어 가장 선명한 위치를 찾아 고정한다 (원샷 AF).

        자동초점을 끄면 렌즈는 '마지막에 있던 자리'에 그대로 멈춘다. 그 자리가
        웨이퍼 거리와 안 맞으면 모든 프레임이 똑같이 흐리고, 선명도 점수는
        흔들리지 않으므로 'autofocus may still be active' 경고에도 안 걸린다.
        실제로 그렇게 찍힌 사진이 나왔다. 그래서 한 번 훑어서 직접 맞춘다.
        """
        cap = self.cap
        if cap is None:
            raise CameraError("카메라가 열려 있지 않습니다.")
        cap.set(cv2.CAP_PROP_AUTOFOCUS, 0)

        settle = float(self.p.get("focus_settle_s", 0.35))

        def measure(v):
            cap.set(cv2.CAP_PROP_FOCUS, float(v))
            t_end = time.time() + settle
            while time.time() < t_end:       # 렌즈가 움직이는 동안의 프레임은 버린다
                cap.read()
            best = 0.0
            for _ in range(3):
                f = self.read()
                if f is not None:
                    best = max(best, focus_score(f))
            return best

        lo = int(self.p.get("focus_min", 0))
        hi = int(self.p.get("focus_max", 255))
        coarse = max(1, int(self.p.get("focus_step", 15)))
        curve = []
        for v in range(lo, hi + 1, coarse):
            s = measure(v)
            curve.append((v, round(s, 1)))
            if progress:
                progress(v, s)

        vals = [s for _, s in curve]
        if not vals or max(vals) <= 0:
            return {"supported": False, "reason": "프레임을 받지 못했습니다.",
                    "curve": curve, "best_value": None, "best_score": 0.0}
        # 어느 위치에서나 점수가 같으면 이 카메라는 초점 제어를 안 받아준다
        # (고정초점 렌즈이거나 드라이버가 무시). 그때는 렌즈/거리를 손으로 맞춰야 한다.
        if max(vals) / max(min(vals), 1e-6) < 1.15:
            return {"supported": False,
                    "reason": "focus 값을 바꿔도 선명도가 변하지 않습니다 "
                              "(고정초점이거나 드라이버가 무시).",
                    "curve": curve, "best_value": None, "best_score": max(vals)}

        best_v = curve[int(np.argmax(vals))][0]
        # 굵게 훑은 뒤 그 주변만 촘촘히 다시 훑는다
        fine = max(1, coarse // 4)
        for v in range(max(lo, best_v - coarse), min(hi, best_v + coarse) + 1, fine):
            if any(c[0] == v for c in curve):
                continue
            s = measure(v)
            curve.append((v, round(s, 1)))
            if progress:
                progress(v, s)
        curve.sort()
        best_v, best_s = max(curve, key=lambda c: c[1])

        measure(best_v)                      # 최적값으로 고정
        self.p["focus"] = best_v
        self.info["focus"] = best_v
        self.info["focus_sweep_score"] = round(best_s, 1)
        return {"supported": True, "reason": "", "curve": curve,
                "best_value": best_v, "best_score": best_s}

    def release(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()


def capture_warnings(info, stats, session_max_score, min_score=0):
    """촬영 관련 경고 문구.

    min_score 는 '세션 최대 대비' 로는 못 잡는 경우를 위한 절대 하한이다.
    렌즈가 엉뚱한 자리에 고정되면 모든 프레임이 고르게 흐려서 상대 기준은
    아무 경고도 못 낸다. 실제로 그렇게 놓친 촬영이 있었다.
    """
    w = []
    fmin = max(stats.get("focus_min", 0.0), 1e-6)
    spread = stats.get("focus_max", 0.0) / fmin
    if spread >= 3.0:
        # AF 를 켠 상태에서는 예상된 일이지만, 폭이 크면 고른 한 장도
        # 운에 좌우되므로 그대로 알려 준다.
        w.append("autofocus was hunting (focus varied %.0fx across frames)" % spread
                 if info.get("autofocus") else "autofocus may still be active")
    chosen = stats.get("chosen_focus_score", 0.0)
    if session_max_score and chosen < 0.35 * session_max_score:
        w.append("image may be out of focus")
    elif min_score and chosen < float(min_score):
        w.append("image may be out of focus (score %.0f < %.0f)"
                 % (chosen, float(min_score)))
    if (info.get("actual_fourcc") or "").upper() != (info.get("requested_fourcc") or "").upper():
        if (info.get("actual_fourcc") or "").upper().startswith("YUY"):
            w.append("camera stream is uncompressed (YUY2)")
        else:
            w.append("camera stream is uncompressed (%s)" % info.get("actual_fourcc"))
    return w
