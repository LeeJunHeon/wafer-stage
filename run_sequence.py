"""run_sequence.py - 촬영 -> 샘플 검출 -> 스테이지 자동 순회.

  python run_sequence.py                     # 촬영 후 no 순서로 전부 순회
  python run_sequence.py --dwell 0           # 각 샘플에서 Enter 를 기다림
  python run_sequence.py --pick              # 번호를 직접 골라 이동
  python run_sequence.py --only 3,8,12       # 지정한 번호만
  python run_sequence.py --dry               # 시리얼 없이 이동 명령만 출력
  python run_sequence.py --image photo.png   # 촬영 대신 파일 사용

좌표는 반드시 '그 프레임' 의 마커로 다시 보정해서 낸다. 마커를 못 찾으면
스테이지를 아예 움직이지 않는다 - 틀린 좌표로 움직이는 것보다 멈추는 게 낫다.
"""

import argparse
import json
import os
import sys
import time

import calib
import paths
import stage as stage_mod

SEQ_LOG = os.path.join(paths.OUT_DIR, "sequence_log.jsonl")


def log_move(no, x_mm, y_mm, reports):
    try:
        os.makedirs(os.path.dirname(SEQ_LOG), exist_ok=True)
        with open(SEQ_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({
                "time": time.strftime("%Y-%m-%d %H:%M:%S"),
                "sample_no": no,
                "target_mm": [round(float(x_mm), 2), round(float(y_mm), 2)],
                "report": [r.strip() for r in reports],
            }, ensure_ascii=False) + "\n")
    except OSError:
        pass


def move_to(st, no, x_mm, y_mm):
    """범위 검사 -> 이동 -> 기록. 범위 밖이면 False (건너뛴다)."""
    if not calib.in_range(x_mm, y_mm):
        print("  건너뜀: #%s (%.1f, %.1f) 는 가동범위 %g~%g mm 밖"
              % (no, x_mm, y_mm, calib.AXIS_MIN, calib.AXIS_MAX))
        return False
    reports = st.goto_mm(x_mm, y_mm)          # 펌웨어에 동시 이동이 없다: X -> Y
    log_move(no, x_mm, y_mm, reports)
    return True


def park(st, xy):
    """파킹 위치로. 이미 그 자리면 펌웨어가 '이미 그 위치' 로 답한다."""
    print("파킹      : X%.1f Y%.1f 로 이동" % (xy[0], xy[1]))
    reports = st.goto_mm(xy[0], xy[1])
    log_move("park", xy[0], xy[1], reports)


def save_seq(bgr, res, params):
    """이번 순회의 근거를 한 폴더에 남긴다."""
    d = os.path.join(paths.OUT_DIR, "seq_" + time.strftime("%Y%m%d_%H%M%S"))
    os.makedirs(d, exist_ok=True)
    calib.save_samples_image(bgr, res, params, outdir=d,
                             ann_name="annotated.jpg", raw_name="raw.png")
    with open(os.path.join(d, "samples.json"), "w", encoding="utf-8") as f:
        json.dump([{"no": no, "u": round(u, 1), "v": round(v, 1),
                    "X": round(x, 2), "Y": round(y, 2)}
                   for no, u, v, x, y in res["rows"]], f, ensure_ascii=False, indent=2)
    return d


def ask(prompt):
    try:
        return input(prompt).strip().lower()
    except EOFError:
        return "q"


def run_auto(st, rows, dwell):
    """no 순서로 순회. dwell 0 이면 각 샘플에서 Enter 를 기다린다."""
    total = len(rows)
    for k, (no, _u, _v, x, y) in enumerate(rows, 1):
        if not move_to(st, no, x, y):
            continue
        print("샘플 %d/%d (#%s) X%.1f Y%.1f 도착" % (k, total, no, x, y))
        if dwell > 0:
            time.sleep(dwell)
        else:
            a = ask("  Enter=다음 / s=건너뛰기 / q=종료 : ")
            if a == "q":
                print("사용자 종료")
                return
            # s 는 '이미 도착한 뒤' 라 다음으로 넘어가는 것과 같다 (기록만 남긴다)
            if a == "s":
                print("  건너뜀")


def run_pick(st, rows):
    """번호를 직접 골라 이동. Enter 면 다음 순서, q 면 종료."""
    order = [r[0] for r in rows]
    idx = 0
    by_no = {r[0]: r for r in rows}
    while True:
        a = ask("샘플 번호 (Enter=다음 순서, q=종료): ")
        if a == "q":
            print("사용자 종료")
            return
        if a == "":
            if idx >= len(order):
                print("  마지막 샘플까지 갔습니다.")
                continue
            no = order[idx]
            idx += 1
        else:
            try:
                no = int(a)
            except ValueError:
                print("  숫자를 입력하세요.")
                continue
            if no not in by_no:
                print("  #%d 는 이번 검출에 없습니다. (있는 번호: %s)"
                      % (no, ", ".join(str(n) for n in order)))
                continue
            if no in order:
                idx = order.index(no) + 1
        _n, _u, _v, x, y = by_no[no]
        if move_to(st, no, x, y):
            print("샘플 #%s X%.1f Y%.1f 도착" % (no, x, y))


def main(argv=None):
    ap = argparse.ArgumentParser(description="촬영 -> 검출 -> 스테이지 순회")
    ap.add_argument("--image", help="촬영 대신 저장된 사진 사용")
    ap.add_argument("--dry", action="store_true", help="시리얼 없이 이동 명령만 출력")
    ap.add_argument("--pick", action="store_true", help="번호를 직접 골라 이동")
    ap.add_argument("--only", help="지정한 번호만 순회 (예: 3,8,12)")
    ap.add_argument("--dwell", type=float, help="샘플마다 대기 초 (0 = Enter 대기)")
    ap.add_argument("--port", help="시리얼 포트 (예: COM5)")
    a = ap.parse_args(argv)

    params = calib.load_params()
    dwell = a.dwell if a.dwell is not None else float(params.get("dwell_s", 5))
    park_xy = list(params.get("park_xy", [0, 90]))

    st = stage_mod.Stage(a.port or params.get("serial_port"), dry=a.dry)
    try:
        banner = st.open()
    except stage_mod.StageError as e:
        print(str(e))
        return 1
    print("포트      : %s" % (st.port_name or "(dry)"))
    for ln in (banner or "").splitlines():
        print("  " + ln)

    try:
        s = st.status()
        print("상태      : X %.2f mm  Y %.2f mm  원점 X=%s Y=%s"
              % (s["x_mm"], s["y_mm"], s["homed_x"], s["homed_y"]))
        if not (s["homed_x"] and s["homed_y"]):
            print("원점 없음. 시리얼 모니터에서 fz x / fz y (또는 sp x y) 후 "
                  "save 하고 다시 실행하세요.")
            return 1

        # 파킹 위치로 (카메라 시야에서 캐리지를 치우는 자리)
        if (abs(s["x_mm"] - park_xy[0]) > 0.05 or abs(s["y_mm"] - park_xy[1]) > 0.05):
            park(st, park_xy)
        else:
            print("파킹      : 이미 X%.1f Y%.1f" % (park_xy[0], park_xy[1]))

        bgr = calib.get_image(a.image, params)
        if bgr is None:
            return 2

        # 마커 재보정 실패 시 저장값으로 폴백하지 않는다 (틀린 좌표로 움직이면
        # 스테이지가 엉뚱한 곳을 찍는다).
        res = calib.sense(bgr, params, allow_fallback=False)
        if res is None:
            print("마커 미검출 - 이동하지 않음")
            return 1
        rows = res["rows"]
        if not rows:
            print("샘플 0개 - 이동하지 않음")
            return 1

        if a.only:
            want = set()
            for t in a.only.split(","):
                t = t.strip()
                if t.isdigit():
                    want.add(int(t))
            missing = sorted(want - {r[0] for r in rows})
            rows = [r for r in rows if r[0] in want]
            if missing:
                print("경고      : --only 의 %s 는 이번 검출에 없습니다" % missing)
            if not rows:
                print("순회할 샘플이 없습니다.")
                return 1

        res_all = dict(res)
        calib.print_rows(res_all)
        d = save_seq(bgr, res, params)
        print("")
        print("저장      : %s" % d)
        print("순회      : %d개 / 대기 %s"
              % (len(rows), "Enter" if dwell <= 0 else "%.1f초" % dwell))
        print("")

        if a.pick:
            run_pick(st, rows)
        else:
            run_auto(st, rows, dwell)
        return 0

    except KeyboardInterrupt:
        print("\n중단(Ctrl+C)")
        return 130
    except stage_mod.StageError as e:
        print("스테이지 오류: %s" % e)
        return 1
    finally:
        # 정상/중단/오류 어느 쪽이든 파킹하고 저장한다. 파킹이 실패해도 save 는 한다.
        try:
            park(st, park_xy)
        except Exception as e:
            print("파킹 실패 : %s" % e)
        try:
            for ln in st.save():
                print("저장응답  : %s" % ln.strip())
        except Exception as e:
            print("save 실패 : %s" % e)
        st.close()


if __name__ == "__main__":
    sys.exit(main())
