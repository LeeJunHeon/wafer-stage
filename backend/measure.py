"""measure.py - 계측기 인터페이스.

계측기가 아직 정해지지 않았다. 순회 엔진이 샘플마다 부르는 자리만 만들어 두고,
장비가 정해지면 Measurer 를 구현해 settings 의 measure.driver 로 갈아끼운다.

  class Measurer:
      async def measure(sample: dict, ctx: dict) -> dict | None
          sample: {"no","shape","X","Y",...}   ctx: {"seq_dir","params",...}
          반환:   {"value": 3.21, "unit": "V", "raw": {...}} 또는 None(측정 없음)
"""

import logger


class Measurer:
    name = "base"

    async def measure(self, sample, ctx):   # pragma: no cover - 인터페이스
        raise NotImplementedError

    async def close(self):
        return None


class DummyMeasurer(Measurer):
    """계측기 미연결. 값 없이 통과시킨다(순회 동작 자체는 검증할 수 있게)."""

    name = "dummy"

    def __init__(self):
        self._told = False

    async def measure(self, sample, ctx):
        if not self._told:
            self._told = True
            logger.write("warn", "계측기 미연결 - 측정값 없음 (settings 의 measure.driver)")
        return None


def make(settings):
    drv = str(((settings or {}).get("measure") or {}).get("driver", "dummy")).lower()
    if drv in ("", "dummy", "none"):
        return DummyMeasurer()
    logger.write("warn", "알 수 없는 계측기 driver=%s - dummy 로 대체합니다" % drv)
    return DummyMeasurer()
