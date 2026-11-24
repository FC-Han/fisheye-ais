"""事件 / 区域 / 系统状态接口。"""
from __future__ import annotations

import json
import time
from contextlib import closing

from fastapi import APIRouter, Query

from core.config import ROOT, settings
from core.db import connect, db_size_mb

from ..services import events as events_svc
from ..services import vessel_state as vs

router = APIRouter(prefix="/api", tags=["meta"])


@router.get("/health")
def health():
    return {"ok": True, "ts": int(time.time())}


@router.get("/stats/summary")
def stats_summary():
    return vs.summary()


@router.get("/stream/status")
def stream_status():
    now = int(time.time())
    with closing(connect()) as conn:
        h = conn.execute("SELECT * FROM stream_health ORDER BY ts DESC LIMIT 1").fetchone()
        recent = conn.execute(
            "SELECT ts, rate_1m, connected FROM stream_health WHERE ts >= ? ORDER BY ts",
            (now - 3600,),
        ).fetchall()
        meta_rows = conn.execute("SELECT key, value FROM meta").fetchall()
    meta = {r["key"]: r["value"] for r in meta_rows}
    return {
        "now": now,
        "connected": bool(h["connected"]) if h else False,
        "last_health_ts": h["ts"] if h else None,
        "last_message_ts": h["last_msg_ts"] if h else None,
        "seconds_since_message": (now - h["last_msg_ts"]) if h and h["last_msg_ts"] else None,
        "rate_1m": h["rate_1m"] if h else 0,
        "msgs_total": h["msgs_total"] if h else 0,
        "subscription": json.loads(h["bboxes"]) if h and h["bboxes"] else settings.bboxes,
        "region": meta.get("collector_region"),
        "collector_last_beat": int(meta["collector_last_beat"]) if meta.get("collector_last_beat") else None,
        "collector_alive": bool(meta.get("collector_last_beat"))
        and now - int(meta["collector_last_beat"]) < max(120, settings.health_interval * 4),
        "history": [dict(r) for r in recent],
        "db_size_mb": db_size_mb(),
        "analytics_last_run": int(meta["analytics_last_run"]) if meta.get("analytics_last_run") else None,
        "archive_enabled": settings.archive_enabled,
        "llm_enabled": settings.llm_enabled,
    }


@router.get("/events")
def list_events(
    kinds: str = Query("fishing,gap,geofence"),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(200, ge=1, le=1000),
    mmsi: int | None = None,
    zone_id: str | None = None,
):
    kind_list = [k.strip() for k in kinds.split(",") if k.strip()]
    evs = events_svc.query_events(kinds=kind_list, hours=hours, limit=limit, mmsi=mmsi, zone_id=zone_id)
    return {"count": len(evs), "events": evs}


@router.get("/zones")
def list_zones():
    return {"zones": events_svc.zones()}


@router.get("/regions")
def list_regions():
    p = ROOT / "config" / "regions.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    out = []
    for key, v in data.items():
        bbs = v.get("bboxes") or []
        if not bbs:
            continue
        b = bbs[0]
        out.append({
            "key": key,
            "name": v.get("name", key),
            "bbox": [b[0][0], b[0][1], b[1][0], b[1][1]],
            "note": v.get("note"),
        })
    return {"regions": out, "active": settings.bboxes}
