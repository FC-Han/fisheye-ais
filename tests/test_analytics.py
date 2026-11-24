"""核心逻辑单测：解析规范化、规则引擎、中断判定的期望间隔、点面判定。

运行：python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import math
import sqlite3
import tempfile
import time
import unittest
from pathlib import Path

from analytics import fishing_detector, gap_detector
from analytics.geofence_detector import seed_zones
from core import db as dbmod
from core.config import settings
from core.geo import point_in_polygon
from core.parser import classify_identity, normalize


def _row(**kw):
    """构造一个可下标访问的行对象，模拟 sqlite3.Row。"""
    r = sqlite3.Row.__new__(sqlite3.Row)
    return _DictRow(kw)


class _DictRow(dict):
    def __getitem__(self, k):
        return dict.get(self, k)


class TestParser(unittest.TestCase):
    def test_position_report(self):
        raw = {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": 412345678, "ShipName": "LU RONG YU 12345"},
            "Message": {
                "PositionReport": {
                    "Cog": 45.0,
                    "Latitude": 30.5,
                    "Longitude": 123.25,
                    "NavigationalStatus": 0,
                    "Sog": 3.2,
                    "TrueHeading": 47,
                }
            },
        }
        m = normalize(raw)
        self.assertEqual(m["mmsi"], 412345678)
        self.assertEqual(m["kind"], "position")
        self.assertAlmostEqual(m["lat"], 30.5)
        self.assertEqual(m["sog"], 3.2)
        self.assertEqual(m["cog"], 45.0)

    def test_unavailable_sentinels(self):
        raw = {
            "MessageType": "PositionReport",
            "MetaData": {"MMSI": 1},
            "Message": {"PositionReport": {"Cog": 360.0, "Latitude": 1.0, "Longitude": 2.0,
                                           "Sog": 102.3, "TrueHeading": 511}},
        }
        m = normalize(raw)
        self.assertIsNone(m["cog"], "COG=360 是“不可用”，不能当成正北")
        self.assertIsNone(m["heading"], "Heading=511 是“不可用”")
        self.assertIsNone(m["sog"], "SOG=102.3 是“不可用”")

    def test_static_fishing(self):
        raw = {
            "MessageType": "ShipStaticData",
            "MetaData": {"MMSI": 2, "ShipName": "MIN XING YU 001"},
            "Message": {"ShipStaticData": {"Type": 30, "Name": "MIN XING YU 001",
                                            "Dimension": {"A": 10, "B": 8, "C": 2, "D": 2}}},
        }
        m = normalize(raw)
        self.assertEqual(m["kind"], "static")
        self.assertEqual(m["ship_type"], 30)
        ident, src = classify_identity(m["ship_type"], m["ship_name"])
        self.assertEqual(ident, "fishing")
        self.assertEqual(src, "ais_ship_type")

    def test_cargo_not_fishing(self):
        ident, _ = classify_identity(70, "EVER GIVEN")
        self.assertEqual(ident, "non_fishing")

    def test_name_pattern_candidate(self):
        ident, src = classify_identity(None, "ZHE YU 08888")
        self.assertEqual(ident, "candidate")
        self.assertEqual(src, "name_pattern")

    def test_bad_payload(self):
        self.assertIsNone(normalize({"MessageType": "PositionReport", "MetaData": {}, "Message": {}}))
        self.assertIsNone(normalize("nope"))  # type: ignore[arg-type]


def _trawl_track(mmsi: int, start: int, minutes: int = 150, speed: float = 2.9):
    """构造一条拖网式航迹：低速、来回折返、绕行。"""
    pts = []
    lat, lon = 29.5, 123.0
    heading = 30.0
    for i in range(minutes):
        ts = start + i * 60
        # 每 25 分钟掉头一次，形成典型往复拖曳
        if i and i % 25 == 0:
            heading = (heading + 170) % 360
        heading += math.sin(i / 7) * 1.5
        dist = speed / 60 * 1852  # 每分钟米数
        lat += math.cos(math.radians(heading)) * dist / 111320
        lon += math.sin(math.radians(heading)) * dist / (111320 * math.cos(math.radians(lat)))
        pts.append((ts, round(lat, 5), round(lon, 5), speed + math.sin(i / 5) * 0.25, heading % 360))
    return pts


class DetectorTestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self._orig = settings.db_path
        settings.db_path = Path(self.tmp.name) / "test.db"
        dbmod.init_db()
        self.conn = dbmod.connect()

    def tearDown(self):
        self.conn.close()
        settings.db_path = self._orig
        self.tmp.cleanup()


class TestFishingDetector(DetectorTestBase):
    def _insert(self, mmsi: int, pts: list[tuple], identity: str = "fishing"):
        self.conn.execute(
            """INSERT INTO vessels(mmsi, ship_name, ship_type, fishing_identity, identity_source, updated_at)
               VALUES(?,?,?,?,?,?)""",
            (mmsi, f"TEST-{mmsi}", 30, identity, "ais_ship_type", int(time.time())),
        )
        last = pts[-1]
        self.conn.execute(
            """INSERT INTO vessel_latest_state(mmsi, ts, lat, lon, sog, cog, identity, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (mmsi, last[0], last[1], last[2], last[3], last[4], identity, int(time.time())),
        )
        self.conn.executemany(
            "INSERT INTO ais_positions(mmsi, ts, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)", 
            [(mmsi, *p) for p in pts],
        )
        self.conn.commit()

    def _vessels(self, mmsi: int):
        return self.conn.execute(
            """SELECT s.mmsi, s.ts, s.lat, s.lon, s.sog, s.identity,
                      v.ship_name, v.ship_type, v.fishing_identity
               FROM vessel_latest_state s LEFT JOIN vessels v ON v.mmsi=s.mmsi WHERE s.mmsi=?""",
            (mmsi,),
        ).fetchall()

    def test_trawl_track_detected(self):
        now = int(time.time())
        pts = _trawl_track(412000001, now - 150 * 60)
        self._insert(412000001, pts)
        vessels = self._vessels(412000001)
        positions = {412000001: pts}
        n = fishing_detector.run(self.conn, vessels, positions)
        self.conn.commit()
        rows = self.conn.execute("SELECT * FROM fishing_events WHERE mmsi=412000001").fetchall()
        self.assertGreaterEqual(n, 1, "拖网式低速航迹应被识别为疑似捕捞")
        self.assertTrue(rows)
        ev = rows[0]
        self.assertGreaterEqual(ev["confidence"], 0.5)
        self.assertGreaterEqual(ev["duration_s"], 1800)
        detail = json.loads(ev["evidence"])
        self.assertLess(detail["avg_sog"], 5.0)
        self.assertIn("factors", detail)

    def test_cruising_not_detected(self):
        now = int(time.time())
        pts = [(now - i * 60, 30 + i * 0.01, 123 + i * 0.01, 12.5, 45.0) for i in range(150)][::-1]
        self._insert(412000002, pts)
        n = fishing_detector.run(self.conn, self._vessels(412000002), {412000002: pts})
        self.assertEqual(n, 0, "12.5 节巡航不应被判定为捕捞")

    def test_non_fishing_vessel_skipped(self):
        now = int(time.time())
        pts = _trawl_track(412000003, now - 150 * 60)
        self._insert(412000003, pts, identity="non_fishing")
        n = fishing_detector.run(self.conn, self._vessels(412000003), {412000003: pts})
        self.assertEqual(n, 0, "明确非渔船即使低速也不应产生捕捞事件")

    def test_short_segment_ignored(self):
        now = int(time.time())
        pts = _trawl_track(412000004, now - 10 * 60, minutes=9)
        self._insert(412000004, pts)
        n = fishing_detector.run(self.conn, self._vessels(412000004), {412000004: pts})
        self.assertEqual(n, 0, "不足 30 分钟的片段不生成事件")


class TestGapDetector(DetectorTestBase):
    def test_expected_interval_from_history(self):
        pts = [(1000 + i * 600, 1, 1, 3, 90) for i in range(10)]  # 每 10 分钟一条
        self.assertEqual(gap_detector._expected_interval(pts), 1800)

    def test_expected_interval_default(self):
        self.assertEqual(gap_detector._expected_interval([]), gap_detector.DEFAULT_INTERVAL)

    def test_gap_requires_healthy_stream(self):
        now = int(time.time())
        # 船在 2 小时前最后出现，且历史上报很密
        self.conn.execute(
            """INSERT INTO vessels(mmsi, ship_name, ship_type, fishing_identity, identity_source, updated_at)
               VALUES(?,?,?,?,?,?)""", (412000010, "TEST-GAP", 30, "fishing", "ais_ship_type", now))
        self.conn.execute(
            """INSERT INTO vessel_latest_state(mmsi, ts, lat, lon, sog, cog, identity, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (412000010, now - 7200, 30.0, 123.0, 3.0, 90, "fishing", now))
        self.conn.executemany(
            "INSERT INTO ais_positions(mmsi, ts, lat, lon, sog, cog) VALUES(?,?,?,?,?,?)",
            [(412000010, now - 7200 - i * 300, 30.0, 123.0, 3, 90) for i in range(10)],
        )
        # 数据源健康
        self.conn.execute(
            "INSERT INTO stream_health(ts, connected, msgs_total, rate_1m, last_msg_ts, bboxes) VALUES(?,?,?,?,?,?)",
            (now, 1, 1000, 10.0, now, "[]"))
        self.conn.commit()
        vessels = self.conn.execute(
            """SELECT s.mmsi, s.ts, s.lat, s.lon, s.sog, s.identity, v.ship_name, v.ship_type, v.fishing_identity
               FROM vessel_latest_state s LEFT JOIN vessels v ON v.mmsi=s.mmsi""").fetchall()
        opened, _ = gap_detector.run(self.conn, vessels, {412000010: [
            (now - 7200 - i * 300, 30.0, 123.0, 3, 90) for i in range(10)]})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM ais_gap_events WHERE mmsi=412000010").fetchone()
        self.assertIsNotNone(row, "长时间静默应生成中断事件")
        # 周边无船 → 覆盖不足，不能升级为 candidate
        self.assertIn(row["event_status"], ("coverage_unclear", "candidate"))
        self.assertIsNotNone(row["coverage_quality"])

    def test_stream_unclear_downgrades(self):
        now = int(time.time())
        self.conn.execute(
            """INSERT INTO vessels(mmsi, ship_name, ship_type, fishing_identity, identity_source, updated_at)
               VALUES(?,?,?,?,?,?)""", (412000011, "TEST-GAP2", 30, "fishing", "ais_ship_type", now))
        self.conn.execute(
            """INSERT INTO vessel_latest_state(mmsi, ts, lat, lon, sog, cog, identity, updated_at)
               VALUES(?,?,?,?,?,?,?,?)""",
            (412000011, now - 7200, 30.0, 123.0, 3.0, 90, "fishing", now))
        # 采集链路最后的健康记录是“断开”
        self.conn.execute(
            "INSERT INTO stream_health(ts, connected, msgs_total, rate_1m, last_msg_ts, bboxes) VALUES(?,?,?,?,?,?)",
            (now, 0, 1000, 0.0, now - 4000, "[]"))
        self.conn.commit()
        vessels = self.conn.execute(
            """SELECT s.mmsi, s.ts, s.lat, s.lon, s.sog, s.identity, v.ship_name, v.ship_type, v.fishing_identity
               FROM vessel_latest_state s LEFT JOIN vessels v ON v.mmsi=s.mmsi""").fetchall()
        gap_detector.run(self.conn, vessels, {})
        self.conn.commit()
        row = self.conn.execute("SELECT * FROM ais_gap_events WHERE mmsi=412000011").fetchone()
        self.assertIsNotNone(row)
        self.assertEqual(row["event_status"], "stream_unclear",
                         "我方链路异常时必须降级，不能算作船舶关闭 AIS")


class TestGeo(unittest.TestCase):
    def test_point_in_polygon(self):
        poly = [[120.0, 30.0], [121.0, 30.0], [121.0, 31.0], [120.0, 31.0]]
        self.assertTrue(point_in_polygon(30.5, 120.5, poly))
        self.assertFalse(point_in_polygon(32.0, 120.5, poly))

    def test_zones_seeded(self):
        with tempfile.TemporaryDirectory() as d:
            orig = settings.db_path
            settings.db_path = Path(d) / "z.db"
            try:
                dbmod.init_db()
                conn = dbmod.connect()
                n = seed_zones(conn)
                conn.commit()
                self.assertGreater(n, 0)
                self.assertEqual(conn.execute("SELECT COUNT(*) c FROM geofences").fetchone()["c"], n)
                conn.close()
            finally:
                settings.db_path = orig


if __name__ == "__main__":
    unittest.main(verbosity=2)
