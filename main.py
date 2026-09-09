"""main.py - 진입점.

GUI 모드:      python main.py
오프라인 모드: python main.py --image photo.png [--set seed_pct=20 ...]
초점 스윕:     python main.py --focus

Tk 창 하나에 미리보기와 결과를 모두 담는다 (cv2.imshow 는 쓰지 않는다 -
닫힌 창을 즉시 다시 만들어서 X 버튼 감지가 안 된다).
"""

import argparse
import json
import os
import subprocess
import sys
import time

import cv2
import numpy as np

import camera
import detect
import imgio
import paths
import report

SETTINGS = paths.SETTINGS_PATH      # 경로는 전부 paths.py 를 거친다
NL = "\n"


# --------------------------------------------------------------------------
def load_settings():
    p = dict(detect.DEFAULTS)
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as f:
                p.update(json.load(f))
        except Exception as e:
            print("settings.json 을 읽지 못해 기본값을 씁니다: %s" % e)
    else:
        with open(SETTINGS, "w", encoding="utf-8") as f:
            json.dump(p, f, ensure_ascii=False, indent=2)
        print("settings.json 을 기본값으로 생성했습니다.")
    return p


def apply_overrides(params, pairs):
    for kv in pairs or []:
        if "=" not in kv:
            print("--set 형식이 올바르지 않습니다: %s (key=value)" % kv)
            continue
        k, v = kv.split("=", 1)
        k = k.strip()
        try:
            params[k] = json.loads(v)
        except Exception:
            params[k] = v
    return params


def save_setting(key, value):
    """settings.json 의 항목 하나만 갱신 (나머지 값과 주석 없는 형식은 그대로)."""
    cur = dict(detect.DEFAULTS)
    if os.path.exists(SETTINGS):
        try:
            with open(SETTINGS, "r", encoding="utf-8") as f:
                cur.update(json.load(f))
        except Exception:
            pass
    cur[key] = value
    with open(SETTINGS, "w", encoding="utf-8") as f:
        json.dump(cur, f, ensure_ascii=False, indent=2)


def focus_only(params):
    """초점 스윕만 돌려 최적 focus 값을 찾고 저장한다."""
    try:
        cam = camera.Camera(params).open()
    except camera.CameraError as e:
        print(str(e))
        return 1
    print("카메라 열림: index=%s backend=%s fourcc=%s"
          % (cam.info["index"], cam.info["backend"], cam.info["actual_fourcc"]))
    if params.get("autofocus", True):
        print("자동초점이 켜져 있어 스윕이 필요 없습니다.")
        print('수동으로 맞추시려면 settings.json 에서 "autofocus": false 로 두고 다시 실행하세요.')
        cam.release()
        return 0
    print("초점 스윕 중... (렌즈가 움직이므로 웨이퍼를 그대로 두세요)")
    r = cam.focus_sweep()
    cam.release()
    top = max((c[1] for c in r["curve"]), default=1.0) or 1.0
    for v, sc in r["curve"]:
        bar = "#" * int(round(40 * sc / top))
        print("  focus %3d : %8.1f %s" % (v, sc, bar))
    if not r["supported"]:
        print("\n초점 제어를 쓸 수 없습니다: %s" % r["reason"])
        print("렌즈 링이나 카메라 거리를 직접 조절하고, 미리보기의 선명도 막대로 확인하세요.")
        return 1
    print("\n최적 focus = %d (선명도 %.1f)" % (r["best_value"], r["best_score"]))
    save_setting("focus", int(r["best_value"]))
    save_setting("min_focus_score", int(r["best_score"] * 0.5))
    print("settings.json 에 focus=%d, min_focus_score=%d 를 저장했습니다."
          % (r["best_value"], int(r["best_score"] * 0.5)))
    return 0


def info_lines(result):
    w = result["wafer"]
    tri = sum(1 for s in result["samples"] if s.get("shape") == "triangle")
    lines = ["samples: %d  (triangles: %d)" % (result["sample_count"], tri)]
    if w["found"]:
        lines += [
            "mm/px: %.5f  (D=%.1f mm)" % (w["mm_per_px"], result["params"]["wafer_diameter_mm"]),
            "tilt: %.1f deg   roi fit: %.2f" % (w["tilt_deg"], w["roi_fit"]),
        ]
    elif w.get("surface") == "plate":
        # 철판 모드는 원판이 없는 게 정상이라 "wafer not found" 로 적으면 안 된다
        lines += ["plate mode (origin = frame center)",
                  ("mm/px: %.5f" % w["mm_per_px"]) if w["mm_per_px"] > 0
                  else "scale: NOT calibrated (pixels)"]
    else:
        lines.append("wafer not found")
    lines.append("focus: %.0f" % result["capture"].get("chosen_focus_score", 0))
    for x in result["warnings"][:4]:
        lines.append("! " + x)
    return lines


def run_detection(bgr, params, note, cam_info, cap_stats, capture_ms, extra_warnings):
    t0 = time.time()
    det = detect.detect(bgr, params)
    timing = {"capture": int(round(capture_ms)), "detect": int(round(det.detect_ms))}
    result = report.build_result(note, bgr.shape, cam_info, cap_stats, timing,
                                 params, det, extra_warnings)
    ann = detect.annotate(bgr, det, info_lines(result))
    d = report.save(paths.resolve_out(params.get("out_dir", "out")),
                    result, bgr, ann, det)
    _ = time.time() - t0
    return det, result, ann, d


# --------------------------------------------------------------------------
# 오프라인 모드
# --------------------------------------------------------------------------
def offline(path, params):
    bgr = imgio.imread_u(path, cv2.IMREAD_COLOR)
    if bgr is None:
        print("이미지를 열 수 없습니다: %s" % path)
        return 2
    cam_info = {
        "index": None, "backend": "file", "requested_fourcc": None,
        "actual_fourcc": None,
        "requested_size": [int(bgr.shape[1]), int(bgr.shape[0])],
        "autofocus_off": None, "focus": None, "exposure": None,
    }
    score = camera.focus_score(bgr)
    cap_stats = {"frames_grabbed": 1, "chosen_focus_score": round(score, 1),
                 "focus_min": round(score, 1), "focus_max": round(score, 1),
                 "focus_median": round(score, 1)}
    try:
        det, result, ann, d = run_detection(bgr, params, "offline:" + os.path.basename(path),
                                            cam_info, cap_stats, 0.0, [])
    except IOError as e:
        print("저장 실패    : %s" % e)
        return 3
    w = result["wafer"]
    print("입력      : %s (%dx%d)" % (path, bgr.shape[1], bgr.shape[0]))
    print("웨이퍼    : found=%s  mm/px=%.5f  tilt=%.1f deg  roi_fit=%.3f"
          % (w["found"], w["mm_per_px"], w["tilt_deg"], w["roi_fit"]))
    print("샘플      : %d 개  (검출 %d ms)" % (result["sample_count"], result["timing_ms"]["detect"]))
    for s in result["samples"]:
        flags = ",".join(k for k in ("edge", "rescued", "split", "merged") if s.get(k))
        print("  #%-3d %-9s x=%+8.2f y=%+8.2f mm  %5.2f x %5.2f mm  area=%6.2f mm2  sol=%.2f  %s"
              % (s["no"], s.get("shape", ""), s["x_mm"], s["y_mm"], s["w_mm"], s["h_mm"],
                 s["area_mm2"], s["solidity"], flags))
    for x in result["warnings"]:
        print("경고      : %s" % x)
    for x in result.get("info", []):
        print("안내      : %s" % x)
    print("저장      : %s" % d)
    return 0


# --------------------------------------------------------------------------
# GUI
# --------------------------------------------------------------------------
class App:
    IMG_W, IMG_H = 880, 560

    def __init__(self, root, cam, params):
        import tkinter as tk
        from tkinter import ttk
        self.tk = tk
        self.root = root
        self.cam = cam
        self.p = params
        self.session_max = 1.0
        self.focus_ref = 0.0        # 초점 스윕으로 확인한 '제대로 맞았을 때' 점수
        self.live = True
        self.photo = None
        self.last_dir = None
        self.busy = False

        root.title("웨이퍼 샘플 위치 검출기")
        root.resizable(False, False)

        main = ttk.Frame(root, padding=8)
        main.grid(row=0, column=0)

        self.canvas = tk.Canvas(main, width=self.IMG_W, height=self.IMG_H,
                                bg="#111", highlightthickness=0)
        self.canvas.grid(row=0, column=0, rowspan=2)

        side = ttk.Frame(main, padding=(10, 0, 0, 0))
        side.grid(row=0, column=1, sticky="n")

        self.status = tk.StringVar(
            value="미리보기 중 (자동초점 켜짐)" if params.get("autofocus", True)
            else "미리보기 중 (수동초점)")
        ttk.Label(side, textvariable=self.status, justify="left",
                  wraplength=260).grid(row=0, column=0, sticky="w")

        ttk.Label(side, text="선명도").grid(row=1, column=0, sticky="w", pady=(10, 0))
        self.bar = tk.Canvas(side, width=260, height=16, bg="#222", highlightthickness=0)
        self.bar.grid(row=2, column=0, sticky="w")
        self.bar_txt = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.bar_txt).grid(row=3, column=0, sticky="w")

        self.warn = tk.StringVar(value="")
        ttk.Label(side, textvariable=self.warn, foreground="#c00", justify="left",
                  wraplength=260).grid(row=11, column=0, sticky="w", pady=(8, 0))

        # 표면 선택. 파일(settings.json)은 건드리지 않고 이 세션의 촬영에만 적용된다.
        # 선택값은 run_detection 에 넘어가는 params 를 통해 result.json 의
        # params.surface 로 자연히 기록된다.
        surf = ttk.LabelFrame(side, text="표면", padding=(8, 4))
        surf.grid(row=4, column=0, sticky="we", pady=(10, 0))
        self.surface = tk.StringVar(value=str(params.get("surface", "auto")))
        for i, (val, txt) in enumerate((("auto", "자동"), ("wafer", "웨이퍼"),
                                        ("plate", "철판"))):
            ttk.Radiobutton(surf, text=txt, value=val, variable=self.surface,
                            command=self.on_surface).grid(row=0, column=i, padx=(0, 8))

        big = tk.Button(side, text="촬  영", command=self.on_shoot,
                        font=("맑은 고딕", 20, "bold"), bg="#1a6fd4", fg="white",
                        activebackground="#1558a8", activeforeground="white",
                        height=2, width=12, relief="raised", bd=3)
        big.grid(row=5, column=0, pady=(16, 6), sticky="we")
        self.shoot_btn = big

        self.focus_btn = ttk.Button(side, text="초점 맞추기", command=self.on_focus)
        self.focus_btn.grid(row=6, column=0, sticky="we", pady=2)
        if params.get("autofocus", True):
            # AF 를 켠 상태에서 스윕을 돌리면 서로 싸운다. 수동 모드에서만 쓴다.
            self.focus_btn.state(["disabled"])
        self.back_btn = ttk.Button(side, text="미리보기로", command=self.on_back,
                                   state="disabled")
        self.back_btn.grid(row=7, column=0, sticky="we", pady=2)
        ttk.Button(side, text="폴더 열기", command=self.on_open_dir).grid(
            row=8, column=0, sticky="we", pady=2)

        ttk.Label(side, text="메모").grid(row=9, column=0, sticky="w", pady=(12, 0))
        self.note = tk.StringVar(value="")
        ttk.Entry(side, textvariable=self.note, width=32).grid(row=10, column=0, sticky="we")

        root.protocol("WM_DELETE_WINDOW", self.on_close)
        self.tick()

    # ------------------------------------------------------------------
    def show(self, bgr):
        from PIL import Image, ImageTk
        h, w = bgr.shape[:2]
        s = min(self.IMG_W / w, self.IMG_H / h)          # 비율 유지 축소
        nw, nh = max(1, int(w * s)), max(1, int(h * s))
        small = cv2.resize(bgr, (nw, nh), interpolation=cv2.INTER_AREA)
        img = Image.fromarray(cv2.cvtColor(small, cv2.COLOR_BGR2RGB))
        self.photo = ImageTk.PhotoImage(img)
        self.canvas.delete("all")
        self.canvas.create_image(self.IMG_W // 2, self.IMG_H // 2, image=self.photo)

    def set_bar(self, score):
        # 절대값은 의미 없으므로 기준치 대비 비율로 표시한다. 기준은 초점 스윕을
        # 했으면 '맞았을 때의 점수', 아니면 세션 최대치.
        self.session_max = max(self.session_max, score)
        r = min(1.0, score / self.session_max)
        self.bar.delete("all")
        col = "#2ecc71" if r > 0.7 else ("#f1c40f" if r > 0.35 else "#e74c3c")
        self.bar.create_rectangle(0, 0, 260 * r, 16, fill=col, width=0)
        self.bar_txt.set("%.0f / %s %.0f  (%d%%)"
                         % (score, "초점기준" if self.focus_ref else "세션최대",
                            self.session_max, r * 100))

    # ------------------------------------------------------------------
    def tick(self):
        if self.live and not self.busy:
            f = self.cam.read()
            if f is not None:
                self.show(f)
                self.set_bar(camera.focus_score(f))
        self.root.after(100, self.tick)          # 10 fps. 미리보기에서 검출은 돌리지 않는다.

    # ------------------------------------------------------------------
    def on_shoot(self):
        if self.busy:
            return
        self.busy = True
        self.status.set("촬영 중...")
        self.root.update_idletasks()
        try:
            t0 = time.time()
            frame, stats = self.cam.capture_best()
            cap_ms = (time.time() - t0) * 1000.0
            self.session_max = max(self.session_max, stats["focus_max"])
            wl = camera.capture_warnings(self.cam.info, stats, self.session_max,
                                         self.p.get("min_focus_score", 0))
            det, result, ann, d = run_detection(frame, self.p, self.note.get(),
                                                self.cam.info, stats, cap_ms, wl)
            self.last_dir = d
            self.live = False
            self.show(ann)
            self.set_bar(stats["chosen_focus_score"])
            w = result["wafer"]
            self.status.set(
                "샘플: %d개\n스케일: %s mm/px\n기울기: %.1f°\nROI 일치: %.2f\n선명도: %.0f\n저장: %s"
                % (result["sample_count"],
                   ("%.5f" % w["mm_per_px"]) if w["found"] else "-",
                   w["tilt_deg"], w["roi_fit"], stats["chosen_focus_score"],
                   os.path.basename(d)))
            self.warn.set("\n".join("⚠ " + x for x in result["warnings"]))
            self.back_btn.config(state="normal")
        except Exception as e:
            self.status.set("촬영 실패")
            self.warn.set(str(e))
        finally:
            self.busy = False

    def on_surface(self):
        """라디오 선택을 이 세션의 파라미터에 즉시 반영한다."""
        self.p["surface"] = self.surface.get()

    def on_focus(self):
        """초점 스윕. 렌즈를 훑어 가장 선명한 위치를 찾아 고정한다."""
        if self.busy:
            return
        self.busy = True
        self.live = True
        self.warn.set("")

        def prog(v, sc):
            self.status.set("초점 스윕 중... focus=%d  선명도 %.0f" % (v, sc))
            self.set_bar(sc)
            self.root.update()          # 스윕은 몇 초 걸리므로 화면을 갱신해 준다
        try:
            r = self.cam.focus_sweep(progress=prog)
            if not r["supported"]:
                self.status.set("초점 제어 불가")
                self.warn.set(r["reason"] + NL +
                              "렌즈나 카메라 거리를 직접 맞추고 선명도 막대를 보세요.")
            else:
                self.focus_ref = r["best_score"]
                self.session_max = r["best_score"]   # 기준을 '맞았을 때' 로 다시 잡는다
                self.p["focus"] = r["best_value"]
                self.p["min_focus_score"] = r["best_score"] * 0.5
                save_setting("focus", int(r["best_value"]))
                save_setting("min_focus_score", int(r["best_score"] * 0.5))
                self.status.set(("초점 맞춤: focus=%d (선명도 %.0f)"
                                 % (r["best_value"], r["best_score"]))
                                + NL + "settings.json 에 저장했습니다.")
        except Exception as e:
            self.status.set("초점 스윕 실패")
            self.warn.set(str(e))
        finally:
            self.busy = False

    def on_back(self):
        self.live = True
        self.back_btn.config(state="disabled")
        self.status.set("미리보기 중 (자동초점 켜짐)" if self.p.get("autofocus", True)
                        else "미리보기 중 (수동초점)")
        self.warn.set("")

    def on_open_dir(self):
        d = self.last_dir or paths.resolve_out(self.p.get("out_dir", "out"))
        os.makedirs(d, exist_ok=True)
        try:
            os.startfile(d)
        except AttributeError:
            subprocess.Popen(["xdg-open", d])

    def on_close(self):
        self.live = False
        self.cam.release()
        self.root.destroy()


def gui(params):
    try:
        import tkinter as tk
        from tkinter import messagebox
        from PIL import ImageTk  # noqa: F401
    except Exception as e:
        print("Tkinter/Pillow 를 불러올 수 없습니다: %s" % e)
        return 2

    try:
        cam = camera.Camera(params).open()
    except camera.CameraError as e:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("카메라를 열 수 없습니다", str(e))
        root.destroy()
        print(str(e))
        return 1

    print("카메라 열림: index=%s backend=%s fourcc=%s"
          % (cam.info["index"], cam.info["backend"], cam.info["actual_fourcc"]))
    root = tk.Tk()
    app = App(root, cam, params)
    if not params.get("autofocus", True) and str(params.get("focus", -1)).lower() == "auto":
        root.after(300, app.on_focus)      # 창이 뜬 뒤에 스윕 (몇 초 걸린다)
    root.mainloop()
    cam.release()
    return 0


# --------------------------------------------------------------------------
def main(argv=None):
    ap = argparse.ArgumentParser(description="웨이퍼 샘플 위치 검출기")
    ap.add_argument("--image", help="저장된 사진으로 검출만 실행 (GUI 없음)")
    ap.add_argument("--set", action="append", dest="sets", metavar="KEY=VALUE",
                    help="설정값 임시 덮어쓰기")
    ap.add_argument("--focus", action="store_true",
                    help="초점 스윕만 실행해 최적 focus 값을 찾고 settings.json 에 저장")
    a = ap.parse_args(argv)

    params = apply_overrides(load_settings(), a.sets)
    os.makedirs(paths.resolve_out(params.get("out_dir", "out")), exist_ok=True)
    if a.image:
        return offline(a.image, params)
    if a.focus:
        return focus_only(params)
    return gui(params)


if __name__ == "__main__":
    sys.exit(main())
