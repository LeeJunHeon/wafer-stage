"""stagectl.py - stage.Stage 를 단일 워커 스레드에서 asyncio 로 감싼다.

stage.Stage 는 블로킹이고 시리얼 포트는 동시 접근을 견디지 못한다. 그래서 모든
호출을 워커 스레드 하나(ThreadPoolExecutor(1))에 몰아 직렬화하고, asyncio 쪽은
run_in_executor 로 기다린다. 이렇게 하면 이동(수 초)이 서버 루프를 막지 않는다.

예외: abort() 는 큐를 기다리면 '비상' 이 아니므로 워커를 거치지 않고 바로 쓴다
(stage.Stage.abort 주석 참고).
"""

import asyncio
import functools
from concurrent.futures import ThreadPoolExecutor

from core import stage as stage_mod

import logger
from connection import push_log_threadsafe


def _serial_line(arrow, text):
    """시리얼 원문 한 줄. 워커 스레드에서 불린다.

    2초 폴링(st / ST …)은 양이 많아 화면에서 기본으로 숨긴다. 어떤 줄이 폴링인지는
    서버가 판단해 표시만 넘기고, 숨길지 말지는 화면이 정한다(파일에는 전부 남는다).
    """
    t = (text or "").strip()
    poll = t == "st" or t.startswith("ST ")
    level = "tx" if arrow == "->" else ("rx" if arrow == "<-" else "info")
    push_log_threadsafe("%s %s" % (arrow, t), level, serial=True, poll=poll)


class StageCtl:
    def __init__(self, dry=False):
        self.dry = bool(dry)
        self.dev = None
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage")

    async def _call(self, fn, *a, **kw):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._ex, functools.partial(fn, *a, **kw))

    # ---------------- 연결 ----------------
    async def connect(self, port=None):
        if self.dev is not None:
            await self.disconnect()
        dev = stage_mod.Stage(port, dry=self.dry)
        dev.on_line = _serial_line          # 주고받은 줄을 화면 로그로도 보낸다
        banner = await self._call(dev.open)
        if self.dry:
            dev.port_name = "--dry"        # 화면 칩에 '?' 대신 무엇인지 보이게
        self.dev = dev
        return banner

    async def disconnect(self):
        dev, self.dev = self.dev, None
        if dev is not None:
            await self._call(dev.close)

    @property
    def connected(self):
        return self.dev is not None

    @property
    def port(self):
        return getattr(self.dev, "port_name", None)

    @property
    def needs_home(self):
        return bool(getattr(self.dev, "needs_home", False))

    def _need(self):
        if self.dev is None:
            raise stage_mod.StageError("스테이지가 연결되지 않았습니다")
        return self.dev

    # ---------------- 명령 ----------------
    async def status(self):
        return await self._call(self._need().status)

    async def goto(self, x_mm, y_mm):
        dev = self._need()
        return await self._call(dev.goto_mm, x_mm, y_mm)

    async def move_x(self, mm):
        return await self._call(self._need().move_x_mm, mm)

    async def move_y(self, mm):
        return await self._call(self._need().move_y_mm, mm)

    async def find_zero(self, axis, search_pulses=None):
        return await self._call(self._need().find_zero, axis, search_pulses)

    async def save(self):
        return await self._call(self._need().save)

    def abort(self):
        """워커 큐를 거치지 않는 즉시 호출 (동기)."""
        if self.dev is None:
            return False
        try:
            return self.dev.abort()
        except Exception as e:             # noqa: BLE001
            logger.write("err", "비상정지 실패: %s" % e)
            return False

    def shutdown(self):
        self._ex.shutdown(wait=False)


StageError = stage_mod.StageError
ctl = StageCtl()
