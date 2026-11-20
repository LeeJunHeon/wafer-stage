"""prep.py - 초기 설정 탭 2~7단계와 사진 · 좌표 · 샘플 찾기. 이동 · 릴레이는 전부 Controller 의 안전 규칙을 지난다.

사진(photo): 마지막으로 찍은 원본 한 장. 판 꼭짓점이 정해져 있으면 보정 사진(판을 위에서 본 정사각형)을 만들어 둔다.
위치 확인: 촬영 위치에서 찍은 사진마다 헤드 마커를 찾아 기억한 자리와의 차이(mm)를 낸다.
왜 마커만으로 좌표를 맞추지 않는가: 마커는 헤드 윗면에 있어 판보다 높다. 비스듬한 카메라에서는 높이가 다르면 같은 X · Y 라도
사진에서 다른 자리에 보인다. 그래서 판 위의 점(꼭짓점)에 핀을 직접 대어 맞추고, 마커는 "원점이나 카메라가 움직였는지" 확인에만 쓴다.
"""

import threading
import time

import cv2
import numpy as np

from app import config as config_mod
from app import geometry as geo
from app import imgio
from app.calib import CORNERS
from app.devices.camera import CameraError
from app.safety import Rejected
from app.state import ALARM, NOTICE
from app.vision import find as find_mod

SNAP_MM = 8.0                                      # 판 사진을 누른 자리가 샘플 가운데에서 이 안이면 샘플 가운데로 붙는다
AT_TOL_MM = 0.2                                    # "그 자리에 있다"고 보는 차이


class Prep:
    def __init__(self, ctrl):
        self.c = ctrl
        self._lock = threading.RLock()
        self.photo = None                           # {"frame", "t", "id", "at_head": 촬영 위치에서 찍었는가}
        self._photo_n = 0
        self._rect = {}                             # 보정 사진 캐시: 이름 → (열쇠, 그림)
        self._empty = None                          # 빈 판 원본(파일에서 읽어 둔 것)
        self.marker_seen = None                     # 마지막으로 헤드 마커가 보였는가
        self.pos_off_mm = None                      # 마지막 위치 확인 결과(mm)
        self._pos_block = False                     # 위치 확인 경보(측정 시작 막음) - pos_block 으로 읽고 쓴다
        self.samples = []
        self.find_info = None                       # {"t", "n", "notes", "path", "ms"}
        self.last_click = None                      # 판 사진에서 마지막으로 누른 자리 {"px": 사진 픽셀, "plate": 판 mm}
        self.contact = {"active": False, "z": None, "text": "", "result": None, "fail": ""}
        self.focus = {"active": False, "points": [], "value": None}      # 초점 찾기 진행 · 결과(초기 설정 3)
        if self.c.calib.note:
            self.c.state.alarm("calib", self.c.calib.note, NOTICE)

    @property
    def pos_block(self):
        return self._pos_block

    @pos_block.setter
    def pos_block(self, on):
        """위치 확인 경보(측정 시작 막음)가 바뀌면 측정 시작 조건을 그 순간 다시 계산한다."""
        on = bool(on)
        if on != self._pos_block:
            self._pos_block = on
            self.c.gate_inputs_changed()

    # ------------------------------------------------------------------
    # 공통
    # ------------------------------------------------------------------
    @property
    def cfg(self):
        return self.c.cfg

    @property
    def calib(self):
        return self.c.calib

    def _size_mm(self):
        return float(self.cfg["plate_size_mm"])

    def _need_camera(self):
        if not self.c.camera.connected:
            raise Rejected("카메라 연결 안 됨")

    def _need_xy(self):
        self.c._need_stage()
        if not (self.c.stage.homed.get("x") and self.c.stage.homed.get("y")):
            raise Rejected("X · Y 원점 필요 (초기 설정 2)")

    def _xy(self):
        return self.c._mm("x"), self.c._mm("y")

    def _at(self, xy):
        x, y = self._xy()
        return x is not None and abs(x - xy[0]) <= AT_TOL_MM and abs(y - xy[1]) <= AT_TOL_MM

    # ------------------------------------------------------------------
    # 사진
    # ------------------------------------------------------------------
    def _fresh(self):
        self._need_camera()
        try:
            return self.c.camera.fresh_frame()
        except CameraError as e:
            raise Rejected(str(e))

    @staticmethod
    def _small_gray(frame):
        g = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY) if frame.ndim == 3 else frame
        w = 320
        if g.shape[1] > w:
            g = cv2.resize(g, (w, max(1, int(round(g.shape[0] * w / g.shape[1])))), interpolation=cv2.INTER_AREA)
        return g

    def _wait_real(self, g, seconds):
        """실제 시간으로 기다린다(카메라는 실제 시간으로 프레임을 낸다). g 가 있으면 [정지] · 비상정지(세대가 바뀜)에 바로 멈춘다."""
        end = time.monotonic() + float(seconds)
        while True:
            if g is not None and self.c.safety.generation != g:
                raise _FocusStop()
            left = end - time.monotonic()
            if left <= 0:
                return
            time.sleep(min(0.05, left))

    def settled(self, why="이동", g=None, wait_s=None):
        """이동 · 초점 변경 뒤의 사진 한 장(마커 등록 · 위치 확인 · 촬영 · 빈 판 · 재촬영 · 초점 찾기가 모두 이것을 쓴다):
        cam_settle_wait_s 기다림 → 연속 새 프레임 두 장(줄인 회색 사진)의 평균 차이가 cam_still_diff 이하가 될 때까지 →
        cam_still_timeout_s 안에 안 멈추면 Rejected "카메라 화면 안정 안 됨". 걸린 시간 · 장수는 로그에.
        까닭: 4080×3060 MJPG(DSHOW) 프레임이 실제보다 늦어, 새 프레임 두 장만 버리면 이동 중 장면이 들어왔다(같은 자리 두 장의 마커가 61.6 · 110.1 px 달랐다)."""
        c = self.cfg
        t0 = time.monotonic()
        self._wait_real(g, float(c["cam_settle_wait_s"]) if wait_s is None else wait_s)
        limit = t0 + float(c["cam_still_timeout_s"])
        prev, n, d = None, 0, None
        while True:
            if g is not None and self.c.safety.generation != g:
                raise _FocusStop()
            frame, t = self._fresh()
            n += 1
            sm = self._small_gray(frame)
            if prev is not None and prev.shape == sm.shape:
                d = float(cv2.absdiff(sm, prev).mean())
                if d <= float(c["cam_still_diff"]):
                    self.c.logs.app("화면 멈춤 · %s 뒤 %.2f s · %d장 · 차이 %.2f" % (why, time.monotonic() - t0, n, d), "detail")
                    return frame, t
            prev = sm
            if time.monotonic() > limit:
                self.c.logs.app("카메라 화면 안정 안 됨 · %s 뒤 %.1f s · %d장 · 마지막 차이 %s" % (why, time.monotonic() - t0, n, "—" if d is None else "%.2f" % d), "warn")
                raise Rejected("카메라 화면 안정 안 됨")

    def capture(self, move=True, need_head=False):
        """사진 한 장. move 면 핀 맨 위 → 촬영 위치로 간 뒤, 이동이 끝나고 새로 들어온 프레임을 쓴다.
        need_head: 촬영 위치가 정해져 있지 않으면 거절(빈 판 · 샘플 사진). 아니면 지금 자리에서 찍는다(초기 설정 3단계)."""
        self._need_camera()
        self.c._no_run("촬영")
        head = self.cfg["photo_head_xy"]
        at_head = False
        if head is None:
            if need_head:
                raise Rejected("촬영 위치 설정 필요 (초기 설정 3)")
        elif move:
            if not self.c.stage.connected or not all(self.c.stage.homed.get(a) for a in "xyz"):
                if need_head:
                    raise Rejected("스테이지 연결 · 세 축 원점 필요")
            else:
                moved = False
                if not self._at(head) or self.c._mm("z") != 0:
                    self.c.goto(head[0], head[1])
                    moved = True
                at_head = True
        if at_head:                                                  # 촬영 위치의 사진: 화면이 멈춘 뒤(방금 움직였으면 cam_settle_wait_s 도 기다린다)
            frame, t = self.settled("이동" if moved else "촬영", wait_s=None if moved else 0.0)
        else:
            frame, t = self._fresh()
        with self._lock:
            self._photo_n += 1
            self.photo = {"frame": frame, "t": t, "id": self._photo_n, "at_head": at_head}
            self._rect.pop("photo", None)
        if at_head:
            self._position_check(frame)
        else:
            self.marker_seen = geo.find_marker(frame) is not None
        self.c.logs.app("사진 찍음 · %d×%d%s" % (frame.shape[1], frame.shape[0], " · 촬영 위치" if at_head else ""))
        return frame

    def _position_check(self, frame):
        """촬영 위치에서 찍은 사진: 헤드 마커가 기억한 자리와 얼마나 다른지(mm). 2 mm 넘으면 주의, 10 mm 넘으면 경보 + 측정 시작 막음."""
        st = self.c.state
        m = geo.find_marker(frame)
        self.marker_seen = m is not None
        if not self.calib.d.get("marker"):
            return
        if m is None:
            self.pos_off_mm = None
            st.alarm("pos_check", "마커 미검출 · 헤드 위치 확인 실패", NOTICE)
            return
        off = self.calib.head_offset_mm(m)
        self.pos_off_mm = None if off is None else round(off, 2)
        if off is None:
            return
        dx, dy = self.calib.head_offset_xy(m)
        mk = self.calib.d["marker"]["px"]
        self.c.logs.app("위치 확인 · 마커 (%.1f, %.1f) · 등록 (%.1f, %.1f) · 차이 X %+.2f · Y %+.2f mm · %.2f mm" % (m[0], m[1], mk[0], mk[1], dx, dy, off), "detail")
        text = "헤드 위치 %.1f mm 차이 · 원점 확인 필요" % off
        if off > float(self.cfg["pos_block_mm"]):
            self.pos_block = True
            st.alarm("pos_check", text, ALARM)
            self.c.logs.app("위치 확인 경보 · %.2f mm" % off, "error")
        elif off > float(self.cfg["pos_warn_mm"]):
            self.pos_block = False
            st.alarm("pos_check", text, NOTICE)
            self.c.logs.app("위치 확인 주의 · %.2f mm" % off, "warn")
        else:
            self.pos_block = False
            st.clear_alarm("pos_check")

    def clear_pos_check(self):
        """원점을 다시 맞추거나 좌표 맞추기를 다시 하면 위치 확인 경보가 풀린다."""
        self.pos_block = False
        self.pos_off_mm = None
        self.c.state.clear_alarm("pos_check")

    # ---- 보정 사진 ----
    def _rectified(self, name, frame, key):
        """판 영역만 잘라 보정한 사진(검출용 여백 포함). 판 꼭짓점이 없거나 사진 크기가 다르면 None."""
        p = self.calib.d.get("plate")
        if frame is None or not p:
            return None
        if [frame.shape[1], frame.shape[0]] != p["size"]:
            return None
        key = (key, p["t"], self._size_mm())
        with self._lock:
            hit = self._rect.get(name)
            if hit and hit[0] == key:
                return hit[1]
        H = self.calib.plate_h(self._size_mm())
        if H is None:
            return None
        img = geo.rectify(frame, H, self._size_mm(), geo.RECT_PPMM, geo.RECT_MARGIN_MM)
        with self._lock:
            self._rect[name] = (key, img)
        return img

    def photo_rect(self):
        ph = self.photo
        return None if ph is None else self._rectified("photo", ph["frame"], ph["id"])

    def empty_frame(self):
        """빈 판 원본(calib/empty_plate.png). 한 번 읽어 둔다(찍은 때 t 가 바뀌면 다시 읽는다). 없으면 None.
        calib 에는 있다고 적혀 있는데 파일을 못 읽으면(없음 · 깨짐 · 경로 문제) 말없이 넘어가지 않는다: 주의를 한 번 띄우고 경로와 까닭을 로그에 남긴다.
        샘플 검출은 지금 규칙대로 사진 한 장으로 계속한다. 파일은 imgio 로 읽는다(cv2.imread 는 Windows 에서 한글 경로를 못 읽는다)."""
        e = self.calib.d.get("empty")
        if not e:
            return None
        if self._empty is None or self._empty[0] != e["t"]:
            path = self.calib.empty_path
            try:
                img = imgio.read(path)
            except imgio.ImageIOError as err:
                img = None
                self.c.logs.app("빈 판 사진 읽기 실패 · %s · %s" % (path, err), "warn")
                self.c.state.alarm("empty_read", "빈 판 사진 읽기 실패 · 초기 설정 4 재촬영 필요", NOTICE, dismiss=True)
            self._empty = (e["t"], img)
        return self._empty[1]

    def _empty_unreadable(self):
        """calib 에 빈 판 사진이 있다고 적혀 있는데 읽어 보니 못 읽었다(읽어 본 결과만 본다 - 여기서 파일을 읽지 않는다)."""
        e = self.calib.d.get("empty")
        return bool(e) and self._empty is not None and self._empty[0] == e["t"] and self._empty[1] is None

    def empty_rect(self):
        e = self.calib.d.get("empty")
        return None if not e else self._rectified("empty", self.empty_frame(), e["t"])

    @staticmethod
    def _inner(img):
        m = int(round(geo.RECT_MARGIN_MM * geo.RECT_PPMM))
        return None if img is None else img[m:img.shape[0] - m, m:img.shape[1] - m]

    @staticmethod
    def _jpeg(img, max_w=None, q=85):
        if img is None:
            return None
        if max_w and img.shape[1] > max_w:
            img = cv2.resize(img, (max_w, int(round(img.shape[0] * max_w / img.shape[1]))), interpolation=cv2.INTER_AREA)
        ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, q])
        return buf.tobytes() if ok else None

    # ------------------------------------------------------------------
    # 화면 방향(문 기준): 그림 · 덧그림 좌표는 화면 방향으로 보내고, 화면 입력(view=True)은 여기서 저장 좌표로 바꾼다(geometry.to_view · from_view 한 곳)
    # ------------------------------------------------------------------
    def _vr(self):
        return int(self.cfg["view_rot"]), bool(self.cfg["view_flip"])

    def _vimg(self, img):
        return geo.rotate_img(img, *self._vr())

    def _vplate(self, p):
        """판 mm → 화면 판 mm."""
        S = self._size_mm()
        u, v = geo.to_view(float(p[0]), float(p[1]), S, S, *self._vr())
        return [round(u, 3), round(v, 3)]

    def _from_vplate(self, u, v):
        S = self._size_mm()
        return geo.from_view(float(u), float(v), S, S, *self._vr())

    def vname(self, stored):
        """저장 꼭짓점 이름 → 화면 이름(화면 왼쪽 위부터 시계 방향)."""
        return geo.view_names(*self._vr())[stored]

    def sname(self, view_name):
        """화면 꼭짓점 이름 → 저장 이름."""
        return geo.store_name(view_name, *self._vr())

    def _dirs(self):
        """사진 칸 둘레의 방향 글: 위 = 안쪽 · 아래 = 문 · 왼쪽 · 오른쪽 = 장비 오른쪽의 반대 · 장비 오른쪽."""
        c = self.cfg
        return {"top": "안쪽", "bottom": "문", "left": config_mod.SIDE_NAME[_opposite(c["machine_right_side"])],
                "right": config_mod.SIDE_NAME[c["machine_right_side"]]}

    def _view_check(self):
        """회전 설정(view_rot · view_flip)이 꼭짓점 방향(초기 설정 5 + 장비 앞쪽 · 오른쪽)과 맞는가 - 다르면 주의."""
        c = self.cfg
        ok = geo.view_matches(self.calib.d["coords"]["stage"], *self._vr(), c["machine_front_side"], c["machine_right_side"])
        if ok is False:
            self.c.state.alarm("view_rot", "사진 회전 설정과 꼭짓점 방향 다름", NOTICE)
        else:
            self.c.state.clear_alarm("view_rot")
        return ok

    def preview_jpeg(self):
        """실시간 미리보기(화면 방향). 돌릴 일이 없으면 카메라의 JPEG 그대로."""
        jpg = self.c.camera.preview_jpeg()
        rot, flip = self._vr()
        if jpg is None or (rot == 0 and not flip):
            return jpg
        img = cv2.imdecode(np.frombuffer(jpg, np.uint8), cv2.IMREAD_COLOR)
        return self._jpeg(self._vimg(img), None, 80) if img is not None else None

    def jpeg(self, what, **kw):
        """화면용 그림. what: raw(찍은 원본, 가로 1600 까지) · crop(원본 해상도 조각: x, y, half) · plate(보정한 판 사진) ·
        empty(보정한 빈 판 사진). 없으면 None."""
        ph = self.photo
        if what == "raw":
            return None if ph is None else self._jpeg(self._vimg(ph["frame"]), 1600)          # 화면 방향(문 기준)
        if what == "crop":                                           # x, y = 화면 방향 사진의 픽셀(kw view) · 아니면 원본 픽셀
            if ph is None:
                return None
            f = ph["frame"]
            x, y, half = float(kw.get("x", 0)), float(kw.get("y", 0)), max(20, min(400, int(float(kw.get("half", 120)))))
            if kw.get("view"):
                x, y = geo.from_view(x, y, f.shape[1], f.shape[0], *self._vr())
            x, y = int(x), int(y)
            pad = cv2.copyMakeBorder(f, half, half, half, half, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            x, y = max(0, min(f.shape[1] - 1, x)), max(0, min(f.shape[0] - 1, y))
            return self._jpeg(self._vimg(pad[y:y + 2 * half, x:x + 2 * half]), None, 90)
        if what == "plate":
            return self._jpeg(self._vimg(self._inner(self.photo_rect())))
        if what == "empty":
            return self._jpeg(self._vimg(self._inner(self.empty_rect())))
        return None

    REGION_MAX_OUT = 2048                                            # 조각 한 장의 가장 큰 가로(px) - 화면이 이보다 넓게 보여 줄 일은 없다

    def region(self, src, x, y, w, h, out=None):
        """크게 보기 창(ui/zoom.js)이 받는 원본 해상도 조각. src: raw(찍은 원본 사진) · plate(보정한 판 사진).
        x, y, w, h = **화면 방향(문 기준)** 사진의 픽셀 사각형(사진 안이어야 한다 - 밖이면 Rejected). 조각도 화면 방향으로 돌려 준다. out = 돌려줄 가로(px, 16~2048) - 조각보다 크면 늘리지 않는다
        (원본 화소 그대로 - 화면이 화소를 그대로 확대한다). 사진이 없으면 None."""
        if src == "raw":
            img = None if self.photo is None else self.photo["frame"]
        elif src == "plate":
            img = self._inner(self.photo_rect())
        else:
            raise Rejected("사진 종류 오류 · raw 또는 plate")
        if img is None:
            return None
        H0, W0 = img.shape[:2]
        rot, flip = self._vr()
        W, H = geo.view_size(W0, H0, rot)
        x, y, w, h = int(x), int(y), int(w), int(h)
        if w < 1 or h < 1 or x < 0 or y < 0 or x + w > W or y + h > H:
            raise Rejected("범위 밖 · 사진 %d×%d" % (W, H))
        o = w if out is None else max(16, min(self.REGION_MAX_OUT, int(out)))
        o = min(o, w)
        a = geo.from_view(x, y, W0, H0, rot, flip)                  # 화면 사각형 → 원본 사각형
        b = geo.from_view(x + w, y + h, W0, H0, rot, flip)
        x0, x1 = int(round(min(a[0], b[0]))), int(round(max(a[0], b[0])))
        y0, y1 = int(round(min(a[1], b[1]))), int(round(max(a[1], b[1])))
        piece = self._vimg(img[max(0, y0):min(H0, y1), max(0, x0):min(W0, x1)])
        if o < w:
            piece = cv2.resize(piece, (o, max(1, int(round(h * o / w)))), interpolation=cv2.INTER_AREA)
        return self._jpeg(piece, None, 90)

    # ------------------------------------------------------------------
    # 3 · 카메라 · 판 영역
    # ------------------------------------------------------------------
    def photo_head_set(self):
        """지금 자리를 촬영 위치로 정한다. 그 자리에서 헤드 마커가 보이는지도 알려 준다."""
        self._need_xy()
        self.c._no_run("촬영 위치 정하기")
        x, y = self._xy()
        self.c._remember(photo_head_xy=[round(x, 2), round(y, 2)])
        seen = None
        if self.c.camera.connected:
            frame, _t = self._fresh()
            seen = geo.find_marker(frame) is not None
            self.marker_seen = seen
        self.calib.set_marker(None, None)                            # 자리가 바뀌면 기억한 마커 자리는 못 쓴다
        self.clear_pos_check()
        self.publish()
        return {"xy": self.cfg["photo_head_xy"], "marker": seen}

    def marker_check(self):
        frame, _t = self._fresh()
        self.marker_seen = geo.find_marker(frame) is not None
        self.publish()
        return {"marker": self.marker_seen}

    def photo_take(self):
        """초기 설정 3단계 [사진 찍기]: 촬영 위치가 정해져 있고 갈 수 있으면 거기로 가서, 아니면 지금 자리에서 찍는다."""
        self.capture(move=True, need_head=False)
        self.publish()
        return {"photo": self._photo_view()}

    def plate_set(self, corners, view=False):
        """사진에서 누른 판 꼭짓점 A · B · C · D(원본 픽셀, 왼쪽 위부터 시계 방향) → "사진 → 판 mm" 변환.
        view = 화면(문 기준)에서 누른 점 - 화면 이름 A · B · C · D 순서 · 화면 픽셀 → 저장(사진 기준) 이름 순서 · 원본 픽셀로 바꾼다."""
        self.c._no_run("판 영역 정하기")
        ph = self.photo
        if ph is None:
            raise Rejected("촬영 필요")
        try:
            pts = [[float(p[0]), float(p[1])] for p in corners]
        except (TypeError, ValueError, IndexError):
            raise Rejected("꼭짓점 4개 선택 필요")
        if len(pts) != 4:
            raise Rejected("꼭짓점 4개 선택 필요")
        h, w = ph["frame"].shape[:2]
        if view:
            rot, flip = self._vr()
            by = {self.sname(geo.VIEW_NAMES[i]): list(geo.from_view(p[0], p[1], w, h, rot, flip)) for i, p in enumerate(pts)}
            pts = [by[n] for n in CORNERS]
        if any(not (0 <= x < w and 0 <= y < h) for x, y in pts):
            raise Rejected("꼭짓점이 사진 밖")
        q = np.asarray(pts, np.float32)
        area = cv2.contourArea(q, True)                              # 사진 좌표(y 가 아래)에서 시계 방향이면 양수
        if not cv2.isContourConvex(q) or abs(area) < 100:
            raise Rejected("사각형 아님 · 다시 선택 필요")
        if area < 0:
            raise Rejected("왼쪽 위부터 시계 방향으로 선택 필요")
        if geo.photo_to_plate(pts, self._size_mm()) is None:
            raise Rejected("변환 계산 실패 · 다시 선택 필요")
        dropped = self.calib.set_plate(pts, (w, h))
        self.clear_pos_check()
        self._refresh_samples()
        self.publish()
        note = "보정 점 %d개 · 마커 등록 삭제" % dropped if dropped else ""
        return {"dropped": dropped, "note": note}

    # ------------------------------------------------------------------
    # 3 · 초점 찾기: 판 영역 안의 선명도(원본 회색 사진의 라플라시안 분산)가 가장 큰 초점 값. 헤드 마커 둘레는 뺀다
    # (마커는 판보다 높아 마커에 맞으면 판이 흐리다). 결과는 [적용] 으로 저장한다.
    # ------------------------------------------------------------------
    def _focus_mask(self, shape, roi, marker):
        """선명도를 잴 자리(원본 픽셀 마스크): 판 꼭짓점 사각형을 focus_roi_inset 만큼 안쪽으로 줄인 영역(없으면 roi 상자) −
        헤드 마커 둘레(반경 focus_marker_pad_mm × 판 배율 · 판 배율이 없으면 사진 가로의 6 %)."""
        h, w = shape[:2]
        mask = np.zeros((h, w), np.uint8)
        p = self.calib.d.get("plate")
        if p and list(p["size"]) == [w, h]:
            q = np.asarray(p["corners"], np.float64)
            ctr = q.mean(axis=0)
            q = ctr + (q - ctr) * (1.0 - float(self.cfg["focus_roi_inset"]))
            cv2.fillConvexPoly(mask, np.rint(q).astype(np.int32), 255)
        elif roi is not None:
            x, y, bw, bh = (int(v) for v in roi)
            x, y = max(0, x), max(0, y)
            mask[y:min(h, y + bh), x:min(w, x + bw)] = 255
        if marker is not None:
            ppmm = self.plate_ppmm()
            r = float(self.cfg["focus_marker_pad_mm"]) * ppmm if ppmm else 0.06 * w
            cv2.circle(mask, (int(round(marker[0])), int(round(marker[1]))), int(round(r)), 0, -1)
        if not mask.any():
            raise Rejected("선명도 잴 영역 없음 · 범위 상자 다시 지정")
        return mask

    @staticmethod
    def sharpness(frame, mask):
        """선명도 = 마스크 안 회색 원본의 라플라시안 분산(초점이 맞을수록 크다)."""
        ys, xs = np.nonzero(mask)
        y0, y1, x0, x1 = ys.min(), ys.max() + 1, xs.min(), xs.max() + 1
        g = frame[y0:y1, x0:x1]
        g = cv2.cvtColor(g, cv2.COLOR_BGR2GRAY) if g.ndim == 3 else g
        lap = cv2.Laplacian(g, cv2.CV_64F)
        return float(lap[mask[y0:y1, x0:x1] > 0].var())

    def focus_find(self, roi=None, view=False):
        """[초점 찾기]: 핀 맨 위 → 촬영 위치(판 밖) → 자동 초점 끔 → focus_coarse_step 간격으로 넓게 → 가장 선명한 곳 둘레를 focus_fine_step 간격으로.
        값마다 focus_settle_s 기다린 뒤 새 프레임으로 잰다. 넣은 뒤 다시 읽어 안 바뀐 값(범위 밖)은 건너뛴다(다시 읽기 미지원이면 그대로 쓴다 · 화면 · 로그에 남김).
        roi = 판 영역이 없을 때 사진에서 그린 상자 [x, y, w, h](원본 픽셀). [정지] · 비상정지면 원래 초점 값으로 되돌린다. 저장은 [적용](focus_apply)."""
        c = self.cfg
        self._need_camera()
        self.c._no_run("초점 찾기")
        if self.focus.get("active"):
            raise Rejected("초점 찾기 중")
        if view and roi is not None and self.photo is not None:     # 화면(문 기준)에서 그린 상자 → 원본 픽셀 상자
            H0, W0 = self.photo["frame"].shape[:2]
            a = geo.from_view(roi[0], roi[1], W0, H0, *self._vr())
            b = geo.from_view(roi[0] + roi[2], roi[1] + roi[3], W0, H0, *self._vr())
            roi = [int(min(a[0], b[0])), int(min(a[1], b[1])), int(abs(b[0] - a[0])), int(abs(b[1] - a[1]))]
        if c["photo_head_xy"] is None:
            raise Rejected("촬영 위치 설정 필요 (초기 설정 3)")
        if not self.calib.d.get("plate") and roi is None:
            raise Rejected("판 영역 없음 · 사진에서 범위 상자 지정 필요")
        lo, hi = int(c["focus_scan_min"]), int(c["focus_scan_max"])
        big, small = int(c["focus_coarse_step"]), int(c["focus_fine_step"])
        if not (lo < hi and small < big):
            raise Rejected("초점 찾기 설정 확인 필요 · 시작 < 끝 · 작은 간격 < 큰 간격")
        cam = self.c.camera
        self.focus = {"active": True, "phase": "촬영 위치로 이동", "points": [], "best": None, "value": self.focus.get("value"),
                      "orig": None, "readback": None, "skipped": [], "note": "", "result": "", "nudge": None, "roi": roi}
        self.publish()
        g = self.c.safety.generation
        orig = None
        changed = False
        try:
            head = c["photo_head_xy"]
            self.c.goto(head[0], head[1])                            # 핀 맨 위 → 촬영 위치(판 밖 - 헤드가 판을 가리지 않게)
            if self.c.safety.generation != g:
                raise _FocusStop()
            read0 = cam.get_focus()
            readback = read0 is not None
            orig = read0 if readback else (int(c["camera_focus_value"]) if int(c["camera_focus_value"]) >= 0 else None)
            self.focus.update(orig=None if orig is None else int(round(orig)), readback=readback)
            frame0, _t = self.settled("이동", g)
            mask = self._focus_mask(frame0.shape, roi, geo.find_marker(frame0))
            settle = float(c["focus_settle_s"])

            def measure(v, phase):
                nonlocal changed
                if self.c.safety.generation != g:
                    raise _FocusStop()
                changed = True
                rb = cam.set_focus(v)
                if readback and (rb is None or abs(rb - v) > 0.5):   # 카메라가 받지 않은 값(범위 밖): 렌즈가 그대로라 잴 필요 없다
                    self.focus["skipped"].append(v)
                    return None
                frame, _t2 = self.settled("초점", g, wait_s=settle)      # 렌즈가 멈춘 뒤(focus_settle_s 기다림 + 화면 멈춤)
                s = self.sharpness(frame, mask)
                self.focus["points"].append([int(v), round(s, 3), phase])
                self.focus["value"] = int(v)
                self.publish()
                return s

            self.focus["phase"] = "넓게 훑기"
            coarse = list(range(lo, hi + 1, big))
            if coarse[-1] != hi:
                coarse.append(hi)
            for v in coarse:
                measure(v, "coarse")
            pts = [p for p in self.focus["points"] if p[2] == "coarse"]
            if not pts:
                raise Rejected("초점 값이 바뀌지 않음 · 카메라 초점 조정 미지원 또는 범위 확인")
            b0 = max(pts, key=lambda p: p[1])[0]
            self.focus["phase"] = "촘촘히 훑기"
            done = {p[0] for p in self.focus["points"]}
            for v in range(max(lo, b0 - big + small), min(hi, b0 + big - small) + 1, small):
                if v not in done:
                    measure(v, "fine")
            best = max(self.focus["points"], key=lambda p: p[1])
            rb = cam.set_focus(best[0])                              # 렌즈를 고른 값에(저장은 [적용])
            self.settled("초점", g, wait_s=settle)
            note = ""
            top, ends = max(p[1] for p in pts), max(pts[0][1], pts[-1][1])
            if top <= 0 or (top - ends) / top < float(c["focus_peak_min"]):
                note = "봉우리 불분명 · 샘플 · 조명 확인"
            if not readback:
                note = (note + " · " if note else "") + "초점 다시 읽기 미지원"
            self.focus.update(best=int(best[0]), value=int(best[0]), note=note, phase="",
                              result="최적 %d · 선명도 %.1f" % (best[0], best[1]))
            self.c.logs.app("초점 찾기 · 최적 %d · 선명도 %.1f · 넓게 %d점 · 촘촘히 %d점 · 범위 밖 %d개 건너뜀 · 다시 읽기 %s%s · 원래 %s" % (
                best[0], best[1], len(pts), len(self.focus["points"]) - len(pts), len(self.focus["skipped"]),
                "지원" if readback else "미지원", " · " + note if note else "", "—" if orig is None else "%d" % round(orig)))
            return {"best": int(best[0]), "points": len(self.focus["points"]), "skipped": len(self.focus["skipped"]), "note": note,
                    "readback": None if rb is None else rb}
        except (_FocusStop, Rejected, CameraError) as e:
            stopped = isinstance(e, _FocusStop) or self.c.safety.generation != g
            back = ""
            if changed and orig is not None and self.c.camera.connected:
                try:
                    cam.set_focus(int(round(orig)))                  # 원래 초점 값으로 되돌린다(이동이 아니라 비상정지 중에도 한다)
                    back = " · 원래 값 %d 으로 복귀" % round(orig)
                    self.focus["value"] = int(round(orig))
                except CameraError as e2:
                    back = " · 원래 값 복귀 실패 · %s" % e2
            why = "정지" if stopped else str(e)
            self.focus.update(phase="", result="중단 · %s%s" % (why, back))
            self.c.logs.app("초점 찾기 중단 · %s%s" % (why, back), "warn")
            raise Rejected("초점 찾기 %s%s" % ("정지" if stopped else "실패 · " + str(e), back))
        finally:
            self.focus["active"] = False
            self.publish()

    def focus_nudge(self, d):
        """초점 미세 조정 [−] [+]: 지금 값 ± focus_nudge → 렌즈 대기 → 그 자리 선명도. 저장은 [적용]."""
        self._need_camera()
        self.c._no_run("초점 조정")
        if self.focus.get("active"):
            raise Rejected("초점 찾기 중")
        cam = self.c.camera
        cur = self.focus.get("value")
        if cur is None:
            rb = cam.get_focus()
            cur = int(round(rb)) if rb is not None else int(self.cfg["camera_focus_value"])
        if cur < 0:
            raise Rejected("초점 값 없음 · [초점 찾기] 필요")
        v = int(cur) + (1 if float(d) > 0 else -1) * int(self.cfg["focus_nudge"])
        v = max(0, min(int(self.cfg["focus_scan_max"]), v))
        rb = cam.set_focus(v)
        if rb is not None and abs(rb - v) > 0.5:
            raise Rejected("초점 %d 적용 안 됨 · 카메라 범위 밖" % v)
        g = self.c.safety.generation
        try:
            frame, _t = self.settled("초점", g, wait_s=float(self.cfg["focus_settle_s"]))
        except _FocusStop:
            raise Rejected("정지로 취소됨")
        if self.calib.d.get("plate") or self.focus.get("roi"):       # 초점 찾기와 같은 자리(판 영역 − 마커 둘레 · 또는 그린 상자)
            mask = self._focus_mask(frame.shape, self.focus.get("roi"), geo.find_marker(frame))
        else:
            mask = np.full(frame.shape[:2], 255, np.uint8)
        s = self.sharpness(frame, mask)
        self.focus.update(value=v, nudge={"value": v, "sharp": round(s, 3)})
        self.publish()
        return {"value": v, "sharp": round(s, 3)}

    def focus_apply(self):
        """[적용]: 지금 초점 값을 camera_focus_value 로 저장 · 초점 고정 켬. 값이 바뀌면 사진 기준 값은 맞지 않으므로(배율 · 위치가 달라진다)
        빈 판 사진 · 헤드 마커 · 보정 점은 버리고 판 영역은 "초점 바뀜 · 다시 확정 필요"(calib.focus_changed)."""
        self._need_camera()
        self.c._no_run("초점 적용")
        if self.focus.get("active"):
            raise Rejected("초점 찾기 중")
        v = self.focus.get("value")
        if v is None:
            raise Rejected("[초점 찾기] 필요")
        old = int(self.cfg["camera_focus_value"])
        rb = self.c.camera.set_focus(int(v))
        self.c._remember(camera_focus_value=int(v), camera_focus_fixed=True)
        self.c.state.clear_alarm("focus")
        note = ""
        if int(v) != old:
            dropped = self.calib.focus_changed()
            self._empty = None
            self.clear_pos_check()
            self._refresh_samples()
            note = "3 · 4단계와 마커 재등록 필요"
            self.c.logs.app("초점 적용 · %d (이전 %s · 읽음 %s) · 빈 판 사진 · 헤드 마커 · 보정 점 %d개 버림 · 판 영역 다시 확정 필요" % (
                int(v), old if old >= 0 else "미설정", "미지원" if rb is None else "%g" % rb, dropped), "warn")
        else:
            self.c.logs.app("초점 적용 · %d (같은 값)" % int(v))
        self.focus["applied"] = int(v)
        self.publish()
        return {"value": int(v), "note": note}

    # ------------------------------------------------------------------
    # 4 · 빈 판 사진
    # ------------------------------------------------------------------
    def empty_take(self):
        """핀 맨 위 → 촬영 위치 → 원본 한 장을 calib/empty_plate.png 로 저장. 언제든 다시 찍어 바꿀 수 있다."""
        frame = self.capture(move=True, need_head=True)
        try:
            imgio.write(self.calib.empty_path, frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        except imgio.ImageIOError as e:
            self.c.logs.app("빈 판 사진 저장 실패 · %s · %s" % (self.calib.empty_path, e), "error")
            raise Rejected("빈 판 사진 저장 실패 · %s" % str(e)[:40])
        self.calib.set_empty((frame.shape[1], frame.shape[0]))
        self._empty = (self.calib.d["empty"]["t"], frame)
        self.c.state.clear_alarm("empty_read")
        self.publish()
        return {"t": self.calib.d["empty"]["t"]}

    # ------------------------------------------------------------------
    # 5 · 좌표 맞추기
    # ------------------------------------------------------------------
    def coord_set(self, name, view=False):
        """[A 등록] … [D 등록]: 지금 스테이지 X · Y 를 그 꼭짓점 자리로 기억한다. 4점이 모이면 헤드 마커도 이어서 기억한다.
        view = 화면 이름(문 기준 - 화면 왼쪽 위부터 시계 방향) → 저장 이름(사진 기준)으로 바꿔 저장한다."""
        name = str(name).upper()
        if name not in CORNERS:
            raise Rejected("꼭짓점은 A · B · C · D 중 하나")
        shown = name
        if view:
            name = self.sname(name)
        else:
            shown = self.vname(name)
        self._need_xy()
        self.c._no_run("좌표 보정")
        self.calib.set_corner_stage(name, self._xy())
        self.clear_pos_check()
        self._refresh_samples()
        self.publish()
        self.c.logs.app("꼭짓점 %s 등록 · X %.2f · Y %.2f mm" % (shown, *self.calib.d["coords"]["stage"][name]))
        out = {"name": shown, "xy": self.calib.d["coords"]["stage"][name]}
        if all(n in self.calib.d["coords"]["stage"] for n in CORNERS):
            try:
                out["marker"] = self.marker_learn()
            except Rejected as e:
                out["marker_note"] = str(e)
                self.c.state.alarm("marker", "마커 등록 실패 · %s" % e, NOTICE)
        return out

    def coord_goto_d(self):
        """남은 꼭짓점 하나의 예상 위치로(세 꼭짓점이 있을 때 - 평행사변형)."""
        g = self.calib.d_guess()
        if g is None:
            raise Rejected("꼭짓점 3개 등록 필요")
        return self.c.goto(g[0], g[1])

    def coord_clear(self):
        self.c._no_run("좌표 보정")
        self.calib.clear_coords()
        self.clear_pos_check()
        self._refresh_samples()
        self.publish()
        return {}

    def _marker_pair(self):
        """지금 자리에서 새 프레임 두 장의 마커 위치. 두 장이 marker_pair_mm(판 배율로 px) 안에서 맞으면 평균. 안 맞으면 한 번 더 찍는다.
        두 번 다 마커가 안 보이면 None(그 자리에서 안 보임). 보이는데 두 번 다 안 맞으면 Rejected(카메라 · 헤드가 흔들린다)."""
        tol = self._mm_px(self.cfg["marker_pair_mm"])
        diff = None
        for _try in range(2):
            m1 = geo.find_marker(self._fresh()[0])
            m2 = geo.find_marker(self._fresh()[0])
            if m1 is None or m2 is None:
                continue
            d = float(np.hypot(m1[0] - m2[0], m1[1] - m2[1]))
            if d <= tol:
                return ((m1[0] + m2[0]) / 2.0, (m1[1] + m2[1]) / 2.0)
            diff = d
        if diff is None:
            return None
        raise Rejected("마커 위치 흔들림 · 두 장 %.2f mm 차이" % (diff / (self.plate_ppmm() or geo.RECT_PPMM)))

    def _mm_px(self, mm):
        """마커 허용(mm) → 사진 px(판 배율 = 판 꼭짓점 픽셀 ÷ 판 한 변). 판 영역이 없으면 보정 사진 배율(RECT_PPMM)."""
        k = self.plate_ppmm()
        return float(mm) * (k if k else geo.RECT_PPMM)

    def plate_ppmm(self):
        """판 배율(사진 px / mm) = 판 꼭짓점 네 변의 평균 픽셀 길이 ÷ 판 한 변. 판 영역이 없으면 None."""
        p = self.calib.d.get("plate")
        if not p:
            return None
        q = np.asarray(p["corners"], float)
        sides = [float(np.hypot(*(q[(i + 1) % 4] - q[i]))) for i in range(4)]
        return sum(sides) / 4.0 / self._size_mm()

    def _plate_dirs(self):
        """판 기준 방향: 스테이지 X+ · Y+ 로 1 mm 갈 때 사진에서 움직이는 방향(판 꼭짓점 사진 픽셀 + 꼭짓점 스테이지 좌표). 좌표 보정이 없으면 None."""
        H = self.calib.stage_h()
        if H is None:
            return None
        st = self.calib.d["coords"]["stage"]
        cx = sum(st[n][0] for n in CORNERS) / 4.0
        cy = sum(st[n][1] for n in CORNERS) / 4.0
        try:
            Hi = np.linalg.inv(np.asarray(H, float))
        except np.linalg.LinAlgError:
            return None
        p0 = np.asarray(geo.apply1(Hi, cx, cy))
        return np.asarray(geo.apply1(Hi, cx + 1.0, cy)) - p0, np.asarray(geo.apply1(Hi, cx, cy + 1.0)) - p0

    def marker_problems(self, J):
        """헤드 마커 "mm 당 픽셀" 행렬 검사. 반환 (문제 목록, 값 {"angle", "ratio", "scale"}). 기준은 모두 설정.
        두 방향 사이 각 90° ± 허용 · 크기 비 · 판 배율 대비 크기 · 판 기준과 같은 방향(좌표 보정이 없으면 방향 검사는 건너뜀)."""
        c = self.cfg
        J = np.asarray(J, float)
        vx, vy = J[:, 0], J[:, 1]
        nx, ny = float(np.hypot(*vx)), float(np.hypot(*vy))
        out, val = [], {"angle": None, "ratio": None, "scale": None}
        if nx < 1e-9 or ny < 1e-9:
            return ["마커 이동량 측정 실패"], val
        ang = float(np.degrees(np.arccos(np.clip(np.dot(vx, vy) / (nx * ny), -1.0, 1.0))))
        ratio = nx / ny
        val.update(angle=ang, ratio=ratio)
        if abs(ang - 90.0) > float(c["marker_angle_tol_deg"]):
            out.append("마커 두 방향 %.0f° · 직각 아님" % ang)
        if not float(c["marker_ratio_min"]) <= ratio <= float(c["marker_ratio_max"]):
            out.append("마커 X · Y 크기 비 %.2f" % ratio)
        ppmm = self.plate_ppmm()
        if ppmm:
            k = (nx + ny) / 2.0 / ppmm
            val["scale"] = k
            if not float(c["marker_scale_min"]) <= k <= float(c["marker_scale_max"]):
                out.append("마커 크기 판 배율의 %.1f배" % k)
        dirs = self._plate_dirs()
        if dirs is not None:
            for name, v, d in (("X", vx, dirs[0]), ("Y", vy, dirs[1])):
                if float(np.dot(v, d)) <= 0:
                    out.append("마커 %s 방향 반대 · 판 기준과 다름" % name)
        return out, val

    def marker_learn(self):
        """촬영 위치에서 헤드 마커 가운데 픽셀을 기억하고, X · Y 로 조금 옮겨 보며 "mm 당 픽셀" 2×2 행렬을 잰다.
        자리마다 새 프레임 두 장(marker_pair_mm 안 · 아니면 한 번 더) · 끝에 촬영 위치로 돌아와 처음 자리와 marker_return_mm 안(mm 는 판 배율로 px) ·
        행렬 검사(marker_problems). 하나라도 어긋나면 저장하지 않는다(기억한 마커 없음 → 위치 확인 안 함) - 이유 한 줄 + 로그."""
        head = self.cfg["photo_head_xy"]
        if head is None:
            raise Rejected("촬영 위치 설정 필요 (초기 설정 3)")
        self._need_camera()
        self.c._no_run("헤드 마커 기억")
        if not all(self.c.stage.homed.get(a) for a in "xyz"):
            raise Rejected("세 축 원점 필요 (초기 설정 2)")
        step = float(self.cfg["marker_probe_mm"])
        seen = {}

        def fail(why):
            raise Rejected(why)

        def see(x, y):
            self.c.goto(x, y)
            self.settled("이동")                                     # 화면이 멈춘 뒤 두 장(프레임이 늦게 와 이동 중 장면이 섞이지 않게)
            return self._marker_pair()

        try:
            m0 = see(head[0], head[1])
            self.marker_seen = m0 is not None
            if m0 is None:
                fail("마커 미검출")
            seen["촬영 위치"] = m0
            cols = []
            for axis in (0, 1):
                got = None
                for sign in (1.0, -1.0):                             # +10 mm 에서 안 보이거나 범위 밖이면 −10 mm
                    p = [head[0], head[1]]
                    p[axis] += sign * step
                    if p[axis] < 0 or p[axis] > float(self.cfg["%s_max_mm" % "xy"[axis]]):
                        continue
                    m = see(p[0], p[1])
                    if m is not None:
                        seen["%s 쪽" % "XY"[axis]] = m
                        got = [(m[0] - m0[0]) / (sign * step), (m[1] - m0[1]) / (sign * step)]
                        break
                if got is None:
                    self.c.goto(head[0], head[1])
                    fail("이동 후 마커 미검출")
                cols.append(got)
            mr = see(head[0], head[1])                               # 촬영 위치로 돌아와 다시 본다(스테이지 · 카메라가 그대로인가)
            if mr is None:
                fail("복귀 후 마커 미검출")
            seen["복귀"] = mr
            back = float(np.hypot(mr[0] - m0[0], mr[1] - m0[1]))
            if back > self._mm_px(self.cfg["marker_return_mm"]):
                fail("복귀 후 마커 %.2f mm 차이 · 스테이지 · 카메라 고정 확인" % (back / (self.plate_ppmm() or geo.RECT_PPMM)))
        except Rejected as e:                                        # 미검출 · 흔들림 · 복귀 차이 · 이동 거절 · 정지 · 카메라 오류: 반쯤 잰 값을 남기지 않는다
            self.calib.set_marker(None, None)
            self.clear_pos_check()
            self.publish()
            self.c.logs.app("헤드 마커 등록 실패 · %s · %s" % (e, " · ".join("%s (%.1f, %.1f)" % (k, v[0], v[1]) for k, v in seen.items()) or "마커 없음"), "warn")
            raise
        J = [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]      # 열 = X · Y 로 1 mm 움직일 때 마커가 움직이는 픽셀
        bad, val = self.marker_problems(J)
        self.c.logs.app("헤드 마커 · %s · X 1 mm (%.2f, %.2f) px · Y 1 mm (%.2f, %.2f) px · 각 %s · 크기 비 %s · 판 배율의 %s · %s" % (
            " · ".join("%s (%.1f, %.1f)" % (k, v[0], v[1]) for k, v in seen.items()), cols[0][0], cols[0][1], cols[1][0], cols[1][1],
            "—" if val["angle"] is None else "%.1f°" % val["angle"], "—" if val["ratio"] is None else "%.2f" % val["ratio"],
            "—" if val["scale"] is None else "%.2f배" % val["scale"], "거절 · " + " · ".join(bad) if bad else "통과"), "warn" if bad else "info")
        if bad:
            self.calib.set_marker(None, None)
            self.clear_pos_check()
            self.publish()
            raise Rejected(bad[0])
        self.calib.set_marker(m0, J)
        self.clear_pos_check()
        self.c.state.clear_alarm("marker")
        self.publish()
        return {"px": [round(m0[0], 1), round(m0[1], 1)]}

    def plate_goto(self, x, y, view=False):
        """판 사진을 누른 자리(판 mm - view 면 화면 판 mm)로 간다(핀 맨 위로 올린 뒤). 샘플 근처를 누르면 샘플 가운데로 붙는다."""
        Hp, Hs = self.calib.plate_h(self._size_mm()), self.calib.stage_h()
        if Hp is None or Hs is None:
            raise Rejected("초기 설정 3 · 5 필요")
        px, py = self._from_vplate(x, y) if view else (float(x), float(y))
        snapped = None
        best = None
        for s in self.samples:
            d = float(np.hypot(s["plate"][0] - px, s["plate"][1] - py))
            if d <= SNAP_MM and (best is None or d < best[0]):
                best = (d, s)
        if best is not None:
            px, py = best[1]["plate"]
            snapped = best[1]["name"]
        photo_px = geo.apply1(np.linalg.inv(Hp), px, py)
        sx, sy = geo.apply1(Hs, *photo_px)
        self.last_click = {"px": [round(photo_px[0], 1), round(photo_px[1], 1)], "plate": [round(px, 2), round(py, 2)], "sample": snapped}
        self.publish()
        self.c.goto(sx, sy)
        return {"xy": [round(sx, 2), round(sy, 2)], "sample": snapped, "pos": dict(self.c.state.get("pos"))}     # 화면이 좌표 이동 칸을 채운다

    def coord_add_here(self):
        """[보정 점 추가]: (마지막으로 누른 사진 좌표, 지금 스테이지 X · Y)를 보정 점으로 더하고 변환을 전체 점으로 다시 맞춘다."""
        if self.last_click is None:
            raise Rejected("판 사진에서 위치 선택 필요")
        if not self.calib.coords_ready():
            raise Rejected("좌표 보정 필요 (초기 설정 5)")
        self._need_xy()
        self.c._no_run("보정 점 더하기")
        self.calib.add_extra(self.last_click["px"], self._xy())
        self.last_click = None
        self._refresh_samples()
        self.publish()
        return {"n": len(self.calib.d["coords"]["extra"])}

    def coord_remove(self, index):
        self.c._no_run("보정 점 지우기")
        self.calib.remove_extra(int(index))
        self._refresh_samples()
        self.publish()
        return {"n": len(self.calib.d["coords"]["extra"])}

    # ------------------------------------------------------------------
    # 샘플 찾기
    # ------------------------------------------------------------------
    def samples_find(self):
        """[사진 찍고 샘플 찾기]: 핀 맨 위 → 촬영 위치 → 촬영 → 위치 확인 → 샘플 찾기."""
        if not self.calib.d.get("plate"):
            raise Rejected("판 영역 설정 필요 (초기 설정 3)")
        if not self.calib.plate_ok():
            raise Rejected("초점 바뀜 · 판 영역 다시 확정 필요 (초기 설정 3)")
        t0 = time.monotonic()
        self.capture(move=True, need_head=True)
        cur = self.photo_rect()
        if cur is None:
            raise Rejected("사진 크기 변경됨 · 초기 설정 3 재설정 필요")
        emp = self.empty_rect()
        c = self.cfg
        res = find_mod.find_samples(cur, emp, geo.RECT_PPMM, geo.RECT_MARGIN_MM, log=lambda t: self.c.logs.app("샘플 찾기 · " + t, "detail"),
                                    layout=c["sample_layout"], rows=c["sample_grid_rows"], cols=c["sample_grid_cols"], faint=c["detect_faint"])
        self._note_shift(res.get("shift"))
        if emp is None and not self.calib.d.get("empty"):
            res["notes"].append("빈 판 사진 없음 · 사진 한 장으로 검출")
        # 빈 판 사진이 있다고 적혀 있는데 못 읽었으면 empty_frame 이 주의 "빈 판 사진 읽기 실패 · 초기 설정 4 재촬영 필요" 를 이미 띄웠다(같은 일을 두 번 알리지 않는다)
        batch = self.cfg["batch_name"]
        self.samples = []
        for i, s in enumerate(self._view_order(res["samples"]), 1):      # 번호: 문 기준 화면에서 줄마다 왼쪽 → 오른쪽(시작 구석 sample_number_start)
            self.samples.append({"id": i, "name": "%s-%d" % (batch, i), "plate": s["plate"], "outline": s["outline"], "w": s["w"], "h": s["h"], "area": s["area"],
                                 "source": s["source"], "weak": s["weak"], "small": False, "xy": None, "use": True})
        self._refresh_samples(reset_use=True)
        self.c.runner.reset()                                        # 새 사진 · 새 샘플: 지난 측정 진행 표시는 비운다(파일은 그대로)
        self.find_info = {"t": self.photo["t"], "n": len(self.samples), "notes": res["notes"], "path": res["path"], "ms": int((time.monotonic() - t0) * 1000),
                          "shift": res.get("shift")}
        if res["notes"]:
            self.c.state.alarm("find", " · ".join(res["notes"]), NOTICE, dismiss=True)
        else:
            self.c.state.clear_alarm("find")
        self.c.logs.app("샘플 찾기 · %d개 · %s · %d ms" % (len(self.samples), res["path"], self.find_info["ms"]))
        self.publish()
        self.c.gate_inputs_changed()
        return {"n": len(self.samples), "used": sum(1 for s in self.samples if s["use"]), "notes": res["notes"]}

    def _note_shift(self, sh):
        """빈 판 사진 대비 사진 밀림: 찾을 때마다 로그 한 줄 · 기준(view_shift_warn_mm)을 넘으면 알림(아니면 지움). 좌표는 고치지 않는다."""
        lim = float(self.cfg["view_shift_warn_mm"])
        if sh is not None:
            self.c.logs.app("사진 밀림 · 빈 판 사진 대비 X %+.2f · Y %+.2f mm · %.2f mm · 회전 %+.2f°" % (sh["dx"], sh["dy"], sh["mm"], sh["rot"]))
        if sh is not None and lim > 0 and sh["mm"] > lim:
            self.c.state.alarm("view_shift", "사진 %.1f mm 밀림 · 샘플 좌표 어긋날 수 있음 · 카메라 · 판 확인" % sh["mm"], NOTICE, dismiss=True)
            self.c.logs.app("사진 %.2f mm 밀림 · 기준 %g mm 초과 · 카메라 · 판 확인" % (sh["mm"], lim), "warn")
        else:
            self.c.state.clear_alarm("view_shift")

    def _view_order(self, samples):
        """샘플 순서: 문 기준 화면에서 왼쪽 위(또는 왼쪽 아래 - 문 쪽)부터 줄마다 왼쪽 → 오른쪽."""
        if not samples:
            return []
        S = self._size_mm()
        bottom = self.cfg["sample_number_start"] == "bottom_left"
        proxy = []
        for s in samples:
            u, v = self._vplate(s["plate"])
            proxy.append({"plate": [u, S - v if bottom else v], "_s": s})
        long_mm = max(max(float(s["w"]), float(s["h"])) for s in samples)
        return [p["_s"] for p in find_mod.order_rows(proxy, long_mm)]

    def _refresh_samples(self, reset_use=False):
        """샘플의 스테이지 X · Y 와 "핀보다 작음"을 지금 보정 값으로 다시 계산한다."""
        T = self.calib.plate_to_stage(self._size_mm())
        cfg = self.cfg
        long_axis = 1 if cfg["pin_long_axis"] == "y" else 0
        for s in self.samples:
            cx, cy = s["plate"]
            long_dir = (0.0, 1.0) if long_axis == 1 else (1.0, 0.0)        # 좌표 맞추기 전에는 판 축 = 스테이지 축으로 본다
            if T is not None:
                sx, sy = geo.apply1(T, cx, cy)
                s["xy"] = [round(sx, 2), round(sy, 2)]
                try:
                    Ti = np.linalg.inv(T)
                    d = [0.0, 0.0]
                    d[long_axis] = 5.0
                    a = geo.apply(Ti, [(sx - d[0], sy - d[1]), (sx + d[0], sy + d[1])])   # 스테이지에서 핀 긴 쪽 방향이 판 좌표에서 어느 쪽인가
                    long_dir = (float(a[1][0] - a[0][0]), float(a[1][1] - a[0][1]))
                except np.linalg.LinAlgError:
                    pass
            else:
                s["xy"] = None
            fits = find_mod.pin_fits(s["outline"], (cx, cy), long_dir, float(cfg["pin_long_mm"]), float(cfg["pin_short_mm"]), float(cfg["pin_margin_mm"]))
            s["small"] = not fits
            if reset_use:
                s["use"] = not s["weak"] and not s["small"]                # 확인 필요 · 핀보다 작음은 기본으로 빼 둔다(사람이 다시 넣을 수 있다)

    def on_settings(self):
        """설정이 바뀌었다(핀 크기 · 판 한 변 등): 샘플의 자리 · "핀보다 작음"을 다시 계산한다(사람이 고른 포함/제외는 그대로)."""
        self._refresh_samples()
        self.publish()

    def sample_rename(self, id, name):
        """샘플 이름 고치기. 파일 이름에 못 쓰는 글자는 거절한다. 이미 잰(또는 재는 중인) 샘플은 못 고친다."""
        from app.jobs import files
        name = str(name)
        why = files.name_problem(name)
        if why:
            raise Rejected(why)
        if any(s["name"] == name and s["id"] != int(id) for s in self.samples):
            raise Rejected("같은 이름의 샘플 있음")
        from app.jobs.run import WAIT
        it = self.c.runner.items.get(int(id))
        if it is not None and it["status"] != WAIT:
            raise Rejected("측정한 샘플은 이름 수정 불가")
        for s in self.samples:
            if s["id"] == int(id):
                s["name"] = name
                if it is not None:
                    it["name"] = name
                self.publish()
                self.c.runner.publish()
                return {"id": s["id"], "name": name}
        raise Rejected("해당 샘플 없음")

    def batch_set(self, name):
        """묶음. 샘플 이름 "<묶음>-<번호>" 도 같이 바뀐다(사람이 고친 이름은 그대로)."""
        from app import config as config_mod
        from app.jobs import files
        name = str(name)
        why = files.name_problem(name)
        if why:
            raise Rejected(why)
        if self.c.run_active():
            raise Rejected("측정 중 실행 불가")
        old = str(self.cfg["batch_name"])
        try:
            self.cfg.update({"batch_name": name})
            self.cfg.save()
        except (config_mod.ConfigError, OSError) as e:
            raise Rejected(str(e))
        for s in self.samples:
            if s["name"] == "%s-%d" % (old, s["id"]) and s["id"] not in self.c.runner.items:
                s["name"] = "%s-%d" % (name, s["id"])
        self.c.publish_cfg()
        self.publish()
        self.c.runner.publish()
        return {"batch_name": name}

    def sample_use(self, id, use):
        self.c._no_run("샘플 고르기")
        for s in self.samples:
            if s["id"] == int(id):
                s["use"] = bool(use)
                self.publish()
                self.c.gate_inputs_changed()                             # 측정 시작 조건이 고른 샘플을 본다(쿨러 배치 빈 칸)
                return {"id": s["id"], "use": s["use"]}
        raise Rejected("해당 샘플 없음")

    # ------------------------------------------------------------------
    # 7 · 쿨러 구역
    # ------------------------------------------------------------------
    def cooler_only(self, n):
        """[켜 보기]: 쿨러 릴레이(cooler_relays) 하나만 켠다(다른 쿨러는 끈다). n 이 0 이면 쿨러를 모두 끈다.
        송풍 · 열선 · 미배정 릴레이는 여기서 켜지도 끄지도 않는다."""
        n = int(n)
        self.c._need_stage()
        self.c._no_run("쿨러 시험")
        cool = [int(x) for x in self.cfg["cooler_relays"]]
        if n != 0 and n not in cool:
            raise Rejected("R%d · 쿨러 릴레이 아님" % n)
        for i in cool:
            if i != n and self.c.stage.relays[i - 1] is not False:
                self.c.relay_off(i)
        if n:
            self.c.relay_on(n)
        return {"relays": list(self.c.state.get("relays"))}

    def cooler_save(self, grid):
        """쿨러 배치 저장(초기 설정 7): 칸마다 쿨러 릴레이 하나 · 같은 릴레이를 여러 칸에 써도 된다. 쿨러 릴레이 밖의 번호는 거절한다
        (설정을 바꾸는 길 하나 - Controller.update_settings 의 값 검증 + 조합 검사)."""
        from app import config as config_mod
        try:
            self.c.update_settings({"cooler_grid": grid})
        except config_mod.ConfigError as e:
            raise Rejected(str(e))
        self.calib.set_cooler()
        self.c.publish_cfg()
        self.publish()
        return {"cooler_grid": self.cfg["cooler_grid"]}

    def cooler_cell(self, row, col, n=None):
        """칸 하나 정하기(초기 설정 7 · 누를 때마다 저장): 고른 쿨러 릴레이 n 을 그 칸에(쿨러를 켜지 않아도 된다).
        이미 n 이 든 칸을 다시 누르거나 n 이 없으면(None · [비우기]) 그 칸을 비운다. row · col 은 cooler_grid 기준(사진 기준 · 0 행 = A–B 변 쪽)."""
        row, col = int(row), int(col)
        if not (0 <= row < 3 and 0 <= col < 3):
            raise Rejected("칸 범위 밖")
        n = None if n is None else int(n)
        if n is not None and n not in [int(x) for x in self.cfg["cooler_relays"]]:
            raise Rejected("R%d · 쿨러 릴레이 아님" % n)
        grid = [list(r) for r in self.cfg["cooler_grid"]]
        grid[row][col] = None if grid[row][col] == n else n
        return self.cooler_save(grid)

    # ------------------------------------------------------------------
    # 화면에 보낼 상태
    # ------------------------------------------------------------------
    def _photo_view(self):
        ph = self.photo
        if ph is None:
            return None
        vw, vh = geo.view_size(int(ph["frame"].shape[1]), int(ph["frame"].shape[0]), self._vr()[0])
        return {"id": ph["id"], "t": ph["t"], "w": int(ph["frame"].shape[1]), "h": int(ph["frame"].shape[0]), "vw": vw, "vh": vh, "at_head": ph["at_head"], "pos_off_mm": self.pos_off_mm,
                "rect": self.photo_rect() is not None}

    def steps(self):
        """초기 설정 7단계의 끝났는지 + 저장된 값 한 줄."""
        c, cal, st = self.cfg, self.calib.d, self.c.state

        def when(t):
            return time.strftime("%m-%d %H:%M", time.localtime(t)) if t else ""

        dev_ok = all(st.get("devices", k, "state") == "연결" for k in ("stage", "k2110", "k2450", "camera"))
        n_dev = sum(1 for k in ("stage", "k2110", "k2450", "camera") if st.get("devices", k, "state") == "연결")
        homed = st.get("homed")
        how = {"here": "현재 위치", "end": "끝단 접촉"}
        org = cal.get("origin") or {}
        last = max((org[a] for a in org), key=lambda o: o["t"], default=None)
        o_done = all(homed.get(a) for a in "xyz")
        o_text = ("%s · %s" % (how.get(last["how"], last["how"]), when(last["t"])) if (o_done and last) else
                  "원점 있음" if o_done else "원점 없음: " + " · ".join(a.upper() for a in "xyz" if not homed.get(a)))
        plate, head = cal.get("plate"), c["photo_head_xy"]
        p_text = (("초점 바뀜 · 다시 확정 필요" if plate.get("stale") else "판 영역 · %s" % when(plate["t"])) if plate else "판 영역 없음") \
            if head is not None else "촬영 위치 없음"
        emp = cal.get("empty")
        ready = self.calib.coords_ready()
        n_ex = len(cal["coords"]["extra"])
        n_c = sum(1 for n in CORNERS if n in cal["coords"]["stage"])
        co_text = ("4점%s · %s" % (" + 보정 %d점" % n_ex if n_ex else "", when(cal["coords"].get("t")))) if ready else ("꼭짓점 %d / 4" % n_c if n_c else "보정 없음")
        cz = c["contact_z_mm"]
        ct = cal.get("contact")
        cool = cal.get("cooler")
        return [
            {"done": dev_ok, "text": "전체 연결됨" if dev_ok else "%d / 4 연결" % n_dev},
            {"done": o_done, "text": o_text},
            {"done": self.calib.plate_ok() and head is not None, "text": p_text, "warn": bool(plate and plate.get("stale"))},
            {"done": bool(emp) and not self._empty_unreadable(),
             "text": ("읽기 실패 · 재촬영 필요" if self._empty_unreadable() else "촬영 · %s" % when(emp["t"])) if emp else "없어도 측정 가능"},
            {"done": ready, "text": co_text},
            {"done": cz is not None, "text": ("Z %.2f mm · %s" % (cz, when(ct["t"] if ct else None))) if cz is not None else "높이 없음"},
            dict(self._cooler_step(cool, when)),
        ]

    def _cooler_step(self, cool, when):
        """초기 설정 7 의 단계 글: 빈 칸이 있으면 미완료 · 주의 "빈 칸 n개"."""
        empty = config_mod.grid_empty(self.cfg["cooler_grid"])
        if empty:
            return {"done": False, "warn": True, "text": "빈 칸 %d개" % empty}
        if cool:
            return {"done": True, "text": "설정 완료 · %s" % when(cool["t"])}
        if self.cfg["cooler_grid"] == config_mod.BY_KEY["cooler_grid"].default:      # 장비 값(코드 기본값 - 측정 PC 에서 정한 배치)
            return {"done": True, "text": "장비 기본 배치"}
        return {"done": False, "text": "기본 배치 · 확인 필요"}

    def _cooler_view(self):
        """초기 설정 7 의 칸(문 기준 화면 - 공용 변환 to_view): 화면 3×3 칸마다 cooler_grid [행, 열]. 네 구석 = 화면 꼭짓점 이름(A 왼쪽 위부터 시계 방향)."""
        c = self.cfg
        S = self._size_mm()
        cells = [[None] * 3 for _ in range(3)]
        for r in range(3):
            for col in range(3):
                x, y = self._from_vplate(S * (col + 0.5) / 3.0, S * (r + 0.5) / 3.0)
                cells[r][col] = list(geo.cooler_cell((x, y), S))
        return {"layout": {"cells": cells, "corners": {"A": [0, 0], "B": [0, 2], "C": [2, 2], "D": [2, 0]}}, "why": "",
                "front": config_mod.SIDE_NAME[c["machine_front_side"]], "right": config_mod.SIDE_NAME[c["machine_right_side"]],
                "back": config_mod.SIDE_NAME[_opposite(c["machine_front_side"])], "left": config_mod.SIDE_NAME[_opposite(c["machine_right_side"])]}

    def _view_block(self):
        """화면 방향: 회전 · 뒤집기 · 사진 칸 둘레 글 · 꼭짓점 이름 표 · 꼭짓점 방향 검사 · 판 영역 꼭짓점(화면 픽셀 · 화면 이름 순서)."""
        rot, flip = self._vr()
        names = geo.view_names(rot, flip)
        p = self.calib.d.get("plate")
        pv = None
        if p:
            W0, H0 = p["size"]
            inv = {v: k for k, v in names.items()}
            pv = {"corners": [[round(c, 2) for c in geo.to_view(*p["corners"][CORNERS.index(inv[n])], W0, H0, rot, flip)] for n in geo.VIEW_NAMES],
                  "size": list(geo.view_size(W0, H0, rot)), "t": p["t"]}
        return {"rot": rot, "flip": flip, "dirs": self._dirs(), "names": names, "check": self._view_check(), "plate": pv}

    def view(self):
        cal = self.calib.d
        stage_pts = cal["coords"]["stage"]
        quality = None
        if all(n in stage_pts for n in CORNERS):
            quality = geo.quad_quality([stage_pts[n] for n in CORNERS], self._size_mm())
            quality["warn"] = bool(quality["max_side_err"] > float(self.cfg["coord_warn_mm"]) or quality["max_angle_err"] > float(self.cfg["coord_warn_deg"]))
        errs = self.calib.extra_errors()
        to_plate = self.calib.stage_to_plate(self._size_mm())
        S = self._size_mm()
        to_view = None if to_plate is None else geo.view_matrix(S, S, *self._vr()) @ np.asarray(to_plate, float)     # 스테이지 X · Y → 화면 판 mm
        miss = self.calib.missing_corner()
        lc = self.last_click
        return {
            "steps": self.steps(),
            "origin": dict(cal.get("origin") or {}),
            "marker_seen": self.marker_seen,
            "photo": self._photo_view(),
            "plate": cal.get("plate"),
            "empty": cal.get("empty"),
            "view": self._view_block(),
            "coords": {"stage": dict(stage_pts), "d_guess": self.calib.d_guess(), "quality": quality, "ready": self.calib.coords_ready(),
                       "view_stage": {self.vname(n): xy for n, xy in stage_pts.items()},                # 화면 이름(문 기준) → 스테이지 X · Y
                       "guess_name": None if miss is None else self.vname(miss),
                       "to_view": None if to_view is None else [[float(v) for v in row] for row in to_view],
                       "extra": [dict(e, err=errs[i]) for i, e in enumerate(cal["coords"]["extra"])],
                       "marker": None if not cal.get("marker") else {"t": cal["marker"]["t"]},
                       "to_plate": None if to_plate is None else [[float(v) for v in row] for row in to_plate]},   # 스테이지 X · Y → 판 mm (3×3)
            "last_click": None if lc is None else dict(lc, vplate=self._vplate(lc["plate"])),
            "contact": dict(self.contact),
            "z_floor": self._z_floor_view(),
            "pos_block": self.pos_block,
            "find": self.find_info,
            "cooler": self._cooler_view(),
            "focus": {k: v for k, v in self.focus.items() if k != "roi"},
            "focus_cfg": {"value": self.cfg["camera_focus_value"], "lo": self.cfg["focus_scan_min"], "hi": self.cfg["focus_scan_max"]},
        }

    def _z_floor_view(self):
        """수동 Z 하강 한도(초기 설정 6 화면에 보인다): 방식 · 한도 mm(없으면 None) · 한도를 못 정하는 까닭."""
        floor, why = self.c.safety.z_floor()
        return {"mode": self.cfg["z_floor_mode"], "mm": None if floor is None else round(floor, 3), "why": why}

    def publish(self):
        self.c.state.set("prep", self.view())
        self.c.state.set("samples", [dict(s, vplate=self._vplate(s["plate"]), voutline=[self._vplate(q) for q in s["outline"]]) for s in self.samples])     # v… = 화면(문 기준) 판 mm


def _opposite(side):
    """반대쪽(먼 쪽 ↔ 모터 쪽)."""
    return side[0] + ("_motor" if side.endswith("far") else "_far")


class _FocusStop(Exception):
    """초점 찾기 도중 [정지] · 비상정지(세대가 바뀜)."""
