"""safety_ui.py - 모터 안전 화면 시험(playwright, 실제 마우스 · 키보드). dry · fake 두 모드(E2E_MODE).

  python tools/safety_ui.py [latency | keys | pad | z]

  latency  비상정지 버튼 pointerdown → 가짜 펌웨어에 '!' 가 쓰인 순간(ms). 상태마다(쉼 · X·Y 이동 · Z 이동 · 접촉 높이로 내리기 · 마우스 연속 조그 ·
           키보드 연속 조그 · 원점 잡기 · 창 열림(도크) · 카메라 자동 시도 중 · 카메라 열기 지연 중 · CPU 부하 · 측정 단계 · R–T 측정). 한도 100 ms.
  keys     키보드 조그 범위(패드를 직접 눌렀을 때만) · 창 열림 · 탭 바뀜 · 포커스 잃음 · 비상정지 때 눌려 있던 키로 다시 안 움직임
  pad      패드 입력: pointerleave · 탭 숨김 · 서버 끊김에서 멈춤, 다시 연결 뒤 패드 정상
  z        Z 스텝 분리 · Z↓ 연속 없음 · 큰 스텝 한 번만 · 클릭 한 번 = 정확히 한 스텝(펄스 수)
"지연 <상태> <ms>" 줄은 보고용 표다.
"""
import json
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

SLOW = {"WAFER_STAGE_DRY_MOVE_S": "1.2", "WAFER2_FAKE_SCALE": "0.3"}
PATCH = {"jog": {"z_max_mm": 1.0}, "sim": {"time_scale": 100.0}}
INIT = """
window.__t0 = 0; window.__jogs = [];
document.addEventListener('pointerdown', (e) => { if (e.target.closest && e.target.closest('[data-act="estop"]')) window.__t0 = Date.now(); }, true);
const __send = WebSocket.prototype.send;
WebSocket.prototype.send = function (d) { try { const m = JSON.parse(d); if (m.cmd === 'jog') window.__jogs.push([Date.now(), m.args]); if (m.cmd === 'estop') window.__estops = (window.__estops || 0) + 1; } catch (e) {} return __send.call(this, d); };
"""
LAT = {}


def bang(c):
    ok, r = c.dry("bang")
    return (r or {}).get("wall") or [] if ok else []


def page(br, c):
    pg = br.new_page(viewport={"width": 1536, "height": 800})
    pg.add_init_script(INIT)
    errs = []
    pg.on("pageerror", lambda e: errs.append(str(e)))
    pg.goto("http://127.0.0.1:%d/" % c.port)
    pg.wait_for_timeout(1800)
    pg.errs = errs
    return pg


def jogs(pg):
    return pg.evaluate("window.__jogs")


def rec(c):
    """회복: Z · X · Y 원점 잡기."""
    c.pump(1.3)
    for ax in ("z", "x", "y"):
        c.send("origin", axis=ax)
    c.pump(0.5)


def ui_latency(pg, c, name, sel="#estop", dispatch=False, limit=100.0):
    n0 = len(bang(c))
    pg.evaluate("window.__t0 = 0")
    if dispatch:
        pg.locator(sel).dispatch_event("pointerdown", {"button": 0})
    else:
        b = pg.locator(sel).bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        pg.mouse.down()
    ms = None
    for _ in range(40):
        w = bang(c)
        if len(w) > n0:
            t0 = pg.evaluate("window.__t0")
            ms = (w[n0] * 1000.0 - t0) if t0 else None
            break
        pg.wait_for_timeout(50)
    if not dispatch:
        pg.mouse.up()
    LAT[name] = ms
    print("지연 %s %s ms" % (name, "—" if ms is None else "%.0f" % ms), flush=True)
    check(ms is not None and ms <= limit, "화면 비상정지 지연 · %s · %s ms (한도 %.0f)" % (name, "—" if ms is None else "%.0f" % ms, limit))


KBD_ON = '#tab-measure .jogpad .jzstep.on'               # 키보드 조그는 패드(방향 · Z · 스텝 버튼)를 직접 눌렀을 때만 켜진다 - 지금 Z 스텝 버튼을 누르면 스텝은 그대로


def kbd_hold(pg, key, secs, step=0.05):
    end = time.time() + secs
    while time.time() < end:
        pg.keyboard.down(key)
        pg.wait_for_timeout(int(step * 1000))


def run_latency(br):
    c = e2e.Client(patch=PATCH, env=SLOW)
    try:
        pg = page(br, c)
        c.pump(0.5)
        ui_latency(pg, c, "쉼")
        rec(c)
        c.send_nowait("move_xy", x=200.0, y=200.0)
        pg.wait_for_timeout(300)
        ui_latency(pg, c, "X·Y 이동 중")
        rec(c)
        c.dry("z", mm=20.0)
        c.pump(0.3)
        c.send_nowait("z_top")
        pg.wait_for_timeout(300)
        ui_latency(pg, c, "Z 이동 중")
        rec(c)
        c.send("settings_save", patch={"contact_z_mm": 30.0})
        c.send_nowait("contact_try", free=True)
        pg.wait_for_timeout(400)
        ui_latency(pg, c, "접촉 높이로 내리기")
        rec(c)
        # 마우스 연속 조그: 패드를 누른 채(0.5 s 넘게) - 비상정지는 다른 포인터로
        pg.click('#tab-measure .jstep[data-v="1"]')
        up = pg.locator('#tab-measure .jogpad .btn:has-text("↑")').first
        b = up.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2)
        pg.mouse.down()
        pg.wait_for_timeout(1100)
        ui_latency(pg, c, "마우스 연속 조그", dispatch=True)
        pg.mouse.up()
        rec(c)
        # 키보드 연속 조그: 패드(지금 Z 스텝 버튼)를 눌러 켠 뒤 ↑ 를 누른 채 - 비상정지는 마우스로
        pg.click(KBD_ON)
        kbd_hold(pg, "ArrowUp", 1.0)
        ui_latency(pg, c, "키보드 연속 조그")
        pg.keyboard.up("ArrowUp")
        rec(c)
        c.send_nowait("origin", axis="x")
        pg.wait_for_timeout(300)
        ui_latency(pg, c, "원점 잡기")
        rec(c)
        pg.click('button:has-text("기록 보기")')
        pg.wait_for_timeout(300)
        ui_latency(pg, c, "창 열림(도크)", sel="#estopDock")
        pg.keyboard.press("Escape")
        pg.evaluate("closeWin()")
        rec(c)
        c.dry("cpu", s=3.0, n=6)
        pg.wait_for_timeout(200)
        ui_latency(pg, c, "CPU 부하")
        c.pump(3.0)
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:2])
    finally:
        c.close()
    # 카메라 자동 시도 중 · 카메라 열기 지연 중(스테이지는 연결돼 있다)
    c = e2e.Client(patch=PATCH, env=dict(SLOW, WAFER2_MISSING="camera", WAFER2_AUTO_LINK_S="1", WAFER2_AUTO_CAM_S="1"))
    try:
        pg = page(br, c)
        c.pump(2.5)
        ui_latency(pg, c, "카메라 자동 시도 중")
        rec(c)
        c.dry("open_delay", camera=4.0)
        c.dry("missing", camera=False)
        c.pump(1.5)
        ui_latency(pg, c, "카메라 열기 지연 중")
        c.pump(1.0)
    finally:
        c.close()
    # 측정 단계 · R–T 측정 중
    import e2e_rt
    from e2e_cases import start_run
    c = e2e.Client(patch=e2e_rt.patch(jog={"z_max_mm": 1.0}, rt={"repeat": 1}), env=dict(SLOW))
    try:
        pg = page(br, c)
        t = e2e.full_setup(c)
        c.send("capture")
        ok, r = start_run(c, "기본", [c.state["samples"][0]["no"]])
        c.pump(0.8)
        check(ok, "샘플 측정 시작 (%s)" % (r,))
        ui_latency(pg, c, "측정 단계 실행")
        c.pump(1.5)
    finally:
        c.close()
    c = e2e.Client(patch=e2e_rt.patch(jog={"z_max_mm": 1.0}), env={"WAFER_STAGE_DRY_MOVE_S": "0.2", "WAFER2_FAKE_SCALE": "0.05"})
    try:
        pg = page(br, c)
        t = e2e.full_setup(c)
        c.send("capture")
        c.send("goto_sample", no=c.state["samples"][3]["no"])
        e2e_rt.at_contact(c, x=c.state["stage"]["x"], y=c.state["stage"]["y"])
        ok, r = c.send("rt_start")
        c.pump(2.0)
        check(ok, "R–T 측정 시작 (%s)" % (r,))
        ui_latency(pg, c, "R–T 측정")
        c.pump(1.0)
    finally:
        c.close()


def run_keys(br):
    c = e2e.Client(patch=PATCH)
    try:
        pg = page(br, c)
        pg.click('#tab-measure .jstep[data-v="1"]')

        def nj():
            return len(jogs(pg))

        # (a) 조그 카드 밖 버튼에 포커스가 있을 때 ↑ 키는 움직이지 않는다
        pg.click("#tab-measure .samchip, #tab-measure button:has-text('지도')")
        n = nj()
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(500)
        check(nj() == n, "다른 버튼에 포커스: ↑ 키가 조그를 보내지 않음 (%d → %d)" % (n, nj()))
        # (b) 패드를 직접 누르면 켜짐 + 표시(가운데 칸은 [정지] - 키보드 조그를 켜지 않는다)
        pg.click(KBD_ON)
        check(pg.locator("#tab-measure #c-pos.kbd").count() == 1, "패드(스텝 버튼)를 누르면 '키보드 조그 켬' (카드 강조)")
        check(pg.locator("#tab-measure .kbdtag").first.is_visible(), "'키보드 조그' 표시가 보임")
        n = nj()
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(700)
        check(nj() == n + 1, "켜진 뒤 ↑ 키 = 조그 한 번 (%d → %d)" % (n, nj()))
        # (c) 카드 밖을 누르면 꺼짐
        pg.click("#tab-measure .vhead")
        n = nj()
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(400)
        check(nj() == n and pg.locator("#tab-measure #c-pos.kbd").count() == 0, "카드 밖을 누르면 꺼짐")
        # (d) 창이 열려 있으면 어떤 경우에도 안 됨
        pg.click(KBD_ON)
        pg.click('button:has-text("기록 보기")')
        pg.wait_for_timeout(300)
        n = nj()
        pg.keyboard.press("ArrowLeft"); pg.wait_for_timeout(500)
        check(nj() == n, "기록 보기 창이 열린 채 ← 키가 조그를 보내지 않음")
        pg.evaluate("closeWin()")
        pg.keyboard.press("ArrowLeft"); pg.wait_for_timeout(500)
        check(nj() == n, "창을 닫아도 키보드 조그는 꺼진 채(카드를 다시 눌러야 켜짐)")
        # (e) 탭이 바뀌면 꺼짐
        pg.click(KBD_ON)
        pg.click('#tabs .tab[data-tab="results"]'); pg.wait_for_timeout(300)
        pg.click('#tabs .tab[data-tab="measure"]'); pg.wait_for_timeout(300)
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(500)
        check(nj() == n, "탭을 바꿨다 돌아오면 꺼져 있음")
        # (f) 창 포커스를 잃으면 꺼짐
        pg.click(KBD_ON)
        pg.evaluate("window.dispatchEvent(new Event('blur'))")
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(500)
        check(nj() == n, "창 포커스를 잃으면 꺼짐")
        # (g) 입력 칸에서는 안 됨
        pg.click(KBD_ON)
        pg.click("#m-x")
        pg.keyboard.press("ArrowUp"); pg.wait_for_timeout(400)
        check(nj() == n, "X 입력 칸에서는 ↑ 키가 조그를 보내지 않음")
        # (h) 비상정지 때 누른 채인 키: 뗄 때까지 다시 시작하지 않음(자동 반복 포함)
        pg.click(KBD_ON)
        pg.keyboard.down("PageUp")                     # Z↑ (0 이라 거절되지만 보냄 시도는 센다)
        pg.keyboard.up("PageUp")
        n = nj()
        kbd_hold(pg, "ArrowUp", 0.6)
        n_hold = nj() - n
        check(n_hold >= 1, "키를 누르고 있으면 조그가 나감 (%d 번)" % n_hold)
        pg.locator("#estop").click()
        n = nj()
        kbd_hold(pg, "ArrowUp", 1.2)
        check(nj() == n, "비상정지 뒤에도 키를 누르고 있으면 다시 시작하지 않음 (%d → %d)" % (n, nj()))
        pg.keyboard.up("ArrowUp")
        pg.wait_for_timeout(200)
        c.pump(1.5)
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:2])
    finally:
        c.close()


def run_pad(br):
    c = e2e.Client(patch=PATCH)
    try:
        pg = page(br, c)
        pg.click('#tab-measure .jstep[data-v="1"]')
        up = pg.locator('#tab-measure .jogpad .btn:has-text("↑")').first
        b = up.bounding_box()
        cx, cy = b["x"] + b["width"] / 2, b["y"] + b["height"] / 2

        def nj():
            return len(jogs(pg))

        # 비상정지 때 누른 채인 패드: 손을 떼기 전까지 더 안 나감
        pg.mouse.move(cx, cy); pg.mouse.down(); pg.wait_for_timeout(900)
        n_before = nj()
        check(n_before >= 2, "패드를 0.5 s 넘게 누르면 연속으로 나감 (%d 번)" % n_before)
        pg.locator("#estop").dispatch_event("pointerdown", {"button": 0})
        pg.wait_for_timeout(300)
        n = nj()
        pg.wait_for_timeout(900)
        check(nj() == n, "비상정지 뒤 누른 채인 패드는 더 보내지 않음 (%d → %d)" % (n, nj()))
        pg.mouse.up()
        c.pump(1.5)
        for ax in ("z", "x", "y"):
            c.send("origin", axis=ax)
        # pointerleave 로 멈춤: 누른 채 버튼 밖으로
        pg.mouse.move(cx, cy); pg.mouse.down(); pg.wait_for_timeout(900)
        pg.mouse.move(cx, cy + 300); pg.wait_for_timeout(300)
        n = nj()
        pg.wait_for_timeout(900)
        check(nj() <= n + 1, "누른 채 버튼 밖으로 나가면 멈춤 (%d → %d)" % (n, nj()))
        pg.mouse.up()
        # 탭 숨김
        pg.mouse.move(cx, cy); pg.mouse.down(); pg.wait_for_timeout(900)
        pg.evaluate("Object.defineProperty(document, 'hidden', { value: true, configurable: true }); document.dispatchEvent(new Event('visibilitychange'))")
        pg.wait_for_timeout(300)
        n = nj()
        pg.wait_for_timeout(900)
        check(nj() <= n + 1, "탭이 가려지면 멈춤 (%d → %d)" % (n, nj()))
        pg.mouse.up()
        pg.evaluate("Object.defineProperty(document, 'hidden', { value: false, configurable: true })")
        # 서버 연결이 끊기면 대기 중인 명령이 실패로 끝나고 다시 연결 뒤 패드가 산다
        pg.mouse.move(cx, cy); pg.mouse.down(); pg.wait_for_timeout(700)
        pg.evaluate("UI.ws.close()")
        pg.wait_for_timeout(300)
        pg.mouse.up()
        pg.wait_for_timeout(2500)
        check(pg.evaluate("JOG.inFlight === false && JOG.held === null"), "서버가 끊긴 뒤 조그 상태 초기화")
        check(pg.evaluate("UI.connected"), "자동으로 다시 연결")
        c.pump(0.5)
        for ax in ("z", "x", "y"):
            c.send("origin", axis=ax)
        pg.click('#tab-measure .jogpad .btn:has-text("↓")')
        n = nj()
        pg.click('#tab-measure .jogpad .btn:has-text("↓")')
        pg.wait_for_timeout(600)
        check(nj() == n + 1, "다시 연결 뒤 패드가 정상(클릭 한 번 = 한 번) (%d → %d)" % (n, nj()))
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:2])
    finally:
        c.close()


def run_z(br):
    c = e2e.Client(patch=PATCH)
    try:
        pg = page(br, c)

        def pulses(prefix):
            return [x for x in serial_cmds(c) if x.startswith(prefix)]

        # 클릭 한 번 = 정확히 한 스텝(펄스)
        pg.click('#tab-measure .jstep[data-v="1"]')
        n0 = len(pulses("jx"))
        pg.click('#tab-measure .jogpad .btn:has-text("↑")')
        pg.wait_for_timeout(900)
        px = pulses("jx")
        check(len(px) == n0 + 1 and px[-1] in ("jx 160", "jx -160"), "X 스텝 1 mm 클릭 한 번 = %s (160 펄스 한 번)" % (px[-1:],))
        # 큰 스텝(10): 누르고 있어도 한 번
        pg.click('#tab-measure .jstep[data-v="10"]')
        n0 = len(pulses("jx"))
        up = pg.locator('#tab-measure .jogpad .btn:has-text("↑")').first
        b = up.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2); pg.mouse.down(); pg.wait_for_timeout(1500); pg.mouse.up()
        pg.wait_for_timeout(600)
        check(len(pulses("jx")) == n0 + 1, "스텝 10 mm 를 1.5 s 누르고 있어도 한 번만 (%d 번)" % (len(pulses("jx")) - n0))
        # 키보드 ↓ 자동 반복도 한 번
        pg.click(KBD_ON)
        n0 = len(pulses("jx"))
        kbd_hold(pg, "ArrowDown", 1.2); pg.keyboard.up("ArrowDown"); pg.wait_for_timeout(600)
        check(len(pulses("jx")) == n0 + 1, "스텝 10 mm 키보드 자동 반복도 한 번만 (%d 번)" % (len(pulses("jx")) - n0))
        # 스텝 1: 누르고 있으면 연속
        pg.click('#tab-measure .jstep[data-v="1"]')
        n0 = len(pulses("jx"))
        b = pg.locator('#tab-measure .jogpad .btn:has-text("↑")').first.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2); pg.mouse.down(); pg.wait_for_timeout(1500); pg.mouse.up()
        pg.wait_for_timeout(600)
        check(len(pulses("jx")) - n0 >= 3, "스텝 1 mm 는 누르고 있으면 연속 (%d 번)" % (len(pulses("jx")) - n0))
        # Z: 스텝이 따로 · 클릭 한 번 = Z 스텝
        zs = pg.locator("#tab-measure .jzstep")
        check(zs.count() >= 3, "Z 스텝 선택이 따로 있음 (%d 개)" % zs.count())
        pg.click('#tab-measure .jzstep[data-v="0.5"]')
        n0 = len(pulses("jz"))
        pg.click('#tab-measure .jogpad .btn:has-text("Z ↓")')
        pg.wait_for_timeout(700)
        pz = pulses("jz")
        check(len(pz) == n0 + 1 and pz[-1] == "jz 80", "Z 스텝 0.5 mm 클릭 한 번 = jz 80 (%s)" % (pz[-1:],))
        # Z↓ 를 누르고 있어도 한 스텝, PageDown 도
        n0 = len(pulses("jz"))
        b = pg.locator('#tab-measure .jogpad .btn:has-text("Z ↓")').first.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2); pg.mouse.down(); pg.wait_for_timeout(1500); pg.mouse.up()
        pg.wait_for_timeout(600)
        check(len(pulses("jz")) == n0 + 1, "Z↓ 를 1.5 s 누르고 있어도 한 스텝 (%d 번)" % (len(pulses("jz")) - n0))
        n0 = len(pulses("jz"))
        pg.click(KBD_ON)
        kbd_hold(pg, "PageDown", 1.2); pg.keyboard.up("PageDown"); pg.wait_for_timeout(600)
        check(len(pulses("jz")) == n0 + 1, "PageDown 자동 반복도 한 스텝 (%d 번)" % (len(pulses("jz")) - n0))
        # Z↑ 연속은 됨(스텝 0.1)
        pg.click('#tab-measure .jzstep[data-v="0.1"]')
        n0 = len(pulses("jz"))
        b = pg.locator('#tab-measure .jogpad .btn:has-text("Z ↑")').first.bounding_box()
        pg.mouse.move(b["x"] + b["width"] / 2, b["y"] + b["height"] / 2); pg.mouse.down(); pg.wait_for_timeout(1500); pg.mouse.up()
        pg.wait_for_timeout(600)
        check(len(pulses("jz")) - n0 >= 2, "Z↑ 는 누르고 있으면 연속 (%d 번)" % (len(pulses("jz")) - n0))
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:2])
    finally:
        c.close()


def main():
    which = sys.argv[1:] or ["latency", "keys", "pad", "z"]
    with sync_playwright() as p:
        br = p.chromium.launch()
        try:
            for w in which:
                print("== %s" % w, flush=True)
                {"latency": run_latency, "keys": run_keys, "pad": run_pad, "z": run_z}[w](br)
        finally:
            br.close()
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    sys.exit(1 if e2e.FAILS else 0)


if __name__ == "__main__":
    main()
