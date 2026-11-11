"""state.py - 화면에 보낼 모든 것을 한 덩어리로 둔다. 바뀔 때마다 구독자(WebSocket)에게 알린다.

경보(alarms)는 여기 한 곳에만 쌓는다(등급: 주의 · 경보). 같은 경보를 다른 곳에 따로 두지 않는다.
"""

import threading
import time

NOTICE, ALARM = "주의", "경보"
DEVICES = ("stage", "k2110", "k2450", "camera")
DEV_NAMES = {"stage": "스테이지", "k2110": "온도계 K2110", "k2450": "저항계 K2450", "camera": "카메라"}


class State:
    def __init__(self):
        self._lock = threading.RLock()
        self._subs = []
        self.version = 0
        self.d = {
            "devices": {k: {"name": DEV_NAMES[k], "state": "끊김", "why": "", "target": "", "info": ""} for k in DEVICES},
            "pos": {"x": None, "y": None, "z": None},               # mm. 모르면 None
            "homed": {"x": False, "y": False, "z": False},
            "dirty": None,
            "relays": [None] * 10,                                   # True 켜짐 · False 꺼짐 · None 모름
            "temp": {"value": None, "time": None},                   # °C · 읽은 시각(epoch)
            "resistance": {"ohm": None, "current": None, "volt": None, "four_wire": None, "time": None},
            "estop_locked": False,
            "heater_on_since": None,                                 # 히트건을 켠 시각(epoch) · 꺼져 있으면 None
            "alarms": [],
            "busy": "",                                              # 지금 하는 일(이동 중 등)
            "move": None,                                            # 이동 중 정보 {what, target{x,y,z mm}, pending{axis,mm}} - 끝나면 None
            "monitor": False,                                        # 1 초마다 계속 읽기
            "cfg": {},                                               # 화면이 쓰는 설정 일부
            "fake": False,                                           # 가짜 장비로 도는 중
            "prep": None,                                            # 준비 단계 상태(app/prep.py view)
            "samples": [],                                           # 찾은 샘플 목록
            "run": None,                                             # 측정 진행(app/jobs/run.py view). 점 데이터는 넣지 않는다
        }

    # ---- 읽기 ----
    def snapshot(self) -> dict:
        import copy
        with self._lock:
            out = copy.deepcopy(self.d)
            out["version"] = self.version
            out["time"] = time.time()
            return out

    def get(self, *path):
        with self._lock:
            v = self.d
            for p in path:
                v = v[p]
            return v

    # ---- 쓰기 ----
    def set(self, *path_and_value):
        """set("pos", "x", 1.0) - 마지막 인자가 값."""
        *path, value = path_and_value
        with self._lock:
            node = self.d
            for p in path[:-1]:
                node = node[p]
            if node[path[-1]] == value:
                return
            node[path[-1]] = value
            self._bump()

    def update(self, section: str, **kw):
        with self._lock:
            node = self.d[section]
            changed = False
            for k, v in kw.items():
                if node.get(k) != v:
                    node[k] = v
                    changed = True
            if changed:
                self._bump()

    def set_device(self, key: str, state: str, why: str = "", target: str = "", info: str = ""):
        with self._lock:
            dev = self.d["devices"][key]
            if (dev["state"], dev["why"], dev["target"], dev["info"]) != (state, why, target, info):
                dev.update(state=state, why=why, target=target, info=info)
                self._bump()

    # ---- 경보 ----
    def alarm(self, code: str, text: str, level: str = ALARM, dismiss: bool = False):
        """같은 code 는 하나만 둔다(문구 · 등급만 갱신). dismiss = 사람이 [지우기] 로 지울 수 있는 알림(한 번 알려 주면 되는 주의)."""
        with self._lock:
            for a in self.d["alarms"]:
                if a["code"] == code:
                    if (a["text"], a["level"]) != (text, level):
                        a.update(text=text, level=level, time=time.time(), dismiss=bool(dismiss))
                        self._bump()
                    return
            self.d["alarms"].append({"code": code, "text": text, "level": level, "time": time.time(), "dismiss": bool(dismiss)})
            self._bump()

    def clear_alarm(self, *codes: str):
        with self._lock:
            n = len(self.d["alarms"])
            self.d["alarms"] = [a for a in self.d["alarms"] if a["code"] not in codes]
            if len(self.d["alarms"]) != n:
                self._bump()

    def has_alarm(self, code: str) -> bool:
        with self._lock:
            return any(a["code"] == code for a in self.d["alarms"])

    # ---- 구독 ----
    def subscribe(self, fn):
        self._subs.append(fn)

    def unsubscribe(self, fn):
        if fn in self._subs:
            self._subs.remove(fn)

    def _bump(self):
        self.version += 1
        for fn in list(self._subs):
            try:
                fn()
            except Exception:                       # noqa: BLE001 - 구독자 오류가 제어를 막지 않는다
                pass
