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

    주기 폴링(st / ST … · rs / RS …)은 양이 많아 화면에서 기본으로 숨긴다. 어떤 줄이 폴링인지는
    서버가 판단해 표시만 넘기고, 숨길지 말지는 화면이 정한다(파일에는 전부 남는다).
    """
    t = (text or "").strip()
    # rs(릴레이 하트비트)도 폴링이다 - 감시 시간의 1/3 주기로 나간다.
    poll = t in ("st", "rs") or t.startswith("ST ") or t.startswith("RS ")
    level = "tx" if arrow == "->" else ("rx" if arrow == "<-" else "info")
    push_log_threadsafe("%s %s" % (arrow, t), level, serial=True, poll=poll)


class StageCtl:
    def __init__(self, dry=False):
        self.dry = bool(dry)
        self.dev = None
        self._ex = ThreadPoolExecutor(max_workers=1, thread_name_prefix="stage")
        # 펌웨어가 스스로 보낸 사건(통신 끊김 등)을 받을 곳. fn(kind, line) - 워커·감시
        # 스레드에서 불리므로 받는 쪽이 루프로 넘긴다(relays 가 단다).
        self.event_handler = None

    async def _call(self, fn, *a, **kw):
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(self._ex, functools.partial(fn, *a, **kw))

    # ---------------- 연결 ----------------
    async def connect(self, port=None):
        if self.dev is not None:
            await self.disconnect()
        dev = stage_mod.Stage(port, dry=self.dry)
        dev.on_line = _serial_line          # 주고받은 줄을 화면 로그로도 보낸다
        dev.on_event = self._on_event
        banner = await self._call(dev.open)
        if self.dry:
            dev.port_name = "--dry"        # 화면 칩에 '?' 대신 무엇인지 보이게
        self.dev = dev
        return banner

    async def disconnect(self):
        dev, self.dev = self.dev, None
        if dev is not None:
            await self._call(dev.close)

    def _on_event(self, kind, line):
        fn = self.event_handler
        if fn is not None:
            try:
                fn(kind, line)
            except Exception:              # noqa: BLE001
                pass

    @property
    def connected(self):
        return self.dev is not None

    @property
    def port(self):
        return getattr(self.dev, "port_name", None)

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

    async def move_x(self, mm, speed=None):
        return await self._call(self._need().move_x_mm, mm, speed)

    async def move_y(self, mm, speed=None):
        return await self._call(self._need().move_y_mm, mm, speed)

    async def move_z(self, mm, speed=None):
        return await self._call(self._need().move_z_mm, mm, speed)

    async def set_position(self, x_mm, y_mm):
        return await self._call(self._need().set_position, x_mm, y_mm)

    async def set_profile(self, accel, start_pps):
        return await self._call(self._need().set_profile, accel, start_pps)

    async def watchdog(self, ms):
        return await self._call(self._need().watchdog, ms)

    async def relay_set(self, n, on):
        return await self._call(self._need().relay_set, n, on)

    async def relay_all_off(self):
        return await self._call(self._need().relay_all_off)

    async def relay_status(self):
        return await self._call(self._need().relay_status)

    def fw_major(self):
        return stage_mod._major_of(self.fw_version)

    # ---- 검증용(dry 전용) ----
    def dry_true_xy(self):
        dev = self.dev
        if dev is None or not getattr(dev, "dry", False):
            return None
        return dev.dry_true_xy()

    def dry_slip(self, dx, dy):
        if self.dev is not None and self.dev.dry:
            self.dev.dry_slip(dx, dy)

    def dry_stall(self, seconds):
        if self.dev is not None and self.dev.dry:
            self.dev.dry_stall(seconds)

    async def jog_rel(self, axis, mm):
        return await self._call(self._need().jog_rel, axis, mm)

    async def set_zero(self, axis="xy"):
        return await self._call(self._need().set_zero, axis)

    async def forget(self):
        return await self._call(self._need().forget)

    @property
    def fw_version(self):
        return getattr(self.dev, "fw_version", "")

    async def save(self):
        return await self._call(self._need().save)

    def abort(self, relays_off=False, off_relays=()):
        """워커 큐를 거치지 않는 즉시 호출 (동기). relays_off 면 "r all 0" 을 바로 붙인다."""
        if self.dev is None:
            return False
        try:
            return self.dev.abort(relays_off=relays_off, off_relays=off_relays)
        except Exception as e:             # noqa: BLE001
            logger.write("err", "비상정지 실패: %s" % e)
            return False

    def shutdown(self):
        self._ex.shutdown(wait=False)


StageError = stage_mod.StageError


def origin_gap_mm():
    """원점(0)이 끝단에서 물러나는 거리(설정 origin_gap_mm → 드라이버). 매번 읽는다."""
    return stage_mod.ORIGIN_GAP_MM


def z_max_mm():
    """Z 가동범위(mm). 0 = 맨 위, 값이 커질수록 아래다.

    설정(limits.z_max_mm)으로 바뀌므로 값을 복사해 두지 않고 매번 드라이버에서 읽는다.
    """
    return stage_mod.Z_MAX_MM


def limits_mm():
    """세 축의 가동범위(mm). 화면 state 의 limits 가 이 값이다."""
    return stage_mod.limits_mm()


fw_limits_pulse = stage_mod.fw_limits_pulse
ctl = StageCtl()
