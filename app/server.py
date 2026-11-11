"""server.py - FastAPI + WebSocket (127.0.0.1:8000). 화면 파일은 2단계에서 만든다.

  GET  /api/state            상태 한 덩어리
  WS   /ws                   상태를 보내고(바뀔 때마다 + 1 초마다) 명령을 받는다: {"id", "cmd", "args"} → {"id", "ok", "msg" | "result"}
  POST /api/estop            비상정지(WS 의 {"cmd":"estop"} 과 같은 길 - 검사 없이 바로)
  POST /api/estop/release    비상정지 풀기
  POST /api/cmd/{name}       WS 와 같은 명령(시험 · 도구용)
  GET  /api/settings         설정 표 · PUT /api/settings  바꿀 값 {key: value}
비상정지는 명령 줄에 서지 않고 새 스레드에서 바로 처리한다.
"""

import asyncio
import threading
from contextlib import asynccontextmanager

from fastapi import Body, FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse

from app.config import ConfigError
from app.control import Controller
from app.safety import Rejected

HOST, PORT = "127.0.0.1", 8000
PAGE = "<!doctype html><meta charset='utf-8'><title>샘플 자동 측정</title><p>준비 중</p>"


def _estop_thread(ctrl: Controller, source: str):
    """비상정지는 이벤트 루프를 기다리지 않는다 - 받은 즉시 스레드에서."""
    t = threading.Thread(target=ctrl.estop, args=(source,), daemon=True, name="estop")
    t.start()
    return t


def create_app(ctrl: Controller) -> FastAPI:
    @asynccontextmanager
    async def lifespan(app):
        yield
        await asyncio.to_thread(ctrl.shutdown)

    app = FastAPI(title="샘플 자동 측정 v3", lifespan=lifespan)
    app.state.ctrl = ctrl

    async def run(cmd, args):
        """명령 하나 실행 → 응답 dict. 거절은 쉬운 한국어 한 문장."""
        try:
            res = await asyncio.to_thread(ctrl.run_command, cmd, args)
            return {"ok": True, "result": res}
        except Rejected as e:
            return {"ok": False, "msg": str(e)}
        except Exception as e:                                      # noqa: BLE001 - 서버는 죽지 않는다. 사유는 로그에도 남긴다
            ctrl.logs.app("명령 오류 %s · %s: %s" % (cmd, type(e).__name__, e), "error")
            return {"ok": False, "msg": "명령을 처리하지 못했습니다 · %s" % str(e)[:60]}

    @app.get("/", response_class=HTMLResponse)
    async def index():
        return PAGE

    @app.get("/api/state")
    async def get_state():
        return ctrl.state.snapshot()

    @app.post("/api/estop")
    async def post_estop():
        _estop_thread(ctrl, "HTTP")
        return {"ok": True}

    @app.post("/api/estop/release")
    async def post_release():
        return await run("estop_release", {})

    @app.post("/api/cmd/{name}")
    async def post_cmd(name: str, args: dict = Body(default={})):
        if name == "estop":
            _estop_thread(ctrl, "HTTP")
            return {"ok": True}
        return await run(name, args)

    @app.get("/api/settings")
    async def get_settings():
        return ctrl.cfg.describe()

    @app.put("/api/settings")
    async def put_settings(patch: dict = Body(...)):
        try:
            ctrl.cfg.update(patch)
            ctrl.cfg.save()
        except ConfigError as e:
            return JSONResponse({"ok": False, "msg": str(e)}, status_code=400)
        return {"ok": True}

    @app.websocket("/ws")
    async def ws(sock: WebSocket):
        await sock.accept()
        loop = asyncio.get_running_loop()
        changed = asyncio.Event()
        notify = lambda: loop.call_soon_threadsafe(changed.set)    # noqa: E731 - 어느 스레드에서 불러도 된다
        ctrl.state.subscribe(notify)

        async def sender():
            while True:
                await sock.send_json({"type": "state", "state": ctrl.state.snapshot()})
                try:
                    await asyncio.wait_for(changed.wait(), timeout=1.0)   # 바뀌지 않아도 1 초마다 한 번
                except asyncio.TimeoutError:
                    pass
                changed.clear()

        async def handle(m):
            cmd, args, mid = m.get("cmd"), m.get("args") or {}, m.get("id")
            if cmd == "estop":                                      # 맨 앞에서 검사 없이
                _estop_thread(ctrl, "WS")
                res = {"ok": True}
            else:
                res = await run(cmd, args)
            await sock.send_json(dict(res, type="reply", id=mid))

        send_task = asyncio.create_task(sender())
        tasks = set()
        try:
            while True:
                m = await sock.receive_json()
                if m.get("cmd") == "estop":
                    _estop_thread(ctrl, "WS")                       # 응답을 기다리지 않고 바로
                    await sock.send_json({"type": "reply", "id": m.get("id"), "ok": True})
                    continue
                t = asyncio.create_task(handle(m))                  # 명령은 줄 세우지 않는다(메시지마다 작업 하나)
                tasks.add(t)
                t.add_done_callback(tasks.discard)
        except WebSocketDisconnect:
            pass
        finally:
            send_task.cancel()
            ctrl.state.unsubscribe(notify)

    return app
