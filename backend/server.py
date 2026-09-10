"""server.py - FastAPI 서버.

FastAPI 앱 + 정적 서빙(/ , /css, /js) + 프레임(/frame/<id>.jpg) + WebSocket(/ws)
+ lifespan. 주기 태스크는 loops.py, 창은 window.py 가 담당한다.

진입점은 루트의 run.py 다 (python run.py [--dry] [--image P] [--no-window] [--port N]).
"""

import argparse
import contextlib
import json
import os
import sys

# 창 전용 exe 는 콘솔이 없어 sys.stdout 이 None 이다. 라이브러리가 .isatty() 를
# 부르는 순간 죽으므로 다른 import 보다 먼저 devnull 로 갈아끼운다.
if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

# backend 모듈끼리는 이름으로(import engine), core 는 패키지로(from core import calib)
# import 한다. 둘 다 찾을 수 있게 루트와 backend 를 경로에 넣는다.
_BACKEND_DIR = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND_DIR)
for p in (_ROOT, _BACKEND_DIR):
    if p not in sys.path:
        sys.path.insert(0, p)

import asyncio                                                   # noqa: E402

from fastapi import FastAPI, WebSocket, WebSocketDisconnect       # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response  # noqa: E402
from fastapi.staticfiles import StaticFiles                        # noqa: E402

import commands                                                    # noqa: E402
import logger                                                      # noqa: E402
import loops                                                       # noqa: E402
from core import paths                                                  # noqa: E402
import stagectl                                                    # noqa: E402
import version                                                     # noqa: E402
import vision                                                      # noqa: E402
import window                                                      # noqa: E402
from commands import handle_command                                # noqa: E402
from connection import manager, set_loop                           # noqa: E402
from state import state                                            # noqa: E402

HOST = "127.0.0.1"
PORT = 8000

commands.set_shutdown_handler(window.request_shutdown)


@contextlib.asynccontextmanager
async def lifespan(_app):
    logger.configure(state.settings)
    # 워커 스레드(시리얼·미리보기)가 화면으로 로그를 보낼 통로.
    set_loop(asyncio.get_running_loop())
    # 이중 실행 방지는 창 모드에서만이다. uvicorn 으로 직접 띄우거나 --no-window 로
    # 여러 개를 띄우는 것은 개발·검증 경로라 막지 않는다(_acquire_single_instance
    # 가 창 모드가 아니면 항상 True 를 준다).
    if not window._acquire_single_instance():
        msg = "프로그램이 이미 실행 중입니다 · 백엔드 기동 중단"
        print("[error] %s" % msg, flush=True)
        logger.write("err", msg)
        os._exit(1)
    logger.write("info", "%s v%s (%s)" % (version.APP_NAME, version.APP_VERSION,
                                          version.BUILD_DATE))
    state.startup_notices.clear()

    def notice(msg, level="warn"):
        logger.write(level, msg)
        state.startup_notices.append({"msg": msg, "level": level})

    for lv, msg in logger.drain_early():
        state.startup_notices.append({"msg": msg, "level": lv})
    paths.ensure_data_dirs()
    if paths.DATA_DIR_ERROR:
        notice(paths.DATA_DIR_ERROR)
    ok_w, why = paths.check_writable()
    if not ok_w:
        notice("데이터 폴더에 쓸 수 없습니다: %s (%s) - 촬영 결과가 저장되지 않습니다"
               % (paths.DATA_DIR, why))
    if stagectl.ctl.dry:
        # --dry 는 시리얼이 없으므로 붙일 포트를 고를 것도 없다. 바로 연결해 둔다
        # (검증 하네스가 하드웨어 없이 전 흐름을 돌 수 있게).
        with contextlib.suppress(Exception):
            await commands.handle_command({"cmd": "stage_connect"})
    commands.start_preview()               # 미리보기는 기동과 함께 돈다
    tasks = loops.start_all()
    try:
        yield
    finally:
        await loops.stop_all(tasks)
        with contextlib.suppress(Exception):
            await stagectl.ctl.disconnect()
        stagectl.ctl.shutdown()
        # vision.shutdown 은 미리보기 스레드 join 을 기다린다 - 루프를 막지 않는다.
        with contextlib.suppress(Exception):
            await asyncio.get_running_loop().run_in_executor(None, vision.shutdown)


app = FastAPI(lifespan=lifespan)

_ASSET_FILES = [os.path.join(paths.FRONTEND_DIR, "css", "style.css")] + [
    os.path.join(paths.FRONTEND_DIR, "js", f)
    for f in ("core.js", "camera.js", "map.js", "samples.js", "sequence.js", "app.js")]


def _asset_version():
    """자산 URL 에 붙일 값 - 파일을 고치면 바뀐다(WebView2 가 옛 파일을 재사용하는
    '코드는 최신인데 화면은 과거' 사고 차단)."""
    ts = []
    for f in _ASSET_FILES:
        with contextlib.suppress(OSError):
            ts.append(os.path.getmtime(f))
    return "%s-%d" % (version.APP_VERSION, int(max(ts)) if ts else 0)


@app.get("/")
async def root():
    try:
        with open(paths.INDEX_PATH, encoding="utf-8") as fh:
            html = fh.read().replace("__ASSET_V__", _asset_version())
    except OSError as e:
        return HTMLResponse("<h1>index.html 을 읽을 수 없습니다</h1><p>%s</p>" % e,
                            status_code=500)
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return JSONResponse({"ok": True, "version": version.APP_VERSION})


@app.get("/preview.jpg")
async def preview():
    """미리보기 최신 한 장. 화면이 250ms 마다 다시 받아 가므로 캐시하면 안 된다."""
    jpg = vision.holder.preview_jpeg()
    if not jpg:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-store"})


@app.get("/frame/{name}")
async def frame(name):
    """최근 프레임 JPEG. 캐시하면 새 촬영이 화면에 반영되지 않는다."""
    fid = name.split(".")[0]
    jpg = vision.frame_jpeg(fid)
    if jpg is None:
        return Response(status_code=404)
    return Response(jpg, media_type="image/jpeg",
                    headers={"Cache-Control": "no-cache"})


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await manager.connect(ws)
    try:
        while True:
            raw = await ws.receive_text()
            try:
                data = json.loads(raw)
            except Exception:              # noqa: BLE001
                continue
            if isinstance(data, dict) and "cmd" in data:
                await handle_command(data)
    except WebSocketDisconnect:
        manager.disconnect(ws)
    except Exception:                      # noqa: BLE001
        manager.disconnect(ws)


for _sub in ("css", "js"):
    _d = os.path.join(paths.FRONTEND_DIR, _sub)
    if os.path.isdir(_d):
        app.mount("/" + _sub, StaticFiles(directory=_d), name=_sub)
    else:
        print("[error] 정적 폴더 없음: %s" % _d)
        logger.early("err", "정적 폴더 없음: %s" % _d)


def main(argv=None):
    ap = argparse.ArgumentParser(description="샘플 자동 측정 - 통합 서버")
    ap.add_argument("--dry", action="store_true", help="시리얼 없이 실행(이동은 흉내)")
    ap.add_argument("--image", help="촬영 대신 이 사진을 쓴다")
    ap.add_argument("--no-window", action="store_true", help="창 없이 서버만")
    ap.add_argument("--port", type=int, default=PORT)
    a = ap.parse_args(argv)

    stagectl.ctl.dry = bool(a.dry)
    if a.dry:
        logger.early("info", "--dry: 시리얼 없이 실행합니다(이동은 흉내)")
    if a.image:
        vision.set_image_override(os.path.abspath(a.image))
        logger.early("info", "--image: %s" % a.image)
    window.run(app, HOST, a.port, no_window=a.no_window)


if __name__ == "__main__":
    main()
