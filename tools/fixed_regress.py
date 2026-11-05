"""fixed_regress.py - 고정 환경 검출기(core/fixed_detect.py) 회귀 · 튜닝 도구.

data/truth/*.json 의 항목(키 = 폴더명, image, size, samples[{u,v,how}], area, mm_per_px, chip_long_px,
config = ceiling | motor)을 fixed_detect 로 돌려 정답과 대조한다. 기본은 config=ceiling 만 채점,
--all 이면 전부. 감지 영역은 항목의 area, 배율은 항목의 mm_per_px.

  python tools/fixed_regress.py              # 표 출력, 기준(tools/fixed_regress_base.json)보다 나빠지면 종료코드 1
  python tools/fixed_regress.py --update     # 지금 결과를 기준으로 고정
  python tools/fixed_regress.py --loo        # 한 장 빼고 맞춘 값으로 뺀 장을 채점(과적합 확인)
  python tools/fixed_regress.py --set fixed.seed=4 --set fixed.min_fill=0.5   # 파라미터 덮어쓰기
  python tools/fixed_regress.py --dir DIR    # 정답 폴더(기본 ../data/truth)와 사진 폴더 부모(기본 ../data/out)

매칭: 정답과 min(2.5 mm, 칩 긴 변 x 0.35) 안에서 가까운 순 1:1. source 별(detected · local · predicted) 적중을 센다.
"""

import argparse
import glob
import itertools
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

import cv2          # noqa: E402
import numpy as np  # noqa: E402

from core import fixed_detect as fd, imgio, plate  # noqa: E402

BASE = os.path.join(ROOT, "tools", "fixed_regress_base.json")


def _data_root():
    from core import paths
    return os.path.dirname(paths.OUT_DIR)


def load_truth(tdir):
    items = {}
    for f in sorted(glob.glob(os.path.join(tdir, "*.json"))):
        with open(f, encoding="utf-8") as fh:
            d = json.load(fh)
        if not isinstance(d, dict):
            continue
        for k, v in d.items():
            if isinstance(v, dict) and "samples" in v:
                items[k] = v
    return items


def params_with(sets):
    sys.path.insert(0, os.path.join(ROOT, "backend"))
    import settings_schema as sch
    p = sch.defaults()
    for kv in sets or []:
        k, _, v = kv.partition("=")
        val = json.loads(v)
        cur = p
        parts = k.split(".")
        for q in parts[:-1]:
            cur = cur.setdefault(q, {})
        cur[parts[-1]] = val
    return p


def run_item(name, item, params, out_root):
    path = os.path.join(out_root, name, item["image"])
    bgr = imgio.imread_u(path)
    if bgr is None:
        raise IOError("사진 없음: %s" % path)
    crop, valid, (u0, v0), quad_l = plate.prep_crop(bgr, item["area"], item.get("exclude"))
    t0 = time.perf_counter()
    res = fd.detect(crop, params, float(item["mm_per_px"]), valid, area_poly=quad_l, log=lambda *_: None)
    ms = (time.perf_counter() - t0) * 1000.0
    pts = [(s["x_px"] + u0, s["y_px"] + v0, s["source"]) for s in res.samples]
    return pts, ms, res


def match(truth_pts, det_pts, tol):
    """가까운 순 1:1. 반환: [(truth_idx, det_idx)]."""
    pairs = sorted(((np.hypot(t[0] - d[0], t[1] - d[1]), i, j)
                    for i, t in enumerate(truth_pts) for j, d in enumerate(det_pts)), key=lambda x: x[0])
    ut, ud, out = set(), set(), []
    for dist, i, j in pairs:
        if dist > tol:
            break
        if i in ut or j in ud:
            continue
        ut.add(i)
        ud.add(j)
        out.append((i, j))
    return out


def score(name, item, pts):
    tr = [(s["u"], s["v"]) for s in item["samples"]]
    tol = min(2.5 / item["mm_per_px"], 0.35 * item.get("chip_long_px", 1e9))
    m = match(tr, pts, tol)
    hit = len(m)
    src = {"detected": 0, "local": 0, "predicted": 0}
    for _i, j in m:
        src[pts[j][2]] += 1
    miss = len(tr) - hit
    fp = len(pts) - hit
    nonpred = sum(1 for p in pts if p[2] != "predicted")
    nonpred_hit = src["detected"] + src["local"]
    return {"hit": hit, "miss": miss, "fp": fp, "n": len(tr), "src": src, "n_out": len(pts),
            "nonpred_hit": nonpred_hit, "nonpred_out": nonpred}


def evaluate(items, params, out_root, verbose=True):
    rows = {}
    for name, item in items.items():
        pts, ms, _res = run_item(name, item, params, out_root)
        r = score(name, item, pts)
        r["ms"] = round(ms)
        rows[name] = r
    return rows


def print_table(rows):
    print("%-28s %5s %5s %5s | %5s %5s %5s | 비예측 %s %s | %6s" %
          ("사진", "적중", "놓침", "오검출", "검출", "보완", "예측", "적중", "출력", "ms"))
    tot = {"hit": 0, "miss": 0, "fp": 0, "n": 0, "d": 0, "l": 0, "p": 0, "nh": 0, "no": 0}
    for name, r in rows.items():
        print("%-28s %2d/%-2d %5d %5d | %5d %5d %5d |      %2d  %2d | %6d" %
              (name, r["hit"], r["n"], r["miss"], r["fp"], r["src"]["detected"], r["src"]["local"],
               r["src"]["predicted"], r["nonpred_hit"], r["nonpred_out"], r["ms"]))
        tot["hit"] += r["hit"]; tot["miss"] += r["miss"]; tot["fp"] += r["fp"]; tot["n"] += r["n"]
        tot["d"] += r["src"]["detected"]; tot["l"] += r["src"]["local"]; tot["p"] += r["src"]["predicted"]
        tot["nh"] += r["nonpred_hit"]; tot["no"] += r["nonpred_out"]
    print("%-28s %2d/%-2d %5d %5d | %5d %5d %5d |      %2d  %2d |" %
          ("전체", tot["hit"], tot["n"], tot["miss"], tot["fp"], tot["d"], tot["l"], tot["p"], tot["nh"], tot["no"]))
    return tot


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true", help="config=motor 포함 전부 채점")
    ap.add_argument("--update", action="store_true")
    ap.add_argument("--loo", action="store_true")
    ap.add_argument("--set", action="append", default=[], help="fixed.seed=4.5 형식")
    ap.add_argument("--dir", default=None)
    a = ap.parse_args()
    root = _data_root()
    tdir = a.dir or os.path.join(root, "truth")
    out_root = os.path.join(root, "out")
    items = load_truth(tdir)
    if not a.all:
        items = {k: v for k, v in items.items() if v.get("config", "ceiling") == "ceiling"}
    if not items:
        print("정답 항목 없음: %s" % tdir)
        return 2
    params = params_with(a.set)
    if a.loo:
        return loo(items, params, out_root)
    rows = evaluate(items, params, out_root)
    tot = print_table(rows)
    if a.update:
        with open(BASE, "w", encoding="utf-8") as f:
            json.dump({k: {x: r[x] for x in ("hit", "miss", "fp", "nonpred_hit")} for k, r in rows.items()}, f, ensure_ascii=False, indent=1)
        print("기준 고정: %s" % BASE)
        return 0
    if os.path.exists(BASE):
        with open(BASE, encoding="utf-8") as f:
            base = json.load(f)
        worse = []
        for k, r in rows.items():
            b = base.get(k)
            if b and (r["hit"] < b["hit"] or r["fp"] > b["fp"] or r["nonpred_hit"] < b.get("nonpred_hit", 0)):
                worse.append("%s: 적중 %d→%d · 오검출 %d→%d · 비예측 적중 %d→%d"
                             % (k, b["hit"], r["hit"], b["fp"], r["fp"], b.get("nonpred_hit", 0), r["nonpred_hit"]))
        if worse:
            print("\n기준보다 나빠짐:\n  " + "\n  ".join(worse))
            return 1
        print("\n기준과 같거나 좋아짐")
    return 0


# 한 장 빼고 맞춘 값 -----------------------------------------------------------
GRID = {
    "fixed.seed": [3.5, 4.0, 4.5, 5.0, 5.5],
    "fixed.min_fill": [0.40, 0.45, 0.55],
    "fixed.min_solidity": [0.70, 0.75, 0.80],
}


def _apply(params, combo):
    p = json.loads(json.dumps(params))
    for k, v in combo.items():
        cur = p
        parts = k.split(".")
        for q in parts[:-1]:
            cur = cur.setdefault(q, {})
        cur[parts[-1]] = v
    return p


def loo(items, params, out_root):
    """각 장을 빼고 나머지로 격자 탐색 → 그 값으로 뺀 장 채점. 기본값(전체로 맞춘 값)과 비교한다."""
    keys = list(GRID)
    combos = [dict(zip(keys, vals)) for vals in itertools.product(*[GRID[k] for k in keys])]
    cache = {}

    def sc(name, combo):
        ck = (name, tuple(sorted(combo.items())))
        if ck not in cache:
            pts, _ms, _r = run_item(name, items[name], _apply(params, combo), out_root)
            r = score(name, items[name], pts)
            cache[ck] = r["hit"] * 10 - r["fp"] * 20 + r["nonpred_hit"]
        return cache[ck]

    print("%-28s | 뺀 장 점수(맞춘 값) | 전체 기본값 점수 | 선택된 값" % "뺀 장")
    ok = True
    for held in items:
        rest = [k for k in items if k != held]
        best = max(combos, key=lambda c: (sum(sc(k, c) for k in rest), -sum(abs(c[k] - params["fixed"][k.split(".")[1]]) for k in keys)))
        pts, _ms, _r = run_item(held, items[held], _apply(params, best), out_root)
        r = score(held, items[held], pts)
        pts0, _ms0, _r0 = run_item(held, items[held], params, out_root)
        r0 = score(held, items[held], pts0)
        print("%-28s | 적중 %2d 오검출 %d | 적중 %2d 오검출 %d | %s" %
              (held, r["hit"], r["fp"], r0["hit"], r0["fp"], best))
        if r["hit"] < r0["hit"] - 1 or r["fp"] > r0["fp"] + 1:
            ok = False
    print("LOO 결과: %s" % ("기본값과 비슷(과적합 징후 없음)" if ok else "뺀 장에서 기본값보다 크게 나빠짐(과적합 징후)"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
