"""regress.py - 지금까지 찍은 사진 전부로 검출 회귀를 본다.

data/out 아래에서 raw.png 가 있는 폴더를 모두 찾아 다시 검출하고, 그 폴더에
저장돼 있는 값(samples.json 또는 result.json)과 비교한다.

  python tools/regress.py            # 표 출력. 개수가 줄어든 사진이 있으면 종료코드 1
  python tools/regress.py --update   # 지금 결과를 그 폴더의 저장값으로 갱신

마커가 4개 보이는 사진은 calib.sense 와 같은 경로(마커 사각형으로 잘라 검출,
기계좌표 mm)로, 아니면 전체 프레임으로 돌린다(웨이퍼 중심 mm).
검출 로직을 고칠 때마다 이걸 돌려서 '있던 샘플이 사라지지 않았는지' 를 본다.
"""

import argparse
import io as _io
import json
import os
import sys
from contextlib import redirect_stdout

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import calib                                             # noqa: E402
from core import detect                                            # noqa: E402
from core import imgio                                             # noqa: E402
from core import paths                                             # noqa: E402

MATCH_MM = 8.0          # 이 안에 있으면 같은 샘플로 본다
MATCH_PX = 30.0


def find_dirs(root):
    out = []
    for name in sorted(os.listdir(root)):
        d = os.path.join(root, name)
        if os.path.isdir(d) and os.path.exists(os.path.join(d, "raw.png")):
            out.append(d)
    return out


def load_saved(d):
    """저장된 좌표 목록 -> [(no, x, y)] 와 단위."""
    p = os.path.join(d, "samples.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            arr = json.load(f)
        return [(s["no"], float(s["X"]), float(s["Y"])) for s in arr], "mm"
    p = os.path.join(d, "result.json")
    if os.path.exists(p):
        with open(p, "r", encoding="utf-8") as f:
            r = json.load(f)
        return ([(s["no"], float(s["x_mm"]), float(s["y_mm"]))
                 for s in r.get("samples", [])], "mm")
    return None, None


def run_one(d, params):
    """다시 검출한다. (rows[(no,x,y)], 단위, 경로설명)"""
    bgr = imgio.imread_u(os.path.join(d, "raw.png"))
    if bgr is None:
        return None, None, "읽기 실패"
    buf = _io.StringIO()
    with redirect_stdout(buf):                  # sense 가 찍는 진행 문구는 감춘다
        mk = calib.fit_from_image(bgr, params)
        if mk is not None:                      # 마커 3개 이상 = calib 과 같은 경로
            res = calib.sense(bgr, params, save=False)
            if res is None:
                return None, None, "sense 실패"
            return [(no, x, y) for no, _u, _v, x, y in res["rows"]], "mm", "marker"
        det = detect.detect(bgr, params)        # main.py 와 같은 전체 프레임 경로
        return ([(s["no"], float(s["x_mm"]), float(s["y_mm"])) for s in det.samples],
                "mm", "frame")


def compare(old, new, unit):
    """가장 가까운 것끼리 짝지어 (짝목록, 새로생김, 사라짐)."""
    lim = MATCH_MM if unit == "mm" else MATCH_PX
    left = list(new)
    pairs, gone = [], []
    for no, x, y in old:
        best, bi = None, -1
        for i, (n2, x2, y2) in enumerate(left):
            dd = ((x - x2) ** 2 + (y - y2) ** 2) ** 0.5
            if best is None or dd < best:
                best, bi = dd, i
        if bi >= 0 and best <= lim:
            pairs.append((no, left[bi][0], best))
            left.pop(bi)
        else:
            gone.append(no)
    return pairs, [n for n, _x, _y in left], gone


def main(argv=None):
    ap = argparse.ArgumentParser(description="검출 회귀 비교")
    ap.add_argument("--update", action="store_true", help="지금 결과로 저장값 갱신")
    ap.add_argument("--dir", help="이 폴더만 (기본: data/out 전부)")
    a = ap.parse_args(argv)

    params = calib.load_params()
    dirs = [a.dir] if a.dir else find_dirs(paths.OUT_DIR)
    if not dirs:
        print("raw.png 이 있는 폴더가 없습니다: %s" % paths.OUT_DIR)
        return 2

    print("%-24s %-7s %5s %5s %8s %8s  %s"
          % ("folder", "path", "old", "new", "mean", "max", "note"))
    worse = 0
    for d in dirs:
        old, unit = load_saved(d)
        new, unit2, how = run_one(d, params)
        name = os.path.basename(d)
        if new is None:
            print("%-24s %-7s %5s %5s %8s %8s  %s" % (name, "-", "-", "-", "-", "-", how))
            continue
        if old is None:
            print("%-24s %-7s %5s %5d %8s %8s  %s"
                  % (name, how, "-", len(new), "-", "-", "저장값 없음"))
            old = []
        if os.path.exists(os.path.join(d, "samples.json")) and how == "frame":
            # 저장값은 기계좌표(mm)인데 마커를 못 찾아 웨이퍼 중심 좌표로 나왔다.
            # 좌표계가 달라 거리 비교는 무의미하므로 개수만 본다.
            note = "좌표계 다름(개수만 비교)"
            if len(new) < len(old):
                worse += 1
                note += " 개수 감소"
            print("%-24s %-7s %5d %5d %8s %8s  %s"
                  % (name, how, len(old), len(new), "-", "-", note))
            continue
        pairs, added, gone = compare(old, new, unit2)
        ds = [p[2] for p in pairs]
        mean = sum(ds) / len(ds) if ds else 0.0
        mx = max(ds) if ds else 0.0
        note = []
        if gone:
            note.append("사라짐 %s" % gone)
        if added:
            note.append("새로생김 %s" % added)
        if len(new) < len(old):
            worse += 1
            note.append("개수 감소")
        print("%-24s %-7s %5d %5d %8.2f %8.2f  %s"
              % (name, how, len(old), len(new), mean, mx, " ".join(note)))

        if a.update:
            p = os.path.join(d, "samples.json")
            if os.path.exists(p):
                with open(p, "r", encoding="utf-8") as f:
                    arr = json.load(f)
                by = {s["no"]: s for s in arr}
                out = []
                for no, x, y in new:
                    s = dict(by.get(no, {}))
                    s.update({"no": no, "X": round(x, 2), "Y": round(y, 2)})
                    out.append(s)
                with open(p, "w", encoding="utf-8") as f:
                    json.dump(out, f, ensure_ascii=False, indent=2)
            else:
                p = os.path.join(d, "result.json")
                if os.path.exists(p):
                    with open(p, "r", encoding="utf-8") as f:
                        r = json.load(f)
                    r["sample_count"] = len(new)
                    for s, (no, x, y) in zip(r.get("samples", []), new):
                        s["no"], s["x_mm"], s["y_mm"] = no, round(x, 2), round(y, 2)
                    with open(p, "w", encoding="utf-8") as f:
                        json.dump(r, f, ensure_ascii=False, indent=2)

    if worse:
        print("")
        print("개수가 줄어든 사진 %d장 - 회귀입니다." % worse)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
