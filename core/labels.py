"""labels.py - 샘플 번호 라벨 배치(겹침 회피) · 위치 순서 번호.

촬영본 표시는 번호 하나만 쓴다: 라벨은 칩 · 윤곽 · 다른 라벨과 겹치지 않게 칩 바깥 빈 자리에 두고 짧은 지시선으로 잇는다.
화면(js/camera.js placeLabels)과 저장용 annotated.jpg 가 같은 방식이다. 좌표 숫자는 촬영본에 쓰지 않는다(목록 · 툴팁에만).
번호는 새 촬영의 첫 번호 매김에만 위치 순서로 붙인다(편집 때는 다시 매기지 않는다).
"""

import math

import numpy as np


def _overlap(a, b):
    return min(a[2], b[2]) - max(a[0], b[0]) > 0 and min(a[3], b[3]) - max(a[1], b[1]) > 0


def _area(a, b):
    w = min(a[2], b[2]) - max(a[0], b[0])
    h = min(a[3], b[3]) - max(a[1], b[1])
    return max(0.0, w) * max(0.0, h)


def place(items, W, H, lab_w, lab_h, gap=6.0):
    """items = [{"bbox": (x0, y0, x1, y1)}, ...] (칩 윤곽 바깥 사각형). 반환: 같은 길이의 [{"box": (x0,y0,x1,y1), "from": (x,y), "to": (x,y)}].

    lab_w[i], lab_h[i] = 라벨 크기. 후보: 칩 둘레 바깥 8방향 × 거리 3단계. 칩 · 이미 놓은 라벨과 겹치지 않는 첫 후보,
    없으면 겹침이 가장 적은 후보. 지시선: 칩 사각형의 가까운 점 → 라벨 사각형의 가까운 점.
    """
    chips = [tuple(it["bbox"]) for it in items]
    placed = []
    out = []
    for i, it in enumerate(items):
        x0, y0, x1, y1 = chips[i]
        w, h = lab_w[i], lab_h[i]
        cx, cy = (x0 + x1) / 2.0, (y0 + y1) / 2.0
        cands = []
        for k in (1.0, 2.0, 3.2):
            g = gap * k
            cands += [
                (x1 + g, cy - h / 2.0), (x0 - g - w, cy - h / 2.0),                       # 오른쪽 · 왼쪽
                (cx - w / 2.0, y0 - g - h), (cx - w / 2.0, y1 + g),                        # 위 · 아래
                (x1 + g, y0 - g - h), (x0 - g - w, y0 - g - h),                            # 오른쪽 위 · 왼쪽 위
                (x1 + g, y1 + g), (x0 - g - w, y1 + g)]                                    # 오른쪽 아래 · 왼쪽 아래
        best, best_cost = None, None
        for (lx, ly) in cands:
            lx = min(max(lx, 2.0), max(2.0, W - w - 2.0))
            ly = min(max(ly, 2.0), max(2.0, H - h - 2.0))
            box = (lx, ly, lx + w, ly + h)
            cost = 0.0
            for j, c in enumerate(chips):
                infl = (c[0] - 2, c[1] - 2, c[2] + 2, c[3] + 2)
                cost += _area(box, infl) * (3.0 if j == i else 1.0)
            for p in placed:
                cost += _area(box, p) * 2.0
            if cost == 0.0:
                best, best_cost = box, 0.0
                break
            if best_cost is None or cost < best_cost:
                best, best_cost = box, cost
        placed.append(best)
        # 지시선: 칩 사각형 위의 가장 가까운 점 → 라벨 사각형 위의 가장 가까운 점
        lcx, lcy = (best[0] + best[2]) / 2.0, (best[1] + best[3]) / 2.0
        a = (min(max(lcx, x0), x1), min(max(lcy, y0), y1))
        b = (min(max(a[0], best[0]), best[2]), min(max(a[1], best[1]), best[3]))
        out.append({"box": best, "from": a, "to": b})
    return out


def row_major_order(pts, tol):
    """위치 순서(행 우선): 행 묶음 허용 오차 tol 로 y(=세로 좌표)를 묶고 행 안은 x 순. pts = [(x, y)] → 정렬된 인덱스."""
    idx = sorted(range(len(pts)), key=lambda i: pts[i][1])
    rows, cur, cur_y = [], [], None
    for i in idx:
        y = pts[i][1]
        if cur and abs(y - cur_y) > tol:
            rows.append(cur)
            cur = []
        cur.append(i)
        cur_y = float(np.mean([pts[k][1] for k in cur]))
    if cur:
        rows.append(cur)
    out = []
    for r in rows:
        out += sorted(r, key=lambda i: pts[i][0])
    return out


def grid_order(slots, uv_xy, rows_hint=None):
    """격자 인덱스 순서. slots = [(i, j)], uv_xy = (u 의 (x, y), v 의 (x, y)) 단위 간격 벡터(번호 기준 좌표계에서).
    세로 성분이 큰 축을 '행'으로 삼고 행은 y 증가 순, 열은 x 증가 순으로 정렬한 인덱스."""
    u, v = uv_xy
    row_is_v = abs(v[1]) >= abs(u[1])
    ra, ca = (v, u) if row_is_v else (u, v)
    rs = 1.0 if ra[1] >= 0 else -1.0          # 행 인덱스가 커질수록 y 가 커지게
    cs = 1.0 if ca[0] >= 0 else -1.0          # 열 인덱스가 커질수록 x 가 커지게
    key = (lambda s: (rs * (s[1] if row_is_v else s[0]), cs * (s[0] if row_is_v else s[1])))
    return sorted(range(len(slots)), key=lambda k: key(slots[k]))
