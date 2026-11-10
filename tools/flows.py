"""flows.py - 화면 흐름 시험(playwright, 실제 버튼 클릭. 서버 명령을 직접 부르지 않는다).

  python tools/flows.py [출력 폴더]

흐름 A: 첫 실행(원점 없음) → 띠의 [Z 원점 잡기] [X 원점 잡기] [Y 원점 잡기] → (설정 3–6 은 준비 도우미) → 장비 설정 7 접촉 높이 · 8 릴레이 → 촬영 → 측정 시작
흐름 B: 비상정지 회복(헤드 위치 보정 있음)  - 띠 버튼만
흐름 C: 비상정지 회복(헤드 위치 보정 없음)  - 띠 버튼만
흐름 D: 모든 경보 · 잠금 상태에 다음 동작 버튼이 있는가(막다른 상태 없음)
설정 3–6(판 영역 · 빈 판 사진 · 좌표 보정 · 헤드 위치 보정)은 사진 위 정밀 클릭이라 e2e 도우미(서버 명령)로 준비한다 - 이 부분만 예외다.
"""
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
import e2e_cases as ec  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, "..", "shots_v2")
NOHOME = {"WAFER_STAGE_DRY_NO_HOME": "1", "WAFER2_FAKE_EEPROM": "none"}


class Page:
    def __init__(self, c, p):
        self.c = c
        self.br = p.chromium.launch()
        self.pg = self.br.new_page(viewport={"width": 1920, "height": 1040})
        self.errs = []
        self.pg.on("console", lambda m: self.errs.append(m.text) if m.type == "error" else None)
        self.pg.on("pageerror", lambda e: self.errs.append(str(e)))
        self.pg.goto("http://127.0.0.1:%d/" % c.port)
        self.pg.wait_for_timeout(1500)
        self.clicks = 0

    def shot(self, name):
        os.makedirs(OUT, exist_ok=True)
        self.pg.screenshot(path=os.path.join(OUT, name + ".png"))

    def band_buttons(self):
        return [b.inner_text() for b in self.pg.query_selector_all("#band .btn")]

    def band_text(self):
        el = self.pg.query_selector("#band:not(.hidden)")
        return el.inner_text() if el else ""

    def click_band(self, label):
        for b in self.pg.query_selector_all("#band .btn"):
            if b.inner_text() == label:
                b.click()
                self.clicks += 1
                return True
        return False

    def confirm_dialog(self, label):
        """질문 대화상자의 동작 버튼을 누른다. 원점 잡기는 펌웨어 위치가 없으면(X · Y) "끝단 10 mm 안까지 옮겼습니까?" 로 묻고 [옮겼음 · 원점 잡기](C2)."""
        self.pg.wait_for_selector("#win:not(.hidden)", timeout=4000)
        self.last_dialog = self.pg.inner_text("#win")
        labels = (label, "옮겼음 · 원점 잡기") if label.endswith("원점 잡기") else (label,)
        for b in self.pg.query_selector_all("#win .btn"):
            if b.inner_text() in labels:
                b.click()
                self.clicks += 1
                self.pg.wait_for_timeout(450)                   # 창이 닫힌 자리는 400 ms 누름 무시(더블클릭 방지)
                return True
        return False

    def wait_band(self, pred, timeout=30):
        t0 = time.time()
        while time.time() - t0 < timeout:
            if pred(self.band_buttons()):
                return True
            self.pg.wait_for_timeout(250)
        return False

    def press_recovery(self, labels, dialog_for=()):
        done = []
        for lab in labels:
            ok = self.wait_band(lambda bs, lab=lab: lab in bs, 40)
            if not ok:
                done.append("(없음:%s)" % lab)
                break
            self.click_band(lab)
            if lab in dialog_for:
                self.confirm_dialog(lab)
            done.append(lab)
            self.pg.wait_for_timeout(600)
        return done

    def close(self):
        self.br.close()


def flow_a(p):
    c = e2e.Client(patch={"sim": {"time_scale": 200.0}}, env=NOHOME)
    try:
        pg = Page(c, p)
        pg.shot("a1_first_run_band")
        check("Z 원점 잡기" in pg.band_buttons() and "원점 없음" in pg.band_text(), "첫 실행 · 띠에 [Z 원점 잡기] (%s)" % pg.band_buttons())
        done = pg.press_recovery(["Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"], dialog_for=("Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"))
        pg.pg.wait_for_timeout(800)
        check(done == ["Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"], "버튼으로 Z → X → Y 원점 %s" % done)
        check("끝단 10 mm 안까지 옮겼습니까" in (getattr(pg, "last_dialog", "") or ""), "펌웨어 위치가 없는 X · Y 는 확인 창이 질문으로 묻는다 (%s)" % (getattr(pg, "last_dialog", "") or "")[:40])
        check(not pg.band_text(), "원점 완료 · 띠 사라짐")
        # 장비 설정 1: 원점 상태 표시
        pg.pg.click('#tabs .tab[data-tab="setup"]')
        pg.pg.wait_for_timeout(600)
        pg.shot("a2_setup_stage")
        txt = pg.pg.inner_text("#tab-setup")
        check("원점 정상" in txt or "있음" in txt, "장비 설정 1 · 축별 원점 표시")
        t = e2e.full_setup(c)                         # 설정 3–6 준비(도우미)
        pg.pg.reload()
        pg.pg.wait_for_timeout(1500)
        pg.pg.click('#tabs .tab[data-tab="measure"]')
        pg.pg.click('[data-act="capture"]')
        pg.clicks += 1
        pg.pg.wait_for_function("UI.state && UI.state.samples.length >= 9", timeout=90000)
        # 장비 설정 7 접촉 높이: 샘플 위로 → Z 내리기(0.1 mm 조그 버튼) → 저장
        pg.pg.click('#tabs .tab[data-tab="setup"]')
        pg.pg.click('.sitem >> nth=6')
        pg.pg.wait_for_timeout(700)
        pg.shot("a3_setup_contact")
        pg.pg.click('[data-act="su-ct-goto"]')
        pg.pg.wait_for_timeout(1500)
        pg.pg.click('#tab-setup .jzstep[data-v="1"]')                    # Z 스텝은 따로(1 mm) - Z↓ 는 한 번에 한 스텝
        for _ in range(7):
            pg.pg.click('#tab-setup .jogpad .btn:has-text("Z ↓")')      # Z↓
            pg.pg.wait_for_timeout(450)
        try:
            pg.pg.wait_for_selector('[data-act="su-ct-save"]', timeout=6000)
        except Exception:
            pg.shot("a_dbg_ct_save")
            print("  (접촉 높이 저장 버튼 비활성) " + pg.pg.inner_text("#tab-setup")[-600:].replace(chr(10), " | "), flush=True)
            print("  (state) z=%s locked=%s samples=%s" % (c.pump(0.3)["stage"]["z"], c.state["stage"]["locked"], len(c.state["samples"])), flush=True)
            raise
        pg.pg.click('[data-act="su-ct-save"]')
        pg.pg.wait_for_timeout(800)
        z = c.pump(0.5)["contact"]["z"]
        check(z is not None and z > 5, "화면 버튼으로 접촉 높이 저장 (Z %s)" % z)
        pg.pg.click('[data-act="su-ct-try"]')
        pg.pg.wait_for_timeout(1500)
        pg.shot("a4_contact_saved")
        pg.pg.click('[data-act="su-ztop"]')
        # 장비 설정 8 릴레이 [2 s 켜 보기]
        pg.pg.click('.sitem >> nth=7')
        pg.pg.wait_for_timeout(600)
        pg.shot("a5_setup_relays")
        pg.pg.click('[data-act="su-rl-test"] >> nth=0')
        pg.pg.wait_for_timeout(2600)
        check("r 1 1" in "|".join(ec.serial_cmds(c)) and "r 1 0" in "|".join(ec.serial_cmds(c)), "릴레이 1 을 켰다 끔")
        # 측정
        pg.pg.click('#tabs .tab[data-tab="measure"]')
        pg.pg.click('[data-act="m-target"][data-args*="samples"]')     # 측정할 곳 = 샘플
        pg.pg.wait_for_timeout(800)
        pg.pg.click('[data-act="mstart"]')
        pg.clicks += 1
        pg.pg.wait_for_timeout(800)
        if pg.pg.query_selector("#win:not(.hidden) .btn.primary"):
            pg.pg.click("#win:not(.hidden) .btn.primary")
            pg.pg.wait_for_timeout(450)
        pg.pg.wait_for_function("document.querySelector('#pill') && /측정 중|대기/.test(document.querySelector('#pill').textContent)", timeout=30000)
        pg.pg.wait_for_timeout(1500)
        pg.shot("a6_measure_running")
        ec.wait_run(c, 200)
        pg.pg.wait_for_timeout(800)
        check(c.state["run"]["state"] == "done", "측정 끝 (%s)" % c.state["run"]["state"])
        pg.pg.click('#tabs .tab[data-tab="results"]')
        pg.pg.wait_for_timeout(1500)
        pg.shot("a7_results")
        check("접촉 Z" in pg.pg.inner_text("#tab-results") and "온도" not in pg.pg.inner_text("#tab-results"), "결과 표 = 접촉 Z (온도 열 없음)")
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:3])
        pg.close()
    finally:
        c.close()


def flow_recover(p, with_head, tag):
    c = e2e.Client(patch={"sim": {"time_scale": 200.0}})
    try:
        if with_head:
            e2e.full_setup(c)
        else:
            t = e2e.truth_px(c)
            e2e.setup_area_ref(c, t)
            e2e.setup_calib(c, t)
        pg = Page(c, p)
        pg.pg.click("#estop")
        pg.pg.wait_for_timeout(1200)
        pg.shot("%s1_estop_band" % tag)
        want = ["Z 원점 잡기", "위치 확인", "적용"] if with_head else ["Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"]
        check(pg.band_buttons()[:1] == ["Z 원점 잡기"], "비상정지 · 띠 버튼 %s" % pg.band_buttons())
        done = pg.press_recovery(want, dialog_for=("Z 원점 잡기", "X 원점 잡기", "Y 원점 잡기"))
        pg.pg.wait_for_timeout(800)
        pg.shot("%s2_recovered" % tag)
        check(done == want, "띠 버튼만으로 회복 %s" % done)
        check(not pg.band_text() and not c.pump(0.5)["stage"]["locked"], "회복 뒤 띠 없음 · 잠금 해제")
        pg.close()
    finally:
        c.close()


def flow_dead_ends(p):
    """모든 경보 · 잠금 상태에서 띠에 다음 동작 버튼이 있는가."""
    c = e2e.Client(patch={"sim": {"time_scale": 3.0}})
    try:
        t = e2e.full_setup(c)
        c.send("capture")
        pg = Page(c, p)
        states = []

        def audit(name):
            pg.pg.wait_for_timeout(900)
            bs = pg.band_buttons()
            states.append((name, bs))
            check(len(bs) >= 1, "%s · 띠 버튼 %s" % (name, bs))
            pg.shot("d_" + name.replace(" ", "_"))

        c.http("/estop", "POST")
        audit("비상정지")
        ec.recover(c)
        c.dry("reboot")
        audit("재부팅")
        ec.recover(c)
        c.dry("fault", name="step_exc", on=True)
        ec.start_run(c, ec.TOUCH, nos=[2])
        ec.wait_run(c, 60)
        audit("측정 오류")
        c.dry("fault", name="step_exc", on=False)
        pg.pg.wait_for_timeout(300)
        pg.click_band("확인")
        pg.pg.wait_for_timeout(600)
        check(not pg.band_text(), "[확인] 으로 오류 띠 닫힘")
        c.dry("world", kw={"shift_px": [150.0, 60.0]})
        c.send("capture")
        audit("카메라 위치")
        pg.click_band("다시 촬영")
        pg.pg.wait_for_timeout(1500)
        c.dry("world", kw={"shift_px": [0.0, 0.0]})
        c.send("capture")
        pg.pg.wait_for_timeout(500)
        check(not pg.band_text(), "카메라 위치 복귀 · 띠 닫힘")
        if e2e.MODE == "fake":
            ec.start_run(c, ec.HEAT, nos=[1])
            c.wait(lambda s: s["relays"] and s["relays"]["heater_on"], 60)
            c.dry("cut", s=14.0)
            ec.wait_run(c, 90)
            audit("통신 끊김")
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:3])
        print("  막다른 상태 점검: " + " · ".join("%s=%s" % (n, "/".join(b)) for n, b in states))
        pg.close()
    finally:
        c.close()


def flow_dblclick(p):
    """사진 빈 곳을 더블클릭하면 샘플이 추가되고(Ctrl+Z 로 되돌림), 샘플을 끌면 위치가 바뀐다."""
    c = e2e.Client(patch={"sim": {"time_scale": 200.0}})
    try:
        e2e.full_setup(c)
        c.send("capture")
        pg = Page(c, p)
        pg.pg.click('#tabs .tab[data-tab="measure"]')
        pg.pg.click('[data-act="m-mode"][data-args*="shot"]')          # 촬영 사진에서 더블클릭으로 추가
        pg.pg.wait_for_function("UI.state && UI.state.samples.length >= 9", timeout=30000)
        pg.pg.wait_for_timeout(1200)
        n0 = len(c.pump(0.3)["samples"])
        pos = pg.pg.evaluate("""() => {
          const v = M.viewer, s = UI.state, a = s.calib.area; if (!a || !v.W) return null;
          const xs = a.map(p => p[0]), ys = a.map(p => p[1]);
          let best = null, bd = -1;
          for (let i = 0; i < 40; i++) for (let j = 0; j < 40; j++) {
            const u = Math.min(...xs) + (Math.max(...xs) - Math.min(...xs)) * (i + 0.5) / 40, w = Math.min(...ys) + (Math.max(...ys) - Math.min(...ys)) * (j + 0.5) / 40;
            if (!pointInPoly([u, w], a)) continue;
            const d = Math.min(...s.samples.map(p => Math.hypot(p.u - u, p.v - w)));
            if (d > bd) { bd = d; best = [u, w]; }
          }
          const r = v.el.getBoundingClientRect(), sc = UI.scale;
          return { x: r.left + (v.tx + best[0] * v.k) * sc, y: r.top + (v.ty + best[1] * v.k) * sc, d: bd * v.k };
        }""")
        check(pos is not None and pos["d"] > 20, "빈 자리 찾음 (샘플에서 %.0f px)" % (pos or {}).get("d", 0))
        pg.pg.mouse.dblclick(pos["x"], pos["y"])
        ok = c.wait(lambda s_: len(s_["samples"]) == n0 + 1, 8, "샘플 추가")
        check(ok, "빈 곳 더블클릭 → 샘플 추가 (%d → %d)" % (n0, len(c.state["samples"])))
        pg.shot("e1_dblclick_added")
        pg.pg.keyboard.press("Control+z")
        ok = c.wait(lambda s_: len(s_["samples"]) == n0, 8, "되돌리기")
        check(ok, "Ctrl+Z 로 되돌림 (%d)" % len(c.state["samples"]))
        check(not pg.errs, "콘솔 오류 없음 %s" % pg.errs[:3])
        pg.close()
    finally:
        c.close()


def main(out=None):
    global OUT
    if out:
        OUT = out
    flows = [("흐름 A 첫 실행", lambda p: flow_a(p)),
             ("흐름 B 비상정지 회복 · 헤드 위치 보정 있음", lambda p: flow_recover(p, True, "b")),
             ("흐름 C 비상정지 회복 · 헤드 위치 보정 없음", lambda p: flow_recover(p, False, "c")),
             ("흐름 D 막다른 상태 점검", lambda p: flow_dead_ends(p)),
             ("흐름 E 사진 빈 곳 더블클릭 → 샘플 추가", lambda p: flow_dblclick(p))]
    with sync_playwright() as p:
        for name, fn in flows:
            print("== " + name, flush=True)
            try:
                fn(p)
            except Exception:                                    # noqa: BLE001 - 한 흐름이 예외로 끝나도 실패로 세고 나머지를 돈다
                import traceback
                traceback.print_exc()
                check(False, "예외 · " + name)
    print("\n검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    for f in e2e.FAILS:
        print("  - " + f)
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
