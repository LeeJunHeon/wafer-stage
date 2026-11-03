"""relays.py - 릴레이 10개(히트건 · 쿨러 등 장비 전원)와 펌웨어 통신 감시.

펌웨어의 RS 줄이 진실이다. 화면은 state.relays 가 와야 바뀐다(낙관적 갱신 없음).

  - 사용 안 함(settings relay.items[n].enabled=false) 릴레이는 켜기를 거절한다.
  - 순회 중 수동 켜기/끄기는 거절한다(전체 끄기만 허용). 레시피 단계는 by_engine.
  - 정책(relay.on_estop/on_stop/on_error/on_exit): "off" 면 그 사건 때 전부 끈다.
    비상정지는 engine.estop 이 '!' 와 같은 쓰기에 "r all 0" 을 붙인다(늦어지지 않게).
  - 연결 중에는 감시 시간의 1/3 주기로 rs 를 보낸다 - 펌웨어 통신 감시의 하트비트이자
    상태 동기화. 이동 중에는 보내지 않는다(워커가 보고 줄을 기다린다).
  - 최대 켜짐 시간(max_on_s)을 넘긴 릴레이는 앱이 끄고 경보를 띄운다.
  - 펌웨어가 "[!] 통신 끊김" 을 보내면 경보 배너에 띄우고 상태를 다시 읽는다.
"""

import time

import logger
import stagectl
from connection import push_log, push_state, run_on_loop
from state import state

SRC_WD = "통신 감시"
SRC_MAXON = "릴레이"
_last_sync = 0.0


def supported():
    """연결된 펌웨어가 릴레이를 아는가(V10 이상). 버전을 못 읽었으면 시도한다."""
    if not stagectl.ctl.connected:
        return False
    m = stagectl.ctl.fw_major()
    return m == 0 or m >= 10


def item(n):
    items = state.get("relay.items") or []
    try:
        return items[int(n) - 1] or {}
    except (IndexError, TypeError, ValueError):
        return {}


def name(n):
    return str(item(n).get("name") or ("R%d" % int(n)))


def _apply_bits(bits):
    now = time.time()
    for i, on in enumerate(bits or []):
        if i >= len(state.relays):
            break
        r = state.relays[i]
        if on and not r["on"]:
            r["since"] = now
        if not on:
            r["since"] = None
        r["on"] = bool(on)


def any_on():
    return any(r["on"] for r in state.relays)


def _clear():
    for r in state.relays:
        r["on"] = False
        r["since"] = None


async def sync():
    """rs 로 상태를 다시 읽는다. 실패는 로그만(폴링이 연결 끊김을 따로 본다)."""
    global _last_sync
    if not supported():
        return False
    try:
        _apply_bits(await stagectl.ctl.relay_status())
        _last_sync = time.monotonic()
        return True
    except stagectl.StageError as e:
        logger.write("warn", "릴레이 상태 조회 실패: %s" % logger.short(e))
        return False


async def set_relay(n, on, by_engine=False, busy=False):
    """(ok, reason). reason: bad_args|not_connected|unsupported|disabled|busy|error."""
    try:
        n = int(n)
    except (TypeError, ValueError):
        return False, "bad_args"
    if not 1 <= n <= 10:
        return False, "bad_args"
    if not stagectl.ctl.connected:
        return False, "not_connected"
    if not supported():
        return False, "unsupported"
    if on and not item(n).get("enabled"):
        await push_log("%s 사용 안 함 · 설정에서 지정" % name(n), "warn")
        return False, "disabled"
    if busy and not by_engine:
        await push_log("순회 중 · 릴레이 수동 조작 안 함", "warn")
        return False, "busy"
    try:
        _apply_bits(await stagectl.ctl.relay_set(n, bool(on)))
    except stagectl.StageError as e:
        await push_log("릴레이 실패 · " + logger.short(e), "err")
        return False, "error"
    if not by_engine:
        state.set_alarm(SRC_MAXON, "")       # 사람이 다시 만졌다 - 시간 초과 경보를 내린다
    await push_log("%s %s" % (name(n), "켜짐" if on else "꺼짐"), "ok" if on else "info")
    await push_state()
    return True, ""


async def all_off(why=""):
    """전부 끈다. 연결이 없으면 아무 일도 하지 않는다(펌웨어는 리셋 때 이미 꺼짐)."""
    if not supported():
        return False
    try:
        _apply_bits(await stagectl.ctl.relay_all_off())
    except stagectl.StageError as e:
        await push_log("릴레이 전부 끄기 실패 · " + logger.short(e), "err")
        return False
    await push_log("릴레이 전부 꺼짐" + (" · " + why if why else ""), "warn" if why else "info")
    await push_state()
    return True


def policy_off(event):
    """그 사건(estop|stop|error|exit)에서 릴레이를 전부 끄는 정책인가."""
    return (state.get("relay.on_" + event) or "off") == "off"


async def apply_policy(event, why=""):
    if policy_off(event) and stagectl.ctl.connected:
        await all_off(why)


async def on_connect():
    """연결 직후: 통신 감시 시간을 보내고 상태를 읽는다(보드는 리셋되어 전부 꺼져 있다)."""
    _clear()
    state.set_alarm(SRC_WD, "")
    state.set_alarm(SRC_MAXON, "")
    if not supported():
        return
    if stagectl.ctl.fw_major() >= 11:
        await send_watchdog()
    await sync()


async def send_watchdog():
    ms = int(state.get("relay.watchdog_ms") or 0)
    try:
        await stagectl.ctl.watchdog(ms)
        logger.write("info", "통신 감시 %d ms" % ms)
    except stagectl.StageError as e:
        await push_log("통신 감시 설정 실패 · " + logger.short(e), "warn")


def on_disconnect():
    _clear()


def heartbeat_period():
    """rs 하트비트 주기(초). 감시가 꺼져 있으면 상태 조회 주기."""
    ms = int(state.get("relay.watchdog_ms") or 0)
    if ms > 0:
        return max(0.3, ms / 3000.0)
    return float(state.get("status_poll_s") or 2.0)


async def tick():
    """loops 가 짧게 자주 부른다: 최대 켜짐 시간 검사 + 주기가 되면 rs."""
    if not stagectl.ctl.connected or not supported():
        return
    await _check_max_on()
    if state.stage.get("moving"):
        return
    if time.monotonic() - _last_sync >= heartbeat_period():
        before = [r["on"] for r in state.relays]
        await sync()
        if [r["on"] for r in state.relays] != before:
            await push_state()


async def _check_max_on():
    now = time.time()
    for r in state.relays:
        mx = float(item(r["n"]).get("max_on_s") or 0)
        if r["on"] and mx > 0 and r["since"] and now - r["since"] > mx:
            msg = "%s 최대 켜짐 %g s 초과 · 꺼짐" % (name(r["n"]), mx)
            state.set_alarm(SRC_MAXON, msg)
            await push_log(msg, "warn")
            if state.stage.get("moving"):
                continue                   # 이동이 끝나면 다음 틱에 끈다(워커가 막혀 있다)
            await set_relay(r["n"], False, by_engine=True)


def _on_event(kind, line):
    """펌웨어 사건(워커·감시 스레드). 루프로 넘겨 처리한다."""
    if kind == "wd_trip":
        run_on_loop(_wd_tripped)


async def _wd_tripped():
    state.set_alarm(SRC_WD, "통신 끊김 · 펌웨어가 릴레이를 전부 끔")
    _clear()
    await push_log("통신 끊김 · 펌웨어가 릴레이를 전부 껐습니다", "err")
    await push_state()
    await sync()
    await push_state()


stagectl.ctl.event_handler = _on_event
