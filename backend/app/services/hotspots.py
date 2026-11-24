"""渔船热点分析。

思路：不把「热点」定义成漂亮的红色团块，而是定义为**可解释的作业聚集度**。
三路证据加权合成，每一格都能说清自己为什么热：

1. fishing_events      —— 疑似捕捞片段的时长（最直接，依赖规则引擎产出）
2. ais_positions       —— 渔船低速点密度（历史活动强度，覆盖更广）
3. vessel_latest_state —— 当前仍在低速活动的渔船数（「此刻」态势 + 船名清单）

另附该格 AIS 中断候选数，作为「既有作业聚集、又有信号消失」的复核线索。
"""
from __future__ import annotations

import math
import time
from contextlib import closing

from core import seas
from core.db import connect


def _bbox_clause(bbox: list[float] | None, lat_col: str, lon_col: str) -> tuple[str, list]:
    if not bbox:
        return "", []
    return (
        f" AND {lat_col} BETWEEN ? AND ? AND {lon_col} BETWEEN ? AND ?",
        [bbox[0], bbox[2], bbox[1], bbox[3]],
    )


def fishing_hotspots(
    hours: int = 24,
    bbox: list[float] | None = None,
    cell: float = 1.0,
    limit: int = 40,
) -> dict:
    now = int(time.time())
    since = now - hours * 3600
    cell = max(0.25, min(cell, 10.0))
    limit = max(1, min(limit, 200))

    cells: dict[tuple[int, int], dict] = {}

    def bucket(lat: float, lon: float) -> dict:
        k = (math.floor(lat / cell), math.floor(lon / cell))
        c = cells.get(k)
        if c is None:
            c = {
                "gy": k[0], "gx": k[1],
                "n_events": 0, "event_sec": 0.0, "conf_sum": 0.0,
                "event_vessels": set(),
                "activity_points": 0,
                "low_speed_vessels": 0,
                "gap_candidates": 0,
                "top": {},
            }
            cells[k] = c
        return c

    with closing(connect()) as conn:
        # 1) 疑似捕捞事件
        sql = (
            "SELECT f.lat, f.lon, f.mmsi, f.duration_s, f.confidence "
            "FROM fishing_events f WHERE f.end_ts >= ? AND f.lat IS NOT NULL"
        )
        clause, args = _bbox_clause(bbox, "f.lat", "f.lon")
        for r in conn.execute(sql + clause, [since, *args]).fetchall():
            c = bucket(r["lat"], r["lon"])
            c["n_events"] += 1
            c["event_sec"] += float(r["duration_s"] or 0)
            c["conf_sum"] += float(r["confidence"] or 0)
            c["event_vessels"].add(r["mmsi"])

        # 2) 渔船低速点密度（历史活动强度）
        clause, args = _bbox_clause(bbox, "p.lat", "p.lon")
        psql = (
            "SELECT CAST(p.lat / ? AS INTEGER) AS gy, CAST(p.lon / ? AS INTEGER) AS gx, "
            "COUNT(*) AS n, COUNT(DISTINCT p.mmsi) AS nv "
            "FROM ais_positions p JOIN vessels v ON v.mmsi = p.mmsi "
            "WHERE p.ts >= ? AND p.sog IS NOT NULL AND p.sog BETWEEN 0.3 AND 6 "
            "AND COALESCE(v.fishing_identity, 'unknown') IN ('fishing', 'candidate')"
            + clause
            + " GROUP BY gy, gx"
        )
        for r in conn.execute(psql, [cell, cell, since, *args]).fetchall():
            c = cells.setdefault(
                (r["gy"], r["gx"]),
                {
                    "gy": r["gy"], "gx": r["gx"],
                    "n_events": 0, "event_sec": 0.0, "conf_sum": 0.0,
                    "event_vessels": set(), "activity_points": 0,
                    "low_speed_vessels": 0, "gap_candidates": 0, "top": {},
                },
            )
            c["activity_points"] += r["n"]

        # 3) 当前低速活动的渔船（此刻态势 + 船名）
        clause, args = _bbox_clause(bbox, "s.lat", "s.lon")
        lsql = (
            "SELECT s.mmsi, s.lat, s.lon, s.sog, v.ship_name, "
            "COALESCE(v.fishing_identity, s.identity, 'unknown') AS identity "
            "FROM vessel_latest_state s JOIN vessels v ON v.mmsi = s.mmsi "
            "WHERE s.ts >= ? AND s.sog IS NOT NULL AND s.sog BETWEEN 0.2 AND 6 "
            "AND COALESCE(v.fishing_identity, s.identity, 'unknown') IN ('fishing', 'candidate')"
            + clause
            + " ORDER BY s.sog ASC LIMIT 800"
        )
        for r in conn.execute(lsql, [now - 3600, *args]).fetchall():
            c = bucket(r["lat"], r["lon"])
            c["low_speed_vessels"] += 1
            c["top"][r["mmsi"]] = {
                "mmsi": r["mmsi"],
                "name": r["ship_name"],
                "sog": round(r["sog"], 1) if r["sog"] is not None else None,
                "identity": r["identity"],
            }

        # 4) AIS 中断候选（线索叠加）
        clause, args = _bbox_clause(bbox, "lat", "lon")
        for r in conn.execute(
            "SELECT lat, lon FROM ais_gap_events WHERE gap_start >= ? "
            "AND lat IS NOT NULL AND event_status = 'candidate'" + clause,
            [since, *args],
        ).fetchall():
            bucket(r["lat"], r["lon"])["gap_candidates"] += 1

        # 5) 事件船名补齐
        names: dict[int, str | None] = {}
        mm = {m for c in cells.values() for m in c["event_vessels"]}
        if mm:
            q = ",".join("?" * len(mm))
            for r in conn.execute(
                f"SELECT mmsi, ship_name FROM vessels WHERE mmsi IN ({q})", list(mm)
            ).fetchall():
                names[r["mmsi"]] = r["ship_name"]

    # ---- 打分 ----
    raw_max = 0.0
    for c in cells.values():
        vessel_hours = c["event_sec"] / 3600.0
        avg_conf = (c["conf_sum"] / c["n_events"]) if c["n_events"] else 0.0
        point_hours = c["activity_points"] / 120.0  # 降采样 30s → 船·小时量级
        raw = (
            vessel_hours * (0.6 + 0.4 * avg_conf)
            + 0.5 * point_hours
            + 0.35 * c["low_speed_vessels"]
            + 0.15 * c["gap_candidates"]
        )
        c["raw"] = raw
        raw_max = max(raw_max, raw)

    out = []
    for c in cells.values():
        if c["raw"] <= 0:
            continue
        lat = c["gy"] * cell + cell / 2
        lon = c["gx"] * cell + cell / 2
        basis = "mixed"
        if c["n_events"] and not c["activity_points"] and not c["low_speed_vessels"]:
            basis = "event"
        elif not c["n_events"]:
            basis = "low_speed"
        top = sorted(c["top"].values(), key=lambda v: v["sog"] if v["sog"] is not None else 99)[:3]
        if not top:
            top = [
                {"mmsi": m, "name": names.get(m), "sog": None, "identity": "fishing"}
                for m in list(c["event_vessels"])[:3]
            ]
        out.append({
            "lat": round(lat, 3),
            "lon": round(lon, 3),
            "cell": cell,
            "score": round(100.0 * c["raw"] / raw_max, 1) if raw_max else 0.0,
            "area": seas.describe(lat, lon),
            "n_events": c["n_events"],
            "n_vessels": len(c["event_vessels"]) + len(c["top"]),
            "vessel_hours": round(c["event_sec"] / 3600.0, 1),
            "activity_points": c["activity_points"],
            "low_speed_vessels": c["low_speed_vessels"],
            "gap_candidates": c["gap_candidates"],
            "avg_confidence": round(c["conf_sum"] / c["n_events"], 2) if c["n_events"] else None,
            "basis": basis,
            "top_vessels": top,
        })

    out.sort(key=lambda x: x["score"], reverse=True)
    out = out[:limit]

    return {
        "hours": hours,
        "cell_deg": cell,
        "generated_at": now,
        "hotspot_count": len(out),
        "totals": {
            "cells_with_activity": len(cells),
            "suspected_fishing_events": sum(c["n_events"] for c in cells.values()),
            "vessels_in_events": len({m for c in cells.values() for m in c["event_vessels"]}),
            "live_low_speed_fishing": sum(c["low_speed_vessels"] for c in cells.values()),
        },
        "hotspots": out,
        "bbox": bbox,
        "caveat": (
            "热点是「已接收 AIS 渔船的低速聚集程度」排序，不等于实际捕捞量。"
            "AIS 覆盖稀疏的海域即使渔船多也可能排名靠后；渔船身份仅由 AIS 类型位判定，可能误判。"
        ),
    }
