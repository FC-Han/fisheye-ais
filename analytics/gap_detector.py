"""AIS 信号中断候选检测。

核心立场：**信号中断 ≠ 关闭 AIS**。
必须先排除三种“假中断”：
  1. 我们自己的采集链路断了（stream_health.connected / last_msg_ts）
  2. 该海域本来就没有 AIS 覆盖（周边船舶接收数作为代理指标）
  3. 该船本来就发得很稀疏（用自身历史上报间隔的中位数做动态期望）
只有三样都排除了，才升级为 candidate。
"""
from __future__ import annotations

import json
import sqlite3
import time
from statistics import median

from core.config import settings

from .common import coverage_quality, latest_stream_ok

DEFAULT_INTERVAL = 600   # 无历史时按 10 分钟估计
RECENT_WINDOW = 24 * 3600


def _expected_interval(pts: list[tuple]) -> int:
    ts = [p[0] for p in pts]
    if len(ts) < 4:
        return DEFAULT_INTERVAL
    diffs = [b - a for a, b in zip(ts, ts[1:]) if 0 < b - a < 6 * 3600]
    if len(diffs) < 3:
        return DEFAULT_INTERVAL
    base = median(diffs)
    return int(max(120, base * 3))


def run(conn: sqlite3.Connection, vessels: list[sqlite3.Row], positions: dict[int, list[tuple]]) -> tuple[int, int]:
    now = int(time.time())
    stream_ok, _stream_row = latest_stream_ok(conn)
    opened = closed = 0

    # 1) 关闭已恢复的中断
    for ev in conn.execute(
        "SELECT id, mmsi, gap_start FROM ais_gap_events WHERE gap_end IS NULL"
    ).fetchall():
        mmsi = ev["mmsi"]
        row = conn.execute("SELECT ts FROM vessel_latest_state WHERE mmsi=?", (mmsi,)).fetchone()
        if row and row["ts"] > ev["gap_start"]:
            conn.execute(
                "UPDATE ais_gap_events SET gap_end=?, duration_s=? WHERE id=?",
                (row["ts"], row["ts"] - ev["gap_start"], ev["id"]),
            )
            closed += 1

    # 2) 检测新中断
    for v in vessels:
        mmsi, last_ts = v["mmsi"], v["ts"]
        identity = v["fishing_identity"] or v["identity"] or "unknown"
        if identity not in ("fishing", "candidate"):
            continue
        expected = _expected_interval(positions.get(mmsi) or [])
        expected = max(settings.gap_floor_sec, min(expected, settings.gap_ceiling_sec))
        silent = now - last_ts
        if silent <= expected:
            continue
        exists = conn.execute(
            "SELECT 1 FROM ais_gap_events WHERE mmsi=? AND gap_end IS NULL", (mmsi,)
        ).fetchone()
        if exists:
            # 仍在中断中，刷新时长
            conn.execute(
                "UPDATE ais_gap_events SET duration_s=? WHERE mmsi=? AND gap_end IS NULL",
                (silent, mmsi),
            )
            continue

        cov, n_near = coverage_quality(conn, v["lat"], v["lon"])
        if not stream_ok:
            status = "stream_unclear"
        elif cov == "poor":
            status = "coverage_unclear"
        else:
            status = "candidate"

        evidence = {
            "expected_sec": expected,
            "silent_sec": silent,
            "last_sog": v["sog"],
            "coverage_nearby_vessels": n_near,
            "coverage_quality": cov,
            "stream_healthy": stream_ok,
            "note": "低覆盖/链路异常时不构成船舶异常证据",
        }
        conn.execute(
            """INSERT OR IGNORE INTO ais_gap_events
               (mmsi, gap_start, gap_end, duration_s, lat, lon, expected_sec,
                coverage_quality, stream_ok, event_status, evidence, created_at)
               VALUES(?,?,NULL,?,?,?,?,?,?,?,?,?)""",
            (mmsi, last_ts, silent, v["lat"], v["lon"], expected, cov,
             1 if stream_ok else 0, status, json.dumps(evidence, ensure_ascii=False), now),
        )
        opened += 1

    return opened, closed
