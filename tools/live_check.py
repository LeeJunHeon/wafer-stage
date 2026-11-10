"""live_check.py - 실시간 화면 시험(playwright, 실제 브라우저 · 그림 내용 비교). dry · fake 두 모드(E2E_MODE).

  python tools/live_check.py            # 합성 카메라 큐 4 장 · 8 장 둘 다
  python tools/live_check.py 4          # 큐 4 장만

"실시간" 이라고 쓰인 모든 화면(측정 탭 · 장비 설정 1 · 2 · 4 · 5 · 6 · 7)에서 기본 설정(preview_fps 4)으로 장면이 바뀌면 0.6 s 안에 그림이 바뀐다.
장면 바꾸기 = 합성 카메라의 flash(왼쪽 위 1/4 을 바로 칠함 - 합성기가 다시 그리는 시간이 지연에 끼지 않게). 지연 = 장면을 바꾼 순간 → 화면의 <img> 에
그려진 그림(캔버스로 읽은 16×12 회색)이 바뀐 순간. 탭 이동 · 창 열고 닫기 · 촬영 · 측정 끝 · 화면 두 개 접속 뒤에도 같은지 본다.
카메라를 뽑으면 2 s 안에 "실시간 멈춤" 표시 또는 빈 화면.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

LIMIT_MS = 600.0
SIG_JS = """(scope) => {
  const img = document.querySelector(scope + ' .vp img');
  if (!img || !img.complete || !img.naturalWidth || img.offsetParent === null) return null;
  const c = document.createElement('canvas'); c.width = 16; c.height = 12;
  const g = c.getContext('2d'); g.drawImage(img, 0, 0, 16, 12);
  const d = g.getImageData(0, 0, 16, 12).data; const out = [];
  for (let i = 0; i < d.length; i += 4) out.push(Math.round((d[i] + d[i + 1] + d[i + 2]) / 3));
  return out;
}"""
FLASH = [0]


def sig(pg, scope):
    return pg.evaluate(SIG_JS, scope)


def diff(a, b):
    if a is None or b is None:
        return 0.0
    return max(abs(x - y) for x, y in zip(a, b))


def latency(c, pages, scope, name, n=3):
    """장면을 n 번 바꿔 각 화면(pages)의 그림이 바뀌기까지 걸린 시간(ms) 최대."""
    worst = 0.0
    for _ in range(n):
        for pg in pages:
            pg.wait_for_timeout(400)
        base = [sig(pg, scope) for pg in pages]
        if any(b is None for b in base):
            for pg in pages:
                pg.wait_for_timeout(1000)
            base = [sig(pg, scope) for pg in pages]
        FLASH[0] += 1
        c.dry("world", kw={"flash": FLASH[0]})
        t0 = time.time()
        got = [None] * len(pages)
        while time.time() - t0 < 4.0 and any(g is None for g in got):
            for k, pg in enumerate(pages):
                if got[k] is None and diff(sig(pg, scope), base[k]) > 60:
                    got[k] = (time.time() - t0) * 1000.0
            time.sleep(0.02)
        ms = max(g if g is not None else 9999.0 for g in got)
        worst = max(worst, ms)
    check(worst <= LIMIT_MS, "%s · 장면이 바뀌면 %.0f ms 안에 그림이 바뀜 (한도 %.0f)" % (name, worst, LIMIT_MS))
    return worst


def goto_setup(pg, i):
    pg.click('#tabs .tab[data-tab="setup"]')
    pg.wait_for_timeout(300)
    pg.click("#tab-setup .sitem >> nth=%d" % (i - 1))
    pg.wait_for_timeout(500)


def goto_measure_live(pg):
    pg.click('#tabs .tab[data-tab="measure"]')
    pg.wait_for_timeout(300)
    b = pg.locator('#tab-measure [data-act="m-mode"][data-args*="live"]').first
    if b.count():
        b.click()
    pg.wait_for_timeout(500)


def run_depth(br, depth):
    print("== 실시간 · 합성 카메라 큐 %d 장 (%s)" % (depth, e2e.MODE), flush=True)
    c = e2e.Client(patch={"preview_fps": 4.0}, env={"WAFER2_SYNTH_DEPTH": str(depth)})
    pages = []
    try:
        e2e.full_setup(c)
        pg = br.new_page(viewport={"width": 1536, "height": 800})
        pages.append(pg)
        errs = []
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/" % c.port)
        pg.wait_for_timeout(2000)
        goto_measure_live(pg)
        latency(c, [pg], "#tab-measure", "큐 %d · 측정 탭" % depth)
        title = pg.evaluate("() => { const e = document.querySelector('#tab-measure .livetitle'); return e && e.textContent; }")
        check(title == "실시간", "측정 탭 제목 = 실시간 (%s)" % title)
        for i in (1, 2, 4, 5, 6, 7):
            goto_setup(pg, i)
            latency(c, [pg], "#tab-setup", "큐 %d · 장비 설정 %d" % (depth, i), n=2)
        # 탭 이동 뒤
        goto_measure_live(pg)
        latency(c, [pg], "#tab-measure", "큐 %d · 탭 이동 뒤" % depth, n=2)
        # 창 열고 닫기 뒤
        pg.evaluate("() => openSettings()")
        pg.wait_for_timeout(800)
        pg.evaluate("() => closeWin()")
        pg.wait_for_timeout(500)
        latency(c, [pg], "#tab-measure", "큐 %d · 창 열고 닫기 뒤" % depth, n=2)
        # 촬영 뒤(촬영 사진으로 바뀐 화면을 실시간으로 되돌린다)
        ok, r = c.send("capture")
        check(ok, "촬영 (%s)" % (r,))
        goto_measure_live(pg)
        latency(c, [pg], "#tab-measure", "큐 %d · 촬영 뒤" % depth, n=2)
        # 측정 끝 뒤
        from e2e_cases import start_run, wait_run
        ok, r = start_run(c, "접촉 시험", nos=[c.state["samples"][0]["no"]])
        check(ok and wait_run(c, 240), "샘플 측정 한 번 (%s)" % (r,))
        goto_measure_live(pg)
        latency(c, [pg], "#tab-measure", "큐 %d · 측정 끝 뒤" % depth, n=2)
        # 화면 두 개 접속
        pg2 = br.new_page(viewport={"width": 1280, "height": 720})
        pages.append(pg2)
        pg2.goto("http://127.0.0.1:%d/" % c.port)
        pg2.wait_for_timeout(2000)
        goto_measure_live(pg2)
        latency(c, [pg, pg2], "#tab-measure", "큐 %d · 화면 두 개" % depth, n=2)
        pg2.close()
        pages.remove(pg2)
        # 카메라를 뽑으면 2 s 안에 "멈춤" 또는 빈 화면
        c.dry("unplug", device="camera")
        t0 = time.time()
        shown = None
        while time.time() - t0 < 3.0:
            st = pg.evaluate("""() => { const t = document.querySelector('#tab-measure .livetitle');
              const v = document.querySelector('#tab-measure .vp'); const em = v && v.querySelector('.empty, .vempty');
              const world = v && v.querySelector('.world');
              return {stale: !!t && t.classList.contains('livestale'), text: t && t.textContent, empty: !!world && world.classList.contains('hidden')}; }""")
            if st["stale"] or st["empty"]:
                shown = (time.time() - t0, st)
                break
            time.sleep(0.05)
        check(shown is not None and shown[0] <= 2.0, "카메라를 뽑으면 2 s 안에 '멈춤' 또는 빈 화면 (%s)" % (shown,))
        check(not errs, "화면 오류 없음 (%s)" % errs[:2])
    finally:
        for p in pages:
            try:
                p.close()
            except Exception:                                  # noqa: BLE001
                pass
        c.close()


def main(argv):
    depths = [int(x) for x in argv] or [4, 8]
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            for d in depths:
                try:
                    run_depth(br, d)
                except Exception:                                  # noqa: BLE001
                    import traceback
                    traceback.print_exc()
                    check(False, "예외 · 큐 %d" % d)
        finally:
            br.close()
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
