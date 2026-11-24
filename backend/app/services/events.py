"""统一事件查询：把捕捞 / 中断 / 围栏三类事件归一化成前端可直接渲染的列表。"""
from __future__ import annotations

import json
import time
from contextlib import closing

from core.db import connect

KIND_TITLES = {
    "fishing": "疑似捕捞作业",
    "gap": "AIS 信号中断",
    "geofence": "区域进入/离开",
}


def query_events(
    kinds: list[str] | None = None,
    hours: int = 24,
    limit: int = 200,
    mmsi: int | None = None,
    zone_id: str | None = None,
) -> list[dict]:
    kinds = kinds or ["fishing", "gap", "geofence"]
    since = int(time.time()) - hours * 3600
    out: list[dict] = []

    with closing(connect()) as conn:
        if "fishing" in kinds:
            sql = """SELECT f.*, v.ship_name, COALESCE(v.fishing_identity, 'unknown') AS identity
                     FROM fishing_events f LEFT JOIN vessels v ON v.mmsi=f.mmsi
                     WHERE f.end_ts >= ?"""
            args: list = [since]
            if mmsi:
                sql += " AND f.mmsi=?"
                args.append(mmsi)
            sql += " ORDER BY f.end_ts DESC LIMIT ?"
            args.append(limit)
            for r in conn.execute(sql, args).fetchall():
                ev = json.loads(r["evidence"]) if r["evidence"] else {}
                out.append({
                    "kind": "fishing",
                    "ts": r["end_ts"],
                    "start_ts": r["start_ts"],
                    "end_ts": r["end_ts"],
                    "mmsi": r["mmsi"],
                    "ship_name": r["ship_name"],
                    "identity": r["identity"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "title": KIND_TITLES["fishing"],
                    "confidence": r["confidence"],
                    "status": "suspected",
                    "detail": f"持续 {round((r['duration_s'] or 0)/60)} 分钟，均速 {r['avg_sog']} kn",
                    "evidence": ev,
                })

        if "gap" in kinds:
            sql = """SELECT g.*, v.ship_name FROM ais_gap_events g
                     LEFT JOIN vessels v ON v.mmsi=g.mmsi
                     WHERE (g.gap_end IS NULL OR g.gap_end >= ?)"""
            args = [since]
            if mmsi:
                sql += " AND g.mmsi=?"
                args.append(mmsi)
            sql += " ORDER BY g.gap_start DESC LIMIT ?"
            args.append(limit)
            for r in conn.execute(sql, args).fetchall():
                dur = r["duration_s"] or 0
                status = r["event_status"]
                out.append({
                    "kind": "gap",
                    "ts": r["gap_end"] or r["gap_start"],
                    "start_ts": r["gap_start"],
                    "end_ts": r["gap_end"],
                    "mmsi": r["mmsi"],
                    "ship_name": r["ship_name"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "title": KIND_TITLES["gap"],
                    "status": status,
                    "confidence": 0.7 if status == "candidate" else 0.3,
                    "detail": (
                        f"静默 {round(dur/60)} 分钟（期望 {round((r['expected_sec'] or 0)/60)} 分钟），"
                        f"覆盖 {r['coverage_quality']}"
                    ),
                    "evidence": json.loads(r["evidence"]) if r["evidence"] else {},
                })

        if "geofence" in kinds:
            sql = """SELECT e.*, z.name AS zone_name, z.kind AS zone_kind, v.ship_name
                     FROM geofence_events e
                     JOIN geofences z ON z.id=e.zone_id
                     LEFT JOIN vessels v ON v.mmsi=e.mmsi
                     WHERE e.ts >= ?"""
            args = [since]
            if mmsi:
                sql += " AND e.mmsi=?"
                args.append(mmsi)
            if zone_id:
                sql += " AND e.zone_id=?"
                args.append(zone_id)
            sql += " ORDER BY e.ts DESC LIMIT ?"
            args.append(limit)
            for r in conn.execute(sql, args).fetchall():
                out.append({
                    "kind": "geofence",
                    "ts": r["ts"],
                    "mmsi": r["mmsi"],
                    "ship_name": r["ship_name"],
                    "lat": r["lat"],
                    "lon": r["lon"],
                    "title": KIND_TITLES["geofence"],
                    "zone_id": r["zone_id"],
                    "zone_name": r["zone_name"],
                    "status": r["event_type"],
                    "confidence": 1.0,
                    "detail": f"{'进入' if r['event_type']=='enter' else '离开'} {r['zone_name']}",
                    "evidence": {"event_type": r["event_type"], "zone_kind": r["zone_kind"]},
                })

    out.sort(key=lambda e: e["ts"] or 0, reverse=True)
    return out[:limit]


def zones() -> list[dict]:
    with closing(connect()) as conn:
        rows = conn.execute("SELECT * FROM geofences ORDER BY id").fetchall()
    return [
        {
            "id": r["id"],
            "name": r["name"],
            "kind": r["kind"],
            "source": r["source"],
            "polygon": json.loads(r["polygon"]),
        }
        for r in rows
    ]
