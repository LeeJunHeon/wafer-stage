"""server.py - FastAPI 서버: 정적 화면 · 프레임 JPEG · WebSocket(/ws) · 비상정지 우회로(POST /estop).

서버가 상태의 주인이다. 화면은 요청만 보내고 state 메시지로만 바뀐다.
비상정지는 어떤 상태에서도 받는다: WS 와 HTTP 둘 다, 잠금을 기다리지 않는다.
"""

import argparse
import asyncio
import concurrent.futures
import contextlib
import json
import os
import sys
import threading
import time

if sys.stdout is None:
    sys.stdout = open(os.devnull, "w", encoding="utf-8")
if sys.stderr is None:
    sys.stderr = open(os.devnull, "w", encoding="utf-8")

_BACKEND = os.path.dirname(os.path.abspath(__file__))
_ROOT = os.path.dirname(_BACKEND)
for p in (_ROOT, _BACKEND):
    if p not in sys.path:
        sys.path.insert(0, p)

from fastapi import FastAPI, WebSocket, WebSocketDisconnect          # noqa: E402
from fastapi.responses import HTMLResponse, JSONResponse, Response    # noqa: E402
from fastapi.staticfiles import StaticFiles                           # noqa: E402

import logger                                                        # noqa: E402
import settings_schema as sch                                         # noqa: E402
import vision as vision_mod                                           # noqa: E402
import window                                                         # noqa: E402
from app import App, AppError                                         # noqa: E402
from core import paths                                                # noqa: E402

FRONT = os.path.join(_ROOT, "frontend")
HOST, PORT = "127.0.0.1", 8000

APP = None
HOLDER = {}
CLIENTS = set()
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=16, thread_name_prefix="cmd")
_LOOP = None
_ARGS = None


def _asset_v():
    ts = []
    for r, _d, fs in os.walk(FRONT):
        for f in fs:
            with contextlib.suppress(OSError):
                ts.append(os.path.getmtime(os.path.join(r, f)))
    return str(int(max(ts))) if ts else "0"


@contextlib.asynccontextmanager
async def lifespan(_app):
    global APP, _LOOP
    _LOOP = asyncio.get_running_loop()
    APP = App(dry=_ARGS.dry, image=_ARGS.image)
    logger.subscribe(lambda e: _LOOP.call_soon_threadsafe(lambda: asyncio.ensure_future(_bcast({"type": "log", "e": e}))))
    if _ARGS.dry:
        with contextlib.suppress(Exception):
            await asyncio.to_thread(APP.connect)
    try:
        APP.vision.open()
        APP.vision.start_preview()
    except Exception as e:                                           # noqa: BLE001
        logger.log("카메라 열기 실패 · " + logger.short(e), "warn", raw=repr(e))
    task = asyncio.create_task(_broadcaster())
    try:
        yield
    finally:
        task.cancel()
        if not APP.shutting:
            await asyncio.to_thread(APP.shutdown_cleanup)


app = FastAPI(lifespan=lifespan)


async def _bcast(msg):
    data = json.dumps(msg, ensure_ascii=False, default=_jd)
    for ws in list(CLIENTS):
        try:
            await ws.send_text(data)
        except Exception:                                            # noqa: BLE001
            CLIENTS.discard(ws)


def _jd(o):
    import numpy as np
    if isinstance(o, np.ndarray):
        return o.tolist()
    if isinstance(o, (np.floating, np.integer)):
        return o.item()
    if isinstance(o, (set, tuple)):
        return list(o)
    return str(o)


async def _broadcaster():
    last_v, last_t = -1, 0.0
    while True:
        await asyncio.sleep(0.1)
        if not CLIENTS or APP is None:
            continue
        now = time.time()
        if APP.version != last_v or now - last_t > 1.0:
            last_v, last_t = APP.version, now
            try:
                await _bcast({"type": "state", "state": APP.state(), "log": APP.log_tail()})
            except Exception as e:                                   # noqa: BLE001
                logger.log("상태 전송 실패 · " + logger.short(e), "warn")


@app.get("/")
async def root():
    with open(os.path.join(FRONT, "index.html"), encoding="utf-8") as f:
        html = f.read().replace("__V__", _asset_v())
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/health")
async def health():
    return JSONResponse({"ok": True})


@app.get("/schema")
async def schema():
    return JSONResponse({"schema": sch.public_schema(), "values": APP.cfg.values, "defaults": sch.defaults()}, headers={"Cache-Control": "no-store"})


def _img_resp(img, key):
    jpg = vision_mod.jpeg(img, 2000, 82) if img is not None else None
    if jpg is None:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-cache", "ETag": str(key)})


@app.get("/preview.jpg")
async def preview():
    if APP.vision.preview_jpg is None:
        return Response(status_code=204)
    return Response(APP.vision.preview_jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/shot.jpg")
async def shot_jpg():
    return _img_resp(APP.shot["img"] if APP.shot else None, APP.shot["id"] if APP.shot else 0)


@app.get("/ref.jpg")
async def ref_jpg():
    return _img_resp(APP.ref_img(), APP.ref_id())


@app.get("/snap.jpg")
async def snap_jpg():
    return _img_resp(APP.snap["img"] if APP.snap else None, APP.snap["id"] if APP.snap else 0)


@app.get("/refhist/{name}.jpg")
async def refhist(name):
    from core import imgio
    img = imgio.imread_u(os.path.join(paths.REF_HISTORY_DIR, name + ".png"))
    return _img_resp(img, name)


@app.get("/run/{name}/{file}")
async def run_file(name, file):
    fp = os.path.join(paths.RUNS_DIR, name, os.path.basename(file))
    if not os.path.exists(fp):
        return Response(status_code=404)
    if file.endswith(".json"):
        return JSONResponse(json.load(open(fp, encoding="utf-8")))
    return Response(open(fp, "rb").read(), media_type="image/jpeg" if file.endswith(".jpg") else "text/csv; charset=utf-8")


@app.post("/estop")
async def http_estop():
    """비상정지 우회로: WS 가 끊겨도 도착한다. 잠금을 기다리지 않는다."""
    APP.estop()
    return JSONResponse({"ok": True})


async def _run_cmd(msg, gen):
    cmd = msg.get("cmd")
    try:
        if cmd == "shutdown":
            return await _shutdown()
        r = await asyncio.get_running_loop().run_in_executor(POOL, APP.handle, cmd, msg.get("args") or {}, gen)
        return {"ok": True, "res": r}
    except AppError as e:
        logger.log(str(e), "warn")
        return {"ok": False, "msg": str(e)}
    except Exception as e:                                           # noqa: BLE001
        logger.log("명령 오류 · " + logger.short(e), "err", raw=repr(e))
        return {"ok": False, "msg": "오류 · " + logger.short(e, 40)}


async def _shutdown():
    try:
        APP.c_shutdown_check({}, 0)
    except AppError as e:
        return {"ok": False, "msg": str(e)}
    await asyncio.to_thread(APP.shutdown_cleanup)
    threading.Thread(target=lambda: (time.sleep(0.2), window.request_exit(HOLDER.get("server"))), daemon=True).start()
    return {"ok": True}


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    CLIENTS.add(ws)
    await ws.send_text(json.dumps({"type": "state", "state": APP.state(), "log": APP.log_tail(), "hello": True}, ensure_ascii=False, default=_jd))
    tasks = set()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("cmd") == "estop":
                APP.estop()                                          # 줄 세우지 않는다
                await ws.send_text(json.dumps({"type": "ack", "id": msg.get("id"), "ok": True}))
                continue
            if msg.get("cmd") == "preview":
                APP.vision.preview_on = bool(msg.get("on"))
                continue
            gen = APP.motion.gen if APP.motion else 0                # 명령이 들어온 순간의 세대

            async def go(m=msg, g=gen):
                r = await _run_cmd(m, g)
                with contextlib.suppress(Exception):
                    await ws.send_text(json.dumps(dict(r, type="ack", id=m.get("id")), ensure_ascii=False, default=_jd))
            t = asyncio.create_task(go())
            tasks.add(t)
            t.add_done_callback(tasks.discard)
    except (WebSocketDisconnect, RuntimeError):
        pass
    finally:
        CLIENTS.discard(ws)


app.mount("/static", StaticFiles(directory=FRONT), name="static")


def main(argv=None):
    global _ARGS
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--image", default=None)
    ap.add_argument("--no-window", action="store_true")
    ap.add_argument("--port", type=int, default=PORT)
    ap.add_argument("--cleanup", action="store_true")
    _ARGS = ap.parse_args(argv)
    if _ARGS.cleanup:
        n = window.cleanup_others()
        print("정리한 프로세스 %d개" % n)
        return 0

    def on_closing():
        """창 X: 화면에 종료 확인을 요청한다(측정 중이면 화면이 거절 사유를 보인다)."""
        def ask():
            ok = False
            try:
                ok = window.WINDOW.evaluate_js("(function(){if(window.requestExit){window.requestExit();return true}return false})()")
            except Exception:                                        # noqa: BLE001
                ok = False
            if not ok:
                if APP:
                    APP.shutdown_cleanup()
                window.request_exit(HOLDER.get("server"))
        threading.Thread(target=ask, daemon=True).start()
        return False

    HOLDER["on_closing"] = on_closing
    return window.run(app, HOST, _ARGS.port, _ARGS.no_window, HOLDER)


if __name__ == "__main__":
    sys.exit(main())
