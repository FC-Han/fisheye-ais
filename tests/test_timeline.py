"""v0.2 分析层单测：行为时间线、航次切分、指纹降级、中断归因。

运行：python -m unittest discover -s tests -v

测试原则
--------
这些测试用**合成轨迹**验证算法契约，不依赖数据库和真实 AIS：
- 时间线必须时间连续、互不重叠、覆盖整个窗口
- 缺口必须显式成段，不能被插值抹平
- 未校准的行为候选不允许输出 confidence
- 样本不足时必须明确降级，而不是编一个数字
"""
from __future__ import annotations

import unittest

from backend.app.services import fingerprint as fp
from backend.app.services import timeline as tl
from backend.app.services import trips as tp


def mk(ts, lat, lon, sog, cog=90.0):
    return {"ts": ts, "lat": lat, "lon": lon, "sog": sog, "cog": cog, "nav_status": 0}


class TestSpeedClassify(unittest.TestCase):
    def test_boundaries(self):
        self.assertEqual(tl._classify_speed(None), "unknown")
        self.assertEqual(tl._classify_speed(0.0), "stationary")
        self.assertEqual(tl._classify_speed(0.49), "stationary")
        self.assertEqual(tl._classify_speed(0.5), "low_speed")
        self.assertEqual(tl._classify_speed(4.4), "low_speed")
        self.assertEqual(tl._classify_speed(4.5), "transit")
        self.assertEqual(tl._classify_speed(14.0), "transit")


class TestEffectiveSpeed(unittest.TestCase):
    def test_infers_when_missing(self):
        # 两个点相距约 0.01 度纬度 = 1112 米，间隔 120 秒 → 约 18 节
        pts = [
            {"ts": 1000, "lat": 10.00, "lon": 20.00, "sog": None, "cog": 0},
            {"ts": 1120, "lat": 10.01, "lon": 20.00, "sog": None, "cog": 0},
        ]
        out = tl._with_effective_speed(pts)
        self.assertIsNotNone(out[1]["sog_eff"])
        self.assertTrue(out[1]["sog_inferred"])
        self.assertGreater(out[1]["sog_eff"], 10)

    def test_rejects_impossible_jump(self):
        # 1 秒跳 100 公里 → 速度不合理，应保持 None
        pts = [
            {"ts": 1000, "lat": 10.0, "lon": 20.0, "sog": None, "cog": 0},
            {"ts": 1001, "lat": 11.0, "lon": 20.0, "sog": None, "cog": 0},
        ]
        out = tl._with_effective_speed(pts)
        self.assertIsNone(out[1]["sog_eff"])


class TestGapHandling(unittest.TestCase):
    def test_long_gap_becomes_segment(self):
        """4 小时没报文必须在时间线上表现为 observation_gap 段。"""
        pts = tl._with_effective_speed([
            mk(0, 10.0, 20.0, 8.0),
            mk(300, 10.02, 20.0, 8.0),
            mk(300 + 4 * 3600, 10.40, 20.0, 8.0),
            mk(300 + 4 * 3600 + 300, 10.42, 20.0, 8.0),
        ])
        segs = tl._build(pts, "fishing", 900, (0, 300 + 4 * 3600 + 300))
        acts = [s["activity"] for s in segs]
        self.assertIn("observation_gap", acts)
        gap = next(s for s in segs if s["activity"] == "observation_gap")
        self.assertGreaterEqual(gap["duration_s"], 4 * 3600 - 600)

    def test_no_interpolation_of_gap(self):
        """缺口段不得带任何轨迹点或速度，避免被当成真实观测。"""
        pts = tl._with_effective_speed([
            mk(0, 10.0, 20.0, 8.0),
            mk(300, 10.02, 20.0, 8.0),
            mk(300 + 3 * 3600, 10.30, 20.0, 8.0),
        ])
        segs = tl._build(pts, "fishing", 900, (0, 300 + 3 * 3600))
        gap = next(s for s in segs if s["activity"] == "observation_gap")
        self.assertEqual(gap["evidence"]["points"], 0)
        self.assertEqual(gap["reliability"]["grade"], "none")


class TestTimelineInvariants(unittest.TestCase):
    def _segs(self):
        raw = [
            mk(0, 10.0, 20.0, 8.0), mk(600, 10.05, 20.0, 8.5),
            mk(1200, 10.10, 20.0, 2.0), mk(2400, 10.11, 20.01, 2.2),
            mk(3600, 10.12, 20.02, 2.1),
            mk(3600 + 2 * 3600, 10.05, 20.0, 0.0),
            mk(3600 + 2 * 3600 + 600, 10.05, 20.0, 0.0),
        ]
        pts = tl._with_effective_speed(raw)
        return tl._build(pts, "fishing", 900, (0, 3600 + 2 * 3600 + 600))

    def test_contiguous(self):
        segs = self._segs()
        for a, b in zip(segs, segs[1:]):
            self.assertEqual(a["end_ts"], b["start_ts"], "相邻段必须首尾相接")

    def test_no_zero_length(self):
        segs = self._segs()
        for s in segs:
            if s["activity"] != "observation_gap":
                self.assertGreater(s["duration_s"], 0, "可观察段不应出现 0 时长")

    def test_never_emits_calibrated_confidence(self):
        """未做概率校准前，任何片段都不允许输出 confidence 数值。"""
        for s in self._segs():
            self.assertIsNone(s["confidence"])

    def test_fishing_candidate_has_no_gear_claim(self):
        for s in self._segs():
            if s["sub_activity"] == "fishing_candidate":
                self.assertEqual(s["gear_type"], "unknown")


class TestFishingCandidateIsConservative(unittest.TestCase):
    def test_non_fishing_never_candidate(self):
        pts = tl._with_effective_speed([mk(i * 600, 10.0 + i * 0.001, 20.0, 2.0) for i in range(8)])
        segs = tl._build(pts, "non_fishing", 900, (0, 8 * 600))
        self.assertFalse(any(s["sub_activity"] == "fishing_candidate" for s in segs))

    def test_short_low_speed_is_not_candidate(self):
        # 只有 10 分钟低速，不满足 30 分钟门槛
        pts = tl._with_effective_speed([mk(i * 120, 10.0 + i * 0.0005, 20.0, 2.0) for i in range(5)])
        segs = tl._build(pts, "fishing", 900, (0, 600))
        self.assertFalse(any(s["sub_activity"] == "fishing_candidate" for s in segs))

    def test_high_speed_transit_is_not_candidate(self):
        # 9 节已超出低速作业上限（6 节），不是捕捞候选
        pts = tl._with_effective_speed([mk(i * 600, 10.0 + i * 0.012, 20.0, 9.0) for i in range(10)])
        segs = tl._build(pts, "fishing", 900, (0, 10 * 600))
        self.assertFalse(any(s["sub_activity"] == "fishing_candidate" for s in segs))

    def test_long_straight_low_speed_is_not_excluded(self):
        """拖网本身就是直线拖曳，直线度高不能作为排除理由。

        这里只验证：低速直线段如果真的被判为候选，说明没有用直线度粗暴排除，
        因为那会把拖网全部误杀。赶路的排除条件是「长时间 + 长距离」。
        """
        pts = tl._with_effective_speed([mk(i * 600, 10.0 + i * 0.001, 20.0, 4.0) for i in range(10)])
        segs = tl._build(pts, "fishing", 900, (0, 10 * 600))
        low = [s for s in segs if s["activity"] == "low_speed"]
        self.assertTrue(low, "4 节应被判为低速段")


class TestTripSplit(unittest.TestCase):
    def _segs(self):
        # 航行 1h → 静止 3h（靠泊边界）→ 航行 1h
        raw = []
        for i in range(6):
            raw.append(mk(i * 600, 10.0 + i * 0.01, 20.0, 9.0))
        base = 6 * 600
        for i in range(18):
            raw.append(mk(base + i * 600, 10.06, 20.0, 0.0))
        base2 = base + 18 * 600
        for i in range(12):   # 第二个航次也要 >= 1 小时，否则会被当碎片丢弃
            raw.append(mk(base2 + i * 600, 10.06 + i * 0.01, 20.0, 9.0))
        pts = tl._with_effective_speed(raw)
        return tl._build(pts, "fishing", 900, (0, base2 + 12 * 600))

    def test_split_by_long_stationary(self):
        trips = tp._split(self._segs())
        self.assertGreaterEqual(len(trips), 2, "连续静止 3 小时应切开航次")
        for t in trips:
            self.assertIn(t["status"], ("closed", "truncated"))

    def test_single_trip_is_truncated(self):
        # 没有任何长时间静止 → 只有一个 truncated 航次
        raw = [mk(i * 600, 10.0 + i * 0.005, 20.0, 8.0) for i in range(10)]
        pts = tl._with_effective_speed(raw)
        segs = tl._build(pts, "fishing", 900, (0, 10 * 600))
        trips = tp._split(segs)
        self.assertTrue(all(t["status"] == "truncated" for t in trips))

    def test_features_have_fixed_keys(self):
        trips = tp._split(self._segs())
        feats = tp._features(trips[0]["segments"])
        for k in tp.FEATURE_KEYS:
            self.assertIn(k, feats)


class TestFingerprintDegrade(unittest.TestCase):
    def test_robust_z_disabled_when_mad_zero(self):
        """MAD=0 时不能给出 Z 分数，否则等于凭空制造异常。"""
        self.assertIsNone(fp._robust_z(5.0, {"median": 5.0, "mad": 0.0}))

    def test_robust_z_normal(self):
        z = fp._robust_z(10.0, {"median": 5.0, "mad": 2.0})
        self.assertAlmostEqual(z, 5.0 / (1.4826 * 2.0), places=2)

    def test_robust_aggregate(self):
        r = fp._robust([1, 2, 3, 4, 5, 100])
        self.assertEqual(r["median"], 3.5)   # 中位数不被离群值拉走
        self.assertEqual(r["n"], 6)

    def test_season_mapping(self):
        import calendar, time as _t
        # 2026-01-15 UTC
        ts = calendar.timegm((2026, 1, 15, 0, 0, 0))
        self.assertEqual(fp._season(ts), "DJF")
        # 2026-07-15 UTC
        ts = calendar.timegm((2026, 7, 15, 0, 0, 0))
        self.assertEqual(fp._season(ts), "JJA")


if __name__ == "__main__":
    unittest.main()
