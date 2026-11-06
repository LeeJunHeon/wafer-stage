"""perf4080.py - 실제 해상도(합성 카메라 4080×3060)로 촬영 → 인식 시간 · 실시간 프레임 · 비상정지 지연을 잰다(fake-serial).

  python tools/perf4080.py
"""
import os
import sys
import time

os.environ["E2E_MODE"] = "fake"
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402

e2e.W, e2e.H = 4080, 3060
import e2e_cases as ec  # noqa: E402


def main():
    c = e2e.Client(patch={"width": 4080, "height": 3060, "preview_fps": 4, "capture_frames": 3, "sim": {"time_scale": 60.0}},
                   env={"WAFER2_SYNTH_W": "4080", "WAFER2_SYNTH_H": "3060"})
    out = {}
    try:
        t = e2e.full_setup(c)
        t0 = time.time()
        ok, r = c.send("capture", timeout=300)
        out["촬영→인식 s"] = time.time() - t0
        e2e.check(ok and len(c.state["samples"]) == len(t["chips"]), "4080×3060 촬영 · 인식 %d / %d · %.1f s" % (len(c.state["samples"]), len(t["chips"]), out["촬영→인식 s"]))
        e2e.check(e2e.chips_err(c, t) < 0.8, "좌표 오차 %.2f mm" % e2e.chips_err(c, t))
        # 실시간 프레임: 서버가 만든 미리보기 프레임 수(화면이 실시간을 켠 것처럼 preview 켬)
        import json as _json
        c.ws.send(_json.dumps({"cmd": "preview", "on": True}))
        c.pump(1.0)
        n0, t0 = c.state["camera"]["preview_n"], time.time()
        c.pump(6.0)
        n1 = c.state["camera"]["preview_n"]
        fps = (n1 - n0) / (time.time() - t0)
        out["실시간 프레임 fps"] = fps
        code, body = c.http("/preview.jpg")
        e2e.check(fps >= 1.5 and len(body) > 1000, "실시간 프레임 %.1f fps · 한 장 %d KB (설정 preview_fps 4)" % (fps, len(body) // 1024))
        # 비상정지 지연
        lat = {}

        def estop_ms():
            t1 = time.monotonic()
            c.http("/estop", "POST")
            ok, r = c.dry("bang")
            return (r["t"] - t1) * 1000.0 if r and r.get("t") else 9999.0
        c.dry("slow", s=0.5)
        i = c.send_nowait("jog", axis="x", mm=60.0)
        time.sleep(0.4)
        lat["이동 중"] = estop_ms()
        c.wait_ack(i, 30)
        c.dry("slow", s=0.0)
        ec.recover(c)
        c.send("move_xy", x=10.0, y=90.0)                      # 촬영 위치(합성 카메라의 렌더 결과가 캐시에 있는 자리) - 합성 렌더(테스트 전용 · 수 초)가 측정을 흐리지 않게
        tw = time.time()
        c.send("capture", timeout=300)                         # 한 번 더 찍어 캐시를 데운다 - 합성 렌더가 캐시에서 나오므로 이동 · 프레임 받기 · 정렬 · 검출만의 시간이다
        out["촬영→인식 s (합성 렌더 제외)"] = time.time() - tw
        e2e.check(out["촬영→인식 s (합성 렌더 제외)"] < 10.0, "4080×3060 촬영→인식(합성 렌더 제외) %.1f s" % out["촬영→인식 s (합성 렌더 제외)"])
        c.dry("cam_delay", s=0.5)
        i = c.send_nowait("capture")
        time.sleep(1.0)
        lat["촬영 · 검출 중"] = estop_ms()
        c.wait_ack(i, 120)
        c.dry("cam_delay", s=0.0)
        for k, v in lat.items():
            e2e.check(v <= 100.0, "4080×3060 비상정지 지연 %s = %.0f ms (기준 100 ms 이하)" % (k, v))
        out["비상정지 ms"] = lat
    finally:
        c.close()
    print("\n성능: " + " · ".join("%s %s" % (k, ("%.1f" % v) if isinstance(v, float) else v) for k, v in out.items()))
    print("검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main())
