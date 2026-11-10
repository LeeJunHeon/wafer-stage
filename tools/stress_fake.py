"""stress_fake.py - 동시성 부하 시험(fake-serial). 상태 조회 + 기록용 Z + 쿨러 · 히트건 켜고 끄기 + 이동 + 조그 + 유지 신호를 여러 스레드가 동시에 돌린다.

  python tools/stress_fake.py [초(기본 300)]
통과 기준: 응답이 엇갈린 일 0 · 잘못된 "끄기 미확인" 0 · 이동 시간 초과 0 · 다른 오류 0 · 통신 끊김 0.
진짜 Stage / Motion / Power 코드 경로(거래 잠금 · 입력 비우기 · 수집 · RS 해석)를 그대로 탄다.
"""
import os
import random
import sys
import threading
import time

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "tools"))
os.environ.setdefault("WAFER2_DATA", os.path.join(os.environ.get("TEMP", "."), "wafer2_stress"))

from core import motion as motion_mod, power as power_mod, stage as stage_mod  # noqa: E402
import fake_v11  # noqa: E402


def main(seconds):
    stage_mod.SERIAL_FACTORY = lambda *a: fake_v11.FakeV11(time_scale=0.02)
    st = stage_mod.Stage(port="FAKE")
    st.open()
    events = []
    st.on_event = lambda k, l: events.append((k, l))
    m = motion_mod.Motion(st)
    m.sync_from_status()
    p = power_mod.Power(st, heater_n=10)
    m.pre_xy = p.pre_xy
    st.watchdog(5000)
    m.sync_from_status()
    cnt = {"status": 0, "relay": 0, "heater": 0, "move": 0, "jog": 0, "z": 0, "ping": 0}
    bad = {"mismatch": 0, "heater_unconfirmed": 0, "timeout": 0, "other": 0}
    notes = []
    stop = threading.Event()
    lock = threading.Lock()

    def err(kind, e):
        with lock:
            bad[kind] += 1
            if len(notes) < 12:
                notes.append("%s: %s" % (kind, str(e)[:100]))

    def guard(fn):
        def run():
            while not stop.is_set():
                try:
                    fn()
                except power_mod.PowerError as e:
                    err("heater_unconfirmed" if "미확인" in str(e) else "other", e)
                except motion_mod.MotionError as e:
                    err("timeout" if e.code == "timeout" else "other", e)
                except stage_mod.StageTimeout as e:
                    err("timeout", e)
                except Exception as e:                                  # noqa: BLE001
                    err("other", e)
        return run

    def t_status():
        d = st.status()
        cnt["status"] += 1
        if d.get("z_mm") is None:
            err("mismatch", "ST 줄 해석")
        time.sleep(random.uniform(0.0, 0.03))

    def t_relay():
        n = random.randint(1, 9)
        rs = p.set(n, True, "stress")
        if not rs[n - 1]:
            err("mismatch", "R%d 켜기 응답이 꺼짐" % n)
        time.sleep(random.uniform(0.0, 0.05))
        rs = p.set(n, False, "stress")
        if rs[n - 1]:
            err("mismatch", "R%d 끄기 응답이 켜짐" % n)
        cnt["relay"] += 1

    def t_heater():
        p.heater_on("heat")
        time.sleep(random.uniform(0.05, 0.4))
        p.heater_off()
        if not p.heater_ok:
            err("heater_unconfirmed", "끄기")
        cnt["heater"] += 1
        time.sleep(random.uniform(0.0, 0.2))

    def t_move():
        m.move_xy(random.uniform(5, 200), random.uniform(5, 200))
        cnt["move"] += 1
        time.sleep(random.uniform(0.0, 0.1))

    def t_jog():
        m.jog(random.choice("xy"), random.choice([-1.0, 1.0]))
        cnt["jog"] += 1
        time.sleep(random.uniform(0.0, 0.1))

    def t_z():
        if m.lock.acquire(blocking=False):
            try:
                st.status()
                cnt["z"] += 1
            finally:
                m.lock.release()
        time.sleep(0.05)

    def t_ping():
        if st.ping():
            cnt["ping"] += 1
        time.sleep(0.3)

    ths = [threading.Thread(target=guard(f), daemon=True) for f in (t_status, t_relay, t_heater, t_move, t_jog, t_z, t_ping)]
    t0 = time.time()
    for t in ths:
        t.start()
    while time.time() - t0 < seconds:
        time.sleep(1.0)
        if sum(bad.values()) > 20:
            break
    stop.set()
    for t in ths:
        t.join(15)
    st.close()
    wd = [e for e in events if e[0] == "wd_trip"]
    print("부하 %d 초 · 횟수 %s" % (time.time() - t0, cnt))
    print("엇갈림 %d · 잘못된 끄기 미확인 %d · 이동 시간 초과 %d · 기타 오류 %d · 통신 끊김 %d" % (bad["mismatch"], bad["heater_unconfirmed"], bad["timeout"], bad["other"], len(wd)))
    for n in notes:
        print("  ", n)
    ok = not any(bad.values()) and not wd
    print("부하 시험 %s" % ("통과" if ok else "실패"))
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(int(sys.argv[1]) if len(sys.argv) > 1 else 300))
