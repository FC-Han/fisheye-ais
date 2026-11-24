"""分析模块共用的数据装载工具。"""
from __future__ import annotations

import sqlite3
import time
from collections import defaultdict
from typing import Iterable

from core.config import settings


def bbox_sql(bboxes: Iterable) -> tuple[str, list]:
    """把 bbox 列表编译成 SQL 条件（用 R*Tree 做粗筛）。"""
    clauses, params = [], []
    for bb in bboxes:
        (minlat, minlon), (maxlat, maxlon) = bb
        clauses.append("(minlat <= ? AND maxlat >= ? AND minlon <= ? AND maxlon >= ?)")
        params += [maxlat, minlat, maxlon, minlon]
    if not clauses:
        return "1=1", []
    return "(" + " OR ".join(clauses) + ")", params


def vessels_in_focus(conn: sqlite3.Connection, since_ts: int, limit: int | None = None) -> list[sqlite3.Row]:
    """取出重点海域内、最近仍有报告的渔船/疑似渔船。"""
    cond, params = bbox_sql(settings.analytics_bboxes)
    sql = f"""
        SELECT s.mmsi, s.ts, s.lat, s.lon, s.sog, s.identity,
               COALESCE(v.ship_name, s.ship_name) AS ship_name,
               v.ship_type, v.fishing_identity
        FROM vessel_latest_state s
        JOIN vessel_rtree r ON r.mmsi = s.mmsi
        LEFT JOIN vessels v ON v.mmsi = s.mmsi
        WHERE {cond} AND s.ts >= ?
          AND COALESCE(v.fishing_identity, s.identity, 'unknown') IN ('fishing','candidate','unknown')
        ORDER BY CASE COALESCE(v.fishing_identity, s.identity, 'unknown')
                   WHEN 'fishing' THEN 0 WHEN 'candidate' THEN 1 ELSE 2 END,
                 s.ts DESC
        LIMIT ?
    """
    params = params + [since_ts, limit or settings.analytics_max_vessels]
    return conn.execute(sql, params).fetchall()


def load_positions(conn: sqlite3.Connection, mmsis: list[int], since_ts: int) -> dict[int, list[tuple]]:
    """按 MMSI 分组装载轨迹点，分块查询避免 SQL 变量过多。"""
    out: dict[int, list[tuple]] = defaultdict(list)
    CHUNK = 400
    for i in range(0, len(mmsis), CHUNK):
        chunk = mmsis[i : i + CHUNK]
        marks = ",".join("?" * len(chunk))
        rows = conn.execute(
            f"""SELECT mmsi, ts, lat, lon, sog, cog
                FROM ais_positions
                WHERE mmsi IN ({marks}) AND ts >= ?
                ORDER BY mmsi, ts""",
            [*chunk, since_ts],
        ).fetchall()
        for r in rows:
            out[r["mmsi"]].append((r["ts"], r["lat"], r["lon"], r["sog"], r["cog"]))
    return out


def latest_stream_ok(conn: sqlite3.Connection, max_age: int = 300) -> tuple[bool, dict]:
    row = conn.execute("SELECT * FROM stream_health ORDER BY ts DESC LIMIT 1").fetchone()
    now = int(time.time())
    if not row:
        return False, {"reason": "无健康记录"}
    ok = bool(row["connected"]) and (row["last_msg_ts"] or 0) >= now - max_age
    return ok, dict(row)


def coverage_quality(conn: sqlite3.Connection, lat: float, lon: float, radius_deg: float = 1.5,
                     window: int = 900) -> tuple[str, int]:
    """用周边船舶的接收数量估计该海域的 AIS 覆盖强度。

    这是覆盖代理指标，不是真实岸站覆盖模型；仅用于避免把“没覆盖”误判成“关 AIS”。
    """
    now = int(time.time())
    row = conn.execute(
        """SELECT COUNT(*) AS n FROM vessel_rtree r
           JOIN vessel_latest_state s ON s.mmsi = r.mmsi
           WHERE r.minlat >= ? AND r.maxlat <= ? AND r.minlon >= ? AND r.maxlon <= ? AND s.ts >= ?""",
        (lat - radius_deg, lat + radius_deg, lon - radius_deg, lon + radius_deg, now - window),
    ).fetchone()
    n = row["n"] if row else 0
    if n >= 20:
        return "good", n
    if n >= 5:
        return "fair", n
    return "poor", n
