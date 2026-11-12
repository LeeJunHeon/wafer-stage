"""results.py - 결과 탭: 저장된 측정 기록을 읽는다(<데이터 폴더>/<YYYY-MM-DD>/<HHMMSS>_<묶음>/run.json 과 .dat).

기록 id = "<날짜 폴더>/<기록 폴더>"(예 "2026-11-11/142000_1"). 점은 회차 .dat 파일에서 읽는다(온도 · 저항) - 가열/냉각 구분은
run.json 의 heat_rows(가열 줄 수), 없으면 최고 온도에서 나눈다. 표는 첫 회차로 계산하고, 그래프용 점은 모든 회차를 이어서 준다.
"""

import json
import os
import re
import shutil
import time
from pathlib import Path

from app.jobs import analysis, files
from app.safety import Rejected

ID_RE = re.compile(r"^\d{4}-\d{2}-\d{2}/[^/\\]+$")
MAX_LIST = 300


def _folder(data_dir, rec_id):
    if not ID_RE.match(str(rec_id or "")):
        raise Rejected("그런 측정 기록이 없습니다")
    root = Path(data_dir).resolve()
    p = (root / rec_id).resolve()
    if root not in p.parents or not (p / "run.json").is_file():
        raise Rejected("그런 측정 기록이 없습니다")
    return p


def _meta(folder):
    try:
        return json.loads((folder / "run.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raise Rejected("측정 기록을 읽지 못했습니다")


def list_records(data_dir):
    """측정 기록 목록(최근이 위). [{"id", "started", "batch", "n", "done", "state"}]"""
    root = Path(data_dir)
    out = []
    if not root.is_dir():
        return out
    days = sorted((d for d in root.iterdir() if d.is_dir() and re.match(r"^\d{4}-\d{2}-\d{2}$", d.name)), reverse=True)
    for d in days:
        for f in sorted((x for x in d.iterdir() if x.is_dir() and (x / "run.json").is_file()), reverse=True):
            try:
                m = json.loads((f / "run.json").read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            ss = m.get("samples") or []
            out.append({"id": "%s/%s" % (d.name, f.name), "started": m.get("started"), "batch": m.get("batch", ""), "n": len(ss),
                        "done": sum(1 for s in ss if s.get("status") == "끝남"), "state": m.get("state", "")})
            if len(out) >= MAX_LIST:
                return out
    return out


def read_dat(path):
    """회차 .dat → [(온도, 저항)]. 머리글 줄은 건너뛴다. 못 읽는 줄은 버린다."""
    pts = []
    try:
        text = Path(path).read_text(encoding="ascii", errors="replace")
    except OSError:
        return pts
    for ln in text.split("\n"):
        a = ln.strip().split("\t")
        if len(a) != 2:
            continue
        try:
            pts.append((float(a[0]), float(a[1])))
        except ValueError:
            continue
    return pts


def cycle_points(folder, cyc):
    """회차 하나의 점 [[온도, 저항, 단계(0 가열 · 1 냉각)]]."""
    if not cyc:
        return []
    raw = read_dat(folder / cyc["file"])
    if not raw:
        return []
    k = cyc.get("heat_rows")
    if k is None:
        k = max(range(len(raw)), key=lambda i: raw[i][0]) + 1
    return [[t, r, 0 if i < k else 1] for i, (t, r) in enumerate(raw)]


def sample_points(folder, sample):
    """샘플의 첫 회차 점 - 표(전이 온도 · 저항 비)는 첫 회차로 계산한다."""
    return cycle_points(folder, (sample.get("cycles") or [None])[0])


def sample_all_points(folder, sample):
    """샘플의 모든 회차 점을 이어 붙인 것 - 그래프는 반복 측정의 모든 회차를 그린다."""
    out = []
    for cyc in sample.get("cycles") or []:
        out += cycle_points(folder, cyc)
    return out


def load_record(data_dir, rec_id):
    """기록 하나: 샘플 표(시작 온도 저항 · 최고 온도 저항 · 저항 비 · 전이 온도) · 파일 · NAS · 그날 조건."""
    folder = _folder(data_dir, rec_id)
    m = _meta(folder)
    rows = []
    for s in m.get("samples") or []:
        pts = sample_points(folder, s)
        a = analysis.analyze(pts)
        rows.append({"id": s.get("id"), "name": s.get("name"), "order": s.get("order"), "status": s.get("status"), "reason": s.get("reason", ""), "notes": s.get("notes") or [],
                     "n": len(pts), "cycles": len(s.get("cycles") or []), "sum": a, "files": s.get("files") or [], "moved": s.get("moved") or []})
    names = sorted(p.name for p in folder.iterdir() if p.is_file())
    for d in sorted(p for p in folder.iterdir() if p.is_dir() and p.name.startswith("이전_")):      # 다시 재기 전의 파일을 옮겨 둔 폴더
        names.append("%s\\ (예전 파일 %d개)" % (d.name, sum(1 for x in d.iterdir() if x.is_file())))
    return {"id": rec_id, "folder": str(folder), "started": m.get("started"), "batch": m.get("batch", ""), "state": m.get("state", ""), "cond": m.get("cond") or {},
            "nas": m.get("nas"), "samples": rows, "files": names}


def load_points(data_dir, rec_id, sample_id):
    folder = _folder(data_dir, rec_id)
    m = _meta(folder)
    for s in m.get("samples") or []:
        if str(s.get("id")) == str(sample_id):
            return {"name": s.get("name"), "cycles": len(s.get("cycles") or []), "points": sample_all_points(folder, s)}
    raise Rejected("그런 샘플이 없습니다")


def save_png(data_dir, rec_id, data):
    """그래프 그림(PNG)을 그 기록 폴더에 저장하고 경로를 돌려준다."""
    folder = _folder(data_dir, rec_id)
    if not data or data[:8] != b"\x89PNG\r\n\x1a\n":
        raise Rejected("그림 데이터가 아닙니다")
    if len(data) > 20 * 1024 * 1024:
        raise Rejected("그림이 너무 큽니다")
    path, _ch = files.unique_path(str(folder / ("graph_%s.png" % time.strftime("%H%M%S"))))
    try:
        Path(path).write_bytes(data)
    except OSError as e:
        raise Rejected("그림을 저장하지 못했습니다 · %s" % str(e)[:50])
    return {"path": path}


def open_folder(data_dir, rec_id):
    """탐색기로 그 기록 폴더를 연다(Windows)."""
    folder = _folder(data_dir, rec_id)
    opener = getattr(os, "startfile", None)
    if opener is None:
        raise Rejected("이 컴퓨터에서는 폴더를 열 수 없습니다 · 경로: %s" % folder)
    try:
        opener(str(folder))
    except OSError as e:
        raise Rejected("폴더를 열지 못했습니다 · %s" % str(e)[:50])
    return {"folder": str(folder)}


def nas_copy(data_dir, rec_id):
    """[다시 복사]: 그 기록의 .dat 파일을 NAS 폴더로 다시 복사하고 run.json 의 NAS 상태를 고친다."""
    folder = _folder(data_dir, rec_id)
    m = _meta(folder)
    nas = m.get("nas") or {}
    dst = nas.get("dir")
    if not dst:
        raise Rejected("이 기록은 NAS 폴더가 정해져 있지 않습니다")
    dats = sorted(p for p in folder.iterdir() if p.is_file() and p.suffix == ".dat")
    names = dict(nas.get("names") or {})                            # PC 파일 이름 → 이 기록이 NAS 에 쓴 이름
    done, pending, err = [], [], ""
    try:
        os.makedirs(dst, exist_ok=True)
    except OSError as e:
        err = str(e)
    for p in dats:
        if err:
            pending.append(p.name)
            continue
        try:
            # 이 기록이 전에 쓴 파일은 같은 이름으로 덮어쓴다. 처음 보내는 파일은 NAS 에 같은 이름(다른 기록의 것)이 있으면 덮어쓰지 않고 _1 을 붙인다.
            to = os.path.join(dst, names[p.name]) if p.name in names else files.unique_path(os.path.join(dst, p.name))[0]
            shutil.copyfile(str(p), to)
            names[p.name] = os.path.basename(to)
            done.append(p.name)
        except OSError as e:
            pending.append(p.name)
            err = str(e)
    m["nas"] = {"dir": dst, "ok": not pending, "pending": pending, "done": done, "names": names}
    try:
        tmp = folder / "run.json.tmp"
        tmp.write_text(json.dumps(m, ensure_ascii=False, indent=1), encoding="utf-8")
        tmp.replace(folder / "run.json")
    except OSError:
        pass
    if pending:
        raise Rejected("NAS 에 쓰지 못했습니다 · %s" % err[:60])
    return {"copied": len(done)}
