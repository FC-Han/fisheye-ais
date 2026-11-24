"""地理计算工具：距离、方位角变化、点在多边形内判定。"""
from __future__ import annotations

import math

EARTH_R = 6371000.0


def haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = p2 - p1
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * EARTH_R * math.asin(math.sqrt(a))


def angle_diff(a: float, b: float) -> float:
    """两个航向角的最小夹角（0-180）。"""
    d = abs((a - b) % 360)
    return d if d <= 180 else 360 - d


def point_in_polygon(lat: float, lon: float, polygon: list[list[float]]) -> bool:
    """射线法。polygon 为 [[lon, lat], ...]（GeoJSON 顺序）。"""
    inside = False
    n = len(polygon)
    if n < 3:
        return False
    j = n - 1
    for i in range(n):
        xi, yi = polygon[i][0], polygon[i][1]
        xj, yj = polygon[j][0], polygon[j][1]
        if ((yi > lat) != (yj > lat)) and (lon < (xj - xi) * (lat - yi) / (yj - yi + 1e-12) + xi):
            inside = not inside
        j = i
    return inside


def bbox_contains(bbox: tuple[float, float, float, float], lat: float, lon: float) -> bool:
    minlat, minlon, maxlat, maxlon = bbox
    return minlat <= lat <= maxlat and minlon <= lon <= maxlon


def grid_cell(lat: float, lon: float, size: float) -> tuple[float, float]:
    return math.floor(lat / size) * size, math.floor(lon / size) * size
