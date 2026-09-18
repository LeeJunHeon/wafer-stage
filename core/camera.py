"""camera.py - 카메라 열기 / 포맷 협상 / 단발 촬영.

핵심 두 가지:
  1) 자동초점을 반드시 끈다. 웨이퍼 위 샘플은 납작하고 대비가 약해 AF 가
     계속 헌팅하고, 흐린 프레임에서는 검출이 0개가 나온다.
  2) 촬영은 연속 프레임 중 '가장 선명한 한 장' 을 고르는 것이다.
"""

import time

import cv2
import numpy as np

# 환경변수(core/__init__.py)로 이미 막았지만, cv2 가 다른 경로로 먼저 import 된
# 뒤라면 그때는 늦다. 런타임에서도 한 번 더 끈다.
try:
    cv2.utils.logging.setLogLevel(cv2.utils.logging.LOG_LEVEL_SILENT)
except Exception:                          # noqa: BLE001
    pass

BACKENDS = [("DSHOW", cv2.CAP_DSHOW), ("MSMF", cv2.CAP_MSMF), ("ANY", cv2.CAP_ANY)]

# 노출 브라케팅 기본값. 한쪽에서 강한 빛이 들어오는 환경에서 자동 노출은 반사광에
# 반응해 장마다 밝기가 요동하고(웨이퍼 안 평균 36~162), 한 장은 포화로 정보가
# 사라진다. 노출을 고정한 여러 장을 Mertens 융합하면 노출시간을 몰라도 된다.
BRACKET_SETTLE_S = 0.4
BRACKET_DISCARD = 3          # 노출을 바꾼 뒤 버리는 프레임 수(드라이버 큐에 남은 옛 장)
BRACKET_MIN_DIFF = 10.0      # 장별 평균 밝기 차이가 이보다 작으면 카메라가 노출을 무시한 것
# 자동 노출 목록(bracket_exposure 가 비어 있을 때). CAP_PROP_EXPOSURE 는 DSHOW 에서
# log2(초) 단위라 한 단계 = 두 배다. 첫 장은 종이가 PAPER_LO~PAPER_HI 가 되게 맞춘다.
BRACKET_START = -5.0         # 탐색 시작 노출
BRACKET_STEP = 1.0           # 한 단계
BRACKET_SEEK_MAX = 5         # 첫 장을 맞추는 최대 시도 횟수
BRACKET_MAX_SHOTS = 3        # 융합할 최대 장 수
BRACKET_DARK_MEAN = 25.0     # 장 전체 평균이 이보다 어두우면 검은 사진 - 버리고 멈춘다
PAPER_LO, PAPER_HI = 200.0, 235.0   # 첫 장에서 종이(밝은 상위 20%) 평균의 목표 범위


def focus_score(bgr_or_gray):
    """라플라시안 분산. 클수록 선명. 절대값은 의미 없고 상대 비교용."""
    if bgr_or_gray is None:
        return 0.0
    g = bgr_or_gray
    if g.ndim == 3:
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY)
    return float(cv2.Laplacian(g, cv2.CV_64F).var())


def _mean_gray(bgr):
    return float(cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY).mean())


def _paper_mean(bgr):
    """종이 밝기 추정: 밝은 상위 20% 화소의 평균(감지영역을 아직 모르므로 전체 프레임)."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    thr = float(np.percentile(g, 80))
    sel = g[g >= thr]
    return float(sel.mean()) if sel.size else float(g.mean())


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
            "wb_temperature": params.get("wb_temperature"),
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
                self.info["actual_size"] = [int(f.shape[1]), int(f.shape[0])]
                self.info["actual_fps"] = float(cap.get(cv2.CAP_PROP_FPS) or 0)
                return self
            cap.release()
            self.cap = None
            errors.append("%s: opened but no frames" % name)

        # 첫 줄은 화면에 그대로 나갈 한 줄 요약, 다음 줄부터는 파일 로그용 상세다.
        raise CameraError(
            "카메라 %d 열기 실패 (설정에서 카메라 번호 확인)\n"
            "  시도한 백엔드: %s\n"
            "  0번은 노트북 내장 카메라입니다."
            % (idx, ", ".join(errors)))

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
        # 화이트밸런스: AUTO_WB 는 끄지만 값을 주지 않으면 카메라 기본값으로 돌아
        # 종이가 초록끼를 띤다. null/0 이면 건드리지 않는다.
        wb = self.p.get("wb_temperature")
        try:
            wb = int(wb or 0)
        except (TypeError, ValueError):
            wb = 0
        if wb > 0:
            cap.set(cv2.CAP_PROP_WB_TEMPERATURE, wb)

        self.info["actual_fourcc"] = _fourcc_str(cap.get(cv2.CAP_PROP_FOURCC))

    # ------------------------------------------------------------------
    def format_text(self):
        """실제로 열린 포맷 한 줄: "MJPG 1280x720 30fps"."""
        sz = self.info.get("actual_size") or [0, 0]
        return "%s %dx%d %.0ffps" % (self.info.get("actual_fourcc") or "?",
                                     sz[0], sz[1], self.info.get("actual_fps") or 0)

    def format_mismatch(self):
        """요청과 실제가 다르면 경고 문구, 같으면 "". 카메라는 못 하는 요청을 조용히
        가까운 값으로 바꾸므로 여기서 잡아 로그로 알린다."""
        want_fcc = self.info["requested_fourcc"]
        want_sz = self.info["requested_size"]
        got_fcc = self.info.get("actual_fourcc")
        got_sz = self.info.get("actual_size") or [0, 0]
        if want_fcc == got_fcc and list(want_sz) == list(got_sz):
            return ""
        return ("%s %dx%d 요청 · %s %dx%d 으로 열림"
                % (want_fcc, want_sz[0], want_sz[1], got_fcc, got_sz[0], got_sz[1]))

    def read(self):
        if self.cap is None:
            return None
        ok, f = self.cap.read()
        return f if ok and f is not None and f.size else None

    def grab(self):
        """프레임 하나를 받아 버리기만 한다(디코딩 없음).

        미리보기처럼 드문드문 볼 때 read() 만 하면 드라이버 큐에 프레임이 쌓여
        영상이 몇 초씩 늦는다. 계속 grab() 으로 큐를 비우고 보여 줄 때만
        retrieve() 로 꺼내면 항상 최신 장면이 나온다.
        """
        if self.cap is None:
            return False
        return bool(self.cap.grab())

    def retrieve(self):
        """마지막으로 grab() 한 프레임을 디코딩해서 돌려준다. 실패면 None."""
        if self.cap is None:
            return None
        ok, f = self.cap.retrieve()
        return f if ok and f is not None and f.size else None

    def capture_best(self, n=None, retry=True, wait_af=True):
        """연속 n 프레임을 받아 가장 선명한 한 장을 고른다.

        AF 를 껐어도 흔들림/노출 변동이 남아 있으므로 단발 촬영에서도 필요하다.
        """
        n = int(n or self.p.get("capture_frames", 15))
        if wait_af and self.p.get("autofocus", True):
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
            return self.capture_best(n, retry=False, wait_af=False)
        i = int(np.argmax(scores))
        stats = {
            "frames_grabbed": len(frames),
            "chosen_focus_score": round(scores[i], 1),
            "focus_min": round(float(np.min(scores)), 1),
            "focus_max": round(float(np.max(scores)), 1),
            "focus_median": round(float(np.median(scores)), 1),
        }
        return frames[i], stats

    # ------------------------------------------------------------------
    def _set_auto_exposure(self, on):
        be = self.info.get("backend")
        if on:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.75 if be == "DSHOW" else 1)
        else:
            self.cap.set(cv2.CAP_PROP_AUTO_EXPOSURE, 0.25 if be == "DSHOW" else 0)

    def _restore_exposure(self):
        """브라케팅 뒤 설정대로 되돌린다(자동 노출이면 다시 켜고, 고정이면 그 값)."""
        use_ae = bool(self.p.get("auto_exposure", True))
        self._set_auto_exposure(use_ae)
        exp = self.p.get("exposure", 0)
        if exp not in (None, "", 0) and not use_ae:
            self.cap.set(cv2.CAP_PROP_EXPOSURE, float(exp))

    def _shoot_at(self, exposure, settle):
        """노출을 넣고 settle 만큼 기다린 뒤 큐에 남은 옛 장을 버리고 한 장(가장 선명한 장)."""
        self.cap.set(cv2.CAP_PROP_EXPOSURE, float(exposure))
        time.sleep(settle)
        for _ in range(BRACKET_DISCARD):
            self.read()
        f, st = self.capture_best(retry=False, wait_af=False)
        return f, st

    def capture_bracket(self):
        """노출 브라케팅 촬영. (검출에 쓸 이미지, stats).

        settings "bracket" 이 켜져 있으면 자동 노출을 끄고 노출을 바꿔 가며 여러 장을
        받아 cv2.createMergeMertens() 로 융합한다(노출시간을 몰라도 되는 방식).

        노출 목록은 밝기를 보며 정한다("bracket_exposure" 가 비어 있을 때 - 기본):
          1) 첫 장: 자동 노출을 끄고 한 장 받아 종이(밝은 상위 20% 화소) 평균이
             200~235 가 되도록 노출을 한 단계씩 올리거나 내린다(최대 5회).
             고정 목록 -4/-6/-8 은 이 카메라에서 194/44/1 이 나와 셋째 장이 검은
             사진이었고 raw.png 로 남긴 가운데 장도 어두웠다.
          2) 거기서 한 단계씩 내리며 장을 더 받는다. 장 전체 평균이 25 미만이면
             버리고 멈춘다. 최대 BRACKET_MAX_SHOTS 장.
        "bracket_exposure" 에 값을 적어 두면 그 목록을 그대로 쓴다(수동 지정).

        stats["raw"] 는 첫 장(가장 밝으면서 포화되지 않은 장 - raw.png 로 남길 것),
        stats["bracketed"] 는 실제로 융합했는지, stats["bracket_exposure"] 와
        stats["bracket_means"] 는 실제로 쓴 노출값과 장별 평균 밝기다.
        카메라가 노출 지시를 무시하면(장별 밝기 차 < 10) 경고를 남기고 단일
        촬영으로 되돌아간다. 장이 2장 미만이어도 마찬가지다.
        """
        if not bool(self.p.get("bracket", True)):
            f, st = self.capture_best()
            st.update({"bracketed": False, "raw": f})
            return f, st
        manual = self.p.get("bracket_exposure") or []
        try:
            manual = [float(v) for v in manual]
        except (TypeError, ValueError):
            manual = []
        settle = float(self.p.get("bracket_settle_s", BRACKET_SETTLE_S) or 0)

        if self.p.get("autofocus", True):
            self.wait_autofocus()
        frames, means, exposures, shots, notes = [], [], [], [], []
        self._set_auto_exposure(False)
        try:
            if manual:
                for e in manual:
                    f, st = self._shoot_at(e, settle)
                    frames.append(f)
                    means.append(_mean_gray(f))
                    exposures.append(e)
                    shots.append({"exposure": e, "mean": round(means[-1], 1),
                                  "focus": st.get("chosen_focus_score")})
            else:
                # 1) 첫 장: 종이가 200~235 가 되는 노출을 찾는다.
                e = float(self.p.get("bracket_start", BRACKET_START))
                f, st = self._shoot_at(e, settle)
                for _ in range(BRACKET_SEEK_MAX):
                    paper = _paper_mean(f)
                    if PAPER_LO <= paper <= PAPER_HI:
                        break
                    e += BRACKET_STEP if paper < PAPER_LO else -BRACKET_STEP
                    f, st = self._shoot_at(e, settle)
                notes.append("첫 장 노출 %g (종이 %.0f)" % (e, _paper_mean(f)))
                frames.append(f)
                means.append(_mean_gray(f))
                exposures.append(e)
                shots.append({"exposure": e, "mean": round(means[-1], 1),
                              "focus": st.get("chosen_focus_score")})
                # 2) 한 단계씩 내리며 더 받는다. 검은 장(평균 < 25)은 버리고 멈춘다.
                while len(frames) < BRACKET_MAX_SHOTS:
                    e -= BRACKET_STEP
                    f, st = self._shoot_at(e, settle)
                    m = _mean_gray(f)
                    if m < BRACKET_DARK_MEAN:
                        notes.append("노출 %g 는 평균 %.0f · 버림" % (e, m))
                        break
                    frames.append(f)
                    means.append(m)
                    exposures.append(e)
                    shots.append({"exposure": e, "mean": round(m, 1),
                                  "focus": st.get("chosen_focus_score")})
        finally:
            self._restore_exposure()

        stats = {"bracketed": False, "bracket_exposure": exposures,
                 "bracket_means": [round(m, 1) for m in means], "bracket_shots": shots,
                 "bracket_notes": notes, "raw": frames[0] if frames else None,
                 "frames_grabbed": len(frames)}
        if len(frames) < 2:
            stats["bracket_warning"] = "브라케팅 장이 2장 미만 · 단일 촬영"
            f, st = self.capture_best()
            st.update(stats, raw=f)
            return f, st
        if max(means) - min(means) < BRACKET_MIN_DIFF:
            stats["bracket_warning"] = "노출이 바뀌지 않음 · 브라케팅을 건너뜁니다"
            f, st = self.capture_best()
            st.update(stats, raw=f)
            return f, st
        merge = cv2.createMergeMertens()
        fused = merge.process([f for f in frames])
        fused = np.clip(fused * 255.0 + 0.5, 0, 255).astype(np.uint8)
        stats["bracketed"] = True
        stats["chosen_focus_score"] = focus_score(fused)
        return fused, stats

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

    def release(self):
        if self.cap is not None:
            self.cap.release()
            self.cap = None

    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.release()
