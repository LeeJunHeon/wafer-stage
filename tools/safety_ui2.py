"""safety_ui2.py - 정지 버튼 · 패드 다시 그리기 · 키보드 조그 범위 화면 시험(playwright, 실제 마우스 · 키보드, **실제 속도**). dry · fake 두 모드(E2E_MODE).

  python tools/safety_ui2.py [hold | stop | input | keys | steps | enter | menu | dead]

  hold   마우스로 누르고 있기: 1 mm × 4 s 에서 끊기지 않고(연속 두 스텝 사이 1 s 이하 · 떼기 직전 0.6 s 안에도 스텝 · 8 스텝 이상), 0.1 mm × 3 s(10 스텝 이상),
         그동안 패드 노드가 교체된 횟수 0. 누른 채 버튼 밖으로 끌기 · 떼기 · pointercancel · 창 포커스 잃음 뒤 스텝 0 + JOG.held 비어 있음
  stop   [정지]: 이동 중 · 패드가 꺼진 상태(비상정지 잠금)에서도 눌림(누름 → '!' 100 ms 이내), Enter · Space 로는 안 눌림
  input  X 칸에 150 · Y 칸에 87.5 를 한 글자씩 → 칸 값 · 포커스 유지, [이동] → mx 150.0 · my 87.5
  keys   입력 칸 · ⋯ 메뉴 뒤 화살표 → 조그 0, 패드를 누른 뒤 화살표 → 조그 1, 키를 누른 채(자동 반복) 패드를 눌러도 시작 안 함
  steps  "직접" 칸에 0.5 → 바로 패드 = 0.5 mm, 0 · 한도 초과는 붉게 + 패드 눌림 거절, 조그 4 번 뒤 대기 위치 저장 = 펌웨어 위치
  enter  이동 버튼을 누른 뒤 Space · Enter → 명령이 다시 나가지 않음
  menu   ⋯ 메뉴 항목 더블클릭 → 아래 스텝 버튼이 눌리지 않음(스텝 그대로)
  dead   서버를 죽인 채 정지 · 비상정지 → 0.5 s 뒤 "전달 안 됨" 표시
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
from e2e import check  # noqa: E402
from e2e_cases import serial_cmds  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

REAL = {"WAFER_STAGE_DRY_MOVE_S": "real", "WAFER2_FAKE_SCALE": "1.0"}
PATCH = {"jog": {"z_max_mm": 1.0}, "sim": {"time_scale": 100.0}}
INIT = """
window.__msgs = [];
const __send = WebSocket.prototype.send;
WebSocket.prototype.send = function (d) { try { const m = JSON.parse(d); window.__msgs.push([Date.now(), m.cmd, m.args || null]); } catch (e) {} return __send.call(this, d); };
document.addEventListener('pointerdown', (e) => { const t = e.target.closest && e.target.closest('[data-act="stop"],[data-act="estop"]'); if (t) window.__tdown = Date.now(); }, true);
"""


def page(br, c, w=1536, h=800):
    pg = br.new_page(viewport={"width": w, "height": h})
    pg.add_init_script(INIT)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:%d/" % c.port)
    pg.wait_for_timeout(1800)
    pg.errs = errs
    return pg


def msgs(pg, cmd):
    return [m for m in pg.evaluate("window.__msgs") if m[1] == cmd]


def clear(pg):
    pg.evaluate("window.__msgs = []")


def padbtn(pg, label, scope="#tab-measure"):
    return pg.locator(scope + ' .jogpad .jogb:has-text("%s")' % label).first


def center(loc):
    b = loc.bounding_box()
    return b["x"] + b["width"] / 2, b["y"] + b["height"] / 2


def mark_pad(pg, scope="#tab-measure"):
    pg.evaluate("(s) => { window.__padEl = document.querySelector(s + ' .jogpad'); window.__padEl.__mark = 7; }", scope)


def pad_same(pg):
    return pg.evaluate("() => !!window.__padEl && window.__padEl.isConnected && window.__padEl.__mark === 7 && window.__padEl === document.querySelector('.jogpad')")


def hold(pg, c, btn, secs, step_v, name):
    clear(pg)
    pg.click('#tab-measure .jstep[data-v="%s"]' % step_v) if step_v in ("0.1", "1") else None
    mark_pad(pg)
    x, y = center(btn)
    pg.mouse.move(x, y)
    pg.mouse.down()
    pg.wait_for_timeout(int(secs * 1000))
    pg.mouse.up()
    ts = [m[0] for m in msgs(pg, "jog")]
    gaps = [b - a for a, b in zip(ts, ts[1:])]
    t_up = int(time.time() * 1000)
    print("  (%s 스텝 %d · 최대 간격 %.0f ms)" % (name, len(ts), max(gaps or [0])), flush=True)
    check(max(gaps or [0]) <= 1000, "%s · 연속한 두 스텝 사이 1 s 이하 (최대 %.0f ms)" % (name, max(gaps or [0])))
    check(ts and t_up - ts[-1] <= 700, "%s · 떼기 직전 0.6 s 안에도 스텝이 나감 (마지막 %d ms 전)" % (name, t_up - (ts[-1] if ts else 0)))
    check(pad_same(pg), "%s · 그동안 패드 노드가 교체되지 않음" % name)
    return len(ts)


def run_hold(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        n = hold(pg, c, padbtn(pg, "↑"), 4.0, "1", "1 mm × 4 s")
        check(n >= 8, "1 mm 는 8 스텝 이상 (%d)" % n)
        c.pump(1.0)
        c.send("move_xy", x=100.0, y=100.0)
        c.pump(0.5)
        n = hold(pg, c, padbtn(pg, "←"), 3.0, "0.1", "0.1 mm × 3 s")
        check(n >= 10, "0.1 mm 는 10 스텝 이상 (%d)" % n)
        c.pump(1.0)
        # 누른 채 밖으로 끌기 · 떼기 · pointercancel · 창 포커스 잃음
        c.send("move_xy", x=100.0, y=100.0)
        c.pump(0.5)
        b = padbtn(pg, "↑")
        for how in ("끌기", "pointercancel", "blur"):
            clear(pg)
            x, y = center(b)
            pg.mouse.move(x, y)
            pg.mouse.down()
            pg.wait_for_timeout(900)
            if how == "끌기":
                pg.mouse.move(x + 400, y + 300, steps=4)
            elif how == "pointercancel":
                pg.evaluate("() => { const e = document.querySelector('#tab-measure .jogpad .jogb'); e.dispatchEvent(new PointerEvent('pointercancel', {bubbles: true})); }")
            else:
                pg.evaluate("() => window.dispatchEvent(new Event('blur'))")
            pg.wait_for_timeout(300)
            n0 = len(msgs(pg, "jog"))
            pg.wait_for_timeout(1500)
            n1 = len(msgs(pg, "jog"))
            held = pg.evaluate("() => JOG.held")
            pg.mouse.up()
            check(n1 == n0 and held is None, "%s 뒤 스텝 0 · JOG.held 비어 있음 (스텝 %d → %d · held=%s)" % (how, n0, n1, held))
            c.pump(0.8)
        check(not pg.errs, "화면 오류 없음 (%s)" % pg.errs[:2])
    finally:
        c.close()


def run_stop(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        sb = pg.locator("#tab-measure .jstop").first
        check(sb.count() == 1 and sb.inner_text().strip() == "정지", "패드 가운데에 [정지]")
        check(pg.evaluate("() => document.querySelector('#tab-measure .jstop').getAttribute('tabindex')") == "-1", "[정지] 는 포커스를 받지 않음")
        # 이동 중
        c.send_nowait("move_xy", x=200.0, y=200.0)
        pg.wait_for_timeout(700)
        check(pg.evaluate("() => document.querySelector('#tab-measure .jstop').classList.contains('mv')"), "이동 중 [정지] 가 붉은 채움으로 강조")
        n0 = len(c.dry("fw")[1].get("stop_wall", []))
        x, y = center(sb)
        pg.evaluate("window.__tdown = 0")
        pg.mouse.move(x, y)
        pg.mouse.down()
        ms = None
        for _ in range(40):
            w = c.dry("fw")[1].get("stop_wall", [])
            if len(w) > n0:
                ms = w[n0] * 1000.0 - pg.evaluate("window.__tdown")
                break
            pg.wait_for_timeout(50)
        pg.mouse.up()
        check(ms is not None and ms <= 100, "이동 중 누름 → '!' %s ms (한도 100)" % (None if ms is None else int(ms)))
        pg.wait_for_timeout(1200)
        check(len(msgs(pg, "stop")) >= 1, "WS stop 이 나감")
        # 패드가 꺼진 상태(비상정지 잠금)에서도 눌림
        c.http("/estop", "POST")
        pg.wait_for_timeout(900)
        off = pg.evaluate("() => document.querySelector('#tab-measure .jogpad').hasAttribute('data-off')")
        check(off, "비상정지 잠금 · 패드가 꺼져 있음")
        clear(pg)
        n0 = len(c.dry("fw")[1].get("stop_wall", []))
        pg.mouse.move(x, y)
        pg.mouse.down()
        pg.mouse.up()
        pg.wait_for_timeout(600)
        check(len(c.dry("fw")[1].get("stop_wall", [])) > n0, "패드가 꺼진 상태에서도 [정지] 가 눌려 '!' 가 나감")
        # Enter · Space 로는 안 눌림
        clear(pg)
        pg.keyboard.press("Tab")
        pg.keyboard.press("Enter")
        pg.keyboard.press("Space")
        pg.wait_for_timeout(500)
        check(not msgs(pg, "stop"), "Enter · Space 로는 [정지] 가 눌리지 않음")
        check(not pg.evaluate("() => JOG.kbd"), "[정지] 로 키보드 조그가 켜지지 않음")
    finally:
        c.close()


def run_input(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        for sel, text in (("#m-x", "150"), ("#m-y", "87.5")):
            pg.click(sel)
            pg.keyboard.type(text, delay=140)                        # 글자마다 상태 갱신이 끼어든다
            pg.wait_for_timeout(300)
            v = pg.evaluate("(s) => [document.querySelector(s).value, document.activeElement && document.activeElement.id]", sel)
            check(v[0] == text and v[1] == sel[1:], "%s 칸 값 %r · 포커스 유지 (%s)" % (sel, v[0], v[1]))
        mark = len(serial_cmds(c))
        pg.click('[data-act="m-move"]')
        c.pump(4.0)
        cmds = serial_cmds(c)[mark:]
        check("mx 150.0" in cmds and "my 87.5" in cmds, "[이동] → mx 150.0 · my 87.5 (%s)" % [x for x in cmds if x[:2] in ("mx", "my")])
    finally:
        c.close()


def run_keys(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        pg.click("#m-x")
        clear(pg)
        pg.keyboard.press("ArrowUp")
        pg.wait_for_timeout(300)
        check(not msgs(pg, "jog"), "입력 칸을 누른 뒤 화살표 → 조그 0")
        pg.click('#c-pos [data-tip="더 보기"]')
        pg.keyboard.press("ArrowUp")
        pg.wait_for_timeout(300)
        check(not msgs(pg, "jog"), "⋯ 메뉴가 열린 채 화살표 → 조그 0")
        pg.mouse.click(5, 300)
        pg.wait_for_timeout(500)
        pg.mouse.down() if False else None
        b = padbtn(pg, "↑")
        x, y = center(b)
        pg.mouse.move(x, y)
        pg.mouse.down()
        pg.mouse.up()
        pg.wait_for_timeout(2500)
        clear(pg)
        pg.keyboard.press("ArrowUp")
        pg.wait_for_timeout(500)
        check(len(msgs(pg, "jog")) == 1, "패드를 누른 뒤 화살표 → 조그 1 (%d)" % len(msgs(pg, "jog")))
        check(pg.evaluate("() => document.querySelector('#c-pos .jogkbd').classList.contains('on')"), "머리줄에 키보드 조그 켬 표시")
        pg.mouse.click(5, 300)
        pg.wait_for_timeout(2500)
        check(not pg.evaluate("() => JOG.kbd"), "패드 밖을 누르면 꺼짐")
        # 키를 누른 채(자동 반복) 패드를 눌러도 시작하지 않는다
        clear(pg)
        for _ in range(6):
            pg.keyboard.down("ArrowDown")
            pg.wait_for_timeout(60)
        pg.mouse.move(*center(pg.locator('#tab-measure .jstep[data-v="10"]').first))
        pg.mouse.down()
        pg.mouse.up()
        for _ in range(8):
            pg.keyboard.down("ArrowDown")
            pg.wait_for_timeout(60)
        pg.keyboard.up("ArrowDown")
        pg.wait_for_timeout(400)
        check(not msgs(pg, "jog"), "키를 누른 채(자동 반복 중) 패드를 눌러도 조그가 시작되지 않음 (%d)" % len(msgs(pg, "jog")))
    finally:
        c.close()


def run_steps(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        pg.click('#tab-measure .jstep[data-v="10"]')
        inp = pg.locator("#tab-measure .jstep-in").first
        inp.click()
        inp.fill("0.5")
        clear(pg)
        pg.mouse.move(*center(padbtn(pg, "↑")))
        pg.mouse.down()
        pg.mouse.up()
        pg.wait_for_timeout(800)
        j = msgs(pg, "jog")
        check(j and abs(abs(j[0][2]["mm"]) - 0.5) < 1e-6, "직접 칸 0.5 → 바로 패드 = jog mm 0.5 (%s)" % (j and j[0][2]))
        c.pump(1.0)
        for bad in ("0", "999", ""):
            inp.fill(bad)
            clear(pg)
            pg.mouse.move(*center(padbtn(pg, "↑")))
            pg.mouse.down()
            pg.mouse.up()
            pg.wait_for_timeout(500)
            cls = pg.evaluate("() => document.querySelector('#tab-measure .jstep-in').className")
            check(not msgs(pg, "jog") and "bad" in cls, "틀린 값 %r · 패드가 눌리지 않고 칸이 붉음 (%s)" % (bad, cls))
        inp.fill("1")
        c.pump(0.3)
        for _ in range(4):
            pg.mouse.move(*center(padbtn(pg, "↑")))
            pg.mouse.down()
            pg.mouse.up()
            pg.wait_for_timeout(800)
        c.pump(1.2)
        pg.click('#c-pos [data-tip="더 보기"]')
        pg.click('text=지금 위치를 대기 위치로 저장')
        pg.wait_for_timeout(1000)
        c.pump(0.6)
        ok, r = c.send("settings_save", patch={})
        saved = c.state["settings"]["park_xy"]
        f = c.dry("fw")[1]
        fwx = (f["counter"]["x"] / 160.0) if f.get("counter") else c.state["stage"]["x"]
        check(abs(saved[0] - fwx) < 0.15, "대기 위치 저장 = 펌웨어 위치 (%s / %.1f)" % (saved, fwx))
    finally:
        c.close()


def run_enter(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        pg.fill("#m-x", "60")
        pg.fill("#m-y", "60")
        clear(pg)
        pg.click('[data-act="m-move"]')
        pg.keyboard.press("Space")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(600)
        check(len(msgs(pg, "move_xy")) == 1, "[이동] 클릭 뒤 Space · Enter → move_xy 한 번만 (%d)" % len(msgs(pg, "move_xy")))
        pg.wait_for_timeout(3500)
        clear(pg)
        pg.click('[data-act="rt-ztop"]')
        pg.keyboard.press("Space")
        pg.keyboard.press("Enter")
        pg.wait_for_timeout(500)
        check(len(msgs(pg, "z_top")) <= 1, "[Z 맨 위] 도 같음 (%d)" % len(msgs(pg, "z_top")))
        check(pg.evaluate("() => document.activeElement && document.activeElement.tagName") != "BUTTON", "버튼에 포커스가 남지 않음")
    finally:
        c.close()


def run_menu(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        step0 = pg.evaluate("() => JOG.step")
        pg.click('#c-pos [data-tip="더 보기"]')
        item = pg.locator("#pop button", has_text="지금 위치를 촬영 위치로 저장").first
        item.dblclick()
        pg.wait_for_timeout(700)
        check(pg.evaluate("() => JOG.step") == step0, "⋯ 메뉴 항목을 더블클릭해도 아래 스텝 버튼이 눌리지 않음 (스텝 %s → %s)" % (step0, pg.evaluate("() => JOG.step")))
    finally:
        c.close()


def run_dead(br):
    c = e2e.Client(patch=PATCH, env=REAL)
    try:
        pg = page(br, c)
        c.pump(0.5)
        c.proc.kill()
        pg.wait_for_timeout(600)
        for sel, name in (("#estop", "비상정지"), ("#tab-measure .jstop", "정지")):
            pg.evaluate("() => { const e = document.getElementById('nodeliver'); if (e) e.classList.add('hidden'); }")
            x, y = center(pg.locator(sel).first)
            pg.mouse.move(x, y)
            pg.mouse.down()
            pg.mouse.up()
            pg.wait_for_timeout(900)
            vis = pg.evaluate("() => { const e = document.getElementById('nodeliver'); return !!e && !e.classList.contains('hidden') && e.textContent; }")
            check(vis and "전달 안 됨" in vis and "모터 전원" in vis, "서버를 죽인 채 %s → 0.5 s 뒤 '전달 안 됨 · 모터 전원을 끄십시오' (%s)" % (name, vis))
    finally:
        try:
            c.close()
        except Exception:
            pass


RUNS = {"hold": run_hold, "stop": run_stop, "input": run_input, "keys": run_keys, "steps": run_steps, "enter": run_enter, "menu": run_menu, "dead": run_dead}


def main(argv):
    names = argv or list(RUNS)
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            for n in names:
                print("== 화면 시험 · %s (%s)" % (n, e2e.MODE), flush=True)
                RUNS[n](br)
        finally:
            br.close()
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
