"""把 Natural Earth 的 TopoJSON（world-atlas）转成本地 GeoJSON 底图。

产物：frontend/public/geo/land-50m.geojson
不用任何运行时依赖：地图只读本地文件，不依赖任何需要 Key 的瓦片服务。

用法：
    curl -o /tmp/land50.json https://cdn.jsdelivr.net/npm/world-atlas@2/land-50m.json
    python scripts/build_basemap.py /tmp/land50.json
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
OUT = ROOT / "frontend" / "public" / "geo" / "land-50m.geojson"
PRECISION = 3  # 保留 3 位小数 ≈ 100m，全球底图足够


def decode_arc(arc: list, scale: list[float], translate: list[float]) -> list[list[float]]:
    x = y = 0
    out = []
    for dx, dy in arc:
        x += dx
        y += dy
        out.append([round(x * scale[0] + translate[0], PRECISION), round(y * scale[1] + translate[1], PRECISION)])
    return out


def ring_points(arc_indices: list[int], arcs: list[list[list[float]]]) -> list[list[float]]:
    pts: list[list[float]] = []
    for idx in arc_indices:
        arc = arcs[idx] if idx >= 0 else list(reversed(arcs[~idx]))
        if pts and pts[-1] == arc[0]:
            pts.extend(arc[1:])
        else:
            pts.extend(arc)
    # 去掉连续重复点
    dedup = [pts[0]] if pts else []
    for p in pts[1:]:
        if p != dedup[-1]:
            dedup.append(p)
    return dedup


def main(src: Path, out: Path | None = None, precision: int = PRECISION) -> int:
    global OUT, PRECISION
    if out:
        OUT = out
    PRECISION = precision
    topo = json.loads(src.read_text(encoding="utf-8"))
    tr = topo.get("transform", {"scale": [1, 1], "translate": [0, 0]})
    scale, translate = tr["scale"], tr["translate"]
    arcs = [decode_arc(a, scale, translate) for a in topo["arcs"]]

    obj = topo["objects"].get("land") or next(iter(topo["objects"].values()))
    features = []

    def add_polygon(rings: list[list[int]]):
        coords = [ring_points(r, arcs) for r in rings]
        coords = [c for c in coords if len(c) >= 4]
        if not coords:
            return
        outer = coords[0]
        lons = [p[0] for p in outer]
        lats = [p[1] for p in outer]
        lon_span = max(lons) - min(lons)
        lat_span = max(lats) - min(lats)
        # 跨 180° 经线产生的退化环会把地图糊成一条横贯全球的亮带。
        # 南极洲（min_lat < -60）本身横跨全球，属正常情况，保留。
        if (lat_span < 0.1 and lon_span > 300) or (lon_span > 350 and min(lats) > -60):
            return
        # 丢弃极小岛屿
        if lon_span * lat_span < 0.002 and len(outer) < 40:
            return
        if len(coords) == 1:
            features.append({"type": "Feature", "properties": {}, "geometry": {"type": "Polygon", "coordinates": coords}})
        else:
            features.append({"type": "Feature", "properties": {}, "geometry": {"type": "MultiPolygon", "coordinates": [coords]}})

    if obj["type"] == "MultiPolygon":
        for poly in obj["arcs"]:
            add_polygon(poly)
    elif obj["type"] == "Polygon":
        add_polygon(obj["arcs"])
    elif obj["type"] == "GeometryCollection":
        for g in obj["geometries"]:
            if g["type"] == "MultiPolygon":
                for poly in g["arcs"]:
                    add_polygon(poly)
            elif g["type"] == "Polygon":
                add_polygon(g["arcs"])

    fc = {"type": "FeatureCollection", "features": features}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(fc, separators=(",", ":")), encoding="utf-8")
    print(f"wrote {OUT} | features={len(features)} | {OUT.stat().st_size/1024:.0f} KB")
    return 0


if __name__ == "__main__":
    src = Path(sys.argv[1] if len(sys.argv) > 1 else "/tmp/land50.json")
    out = Path(sys.argv[2]) if len(sys.argv) > 2 else None
    prec = int(sys.argv[3]) if len(sys.argv) > 3 else PRECISION
    sys.exit(main(src, out, prec))
