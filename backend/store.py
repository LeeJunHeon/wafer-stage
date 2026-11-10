"""store.py - 원자적 JSON 저장 · 설정 저장소(검증 통과해야 저장, 직전본 20개 보관) · 이전 프로그램 설정 가져오기(읽기 전용)."""

import copy
import json
import os
import shutil
import threading
import time

import settings_schema as sch
from core import paths

BACKUP_KEEP = 20


def write_json(path, obj):
    """임시 파일에 쓴 뒤 바꿔치기(전원이 나가도 반쯤 쓴 파일이 남지 않는다)."""
    os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1, default=_jd)
    os.replace(tmp, path)


def _jd(o):
    try:
        import numpy as np
        if isinstance(o, np.ndarray):
            return o.tolist()
        if isinstance(o, (np.floating, np.integer)):
            return o.item()
    except ImportError:
        pass
    raise TypeError(type(o))


def read_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return default


class Settings:
    def __init__(self, path=None):
        self.path = path or paths.SETTINGS_PATH
        self.lock = threading.RLock()
        raw = read_json(self.path, {}) or {}
        mig, notes = sch.migrate(raw, fill=True)
        vals, errs, _w = sch.validate(sch.deep_merge(sch.defaults(), mig))
        if errs:                                              # 깨진 값은 기본값으로 되돌리고 시작
            bad = sch.defaults()
            for k in list(errs):
                if sch.has_path(vals, k):
                    sch.set_path(vals, k, sch.get_path(bad, k))
        self.values = vals
        self.extra = {k: v for k, v in raw.items() if k not in vals}
        self.migrate_notes = [n for n in notes if "예전 기본값" in n or "이전 안 함" in n]
        if raw and raw.get("settings_rev") != vals.get("settings_rev"):   # 판 번호를 올린 이전(한 번만): 바로 저장한다(직전본은 settings_backup 에)
            try:
                self._backup()
                out = dict(self.extra)
                out.update(vals)
                write_json(self.path, out)
            except OSError as e:
                self.migrate_notes.append("설정 이전 저장 실패 · %s" % e)

    def get(self, key, default=None):
        return sch.get_path(self.values, key, default)

    def save(self, patch):
        """patch(점 표기 또는 중첩 dict)를 합쳐 전부 검증하고 통과하면 원자적으로 저장. 반환 (ok, errors, warnings)."""
        with self.lock:
            flat = patch if all("." in k or k in self.values for k in patch) else patch
            new = copy.deepcopy(self.values)
            for k, v in flat.items():
                if "." in k:
                    sch.set_path(new, k, v)
                else:
                    new[k] = v
            vals, errs, warns = sch.validate(new)
            if errs:
                return False, errs, warns
            self._backup()
            out = dict(self.extra)
            out.update(vals)
            write_json(self.path, out)
            self.values = vals
            return True, {}, warns

    def check(self, patch):
        """저장하지 않고 검증만. (errors{key: 문구}, warnings)."""
        with self.lock:
            new = copy.deepcopy(self.values)
            for k, v in patch.items():
                if "." in k:
                    sch.set_path(new, k, v)
                else:
                    new[k] = v
            _vals, errs, warns = sch.validate(new)
            return errs, warns

    def _backup(self):
        if not os.path.exists(self.path):
            return
        os.makedirs(paths.SETTINGS_BACKUP_DIR, exist_ok=True)
        dst = os.path.join(paths.SETTINGS_BACKUP_DIR, time.strftime("settings_%Y%m%d_%H%M%S_") + "%03d.json" % (int(time.time() * 1000) % 1000))
        shutil.copy2(self.path, dst)
        files = sorted(f for f in os.listdir(paths.SETTINGS_BACKUP_DIR) if f.startswith("settings_"))
        for f in files[:-BACKUP_KEEP]:
            try:
                os.remove(os.path.join(paths.SETTINGS_BACKUP_DIR, f))
            except OSError:
                pass


IMPORT_KEYS = ["serial_port", "camera_index", "width", "height", "fourcc", "wb_temperature", "exposure", "focus_mode", "focus",
               "limits", "motion", "park_xy", "capture_xy", "relay", "cooler"]


def import_old(path):
    """이전 프로그램 settings.json 을 **읽기만** 해서 가져올 항목의 patch(dict)를 돌려준다. 보정 파일은 가져오지 않는다."""
    raw = read_json(path)
    if not isinstance(raw, dict):
        return None, "설정 파일을 읽지 못함"
    mig, _n = sch.migrate(raw, fill=False)
    patch, skipped = {}, []
    for k in IMPORT_KEYS:
        if k in mig:
            patch[k] = copy.deepcopy(mig[k])
    hr = (mig.get("heater") or {}).get("relay") if isinstance(mig.get("heater"), dict) else None
    if isinstance(hr, int) and 1 <= hr <= 10:
        patch["heater"] = {"relay": hr}                     # 히트건 릴레이 번호만(온도 보호 값은 이 프로그램에 없다)
    return patch, skipped
