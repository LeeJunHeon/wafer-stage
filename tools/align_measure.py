"""align_measure.py - 정렬 잔차 문턱 실측. truth 항목마다 바른 정렬 · 일부러 틀린 정렬(10 / 20 / 40 px) 의 잔차를 출력한다.

  python tools/align_measure.py [--truth DIR]      (기본 ../data_v2/truth)
"""
import argparse
import glob
import json
import os
import sys

import numpy as np

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))
from core import align, imgio  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--truth", default=os.path.join(os.path.dirname(align.__file__), "..", "..", "data_v2", "truth"))
    a = ap.parse_args()
    tdir = os.path.normpath(a.truth)
    good, bad = [], {10: [], 20: [], 40: []}
    print("%-26s %-8s %-6s %-6s %-6s %s" % ("항목", "모델", "바른", "유클리드", "원근", "틀림 10/20/40 px"))
    for f in sorted(glob.glob(os.path.join(tdir, "*.json"))):
        for name, it in json.load(open(f, encoding="utf-8")).items():
            if not it.get("ref"):
                continue
            ref = imgio.imread_u(os.path.join(tdir, it["ref"]))
            cur = imgio.imread_u(os.path.join(tdir, it["image"]))
            r = align.align(ref, cur, it["area"])
            h, w = ref.shape[:2]
            ha = align._hp(__import__("cv2").resize(__import__("cv2").cvtColor(ref, 6), None, fx=0.25, fy=0.25))
            hb = align._hp(__import__("cv2").resize(__import__("cv2").cvtColor(cur, 6), None, fx=0.25, fy=0.25))
            mask = align.area_mask(ha.shape, it["area"], 0.25)
            S = np.diag([0.25, 0.25, 1.0])
            Ms = S @ r["A"].M @ np.linalg.inv(S)
            wrong = []
            for px in (10, 20, 40):
                vals = []
                for ang in (0, 90, 180, 270):
                    T = np.eye(3)
                    T[0, 2] = px * 0.25 * np.cos(np.radians(ang))
                    T[1, 2] = px * 0.25 * np.sin(np.radians(ang))
                    vals.append(align.residual(ha, hb, T @ Ms, mask))
                bad[px].append(min(vals))
                wrong.append("%.2f" % min(vals))
            good.append(r["resid"])
            print("%-26s %-8s %-6.2f %-6.2f %-6s %s" % (name[:26], r["A"].model, r["resid"], r["resid_euclid"],
                  "-" if r["resid_homography"] is None else "%.2f" % r["resid_homography"], " / ".join(wrong)))
    if good:
        print("\n바른 정렬 %.2f ~ %.2f" % (min(good), max(good)))
        for px, v in bad.items():
            print("틀림 %2d px   %.2f ~ %.2f" % (px, min(v), max(v)))


if __name__ == "__main__":
    main()
