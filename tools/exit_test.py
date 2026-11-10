"""exit_test.py - Windows 창 모드 --dry 실행 -> 종료 -> 남은 프로세스 없음 -> 바로 재실행. 3회 반복.

  python tools/exit_test.py [횟수]
임시 데이터 폴더 · 임시 설정 파일을 쓴다(실제 data_v2 · settings.json 을 건드리지 않는다).
확인: (1) 종료 명령 뒤 시간 안에 프로세스가 끝남 (2) 자식(msedgewebview2 등) 포함 남은 프로세스 없음 (3) 콘솔에 1411 오류 없음 (4) 바로 다시 실행됨.
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time

import psutil

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from websockets.sync.client import connect  # noqa: E402


def run_once(i):
    tmp = tempfile.mkdtemp(prefix="wafer2_exit_")
    st = os.path.join(tmp, "settings.json")
    json.dump({"width": 1280, "height": 960}, open(st, "w"))
    env = dict(os.environ, WAFER2_DATA=os.path.join(tmp, "data"), WAFER2_SETTINGS=st, PYTHONUTF8="1", WAFER2_SYNTH_W="1280", WAFER2_SYNTH_H="960")
    out = open(os.path.join(tmp, "out.txt"), "w", encoding="utf-8")
    t0 = time.time()
    p = subprocess.Popen([sys.executable, os.path.join(ROOT, "run.py"), "--dry"], env=env, stdout=out, stderr=subprocess.STDOUT, cwd=tmp)
    url = None
    while time.time() - t0 < 40:
        txt = open(os.path.join(tmp, "out.txt"), encoding="utf-8", errors="replace").read()
        m = re.search(r"서버 준비 완료 (http://\S+)", txt)
        if m:
            url = m.group(1)
            break
        if p.poll() is not None:
            break
        time.sleep(0.3)
    if not url:
        print("  실행 실패:", open(os.path.join(tmp, "out.txt"), encoding="utf-8", errors="replace").read()[-600:])
        return False, "시작 실패", tmp
    time.sleep(4)                                              # 창이 뜰 시간
    try:
        kids = [c for c in psutil.Process(p.pid).children(recursive=True)]
    except psutil.Error:
        kids = []
    names = sorted({c.name() for c in kids if c.is_running()})
    ws = connect(url.replace("http", "ws") + "ws", max_size=None)
    time.sleep(1.0)
    ws.send(json.dumps({"id": 1, "cmd": "shutdown", "args": {}}))
    t1 = time.time()
    ok_exit = False
    while time.time() - t1 < 25:
        if p.poll() is not None:
            ok_exit = True
            break
        time.sleep(0.2)
    dt = time.time() - t1
    time.sleep(1.5)
    left = []
    for c in kids:
        try:
            if c.is_running() and c.status() != psutil.STATUS_ZOMBIE:
                left.append("%s(%d)" % (c.name(), c.pid))
        except psutil.Error:
            pass
    if not ok_exit:
        subprocess.run(["taskkill", "/F", "/T", "/PID", str(p.pid)], capture_output=True)
    out.close()
    txt = open(os.path.join(tmp, "out.txt"), encoding="utf-8", errors="replace").read()
    has1411 = "1411" in txt or "Failed to unregister" in txt
    print("  %d회: 종료 %s (%.1f s) · 자식 %s · 남은 %s · 1411 %s" % (i, "정상" if ok_exit else "시간 초과", dt, names, left or "없음", "있음" if has1411 else "없음"))
    print("   출력:", [l for l in txt.splitlines() if "[exit]" in l])
    shutil.rmtree(tmp, ignore_errors=True)
    return ok_exit and not left and not has1411, "", tmp


def main(n):
    good = 0
    for i in range(1, n + 1):
        ok, why, _t = run_once(i)
        good += 1 if ok else 0
    print("창 모드 종료 %d / %d 통과" % (good, n))
    return 0 if good == n else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 3))
