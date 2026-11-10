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
POOL = concurrent.futures.ThreadPoolExecutor(max_workers=32, thread_name_prefix="cmd")      # 긴 명령 여럿이 풀을 다 차지해 조그 · 정지가 밀리지 않게 넉넉히
_LOOP = None
_ARGS = None


def _asset_v():
    ts = []
    for r, _d, fs in os.walk(FRONT):
        for f in fs:
            with contextlib.suppress(OSError):
                ts.append(os.path.getmtime(os.path.join(r, f)))
    return str(int(max(ts))) if ts else "0"


def _startup_link():
    APP.link_camera("start")
    APP.start_auto_link()


@contextlib.asynccontextmanager
async def lifespan(_app):
    global APP, _LOOP
    _LOOP = asyncio.get_running_loop()
    APP = App(dry=_ARGS.dry, image=_ARGS.image, fake=_ARGS.fake_serial)
    logger.subscribe(lambda e: _LOOP.call_soon_threadsafe(lambda: asyncio.ensure_future(_bcast({"type": "log", "e": e}))))
    # 스테이지는 서버가 뜨기 전에 한 번 연결해 본다(빠르다). 카메라 열기는 백그라운드(열기 실패가 화면 · 비상정지를 붙잡지 않게).
    # 실패는 장치 상태 · 띠로 보이고, 이후는 자동 다시 연결이 맡는다.
    await asyncio.to_thread(APP.link_stage, "start")
    threading.Thread(target=_startup_link, name="startup-link", daemon=True).start()
    task = asyncio.create_task(_broadcaster())
    try:
        yield
    finally:
        task.cancel()
        if not APP.shutting:
            await asyncio.to_thread(APP.shutdown_cleanup)


app = FastAPI(lifespan=lifespan)


def _state_json():
    return json.dumps({"type": "state", "state": APP.state(), "log": APP.log_tail()}, ensure_ascii=False, default=_jd)


async def _bcast_raw(data):
    for ws in list(CLIENTS):
        try:
            await ws.send_text(data)
        except Exception:                                            # noqa: BLE001
            CLIENTS.discard(ws)


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
                data = await asyncio.to_thread(_state_json)      # 상태 만들기 · 직렬화는 루프 밖에서(비상정지 요청이 루프에서 늦지 않게)
                await _bcast_raw(data)
            except Exception as e:                                   # noqa: BLE001
                logger.log("상태 전송 실패 · " + logger.short(e), "warn")


def _index_html():
    with open(os.path.join(FRONT, "index.html"), encoding="utf-8") as f:
        return f.read().replace("__V__", _asset_v())


@app.get("/")
async def root():
    html = await asyncio.to_thread(_index_html)                 # 파일 읽기 · 폴더 훑기는 이벤트 루프 밖에서
    return HTMLResponse(html, headers={"Cache-Control": "no-cache"})


@app.get("/favicon.ico")
async def favicon():
    return Response(status_code=204)


@app.get("/serial.json")
def serial_json(n: int = 200):
    """기록 보기 "시리얼" 탭: 보낸 줄 · 받은 줄 원문(최근 n 줄)."""
    rows = list(APP.serial_log)[-max(1, min(int(n), 400)):] if APP else []
    return JSONResponse({"rows": [{"t": t, "d": d, "x": x} for t, d, x in rows]}, headers={"Cache-Control": "no-store"})


@app.get("/rt.json")
def rt_json(since: int = 0, id: str = ""):
    """R–T 측정 그래프용 점: since 번째부터. id 가 지금 측정(stamp)과 다르면 처음부터 다시 준다."""
    r = APP.rt if APP else None
    if r is None:
        return JSONResponse({"id": "", "n": 0, "points": []}, headers={"Cache-Control": "no-store"})
    if id != r.stamp + r.mode:
        since = 0
    pts = r.points[max(0, int(since)):][:4000]
    return JSONResponse({"id": r.stamp + r.mode, "n": max(0, int(since)) + len(pts), "total": len(r.points), "points": pts}, headers={"Cache-Control": "no-store"})


@app.get("/rt_data/{name}")
def rt_data(name: str):
    """결과 탭 [그래프 보기]: 측정 폴더의 회차 파일(.dat)들을 [[T, R], ...] 로."""
    import rt as rt_mod
    d = os.path.join(paths.DATA_DIR, "measure", os.path.basename(name))
    try:
        files = sorted(f for f in os.listdir(d) if f.endswith(".dat") and not f.endswith("merged.dat"))
    except OSError:
        return JSONResponse({"cycles": []})
    return JSONResponse({"cycles": [{"file": f, "pts": rt_mod.read_dat(os.path.join(d, f))} for f in files]})


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
    """실시간 그림. 이 요청이 최근 2 s 안에 있었으면 미리보기가 켜져 있다(화면이 켜고 끄는 상태를 따로 기억하지 않는다).
    카메라가 3 s 넘게 새 프레임을 못 주면 옛 그림 대신 204. 응답 헤더: 프레임 번호 · 찍은 시각(벽시계 s)."""
    r = APP.vision.want_preview()
    if r is None and APP.vision.cam is not None:                 # 막 켜졌다(옛 그림이 오래됨): 첫 새 프레임을 잠깐 기다린다(루프는 막지 않음) - 빈 화면 깜빡임 방지
        for _ in range(12):
            await asyncio.sleep(0.05)
            r = APP.vision.want_preview()
            if r is not None:
                break
    if r is None:
        return Response(status_code=204, headers={"Cache-Control": "no-store"})
    jpg, seq, t = r
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store", "X-Preview-Seq": str(seq), "X-Preview-T": "%.3f" % t,
                                                           "Access-Control-Expose-Headers": "X-Preview-Seq, X-Preview-T"})


@app.get("/preview.json")
async def preview_info():
    """화면의 "실시간 멈춤" 표시용: 프레임 번호 · 몇 초 전 그림 · 실제 갱신 장/초. 이 요청은 미리보기를 켜지 않는다."""
    v = APP.vision
    age = None if v.preview_mono <= 0 else round(time.monotonic() - v.preview_mono, 2)
    return JSONResponse({"seq": v.preview_n, "age": age, "fps": v.preview_fps(), "open": v.cam is not None}, headers={"Cache-Control": "no-store"})


@app.get("/camthumb/{idx}.jpg")
def camthumb(idx: int):
    """[카메라 찾기] 번호별 작은 사진."""
    jpg = APP.cam_thumbs.get(int(idx)) if APP else None
    if not jpg:
        return Response(status_code=204)
    return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})


@app.get("/shot.jpg")
def shot_jpg():
    return _img_resp(APP.shot["img"] if APP.shot else None, APP.shot["id"] if APP.shot else 0)


@app.get("/ref.jpg")
def ref_jpg():
    return _img_resp(APP.ref_img(), APP.ref_id())


@app.get("/snap.jpg")
def snap_jpg():
    return _img_resp(APP.snap["img"] if APP.snap else None, APP.snap["id"] if APP.snap else 0)


@app.get("/refhist/{name}.jpg")
def refhist(name):
    from core import imgio
    img = imgio.imread_u(os.path.join(paths.REF_HISTORY_DIR, name + ".png"))
    return _img_resp(img, name)


@app.get("/run/{name}/{file}")
def run_file(name, file):
    fp = os.path.join(paths.RUNS_DIR, name, os.path.basename(file))
    if not os.path.exists(fp):
        return Response(status_code=404)
    if file.endswith(".json"):
        return JSONResponse(json.load(open(fp, encoding="utf-8")))
    return Response(open(fp, "rb").read(), media_type="image/jpeg" if file.endswith(".jpg") else "text/csv; charset=utf-8")


@app.post("/estop")
async def http_estop():
    """비상정지 우회로: WS 가 끊겨도 도착한다. 전용 스레드에서 실행하고 루프는 기다리기만 한다(막히지 않는다)."""
    await asyncio.wrap_future(APP.estop_async(time.time()))
    return JSONResponse({"ok": True})


@app.post("/stop")
async def http_stop():
    """정지 우회로: 비상정지와 같은 수준의 빠른 길(실행 스레드만 따로). 움직이는 모터를 멈추고 이어질 이동을 취소한다 - 잠그지 않는다."""
    r = await asyncio.wrap_future(APP.stop_async(time.time()))
    return JSONResponse({"ok": True, "cut": bool(r.get("cut"))})


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
    await ws.send_text(await asyncio.to_thread(_state_json))
    tasks = set()
    try:
        while True:
            raw = await ws.receive_text()
            msg = json.loads(raw)
            if msg.get("cmd") in ("estop", "stop"):
                fut = (APP.estop_async if msg.get("cmd") == "estop" else APP.stop_async)(time.time())     # 전용 스레드 · 줄 세우지 않는다. 이 루프는 기다리지 않는다

                async def ack_estop(f=fut, i=msg.get("id")):
                    with contextlib.suppress(Exception):
                        await asyncio.wrap_future(f)
                        await ws.send_text(json.dumps({"type": "ack", "id": i, "ok": True}))
                t = asyncio.create_task(ack_estop())
                tasks.add(t)
                t.add_done_callback(tasks.discard)
                continue
            if msg.get("cmd") == "preview":                           # 옛 호환: 무시한다(켜짐 · 꺼짐은 /preview.jpg 요청으로만 정해진다)
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
    sys.setswitchinterval(0.0002)                             # 파이썬 스레드 전환을 잘게: 비상정지 쓰기가 다른 스레드에 밀려 늦지 않게
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--image", default=None)
    ap.add_argument("--fake-serial", action="store_true", help="tools/fake_v11.py 펌웨어 흉내(시험용)")
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
            if not ok:                                               # 화면이 응답 못 해도 측정 · 이동 중이면 거절한다
                try:
                    APP.c_shutdown_check({}, 0)
                except Exception as e:                           # noqa: BLE001
                    logger.log("종료 거절 · " + logger.short(e), "warn")
                    return
                APP.shutdown_cleanup()
                window.request_exit(HOLDER.get("server"))
        threading.Thread(target=ask, daemon=True).start()
        return False

    HOLDER["on_closing"] = on_closing
    return window.run(app, HOST, _ARGS.port, _ARGS.no_window, HOLDER)


if __name__ == "__main__":
    sys.exit(main())
