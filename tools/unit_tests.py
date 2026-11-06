"""unit_tests.py - 단위 시험 (python tools/unit_tests.py [-v] [이름일부]). 장비 · 카메라 없이 돈다.

프레임 변환 · 퇴화 변환 거절 · 구간 반복 펼치기 · 설정 검증 · 이동 시간 한도 · 오래된 프레임 거절 · 위치 확인 카메라 이동 보정 …
"""
import os
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
sys.path.insert(0, os.path.join(ROOT, "backend"))

import numpy as np  # noqa: E402

from core import frames, geom  # noqa: E402


class FramesTest(unittest.TestCase):
    def test_roundtrip_euclid(self):
        th = np.radians(1.5)
        M = np.array([[np.cos(th), -np.sin(th), 12.0], [np.sin(th), np.cos(th), -7.0]])
        A = frames.Align(M)
        P = np.array([[100.0, 200.0], [1500.0, 900.0]])
        np.testing.assert_allclose(A.to_ref(A.to_cur(P)), P, atol=1e-9)

    def test_roundtrip_homography(self):
        M = np.array([[1.0, 0.01, 5.0], [-0.01, 1.0, 3.0], [1e-6, 2e-6, 1.0]])
        A = frames.Align(M, model="homography")
        P = np.array([[300.0, 400.0], [2000.0, 1500.0]])
        np.testing.assert_allclose(A.to_ref(A.to_cur(P)), P, atol=1e-6)

    def test_tag_mismatch_rejected(self):
        A = frames.Align.identity(frames.SHOT)
        with self.assertRaises(frames.FrameError):
            A.pt_to_ref(frames.PxPoint(1, 2, frames.SNAP))          # 단발 촬영 점을 촬영 정렬로 옮기면 거절
        with self.assertRaises(frames.FrameError):
            A.pt_to_cur(frames.PxPoint(1, 2, frames.SHOT))          # 기준 점만 현재로
        with self.assertRaises(frames.FrameError):
            frames.PxPoint(1, 2, "x")

    def test_shift(self):
        A = frames.Align([[1, 0, 10], [0, 1, 0]])
        self.assertAlmostEqual(A.shift_px([0, 0]), 10.0)


class DegenerateTest(unittest.TestCase):
    QUAD = [[0, 0], [1000, 0], [1000, 1000], [0, 1000]]

    def pts(self, uv):
        return [{"u": u, "v": v, "x": u * 0.15, "y": v * 0.15} for u, v in uv]

    def test_collinear_rejected(self):
        p = self.pts([(100, 100), (300, 300), (500, 500), (700, 700), (900, 900)])
        self.assertIn("직선", geom.degenerate_reason(p, self.QUAD))
        self.assertIsNone(geom.fit(p, self.QUAD))

    def test_clustered_rejected(self):
        p = self.pts([(100, 100), (150, 120), (120, 180), (180, 160), (140, 140)])
        self.assertIn("못 채움", geom.degenerate_reason(p, self.QUAD))

    def test_good_accepted(self):
        p = self.pts([(100, 100), (900, 120), (880, 900), (120, 880), (500, 500)])
        self.assertEqual(geom.degenerate_reason(p, self.QUAD), "")
        f = geom.fit(p, self.QUAD)
        self.assertIsNotNone(f)
        self.assertLess(f["max"], 1e-6)

    def test_too_few(self):
        self.assertIn("4개", geom.degenerate_reason(self.pts([(1, 1), (2, 2), (3, 5)]), self.QUAD))


class ConditionsTest(unittest.TestCase):
    def test_repeat_exact_range(self):
        import conditions as c
        steps = [{"type": "move_sample"}, {"type": "wait", "s": 1}, {"type": "wait", "s": 2}, {"type": "wait", "s": 3},
                 {"type": "repeat", "from": 2, "to": 3, "count": 3}]
        flat = [i for i, _ in c.expand(steps)]
        # 첫 통과 0,1,2,3 + 구간(2~3 = 인덱스 1,2)을 2번 더. 4번째 단계(인덱스 3)는 구간 밖이라 한 번만.
        self.assertEqual(flat, [0, 1, 2, 3, 1, 2, 1, 2])

    def test_repeat_invalid_skipped_and_rejected(self):
        import conditions as c
        steps = [{"type": "move_sample"}, {"type": "wait", "s": 1}, {"type": "repeat", "from": 1, "to": 2, "count": 2}]
        self.assertIsNotNone(c._validate_repeat(steps[2], 2, steps))
        self.assertEqual([i for i, _ in c.expand(steps)], [0, 1])

    def test_heater_relay_rejected(self):
        import conditions as c
        vals = {"relay": {"items": [{"enabled": True}] * 10}, "heater": {"relay": 10}, "limits": {"z_max_mm": 45}}
        self.assertIn("히트건 전용", c.validate_step({"type": "relay", "n": 10, "on": True}, vals, 45))
        self.assertIn("히트건 전용", c.validate_step({"type": "cooler", "action": "on", "target": "n", "n": 10}, vals, 45))
        self.assertIsNone(c.validate_step({"type": "relay", "n": 3, "on": True}, vals, 45))


class SchemaTest(unittest.TestCase):
    def test_defaults_validate(self):
        import settings_schema as s
        vals, errs, _w = s.validate(s.defaults())
        self.assertEqual(errs, {})

    def test_list_length_and_range(self):
        import settings_schema as s
        d = s.defaults()
        d["park_xy"] = []
        _v, errs, _w = s.validate(d)
        self.assertTrue(errs, "빈 좌표 쌍이 통과함")
        d = s.defaults()
        d["fixed"]["area_ratio"] = []
        _v, errs, _w = s.validate(d)
        self.assertTrue(errs, "빈 면적 범위가 통과함")


# ---------------------------------------------------------------------------
# 이동 · 전원 계층 (dry 스테이지, 느린 이동으로 중간 끼어들기 시험)
# ---------------------------------------------------------------------------
import threading  # noqa: E402
import time  # noqa: E402

from core import motion as motion_mod, power as power_mod, stage as stage_mod  # noqa: E402


def make_motion(move_s=0.0):
    stage_mod.DRY_MOVE_S = move_s
    st = stage_mod.Stage(dry=True)
    st.open()
    sent = []
    orig = st._write

    def rec(cmd):
        orig(cmd)                       # 가드가 거절하면 여기서 예외 - 기록은 가드를 통과한 것만
        sent.append(cmd)
    st._write = rec
    m = motion_mod.Motion(st)
    st.write_guard = m._guard
    return m, st, sent


class MotionTest(unittest.TestCase):
    def tearDown(self):
        stage_mod.DRY_MOVE_S = 0.0

    def test_xy_lifts_z_first_by_st(self):
        m, st, sent = make_motion()
        st._dry_xy["z"] = 12.0
        m.move_xy(50, 60)
        i_z, i_x = sent.index("mz 0.00"), sent.index("mx 50.0")
        self.assertLess(i_z, i_x)
        self.assertEqual(st.status()["z_mm"], 0.0)

    def test_z_lift_failure_blocks_xy(self):
        m, st, sent = make_motion()
        st._dry_xy["z"] = 12.0
        st.move_z_mm = lambda *a, **k: "(실패 흉내)"             # 올림이 실제로 안 됨 -> ST 가 여전히 12
        with self.assertRaises(motion_mod.MotionError) as c:
            m.move_xy(50, 60)
        self.assertEqual(c.exception.code, "z_lift")
        self.assertNotIn("mx 50.0", sent)

    def test_no_z_origin_rejects_xy(self):
        m, st, sent = make_motion()
        m.z_ok = False
        with self.assertRaises(motion_mod.MotionError) as c:
            m.move_xy(10, 10)
        self.assertEqual(c.exception.code, "no_z")
        self.assertFalse([x for x in sent if x.startswith("m")])

    def test_estop_cancels_pending_jog_chunks_and_locks(self):
        m, st, sent = make_motion(move_s=0.25)
        res = {}

        def run():
            try:
                m.jog("x", 150.0)                              # 8000 펄스(50 mm) 조각 3개
            except motion_mod.MotionError as e:
                res["e"] = e
        t = threading.Thread(target=run)
        t.start()
        time.sleep(0.1)
        n_before = len([x for x in sent if x.startswith("jx")])
        m.estop()
        t.join(5)
        n_after = len([x for x in sent if x.startswith("jx")])
        self.assertEqual(res["e"].code, "estop")
        self.assertEqual(n_before, 1)
        self.assertEqual(n_after, 1, "비상정지 뒤 조각이 더 나감")
        self.assertTrue(m.estop_locked)
        self.assertNotIn("sp", " ".join(x.split()[0] for x in sent))

    def test_recovery_order(self):
        m, st, sent = make_motion()
        m.estop()
        with self.assertRaises(motion_mod.MotionError) as c:
            m.move_xy(10, 10)
        self.assertEqual(c.exception.code, "locked")
        with self.assertRaises(motion_mod.MotionError) as c:
            m.apply_position(10, 10)                           # Z 원점 등록 전에는 적용 거절
        self.assertEqual(c.exception.code, "no_z")
        self.assertFalse([x for x in sent if x.startswith("sp")])
        m.register_z_origin()
        self.assertTrue(m.estop_locked, "Z 등록만으로 풀리면 안 됨")
        with self.assertRaises(motion_mod.MotionError):
            m.move_xy(10, 10)
        m.apply_position(10, 10)
        self.assertFalse(m.estop_locked)
        m.move_xy(20, 20)

    def test_sp_refused_when_estop_during_shot(self):
        m, st, sent = make_motion()
        g = m.gen
        m.estop()
        m.register_z_origin()
        with self.assertRaises(motion_mod.MotionError) as c:
            m.apply_position(5, 5, expect_gen=g)
        self.assertEqual(c.exception.code, "estop")
        self.assertFalse([x for x in sent if x.startswith("sp")])

    def test_sp_refused_when_position_changed_between_shot(self):
        m, st, sent = make_motion()
        st._dry_xy.update(x=10.0, y=10.0)
        before = (10.0, 10.0)
        st._dry_xy.update(x=40.0, y=10.0)
        with self.assertRaises(motion_mod.MotionError) as c:
            m.apply_position(10, 10, expect_xy=before)
        self.assertEqual(c.exception.code, "moved")

    def test_move_timeout_sends_stop_and_keeps_connection(self):
        m, st, sent = make_motion()
        st.dry_hang = True
        m.speed_pps, m.accel = 60000, 400000                    # 한도를 짧게(짧은 거리 + 여유 2 s)
        t0 = time.time()
        with self.assertRaises(motion_mod.MotionError) as c:
            m.move_xy(1, 0)
        self.assertEqual(c.exception.code, "timeout")
        self.assertLess(time.time() - t0, 6.0)
        self.assertFalse(m.xy_ok, "시간 초과 뒤 위치를 믿으면 안 됨")
        self.assertFalse(st.dry_hang)
        self.assertTrue(st.status() is not None)                # 연결 유지

    def test_deadline_grows_with_distance(self):
        m, st, sent = make_motion()
        self.assertLess(m.deadline_s(10), m.deadline_s(200))
        self.assertGreater(m.deadline_s(0), 0)

    def test_reboot_event_locks(self):
        m, st, sent = make_motion()
        seen = []
        m.on_reboot = seen.append
        st._event("reboot", "=== 3-AXIS V11 ===")
        self.assertTrue(m.estop_locked and m.rebooted)
        self.assertEqual(len(seen), 1)
        with self.assertRaises(motion_mod.MotionError):
            m.move_xy(1, 1)


class PowerTest(unittest.TestCase):
    def setUp(self):
        stage_mod.DRY_MOVE_S = 0.0
        self.st = stage_mod.Stage(dry=True)
        self.st.open()
        self.p = power_mod.Power(self.st, heater_n=10)

    def test_heater_relay_refused_for_others(self):
        for owner in ("recipe", "manual", "cooler"):
            with self.assertRaises(power_mod.PowerError):
                self.p.set(10, True, owner)
        self.assertFalse(self.st._dry_relay[9])
        self.p.heater_on()
        self.assertTrue(self.st._dry_relay[9])

    def test_heater_off_always_sent_even_if_cache_says_off(self):
        sent = []
        orig = self.st._write
        self.st._write = lambda c: (sent.append(c), orig(c))[1]
        self.p.heater_off()
        self.assertIn("r 10 0", sent)

    def test_heater_off_failure_blocks_xy(self):
        m = motion_mod.Motion(self.st)
        m.pre_xy = self.p.pre_xy
        self.p.heater_on()
        orig = self.st.relay_set

        def bad(n, on):                                         # 끄기를 보냈는데 켜진 채라고 답함
            orig(n, on)
            return [True] * 10
        self.st.relay_set = bad
        with self.assertRaises(motion_mod.MotionError) as c:
            m.move_xy(5, 5)
        self.assertEqual(c.exception.code, "heater")
        self.assertFalse(self.p.heater_ok)

    def test_heater_for_always_turns_off(self):
        calls = []
        try:
            self.p.heater_for(5, sleep=lambda s: (_ for _ in ()).throw(RuntimeError("예외")))
        except RuntimeError:
            pass
        self.assertFalse(self.st._dry_relay[9])
        with self.assertRaises(power_mod.PowerError):
            self.p.heater_for(self.p.max_heat_s + 1)
        del calls

    def test_all_off(self):
        self.p.set(1, True, "engine")
        self.p.heater_on()
        self.p.all_off()
        self.assertFalse(any(self.st._dry_relay))


class FakeBufferedCam:
    """이동 전 프레임 depth 장이 큐에 남은 카메라. content 가 현재 장면, 큐에는 옛 장면(content_old)."""

    def __init__(self, depth, old="이동 전", new="이동 후"):
        self.q = [old] * depth
        self.new = new
        self.cur = None

    def grab(self):
        self.cur = self.q.pop(0) if self.q else self.new
        return True

    def retrieve(self):
        return self.cur


class FrameSourceTest(unittest.TestCase):
    def test_naive_read_returns_stale(self):
        cam = FakeBufferedCam(4)
        cam.grab()
        self.assertEqual(cam.retrieve(), "이동 전")

    def test_frame_after_skips_stale_buffer(self):
        from core import framesrc
        cam = FakeBufferedCam(4)
        fs = framesrc.FrameSource(cam, min_fresh=5)
        st = fs.frame_after(time.monotonic() - 0.01, timeout_s=2.0)
        self.assertEqual(st.frame, "이동 후")

    def test_frame_after_waits_for_move_end_time(self):
        from core import framesrc
        cam = FakeBufferedCam(0)
        fs = framesrc.FrameSource(cam, min_fresh=2)
        t_after = time.monotonic() + 0.3
        st = fs.frame_after(t_after, timeout_s=3.0)
        self.assertGreaterEqual(st.t, t_after)

    def test_frame_after_times_out(self):
        from core import framesrc

        class Dead:
            def grab(self):
                return False

            def retrieve(self):
                return None
        fs = framesrc.FrameSource(Dead(), min_fresh=2)
        with self.assertRaises(framesrc.StaleFrameError):
            fs.frame_after(time.monotonic(), timeout_s=0.3)

    def test_motion_records_last_move_end(self):
        m, st, sent = make_motion()
        t0 = time.monotonic()
        m.move_xy(5, 5)
        self.assertGreaterEqual(m.last_move_end, t0)


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:]], verbosity=2)
