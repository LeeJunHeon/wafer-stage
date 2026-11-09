"""jog_check.py - 조그 방향 시험(playwright, 실제 클릭 · 키). 측정 화면 · 장비 설정 1 · 7 에서 '↑' 버튼과 키보드 ↑ 를 누르면
합성 카메라 사진에서 헤드가 위로 움직이는지(사진 v 좌표가 줄어드는지) 본다. 좌표 보정이 있는 배치(합성 사진)와 없는 배치(기존 프로그램 배치)를 모두 본다.

  python tools/jog_check.py
"""
import os
import sys

import numpy as np

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402


def head_px(c, H):
    st = c.pump(0.3)["stage"]
    p = H @ np.array([st["x"], st["y"], 1.0])
    return p[:2] / p[2]


def main():
    c = e2e.Client(patch={"sim": {"time_scale": 100.0}})
    try:
        e2e.full_setup(c)
        c.send("capture")
        ok, t = c.dry("truth")
        H = np.array(t["H"])
        c.send("move_xy", x=100.0, y=100.0)
        c.pump(1.0)
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1920, "height": 1040})
            errs = []
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(2000)

            def stage_up_check(where, scope):
                """scope: 패드가 있는 화면의 CSS 접두사"""
                # 스텝 10 mm 로
                pg.click(scope + ' .jstep[data-v="10"]')
                lab = pg.evaluate("JOG.dirs && ['up','down','left','right'].map((k) => k + ':' + JOG.dirs[k].join(''))")
                up = pg.locator(scope + ' .jogpad .btn:has-text("↑")').first
                check(up.count() > 0 and "↑" in up.inner_text(), "[%s] 위 버튼 글자 '↑ 축' (%s)" % (where, lab))
                b = head_px(c, H)
                up.click()
                c.pump(1.2)
                a = head_px(c, H)
                check(a[1] < b[1] - 3 and abs(a[0] - b[0]) < 3, "[%s] ↑ 버튼 → 사진에서 헤드가 위로 (v %.0f → %.0f)" % (where, b[1], a[1]))
                b = head_px(c, H)
                pg.mouse.move(5, 500)
                pg.keyboard.press("ArrowUp")
                c.pump(1.2)
                a = head_px(c, H)
                check(a[1] < b[1] - 3 and abs(a[0] - b[0]) < 3, "[%s] 키보드 ↑ → 사진에서 헤드가 위로 (v %.0f → %.0f)" % (where, b[1], a[1]))
                for key, name, axis in (("ArrowLeft", "왼쪽", 0), ("ArrowRight", "오른쪽", 0)):
                    b = head_px(c, H)
                    pg.keyboard.press(key)
                    c.pump(1.2)
                    a = head_px(c, H)
                    ok_ = (a[0] < b[0] - 3) if key == "ArrowLeft" else (a[0] > b[0] + 3)
                    check(ok_ and abs(a[1] - b[1]) < 3, "[%s] 키보드 %s → 사진에서 헤드가 %s (u %.0f → %.0f)" % (where, key, name, b[0], a[0]))

            pg.click('#tabs .tab[data-tab="measure"]')
            pg.wait_for_timeout(800)
            stage_up_check("측정 화면", "#tab-measure")
            pg.click('#tabs .tab[data-tab="setup"]')
            pg.wait_for_timeout(500)
            for step in (1, 7):
                pg.click(".sitem >> nth=%d" % (step - 1))
                pg.wait_for_timeout(700)
                stage_up_check("장비 설정 %d" % step, "#tab-setup")
            check(not errs, "콘솔 오류 없음 %s" % errs[:2])
            br.close()
    finally:
        c.close()
    print("검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    sys.exit(1 if e2e.FAILS else 0)


if __name__ == "__main__":
    main()
