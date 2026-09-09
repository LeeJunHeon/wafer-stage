"""report.py - 결과 저장.

저장 위치는 data 폴더다 (paths.py 참고). 호출자가 이미 실제 경로로 풀어서
넘겨주므로 여기서는 받은 경로에 그대로 쓴다.

촬영 한 번마다 out/<타임스탬프>/ 폴더 하나를 만들고, 추가로 out/log.jsonl 에
한 줄을 append 한다. log.jsonl 한 줄에는 축소 JPEG 이 base64 로 들어 있어서
그 파일 하나만 있으면 그림까지 확인할 수 있다.
"""

import base64
import csv
import json
import os
import time

import cv2

import imgio

CSV_COLS = ["no", "shape", "x_mm", "y_mm", "x_px", "y_px", "w_mm", "h_mm",
            "angle_deg", "area_mm2", "solidity", "mean_dev_pct",
            "edge", "rescued", "split", "merged", "color_only"]


def timestamp_dir(out_dir):
    d = os.path.join(out_dir, time.strftime("%Y%m%d_%H%M%S"))
    if os.path.exists(d):                       # 같은 초에 두 번 찍는 경우
        for i in range(2, 100):
            c = "%s_%d" % (d, i)
            if not os.path.exists(c):
                d = c
                break
    os.makedirs(d, exist_ok=True)
    return d


def build_result(note, image_shape, cam_info, cap_stats, timing_ms, params,
                 det, extra_warnings=()):
    from detect import samples_public
    h, w = image_shape[:2]
    warnings = list(extra_warnings) + list(det.warnings)
    seen, uniq = set(), []
    for x in warnings:
        if x not in seen:
            seen.add(x)
            uniq.append(x)
    return {
        "version": "1.0",
        "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        "note": note or "",
        "image": {"width": int(w), "height": int(h)},
        "camera": cam_info,
        "capture": cap_stats,
        "timing_ms": timing_ms,
        "params": params,
        "wafer": det.wafer.as_dict(),
        "markers": list(det.markers),
        "sample_count": len(det.samples),
        "samples": samples_public(det.samples),
        "warnings": uniq,
        "info": list(det.info),
    }


def _jpeg_b64(bgr, width=1100, quality=85):
    h, w = bgr.shape[:2]
    if w > width:
        bgr = cv2.resize(bgr, (width, max(1, int(round(h * width / w)))),
                         interpolation=cv2.INTER_AREA)
    ok, buf = cv2.imencode(".jpg", bgr, [int(cv2.IMWRITE_JPEG_QUALITY), quality])
    if not ok:
        return None
    return base64.b64encode(buf.tobytes()).decode("ascii")


def save(out_dir, result, raw, annotated, det):
    d = timestamp_dir(out_dir)
    imgio.imwrite_u(os.path.join(d, "raw.png"), raw)                 # 보정 전 원본이 제일 중요
    imgio.imwrite_u(os.path.join(d, "annotated.png"), annotated)
    if det.mask is not None:
        imgio.imwrite_u(os.path.join(d, "mask.png"), det.mask)
    if det.dev_img is not None:
        imgio.imwrite_u(os.path.join(d, "deviation.png"), det.dev_img)
    if det.wafer_mask is not None:
        imgio.imwrite_u(os.path.join(d, "wafer_mask.png"), det.wafer_mask)

    with open(os.path.join(d, "result.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)

    with open(os.path.join(d, "samples.csv"), "w", encoding="utf-8-sig", newline="") as f:
        wr = csv.DictWriter(f, fieldnames=CSV_COLS)
        wr.writeheader()
        for s in result["samples"]:
            wr.writerow({k: s.get(k, "") for k in CSV_COLS})

    line = dict(result)
    line["dir"] = os.path.basename(d)
    line["raw_jpeg_b64"] = _jpeg_b64(raw)
    line["annotated_jpeg_b64"] = _jpeg_b64(annotated)
    os.makedirs(out_dir, exist_ok=True)
    with open(os.path.join(out_dir, "log.jsonl"), "a", encoding="utf-8") as f:
        f.write(json.dumps(line, ensure_ascii=False) + "\n")

    return d
