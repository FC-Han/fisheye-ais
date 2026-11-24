"""港口点位查询（World Port Index）。

用途只有一个：给「静止段」判断它到底在港口里，还是在海上漂着。
这是行为分析里差别很大的一件事——在港 8 小时和在海上原地漂 8 小时，
渔业意义完全不同。

实现说明
--------
- 数据源：NGA World Port Index。优先 `data/ports/WPI.dbf`，否则用仓库自带的
  `world_port_index.zip`，首次查询时自动导入，克隆后无需手动操作
- 索引：内存里按 0.5° 网格分桶，查询时只扫邻近几格，3669 个点毫秒级返回
- 距离：Haversine，单位公里
- 不引入 numpy/scipy：这个量级完全不需要
"""
from __future__ import annotations

import logging
import math
import threading
from contextlib import closing

from core.db import connect
from core.geo import haversine_m

log = logging.getLogger("ports")

# 在港判定半径（公里），按 WPI 的港口规模分档。
#
# WPI 记录的是港口参考点（港湾中心或主泊位），不是泊位面；大港的实际靠泊范围
# 能铺开十几公里（比如汉堡沿易北河、鹿特丹沿新水道），而小渔码头只有几百米。
# 用一个固定半径必然两头都错，所以按规模分档：
#   L 大港 12 km / M 中等 8 km / S 小港 4 km / V 很小 3 km / 未知 5 km
# 即便分了档，结论仍然只是「候选」——WPI 没有泊位边界，也不保证收录所有渔码头。
PORT_RADIUS_KM = {"L": 12.0, "M": 8.0, "S": 4.0, "V": 3.0}
PORT_RADIUS_DEFAULT_KM = 5.0
# 近岸锚地判定半径：在这个范围内但仍不在港，按近港锚地处理
PORT_ANCHORAGE_KM = 25.0

GRID = 0.5  # 索引网格边长（度）

_cache_lock = threading.Lock()
_index: dict[tuple[int, int], list[dict]] | None = None
_count = 0


_PORT_SQL = (
    "SELECT port_id, name, country, lat, lon, harbor_size, harbor_type, "
    "harbor_size_code, harbor_type_code FROM ports"
)


def _fetch_rows() -> list:
    with closing(connect(readonly=True)) as conn:
        return conn.execute(_PORT_SQL).fetchall()


def _auto_import() -> list:
    """ports 表为空时，从仓库自带的 world_port_index.zip 导入一次。

    克隆后不需要手动跑 scripts/load_wpi.py。失败只降级（所有静止点被判为
    anchorage_candidate），不影响时间线、航次、指纹等其它能力。
    """
    try:
        from core.config import ROOT

        zip_path = ROOT / "world_port_index.zip"
        if not zip_path.exists():
            log.warning("没有港口数据：%s 不存在，港/锚地判定将退化为「未知」", zip_path.name)
            return []

        import sys

        if str(ROOT) not in sys.path:
            sys.path.insert(0, str(ROOT))
        from scripts.load_wpi import import_ports

        good, _skipped = import_ports(zip_path)
        log.info("ports 表为空，已从 %s 自动导入 %d 个港口", zip_path.name, good)
    except Exception as exc:  # 数据缺失不该让整条分析链挂掉
        log.warning("港口数据自动导入失败，港/锚地判定将退化为「未知」：%s", exc)
        return []
    return _fetch_rows()


def _load() -> dict[tuple[int, int], list[dict]]:
    """把 ports 表读进内存并按网格分桶。只在首次调用时执行。"""
    global _index, _count
    rows = _fetch_rows()
    if not rows:
        rows = _auto_import()
    idx: dict[tuple[int, int], list[dict]] = {}
    for r in rows:
        p = {
            "port_id": r["port_id"],
            "name": r["name"],
            "country": r["country"],
            "lat": r["lat"],
            "lon": r["lon"],
            "harbor_size": r["harbor_size"],
            "harbor_type": r["harbor_type"],
            "harbor_size_code": r["harbor_size_code"],
            "harbor_type_code": r["harbor_type_code"],
        }
        key = (math.floor(p["lat"] / GRID), math.floor(p["lon"] / GRID))
        idx.setdefault(key, []).append(p)
    _count = len(rows)
    return idx


def _get_index() -> dict[tuple[int, int], list[dict]]:
    global _index
    if _index is None:
        with _cache_lock:
            if _index is None:
                _index = _load()
    return _index


def reset_cache() -> None:
    """重新导入 ports 表之后调用，让下次查询读到新数据。"""
    global _index, _count
    with _cache_lock:
        _index = None
        _count = 0


def port_count() -> int:
    _get_index()
    return _count


def nearest_port(lat: float, lon: float, max_km: float = PORT_ANCHORAGE_KM) -> dict | None:
    """返回 (lat, lon) 指定半径内最近的港口，附距离。没有则返回 None。

    距离为 None 表示「附近没有已知港口」，这是一个有信息量的结论：
    说明这段静止更可能是海上锚泊/漂航，而不是靠港。
    """
    if lat is None or lon is None:
        return None
    idx = _get_index()
    if not idx:
        return None

    # 邻近网格圈数：max_km 换算成度，再除以网格步长，向上取整 + 1 兜底
    ring = math.ceil(max_km / 111.0 / GRID) + 1
    cy, cx = math.floor(lat / GRID), math.floor(lon / GRID)

    best: dict | None = None
    best_m = max_km * 1000.0
    for gy in range(cy - ring, cy + ring + 1):
        for gx in range(cx - ring, cx + ring + 1):
            for p in idx.get((gy, gx), ()):
                d = haversine_m(lat, lon, p["lat"], p["lon"])
                if d < best_m:
                    best_m = d
                    best = p
    if best is None:
        return None
    code = (best.get("harbor_size_code") or "").strip().upper()
    return {
        "port_id": best["port_id"],
        "name": best["name"],
        "country": best["country"],
        "harbor_size": best["harbor_size"],
        "harbor_type": best["harbor_type"],
        "distance_km": round(best_m / 1000.0, 2),
        "in_port_radius_km": in_port_radius(code),
    }


def in_port_radius(size_code: str | None) -> float:
    """该规模港口的在港判定半径（公里）。"""
    return PORT_RADIUS_KM.get((size_code or "").strip().upper(), PORT_RADIUS_DEFAULT_KM)


def classify_stationary(lat: float, lon: float) -> tuple[str, dict]:
    """静止点的港/锚地分类。

    返回 (sub_activity, evidence)：
      - `port_stay`            距已知港口在「该规模港口的在港半径」内
      - `anchorage_candidate`  不在港内（近岸锚地或完全在海上）
    两种都是候选，不是结论——WPI 是参考点，没有泊位边界。
    """
    near = nearest_port(lat, lon, PORT_ANCHORAGE_KM)
    if near is None:
        return "anchorage_candidate", {
            "nearest_port": None,
            "offshore": True,
            "note": f"{PORT_ANCHORAGE_KM:.0f} km 内无已知港口，更可能是海上锚泊或漂航",
        }
    radius = near["in_port_radius_km"]
    if near["distance_km"] <= radius:
        return "port_stay", {
            "nearest_port": near,
            "offshore": False,
            "in_port_radius_km": radius,
            "note": (
                f"距最近港口 {near['name']}（{near['country']}，{near['harbor_size'] or '规模未知'}）"
                f"约 {near['distance_km']} km，在港半径 {radius} km 内"
            ),
        }
    return "anchorage_candidate", {
        "nearest_port": near,
        "offshore": False,
        "in_port_radius_km": radius,
        "note": (
            f"距最近港口 {near['name']}（{near['country']}）约 {near['distance_km']} km，"
            f"超出该港在港半径 {radius} km，更可能是锚地或漂航"
        ),
    }


def lookup_many(points: list[tuple[float, float]], max_km: float = PORT_ANCHORAGE_KM) -> list[dict | None]:
    """批量查询，供导出/分析用。"""
    return [nearest_port(lat, lon, max_km) for lat, lon in points]
