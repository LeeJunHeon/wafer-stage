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
            "rms": float(cal.get("corner_rms_mm", cal.get("rms_mm", 0.0))),
            "max": float(cal.get("corner_max_mm", cal.get("max_mm", 0.0))),
            "wafer_sat": wsat,
            "warnings": list(det.warnings)}


def check_one(path, params, save_dir=None):
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
    if save_dir and info1["flat"]["applied"]:
        os.makedirs(save_dir, exist_ok=True)
        imgio.imwrite_u(os.path.join(save_dir, "flat_%s.png" % name), img1)
    return before, after


def main(argv=None):
    ap = argparse.ArgumentParser(description="평탄화 전후 검출 비교")
    ap.add_argument("paths", nargs="*", help="raw.png 또는 그 폴더")
    ap.add_argument("--all", action="store_true", help="data/out 아래 전부")
    ap.add_argument("--save", help="평탄화 결과 PNG 를 이 폴더에 남긴다")
    a = ap.parse_args(argv)

    files = []
    if a.all:
        for n in sorted(os.listdir(paths.OUT_DIR)):
            p = os.path.join(paths.OUT_DIR, n, "raw.png")
            if os.path.exists(p):
                files.append(p)
    for p in a.paths:
        files.append(os.path.join(p, "raw.png") if os.path.isdir(p) else p)
    if not files:
        ap.print_help()
        return 2

    params = calib.load_params()
    worse = 0
    rows = []
    for f in files:
        r = check_one(f, params, a.save)
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
