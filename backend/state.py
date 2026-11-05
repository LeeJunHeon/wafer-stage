"""state.py - 서버 상태의 단일 주인 + settings.json 로드/저장.

서버가 상태의 주인이다. 화면은 여기서 만든 snapshot() 을 받아 그리기만 한다.
화면이 스스로 값을 지어내지 않으므로 "화면은 바뀌었는데 장비는 그대로" 가 없다.
"""

import copy
import os
import time

from core import calib, detect, paths
from core import stage as stage_mod

import logger
import recipe as recipe_mod
import settings_schema as schema
import version
from storage import atomic_write_json, safe_read_json

# 앱이 편집하는 설정의 기본값·키는 settings_schema 가 단일 출처다(중복 금지).
# 검출 파라미터(detect.DEFAULTS)는 settings.json 에 그대로 있고 파일에서 편집한다.
APP_KEYS = schema.app_keys()
DEFAULT_APP = schema.defaults()
BACKUP_KEEP = 20


class State:
    def __init__(self):
        self.settings = {}
        self.params = {}                   # detect/calib 에 넘기는 전체 파라미터
        self.load_settings()

        self.stage = {"connected": False, "port": None,
                      "homed_x": False, "homed_y": False, "homed_z": False,
                      "x_mm": None, "y_mm": None, "z_mm": None, "u": None, "v": None,
                      "moving": False, "dirty": False, "needs_home": False,
                      "jog_mode_x": "rel", "jog_mode_y": "rel", "jog_mode_z": "rel",
                      "fw": "", "last_error": ""}
        self.camera = {"index": self.settings.get("camera_index", 1), "ok": None,
                       "last_error": "", "capturing": False, "preview": False}
        self.frame = None                  # {"id","ts","w","h","url"}
        self.calib = None                  # {"refit","used_ids",...}
        self.sensing = None                # {"rect":[u0,v0,u1,v1]}
        self.wafer = None                  # {"found","cx","cy","r_px","center_mm"}
        self.markers = {}                  # {id: [[u,v] x4]}
        self.head_marker = None            # 촬영본의 헤드 마커 {"center","corners"} (표시용)
        # 고정 보정(감지 영역 + 판 보정) 요약 - backend/platecal.py 가 채운다
        self.plate = {"mode": "fixed", "valid": False, "area_ok": False, "reason": "감지 영역 없음",
                      "data": None, "shot": None}
        self.samples = []                  # 화면용 샘플 목록(늘 번호순)
        # 샘플 편집(수동 추가·삭제). 스택에 있는 것 = 지금 효력이 있는 편집이고
        # 되돌리기는 끝에서부터 뺀다. 항목 {"op":"add","no"} / {"op":"del","sample":dict}.
        # 편집은 그 촬영에만 속한다 - 새 촬영이 스택을 비운다(engine._apply_sense).
        self.edits = []
        self.next_no = 1                   # 수동 추가가 받을 번호(되돌려도 줄지 않는다)
        self.sequence = {"phase": "idle", "mode": "auto",
                         "cur_no": None, "done": 0, "total": 0, "elapsed_s": 0,
                         "out_dir": None, "message": "",
                         # 레시피 실행 상태(engine 이 채운다)
                         "recipe": None, "tool": None, "step_index": None,
                         "step_total": 0, "step_desc": "", "step_left_s": None,
                         "steps": [], "est_sample_s": None, "est_total_s": None,
                         "used": False}
        # 릴레이 10개(펌웨어 RS 줄이 진실). since = 켜진 것을 본 시각(epoch)
        self.relays = [{"n": i + 1, "on": False, "since": None} for i in range(10)]
        # 사진 호밍(헤드 마커) 상태 - 상단 '위치 확인' 칩
        self.photohome = {"status": "unknown", "d_mm": None, "time": None,
                          "x_mm": None, "y_mm": None, "msg": "",
                          "calib": None, "calib_valid": False, "calib_reason": "보정 없음"}
        # 경보 배너용 {src, text}. 통신 끊김 · 릴레이 시간 초과 등
        self.alarms = []
        # 순회 밖의 긴 작업(헤드 보정 · 공구 가열) 진행 - engine.run_job 이 채운다
        self.job = {"kind": None, "running": False, "i": 0, "n": 0, "message": "",
                    "result": None}
        self.warnings = []
        self.startup_notices = []

    # ---------------- settings ----------------
    def load_settings(self):
        """settings.json 하나가 설정의 단일 출처. 없으면 기본값으로 만든다.

        예전 형식은 schema.migrate 로 이행해서 읽는다(파일은 다음 저장 때 바뀐다).
        파일의 값이 검증에 걸리면 그 키만 기본값으로 돌리고 경고를 남긴다 -
        잘못된 설정 하나로 프로그램이 안 뜨면 고칠 화면도 못 연다.
        """
        got = safe_read_json(paths.SETTINGS_PATH)
        existed = bool(got)
        if not got:
            # settings.json 은 장비별 로컬 파일(git 제외)이다. 없으면 스키마 기본값으로 시작하고
            # 설정 창에서 처음 저장할 때 만들어진다(commit).
            got = {}
            logger.early("info", "settings.json 없음 · 스키마 기본값으로 시작")
        migrated, notes = schema.migrate(got)
        for n in (notes if existed else []):
            logger.early("info", "설정 이행 · " + n)
        merged = schema.deep_merge(DEFAULT_APP, migrated)
        v, errs, _w = schema.validate(merged)
        for key, why in errs.items():
            top = key.split("[")[0]
            it = schema.BY_KEY.get(top)
            if it is not None:
                schema.set_path(merged, top, copy.deepcopy(it["default"]))
            logger.early("warn", "설정 %s 무시(%s) · 기본값 사용" % (key, why))
        if errs:
            v, _e, _w = schema.validate(merged)
        p = dict(detect.DEFAULTS)
        p.update(merged)                   # 파일의 검출 파라미터·모르는 키까지
        p.update({k: v[k] for k in APP_KEYS})
        self.params = p
        self.settings = {k: copy.deepcopy(v[k]) for k in APP_KEYS}
        self.apply_to_core()
        return p

    def apply_to_core(self):
        """설정 중 core 가 쓰는 값(마커 좌표·가동범위·속도·시간)을 core 모듈에 반영한다."""
        g = self.get
        calib.set_marker_mm(self.settings.get("marker_mm_xy"))
        lim = self.settings.get("limits") or {}
        stage_mod.set_limits(lim.get("x_max_mm"), lim.get("y_max_mm"), lim.get("z_max_mm"))
        got = stage_mod.limits_mm()
        calib.set_limits(got["x_max_mm"], got["y_max_mm"])
        stage_mod.configure(speed_pps=g("motion.speed_pps"), jog_pps=g("motion.jog_pps"),
                            jog_slow_pps=g("motion.jog_slow_pps"),
                            line_timeout_s=g("serial.line_timeout_s"),
                            move_timeout_s=g("serial.move_timeout_s"),
                            boot_wait_s=g("serial.boot_wait_s"),
                            origin_gap_mm=g("origin_gap_mm"))

    def candidate(self, values=None, patch=None):
        """지금 설정 위에 values(중첩 dict) · patch({점 키: 값})를 얹은 후보."""
        cur = copy.deepcopy(self.settings)
        if values:
            cur = schema.deep_merge(cur, {k: v for k, v in values.items() if k in APP_KEYS})
        for k, v in (patch or {}).items():
            if k.split(".")[0] in APP_KEYS:
                schema.set_path(cur, k, v)
        return cur

    def check(self, cand):
        """(정리된 값, errors, warnings) - 저장하지 않는다."""
        return schema.validate(cand)

    def commit(self, v):
        """검증을 통과한 값 v 를 저장한다. 직전 파일을 백업하고 원자적으로 쓴다.

        앱 키만 바꾸고 파일의 나머지(검출 파라미터·모르는 키)는 그대로 둔다.
        """
        cur = safe_read_json(paths.SETTINGS_PATH)
        if cur is None:
            cur = schema.deep_merge(dict(detect.DEFAULTS), DEFAULT_APP)
        else:
            self._backup_file()
        for k in APP_KEYS:
            cur[k] = copy.deepcopy(v[k])
        atomic_write_json(paths.SETTINGS_PATH, cur)
        self.load_settings()
        self.camera["index"] = self.settings.get("camera_index", 1)

    def _backup_file(self):
        """저장 직전본을 data/settings_backup 에 남긴다(최근 BACKUP_KEEP 개)."""
        try:
            os.makedirs(paths.SETTINGS_BACKUP_DIR, exist_ok=True)
            base = "settings_" + time.strftime("%Y%m%d_%H%M%S")
            dst = os.path.join(paths.SETTINGS_BACKUP_DIR, base + ".json")
            i = 1
            while os.path.exists(dst):
                dst = os.path.join(paths.SETTINGS_BACKUP_DIR, "%s_%02d.json" % (base, i))
                i += 1
            with open(paths.SETTINGS_PATH, "rb") as f:
                data = f.read()
            with open(dst, "wb") as f:
                f.write(data)
            olds = sorted(n for n in os.listdir(paths.SETTINGS_BACKUP_DIR)
                          if n.startswith("settings_") and n.endswith(".json"))
            for n in olds[:-BACKUP_KEEP]:
                os.remove(os.path.join(paths.SETTINGS_BACKUP_DIR, n))
        except OSError as e:
            logger.write("warn", "설정 백업 실패: %s" % e)

    def save_settings(self, patch):
        """점 키 patch 를 검증해 저장한다(현재 위치를 파킹으로 등). 실패면 errors."""
        v, errs, _w = self.check(self.candidate(patch=patch))
        if errs:
            return errs
        self.commit(v)
        return {}

    def export_settings(self):
        return {"app": version.APP_NAME, "version": version.APP_VERSION,
                "exported": time.strftime("%Y-%m-%d %H:%M:%S"),
                "settings": copy.deepcopy(self.settings)}

    def import_candidate(self, data):
        """가져올 JSON(내보내기 형식 또는 settings.json 그대로)을 후보로. 형식이 틀리면 None."""
        body = data.get("settings") if isinstance(data, dict) and isinstance(
            data.get("settings"), dict) else data
        if not isinstance(body, dict):
            return None
        migrated, _notes = schema.migrate(body, fill=False)
        values = {k: migrated[k] for k in APP_KEYS if k in migrated}
        if not values:
            return None                    # 설정 키가 하나도 없다 - 설정 파일이 아니다
        return self.candidate(values=values)

    # ---------------- 편의 ----------------
    def park_xy(self):
        xy = self.settings.get("park_xy") or DEFAULT_APP["park_xy"]
        return [float(xy[0]), float(xy[1])]

    def capture_xy(self):
        xy = self.settings.get("capture_xy") or self.park_xy()
        return [float(xy[0]), float(xy[1])]

    def get(self, key, default=None):
        """점 키로 설정값."""
        return schema.get_path(self.settings, key, default)

    def tool(self):
        """활성 공구 {name, dx_mm, dy_mm, z_work_mm, note}."""
        return recipe_mod.active_tool(self.settings)

    def stage_target(self, sx, sy):
        """샘플 S 를 활성 공구로 겨냥할 때 스테이지가 갈 자리 P = S − (dx, dy).

        스테이지 좌표 P 는 '기준 포인터가 가리키는 점' 이다. 공구 끝은 P + (dx, dy)
        를 가리킨다. 가동범위 검사는 P 로 한다.
        """
        t = self.tool()
        return (float(sx) - float(t.get("dx_mm") or 0.0),
                float(sy) - float(t.get("dy_mm") or 0.0))

    def recipe(self, name=None):
        return recipe_mod.find(self.settings, name)

    def estimate(self, name=None):
        """레시피의 샘플당 · 전체 예상 시간(초). 이동은 대상 샘플 사이 평균 거리로 어림한다."""
        rc = self.recipe(name)
        speed = float(self.get("motion.speed_pps") or 6000) / stage_mod.PPMM   # mm/s
        pts = [self.stage_target(s["X"], s["Y"]) for s in self.samples
               if s.get("on") and s.get("status") not in ("skip", "done") and s.get("X") is not None]
        if len(pts) >= 2:
            dist = sum(((pts[i][0] - pts[i - 1][0]) ** 2 + (pts[i][1] - pts[i - 1][1]) ** 2) ** 0.5
                       for i in range(1, len(pts))) / (len(pts) - 1)
        else:
            dist = 30.0
        move_s = 0.6 + dist / max(speed, 1e-6) * 1.2      # 가감속 몫 20%
        per = recipe_mod.estimate_sample_s(rc, self.settings, move_s, speed)
        has_confirm = any((st or {}).get("type") == "confirm" for st in rc.get("steps") or [])
        return {"recipe": rc.get("name"), "sample_s": round(per, 1),
                "total_s": round(per * len(pts), 1), "n": len(pts),
                "confirm": has_confirm, "relays": recipe_mod.relays_used(rc)}

    def set_alarm(self, src, text):
        """경보 배너 항목(출처마다 하나). text 가 비면 지운다."""
        self.alarms = [a for a in self.alarms if a["src"] != src]
        if text:
            self.alarms.append({"src": src, "text": text})

    def sample(self, no):
        for s in self.samples:
            if s["no"] == no:
                return s
        return None

    def jog_mode(self, axis):
        """그 축의 수동 이동이 절대(abs)인가 상대(rel)인가.

        원점은 축마다 따로다(펌웨어의 HX/HY/HZ). 원점이 있고 비상정지 뒤가 아니면
        그 축은 절대 좌표로 자를 수 있고, 아니면 기준이 없어 상대 이동뿐이다
        (사람이 끝단까지 몰고 가 원점을 등록하는 경로).
        """
        st = self.stage
        if axis == "z":
            # Z 는 X·Y 잠금과 무관한 독립 축이다. 비상정지 때는 펌웨어 forget 이
            # HZ 까지 지우므로 homed_z 만 봐도 그때는 rel 이 된다.
            return "abs" if st["homed_z"] else "rel"
        homed = st["homed_" + axis] and not st.get("needs_home")
        return "abs" if homed else "rel"

    def can_move(self):
        """이동 명령을 받아도 되는 상태인가. 사유 문자열 또는 None."""
        if not self.stage["connected"]:
            return "스테이지 미연결 · 연결 후 사용하세요"
        hx, hy = self.stage["homed_x"], self.stage["homed_y"]
        if not (hx and hy):
            if not hx and not hy:
                return "원점 없음 · 수동 이동에서 등록"
            return "%s 원점 없음 · 수동 이동에서 등록" % ("Y" if hx else "X")
        if self.stage.get("needs_home"):
            # 비상정지 뒤에는 펌웨어가 위치를 안다고 해도 믿을 수 없다.
            return "비상정지 · 원점 등록 필요"
        return None

    def edit_summary(self):
        """화면용 편집 요약: 되돌릴 수 있는 단계 · 목록의 수동 샘플 · 지운 검출 샘플."""
        return {
            "undo": len(self.edits),
            "manual": sum(1 for s in self.samples if s.get("manual")),
            "deleted": sum(1 for e in self.edits
                           if e["op"] == "del" and not e["sample"].get("manual")),
        }

    def set_warnings(self, ws):
        self.warnings = list(ws or [])

    # ---------------- snapshot ----------------
    def snapshot(self):
        return {
            "type": "state",
            "version": {"name": version.APP_NAME, "version": version.APP_VERSION,
                        "build": version.BUILD_DATE},
            "stage": dict(self.stage, jog_mode_x=self.jog_mode("x"),
                          jog_mode_y=self.jog_mode("y"),
                          jog_mode_z=self.jog_mode("z")),
            "camera": dict(self.camera),
            "frame": dict(self.frame) if self.frame else None,
            "calib": dict(self.calib) if self.calib else None,
            "sensing": dict(self.sensing) if self.sensing else None,
            "wafer": dict(self.wafer) if self.wafer else None,
            "markers": {str(k): v for k, v in (self.markers or {}).items()},
            "head_marker": dict(self.head_marker) if self.head_marker else None,
            "plate": dict(self.plate),
            # 가동범위(지금 드라이버가 쓰는 값). 스테이지 맵의 축척과 조그 상한이 쓴다.
            "limits": {k: round(v, 1) for k, v in stage_mod.limits_mm().items()},
            # 서버가 지금 쓰는 마커 기준 좌표. settings 에 marker_mm_xy 가 없어도
            # 설정 창이 이 값으로 표를 채운다.
            "marker_mm": {str(k): [v[0], v[1]] for k, v in calib.MARKER_MM.items()},
            "samples": [dict(s) for s in self.samples],
            "edit": self.edit_summary(),
            "sequence": dict(self.sequence, estopped=_estopped()),
            "warnings": list(self.warnings),
            "settings": copy.deepcopy(self.settings),
            "relays": [dict(r) for r in self.relays],
            "photohome": dict(self.photohome),
            "alarms": [dict(a) for a in self.alarms],
            "tool": dict(self.tool()),
            "job": dict(self.job),
            "estimate": self.estimate(),
            "data_dir": paths.DATA_DIR,
            "log_file": _log_file(),
            "ts": time.time(),
        }


def _log_file():
    try:
        import logger
        return logger.current_file()
    except Exception:                      # noqa: BLE001
        return ""


def _estopped():
    """engine 은 state 를 import 하므로(계층) 여기서만 지연 import 한다."""
    try:
        import engine
        return bool(engine.estopped())
    except Exception:                      # noqa: BLE001
        return False


state = State()
