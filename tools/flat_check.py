"""flat_check.py - 종이 기준 평탄화(core/flat.py)가 검출에 도움이 되는지 본다.

인자로 받은 raw.png(또는 폴더)마다 평탄화 전·후로 calib.sense 를 돌려 검출 개수,
보정 잔차, 종이 RGB, 포화 비율, 경고를 나란히 찍는다. 카메라 없이 돈다.

  python tools/flat_check.py ..\\data\\out\\seq_20260916_153305 [...]
  python tools/flat_check.py --all          # data/out 아래 raw.png 전부
  python tools/flat_check.py --save DIR ... # 평탄화 결과를 DIR 에 flat_<폴더>.png 로

검출 로직·임계값은 건드리지 않는다. 전처리만 붙였다 뗐다 하며 비교한다.
"""

import argparse
import io as _io
import json
import math
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import calib                                             # noqa: E402
from core import flat                                              # noqa: E402
from core import imgio                                             # noqa: E402
from core import paths                                             # noqa: E402


def _sense_quiet(bgr, params):
    buf = _io.StringIO()
    with redirect_stdout(buf):
        res = calib.sense(bgr, params, allow_fallback=False, save=False)
    return res


def _summary(bgr, params, orig=None):
    """검출 한 번: (개수, rms, max, 웨이퍼 포화 %, 경고 목록) 또는 None.

    포화는 언제나 원본(orig)에서 잰다 - 평탄화는 값을 줄일 뿐 잃은 정보를 되살리지 않는다.
    """
    res = _sense_quiet(bgr, params)
    if res is None:
        return None
    cal, det = res["cal"], res["det"]
    w = det.wafer
    wsat = 0.0
    if w.found:
        u0, v0 = res["rect"][:2]
        wsat = flat.wafer_saturation_frac(
            orig if orig is not None else bgr, (w.center_px[0] + u0, w.center_px[1] + v0),
            w.major_px, w.minor_px, w.theta_deg) * 100.0
    return {"n": len(det.samples),
            "rows": [(no, x, y) for no, _u, _v, x, y in res["rows"]],
            "rows_px": [(no, u, v) for no, u, v, _x, _y in res["rows"]],
            "mm_per_px": float(det.wafer.mm_per_px or 0.0),
            "rms": float(cal.get("corner_rms_mm", cal.get("rms_mm", 0.0))),
            "max": float(cal.get("corner_max_mm", cal.get("max_mm", 0.0))),
            "wafer_sat": wsat,
            "warnings": list(det.warnings)}


def _mode_params(params, mode):
    """검출 경로 스위치. both = 통계+엣지 제안, stat = 통계만, edge = 엣지 제안만."""
    q = dict(params)
    if mode == "stat":
        q["edge_propose"] = False
    elif mode == "edge":
        q["stat_seed"] = 1e12          # 씨앗이 없으면 통계 경로는 아무것도 내지 않는다
        q["stat_grow"] = 1e12
    return q


TRUTH_MM = 2.5          # 정답 좌표에서 이 안이면 적중


def load_truth(path):
    """samples.json -> {"mm": [(no, X, Y)], "px": [(no, u, v)] 또는 None}."""
    with open(path, "r", encoding="utf-8") as f:
        arr = json.load(f)
    mm = [(s["no"], float(s["X"]), float(s["Y"])) for s in arr]
    px = ([(s["no"], float(s["u"]), float(s["v"])) for s in arr]
          if all("u" in s and "v" in s for s in arr) else None)
    return {"mm": mm, "px": px}


def score(rows, truth, tol=TRUTH_MM):
    """(적중 목록, 오검출 목록, 놓친 정답 목록). 가까운 것부터 짝짓는다.

    같은 사진의 정답이면 픽셀로 비교한다(tol 은 2.5mm 를 px 로 환산) - 촬영마다
    마커 재보정이 달라 기계좌표는 같은 칩도 몇 mm 씩 어긋난다.
    """
    pairs = sorted((math.hypot(x - tx, y - ty), i, j)
                   for i, (_n, x, y) in enumerate(rows)
                   for j, (_tn, tx, ty) in enumerate(truth))
    used_d, used_t, hits = set(), set(), []
    for d, i, j in pairs:
        if d > tol:
            break
        if i in used_d or j in used_t:
            continue
        used_d.add(i); used_t.add(j)
        hits.append((rows[i][0], truth[j][0], d))
    false = [rows[i] for i in range(len(rows)) if i not in used_d]
    missed = [truth[j] for j in range(len(truth)) if j not in used_t]
    return hits, false, missed


def _source_of(p):
    """폴더(또는 그 안의 어떤 파일)를 주면 앱이 실제로 검출한 원천을 고른다:
    fused.png(브라케팅 융합본)가 있으면 그것, 없으면 raw.png. flat.png 는 입력으로 쓰지
    않는다 - 앱이 다시 평탄화해서(이중 평탄화) 결과가 달라진다(143454: 14/16 vs 15/16)."""
    d = p if os.path.isdir(p) else os.path.dirname(p)
    for name in ("fused.png", "raw.png"):
        q = os.path.join(d, name)
        if os.path.exists(q):
            return q
    return p


def check_one(path, params, save_dir=None, modes=("both",), truth=None):
    bgr = imgio.imread_u(path)
    if bgr is None:
        print("%s: 읽기 실패" % path)
        return None
    name = os.path.basename(os.path.dirname(path)) or os.path.basename(path)
    p_off = dict(params, flat_field=False)
    p_on = dict(params, flat_field=True)
    img1, info1 = calib.preprocess(bgr, p_on)
    rect = info1["rect"]
    before = _summary(bgr, p_off)
    after = _summary(img1, p_on, orig=bgr) if info1["flat"]["applied"] else None
    fi = info1["flat"]
    # 경로별 기여: 평탄화본에서 통계만 / 엣지 제안만 돌려 개수를 나란히 본다.
    by_mode = {}
    for m in modes:
        if m == "both":
            continue
        r = _summary(img1 if fi["applied"] else bgr, _mode_params(p_on, m), orig=bgr)
        by_mode[m] = None if r is None else r["n"]

    print("=" * 78)
    print("%s   마커 %d개   감지영역 %s" % (name, info1["n_markers"], rect))
    print("  종이 %.0f%%  RGB 전 %s -> 후 %s   %s"
          % (fi.get("paper_frac", 0) * 100, fi.get("paper_rgb_before"),
             fi.get("paper_rgb_after"), fi.get("warning") or "평탄화 적용"))
    if rect is not None:
        print("  감지영역 포화(원본) %.1f%%" % (flat.saturation_frac(bgr, rect) * 100))
    for tag, s in (("전", before), ("후", after)):
        if s is None:
            print("  [%s] 검출 못 함(마커 부족 또는 평탄화 건너뜀)" % tag)
            continue
        print("  [%s] 검출 %2d개  보정 RMS %.2f 최대 %.2f mm  웨이퍼 포화 %.1f%%"
              % (tag, s["n"], s["rms"], s["max"], s["wafer_sat"]))
        for wmsg in s["warnings"]:
            print("       경고: %s" % wmsg)
    if by_mode:
        print("  경로별(평탄화 후): " + " · ".join("%s %s" % ({"stat": "통계만", "edge": "엣지만"}[m], n)
                                           for m, n in by_mode.items()))
    if truth is not None:
        for tag, s in (("전", before), ("후", after)):
            if s is None:
                continue
            if truth.get("px") and truth.get("same") and s.get("mm_per_px"):
                hits, false, missed = score(s["rows_px"], truth["px"],
                                            tol=TRUTH_MM / s["mm_per_px"])
                unit = "px"
            else:
                hits, false, missed = score(s["rows"], truth["mm"])
                unit = "mm"
            print("  [%s] 정답 %d(%s): 적중 %d · 오검출 %d %s · 놓침 %d %s"
                  % (tag, len(truth["mm"]), unit, len(hits), len(false),
                     [("#%d" % n, round(x, 1), round(y, 1)) for n, x, y in false],
                     len(missed), [("#%d" % n, round(x, 1), round(y, 1)) for n, x, y in missed]))
    if save_dir and info1["flat"]["applied"]:
        os.makedirs(save_dir, exist_ok=True)
        imgio.imwrite_u(os.path.join(save_dir, "flat_%s.png" % name), img1)
    return before, after


def main(argv=None):
    ap = argparse.ArgumentParser(description="평탄화 전후 검출 비교")
    ap.add_argument("paths", nargs="*", help="raw.png 또는 그 폴더")
    ap.add_argument("--all", action="store_true", help="data/out 아래 전부")
    ap.add_argument("--save", help="평탄화 결과 PNG 를 이 폴더에 남긴다")
    ap.add_argument("--truth", help="정답 samples.json. 'self' 면 그 사진 폴더의 samples.json")
    ap.add_argument("--mode", default="both",
                    help="both(기본) · stat(통계 경로만) · edge(엣지 제안만) · all(셋 다 나란히)")
    a = ap.parse_args(argv)

    files = []
    if a.all:
        for n in sorted(os.listdir(paths.OUT_DIR)):
            p = _source_of(os.path.join(paths.OUT_DIR, n))
            if p and os.path.exists(p):
                files.append(p)
    for p in a.paths:
        files.append(_source_of(p))
    if not files:
        ap.print_help()
        return 2

    params = calib.load_params()
    modes = {"both": ("both",), "stat": ("stat",), "edge": ("edge",),
             "all": ("both", "stat", "edge")}.get(a.mode, ("both",))
    if a.mode in ("stat", "edge"):
        params = _mode_params(params, a.mode)
    worse = 0
    rows = []
    for f in files:
        truth = None
        if a.truth:
            tp = os.path.join(os.path.dirname(f), "samples.json") if a.truth == "self" else a.truth
            truth = load_truth(tp) if os.path.exists(tp) else None
            if truth is not None:
                # 같은 사진 폴더의 정답이면 px 로, 다른 촬영의 정답이면 기계좌표(mm)로 비교
                # (촬영 사이에 카메라·웨이퍼 픽셀 위치는 바뀌어도 기계좌표는 같다).
                truth["same"] = (os.path.dirname(os.path.abspath(tp))
                                 == os.path.dirname(os.path.abspath(f)))
        r = check_one(f, params, a.save, modes, truth)
        if r is None:
            continue
        before, after = r
        nb = before["n"] if before else None
        na = after["n"] if after else None
        rows.append((os.path.basename(os.path.dirname(f)), nb, na))
        if nb is not None and na is not None and na < nb:
            worse += 1
    print("=" * 78)
    print("%-28s %5s %5s" % ("folder", "전", "후"))
    for name, nb, na in rows:
        print("%-28s %5s %5s%s" % (name, "-" if nb is None else nb, "-" if na is None else na,
                                   "  감소" if (nb is not None and na is not None and na < nb) else ""))
    print("검출 개수가 줄어든 사진: %d" % worse)
    return 1 if worse else 0


if __name__ == "__main__":
    sys.exit(main())
