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


if __name__ == "__main__":
    unittest.main(argv=[sys.argv[0]] + [a for a in sys.argv[1:]], verbosity=2)
