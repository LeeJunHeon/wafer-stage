"""ui_lint.py - 화면 배치 검사기(playwright). 모든 화면 · 상태를 4가지 창 크기(1920×1040 · 1536×800 · 1366×728 · 1280×680)에서 열어
컨트롤 겹침 · 같은 줄 가로 간격(< 6 px) · 위아래 간격(< 4 px) · 글자 잘림(줄임표도 툴팁이 없으면 실패) · 카드/패널 밖으로 나감 ·
스크롤 없이 잘린 컨트롤 · 가로 스크롤을 찾는다. 결과 0 건이어야 한다. 스크린샷은 ..\\shots_v2\\ui\\ 에(첫 크기 + 위반이 있는 화면).

  python tools/ui_lint.py [--quick] [--out 폴더]
    --quick  크기 둘(1920×1040 · 1280×680)만
    --size WxH  그 크기 하나만(예: --size 1366x728) - 크기별로 나눠 돌 때

서버는 --dry + 합성 카메라 + 가짜 장비, 임시 데이터 폴더. 실제 포트 · 카메라 · VISA 는 열지 않는다.
"""
import json
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
import e2e_rt  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, "..", "shots_v2", "ui")
SIZES = [(1920, 1040), (1536, 800), (1366, 728), (1280, 680)]

LINT_JS = r"""
() => {
  const out = [];
  const vis = (el) => {
    if (!el.getClientRects().length) return false;
    const r = el.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) return false;
    for (let e = el; e; e = e.parentElement) { if (e.classList && e.classList.contains('hidden')) return false; const cs = getComputedStyle(e); if (cs.display === 'none' || cs.visibility === 'hidden') return false; }
    return true;
  };
  const name = (el) => { const t = (el.innerText || el.value || el.getAttribute('placeholder') || el.getAttribute('data-act') || '').trim().replace(/\s+/g, ' ').slice(0, 24); return el.tagName.toLowerCase() + (el.id ? '#' + el.id : '') + (t ? '「' + t + '」' : ''); };
  const scope = document.querySelector('#win:not(.hidden)') ? document.querySelector('#win') : document.querySelector('#canvas');
  const inWin = scope.id === 'win';
  const sel = 'button, input:not([type=hidden]), select, textarea, .sc';
  let ctl = Array.from(scope.querySelectorAll(sel)).filter(vis);
  ctl = ctl.filter((e) => !e.closest('#tip') && !e.closest('#toast') && !e.closest('.pop'));
  if (!inWin) ctl = ctl.filter((e) => !e.closest('#win'));
  // 보이는 부분만 본다: 스크롤 컨테이너 · 잘림 조상에 가려진 부분은 겹침으로 세지 않는다
  const visRect = (e) => {
    const r = e.getBoundingClientRect();
    let L = r.left, T = r.top, R = r.right, B = r.bottom;
    for (let a = e.parentElement; a && a !== document.documentElement; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (cs.overflowX !== 'visible' || cs.overflowY !== 'visible') { const ar = a.getBoundingClientRect(); L = Math.max(L, ar.left); T = Math.max(T, ar.top); R = Math.min(R, ar.right); B = Math.min(B, ar.bottom); }
    }
    return { left: L, top: T, right: R, bottom: B, width: R - L, height: B - T };
  };
  const rects = ctl.map((e) => ({ e, r: visRect(e) })).filter((x) => x.r.width > 1 && x.r.height > 1);
  const exempt = (e) => e.closest('.seg, .mtarget, #tabs, .setnav');
  const key = (e) => name(e);
  for (let i = 0; i < rects.length; i++) for (let j = i + 1; j < rects.length; j++) {
    const a = rects[i], b = rects[j];
    if (a.e.contains(b.e) || b.e.contains(a.e)) continue;
    const ox = Math.min(a.r.right, b.r.right) - Math.max(a.r.left, b.r.left), oy = Math.min(a.r.bottom, b.r.bottom) - Math.max(a.r.top, b.r.top);
    if (ox > 1 && oy > 1) { out.push('겹침 ' + key(a.e) + ' × ' + key(b.e)); continue; }
    if (exempt(a.e) && exempt(b.e) && a.e.parentElement === b.e.parentElement) continue;
    // 같은 줄: 세로 범위가 겹치고 가로 간격이 6 px 미만
    if (oy > 4 && ox <= 0 && -ox < 6) out.push('가로 간격 ' + Math.round(-ox) + 'px ' + key(a.e) + ' | ' + key(b.e));
    // 위아래: 가로 범위가 겹치고 세로 간격이 4 px 미만
    if (ox > 4 && oy <= 0 && -oy < 4) out.push('세로 간격 ' + Math.round(-oy) + 'px ' + key(a.e) + ' / ' + key(b.e));
  }
  // 글자 잘림
  const tipped = (e) => { for (let x = e, n = 0; x && n < 4; x = x.parentElement, n++) if (x.getAttribute && (x.getAttribute('data-tip') || x.getAttribute('title'))) return true; return false; };
  const textEls = Array.from(scope.querySelectorAll('button, label, .snm, .cell .v, .cell .k, .sitem .s, .sitem .s2, .sitem .t, .chip, .why, .nasprev, h2, h3, .sub, .band span, .gate span')).filter(vis).filter((e) => !e.closest('#tip') && !e.closest('.pop'));
  textEls.forEach((e) => {
    const cs = getComputedStyle(e);
    if (e.scrollWidth > e.clientWidth + 1 && (cs.overflowX !== 'visible' || cs.textOverflow === 'ellipsis') && !tipped(e)) out.push('글자 잘림 ' + key(e) + ' (' + e.scrollWidth + '>' + e.clientWidth + ')');
  });
  // 카드/패널 밖으로 나감 · 스크롤 없이 잘림
  const clipOf = (e) => {
    for (let a = e.parentElement; a && a !== document.body; a = a.parentElement) {
      const cs = getComputedStyle(a);
      if (/(hidden|clip)/.test(cs.overflowX + cs.overflowY) && a !== scope && !/(auto|scroll)/.test(cs.overflowX + cs.overflowY)) return a;
    }
    return null;
  };
  ctl.forEach((e) => {
    const r = e.getBoundingClientRect();
    // 가장 가까운 스크롤 컨테이너 안이면(스크롤로 닿을 수 있음) 통과
    let scrollable = false;
    for (let a = e.parentElement; a && a !== document.body; a = a.parentElement) { const cs = getComputedStyle(a); if (/(auto|scroll)/.test(cs.overflowX + cs.overflowY)) { scrollable = true; const ar = a.getBoundingClientRect(); if (r.left < ar.left - 1 || r.right > ar.right + 1) { if (a.scrollWidth > a.clientWidth + 1) scrollable = true; else scrollable = false; } break; } }
    const card = e.closest('.card, .panel, .viewbox, .setmain, .setnav');
    if (card && !scrollable) {
      const cr = card.getBoundingClientRect();
      if (r.right > cr.right + 1 || r.left < cr.left - 1 || r.bottom > cr.bottom + 1 || r.top < cr.top - 1) out.push('카드 밖 ' + key(e) + ' ∉ ' + name(card));
    }
    const cl = clipOf(e);
    if (cl && !scrollable) { const cr = cl.getBoundingClientRect(); if (r.right > cr.right + 1 || r.bottom > cr.bottom + 1 || r.left < cr.left - 1 || r.top < cr.top - 1) out.push('잘림 ' + key(e) + ' ∉ ' + name(cl)); }
  });
  // 화면 밖(창 가장자리)으로 나간 컨트롤
  ctl.forEach((e) => { const r = e.getBoundingClientRect(); if (r.right > innerWidth + 1 || r.bottom > innerHeight + 1 && !/(auto|scroll)/.test(getComputedStyle(e.closest('#stage') || document.body).overflowY)) { if (!e.closest('.mtop, .setscroll, .panel, .slist, #tab-results, .scroll')) out.push('화면 밖 ' + key(e)); } });
  const st = document.querySelector('#stage');
  if (innerWidth >= 1280 && st && st.scrollWidth > st.clientWidth + 1) {
    const wide = Array.from(document.querySelectorAll('#stage *')).filter((e) => e.getBoundingClientRect().right > st.clientWidth + 1 && e.getClientRects().length).slice(0, 3).map((e) => e.tagName.toLowerCase() + (e.id ? '#' + e.id : '') + '.' + String(e.className).slice(0, 20));
    out.push('가로 스크롤 #stage ' + st.scrollWidth + '>' + st.clientWidth + ' ← ' + wide.join(', '));
  }
  // (1) 주 동작은 스크롤 없이 보인다: 비상정지 · 조그 · Z · 스텝 · 촬영 · 측정 시작 · 중지 · 일시정지 · 다음 단계 · [다음]
  if (!inWin) {
    const MAIN = ['#estop', '#tab-measure .jstop', '#tab-measure .jogpad .jogb', '#tab-measure .jstep', '#tab-measure .jzstep', '#tab-measure [data-act="capture"]', '#tab-measure [data-act="mstart"]', '#tab-measure [data-act="rt-start"]',
      '#tab-measure [data-act="mstop"]', '#tab-measure [data-act="rt-stop"]', '#tab-measure [data-act="mpause"]', '#tab-measure [data-act="mresume"]', '#tab-measure [data-act="manswer"]',
      '#tab-setup .jogpad .jogb', '#tab-setup .jstep', '#tab-setup .jzstep', '#tab-setup [data-act="su-next"]'];
    MAIN.forEach((q) => Array.from(document.querySelectorAll(q)).filter(vis).forEach((e) => {
      const r = e.getBoundingClientRect(), v = visRect(e);
      if (r.right > innerWidth + 1 || r.bottom > innerHeight + 1 || r.left < -1 || r.top < -1 || v.width < r.width - 1 || v.height < r.height - 1) out.push('주 동작이 안 보임 ' + key(e) + ' (보이는 ' + Math.round(v.width) + 'x' + Math.round(v.height) + ' / ' + Math.round(r.width) + 'x' + Math.round(r.height) + ')');
    }));
  }
  // (2) 그래프 · 지도 SVG 글자가 틀 밖으로 잘림
  Array.from(document.querySelectorAll('.gsvg, .minimap')).filter(vis).forEach((sv) => {
    const sr = sv.getBoundingClientRect();
    Array.from(sv.querySelectorAll('text')).forEach((t) => {
      const r = t.getBoundingClientRect();
      if (!r.width) return;
      if (r.left < sr.left - 1 || r.right > sr.right + 1 || r.top < sr.top - 1 || r.bottom > sr.bottom + 1) out.push('SVG 글자 잘림 「' + (t.textContent || '').slice(0, 16) + '」 in ' + (sv.parentElement && sv.parentElement.id || sv.getAttribute('class')));
    });
  });
  // (3) 카메라 사진 위를 덮는 요소(지도 · 오류 띠)는 사진 면적의 10 % 이하
  Array.from(document.querySelectorAll('.vp')).filter(vis).forEach((vp) => {
    const vr = vp.getBoundingClientRect(), area = vr.width * vr.height;
    let cov = 0;
    Array.from(vp.children).forEach((ch) => {
      if (!vis(ch) || ch.tagName === 'IMG' || ch.tagName === 'CANVAS' || ch.classList.contains('empty') || ch.classList.contains('world') || ch.classList.contains('cap-msg') || (ch.tagName.toLowerCase() === 'svg' && !ch.classList.contains('minimap'))) return;
      const r = ch.getBoundingClientRect();
      cov += Math.max(0, Math.min(r.right, vr.right) - Math.max(r.left, vr.left)) * Math.max(0, Math.min(r.bottom, vr.bottom) - Math.max(r.top, vr.top));
    });
    if (area > 0 && cov / area > 0.10) out.push('사진을 덮는 요소 ' + Math.round(100 * cov / area) + ' % (한도 10 %)');
  });
  // (4) 내용이 있는데 높이가 60 px 미만인 카드
  Array.from(scope.querySelectorAll('.card, .viewbox, .gbox, .panel')).filter(vis).forEach((e) => {
    const r = e.getBoundingClientRect();
    if (e.closest('.pop')) return;
    if (r.height < 60 && (e.querySelector('button, input, select, svg, img') || (e.textContent || '').trim().length > 3)) out.push('카드 높이 부족 ' + name(e) + '.' + String(e.className).slice(0, 24) + ' ' + Math.round(r.height) + 'px');
  });
  return Array.from(new Set(out));
}
"""


class Lint:
    def __init__(self, out, sizes, client=None):
        self.out, self.sizes, self.total, self.screens, self.c = out, sizes, 0, 0, client
        self.fails = []

    def run(self, pg, tag, first_shot=True):
        """현재 화면을 모든 크기에서 검사."""
        self.screens += 1
        for k, (w, h) in enumerate(self.sizes):
            pg.set_viewport_size({"width": w, "height": h})
            pg.wait_for_timeout(450)
            if self.c:
                self.c.pump(0.05)                           # 서버가 보낸 상태를 읽어 비운다(안 읽으면 서버가 연결을 끊는다)
            v = pg.evaluate(LINT_JS)
            self.total += len(v)
            if (k == 0 and first_shot) or v:
                pg.screenshot(path=os.path.join(self.out, "%s_%dx%d.png" % (tag, w, h)))
            for x in v:
                self.fails.append("[%s %dx%d] %s" % (tag, w, h, x))
            print("%s %dx%d · 위반 %d" % (tag, w, h, len(v)), flush=True)
        pg.set_viewport_size({"width": self.sizes[0][0], "height": self.sizes[0][1]})
        pg.wait_for_timeout(300)


def main():
    args = sys.argv[1:]
    out = OUT
    if "--out" in args:
        out = args[args.index("--out") + 1]
    sizes = [SIZES[0], SIZES[3]] if "--quick" in args else SIZES
    if "--size" in args:                                  # 한 크기만(메모리가 모자랄 때 크기별로 나눠 돈다): --size 1366x728
        w_, h_ = args[args.index("--size") + 1].split("x")
        sizes = [(int(w_), int(h_))]
    os.makedirs(out, exist_ok=True)
    lint = Lint(out, sizes)
    with sync_playwright() as pw:
        br = pw.chromium.launch()

        def newpage(c):
            lint.c = c
            pg = br.new_page(viewport={"width": sizes[0][0], "height": sizes[0][1]})
            pg.on("pageerror", lambda e: lint.fails.append("콘솔 오류 " + str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(2200)
            return pg

        def tab(pg, t):
            pg.click('#tabs .tab[data-tab="%s"]' % t)
            pg.wait_for_timeout(500)

        # ---- 1) 아무것도 연결 안 됨: 띠 여러 줄 ----
        c = e2e.Client(env={"WAFER2_MISSING": "stage,camera,visa", "WAFER2_AUTO_LINK_S": "3600"})
        try:
            pg = newpage(c)
            pg.evaluate("send('inst_connect')")
            pg.wait_for_timeout(1200)
            tab(pg, "measure"); lint.run(pg, "a_measure")
            tab(pg, "results"); lint.run(pg, "a_results")
            tab(pg, "setup")
            for i in range(1, 9):
                pg.click(".sitem >> nth=%d" % (i - 1)); pg.wait_for_timeout(500); lint.run(pg, "a_setup%d" % i, first_shot=(i in (1, 2, 3, 8)))
            pg.close()
        finally:
            c.close()

        # ---- 2) 모두 연결 + 측정 ----
        c = e2e.Client(patch=e2e_rt.patch(rt={"repeat": 1, "cooler_target": "sample", "file_path": ""}, sim={"time_scale": 6.0}))
        try:
            e2e.full_setup(c)
            c.send("capture")
            c.send("inst_connect")
            pg = newpage(c)
            tab(pg, "measure"); lint.run(pg, "b_measure_ready")
            pg.click('[data-act="m-mode"][data-args*="shot"]'); pg.wait_for_timeout(600); lint.run(pg, "b_measure_shot", first_shot=True)
            pg.click('[data-act="m-mode"][data-args*="live"]'); pg.wait_for_timeout(400)
            pg.click('[data-act="m-target"][data-args*="samples"]'); pg.wait_for_timeout(500); lint.run(pg, "b_measure_samples")
            pg.click('[data-act="steps-toggle"]'); pg.wait_for_timeout(400); lint.run(pg, "b_measure_steps")
            pg.click('[data-act="cond-edit"]'); pg.wait_for_timeout(600); lint.run(pg, "b_cond_edit"); pg.click(".win .btns .btn:has-text('닫기')"); pg.wait_for_timeout(300)
            pg.click('[data-act="steps-toggle"]'); pg.click('[data-act="m-target"][data-args*="here"]'); pg.wait_for_timeout(400)
            # 설정 창: 분류 전부
            pg.click('[data-act="m-cond"] >> nth=0'); pg.wait_for_selector(".setwin .setrows", timeout=6000); pg.wait_for_timeout(400)
            n_tabs = pg.locator(".setnav .ni").count()
            for k in range(n_tabs):
                pg.locator(".setnav .ni").nth(k).click(); pg.wait_for_timeout(350)
                lint.run(pg, "b_settings_%02d" % k, first_shot=(k < 3))
            pg.click('[data-act="set-cancel"]'); pg.wait_for_timeout(400)
            # 이 위치 측정 진행
            c.send("move_xy", x=70.0, y=70.0); c.pump(1.0)
            c.send("contact_try", free=True); c.pump(1.5)
            pg.click("#tabs .tab[data-tab='measure']")
            c.send("rt_start"); c.pump(3.0); pg.wait_for_timeout(800)
            lint.run(pg, "b_rt_running")
            c.send("rt_stop"); c.wait(lambda s: s["rt"]["run"] and s["rt"]["run"]["state"] in ("stopped", "done", "error"), 40, "중지"); pg.wait_for_timeout(800)
            lint.run(pg, "b_rt_stopped")
            # 대화상자
            pg.evaluate("dialog('측정을 중지할까요?', '지금 단계를 끊고 히트건을 끄고 K2450 출력을 끕니다.', [{ label: '계속 측정' }, { label: '중지', cls: 'danger' }])"); pg.wait_for_timeout(400)
            lint.run(pg, "b_dialog"); pg.evaluate("closeWin()")
            # 샘플 측정(순회)
            pg.click('[data-act="m-target"][data-args*="samples"]'); pg.wait_for_timeout(400)
            c.send("measure_start", condition="수동 측정", nos=[2, 3], ok_weak=True); c.pump(2.0); pg.wait_for_timeout(800)
            lint.run(pg, "b_tour_running")
            c.send("measure_pause"); c.pump(1.5); pg.wait_for_timeout(600); lint.run(pg, "b_tour_paused")
            c.send("measure_stop"); c.wait(lambda s: s["run"] and s["run"]["state"] in ("done", "stopped", "error"), 60, "순회 끝"); pg.wait_for_timeout(800)
            lint.run(pg, "b_tour_done")
            tab(pg, "results"); lint.run(pg, "b_results")
            pg.click('[data-act="r-kind"][data-args*="rt"]') if pg.locator('[data-act="r-kind"][data-args*="rt"]').count() else None
            pg.wait_for_timeout(500); lint.run(pg, "b_results_rt")
            tab(pg, "setup")
            for i in range(1, 9):
                pg.click(".sitem >> nth=%d" % (i - 1)); pg.wait_for_timeout(600); lint.run(pg, "b_setup%d" % i, first_shot=True)
            pg.close()
        finally:
            c.close()
        # ---- 3) 띠 4줄: 비상정지 경보 + 스테이지(뽑힘) + 카메라 + 측정 장비 ----
        c = e2e.Client(env={"WAFER2_MISSING": "camera,visa", "WAFER2_AUTO_LINK_S": "3600"}, patch={"jog": {"z_max_mm": 1.0}})
        try:
            pg = newpage(c)
            c.pump(0.5)
            pg.evaluate("send('inst_connect')"); pg.wait_for_timeout(800)
            c.http("/estop", "POST"); c.pump(1.2)
            c.dry("unplug", device="stage"); c.pump(4.0); pg.wait_for_timeout(500)
            print("띠", [b["id"] for b in c.state.get("bands", [])], flush=True)
            tab(pg, "measure"); lint.run(pg, "c_measure_band4")
            tab(pg, "setup")
            for i in (1, 2, 7):
                pg.click(".sitem >> nth=%d" % (i - 1)); pg.wait_for_timeout(500); lint.run(pg, "c_setup%d_band4" % i)
            tab(pg, "results"); lint.run(pg, "c_results_band4")
            pg.close()
        finally:
            c.close()
        br.close()
    print("\n화면 %d · 크기 %d · 위반 %d" % (lint.screens, len(sizes), lint.total))
    seen = set()
    for f in lint.fails:
        k = f.split("] ", 1)[-1]
        if k in seen:
            continue
        seen.add(k)
        print("  -", f)
    print("(같은 문구는 처음 한 번만 표시)")
    sys.exit(1 if lint.total or any("콘솔" in f for f in lint.fails) else 0)


if __name__ == "__main__":
    main()
