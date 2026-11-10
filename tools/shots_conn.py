"""shots_conn.py - 장치 연결 상태별 화면 점검 + 스크린샷(playwright). 서버는 --dry 에 장치를 뺀 환경(WAFER2_MISSING)으로 띄운다 - 실제 포트 · 카메라 · VISA 는 열지 않는다.

  python tools/shots_conn.py [시나리오 ...] [--out 폴더]

시나리오(이름 일부로 고른다):
  a_none         아무것도 연결 안 됨(스테이지 · 카메라 · VISA 없음)
  b_stage_only   스테이지만 연결(카메라 · VISA 없음)
  c_camera_only  카메라만 연결(스테이지 · VISA 없음)
  d_all          모두 연결(dry) + 측정 장비 연결
  e_inst_fail    스테이지 · 카메라 연결, 측정 장비(VISA) 연결 실패

시나리오마다 화면: 측정(이 위치 측정 · 샘플 순회) · 결과 · 장비 설정 1–8 을 찍고, 연결 상태에 맞는지 검사한다
(연결됨 · 완료로 보이는 칸 없음 · 띠 + [다시 연결] · 깨진 이미지 없음 · 사진 필요한 버튼 비활성 + 사유 · 시작 못 하는 사유는 한 줄).
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, "..", "shots_v2", "conn")

SCN = {
    "a_none": {"missing": "stage,camera,visa", "title": "아무것도 연결 안 됨"},
    "b_stage_only": {"missing": "camera,visa", "title": "스테이지만 연결"},
    "c_camera_only": {"missing": "stage,visa", "title": "카메라만 연결"},
    "d_all": {"missing": "", "title": "모두 연결"},
    "e_inst_fail": {"missing": "visa", "title": "측정 장비 연결 실패"},
}

JS_BROKEN_IMGS = """
(() => {
  const bad = [];
  document.querySelectorAll('img').forEach((im) => {
    let el = im, hidden = false;
    while (el) { if (el.classList && el.classList.contains('hidden')) { hidden = true; break; } el = el.parentElement; }
    const shown = !hidden && im.getClientRects().length > 0;
    if (shown && !(im.naturalWidth > 0)) bad.push(im.getAttribute('src'));
  });
  return bad;
})()
"""


def no_broken(pg):
    """깨진 이미지가 없는가. 실시간 사진은 250 ms 마다 새로 받아 오므로 순간적으로 못 읽은 것은 몇 번 다시 본다."""
    for _ in range(4):
        if not pg.evaluate(JS_BROKEN_IMGS):
            return True
        pg.wait_for_timeout(300)
    return False


def run_scenario(name, cfg, out, pw):
    out_dir = out
    os.makedirs(out_dir, exist_ok=True)
    env = {"WAFER2_MISSING": cfg["missing"], "WAFER2_AUTO_LINK_S": "3600"}          # 이 점검은 자동 다시 연결이 끼어들지 않게
    c = e2e.Client(env=env)
    errs = []
    try:
        if name == "d_all":
            e2e.full_setup(c)
            c.send("capture")
        br = pw.chromium.launch()
        pg = br.new_page(viewport={"width": 1920, "height": 1040})
        pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
        pg.on("pageerror", lambda e: errs.append(str(e)))
        pg.goto("http://127.0.0.1:%d/" % c.port)
        pg.wait_for_timeout(2000)

        def shot(tag, wait=500):
            pg.wait_for_timeout(wait)
            pg.screenshot(path=os.path.join(out_dir, "%s_%s.png" % (name, tag)))
            print("shot", name, tag, flush=True)

        def state():
            return json.loads(pg.evaluate("JSON.stringify(UI.state)"))

        def goto_tab(t):
            pg.click('#tabs [data-tab="%s"]' % t)
            pg.wait_for_timeout(500)

        def goto_step(i):
            pg.click('.sitem >> nth=%d' % (i - 1))
            pg.wait_for_timeout(500)

        title = cfg["title"]
        stg = "stage" not in cfg["missing"]
        cam = "camera" not in cfg["missing"]
        if name == "e_inst_fail":
            pg.evaluate("send('inst_connect')")
            pg.wait_for_timeout(1500)
        if name == "d_all":
            pg.evaluate("send('inst_connect')")
            pg.wait_for_timeout(1500)

        # ---- 측정 화면(하나) ----
        goto_tab("measure")
        shot("m1_measure")
        head = pg.inner_text("#head")
        bands = pg.inner_text("#band") if pg.is_visible("#band") else ""
        s = state()
        # 머리줄 상태 필은 동작 상태만
        check(s["phase"] in ("대기", "촬영 중", "이동 중", "측정 중", "일시정지", "비상정지", "오류"), "[%s] 상태 필은 동작 상태만 (%s)" % (title, s["phase"]))
        # 연결되지 않은 장치가 연결 · 완료로 보이지 않는다
        if not stg:
            cell = pg.inner_text("#cells")
            check("스테이지\n연결 안 됨" in cell and "릴레이\n알 수 없음" in cell, "[%s] 머리줄 스테이지 연결 안 됨 · 릴레이 알 수 없음 (%s)" % (title, cell.replace("\n", " ")))
            check("스테이지" in bands and "다시 연결" in bands, "[%s] 스테이지 띠 + [다시 연결]" % title)
        if not cam:
            check("카메라\n연결 안 됨" in pg.inner_text("#cells") and "카메라 연결 안 됨" in bands, "[%s] 카메라 띠 · 머리줄" % title)
        if stg and cam and name == "d_all":
            check(not bands.strip(), "[%s] 연결 문제 띠 없음 (%s)" % (title, bands))
        if name == "e_inst_fail":
            check("측정 장비 연결 안 됨 · K2110 · K2450" in bands, "[%s] 측정 장비 띠 (%s)" % (title, bands.replace("\n", " ")))
            check(pg.locator('#band .band:has-text("K2110") button:has-text("측정 장비 연결")').count() == 1, "[%s] 측정 장비 띠 + [장비 연결]" % title)
        n_rows = pg.locator("#band .band").count()
        want_rows = (0 if stg else 1) + (0 if cam else 1) + (1 if name == "e_inst_fail" else 0)
        check(n_rows == want_rows, "[%s] 장치마다 띠 한 줄 (%d / %d)" % (title, n_rows, want_rows))
        # 시작 못 하는 사유는 한 곳에 하나
        txt = pg.inner_text("#tab-measure")
        check(pg.locator("#tab-measure #gate").count() <= 1, "[%s] 시작 막는 사유 한 곳 (%d)" % (title, pg.locator("#tab-measure #gate").count()))
        check(pg.locator("#tab-measure .why").count() == 0, "[%s] 흩어진 빨간 문구 없음" % title)
        check(no_broken(pg), "[%s] 깨진 이미지 없음 (here)" % title)
        if not stg:
            check("스테이지 연결 안 됨" in pg.inner_text("#gate") and pg.locator('#gate button:has-text("다시 연결")').count() == 1, "[%s] 시작 사유 = 스테이지 연결 안 됨 · [다시 연결]" % title)
            check("히트건 —" in txt and "쿨러 —" in txt, "[%s] 히트건 · 쿨러 상태 — (%s)" % (title, [l for l in txt.splitlines() if "히트건" in l][:1]))

        # ---- 측정 화면: 샘플 · 사진 자리 ----
        shot("m2_samples")
        txt = pg.inner_text("#tab-measure")
        check(pg.locator("#tab-measure .why").count() == 0, "[%s] 샘플 · 흩어진 빨간 문구 없음" % title)
        check("아직 촬영 안 함" in txt or name == "d_all", "[%s] 촬영 전 문구 = 작은 회색 안내" % title)
        check(pg.locator("#tab-measure #gate").count() <= 1, "[%s] 샘플 · 시작 사유 한 곳" % title)
        check(no_broken(pg), "[%s] 깨진 이미지 없음 (samples)" % title)
        if not cam or name != "d_all":
            cap_dis = pg.evaluate("(() => { const b = [...document.querySelectorAll('#tab-measure .btn')].find((x) => x.textContent.trim() === '촬영'); return b ? b.classList.contains('dis') : null; })()")
            if name != "d_all":
                check(cap_dis is True or not stg or not cam, "[%s] 촬영 버튼 비활성 (%s)" % (title, cap_dis))
        # 실시간 · 촬영 사진 전환: 사진 자리가 빈 상태 안내
        pg.click('[data-act="m-mode"][data-args*="live"]')
        pg.wait_for_timeout(800)
        shot("m3_live")
        ve = pg.inner_text("#m-view .vp .empty") if pg.locator("#m-view .vp .empty:not(.hidden)").count() else ""
        if not cam:
            check("카메라 연결 안 됨" in ve and "다시 연결" in ve, "[%s] 실시간 자리 = 카메라 연결 안 됨 · [다시 연결] (%s)" % (title, ve))
        check(no_broken(pg), "[%s] 깨진 이미지 없음 (live)" % title)
        pg.click('[data-act="m-mode"][data-args*="shot"]')
        pg.wait_for_timeout(500)
        if name != "d_all":
            ve = pg.inner_text("#m-view .vp .empty") if pg.locator("#m-view .vp .empty:not(.hidden)").count() else ""
            check("촬영 사진 없음" in ve and "촬영" in ve, "[%s] 촬영 사진 자리 = 촬영 사진 없음 · [촬영] (%s)" % (title, ve))
            shot("m4_shot")

        # ---- 결과 ----
        goto_tab("results")
        shot("res")
        check(no_broken(pg), "[%s] 깨진 이미지 없음 (results)" % title)

        # ---- 장비 설정 1–8 ----
        goto_tab("setup")
        list_txt = pg.inner_text(".slist")
        if name == "a_none":
            check("완료" not in list_txt, "[%s] 단계 목록에 완료 없음 (%s)" % (title, list_txt.replace("\n", " ")[:200]))
            check(pg.locator(".sitem.down").count() >= 3, "[%s] 연결 안 됨(빨강) 단계 (%d)" % (title, pg.locator(".sitem.down").count()))
        for i in range(1, 9):
            goto_step(i)
            shot("s%d" % i, 600)
            check(no_broken(pg), "[%s] 깨진 이미지 없음 (설정 %d)" % (title, i))
            foot = pg.locator(".panel .foot")
            if i < 8:
                check(foot.count() == 1, "[%s] 설정 %d · [다음 단계] 패널 안" % (title, i))
                if foot.count():
                    fb, pb = foot.bounding_box(), pg.locator(".panel").bounding_box()
                    check(fb and pb and fb["x"] >= pb["x"] - 1 and fb["x"] + fb["width"] <= pb["x"] + pb["width"] + 1, "[%s] 설정 %d · [다음 단계] 가 패널 폭 안" % (title, i))
            if not cam and i in (3, 4, 5, 6):
                dis = pg.evaluate("""(() => [...document.querySelectorAll('.panel .btn.primary')].map((b) => [b.textContent.trim(), b.classList.contains('dis'), b.getAttribute('data-tip')]))()""")
                prim = [d for d in dis if d[0] and d[0] != "다음 단계"]
                check(prim and all(d[1] and d[2] for d in prim), "[%s] 설정 %d · 사진 필요한 버튼 비활성 + 사유 (%s)" % (title, i, prim))
            if not cam and i in (3,):
                check(pg.locator("#tab-setup .vp .world:not(.hidden) circle").count() == 0, "[%s] 설정 3 · 사진 없으면 꼭짓점 편집기 없음" % title)
            if i == 2 and not cam:
                pg.click('button:has-text("카메라 찾기")')
                pg.wait_for_timeout(2500)
                shot("s2_scan", 300)
        check(not errs, "[%s] 콘솔 오류 없음 %s" % (title, errs[:3]))
        br.close()
    finally:
        try:
            c.proc.kill()
        except Exception:                                   # noqa: BLE001
            pass


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    out = OUT
    if "--out" in sys.argv:
        out = sys.argv[sys.argv.index("--out") + 1]
        args = [a for a in args if a != out]
    names = [n for n in SCN if not args or any(a in n for a in args)]
    with sync_playwright() as pw:
        for n in names:
            print("==", n, SCN[n]["title"], flush=True)
            run_scenario(n, SCN[n], out, pw)
    n_fail = len(e2e.FAILS) if hasattr(e2e, "FAILS") else 0
    print("검사 %d · 실패 %d" % (e2e.CHECKS[0] if hasattr(e2e, "CHECKS") else 0, n_fail))
    sys.exit(1 if n_fail else 0)


if __name__ == "__main__":
    main()
