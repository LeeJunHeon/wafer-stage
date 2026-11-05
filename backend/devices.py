"""devices.py - 측정 장비 드라이버 인터페이스(온도 · 접촉)와 더미(시뮬레이터), 시간 배율, 실시간 기록 허브.

장비가 아직 정해지지 않았다. 여기서는 순회 엔진이 부르는 자리만 만들고, 실장치는 같은 인터페이스를 구현해
설정 temp.driver / contact.driver 로 갈아끼운다.

  TempSource.read() -> °C | None(센서 오류)
      dummy       열 모델 시뮬레이터. 쿨러 · 히트건 릴레이 상태에 따라 지수 응답(주변 온도 · 시간 상수 ·
                  잡음은 설정 sim.*). 실장치(미정)를 붙일 자리: 계측기(Measurer) 경유 · 아두이노 아날로그 입력 ·
                  별도 시리얼 장치 중 하나 - make_temp() 에 드라이버 이름을 더하면 된다.
  ContactSource.is_contact() -> bool | None(읽을 수 없음)
      dummy       Z 가 sim.contact_z_mm 이상이면 접촉
      measurer    계측기가 돌려줌(Measurer.contact())
      di          아두이노 디지털 입력 핀(contact.di_pin · di_pullup · di_active_high). 펌웨어에 `di <pin>` 읽기
                  명령(V12)이 필요하다 - 없으면 오류로 알린다(--dry 에서는 Z 로 흉내)
      manual      사람이 [접촉 확인] 버튼으로 켜고 끈다

시간 배율(sim.time_scale): 더미 열 모델 · 새 레시피 단계(접촉 대기 · 온도 대기 · 가열 · 기록)의 시간이 이 배율로
빨라진다(테스트용 - 1 = 실시간). vnow()/vsleep() 이 그 시계다.

Hub 는 temp.period_s 마다 온도를 읽어 state.measure 에 두고 WebSocket 으로 {"type":"temp",...} 를 보낸다.
히트건이 켜져 있는 동안 온도가 heater.max_c 를 넘거나 센서 오류가 heater.sensor_fail_n 번 이어지면 즉시 끈다
(레시피 단계와 무관한 과열 보호).
"""

import asyncio
import collections
import math
import random
import time

import logger
from connection import manager, push_log, push_state
from state import state

# --------------------------------------------------------------------------
# 시간 배율 시계
# --------------------------------------------------------------------------
_vt = {"v": 0.0, "last": None}


def scale():
    try:
        return max(0.01, float(state.get("sim.time_scale") or 1.0))
    except (TypeError, ValueError):
        return 1.0


def vnow():
    """가상 시간(초). 실제 경과 x sim.time_scale. 배율이 바뀌어도 이어진다."""
    now = time.monotonic()
    if _vt["last"] is None:
        _vt["last"] = now
    _vt["v"] += (now - _vt["last"]) * scale()
    _vt["last"] = now
    return _vt["v"]


async def vsleep(dt_v):
    """가상 시간 dt_v 초를 기다린다(실제로는 dt_v / 배율)."""
    await asyncio.sleep(max(0.0, dt_v) / scale())


# --------------------------------------------------------------------------
# 온도
# --------------------------------------------------------------------------
def _relay_on(n):
    try:
        return bool(state.relays[int(n) - 1]["on"])
    except (IndexError, TypeError, ValueError, KeyError):
        return False


def heater_relay():
    return int(state.get("heater.relay") or 10)


def zone_relays():
    from core import zones as zones_mod
    z, _ch = zones_mod.migrate(state.get("cooler.zones"))
    return [int(x) for x in zones_mod.relays_of(z)]


class TempSource:
    name = "base"

    def read(self):                         # pragma: no cover - 인터페이스
        raise NotImplementedError

    async def close(self):
        return None


class DummyTemp(TempSource):
    """열 모델: dT/dt = (히터°C − T)/τ_heat·[히트건 켜짐] + (쿨러°C − T)/τ_cool·[쿨러 켜짐] + (주변 − T)/τ_amb."""

    name = "dummy"

    def __init__(self):
        self.t = None
        self.t_last = None
        self.force_c = None                 # 검증용: 값 강제(dry_temp)
        self.sensor_fail = False            # 검증용: 센서 오류 흉내

    def _p(self, k, d):
        try:
            return float(state.get(k))
        except (TypeError, ValueError):
            return d

    def read(self):
        now = vnow()
        amb = self._p("sim.ambient_c", 25.0)
        if self.t is None:
            self.t, self.t_last = amb, now
        dt = max(0.0, now - self.t_last)
        self.t_last = now
        if self.force_c is not None:
            self.t = float(self.force_c)
        elif dt > 0:
            h = _relay_on(heater_relay())
            c = any(_relay_on(n) for n in zone_relays())
            # 적분(작은 스텝으로 쪼갠다 - 시간 배율이 커도 안정)
            n_sub = max(1, int(math.ceil(dt / 0.5)))
            sub = dt / n_sub
            for _ in range(n_sub):
                d = (amb - self.t) / max(0.1, self._p("sim.tau_amb_s", 120.0))
                if h:
                    d += (self._p("sim.heater_c", 320.0) - self.t) / max(0.1, self._p("sim.tau_heat_s", 15.0))
                if c:
                    d += (self._p("sim.cooler_c", 5.0) - self.t) / max(0.1, self._p("sim.tau_cool_s", 25.0))
                self.t += d * sub
        if self.sensor_fail:
            return None
        return self.t + random.gauss(0.0, self._p("sim.noise_c", 0.2))


def make_temp(settings_get):
    drv = str(settings_get("temp.driver") or "dummy").lower()
    if drv in ("", "dummy"):
        return DummyTemp()
    logger.write("warn", "알 수 없는 온도 드라이버 %s - dummy 로 대체합니다" % drv)
    return DummyTemp()


# --------------------------------------------------------------------------
# 접촉
# --------------------------------------------------------------------------
class ContactSource:
    name = "base"

    async def is_contact(self):             # pragma: no cover - 인터페이스
        raise NotImplementedError


def _z_mm():
    z = (state.stage or {}).get("z_mm")
    return None if z is None else float(z)


class DummyContact(ContactSource):
    name = "dummy"

    async def is_contact(self):
        z = _z_mm()
        if z is None:
            return None
        try:
            lim = float(state.get("sim.contact_z_mm"))
        except (TypeError, ValueError):
            return None
        return z >= lim - 1e-9


class MeasurerContact(ContactSource):
    name = "measurer"

    def __init__(self):
        import measure as measure_mod
        self.m = measure_mod.make(state.settings)

    async def is_contact(self):
        try:
            return await self.m.contact()
        except Exception as e:              # noqa: BLE001
            logger.write("warn", "계측기 접촉 읽기 실패: %s" % e)
            return None


class DiContact(ContactSource):
    name = "di"

    async def is_contact(self):
        import stagectl
        pin = int(state.get("contact.di_pin") or 0)
        if stagectl.ctl.dry:                # dry: Z 로 흉내(펌웨어 없음)
            return await DummyContact().is_contact()
        fn = getattr(stagectl.ctl, "digital_read", None)
        if fn is None:
            logger.write("warn", "접촉 di: 펌웨어 입력 읽기 명령(V12) 미지원")
            return None
        try:
            raw = bool(await fn(pin, bool(state.get("contact.di_pullup"))))
        except Exception as e:              # noqa: BLE001
            logger.write("warn", "접촉 di 읽기 실패: %s" % e)
            return None
        return raw if bool(state.get("contact.di_active_high")) else (not raw)


class ManualContact(ContactSource):
    name = "manual"
    on = False

    async def is_contact(self):
        return bool(ManualContact.on)


def make_contact(settings_get):
    drv = str(settings_get("contact.driver") or "dummy").lower()
    if drv == "measurer":
        return MeasurerContact()
    if drv == "di":
        return DiContact()
    if drv == "manual":
        return ManualContact()
    if drv in ("", "dummy"):
        return DummyContact()
    if drv in ("none", "off", "unused"):
        return None
    logger.write("warn", "알 수 없는 접촉 드라이버 %s - dummy 로 대체합니다" % drv)
    return DummyContact()


# --------------------------------------------------------------------------
# 허브: 주기 읽기 · 상태 · 이력 · 과열 보호
# --------------------------------------------------------------------------
class Hub:
    def __init__(self):
        self.temp = None
        self.contact = None
        self._drv = (None, None)
        self.hist = collections.deque(maxlen=1500)     # (가상 시간, °C, 히트건 켜짐)
        self.fail = 0
        self.tripped = ""
        self.last_c = None                              # 마지막 읽은 °C(None = 센서 오류)
        self.last_t = 0.0
        self.target = None                              # 그래프 목표선(레시피 단계가 채운다)
        self.label = ""
        self.left_s = None

    def configure(self):
        drv = (str(state.get("temp.driver") or "dummy"), str(state.get("contact.driver") or "dummy"))
        if drv != self._drv or self.temp is None:
            self._drv = drv
            self.temp = make_temp(state.get)
            self.contact = make_contact(state.get)
            self.hist.clear()
            self.fail = 0
            logger.write("info", "측정 장비 드라이버 · 온도 %s · 접촉 %s" % drv)

    async def contact_now(self):
        """지금 접촉 여부(True/False) 또는 None(드라이버 없음 · 읽기 실패)."""
        self.configure()
        if self.contact is None:
            return None
        return await self.contact.is_contact()

    def temp_ok(self):
        return self.last_c is not None

    async def tick(self):
        """한 번 읽는다: 상태 · 이력 · 방송 · 과열 보호."""
        self.configure()
        c = self.temp.read()
        t = vnow()
        self.last_c, self.last_t = c, t
        heater_on = _relay_on(heater_relay())
        self.hist.append((round(t, 2), None if c is None else round(c, 2), heater_on))
        ct = None
        try:
            ct = await self.contact_now()
        except Exception:                               # noqa: BLE001
            ct = None
        m = state.measure
        m.update({"temp": None if c is None else round(c, 1), "temp_ok": c is not None,
                  "temp_driver": self._drv[0], "contact_driver": self._drv[1],
                  "contact": ct, "heater_on": heater_on,
                  "cooler_on": [n for n in zone_relays() if _relay_on(n)],
                  "target": self.target, "label": self.label, "left_s": self.left_s,
                  "tripped": self.tripped})
        await manager.broadcast({"type": "temp", "t": round(t, 2), "c": m["temp"], "heater": heater_on,
                                 "target": self.target, "label": self.label, "left_s": self.left_s,
                                 "contact": ct, "cooler": m["cooler_on"]})
        # 과열 보호: 히트건이 켜져 있는 동안만 본다
        if heater_on:
            if c is None:
                self.fail += 1
            else:
                self.fail = 0
            lim = float(state.get("heater.max_c") or 300.0)
            n_fail = int(state.get("heater.sensor_fail_n") or 3)
            why = ""
            if c is not None and c > lim:
                why = "과열 보호 · %.0f °C > %g °C" % (c, lim)
            elif self.fail >= n_fail:
                why = "과열 보호 · 온도 센서 오류 %d회 연속" % self.fail
            if why:
                await self.trip(why)
        else:
            self.fail = 0

    async def trip(self, why):
        """히트건을 즉시 끄고 경보를 낸다(진행 중인 가열 · 대기 단계가 이를 보고 순회를 중단한다)."""
        import relays
        self.tripped = why
        state.measure["tripped"] = why
        state.set_alarm("히트건", why)
        await push_log(why + " · 히트건 끔", "err")
        await relays.set_relay(heater_relay(), False, by_engine=True)
        await push_state()

    def clear_trip(self):
        self.tripped = ""
        state.measure["tripped"] = ""
        state.set_alarm("히트건", "")


hub = Hub()


async def loop():
    """loops 가 태스크로 띄운다. temp.period_s(가상 시간) 마다 읽는다."""
    while True:
        try:
            await hub.tick()
        except asyncio.CancelledError:
            raise
        except Exception as e:                          # noqa: BLE001
            logger.exc("측정 장비 루프", e)
        try:
            period = max(0.05, float(state.get("temp.period_s") or 0.5))
        except (TypeError, ValueError):
            period = 0.5
        await vsleep(period)
