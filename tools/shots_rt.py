"""shots_rt.py - R–T 측정 화면 흐름 + 스크린샷(playwright, 실제 클릭). 서버 명령은 준비(좌표 보정)에만 쓰고 측정 흐름은 전부 버튼 클릭이다.

  python tools/shots_rt.py [출력 폴더]

흐름 E: 측정 탭(이 위치 측정) → [장비 연결] → 샘플 고르기 → [샘플 위로] → [접촉 높이로 내리기] → [측정 시작] → 가열 · 냉각 그래프 확인 → 끝
        → 결과 탭 [R–T 측정 기록] → [그래프 보기] → 장비 설정 8 [연결 시험] → [중지] 흐름도 한 번.
"""
import json
import os
import sys
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
import e2e_rt  # noqa: E402
from e2e import check  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

OUT = os.path.join(ROOT, "..", "shots_v2")


def main(out=None):
    out = out or OUT
    os.makedirs(out, exist_ok=True)
    c = e2e.Client(patch=e2e_rt.patch(rt={"repeat": 2, "cooler_target": "sample", "file_path": ""}, sim={"time_scale": 14.0}))
    errs = []
    clicks = [0]
    try:
        t = e2e.full_setup(c)
        c.send("capture")
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1920, "height": 1040})
            pg.on("console", lambda m: errs.append(m.text) if m.type == "error" else None)
            pg.on("pageerror", lambda e: errs.append(str(e)))
            pg.goto("http://127.0.0.1:%d/" % c.port)
            pg.wait_for_timeout(1800)

            def shot(name, wait=500):
                pg.wait_for_timeout(wait)
                pg.screenshot(path=os.path.join(out, name + ".png"))
                print("shot", name, flush=True)

            def click(sel):
                pg.click(sel)
                clicks[0] += 1

            def state():
                return json.loads(pg.evaluate("JSON.stringify({rt: UI.state.rt, stage: UI.state.stage, phase: UI.state.phase})"))

            check("지금 위치" in pg.inner_text("#c-run") and "이 위치 측정" not in pg.inner_text("#tab-measure") and "샘플 순회" not in pg.inner_text("#tab-measure"), "측정 화면 하나 · 측정할 곳 [지금 위치 | 샘플 N개]")
            shot("r1_here_empty", 800)
            check("측정 장비 연결 안 됨" in pg.inner_text("#gate") and pg.locator("#gate").count() == 1, "시작 못 하는 사유가 한 곳에 한 줄로 보임 (%s)" % pg.inner_text("#gate"))
            click('#gate [data-act="inst_connect"]')
            pg.wait_for_function("UI.state.rt.inst.k2110 && UI.state.rt.inst.k2110.ok", timeout=8000)
            pg.wait_for_timeout(700)
            check("접촉 높이" in pg.inner_text("#gate"), "연결 뒤 다음 사유 = 접촉 높이 (%s)" % pg.inner_text("#gate"))
            # 샘플 고르기 → 샘플 위로 → 접촉 높이로 내리기
            pg.select_option("#c-pos select.msel", index=4)
            pg.wait_for_timeout(600)
            click('[data-act="m-goto"]')
            pg.wait_for_function("UI.state.phase === '대기' && UI.state.stage.x > 20", timeout=30000)
            pg.wait_for_timeout(900)
            click('[data-act="rt-down"]')
            pg.wait_for_function("UI.state.stage.z > 19", timeout=30000)
            pg.wait_for_timeout(1500)
            shot("r2_here_ready", 600)
            check(state()["rt"]["block"] == "", "시작 조건 충족 (%s)" % state()["rt"]["block"])
            # 측정 조건 창(설정 창): 저장 위치 · 반복 횟수를 고쳐 저장
            click('[data-act="m-cond"] >> nth=0')
            pg.wait_for_selector(".setwin .setrows", timeout=5000)
            shot("r3_here_conditions_open", 400)
            check("R–T 측정" in pg.inner_text(".setnav") and pg.locator(".setnav .ni.on").inner_text().strip() == "R–T 측정", "측정 조건 창 · 왼쪽 분류 목록에서 R–T 측정")
            pg.fill('.setrow[data-key="rt.file_path"] input', r"\\VanaM_NAS\VanaM_Measurement\VO2\VO2 data (NAS)\data")
            pg.press('.setrow[data-key="rt.file_path"] input', "Tab")
            pg.fill('.setrow[data-key="rt.batch_folder"] input', "6")
            pg.press('.setrow[data-key="rt.batch_folder"] input', "Tab")
            if not pg.evaluate("UI.state.settings.rt.date_folder"):
                pg.check('.setrow[data-key="rt.date_folder"] input')
            pg.fill('.setrow[data-key="rt.repeat"] input', "2")
            pg.press('.setrow[data-key="rt.repeat"] input', "Tab")
            pg.wait_for_timeout(600)
            check(pg.locator(".setrow.chg").count() >= 3, "저장 안 한 변경 표시 (%d)" % pg.locator(".setrow.chg").count())
            shot("r3b_here_conditions_k2450_save", 300)
            rg = pg.evaluate("[UI.rtRange([], []), UI.rtRange([12, 121], [5, 2e7])]")
            check(rg[0] == {"x": [20, 100], "y": [10, 1000000]} and rg[1]["x"] == [10, 125], "그래프 기본 범위 20–100 °C · 1E+1–1E+6 Ω, 넘으면 넓힘 (%s)" % (rg,))
            pg.fill('.setrow[data-key="rt.file_path"] input', "")                  # 이어지는 측정은 PC 에만 저장
            pg.fill('.setrow[data-key="rt.batch_folder"] input', "")
            pg.press('.setrow[data-key="rt.batch_folder"] input', "Tab")
            pg.wait_for_timeout(500)
            click('#set-save')
            pg.wait_for_function("UI.state.settings.rt.repeat === 2 && UI.state.settings.rt.file_path === ''", timeout=8000)
            pg.wait_for_timeout(500)
            pv = pg.inner_text("#m-path")
            check(pv.strip() == "PC 에만 저장", "저장 위치가 비면 PC 에만 저장 (%s)" % pv)
            click('[data-act="rt-start"]')
            check(True, "[측정 시작] 누름")
            pg.wait_for_function("UI.state.rt.run && UI.state.rt.run.phase === '가열' && UI.state.rt.run.cycle === 1", timeout=60000)
            pg.wait_for_timeout(4000)
            shot("r4_here_heating", 300)
            # 측정 중 조그 버튼은 막혀 있다
            cls = pg.get_attribute(".pad .btn >> nth=0", "class") or ""
            check("dis" in cls, "측정 중 조그 버튼 비활성 (%s)" % cls)
            pg.wait_for_function("UI.state.rt.run && UI.state.rt.run.cycle === 2 && UI.state.rt.run.phase === '냉각'", timeout=120000)
            pg.wait_for_timeout(2500)
            shot("r5_here_cooling_cycle2", 300)
            pg.wait_for_function("UI.state.rt.run && UI.state.rt.run.state === 'done'", timeout=240000)
            pg.wait_for_timeout(1500)
            shot("r6_here_done", 500)
            st = state()["rt"]["run"]
            check(st["status"] == "완료" and st["cycle"] == 2, "측정 완료 (%s)" % st["status"])
            # 결과 탭
            click('#tabs .tab[data-tab="results"]')
            pg.wait_for_timeout(800)
            if pg.query_selector('[data-act="r-kind"][data-args*="rt"]') and not pg.query_selector("#rt-table"):
                click('[data-act="r-kind"][data-args*="rt"]')
            pg.wait_for_selector("#rt-table", timeout=5000)
            shot("r7_results_rt", 500)
            txt = pg.inner_text("#rt-table")
            check("1-1_" in txt and "2 / 2" in txt and "완료" in txt, "R–T 측정 목록 (시각 · File name · 회차 · 결과)")
            click('[data-act="r-graph"]')
            pg.wait_for_selector("#g-view", timeout=5000)
            shot("r8_results_graph", 800)
            click("#win .btns .btn")
            # 장비 설정 8
            click('#tabs .tab[data-tab="setup"]')
            pg.click('.sitem >> nth=7')
            pg.wait_for_timeout(900)
            click('[data-act="su-inst-test"]')
            pg.wait_for_function("UI.state.rt.inst.test && UI.state.rt.inst.test.R", timeout=15000)
            shot("r9_setup8_instruments", 900)
            # 중지 흐름
            click('#tabs .tab[data-tab="measure"]')
            pg.wait_for_timeout(600)
            click('[data-act="rt-down"]')
            pg.wait_for_function("UI.state.stage.z > 19", timeout=30000)
            pg.wait_for_timeout(1500)
            click('[data-act="rt-start"]')
            pg.wait_for_function("UI.state.rt.run && UI.state.rt.run.state === 'running' && UI.state.rt.run.phase === '가열'", timeout=60000)
            click('[data-act="rt-stop"]')
            pg.wait_for_selector("#win:not(.hidden)", timeout=4000)
            shot("r10_stop_dialog", 300)
            for b in pg.query_selector_all("#win .btn"):
                if b.inner_text() == "중지":
                    b.click()
                    clicks[0] += 1
                    break
            pg.wait_for_function("UI.state.rt.run && UI.state.rt.run.state === 'stopped'", timeout=60000)
            pg.wait_for_timeout(1500)
            shot("r11_here_stopped", 300)
            check(not state()["rt"]["run"]["heater"], "중지 뒤 히트건 꺼짐")
            # 샘플 순회 쪽도 그대로 보인다
            click('#tabs .tab[data-tab="measure"]')
            click('[data-act="m-target"][data-args*="samples"]')
            pg.wait_for_timeout(1500)
            shot("r12_tour_after", 300)
            br.close()
    finally:
        c.close()
    print("클릭 %d · 콘솔 오류 %d" % (clicks[0], len(errs)))
    for e in errs[:20]:
        print("  ", e)
    check(not errs, "콘솔 오류 없음")
    print("검사 %d · 실패 %d" % (e2e.CHECKS[0], len(e2e.FAILS)))
    return 1 if e2e.FAILS else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1] if len(sys.argv) > 1 else None))
