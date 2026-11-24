"""区域围栏事件：进入 / 离开。

只输出“观测事实”：某船在某时刻位于某多边形内。
不输出“违规”，因为是否合法取决于该区域的法规、船旗、许可，系统不掌握这些证据。
"""
from __future__ import annotations

import json
import sqlite3
import time
from pathlib import Path

from core.config import ROOT
from core.geo import point_in_polygon


def seed_zones(conn: sqlite3.Connection) -> int:
    p: Path = ROOT / "config" / "zones.json"
    if not p.exists():
        return 0
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return 0
    n = 0
    for z in data.get("zones", []):
        conn.execute(
            """INSERT INTO geofences(id, name, kind, polygon, props, source)
               VALUES(?,?,?,?,?,?)
               ON CONFLICT(id) DO UPDATE SET name=excluded.name, kind=excluded.kind,
                 polygon=excluded.polygon, props=excluded.props, source=excluded.source""",
            (z["id"], z["name"], z.get("kind"), json.dumps(z["polygon"]),
             json.dumps({k: v for k, v in z.items() if k not in ("id", "name", "kind", "polygon")},
                        ensure_ascii=False),
             z.get("source")),
        )
        n += 1
    return n


def run(conn: sqlite3.Connection) -> int:
    now = int(time.time())
    zones = conn.execute("SELECT id, name, polygon FROM geofences").fetchall()
    if not zones:
        return 0

    events = 0
    for z in zones:
        poly = json.loads(z["polygon"])
        lons = [pt[0] for pt in poly]
        lats = [pt[1] for pt in poly]
        minlat, maxlat = min(lats), max(lats)
        minlon, maxlon = min(lons), max(lons)
        # 只看围栏包围盒内、最近 30 分钟仍活动的船
        rows = conn.execute(
            """SELECT s.mmsi, s.ts, s.lat, s.lon, COALESCE(v.fishing_identity, s.identity) AS identity
               FROM vessel_latest_state s
               JOIN vessel_rtree r ON r.mmsi = s.mmsi
               LEFT JOIN vessels v ON v.mmsi = s.mmsi
               WHERE r.minlat >= ? AND r.maxlat <= ? AND r.minlon >= ? AND r.maxlon <= ?
                 AND s.ts >= ?""",
            (minlat, maxlat, minlon, maxlon, now - 1800),
        ).fetchall()

        for r in rows:
            inside = 1 if point_in_polygon(r["lat"], r["lon"], poly) else 0
            st = conn.execute(
                "SELECT inside FROM geofence_state WHERE zone_id=? AND mmsi=?", (z["id"], r["mmsi"])
            ).fetchone()
            prev = st["inside"] if st else 0
            if inside != prev:
                etype = "enter" if inside else "exit"
                conn.execute(
                    """INSERT OR IGNORE INTO geofence_events(zone_id, mmsi, event_type, ts, lat, lon, created_at)
                       VALUES(?,?,?,?,?,?,?)""",
                    (z["id"], r["mmsi"], etype, r["ts"], r["lat"], r["lon"], now),
                )
                events += 1
                conn.execute(
                    """INSERT INTO geofence_state(zone_id, mmsi, inside, last_ts) VALUES(?,?,?,?)
                       ON CONFLICT(zone_id, mmsi) DO UPDATE SET inside=excluded.inside, last_ts=excluded.last_ts""",
                    (z["id"], r["mmsi"], inside, r["ts"]),
                )
            else:
                conn.execute(
                    """INSERT INTO geofence_state(zone_id, mmsi, inside, last_ts) VALUES(?,?,?,?)
                       ON CONFLICT(zone_id, mmsi) DO UPDATE SET last_ts=excluded.last_ts""",
                    (z["id"], r["mmsi"], inside, r["ts"]),
                )
    return events
