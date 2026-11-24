"""v0.3 单测：港口判定、环境引擎采样/分桶、异步任务队列。

运行：python -m unittest discover -s tests -v

测试原则
--------
- 不依赖网络：环境引擎只测「采样、分桶、聚合」这些纯函数，不发起 Copernicus 请求
- 不污染主库：任务队列测试用临时 SQLite
- 边界必须是「如实降级」而不是「编一个数」：港口表为空时不能谎称在港，
  任务失败时必须写 error 而不是假装 done
"""
from __future__ import annotations

import os
import tempfile
import time
import unittest
from contextlib import closing
from pathlib import Path

from backend.app.services import jobs as job_svc
from backend.app.services import ocean as ocean_svc
from backend.app.services import ports as port_svc
from core.db import connect


class TestPortClassification(unittest.TestCase):
    """港口判定。依赖 ports 表（scripts/load_wpi.py 导入）；表为空时跳过。"""

    @classmethod
    def setUpClass(cls):
        port_svc.reset_cache()
        cls.count = port_svc.port_count()

    def setUp(self):
        if self.count == 0:
            self.skipTest("ports 表为空，先运行 scripts/load_wpi.py")

    def test_in_port_radius_by_size(self):
        # 大港给大半径、小码头给小半径，否则泊位范围十几公里的港会被判成锚地
        self.assertGreater(port_svc.in_port_radius("L"), port_svc.in_port_radius("M"))
        self.assertGreater(port_svc.in_port_radius("M"), port_svc.in_port_radius("S"))
        self.assertGreater(port_svc.in_port_radius("S"), port_svc.in_port_radius("V"))
        self.assertEqual(port_svc.in_port_radius(None), port_svc.PORT_RADIUS_DEFAULT_KM)

    def test_harbour_centre_is_port_stay(self):
        # 阿伯丁港参考点
        sub, ev = port_svc.classify_stationary(57.144, -2.093)
        self.assertEqual(sub, "port_stay")
        self.assertIsNotNone(ev["nearest_port"])
        self.assertLess(ev["nearest_port"]["distance_km"], 5)

    def test_open_ocean_is_anchorage_candidate(self):
        # 北海中部，25 km 内没有港口
        sub, ev = port_svc.classify_stationary(54.0, 2.5)
        self.assertEqual(sub, "anchorage_candidate")
        self.assertIsNone(ev["nearest_port"])
        self.assertTrue(ev["offshore"])

    def test_never_claims_conclusion(self):
        # 两种结果都必须是「候选」语义，不能出现确定性的措辞
        for lat, lon in ((57.144, -2.093), (54.0, 2.5)):
            sub, ev = port_svc.classify_stationary(lat, lon)
            self.assertIn(sub, ("port_stay", "anchorage_candidate"))
            self.assertIn("nearest_port", ev)
            self.assertIn("note", ev)

    def test_distance_is_reported(self):
        _, ev = port_svc.classify_stationary(57.144, -2.093)
        self.assertGreater(ev["nearest_port"]["distance_km"], 0.0)
        self.assertLess(ev["nearest_port"]["distance_km"], 100.0)


class TestOceanSampling(unittest.TestCase):
    """采样与分桶：纯函数，不访问网络。"""

    def _points(self, n, step=60, lat=54.0, lon=2.0):
        t0 = 1793000000
        return [
            {"ts": t0 + i * step, "lat": lat + i * 1e-4, "lon": lon, "mmsi": 1}
            for i in range(n)
        ]

    def test_sample_respects_interval(self):
        pts = self._points(120, step=60)   # 每 60 秒一个点，共 2 小时
        got = ocean_svc._sample(pts, minutes=30, max_points=100)
        self.assertLess(len(got), len(pts))
        # 末尾会强制补一个终点（保证时间覆盖到窗口末端），所以只看除最后一对之外的间隔
        for a, b in zip(got, got[1:-1]):
            self.assertGreaterEqual(b["ts"] - a["ts"], 30 * 60)

    def test_sample_keeps_endpoints(self):
        pts = self._points(120, step=60)
        got = ocean_svc._sample(pts, minutes=30, max_points=100)
        self.assertEqual(got[0]["ts"], pts[0]["ts"])
        self.assertEqual(got[-1]["ts"], pts[-1]["ts"])

    def test_sample_caps_points(self):
        pts = self._points(2000, step=10)
        got = ocean_svc._sample(pts, minutes=1, max_points=240)
        self.assertLessEqual(len(got), 241)   # 允许末尾补一个终点

    def test_sample_empty(self):
        self.assertEqual(ocean_svc._sample([]), [])

    def test_group_by_tile_and_day(self):
        t0 = 1793000000
        pts = [
            {"ts": t0, "lat": 54.1, "lon": 2.1, "mmsi": 1},
            {"ts": t0 + 3600, "lat": 54.9, "lon": 2.9, "mmsi": 1},   # 同一 1° 瓦片
            {"ts": t0 + 86400, "lat": 54.1, "lon": 2.1, "mmsi": 1},  # 次日 → 新组
        ]
        groups = ocean_svc._group(pts)
        self.assertEqual(len(groups), 2)
        self.assertEqual(sum(len(v) for v in groups.values()), 3)

    def test_tile_of_floors_to_degree(self):
        self.assertEqual(ocean_svc._tile_of(54.9, 2.9), (54.0, 2.0))
        self.assertEqual(ocean_svc._tile_of(-0.5, -0.5), (-1.0, -1.0))

    def test_day_bounds_are_utc(self):
        ts = 1793000000
        a, b = ocean_svc._day_bounds(ts)
        self.assertEqual(b - a, 86399)
        self.assertLessEqual(a, ts)
        self.assertGreaterEqual(b, ts)


class TestOceanCapabilities(unittest.TestCase):
    def test_capabilities_shape(self):
        caps = ocean_svc.capabilities()
        for key in ("available", "reason", "dependencies", "products",
                    "not_implemented", "caveat", "limits"):
            self.assertIn(key, caps)
        # 不可用时必须给出原因，不能只返回 False
        if not caps["available"]:
            self.assertTrue(caps["reason"])

    def test_declares_missing_products(self):
        # 没实现的要写清楚，避免用户以为都能查
        text = " ".join(ocean_svc.capabilities()["not_implemented"])
        self.assertIn("实时叶绿素", text)
        self.assertIn("风场", text)

    def test_products_carry_basis(self):
        """basis 必须显式声明：实时场 vs 气候态，这是不能含糊的地方。"""
        caps = ocean_svc.capabilities()
        by_key = {p["key"]: p for p in caps["products"]}
        self.assertEqual(by_key["physics"]["basis"], "analysis_forecast")
        self.assertEqual(by_key["waves"]["basis"], "analysis_forecast")
        self.assertEqual(by_key["bgc_clim"]["basis"], "climatology")
        # 气候态必须标注参考年份，否则无法判断它代表哪段时间
        self.assertTrue(caps["climatology_reference"]["years"])

    def test_lag_is_bounded(self):
        self.assertGreater(ocean_svc.MAX_LAG_HOURS, 0)
        self.assertLessEqual(ocean_svc.MAX_LAG_HOURS, 72)


class TestSeaState(unittest.TestCase):
    """海况分档是查表，不是推断；边界必须严格按道格拉斯表。"""

    def test_thresholds(self):
        self.assertEqual(ocean_svc.sea_state_of(0.05)["code"], "calm")
        self.assertEqual(ocean_svc.sea_state_of(0.3)["code"], "smooth")
        self.assertEqual(ocean_svc.sea_state_of(1.0)["code"], "slight")
        self.assertEqual(ocean_svc.sea_state_of(2.0)["code"], "moderate")
        self.assertEqual(ocean_svc.sea_state_of(3.0)["code"], "rough")
        self.assertEqual(ocean_svc.sea_state_of(5.0)["code"], "very_rough")

    def test_boundary_is_exclusive_upper(self):
        # 0.5 m 恰好落在下一档，不能划进 calm
        self.assertEqual(ocean_svc.sea_state_of(0.5)["code"], "slight")

    def test_none_is_unknown_not_zero(self):
        # 缺测必须说未知，不能默认成「无浪」
        self.assertEqual(ocean_svc.sea_state_of(None)["code"], "unknown")


class TestProductNormalization(unittest.TestCase):
    def test_default_uses_near_realtime_wind(self):
        # 默认风源必须是能覆盖「今天」的那个：L4 观测滞后 2 天、ERA5 滞后 5 天，
        # 拿它们当默认会让最近两天的轨迹一条风都取不到。
        default = ocean_svc.normalize_products(None)
        self.assertIn("wind_gfs", default)
        self.assertNotIn("wind_era5", default)
        self.assertEqual(
            default, ["physics", "waves", "wind_gfs", "bgc_clim"]
        )

    def test_csv_and_dedupe(self):
        self.assertEqual(ocean_svc.normalize_products("waves,physics,waves"),
                         ["waves", "physics"])

    def test_unknown_rejected(self):
        with self.assertRaises(ValueError):
            ocean_svc.normalize_products("wind_gfs_nope")


class TestColumnScopedPersist(unittest.TestCase):
    """同一个 (mmsi, ts) 会被多个产品先后写入，整行覆盖会互相清空数据。"""

    def setUp(self):
        from core import db as db_mod
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = db_mod.settings.db_path
        db_mod.settings.db_path = Path(self._tmp.name) / "ocean.db"
        db_mod.init_db()

    def tearDown(self):
        from core import db as db_mod
        db_mod.settings.db_path = self._orig
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(Path(self._tmp.name) / f"ocean.db{suffix}")
            except OSError:
                pass
        self._tmp.cleanup()

    def _row(self, **values):
        return {
            "mmsi": 111, "ts": 1793000000, "lat": 37.5, "lon": 23.5,
            "values": values, "source_time": "2026-10-27 03:00:00",
            "lag_hours": 0.5, "quality": "ok", "basis": "analysis_forecast",
        }

    def test_second_product_does_not_wipe_first(self):
        ocean_svc._persist([self._row(sst_c=22.3, u_m_s=0.1, v_m_s=0.05)], "physics")
        ocean_svc._persist([self._row(wave_height_m=0.8)], "waves")

        with closing(connect(readonly=True)) as conn:
            r = conn.execute("SELECT * FROM ocean_features WHERE mmsi=111").fetchone()
        self.assertAlmostEqual(r["sst_c"], 22.3, places=3)
        self.assertAlmostEqual(r["wave_height_m"], 0.8, places=3)
        self.assertEqual(sorted((r["products"] or "").split(",")), ["phy", "wav"])

    def test_climatology_does_not_overwrite_lag(self):
        ocean_svc._persist([self._row(sst_c=22.3)], "physics")
        ocean_svc._persist([{
            **self._row(chl_mg_m3=0.09),
            "source_time": "2019-2023 年 10 月气候态",
            "lag_hours": None, "basis": "climatology",
        }], "bgc_clim")

        with closing(connect(readonly=True)) as conn:
            r = conn.execute("SELECT * FROM ocean_features WHERE mmsi=111").fetchone()
        # 叶绿素是气候态，绝不能把实时产品的 lag 改成 null / 0
        self.assertEqual(r["lag_hours"], 0.5)
        self.assertEqual(r["source_time"], "2026-10-27 03:00:00")
        self.assertEqual(r["chl_basis"], "climatology")
        self.assertTrue(r["chl_ref"])

    def test_land_mask_row_still_records_product(self):
        ocean_svc._persist([{
            **self._row(), "values": {}, "quality": "land_mask",
        }], "waves")
        with closing(connect(readonly=True)) as conn:
            r = conn.execute("SELECT * FROM ocean_features WHERE mmsi=111").fetchone()
        # 没有数值也要记下「这个产品查过了」，否则每次都会重复下载瓦片
        self.assertIn("wav", (r["products"] or "").split(","))


class TestJobQueue(unittest.TestCase):
    """任务队列：用临时库，不碰主库。"""

    def setUp(self):
        from core import db as db_mod
        # Windows 下 sqlite 的 -wal/-shm 可能短暂占用目录，退出清理时忽略即可
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = db_mod.settings.db_path
        db_mod.settings.db_path = Path(self._tmp.name) / "test.db"
        db_mod.init_db()

    def tearDown(self):
        from core import db as db_mod
        db_mod.settings.db_path = self._orig
        for suffix in ("", "-wal", "-shm"):
            p = Path(self._tmp.name) / f"test.db{suffix}"
            try:
                os.unlink(p)
            except OSError:
                pass
        self._tmp.cleanup()

    def test_enqueue_and_claim(self):
        r = job_svc.enqueue("ocean_track", {"mmsi": 123, "hours": 6})
        self.assertEqual(r["status"], "queued")
        job = job_svc.claim_next()
        self.assertEqual(job["job_id"], r["job_id"])
        self.assertEqual(job["params"]["mmsi"], 123)
        # 抢过之后不能再被抢一次
        self.assertIsNone(job_svc.claim_next())

    def test_dedupe_same_params(self):
        a = job_svc.enqueue("ocean_track", {"mmsi": 1, "hours": 6})
        b = job_svc.enqueue("ocean_track", {"mmsi": 1, "hours": 6})
        self.assertEqual(a["job_id"], b["job_id"])
        self.assertTrue(b["deduped"])
        # 参数不同则必须是新任务
        c = job_svc.enqueue("ocean_track", {"mmsi": 2, "hours": 6})
        self.assertNotEqual(a["job_id"], c["job_id"])

    def test_finish_writes_result(self):
        r = job_svc.enqueue("ocean_track", {"mmsi": 7, "hours": 6})
        job_svc.claim_next()
        rid = job_svc.finish(r["job_id"], result={"status": "ok", "sst_c": {"median": 20.0}},
                             meta={"kind": "ocean_track", "model_version": "ocean-v1"})
        self.assertIsNotNone(rid)
        got = job_svc.get(r["job_id"], with_result=True)
        self.assertEqual(got["status"], "done")
        self.assertEqual(got["result"]["status"], "ok")
        self.assertEqual(got["result_meta"]["model_version"], "ocean-v1")

    def test_finish_with_error_marks_failed(self):
        r = job_svc.enqueue("ocean_track", {"mmsi": 8})
        job_svc.claim_next()
        job_svc.finish(r["job_id"], error="RuntimeError: 抓取失败")
        got = job_svc.get(r["job_id"])
        self.assertEqual(got["status"], "failed")
        self.assertIn("抓取失败", got["error"])

    def test_unknown_kind_rejected(self):
        with self.assertRaises(ValueError):
            job_svc.enqueue("not_a_kind", {})

    def test_recover_stale(self):
        r = job_svc.enqueue("ocean_track", {"mmsi": 9})
        job_svc.claim_next()
        # 手动把开始时间调到很久以前，模拟进程被杀
        from contextlib import closing
        from core.db import connect
        with closing(connect()) as conn:
            conn.execute("UPDATE analysis_jobs SET started_at=? WHERE job_id=?",
                         (int(time.time()) - 7200, r["job_id"]))
            conn.commit()
        self.assertEqual(job_svc.recover_stale(timeout_s=1800), 1)
        self.assertEqual(job_svc.get(r["job_id"])["status"], "queued")

    def test_queue_depth(self):
        job_svc.enqueue("ocean_track", {"mmsi": 11})
        depth = job_svc.queue_depth()
        self.assertEqual(depth.get("queued"), 1)


if __name__ == "__main__":
    unittest.main()


class TestCachedRowIsPerProduct(unittest.TestCase):
    """只判断「行存在」会让后写的产品误以为全部命中缓存。"""

    def test_physics_row_does_not_satisfy_waves(self):
        row = {"products": None, "sst_c": 22.1, "wave_height_m": None}
        self.assertFalse(ocean_svc._row_has_product(row, "waves"))
        self.assertTrue(ocean_svc._row_has_product(row, "physics"))

    def test_products_field_counts_even_without_values(self):
        # 陆地掩膜的点没有值，但产品已记录，不能每次重复下载瓦片
        row = {"products": "wav", "wave_height_m": None}
        self.assertTrue(ocean_svc._row_has_product(row, "waves"))


class TestEvidenceFallback(unittest.TestCase):
    """时间线画得出缺口，证据卡就必须能解释这段缺口。"""

    def setUp(self):
        from core import db as db_mod
        self._tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self._orig = db_mod.settings.db_path
        db_mod.settings.db_path = Path(self._tmp.name) / "ev.db"
        db_mod.init_db()
        with closing(connect()) as conn:
            conn.executemany(
                "INSERT INTO ais_positions (mmsi, ts, lat, lon, sog, cog) VALUES (?,?,?,?,?,?)",
                [
                    (999, 1793000000, 37.50, 23.50, 12.0, 90.0),
                    # 断档 8 小时后重新出现，位置只有小幅漂移
                    (999, 1793028800, 37.62, 23.63, 9.0, 95.0),
                ],
            )
            conn.commit()

    def tearDown(self):
        from core import db as db_mod
        db_mod.settings.db_path = self._orig
        for suffix in ("", "-wal", "-shm"):
            try:
                os.unlink(Path(self._tmp.name) / f"ev.db{suffix}")
            except OSError:
                pass
        self._tmp.cleanup()

    def test_card_built_without_event_row(self):
        from backend.app.services import evidence as ev

        card = ev.gap_evidence_card(999, 1793000000, 1793028800)
        self.assertTrue(card["found"])
        self.assertAlmostEqual(card["duration_h"], 8.0, places=1)
        # 必须把观测事实和推断分开，且给出复核状态
        kinds = {e["kind"] for e in card["evidence"]}
        self.assertIn("observed", kinds)
        self.assertTrue(card["alternative_explanations"])
        self.assertIn(card["review_status"], ("unreviewed", "unverifiable", "confirmed", "rejected"))


class TestWindDerivation(unittest.TestCase):
    """风场只给 u/v 分量，风速风向全是派生量，算错一个方向整条结论都反了。"""

    def test_direction_is_where_wind_comes_from(self):
        # u=+1 表示空气向东运动 → 风来自西边 → 270°
        speed, deg = ocean_svc.wind_from_uv(1.0, 0.0)
        self.assertAlmostEqual(speed, 1.0, places=3)
        self.assertAlmostEqual(deg, 270.0, places=1)
        # v=+1 表示空气向北运动 → 风来自南边 → 180°
        _, deg = ocean_svc.wind_from_uv(0.0, 1.0)
        self.assertAlmostEqual(deg, 180.0, places=1)
        # 北风：来自北边，空气向南运动
        _, deg = ocean_svc.wind_from_uv(0.0, -3.0)
        self.assertAlmostEqual(deg, 0.0, places=1)

    def test_missing_component_yields_null(self):
        self.assertEqual(ocean_svc.wind_from_uv(None, 2.0), (None, None))

    def test_beaufort_boundaries(self):
        # 边界必须落到上一档，不能把 5.5 算成 3 级
        self.assertEqual(ocean_svc.beaufort_of(0.2)["force"], 0)
        self.assertEqual(ocean_svc.beaufort_of(0.3)["force"], 1)
        self.assertEqual(ocean_svc.beaufort_of(5.5)["force"], 4)
        self.assertEqual(ocean_svc.beaufort_of(32.7)["force"], 12)

    def test_beaufort_unknown_is_not_calm(self):
        # 缺测不能默认成 0 级无风
        self.assertIsNone(ocean_svc.beaufort_of(None)["force"])

    def test_compass_rounding(self):
        self.assertEqual(ocean_svc.wind_dir_text(0), "北")
        self.assertEqual(ocean_svc.wind_dir_text(359.9), "北")
        self.assertEqual(ocean_svc.wind_dir_text(352.8), "北")
        self.assertEqual(ocean_svc.wind_dir_text(90), "东")
        self.assertEqual(ocean_svc.wind_dir_text(225), "西南")


class TestSharedColumnProducts(unittest.TestCase):
    """三个风产品写同一批 wind_* 列，不能靠「列有值」判断谁跑过。"""

    def test_wind_products_share_columns(self):
        for key in ("wind", "wind_gfs", "wind_era5"):
            self.assertFalse(
                ocean_svc._has_exclusive_columns(key),
                f"{key} 的列与其它风产品共享，不应被判为独占",
            )

    def test_physics_owns_its_columns(self):
        self.assertTrue(ocean_svc._has_exclusive_columns("physics"))
        self.assertTrue(ocean_svc._has_exclusive_columns("waves"))
        self.assertTrue(ocean_svc._has_exclusive_columns("bgc_clim"))

    def test_shared_column_product_needs_products_field(self):
        # 行里有风速，但 products 只写了 gfs：不能因此认定 era5 也跑过
        row = {"products": "gfs", "wind_speed": 7.5}
        self.assertTrue(ocean_svc._row_has_product(row, "wind_gfs"))
        self.assertFalse(ocean_svc._row_has_product(row, "wind_era5"))
        self.assertFalse(ocean_svc._row_has_product(row, "wind"))

    def test_legacy_row_without_products_field(self):
        # 老行没有 products 字段，独占列可以反推，共享列一律按未命中
        row = {"products": None, "sst_c": 22.1, "wind_speed": 7.5}
        self.assertTrue(ocean_svc._row_has_product(row, "physics"))
        self.assertFalse(ocean_svc._row_has_product(row, "wind_gfs"))


class TestWindProductConfig(unittest.TestCase):
    def test_default_includes_near_realtime_wind(self):
        # 默认必须含 GFS：另外两个风源滞后 2–5 天，覆盖不到「今天」
        self.assertIn("wind_gfs", ocean_svc.DEFAULT_PRODUCTS)
        self.assertNotIn("wind_era5", ocean_svc.DEFAULT_PRODUCTS)

    def test_request_variables_differ_from_columns(self):
        # 请求变量（eastward_wind / u10）绝不能和写入列（wind_speed）混用
        for key in ("wind", "wind_gfs", "wind_era5"):
            prod = ocean_svc.PRODUCTS[key]
            req = prod["request_variables"]
            cols = set(prod["variables"].values())
            self.assertTrue(req, f"{key} 缺 request_variables")
            self.assertFalse(set(req) & cols, f"{key} 的请求变量与写入列重名")
            self.assertIn("wind_uv", prod)

    def test_era5_uses_valid_time_dim(self):
        # ERA5 的 NetCDF 时间维是 valid_time，写成 time 会取不到任何时刻
        self.assertEqual(ocean_svc.PRODUCTS["wind_era5"]["time_dim"], "valid_time")
        self.assertEqual(ocean_svc.PRODUCTS["wind_gfs"]["time_dim"], "time")
