"""scale_ui.py - 화면 배율 · 사진 크기 시험(playwright). dry 모드.

  python tools/scale_ui.py [photo | scale]

  photo  합성 카메라 2592×1944 로 판 영역 · 빈 판 사진 · 좌표 보정을 만든 뒤 카메라를 끊고, 장비 설정 3 · 8 의 빈 판 사진이 자기 크기로
         그려지는지(사진 경계 · 판 영역 점선이 사진 위 제 자리) 본다.
  scale  자동 배율 결과(CSS 크기) · 꼭짓점 끌기 오차 · 비상정지 자리 · 크기 · 클릭 · 패드 버튼 적중.
         (deviceScaleFactor 1.5 · 1280×688 = 장비 PC 최대화, 1280×640 = 창 모드, 1.0 · 1920×1040, 1.0 · 1536×800)
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
    pg.evaluate("S.viewer.fit('all', S.viewer.area)")
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
                pg.evaluate("S.viewer.fit('all', S.viewer.area)")
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


def main():
    which = sys.argv[1:] or ["photo", "scale"]
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            for w in which:
                print("== %s" % w, flush=True)
                {"photo": run_photo, "scale": run_scale}[w](br)
        finally:
            br.close()
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    sys.exit(1 if e2e.FAILS else 0)


if __name__ == "__main__":
    main()
