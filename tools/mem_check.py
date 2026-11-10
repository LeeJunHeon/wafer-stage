"""mem_check.py - 오래 켜 둔 서버의 메모리(RSS) · 실시간 스레드 CPU 사용률 시험(dry + 합성 카메라, 실제 브라우저 하나).

  python tools/mem_check.py                 # 1280×960 · 실시간 10 분 · 촬영 50 번 · 샘플 측정 1 번 (약 15 분)
  python tools/mem_check.py --size 4080     # 4080×3060(실제 카메라 크기, 화소 10 배) · 같은 순서
  python tools/mem_check.py --quick         # 축약판(1~2 분): 실시간 60 s · 촬영 10 번 · 측정 1 번
  python tools/mem_check.py --live-s 300 --shots 20 --no-browser

순서: 시작 직후 RSS → 쉬는 중 CPU(실시간 끔) → 실시간 화면(측정 탭, 브라우저가 /preview.jpg 를 화면 주기로 요청) N 초 동안 10 s 마다 RSS · CPU ·
실제 갱신 장/초 · 헤드를 가끔 조그(그림이 바뀌게) → 촬영 n 번 → 샘플 측정 한 번 → 결과.
기준(실패면 exit 1):
  - 1280×960: 실시간 동안 RSS 증가 ≤ +50 MB (앞 1 분을 뺀 구간의 최소 → 끝 값) · 서버 RSS 최고 ≤ 1 GB
  - 4080×3060: 실시간 동안 RSS 증가 ≤ +150 MB (절대값 · CPU 는 보고만)
  - 시험 전체 동안 컴퓨터 전체 사용량 최고치 - 시작 전 사용량 ≤ 3 GB (서버 하나 · 브라우저 하나)
합성 카메라는 빠른 모드(WAFER2_SYNTH_FAST=1: 판 그림을 한 번만 그리고 프레임마다 복사본에 헤드 표시)로 돈다 - 4080×3060 에서 프레임마다
다시 그리면 4 s 씩 걸려 프로그램이 아니라 합성기를 재게 된다. 카메라 속도는 WAFER2_SYNTH_FPS(기본 30, 4080 은 10).
실제 포트 · 카메라 · VISA 는 열지 않는다(--dry, 임시 데이터 폴더 · 임시 설정 파일).
"""
import argparse
import json
import os
import sys
import threading
import time

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402

MB = 1e6


class Sampler(threading.Thread):
    """2 Hz: 서버 RSS 최고 · 컴퓨터 전체 사용량 최고."""

    def __init__(self, pid):
        super().__init__(daemon=True)
        self.p = psutil.Process(pid)
        self.base_used = psutil.virtual_memory().used
        self.peak_used = self.base_used
        self.peak_rss = 0
        self.alive = True

    def run(self):
        while self.alive:
            try:
                self.peak_rss = max(self.peak_rss, self.p.memory_info().rss)
                self.peak_used = max(self.peak_used, psutil.virtual_memory().used)
            except psutil.Error:
                return
            time.sleep(0.5)


def rss(p):
    return p.memory_info().rss / MB


def cpu_over(p, secs):
    """secs 동안 프로세스 CPU 사용률(%, 한 코어 = 100)."""
    t0 = p.cpu_times()
    w0 = time.time()
    time.sleep(secs)
    t1 = p.cpu_times()
    return 100.0 * ((t1.user - t0.user) + (t1.system - t0.system)) / max(1e-6, time.time() - w0)


def main(argv):
    ap = argparse.ArgumentParser()
    ap.add_argument("--size", type=int, default=1280, choices=(1280, 4080))
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--live-s", type=float, default=None)
    ap.add_argument("--shots", type=int, default=None)
    ap.add_argument("--no-browser", action="store_true")
    ap.add_argument("--out", default=None, help="결과 JSON 파일(기본: 출력만)")
    a = ap.parse_args(argv)
    live_s = a.live_s if a.live_s is not None else (60.0 if a.quick else 600.0)
    shots = a.shots if a.shots is not None else (10 if a.quick else 50)
    W, H = (1280, 960) if a.size == 1280 else (4080, 3060)
    e2e.W, e2e.H = W, H                                    # 판 영역 지정 크기 등 e2e 도우미가 쓰는 값
    env = {"WAFER2_SYNTH_W": str(W), "WAFER2_SYNTH_H": str(H), "WAFER2_SYNTH_FAST": "1",
           "WAFER2_SYNTH_FPS": os.environ.get("WAFER2_SYNTH_FPS", "30" if a.size == 1280 else "10")}
    patch = {"width": W, "height": H, "preview_fps": 4.0, "capture_frames": 3, "status_poll_s": 2.0,
             "sim": {"time_scale": 50.0}}
    out = {"size": "%d×%d" % (W, H), "live_s": live_s, "shots": shots, "series": []}
    t_all = time.time()
    c = e2e.Client(patch=patch, env=env)
    proc = psutil.Process(c.proc.pid)
    smp = Sampler(c.proc.pid)
    smp.start()
    pw = br = pg = None
    try:
        c.pump(5.0)
        out["rss_start_mb"] = round(rss(proc), 1)
        print("시작 직후 RSS %.0f MB" % out["rss_start_mb"], flush=True)
        out["cpu_idle_pct"] = round(cpu_over(proc, 10.0), 1)
        print("쉬는 중 CPU %.1f %%" % out["cpu_idle_pct"], flush=True)
        # ---- 실시간 ----
        if not a.no_browser:
            from playwright.sync_api import sync_playwright
            pw = sync_playwright().start()
            br = pw.chromium.launch()
            pg = br.new_page(viewport={"width": 1536, "height": 800})
            pg.goto("http://127.0.0.1:%d/" % c.port)
        t0 = time.time()
        seq0 = None
        k = 0
        cpus = []
        while time.time() - t0 < live_s:
            if a.no_browser:                               # 브라우저 없이: 화면처럼 0.25 s 마다 요청
                end = time.time() + 10.0
                while time.time() < end:
                    c.http("/preview.jpg")
                    time.sleep(0.25)
                cpu = None
            else:
                cpu = cpu_over(proc, 10.0)
                cpus.append(cpu)
            k += 1
            if k % 3 == 0:                                 # 헤드를 가끔 움직여 그림이 바뀌게(이동은 짧게)
                c.send_nowait("jog", axis="x", mm=1.0 if (k // 3) % 2 else -1.0)
            st, body = c.http("/preview.json")
            j = json.loads(body)
            seq0 = j["seq"] if seq0 is None else seq0
            row = {"t": round(time.time() - t0), "rss_mb": round(rss(proc), 1), "cpu_pct": None if cpu is None else round(cpu, 1),
                   "fps": j["fps"], "seq": j["seq"]}
            out["series"].append(row)
            c.pump(0.05)
            print("  실시간 %4d s · RSS %.0f MB · CPU %s %% · 갱신 %.1f 장/초" % (row["t"], row["rss_mb"], "—" if cpu is None else "%.0f" % cpu, j["fps"]), flush=True)
        ser = out["series"]
        warm = [r for r in ser if r["t"] >= min(60, live_s / 3)] or ser
        out["rss_live_end_mb"] = ser[-1]["rss_mb"]
        out["rss_live_growth_mb"] = round(ser[-1]["rss_mb"] - min(r["rss_mb"] for r in warm), 1)
        out["rss_live_max_mb"] = max(r["rss_mb"] for r in ser)
        out["cpu_live_pct"] = round(sum(cpus) / len(cpus), 1) if cpus else None
        out["cpu_live_minus_idle_pct"] = None if not cpus else round(out["cpu_live_pct"] - out["cpu_idle_pct"], 1)
        out["live_fps"] = round(sum(r["fps"] for r in ser) / len(ser), 1)
        if pg is not None:
            try:
                out["browser_js_heap_mb"] = round(pg.evaluate("() => performance.memory ? performance.memory.usedJSHeapSize : 0") / MB, 1)
            except Exception:                              # noqa: BLE001
                pass
            br.close()
            pw.stop()
            pw = br = pg = None
        print("실시간 %.0f s 뒤 RSS %.0f MB (증가 %+.0f MB) · CPU %s %% (쉬는 중보다 %s) · %.1f 장/초" % (
            live_s, out["rss_live_end_mb"], out["rss_live_growth_mb"], out["cpu_live_pct"], out["cpu_live_minus_idle_pct"], out["live_fps"]), flush=True)
        # ---- 촬영 n 번 ----
        t = e2e.truth_px(c)
        e2e.setup_area_ref(c, t)
        nfail = 0
        for i in range(shots):
            ok, r = c.send("capture", timeout=180)
            if not ok:
                nfail += 1
            if (i + 1) % 10 == 0:
                print("  촬영 %d · RSS %.0f MB" % (i + 1, rss(proc)), flush=True)
        out["rss_after_shots_mb"] = round(rss(proc), 1)
        out["shot_fail"] = nfail
        print("촬영 %d 번 뒤 RSS %.0f MB (실패 %d)" % (shots, out["rss_after_shots_mb"], nfail), flush=True)
        # ---- 샘플 측정 한 번 ----
        ok, r = e2e.setup_calib(c, t)
        ok2, r2 = c.send("capture", timeout=180)
        from e2e_cases import start_run, wait_run
        ok3, r3 = start_run(c, "접촉 시험", nos=[s["no"] for s in c.state["samples"]][:4])
        done = wait_run(c, 600)
        out["run_ok"] = bool(ok and ok2 and ok3 and done)
        out["rss_after_run_mb"] = round(rss(proc), 1)
        print("샘플 측정 뒤 RSS %.0f MB (%s)" % (out["rss_after_run_mb"], "완료" if out["run_ok"] else "실패 %s %s %s" % (r, r2, r3)), flush=True)
    finally:
        smp.alive = False
        out["rss_peak_mb"] = round(smp.peak_rss / MB, 1)
        out["system_peak_delta_mb"] = round((smp.peak_used - smp.base_used) / MB, 1)
        out["minutes"] = round((time.time() - t_all) / 60.0, 1)
        try:
            if br is not None:
                br.close()
            if pw is not None:
                pw.stop()
        except Exception:                                  # noqa: BLE001
            pass
        c.close()
    left = [p for p in psutil.process_iter(["cmdline"]) if p.info["cmdline"] and any("run.py" in x for x in p.info["cmdline"]) and str(c.port) in " ".join(p.info["cmdline"])]
    lim_growth = 50.0 if a.size == 1280 else 150.0
    check(out.get("rss_live_growth_mb", 1e9) <= lim_growth, "%s · 실시간 %.0f s 동안 RSS 증가 %+.0f MB ≤ %+.0f MB" % (out["size"], live_s, out.get("rss_live_growth_mb", float("nan")), lim_growth))
    if a.size == 1280:
        check(out["rss_peak_mb"] <= 1000.0, "%s · 서버 RSS 최고 %.0f MB ≤ 1 GB" % (out["size"], out["rss_peak_mb"]))
    check(out["system_peak_delta_mb"] <= 3000.0, "%s · 컴퓨터 전체 사용량 최고치 증가 %.0f MB ≤ 3 GB" % (out["size"], out["system_peak_delta_mb"]))
    check(out.get("shot_fail", 1) == 0, "촬영 %d 번 모두 성공 (실패 %s)" % (shots, out.get("shot_fail")))
    check(out.get("run_ok"), "샘플 측정 한 번 완료")
    check(not left, "서버 프로세스가 남지 않음 (%d)" % len(left))
    print(json.dumps({k: v for k, v in out.items() if k != "series"}, ensure_ascii=False))
    if a.out:
        with open(a.out, "w", encoding="utf-8") as f:
            json.dump(out, f, ensure_ascii=False, indent=1)
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
