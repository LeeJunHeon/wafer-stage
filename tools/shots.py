"""shots.py - 1920×1040 스크린샷(playwright). e2e 와 같은 dry 서버를 띄워 상태를 만들고 탭 · 상태별로 찍는다.

  python tools/shots.py OUT_DIR
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
import e2e_cases as ec  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def main(out):
    os.makedirs(out, exist_ok=True)
    c = e2e.Client(patch={"sim": {"time_scale": 3.0}})
    errs = []
    try:
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1920, "height": 1040})
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(1500)

            def shot(name, wait=700):
                pg.wait_for_timeout(wait)
                pg.screenshot(path=os.path.join(out, name + ".png"))
                print("shot", name, flush=True)

            def tab(t):
                pg.click('#tabs .tab[data-tab="%s"]' % t)

            shot("01_measure_empty")
            for i in range(1, 9):
                tab("setup")
                pg.click('.sitem >> nth=%d' % (i - 1))
                shot("02_setup_%d_before" % i, 500)
            # 준비
            t = e2e.full_setup(c)
            c.send("capture")
            c.send("sample_select", no=4)
            c.send("sample_exclude", no=9, on=True)
            tab("measure")
            pg.click('[data-act="m-mode"][data-args*="shot"]')
            shot("03_measure_shot_plate", 1200)
            pg.click('[data-act="m-fit"][data-args*="all"]')
            shot("04_measure_shot_all", 800)
            pg.click('[data-act="m-fit"][data-args*="plate"]')
            shot("05_measure_selected_pin", 800)
            pg.click('[data-act="m-mode"][data-args*="live"]')
            shot("06_measure_live", 1500)
            pg.click('[data-act="m-mode"][data-args*="shot"]')
            for i in range(1, 9):
                tab("setup")
                pg.click('.sitem >> nth=%d' % (i - 1))
                shot("07_setup_%d" % i, 800)
            # 측정 중
            c.dry("slow", s=0.0)
            ec.start_run(c, ec.HEAT, nos=[1, 2, 3])
            c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 120)
            tab("measure")
            shot("08_run_heating", 900)
            c.send("measure_pause")
            c.wait(lambda s: s["phase"] == "일시정지", 30)
            shot("09_run_paused", 900)
            c.send("measure_resume")
            ec.wait_run(c, 200)
            tab("results")
            shot("10_results", 1500)
            pg.click("#res-left table tr >> nth=1")
            shot("11_results_detail", 1200)
            # 확인 대기(수동 측정)
            c.dry("slow", s=0.0)
            ec.start_run(c, ec.MANUAL, nos=[2, 3])
            c.wait(lambda s_: s_["run"] and s_["run"]["waiting"] == "확인 대기", 120)
            shot("08b_run_confirm_wait", 900)
            c.send("measure_answer", value="next")
            c.wait(lambda s_: s_["run"] and s_["run"]["waiting"] == "확인 대기", 120)
            c.send("measure_answer", value="next")
            ec.wait_run(c, 120)
            # 비상정지 띠
            ec.estop_http(c)
            tab("measure")
            shot("12_estop_band", 1000)
            ec.recover(c)
            # 오류 띠
            c.dry("fault", name="step_exc", on=True)
            ec.start_run(c, ec.TOUCH, nos=[2])
            ec.wait_run(c, 60)
            shot("13_error_band", 1200)
            c.dry("fault", name="step_exc", on=False)
            br.close()
    finally:
        c.close()
    print("콘솔 오류 %d" % len(errs))
    for e in errs[:20]:
        print("  ", e)


if __name__ == "__main__":
    main(sys.argv[1] if len(sys.argv) > 1 else os.path.join(ROOT, "..", "shots_v2"))
