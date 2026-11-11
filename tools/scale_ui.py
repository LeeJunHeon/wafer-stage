"""scale_ui.py - 화면 배율 · 사진 크기 시험(playwright). dry 모드.

  python tools/scale_ui.py [photo | scale | zoom]

  photo  합성 카메라 2592×1944 로 판 영역 · 빈 판 사진 · 좌표 보정을 만든 뒤 카메라를 끊고, 장비 설정 3 · 8 의 빈 판 사진이 자기 크기로
         그려지는지(사진 경계 · 판 영역 점선이 사진 위 제 자리) 본다.
  scale  자동 배율 결과(CSS 크기) · 꼭짓점 끌기 오차 · 비상정지 자리 · 크기 · 클릭 · 패드 버튼 적중.
         (deviceScaleFactor 1.5 · 1280×688 = 장비 PC 최대화, 1280×640 = 창 모드, 1.0 · 1920×1040, 1.0 · 1536×800)
  zoom   배율 67 · 80 · 100 · 125 %(설정 ui_zoom)마다: 촬영 사진 더블클릭 → 서버가 받은 사진 좌표 오차(화면 1 px 이내) · 꼭짓점 끌기 ·
         조그 패드 누름 · 누른 채 유지(연속) · 키보드 조그 · 툴팁 · 메뉴 · 창 위치 · 비상정지 위치 · 크기 · 클릭.
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

ENV = {"WAFER2_SYNTH_W": "2592", "WAFER2_SYNTH_H": "1944", "WAFER2_AUTO_LINK_S": "3600"}


def new_page(br, c, w, h, dsf):
    ctx = br.new_context(viewport={"width": w, "height": h}, device_scale_factor=dsf)
    pg = ctx.new_page()
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:%d/" % c.port)
    pg.wait_for_timeout(1500)
    pg.errs = errs
    pg.ctx = ctx
    return pg


def open_step(pg, n):
    pg.evaluate("gotoSetup(%d)" % n)
    pg.wait_for_timeout(700)


def img_rect(pg):
    return pg.evaluate("(() => { const r = S.viewer.img.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")


def check_photo(pg, c, step, area, size):
    open_step(pg, step)
    W, H = pg.evaluate("[S.viewer.W, S.viewer.H]")
    check([W, H] == size, "장비 설정 %d · 사진 크기 %dx%d 로 그림 (%s)" % (step, size[0], size[1], [W, H]))
    pg.evaluate("UI.acts['su-fit']({ m: 'all' })")
    pg.wait_for_timeout(300)
    ir = img_rect(pg)
    vp = pg.evaluate("(() => { const r = S.viewer.el.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
    fill = max(ir[2] / vp[2], ir[3] / vp[3])
    check(fill > 0.85, "장비 설정 %d · 사진이 영역을 채움 (%.2f)" % (step, fill))
    poly = pg.evaluate("(() => { const p = S.viewer.svg.querySelector('polygon'); if (!p) return null; const r = p.getBoundingClientRect(); return [r.left, r.top, r.width, r.height]; })()")
    check(poly is not None, "장비 설정 %d · 판 영역 점선이 있음" % step)
    if poly:
        us = [p[0] for p in area]
        vs = [p[1] for p in area]
        ex = ir[0] + min(us) / size[0] * ir[2]
        ey = ir[1] + min(vs) / size[1] * ir[3]
        err = max(abs(poly[0] - ex), abs(poly[1] - ey))
        check(err <= 2.0, "장비 설정 %d · 점선 왼쪽 위가 사진의 판 영역 자리와 맞음 (오차 %.1f px)" % (step, err))


def run_photo(br):
    c = e2e.Client(env=ENV)
    try:
        t = e2e.truth_px(c)
        e2e.setup_area_ref(c, t)
        ok, r = e2e.setup_calib(c, t)
        check(ok, "좌표 보정 (%s)" % (r,))
        s = c.state
        size = list(s["ref"]["size"])
        check(size == [2592, 1944], "빈 판 사진 크기 기록 (%s)" % size)
        area = s["calib"]["area"]
        c.dry("unplug", device="camera")
        c.wait(lambda st: not st["camera"]["open"], 20, "카메라 끊김")
        c.pump(1.0)
        check(c.state["camera"]["size"] is None, "카메라 끊김 · camera.size 없음")
        pg = new_page(br, c, 1536, 800, 1.0)
        try:
            for step in (3, 8):
                check_photo(pg, c, step, area, size)
            foot = pg.inner_text(".legend")
            check("쿨러 구역" in foot, "장비 설정 8 · 설명 (%s)" % foot.replace("\n", " "))
            check(not pg.errs, "화면 오류 없음 (%s)" % pg.errs[:2])
        finally:
            pg.ctx.close()
    finally:
        c.close()


CONFIGS = [  # (이름, 뷰포트 w, h, dsf, 기대 CSS 폭, 높이)
    ("장비 PC 최대화", 1280, 688, 1.5, 1920, 1032),
    ("장비 PC 창 모드", 1280, 640, 1.5, 1920, 960),
    ("노트북 1920x1040", 1920, 1040, 1.0, 1920, 1040),
    ("노트북 1536x800", 1536, 800, 1.0, 1536, 800),
]


def run_scale(br):
    c = e2e.Client(env=dict(ENV, WAFER2_SYNTH_W="1280", WAFER2_SYNTH_H="960"))
    try:
        t = e2e.truth_px(c)
        e2e.setup_area_ref(c, t)
        for name, w, h, dsf, ew, eh in CONFIGS:
            pg = new_page(br, c, w, h, dsf)
            try:
                css = pg.evaluate("[document.getElementById('canvas').offsetWidth, document.getElementById('canvas').offsetHeight, UI.scale]")
                check(abs(css[0] - ew) <= 2 and abs(css[1] - eh) <= 3, "[%s] 자동 배율 CSS %dx%d (배율 %.3f)" % (name, css[0], css[1], css[2]))
                # 비상정지: 오른쪽 위 · 크기 · 가려지지 않음 · 눌림
                er = pg.evaluate("(() => { const r = document.getElementById('estop').getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom, innerWidth]; })()")
                check(er[2] > er[4] - 40 and er[1] < 20, "[%s] 비상정지 오른쪽 위 (%s)" % (name, [round(x) for x in er]))
                z = css[2]
                check((er[2] - er[0]) / z >= 195 and (er[3] - er[1]) / z >= 50, "[%s] 비상정지 크기 CSS 200x54 유지" % name)
                hit = pg.evaluate("(() => { const r = document.getElementById('estop').getBoundingClientRect(); const e = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2); return !!(e && e.closest('#estop')); })()")
                check(hit, "[%s] 비상정지가 다른 것에 가려지지 않음" % name)
                # 꼭짓점 끌기: 3 단계, 알려진 점으로 끌어 놓고 S.area 값을 본다
                open_step(pg, 3)
                pg.evaluate("UI.acts['su-fit']({ m: 'all' })")         # 화면의 [전체] 와 같은 길(S.zoom 도 바뀐다 - 다음 그리기가 판 맞춤으로 되돌리지 않게)
                pg.wait_for_timeout(300)
                pg.wait_for_function("S.area && S.viewer.W > 0", timeout=5000)
                ir = img_rect(pg)
                W, H = pg.evaluate("[S.viewer.W, S.viewer.H]")
                x0, y0 = pg.evaluate("S.area[0]")
                tx, ty = W * 0.4, H * 0.35
                sx = lambda u: ir[0] + u / W * ir[2]  # noqa: E731
                sy = lambda v: ir[1] + v / H * ir[3]  # noqa: E731
                pg.mouse.move(sx(x0), sy(y0))
                pg.mouse.down()
                pg.mouse.move(sx((x0 + tx) / 2), sy((y0 + ty) / 2), steps=4)
                pg.mouse.move(sx(tx), sy(ty), steps=4)
                pg.mouse.up()
                pg.wait_for_timeout(200)
                nx, ny = pg.evaluate("S.area[0]")
                tol = 1.5 * W / ir[2] / 1.0
                check(abs(nx - tx) <= tol and abs(ny - ty) <= tol, "[%s] 꼭짓점 끌기 오차 %.1f · %.1f 사진 화소 (한 화면 화소 = %.1f)" % (name, nx - tx, ny - ty, W / ir[2]))
                # 패드 버튼 적중
                open_step(pg, 1)
                n_bad = pg.evaluate("""(() => { let bad = 0, n = 0; document.querySelectorAll('.jogpad .jogb').forEach((b) => { const r = b.getBoundingClientRect(); if (!r.width) return; n++;
                    const e = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2); if (!(e && (e === b || b.contains(e)))) bad++; }); return n ? bad : -1; })()""")
                check(n_bad == 0, "[%s] 조그 패드 버튼 누르는 자리가 맞음 (어긋남 %s)" % (name, n_bad))
                # 비상정지 클릭
                pg.mouse.click((er[0] + er[2]) / 2, (er[1] + er[3]) / 2)
                c.pump(0.8)
                check(c.state["stage"] and c.state["stage"]["locked"], "[%s] 비상정지 클릭 → 잠금" % name)
                check(not pg.errs, "[%s] 화면 오류 없음 (%s)" % (name, pg.errs[:2]))
            finally:
                pg.ctx.close()
            c.send("reconnect")
            c.pump(1.0)
    finally:
        c.close()


ZOOMS = ["67", "80", "100", "125"]


def set_zoom(pg, c, z):
    c.send("settings_save", patch={"ui_zoom": z})
    c.pump(0.5)
    pg.wait_for_function("Math.abs(UI.scale - %s) < 1e-6" % (int(z) / 100.0), timeout=5000)
    pg.wait_for_timeout(600)


def rect(pg, sel):
    return pg.evaluate("(() => { const e = document.querySelector(%r); if (!e) return null; const r = e.getBoundingClientRect(); return [r.left, r.top, r.right, r.bottom]; })()" % sel)


def run_zoom(br):
    """배율마다 좌표 · 위치 · 누름 시험. 서버가 받은 좌표는 sample_add(더블클릭) 로 본다(칩이 없는 자리 - 붙이기가 좌표를 바꾸지 않게)."""
    c = e2e.Client(env=dict(ENV, WAFER2_SYNTH_W="1280", WAFER2_SYNTH_H="960"), patch={"sim": {"time_scale": 100.0}})
    try:
        t = e2e.truth_px(c)
        e2e.setup_area_ref(c, t)
        ok, r = c.send("capture")
        check(ok, "촬영 (%s)" % (r,))
        pg = new_page(br, c, 1920, 1040, 1.0)
        try:
            for z in ZOOMS:
                set_zoom(pg, c, z)
                zf = int(z) / 100.0
                css = pg.evaluate("[document.getElementById('canvas').offsetWidth, document.getElementById('canvas').offsetHeight]")
                check(abs(css[0] - 1920 / zf) <= 2, "[%s %%] CSS 크기 %dx%d" % (z, css[0], css[1]))
                # 1) 촬영 사진 더블클릭 → 서버가 받은 사진 좌표
                pg.click('#tabs .tab[data-tab="measure"]')
                pg.wait_for_timeout(400)
                pg.click('[data-act="m-mode"][data-args*="shot"]')
                pg.wait_for_timeout(700)
                pg.evaluate("M.viewer.fit('all', M.viewer.area)")
                pg.wait_for_timeout(200)
                W, H = pg.evaluate("[M.viewer.W, M.viewer.H]")
                for (fu, fv) in ((0.06, 0.5), (0.95, 0.12)):
                    u0, v0 = W * fu, H * fv
                    sx, sy = pg.evaluate("((u, v) => { const r = M.viewer.el.getBoundingClientRect(), sc = UI.scale; return [r.left + (M.viewer.tx + u * M.viewer.k) * sc, r.top + (M.viewer.ty + v * M.viewer.k) * sc]; })(%f, %f)" % (u0, v0))
                    sx, sy = round(sx), round(sy)                                   # 실제 마우스는 화면 화소 정수 자리에 있다(브라우저도 clientX 를 정수로 준다)
                    u0, v0 = pg.evaluate("((x, y) => { const r = M.viewer.el.getBoundingClientRect(), sc = UI.scale; return [((x - r.left) / sc - M.viewer.tx) / M.viewer.k, ((y - r.top) / sc - M.viewer.ty) / M.viewer.k]; })(%d, %d)" % (sx, sy))
                    n0 = len(c.state["samples"])
                    pg.mouse.dblclick(sx, sy)
                    c.wait(lambda st: len(st["samples"]) > n0, 10, "샘플 추가")
                    sm = c.state["samples"][-1]
                    k_scr = pg.evaluate("M.viewer.k * UI.scale")                 # 사진 1 화소 = 화면 몇 화소
                    eu, ev = (sm["u"] - u0) * k_scr, (sm["v"] - v0) * k_scr
                    check(abs(eu) <= 1.0 and abs(ev) <= 1.0, "[%s %%] 사진 더블클릭 → 서버가 받은 좌표 오차 화면 %.2f · %.2f px (사진 %.1f · %.1f px)" % (z, eu, ev, sm["u"] - u0, sm["v"] - v0))
                    c.send("sample_delete", no=sm["no"])
                # 2) 메뉴(⋯) 위치: 누른 버튼 바로 아래 · 화면 안
                more = '#tab-measure #c-pos .chead button:has-text("⋯")'
                ar = pg.locator(more).first.bounding_box()
                pg.locator(more).first.click()
                pg.wait_for_timeout(300)
                pr = rect(pg, "#pop")
                check(pr is not None and abs(pr[1] - (ar["y"] + ar["height"] + 4 * zf)) <= 2 and pr[2] <= 1920 + 1 and pr[3] <= 1040 + 1 and pr[0] >= -1,
                      "[%s %%] 메뉴가 버튼 바로 아래 · 화면 안 (%s · 버튼 %s)" % (z, pr and [round(x) for x in pr], [round(ar["x"]), round(ar["y"] + ar["height"])]))
                pg.mouse.click(5, 1030)
                pg.wait_for_timeout(450)
                # 3) 툴팁: 마우스 오른쪽 아래(CSS 14 · 18 = 화면 화소로 × 배율) · 오른쪽 · 아래 끝이면 화면 안으로 당긴다
                left_sel = pg.evaluate("""(() => { const e = [...document.querySelectorAll('#canvas [data-tip]')].find((x) => { const r = x.getBoundingClientRect(); return r.width > 20 && r.height > 10 && r.left > 40 && r.left < 700 && !x.closest('#estop'); });
                    if (!e) return null; e.setAttribute('data-ztest', '1'); return '[data-ztest="1"]'; })()""")
                for sel_, where in ((left_sel, "왼쪽"), ('#tab-measure [data-act="m-fillnow"]', "오른쪽 끝")):
                    bx = pg.locator(sel_).first.bounding_box()
                    mx, my = round(bx["x"] + 6), round(bx["y"] + 6)
                    pg.mouse.move(mx - 30, my)
                    pg.mouse.move(mx, my, steps=3)
                    pg.wait_for_timeout(500)
                    tr = rect(pg, "#tip:not(.hidden)")
                    if tr is None:
                        check(False, "[%s %%] 툴팁(%s) 보임" % (z, where))
                        continue
                    tw = tr[2] - tr[0]
                    clamped = mx + 14 * zf + tw > 1920 - 8 * zf
                    okx = (1920 - 14 * zf <= tr[2] <= 1920 + 0.5) if clamped else abs(tr[0] - (mx + 14 * zf)) <= 2
                    check(okx and abs(tr[1] - (my + 18 * zf)) <= 2 and tr[2] <= 1920 + 1,
                          "[%s %%] 툴팁(%s)이 마우스 옆 · 화면 안 (%s · 마우스 %d,%d)" % (z, where, [round(x) for x in tr], mx, my))
                # 4) 창 위치 + 비상정지 도크
                pg.evaluate("openSettings('rt')")
                pg.wait_for_selector("#win:not(.hidden) .setrows", timeout=6000)
                wr = rect(pg, "#win")
                check(wr and wr[0] >= 0 and wr[1] >= 0 and wr[2] <= 1920 + 1 and wr[3] <= 1040 + 1, "[%s %%] 창이 화면 안 (%s)" % (z, [round(x) for x in wr]))
                dk = rect(pg, "#estopDock")
                hit = pg.evaluate("(() => { const r = document.getElementById('estopDock').getBoundingClientRect(); const e = document.elementFromPoint((r.left + r.right) / 2, (r.top + r.bottom) / 2); return !!(e && e.closest('#estopDock')); })()")
                check(dk and hit and dk[2] > 1920 - 40 and dk[1] < 20, "[%s %%] 창 위 비상정지 도크 오른쪽 위 · 가려지지 않음 (%s)" % (z, dk and [round(x) for x in dk]))
                pg.evaluate("closeWin()")
                pg.wait_for_timeout(450)
                # 5) 꼭짓점 끌기(장비 설정 3)
                open_step(pg, 3)
                pg.evaluate("UI.acts['su-fit']({ m: 'all' })")
                pg.wait_for_timeout(300)
                pg.wait_for_function("S.area && S.viewer.W > 0", timeout=5000)
                W3, H3 = pg.evaluate("[S.viewer.W, S.viewer.H]")
                x0, y0 = pg.evaluate("S.area[0]")
                tx_, ty_ = W3 * 0.4, H3 * 0.35
                scr = "((u, v) => { const r = S.viewer.el.getBoundingClientRect(), sc = UI.scale; return [r.left + (S.viewer.tx + u * S.viewer.k) * sc, r.top + (S.viewer.ty + v * S.viewer.k) * sc]; })(%f, %f)"
                a0 = pg.evaluate(scr % (x0, y0))
                a1 = pg.evaluate(scr % (tx_, ty_))
                pg.mouse.move(a0[0], a0[1])
                pg.mouse.down()
                pg.mouse.move((a0[0] + a1[0]) / 2, (a0[1] + a1[1]) / 2, steps=4)
                pg.mouse.move(a1[0], a1[1], steps=4)
                pg.mouse.up()
                pg.wait_for_timeout(200)
                nx, ny = pg.evaluate("S.area[0]")
                k3 = pg.evaluate("S.viewer.k * UI.scale")
                check(abs(nx - tx_) * k3 <= 1.0 and abs(ny - ty_) * k3 <= 1.0, "[%s %%] 꼭짓점 끌기 오차 화면 %.2f · %.2f px" % (z, (nx - tx_) * k3, (ny - ty_) * k3))
                pg.evaluate("UI.acts['su-area-reset'] && 0")
                # 6) 조그 패드: 누름 한 번 = 한 스텝 · 누른 채 유지 = 연속 · 키보드 (가운데에서 시작 - 연속이 끝단에 닿지 않게)
                ok_, r_ = c.send("move_xy", x=120.0, y=120.0)
                check(ok_, "[%s %%] 가운데로 이동 (%s)" % (z, r_))
                open_step(pg, 1)
                pg.click('#tab-setup .jstep[data-v="1"]')
                up = pg.locator('#tab-setup .jogpad .jogb[data-slot="up"]').first
                ub = up.bounding_box()
                ax_ = up.get_attribute("data-axis")
                key = lambda st: st["stage"][ax_]  # noqa: E731
                c.pump(0.4)
                p0 = key(c.state)
                pg.mouse.move(ub["x"] + ub["width"] / 2, ub["y"] + ub["height"] / 2)
                pg.mouse.down()
                pg.mouse.up()
                c.pump(1.0)
                p1 = key(c.state)
                check(abs(abs(p1 - p0) - 1.0) < 0.05, "[%s %%] 패드 한 번 누름 = 한 스텝 (%s %.2f → %.2f)" % (z, ax_, p0, p1))
                pg.keyboard.press("ArrowUp")                                     # 패드를 눌렀으므로 키보드 조그가 켜져 있다
                c.pump(1.0)
                p2 = key(c.state)
                check(abs(abs(p2 - p1) - 1.0) < 0.05 and (p2 - p1) * (p1 - p0) > 0, "[%s %%] 키보드 ↑ = 같은 방향 한 스텝 (%s %.2f → %.2f)" % (z, ax_, p1, p2))
                pg.mouse.down()
                pg.wait_for_timeout(1200)
                pg.mouse.up()
                c.pump(1.0)
                p3 = key(c.state)
                check(abs(p3 - p2) >= 2.0 and (p3 - p2) * (p1 - p0) > 0, "[%s %%] 누른 채 유지 = 같은 방향 연속 (%s %.2f → %.2f)" % (z, ax_, p2, p3))
                # 7) 비상정지: 오른쪽 위 · CSS 200×54 · 클릭 → 잠금 → 다시 연결
                er = rect(pg, "#estop")
                check(er[2] > 1920 - 40 and er[1] < 20 and abs((er[2] - er[0]) / zf - 200) <= 3 and abs((er[3] - er[1]) / zf - 54) <= 3,
                      "[%s %%] 비상정지 오른쪽 위 · CSS 200×54 (%s)" % (z, [round(x) for x in er]))
                pg.mouse.click((er[0] + er[2]) / 2, (er[1] + er[3]) / 2)
                c.pump(0.8)
                check(c.state["stage"]["locked"], "[%s %%] 비상정지 클릭 → 잠금" % z)
                pg.wait_for_timeout(1100)                                       # 비상정지 직후 1 s 거절
                for ax in ("z", "x", "y"):                                       # 사람의 회복 동작(원점 잡기)으로만 풀린다
                    ok_, r_ = c.send("origin", axis=ax, timeout=60)
                    check(ok_, "[%s %%] 회복 · %s 원점 (%s)" % (z, ax.upper(), r_))
                c.wait(lambda st: st["stage"] and not st["stage"]["locked"], 20, "잠금 풀림")
                pg.wait_for_timeout(600)
            check(not pg.errs, "화면 오류 없음 (%s)" % pg.errs[:2])
        finally:
            pg.ctx.close()
    finally:
        c.close()


def main():
    which = sys.argv[1:] or ["photo", "scale", "zoom"]
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            for w in which:
                print("== %s" % w, flush=True)
                {"photo": run_photo, "scale": run_scale, "zoom": run_zoom}[w](br)
        finally:
            br.close()
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    sys.exit(1 if e2e.FAILS else 0)


if __name__ == "__main__":
    main()
