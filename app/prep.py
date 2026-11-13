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

from app import geometry as geo
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
        self.pos_block = False                      # 위치 확인 경보(측정 시작 막음)
        self.samples = []
        self.find_info = None                       # {"t", "n", "notes", "path", "ms"}
        self.last_click = None                      # 판 사진에서 마지막으로 누른 자리 {"px": 사진 픽셀, "plate": 판 mm}
        self.contact = {"active": False, "z": None, "text": "", "result": None, "fail": ""}
        if self.c.calib.note:
            self.c.state.alarm("calib", self.c.calib.note, NOTICE)

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
                if not self._at(head) or self.c._mm("z") != 0:
                    self.c.goto(head[0], head[1])
                at_head = True
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
        e = self.calib.d.get("empty")
        if not e:
            return None
        if self._empty is None or self._empty[0] != e["t"]:
            img = cv2.imread(str(self.calib.empty_path), cv2.IMREAD_COLOR) if self.calib.empty_path.exists() else None
            self._empty = (e["t"], img)
        return self._empty[1]

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

    def jpeg(self, what, **kw):
        """화면용 그림. what: raw(찍은 원본, 가로 1600 까지) · crop(원본 해상도 조각: x, y, half) · plate(보정한 판 사진) ·
        empty(보정한 빈 판 사진). 없으면 None."""
        ph = self.photo
        if what == "raw":
            return None if ph is None else self._jpeg(ph["frame"], 1600)
        if what == "crop":
            if ph is None:
                return None
            f = ph["frame"]
            x, y, half = int(float(kw.get("x", 0))), int(float(kw.get("y", 0))), max(20, min(400, int(float(kw.get("half", 120)))))
            pad = cv2.copyMakeBorder(f, half, half, half, half, cv2.BORDER_CONSTANT, value=(255, 255, 255))
            x, y = max(0, min(f.shape[1] - 1, x)), max(0, min(f.shape[0] - 1, y))
            return self._jpeg(pad[y:y + 2 * half, x:x + 2 * half], None, 90)
        if what == "plate":
            return self._jpeg(self._inner(self.photo_rect()))
        if what == "empty":
            return self._jpeg(self._inner(self.empty_rect()))
        return None

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

    def plate_set(self, corners):
        """사진에서 누른 판 꼭짓점 A · B · C · D(원본 픽셀, 왼쪽 위부터 시계 방향) → "사진 → 판 mm" 변환."""
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
    # 4 · 빈 판 사진
    # ------------------------------------------------------------------
    def empty_take(self):
        """핀 맨 위 → 촬영 위치 → 원본 한 장을 calib/empty_plate.png 로 저장. 언제든 다시 찍어 바꿀 수 있다."""
        frame = self.capture(move=True, need_head=True)
        self.calib.dir.mkdir(parents=True, exist_ok=True)
        ok, buf = cv2.imencode(".png", frame, [cv2.IMWRITE_PNG_COMPRESSION, 1])
        if not ok:
            raise Rejected("빈 판 사진 저장 실패")
        try:
            self.calib.empty_path.write_bytes(buf.tobytes())
        except OSError as e:
            raise Rejected("빈 판 사진 저장 실패 · %s" % str(e)[:40])
        self.calib.set_empty((frame.shape[1], frame.shape[0]))
        self._empty = (self.calib.d["empty"]["t"], frame)
        self.publish()
        return {"t": self.calib.d["empty"]["t"]}

    # ------------------------------------------------------------------
    # 5 · 좌표 맞추기
    # ------------------------------------------------------------------
    def coord_set(self, name):
        """[여기가 A] … [여기가 D]: 지금 스테이지 X · Y 를 그 꼭짓점 자리로 기억한다. 4점이 모이면 헤드 마커도 이어서 기억한다."""
        name = str(name).upper()
        if name not in CORNERS:
            raise Rejected("꼭짓점은 A · B · C · D 중 하나")
        self._need_xy()
        self.c._no_run("좌표 보정")
        self.calib.set_corner_stage(name, self._xy())
        self.clear_pos_check()
        self._refresh_samples()
        self.publish()
        out = {"name": name, "xy": self.calib.d["coords"]["stage"][name]}
        if all(n in self.calib.d["coords"]["stage"] for n in CORNERS):
            try:
                out["marker"] = self.marker_learn()
            except Rejected as e:
                out["marker_note"] = str(e)
                self.c.state.alarm("marker", "마커 등록 실패 · %s" % e, NOTICE)
        return out

    def coord_goto_d(self):
        g = self.calib.d_guess()
        if g is None:
            raise Rejected("A · B · C 등록 필요")
        return self.c.goto(g[0], g[1])

    def coord_clear(self):
        self.c._no_run("좌표 보정")
        self.calib.clear_coords()
        self.clear_pos_check()
        self._refresh_samples()
        self.publish()
        return {}

    def marker_learn(self):
        """촬영 위치에서 헤드 마커 가운데 픽셀을 기억하고, X · Y 로 조금 옮겨 보며 "mm 당 픽셀" 2×2 행렬을 잰다."""
        head = self.cfg["photo_head_xy"]
        if head is None:
            raise Rejected("촬영 위치 설정 필요 (초기 설정 3)")
        self._need_camera()
        self.c._no_run("헤드 마커 기억")
        if not all(self.c.stage.homed.get(a) for a in "xyz"):
            raise Rejected("세 축 원점 필요 (초기 설정 2)")
        step = float(self.cfg["marker_probe_mm"])

        def see(x, y):
            self.c.goto(x, y)
            frame, _t = self._fresh()
            return geo.find_marker(frame)

        m0 = see(head[0], head[1])
        self.marker_seen = m0 is not None
        if m0 is None:
            self.calib.set_marker(None, None)
            self.publish()
            raise Rejected("마커 미검출")
        cols = []
        for axis in (0, 1):
            got = None
            for sign in (1.0, -1.0):                                 # +10 mm 에서 안 보이거나 범위 밖이면 −10 mm
                p = [head[0], head[1]]
                p[axis] += sign * step
                if p[axis] < 0 or p[axis] > float(self.cfg["%s_max_mm" % "xy"[axis]]):
                    continue
                m = see(p[0], p[1])
                if m is not None:
                    got = [(m[0] - m0[0]) / (sign * step), (m[1] - m0[1]) / (sign * step)]
                    break
            if got is None:
                self.c.goto(head[0], head[1])
                self.calib.set_marker(None, None)
                self.publish()
                raise Rejected("이동 후 마커 미검출")
            cols.append(got)
        self.c.goto(head[0], head[1])
        J = [[cols[0][0], cols[1][0]], [cols[0][1], cols[1][1]]]      # 열 = X · Y 로 1 mm 움직일 때 마커가 움직이는 픽셀
        if abs(np.linalg.det(np.asarray(J))) < 1e-6:
            self.calib.set_marker(None, None)
            self.publish()
            raise Rejected("마커 이동량 측정 실패")
        self.calib.set_marker(m0, J)
        self.clear_pos_check()
        self.c.state.clear_alarm("marker")
        self.publish()
        self.c.logs.app("헤드 마커 기억 · 픽셀 (%.1f, %.1f) · mm 당 %.2f 픽셀" % (m0[0], m0[1], float(np.hypot(*cols[0]))))
        return {"px": [round(m0[0], 1), round(m0[1], 1)]}

    def plate_goto(self, x, y):
        """판 사진을 누른 자리(판 mm)로 간다(핀 맨 위로 올린 뒤). 샘플 근처를 누르면 샘플 가운데로 붙는다."""
        Hp, Hs = self.calib.plate_h(self._size_mm()), self.calib.stage_h()
        if Hp is None or Hs is None:
            raise Rejected("초기 설정 3 · 5 필요")
        px, py = float(x), float(y)
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
        return {"xy": [round(sx, 2), round(sy, 2)], "sample": snapped}

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
        t0 = time.monotonic()
        self.capture(move=True, need_head=True)
        cur = self.photo_rect()
        if cur is None:
            raise Rejected("사진 크기 변경됨 · 초기 설정 3 재설정 필요")
        emp = self.empty_rect()
        res = find_mod.find_samples(cur, emp, geo.RECT_PPMM, geo.RECT_MARGIN_MM, log=lambda t: self.c.logs.app("샘플 찾기 · " + t, "detail"))
        if emp is None:
            res["notes"].append("빈 판 사진 없음 · 사진 한 장으로 검출")
        batch = self.cfg["batch_name"]
        self.samples = []
        for i, s in enumerate(res["samples"], 1):
            self.samples.append({"id": i, "name": "%s-%d" % (batch, i), "plate": s["plate"], "outline": s["outline"], "w": s["w"], "h": s["h"], "area": s["area"],
                                 "source": s["source"], "weak": s["weak"], "small": False, "xy": None, "use": True})
        self._refresh_samples(reset_use=True)
        self.c.runner.reset()                                        # 새 사진 · 새 샘플: 지난 측정 진행 표시는 비운다(파일은 그대로)
        self.find_info = {"t": self.photo["t"], "n": len(self.samples), "notes": res["notes"], "path": res["path"], "ms": int((time.monotonic() - t0) * 1000)}
        if res["notes"]:
            self.c.state.alarm("find", " · ".join(res["notes"]), NOTICE, dismiss=True)
        else:
            self.c.state.clear_alarm("find")
        self.c.logs.app("샘플 찾기 · %d개 · %s · %d ms" % (len(self.samples), res["path"], self.find_info["ms"]))
        self.publish()
        return {"n": len(self.samples), "used": sum(1 for s in self.samples if s["use"]), "notes": res["notes"]}

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
                return {"id": s["id"], "use": s["use"]}
        raise Rejected("해당 샘플 없음")

    # ------------------------------------------------------------------
    # 7 · 쿨러 구역
    # ------------------------------------------------------------------
    def cooler_only(self, n):
        """쿨러 릴레이 하나만 켠다(다른 쿨러는 끈다). n 이 0 이면 쿨러를 모두 끈다."""
        n = int(n)
        self.c._need_stage()
        self.c._no_run("쿨러 시험")
        heater = int(self.cfg["heater_relay"])
        if n != 0 and (n == heater or not 1 <= n <= 10):
            raise Rejected("쿨러 릴레이 번호 아님")
        for i, on in enumerate(self.c.stage.relays, 1):
            if on and i != heater and i != n:
                self.c.relay_off(i)
        if n:
            self.c.relay_on(n)
        return {"relays": list(self.c.state.get("relays"))}

    def cooler_save(self, grid):
        from app import config as config_mod
        try:
            self.cfg.update({"cooler_grid": grid})
            self.cfg.save()
        except config_mod.ConfigError as e:
            raise Rejected(str(e))
        self.calib.set_cooler()
        self.c.publish_cfg()
        self.publish()
        return {"cooler_grid": self.cfg["cooler_grid"]}

    # ------------------------------------------------------------------
    # 화면에 보낼 상태
    # ------------------------------------------------------------------
    def _photo_view(self):
        ph = self.photo
        if ph is None:
            return None
        return {"id": ph["id"], "t": ph["t"], "w": int(ph["frame"].shape[1]), "h": int(ph["frame"].shape[0]), "at_head": ph["at_head"], "pos_off_mm": self.pos_off_mm,
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
        p_text = ("판 영역 · %s" % when(plate["t"]) if plate else "판 영역 없음") if head is not None else "촬영 위치 없음"
        emp = cal.get("empty")
        ready = self.calib.coords_ready()
        n_ex = len(cal["coords"]["extra"])
        n_c = sum(1 for n in CORNERS if n in cal["coords"]["stage"])
        co_text = ("4점%s · %s" % (" + 보정 %d점" % n_ex if n_ex else "", when(cal["coords"].get("t")))) if ready else ("꼭짓점 %d / 4" % n_c if n_c else "")
        cz = c["contact_z_mm"]
        ct = cal.get("contact")
        cool = cal.get("cooler")
        return [
            {"done": dev_ok, "text": "전체 연결됨" if dev_ok else "%d / 4 연결" % n_dev},
            {"done": o_done, "text": o_text},
            {"done": bool(plate) and head is not None, "text": p_text},
            {"done": bool(emp), "text": "촬영 · %s" % when(emp["t"]) if emp else "없어도 측정 가능"},
            {"done": ready, "text": co_text},
            {"done": cz is not None, "text": ("Z %.2f mm · %s" % (cz, when(ct["t"] if ct else None))) if cz is not None else ""},
            {"done": bool(cool), "text": "설정 완료 · %s" % when(cool["t"]) if cool else "번호순 기본값"},
        ]

    def view(self):
        cal = self.calib.d
        stage_pts = cal["coords"]["stage"]
        quality = None
        if all(n in stage_pts for n in CORNERS):
            quality = geo.quad_quality([stage_pts[n] for n in CORNERS], self._size_mm())
            quality["warn"] = bool(quality["max_side_err"] > float(self.cfg["coord_warn_mm"]) or quality["max_angle_err"] > float(self.cfg["coord_warn_deg"]))
        errs = self.calib.extra_errors()
        to_plate = self.calib.stage_to_plate(self._size_mm())
        return {
            "steps": self.steps(),
            "origin": dict(cal.get("origin") or {}),
            "marker_seen": self.marker_seen,
            "photo": self._photo_view(),
            "plate": cal.get("plate"),
            "empty": cal.get("empty"),
            "coords": {"stage": dict(stage_pts), "d_guess": self.calib.d_guess(), "quality": quality, "ready": self.calib.coords_ready(),
                       "extra": [dict(e, err=errs[i]) for i, e in enumerate(cal["coords"]["extra"])],
                       "marker": None if not cal.get("marker") else {"t": cal["marker"]["t"]},
                       "to_plate": None if to_plate is None else [[float(v) for v in row] for row in to_plate]},   # 스테이지 X · Y → 판 mm (3×3)
            "last_click": self.last_click,
            "contact": dict(self.contact),
            "pos_block": self.pos_block,
            "find": self.find_info,
        }

    def publish(self):
        self.c.state.set("prep", self.view())
        self.c.state.set("samples", [dict(s) for s in self.samples])
