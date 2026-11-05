"""make_truth.py - 앱에서 샘플 편집(추가 · 삭제)으로 고친 촬영을 정답 파일로 내보낸다.

  python tools/make_truth.py data/out/seq_20261105_150000 [더 많은 폴더 ...]
  python tools/make_truth.py DIR --config motor          # 모터 쪽 배치 등 참고용(회귀는 ceiling 만 채점)
  python tools/make_truth.py DIR --plate PATH            # 감지 영역을 읽을 plate_calib.json(기본: 데이터 폴더의 것)

폴더의 samples_edit.json(앱의 편집본 - 없으면 검출 원본 samples.json)을 읽어 data/truth/<폴더명>.json 에
쓴다. fixed_regress.py 가 이 폴더의 *.json 을 전부 읽는다. 형식(항목 키 = 폴더명):
  {"<폴더명>": {"image": "fused.png|raw.png", "size": [w, h], "samples": [{"u", "v", "how"}],
                "area": [[u,v] x4], "mm_per_px": 근사, "chip_long_px": 근사, "config": "ceiling"|"motor",
                "note": ...}}
area 는 그 촬영 때 저장돼 있던 plate_calib.json 의 감지 영역(해상도가 같을 때), mm_per_px 는 판 보정(H)의
국소 배율 - 없으면 설정 calib.est_mm_per_px. how = "edited"(편집본) | "detected"(검출 원본).
"""

import argparse
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

from core import imgio, paths, plate  # noqa: E402


def _est_mm_px():
    try:
        import settings_schema
        return float(settings_schema.defaults()["calib"]["est_mm_per_px"])
    except Exception:          # noqa: BLE001
        return 0.143


def build(folder, plate_path, config, chip_mm):
    name = os.path.basename(os.path.normpath(folder))
    edit = os.path.join(folder, "samples_edit.json")
    orig = os.path.join(folder, "samples.json")
    src, how = (edit, "edited") if os.path.exists(edit) else (orig, "detected")
    if not os.path.exists(src):
        raise SystemExit("samples_edit.json / samples.json 없음: %s" % folder)
    with open(src, encoding="utf-8") as f:
        rows = json.load(f)
    img_name = "fused.png" if os.path.exists(os.path.join(folder, "fused.png")) else "raw.png"
    bgr = imgio.imread_u(os.path.join(folder, img_name))
    if bgr is None:
        raise SystemExit("사진을 열 수 없습니다: %s/%s" % (folder, img_name))
    size = [int(bgr.shape[1]), int(bgr.shape[0])]
    d = plate.load(plate_path)
    if not d or not d.get("area") or [int(x) for x in d.get("size") or []] != size:
        raise SystemExit("감지 영역이 없거나 해상도가 다릅니다(%s) · --plate 로 그 촬영 때의 plate_calib.json 을 주세요" % (d and d.get("size")))
    import numpy as np
    mm_px = plate.local_mm_per_px(d["H"], np.asarray(d["area"], np.float64).mean(axis=0)) if d.get("H") is not None else _est_mm_px()
    samples = [{"u": round(float(r["u"]), 1), "v": round(float(r["v"]), 1), "how": r.get("source") or how}
               for r in rows if r.get("u") is not None]
    return name, {"image": img_name, "size": size, "samples": samples,
                  "area": [[round(float(a), 1), round(float(b), 1)] for a, b in d["area"]],
                  "mm_per_px": round(float(mm_px), 5), "chip_long_px": round(chip_mm / mm_px, 1),
                  "config": config,
                  "note": "make_truth.py · %s · 샘플 %d개 · 편집본 우선" % (os.path.basename(src), len(samples))}


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("folders", nargs="+")
    ap.add_argument("--config", default="ceiling", choices=["ceiling", "motor"])
    ap.add_argument("--plate", default=None)
    ap.add_argument("--chip-mm", type=float, default=11.0, help="칩 긴 변 근사(mm). 채점 허용 거리 계산용")
    ap.add_argument("--out", default=None, help="출력 폴더(기본: 데이터 폴더의 truth)")
    a = ap.parse_args()
    out_dir = a.out or os.path.join(os.path.dirname(paths.OUT_DIR), "truth")
    os.makedirs(out_dir, exist_ok=True)
    for folder in a.folders:
        name, item = build(folder, a.plate, a.config, a.chip_mm)
        p = os.path.join(out_dir, name + ".json")
        with open(p, "w", encoding="utf-8") as f:
            json.dump({name: item}, f, ensure_ascii=False, indent=1)
        print("정답 저장: %s (%d개 · %s)" % (p, len(item["samples"]), item["config"]))


if __name__ == "__main__":
    main()
