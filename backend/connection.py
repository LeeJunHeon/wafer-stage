"""connection.py - WebSocket 연결 관리 + state/log 브로드캐스트."""

import json

import logger
from state import state


class ConnectionManager:
    def __init__(self):
        self.active = set()

    async def connect(self, ws):
        await ws.accept()
        self.active.add(ws)
        # 접속 직후 전체 상태를 한 번 보낸다. 화면은 이 값이 와야 그리기 시작한다.
        await self._send(ws, state.snapshot())
        # 기동 시점에는 접속자가 0명이라 놓친 진단을 지금 전달한다(목록은 비우지
        # 않는다 - 새로고침해도 다시 보여야 한다).
        for n in state.startup_notices:
            await self._send(ws, {"type": "log", "msg": "[프로그램 진단] " + n["msg"],
                                  "level": n.get("level", "warn")})

    def disconnect(self, ws):
        self.active.discard(ws)

    async def _send(self, ws, obj):
        try:
            await ws.send_text(json.dumps(obj, ensure_ascii=False))
        except Exception:                  # noqa: BLE001
            self.active.discard(ws)

    async def broadcast(self, obj):
        if not self.active:
            return
        text = json.dumps(obj, ensure_ascii=False)
        dead = []
        for ws in list(self.active):
            try:
                await ws.send_text(text)
            except Exception:              # noqa: BLE001
                dead.append(ws)
        for ws in dead:
            self.active.discard(ws)


manager = ConnectionManager()


async def push_state():
    await manager.broadcast(state.snapshot())


async def push_log(msg, level="info"):
    logger.write(level, msg)               # 화면과 파일에 함께 남긴다
    await manager.broadcast({"type": "log", "msg": str(msg), "level": level})


async def push_ack(of, ok, reason="", needs_confirm=None):
    await manager.broadcast({"type": "ack", "of": of, "ok": bool(ok),
                             "reason": reason, "needs_confirm": needs_confirm or []})
