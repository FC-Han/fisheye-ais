"""地图 / 船舶相关接口。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Query

from ..services import behavior as behavior_svc
from ..services import hotspots as hotspot_svc
from ..services import vessel_state as vs

router = APIRouter(prefix="/api", tags=["map"])


def _bbox(bbox: str) -> tuple[float, float, float, float]:
    try:
        minlat, minlon, maxlat, maxlon = (float(x) for x in bbox.split(","))
    except (ValueError, TypeError):
        raise HTTPException(400, "bbox 格式应为 minlat,minlon,maxlat,maxlon")
    if not (-90 <= minlat < maxlat <= 90 and -180 <= minlon < maxlon <= 180):
        raise HTTPException(400, "bbox 超出合法经纬度范围")
    return minlat, minlon, maxlat, maxlon


@router.get("/vessels")
def list_vessels(
    bbox: str = Query(..., description="minlat,minlon,maxlat,maxlon"),
    limit: int = Query(3000, ge=1, le=20000),
    identity: str = Query("all"),
    since_sec: int = Query(3600, ge=60, le=86400),
    q: str | None = None,
):
    rows = vs.vessels_in_bbox(_bbox(bbox), limit=limit, identity=identity, since_sec=since_sec, q=q)
    return {"count": len(rows), "limit": limit, "vessels": rows}


@router.get("/map/density")
def map_density(
    bbox: str = Query(..., description="minlat,minlon,maxlat,maxlon"),
    cell: float = Query(5.0, gt=0.05, le=30),
    since_sec: int = Query(3600, ge=60, le=86400),
):
    return {"cells": vs.density(_bbox(bbox), cell=cell, since_sec=since_sec)}


@router.get("/search")
def search(q: str = Query(..., min_length=1), limit: int = Query(20, ge=1, le=100)):
    return {"results": vs.search(q, limit=limit)}


@router.get("/vessels/{mmsi}")
def get_vessel(mmsi: int):
    d = vs.vessel_detail(mmsi)
    if not d:
        raise HTTPException(404, "无该 MMSI 的记录")
    return d


@router.get("/vessels/{mmsi}/track")
def get_track(mmsi: int, hours: int = Query(24, ge=1, le=168), max_points: int = Query(5000, le=20000)):
    return vs.vessel_track(mmsi, hours=hours, max_points=max_points)


@router.get("/vessels/{mmsi}/behavior")
def get_behavior(mmsi: int, hours: int = Query(24, ge=1, le=168)):
    """单船运动状态：速度分段、里程、趋势、与事件的对应关系。"""
    return behavior_svc.vessel_behavior(mmsi, hours=hours)


@router.get("/hotspots")
def hotspots(
    hours: int = Query(24, ge=1, le=168),
    cell: float = Query(1.0, gt=0.1, le=10),
    limit: int = Query(40, ge=1, le=200),
    bbox: str | None = Query(None, description="可选 minlat,minlon,maxlat,maxlon"),
):
    """渔船作业热点：按网格聚合的低速聚集度排序。"""
    bb = list(_bbox(bbox)) if bbox else None
    return hotspot_svc.fishing_hotspots(hours=hours, bbox=bb, cell=cell, limit=limit)
