"""zones.py - 쿨러 구역(판 사각형 mm 기준 rows x cols 칸) 정의 · 이행 · 검증 · 판정.

쿨러는 판 밑에 고정된 9개(릴레이 9개)다. 구역은 사진 감지 영역 분할이 아니라 **판 사각형 좌표(mm, 0~area_size_mm)** 로 정의한다.
판 좌표계 = 감지 영역 네 꼭짓점(사진 기준 TL · TR · BR · BL)을 (0,0) · (W,0) · (W,H) · (0,H) 로 놓은 것(core/plate.py plate_homography).
판 사각형 mm → 사진 px 는 감지 영역 호모그래피, → 스테이지 mm 는 판 보정으로 변환한다.

cooler.zones = {
  "rows": 3, "cols": 3,
  "row_edges": [0, ..., H] | None, "col_edges": [0, ..., W] | None,        # None = 등분(area_size_mm 에 맞춰 자동)
  "cells": [{"cooler": 1~9(표시용), "relay": 1~10, "name": "쿨러 n"}, ...]  # 행 우선, rows*cols 개
}
예전 설정({"rows","cols","relays"}: 사진 분할 기준)은 migrate() 가 같은 rows · cols · 릴레이 배정으로 등분 칸에 옮긴다.
순수 함수만 둔다(서버 상태를 모른다).
"""

import copy

DEFAULT_ROWS = DEFAULT_COLS = 3


DEFAULT_RELAY0 = 2                     # 기본 배치: 3×3 칸에 R2~R10 을 행 우선으로(히트건 = R1). 실제 배치는 [2 초 켜 보기]로 확인


def default_zones(rows=DEFAULT_ROWS, cols=DEFAULT_COLS, relay0=DEFAULT_RELAY0):
    n = rows * cols
    return {"rows": rows, "cols": cols, "row_edges": None, "col_edges": None,
            "cells": [{"cooler": i + 1, "relay": i + relay0, "name": "쿨러 %d" % (i + 1)} for i in range(n)]}


def migrate(z):
    """예전 형식({rows, cols, relays})이면 새 형식으로. 새 형식이면 빠진 값만 채워 그대로. 반환: (zones, 바뀌었나)."""
    if not isinstance(z, dict):
        return default_zones(), True
    z = copy.deepcopy(z)
    changed = False
    try:
        rows, cols = int(z.get("rows") or DEFAULT_ROWS), int(z.get("cols") or DEFAULT_COLS)
    except (TypeError, ValueError):
        rows, cols = DEFAULT_ROWS, DEFAULT_COLS
    z["rows"], z["cols"] = rows, cols
    if "cells" not in z:
        rl = z.get("relays") if isinstance(z.get("relays"), list) else list(range(1, rows * cols + 1))
        cells = []
        for i in range(rows * cols):
            r = int(rl[i]) if i < len(rl) else i + 1
            cells.append({"cooler": i + 1, "relay": r, "name": "쿨러 %d" % (i + 1)})
        z["cells"] = cells
        changed = True
    z.pop("relays", None) if "relays" in z else None
    for k in ("row_edges", "col_edges"):
        if k not in z:
            z[k] = None
            changed = True
    return z, changed


def edges_of(z, size_mm):
    """(행 경계 H 방향 rows+1 개, 열 경계 W 방향 cols+1 개) mm. 저장이 없으면 등분."""
    w, h = float(size_mm[0]), float(size_mm[1])
    rows, cols = int(z["rows"]), int(z["cols"])
    re = z.get("row_edges") or [h * i / rows for i in range(rows + 1)]
    ce = z.get("col_edges") or [w * i / cols for i in range(cols + 1)]
    return [float(v) for v in re], [float(v) for v in ce]


def cell_at(z, size_mm, x, y):
    """판 좌표 (x, y) mm 가 속한 칸 (row, col) | None(판 밖). 열 = x(W 방향), 행 = y(H 방향). 경계는 아래 · 오른쪽 칸."""
    re, ce = edges_of(z, size_mm)
    if not (ce[0] - 1e-9 <= x <= ce[-1] + 1e-9 and re[0] - 1e-9 <= y <= re[-1] + 1e-9):
        return None
    col = max(0, min(len(ce) - 2, sum(1 for e in ce[1:-1] if x >= e)))
    row = max(0, min(len(re) - 2, sum(1 for e in re[1:-1] if y >= e)))
    return row, col


def cell_quad_plate(z, size_mm, row, col):
    """칸의 판 좌표 꼭짓점 4개(TL · TR · BR · BL) mm."""
    re, ce = edges_of(z, size_mm)
    x0, x1, y0, y1 = ce[col], ce[col + 1], re[row], re[row + 1]
    return [(x0, y0), (x1, y0), (x1, y1), (x0, y1)]


def relays_of(z):
    return [int(c["relay"]) for c in (z or {}).get("cells") or []]


def validate(z, size_mm, n_relay=10, heater_relay=None):
    """오류 문구 | None. 릴레이 중복 · 범위 · 경계 단조 증가."""
    if not isinstance(z, dict):
        return "형식 오류"
    try:
        rows, cols = int(z.get("rows")), int(z.get("cols"))
    except (TypeError, ValueError):
        return "행 · 열 숫자 필요"
    if not (1 <= rows <= 6 and 1 <= cols <= 6):
        return "행 · 열 1~6"
    cells = z.get("cells")
    if not isinstance(cells, list) or len(cells) != rows * cols:
        return "칸 %d개 필요(행 × 열)" % (rows * cols)
    seen = {}
    names = set()
    for i, c in enumerate(cells):
        if not isinstance(c, dict):
            return "칸 %d 형식 오류" % (i + 1)
        try:
            r = int(c.get("relay"))
            cn = int(c.get("cooler"))
        except (TypeError, ValueError):
            return "칸 %d · 쿨러 · 릴레이 번호 필요" % (i + 1)
        if not 1 <= r <= n_relay:
            return "칸 %d · 릴레이 번호 1~%d" % (i + 1, n_relay)
        if not 1 <= cn <= 9:
            return "칸 %d · 쿨러 번호 1~9" % (i + 1)
        if r in seen:
            return "릴레이 R%d 중복 (칸 %d · 칸 %d)" % (r, seen[r] + 1, i + 1)
        seen[r] = i
        nm = str(c.get("name") or "")
        if len(nm) > 20:
            return "칸 %d · 이름 20자 이내" % (i + 1)
        names.add(nm)
    if heater_relay is not None and int(heater_relay) in seen:
        return "히트건 릴레이 R%d 는 쿨러로 쓸 수 없음" % int(heater_relay)
    w, h = float(size_mm[0]), float(size_mm[1])
    for key, n, total in (("row_edges", rows, h), ("col_edges", cols, w)):
        e = z.get(key)
        if e is None:
            continue
        if not isinstance(e, list) or len(e) != n + 1:
            return "%s %d개 필요" % ("행 경계" if key == "row_edges" else "열 경계", n + 1)
        try:
            e = [float(v) for v in e]
        except (TypeError, ValueError):
            return "경계 숫자 필요"
        if abs(e[0]) > 1e-6 or abs(e[-1] - total) > 0.5:
            return "경계는 0 에서 %g mm(판 크기)까지" % total
        if any(b - a < 1.0 for a, b in zip(e, e[1:])):
            return "경계는 1 mm 이상 간격으로 증가해야 함"
    return None
