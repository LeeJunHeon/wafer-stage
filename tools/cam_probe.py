"""cam_probe.py - 카메라가 실제로 무엇을 내보내는지 잰다 (카메라가 붙어 있어야 돈다).

USB 카메라는 못 하는 요청(해상도·포맷)을 조용히 가까운 값으로 바꿔 버리고, 표기
화소 수(예: 5000만)는 대개 보간이라 실제 정보가 늘지 않는다. 추측하지 말고 잰다.

  python tools/cam_probe.py                        # 기본 후보 전부
  python tools/cam_probe.py --index 1 --backend DSHOW
  python tools/cam_probe.py --fourcc MJPG --size 1280x720 --size 1920x1080
  python tools/cam_probe.py --frames 5

조합(fourcc x 해상도)마다:
  · 실제로 협상된 폭·높이·fourcc·fps 를 되읽어 요청과 다르면 표시한다.
  · 프레임을 몇 장 받아 한 장당 시간(초)과 focus_score(라플라시안 분산)를 잰다.
  · 실질 해상도 지표 "detail": 프레임을 1/2 로 줄였다 다시 키운 뒤 원본과의 RMS 차.
    보간으로 부풀린 사진은 0 에 가깝고 진짜 해상도면 뚜렷하게 크다. 같은 장면을
    연속으로 찍어 나란히 적는다 - 조명·장면이 바뀌면 비교가 무의미하다.
설정(settings.json)은 읽지도 쓰지도 않는다. 앱과 동시에 쓸 수 없다(장치 점유).
"""

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from core import camera                                            # noqa: E402  (cv2 로그를 먼저 끈다)

import cv2                                                         # noqa: E402
import numpy as np                                                 # noqa: E402

FOURCCS = ["MJPG", "YUY2"]
SIZES = [(1280, 720), (1920, 1080), (2560, 1440), (3264, 2448),
         (4000, 3000), (5000, 4000), (8000, 6000)]
BACKENDS = {"DSHOW": cv2.CAP_DSHOW, "MSMF": cv2.CAP_MSMF, "ANY": cv2.CAP_ANY}
WARMUP = 3            # 포맷을 바꾼 직후의 장은 옛 포맷일 수 있어 버린다


def detail_score(bgr):
    """1/2 축소 -> 원래 크기 확대 -> 원본과의 RMS 차(0~255). 보간 사진은 0 근처."""
    g = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
    h, w = g.shape
    small = cv2.resize(g, (max(1, w // 2), max(1, h // 2)), interpolation=cv2.INTER_AREA)
    back = cv2.resize(small, (w, h), interpolation=cv2.INTER_LINEAR)
    d = g.astype(np.float32) - back.astype(np.float32)
    return float(np.sqrt(np.mean(d * d)))


def probe_one(index, backend_flag, fourcc, size, frames):
    """한 조합. dict(요청, 실제, 측정) 또는 {"error": ...}."""
    w, h = size
    cap = cv2.VideoCapture(index, backend_flag)
    try:
        if not cap.isOpened():
            return {"error": "열기 실패"}
        cap.set(cv2.CAP_PROP_FOURCC, cv2.VideoWriter_fourcc(*fourcc))
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, w)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, h)
        got = {
            "fourcc": camera._fourcc_str(cap.get(cv2.CAP_PROP_FOURCC)),
            "w": int(cap.get(cv2.CAP_PROP_FRAME_WIDTH)),
            "h": int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT)),
            "fps": float(cap.get(cv2.CAP_PROP_FPS) or 0),
        }
        for _ in range(WARMUP):
            cap.read()
        times, focus, detail, shape = [], [], [], None
        for _ in range(frames):
            t0 = time.perf_counter()
            ok, f = cap.read()
            dt = time.perf_counter() - t0
            if not ok or f is None or f.size == 0:
                continue
            times.append(dt)
            focus.append(camera.focus_score(f))
            detail.append(detail_score(f))
            shape = (f.shape[1], f.shape[0])
        if not times:
            return {"error": "프레임 없음", "got": got}
        return {"got": got, "frame_wh": shape,
                "sec_per_frame": float(np.median(times)),
                "focus": float(np.median(focus)),
                "detail": float(np.median(detail)),
                "n": len(times)}
    finally:
        cap.release()


def _size(s):
    try:
        w, h = s.lower().split("x")
        return int(w), int(h)
    except ValueError:
        raise argparse.ArgumentTypeError("해상도는 1280x720 처럼 씁니다: %s" % s)


def main(argv=None):
    ap = argparse.ArgumentParser(description="카메라 포맷·해상도 실측")
    ap.add_argument("--index", type=int, default=1, help="카메라 번호 (기본 1)")
    ap.add_argument("--backend", default="DSHOW", choices=sorted(BACKENDS))
    ap.add_argument("--fourcc", action="append", help="MJPG / YUY2 (여러 번 가능)")
    ap.add_argument("--size", action="append", type=_size, help="1280x720 (여러 번 가능)")
    ap.add_argument("--frames", type=int, default=5, help="조합당 받을 프레임 수")
    a = ap.parse_args(argv)
    fourccs = [f.upper() for f in (a.fourcc or FOURCCS)]
    sizes = a.size or SIZES
    flag = BACKENDS[a.backend]

    # 먼저 장치가 열리는지 본다. 안 열리면 조합을 돌 이유가 없다.
    cap = cv2.VideoCapture(a.index, flag)
    opened = cap.isOpened()
    ok = False
    if opened:
        for _ in range(5):
            r, f = cap.read()
            if r and f is not None and f.size:
                ok = True
                break
    cap.release()
    if not ok:
        print("카메라 %d 을(를) 열지 못했습니다 (backend %s · %s)."
              % (a.index, a.backend, "열렸지만 프레임 없음" if opened else "open 실패"))
        print("  0번은 노트북 내장 카메라입니다. 앱이 켜져 있으면 장치가 점유돼 열리지 않습니다.")
        return 2

    print("카메라 %d · %s · 조합 %d개 · 조합당 %d 프레임 (같은 장면을 연속으로 찍는다)"
          % (a.index, a.backend, len(fourccs) * len(sizes), a.frames))
    hdr = "%-5s %-10s | %-5s %-10s %5s | %6s %8s %7s %3s  %s" % (
        "req", "size", "got", "size", "fps", "s/frm", "focus", "detail", "n", "note")
    print(hdr)
    print("-" * len(hdr))
    for fcc in fourccs:
        for size in sizes:
            r = probe_one(a.index, flag, fcc, size, a.frames)
            req = "%dx%d" % size
            if "error" in r:
                g = r.get("got") or {}
                print("%-5s %-10s | %-5s %-10s %5s | %6s %8s %7s %3s  실패: %s"
                      % (fcc, req, g.get("fourcc", "-"),
                         ("%dx%d" % (g["w"], g["h"])) if g else "-", "-",
                         "-", "-", "-", "0", r["error"]))
                continue
            g = r["got"]
            fw, fh = r["frame_wh"]
            note = []
            if g["fourcc"] != fcc:
                note.append("포맷 다름")
            if (fw, fh) != size:
                note.append("해상도 다름")
            if (fw, fh) != (g["w"], g["h"]):
                note.append("프레임 %dx%d" % (fw, fh))
            print("%-5s %-10s | %-5s %-10s %5.1f | %6.3f %8.1f %7.2f %3d  %s"
                  % (fcc, req, g["fourcc"], "%dx%d" % (fw, fh), g["fps"],
                     r["sec_per_frame"], r["focus"], r["detail"], r["n"],
                     " · ".join(note) if note else "요청대로"))
    print("")
    print("detail 은 같은 장면에서만 비교합니다. 해상도를 올렸는데 detail 이 오르지 않으면")
    print("보간(표기 화소)입니다 - 실제 정보는 그대로이고 촬영·검출만 느려집니다.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
