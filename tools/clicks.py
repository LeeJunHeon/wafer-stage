"""clicks.py - 클릭 수 시험(playwright): 장비 설정이 끝난 상태에서 실행 → 촬영 → 측정 시작이 클릭 2번 + 확인 대화상자 1번 이하인가.

  python tools/clicks.py
"""
import os
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
import e2e  # noqa: E402
import e2e_cases as ec  # noqa: E402,F401
from playwright.sync_api import sync_playwright  # noqa: E402


def main():
    c = e2e.Client(patch={"sim": {"time_scale": 20.0}, "condition": {"active": "가열"}})           # 가열 측정이 몇 초 걸리게(화면이 측정 중 상태를 본다)
    clicks, dialogs = [0], [0]
    try:
        e2e.full_setup(c)                                # 장비 설정이 끝난 상태(설정 · 보정 파일은 데이터 폴더에 남는다)
        with sync_playwright() as p:
            br = p.chromium.launch()
            pg = br.new_page(viewport={"width": 1920, "height": 1040})
            pg.goto("http://127.0.0.1:%d/" % c.port)       # 프로그램 실행
            pg.wait_for_timeout(2000)

            def click(sel):
                pg.click(sel)
                clicks[0] += 1

            click('[data-act="capture"]')                  # 1) 촬영
            pg.wait_for_function("document.querySelectorAll('.sgrid .sc').length >= 9", timeout=90000)
            pg.wait_for_timeout(500)
            click('[data-act="mstart"]')                   # 2) 측정 시작
            pg.wait_for_timeout(800)
            if pg.query_selector("#win:not(.hidden) .btn.primary"):
                dialogs[0] += 1
                click("#win:not(.hidden) .btn.primary")
            pg.wait_for_function("document.querySelector('#pill') && document.querySelector('#pill').textContent.startsWith('측정 중')", timeout=30000)
            pg.screenshot(path=os.path.join(ROOT, "..", "shots_v2", "clicks_running.png"))
            br.close()
    finally:
        c.close()
    print("클릭 %d번 · 확인 대화상자 %d개" % (clicks[0], dialogs[0]))
    ok = clicks[0] - dialogs[0] <= 2 and dialogs[0] <= 1
    print("클릭 수 목표 %s" % ("통과" if ok else "실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
