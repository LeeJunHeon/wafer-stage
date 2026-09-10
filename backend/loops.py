"""loops.py - 백그라운드 주기 태스크.

status_loop : 이동 중이 아닐 때 2초마다 st 로 위치·원점 상태를 갱신한다.
              시리얼이 빠지면(예외) connected=false 로 내리고 한 번만 로그한다.

server.py 의 lifespan 에서 start_all()/stop_all() 로만 쓴다. server.py 를
import 하지 않는다(순환 방지).
"""

import asyncio
import contextlib

import engine
import logger
import stagectl
from connection import push_log, push_state
from state import state

STATUS_INTERVAL_S = 2.0


async def status_loop():
    fail = 0
    while True:
        await asyncio.sleep(STATUS_INTERVAL_S)
        if not stagectl.ctl.connected or state.stage["moving"]:
            continue                       # 이동 중에는 워커가 보고 줄을 기다린다
        try:
            st = await stagectl.ctl.status()
            before = (state.stage["x_mm"], state.stage["y_mm"],
                      state.stage["homed_x"], state.stage["homed_y"])
            engine._apply_status(st)
            fail = 0
            after = (state.stage["x_mm"], state.stage["y_mm"],
                     state.stage["homed_x"], state.stage["homed_y"])
            if before != after:
                await push_state()
        except Exception as e:             # noqa: BLE001
            fail += 1
            if fail == 1:                  # 도배하지 않는다 - 첫 실패만 알린다
                logger.exc("상태 폴링 실패", e)
                state.stage.update({"connected": False, "port": None, "moving": False,
                                    "x_mm": None, "y_mm": None, "u": None, "v": None,
                                    "homed_x": False, "homed_y": False})
                state.stage["last_error"] = str(e)
                await push_log("스테이지 응답 없음 · 연결 해제 (%s)" % e, "err")
                with contextlib.suppress(Exception):
                    await stagectl.ctl.disconnect()
                await push_state()


def start_all():
    return [asyncio.create_task(status_loop())]


async def stop_all(tasks):
    for t in tasks or []:
        t.cancel()
    for t in tasks or []:
        with contextlib.suppress(asyncio.CancelledError, Exception):
            await t
