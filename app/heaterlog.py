"""heaterlog.py - 히트건을 쓰는 동안의 기록(어떤 일이든): <데이터 폴더>/heater_log/YYYYMMDD_HHMMSS.csv(UTF-8 BOM).

히트건이 켜지면(송풍 또는 열선 켜짐 · 송풍 끄기 대기 포함) 새 파일을 열고, 꺼진 뒤 TAIL_S(60 s)까지 더 쓰고 닫는다.
온도를 받을 때마다(Controller.feed_temperature - 감시 스레드 · 측정 · 온도 시험) 한 줄. 앞 줄과 MIN_DT_S 보다 가까우면 건너뛴다.
줄마다 flush. 여러 스레드에서 오므로 잠금. 쓰기 실패는 로그를 한 번 남기고 그 파일만 멈춘다(부른 쪽으로 예외를 올리지 않는다).
"""

import threading
import time

TAIL_S = 60.0                                       # 꺼진 뒤 더 쓰는 시간
MIN_DT_S = 0.2                                      # 앞 줄과 이보다 가까우면 건너뜀
HEADER = ("시각", "경과(s)", "온도(°C)", "열선 출력(%)", "출력 방식", "송풍", "열선", "켜기 유지(s)", "쿨러", "하는 일")


class HeaterLog:
    def __init__(self, ctrl):
        self.c = ctrl
        self._lock = threading.Lock()
        self._f = None
        self.path = None
        self._t0 = None                             # 파일을 연 시각(clock)
        self._last = None                           # 마지막 줄 시각(clock)
        self._off_t = None                          # 히트건이 꺼진 시각(clock) - 꼬리
        self._failed = False                        # 이 파일 쓰기 실패(다음 파일까지 멈춤)

    def tail_open(self):
        """기록 파일이 열려 있는가(켜져 있거나 꺼진 뒤 꼬리)."""
        return self._f is not None

    def _active(self):
        c = self.c
        return bool(c.stage.connected and (c._heater_on() or c.heater_fan_pending()))

    def _who(self):
        c = self.c
        if c.runner.active:
            return "측정"
        if c.temptest.active:
            return "온도 시험"
        return "수동"

    def _open(self, now):
        c = self.c
        base = c.cfg.data_path() / "heater_log"
        name = time.strftime("%Y%m%d_%H%M%S") + ".csv"
        try:
            base.mkdir(parents=True, exist_ok=True)
            self._f = open(base / name, "w", encoding="utf-8-sig", newline="")
            self._f.write(",".join(HEADER) + "\r\n")
            self._f.flush()
        except OSError as e:
            self._f = None
            self._failed = True
            c.logs.app("히트건 기록 열기 실패 · %s · %s" % (base / name, e), "warn")
            return
        self.path = base / name
        self._t0, self._last, self._off_t, self._failed = now, None, None, False
        c.logs.app("히트건 기록 · %s" % self.path)

    def _close(self):
        f, self._f = self._f, None
        if f is not None:
            try:
                f.close()
            except OSError as e:
                self.c.logs.app("히트건 기록 닫기 실패 · %s" % e, "warn")

    def feed(self, T):
        """온도 한 값(None 이어도 줄을 쓴다 - 빈 칸)."""
        c = self.c
        try:
            now = c.clock()
            on = self._active()
        except Exception:                                           # noqa: BLE001 - 상태를 못 읽으면 이번 값은 건너뛴다(기록이 장비 쪽을 멈추지 않게)
            return
        with self._lock:
            if self._f is None:
                if not on:
                    self._failed = False                    # 꺼진 동안: 다음 켜기에 새 파일
                    return
                if self._failed:
                    return
                self._open(now)
                if self._f is None:
                    return
            if on:
                self._off_t = None
            elif self._off_t is None:
                self._off_t = now
            elif now - self._off_t >= TAIL_S:
                self._close()
                return
            if self._last is not None and now - self._last < MIN_DT_S:
                return
            self._last = now
            try:
                ht = c.state.get("heater") or {}
                cool = [n for n in c._roles()[2] if c._relay_is_on(n)]
                lt = time.localtime()
                row = "%s.%d,%.2f,%s,%.1f,%s,%d,%d,%.1f,%s,%s\r\n" % (
                    time.strftime("%H:%M:%S", lt), int((time.time() % 1) * 10), now - self._t0, "" if T is None else "%.3f" % float(T),
                    c.heater_pct(), ht.get("method") or "", 1 if c._fan_on() else 0, 1 if c._element_on() else 0, c.boot_left(),
                    " ".join("R%d" % n for n in cool), self._who())
                self._f.write(row)
                self._f.flush()
            except OSError as e:
                c.logs.app("히트건 기록 쓰기 실패 · %s · %s · 이 파일 멈춤" % (self.path, e), "warn")
                self._failed = True
                self._close()

    def close(self):
        with self._lock:
            self._close()
