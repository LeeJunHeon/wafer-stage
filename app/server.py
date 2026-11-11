"""server.py - FastAPI + WebSocket (127.0.0.1:8000). 화면 파일은 2단계에서 만든다.

  GET  /                     화면(ui/index.html) · /ui/ 아래 정적 파일
  GET  /api/camera/preview.jpg  카메라 미리보기(가로 preview_width 로 줄인 JPEG, 1 초에 한 장 갱신. 없으면 204)
  GET  /api/img/{raw|crop|plate|empty|live}.jpg  찍은 사진 · 원본 조각 · 보정한 판 사진 · 빈 판 사진 · 지금 카메라의 보정 사진(없으면 204)
  GET  /api/run/points?sample=<id>&from=<n>   측정 점(새 점만)
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

from pathlib import Path

from fastapi import Body, FastAPI, Query, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from app.config import ConfigError
from app.control import Controller
from app.safety import Rejected

HOST, PORT = "127.0.0.1", 8000
UI_DIR = Path(__file__).resolve().parent.parent / "ui"


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

    @app.get("/")
    async def index():
        return FileResponse(UI_DIR / "index.html", headers={"Cache-Control": "no-store"})

    @app.get("/api/camera/preview.jpg")
    async def camera_preview():
        jpg = ctrl.camera.preview_jpeg()
        if jpg is None:
            return Response(status_code=204)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/img/{what}.jpg")
    async def img(what: str, x: float = 0.0, y: float = 0.0, half: int = 120):
        """화면용 그림: raw(찍은 원본) · crop(원본 해상도 조각 x, y, half) · plate(보정한 판 사진) · empty(보정한 빈 판 사진) · live(지금 카메라의 보정 사진)."""
        jpg = await asyncio.to_thread(ctrl.prep.jpeg, what, x=x, y=y, half=half)
        if jpg is None:
            return Response(status_code=204)
        return Response(jpg, media_type="image/jpeg", headers={"Cache-Control": "no-store"})

    @app.get("/api/run/points")
    async def run_points(sample: int, frm: int = Query(0, alias="from")):
        """측정 중인(또는 이번 측정에서 잰) 샘플의 점 [온도, 저항, 단계] 중 from 번째부터. 점 데이터는 state 에 넣지 않는다."""
        return ctrl.runner.points(sample, frm)

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
        ctrl.publish_cfg()
        ctrl.prep.on_settings()
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

    if UI_DIR.is_dir():
        app.mount("/ui", StaticFiles(directory=str(UI_DIR)), name="ui")
    return app
