"""船舶状态查询（只读）。"""
from __future__ import annotations

import sqlite3
import time
from contextlib import closing

from core.db import connect

IDENTITY_FILTER = {
    "fishing": "v.fishing_identity = 'fishing'",
    "candidate": "v.fishing_identity = 'candidate'",
    "all_fishing": "v.fishing_identity IN ('fishing','candidate')",
    "non_fishing": "v.fishing_identity = 'non_fishing'",
}

BASE_COLS = """
    s.mmsi, s.ts, s.lat, s.lon, s.sog, s.cog, s.heading, s.nav_status,
    COALESCE(v.fishing_identity, s.identity, 'unknown') AS identity,
    v.ship_name, v.ship_type, v.ship_type_text, v.call_sign, v.length, v.destination, v.imo
"""


def _row(r: sqlite3.Row) -> dict:
    d = dict(r)
    d["lat"] = round(d["lat"], 5)
    d["lon"] = round(d["lon"], 5)
    if d.get("sog") is not None:
        d["sog"] = round(d["sog"], 1)
    if d.get("cog") is not None:
        d["cog"] = round(d["cog"], 0)
    return d


def vessels_in_bbox(
    bbox: tuple[float, float, float, float],
    limit: int = 3000,
    identity: str = "all",
    since_sec: int = 3600,
    q: str | None = None,
    ts_after: int | None = None,
) -> list[dict]:
    minlat, minlon, maxlat, maxlon = bbox
    cond, params = "", []
    if identity in IDENTITY_FILTER:
        cond = " AND " + IDENTITY_FILTER[identity]
    if q:
        cond += " AND (v.ship_name LIKE ? OR CAST(s.mmsi AS TEXT) LIKE ?)"
        params += [f"%{q}%", f"{q}%"]
    lower = int(time.time()) - since_sec
    if ts_after:
        cond += " AND s.ts > ?"
        params.append(ts_after)
    sql = f"""
        SELECT {BASE_COLS}
        FROM vessel_latest_state s
        JOIN vessel_rtree r ON r.mmsi = s.mmsi
        LEFT JOIN vessels v ON v.mmsi = s.mmsi
        WHERE r.minlat >= ? AND r.maxlat <= ? AND r.minlon >= ? AND r.maxlon <= ?
          AND s.ts >= ?{cond}
        LIMIT ?
    """
    args = [minlat, maxlat, minlon, maxlon, lower, *params, limit]
    with closing(connect()) as conn:
        return [_row(r) for r in conn.execute(sql, args).fetchall()]


def density(
    bbox: tuple[float, float, float, float], cell: float = 5.0, since_sec: int = 3600
) -> list[dict]:
    minlat, minlon, maxlat, maxlon = bbox
    sql = """
        SELECT CAST(lat / ? AS INTEGER) AS gy, CAST(lon / ? AS INTEGER) AS gx,
               COUNT(*) AS n,
               SUM(CASE WHEN COALESCE(identity,'unknown') IN ('fishing','candidate') THEN 1 ELSE 0 END) AS n_fishing
        FROM vessel_latest_state
        WHERE ts >= ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
        GROUP BY gy, gx
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            sql, (cell, cell, int(time.time()) - since_sec, minlat, maxlat, minlon, maxlon)
        ).fetchall()
    out = []
    for r in rows:
        out.append({
            "lat": round((r["gy"] + 0.5) * cell, 3),
            "lon": round((r["gx"] + 0.5) * cell, 3),
            "count": r["n"],
            "fishing": r["n_fishing"] or 0,
        })
    return out


def vessel_detail(mmsi: int) -> dict | None:
    sql = f"""
        SELECT {BASE_COLS}, v.first_seen, v.updated_at, v.draught, v.identity_source
        FROM vessel_latest_state s
        LEFT JOIN vessels v ON v.mmsi = s.mmsi
        WHERE s.mmsi = ?
    """
    with closing(connect()) as conn:
        r = conn.execute(sql, (mmsi,)).fetchone()
        if not r:
            return None
        d = _row(r)
        stat = conn.execute(
            """SELECT MIN(ts) AS first_ts, MAX(ts) AS last_ts, COUNT(*) AS points,
                      AVG(sog) AS avg_sog, MAX(sog) AS max_sog
               FROM ais_positions WHERE mmsi=? AND ts >= ?""",
            (mmsi, int(time.time()) - 24 * 3600),
        ).fetchone()
        d["stats_24h"] = dict(stat) if stat else {}
        d["fishing_events_24h"] = conn.execute(
            "SELECT COUNT(*) c FROM fishing_events WHERE mmsi=? AND end_ts>=?",
            (mmsi, int(time.time()) - 24 * 3600),
        ).fetchone()["c"]
        d["gap_events_24h"] = conn.execute(
            "SELECT COUNT(*) c FROM ais_gap_events WHERE mmsi=? AND gap_start>=?",
            (mmsi, int(time.time()) - 24 * 3600),
        ).fetchone()["c"]
        return d


def vessel_track(mmsi: int, hours: int = 24, max_points: int = 5000) -> dict:
    since = int(time.time()) - hours * 3600
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT ts, lat, lon, sog, cog FROM ais_positions
               WHERE mmsi=? AND ts>=? ORDER BY ts LIMIT ?""",
            (mmsi, since, max_points),
        ).fetchall()
        fish = conn.execute(
            """SELECT start_ts, end_ts, duration_s, avg_sog, lat, lon, confidence, evidence
               FROM fishing_events WHERE mmsi=? AND end_ts>=? ORDER BY start_ts""",
            (mmsi, since),
        ).fetchall()
        gaps = conn.execute(
            """SELECT gap_start, gap_end, duration_s, event_status, coverage_quality, lat, lon
               FROM ais_gap_events WHERE mmsi=? AND (gap_end IS NULL OR gap_end>=?) ORDER BY gap_start""",
            (mmsi, since),
        ).fetchall()
    return {
        "mmsi": mmsi,
        "hours": hours,
        "track": [{"ts": r["ts"], "lat": round(r["lat"], 5), "lon": round(r["lon"], 5),
                   "sog": r["sog"], "cog": r["cog"]} for r in rows],
        "fishing_events": [dict(r) for r in fish],
        "gap_events": [dict(r) for r in gaps],
    }


def summary() -> dict:
    now = int(time.time())
    with closing(connect()) as conn:
        live = conn.execute(
            "SELECT COUNT(*) c FROM vessel_latest_state WHERE ts>=?", (now - 1800,)
        ).fetchone()["c"]
        active_24h = conn.execute(
            "SELECT COUNT(*) c FROM vessel_latest_state WHERE ts>=?", (now - 86400,)
        ).fetchone()["c"]
        fishing_live = conn.execute(
            """SELECT COUNT(*) c FROM vessel_latest_state
               WHERE ts>=? AND COALESCE(identity,'') IN ('fishing','candidate')""",
            (now - 1800,),
        ).fetchone()["c"]
        fishing_events = conn.execute(
            "SELECT COUNT(*) c FROM fishing_events WHERE end_ts>=?", (now - 86400,)
        ).fetchone()["c"]
        gap_open = conn.execute(
            "SELECT COUNT(*) c FROM ais_gap_events WHERE gap_end IS NULL AND event_status='candidate'"
        ).fetchone()["c"]
        gap_all = conn.execute(
            "SELECT COUNT(*) c FROM ais_gap_events WHERE gap_start>=?", (now - 86400,)
        ).fetchone()["c"]
        geo = conn.execute(
            "SELECT COUNT(*) c FROM geofence_events WHERE ts>=?", (now - 86400,)
        ).fetchone()["c"]
        positions = conn.execute("SELECT COUNT(*) c FROM ais_positions").fetchone()["c"]
        health = conn.execute("SELECT * FROM stream_health ORDER BY ts DESC LIMIT 1").fetchone()
    return {
        "vessels_live_30m": live,
        "vessels_24h": active_24h,
        "fishing_live": fishing_live,
        "fishing_events_24h": fishing_events,
        "gap_open_candidate": gap_open,
        "gap_events_24h": gap_all,
        "geofence_events_24h": geo,
        "positions_total": positions,
        "stream": dict(health) if health else None,
    }


def search(q: str, limit: int = 20) -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT v.mmsi, v.ship_name, v.ship_type_text, v.fishing_identity AS identity,
                      v.destination, s.ts AS last_ts, s.lat, s.lon
               FROM vessels v LEFT JOIN vessel_latest_state s ON s.mmsi=v.mmsi
               WHERE v.ship_name LIKE ? OR CAST(v.mmsi AS TEXT) LIKE ?
               LIMIT ?""",
            (f"%{q}%", f"{q}%", limit),
        ).fetchall()
    return [dict(r) for r in rows]
