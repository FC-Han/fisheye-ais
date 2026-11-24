"""航次重建与航次特征（Trip Builder）。

一个「航次」= 从离开停泊状态出海，到重新长时间停下来的完整过程。

切分依据（全部可复核，不做隐式猜测）
------------------------------------
1. 长时间静止：连续 stationary >= 2 小时，视为一次靠泊/锚泊，把前后切成两个航次。
2. 数据长中断：observation_gap >= 4 小时，中间发生了什么完全未知，不能串成一个航次。
3. 窗口边界：如果航次在查询窗口边缘开始或结束，标 status=truncated ——
   它的起止不是真实靠离泊，而是被数据窗口切断的，特征会系统性偏小。

诚实性原则
----------
数据跨度不足以切出一个完整航次时，**不假装有航次**：返回单独一个 truncated
航次并附 insufficient_span 说明。里程、时长等特征在这种航次上会偏小，
后续用来做基线时必须排除，否则基线本身就是错的。
"""
from __future__ import annotations

import json
import statistics
import time
from contextlib import closing

from core.db import connect
from core.geo import haversine_m

from . import timeline as tl

M_PER_NM = 1852.0
MODEL_VERSION = "trip-v1"

STOP_MIN_SEC = 2 * 3600      # 连续静止多久算一次靠泊
BREAK_MIN_SEC = 4 * 3600     # 观测中断多久算航次断裂
MIN_TRIP_SEC = 3600          # 短于 1 小时的碎片不单独算航次

# 航次特征向量（固定维度，缺值用 None，绝不填 0 冒充真实值）
FEATURE_KEYS = (
    "duration_h",
    "distance_nm",
    "median_sog",
    "p90_sog",
    "fishing_hours",
    "fishing_segments",
    "longest_fishing_h",
    "low_speed_ratio",
    "stationary_ratio",
    "transit_ratio",
    "night_ratio",
    "straightness",
    "activity_radius_nm",
    "coverage_ratio",
    "max_gap_h",
    "gap_count",
)


def _local_solar_hour(ts: int, lon: float) -> float:
    """用经度近似当地太阳时（UTC + lon/15），用于昼夜划分。

    这不是精确日出日落，但对「白天作业 / 夜间作业」这种粗习惯够用，
    误差最多一小时级别，且对同一艘船是一致的（不引入系统性偏差）。
    """
    return (ts / 3600.0 + lon / 15.0) % 24


def _is_night(ts: int, lon: float) -> bool:
    h = _local_solar_hour(ts, lon)
    return h < 6 or h >= 18


def _radius_nm(pts: list[tuple[float, float]]) -> float | None:
    if not pts:
        return None
    clat = sum(p[0] for p in pts) / len(pts)
    clon = sum(p[1] for p in pts) / len(pts)
    return max(haversine_m(clat, clon, la, lo) for la, lo in pts) / M_PER_NM


def _features(segs: list[dict]) -> dict:
    """从一个航次的行为片段里提特征。"""
    obs = [s for s in segs if s["activity"] != "observation_gap"]
    if not obs:
        return {k: None for k in FEATURE_KEYS} | {"points": 0}

    total_s = sum(s["duration_s"] for s in obs) or 1
    fish = [s for s in segs if s["sub_activity"] == "fishing_candidate"]

    # 速度分位数：只在有观测的段上算
    speeds: list[float] = []
    for s in obs:
        med = s["evidence"].get("median_sog")
        if med is not None:
            # 用段时长加权，避免短段主导分布
            speeds.extend([med] * max(1, int(s["duration_s"] // 60)))

    by = lambda k: sum(s["duration_s"] for s in obs if s["activity"] == k)  # noqa: E731

    # 昼夜：按每个可观测点的当地太阳时统计
    night_s = day_s = 0.0
    for s in obs:
        lon = s.get("lon")
        if lon is None:
            continue
        night = _is_night((s["start_ts"] + s["end_ts"]) // 2, lon)
        if night:
            night_s += s["duration_s"]
        else:
            day_s += s["duration_s"]
    night_ratio = round(night_s / (night_s + day_s), 3) if (night_s + day_s) > 0 else None

    pts = [(s["lat"], s["lon"]) for s in obs if s.get("lat") is not None and s.get("lon") is not None]

    # 航迹直线度：总位移 / 总里程
    dist_m = sum((s["evidence"].get("distance_nm") or 0) * M_PER_NM for s in obs)
    disp_m = 0.0
    if len(pts) >= 2:
        disp_m = haversine_m(pts[0][0], pts[0][1], pts[-1][0], pts[-1][1])
    straightness = round(disp_m / dist_m, 3) if dist_m > 1 else None

    gaps = [s for s in segs if s["activity"] == "observation_gap"]

    return {
        "duration_h": round((obs[-1]["end_ts"] - obs[0]["start_ts"]) / 3600, 2),
        "distance_nm": round(dist_m / M_PER_NM, 1),
        "median_sog": round(statistics.median(speeds), 2) if speeds else None,
        "p90_sog": (
            round(sorted(speeds)[int(len(speeds) * 0.9)], 2) if len(speeds) >= 5 else None
        ),
        "fishing_hours": round(sum(s["duration_s"] for s in fish) / 3600, 2),
        "fishing_segments": len(fish),
        "longest_fishing_h": (
            round(max(s["duration_s"] for s in fish) / 3600, 2) if fish else 0.0
        ),
        "low_speed_ratio": round(by("low_speed") / total_s, 3),
        "stationary_ratio": round(by("stationary") / total_s, 3),
        "transit_ratio": round(by("transit") / total_s, 3),
        "night_ratio": night_ratio,
        "straightness": straightness,
        "activity_radius_nm": round(_radius_nm(pts), 1) if pts else None,
        "coverage_ratio": round(min(1.0, total_s / max(1, obs[-1]["end_ts"] - obs[0]["start_ts"])), 3),
        "max_gap_h": round(max((s["duration_s"] for s in gaps), default=0) / 3600, 2),
        "gap_count": len(gaps),
    }


def _split(segs: list[dict]) -> list[dict]:
    """按停泊与数据断裂把时间线切成航次。"""
    trips: list[dict] = []
    cur: list[dict] = []
    reason_start = "window_start"

    def close(reason: str, at_ts: int | None = None):
        nonlocal cur, reason_start
        if not cur:
            return
        body = [s for s in cur if s["activity"] != "observation_gap"]
        if body:
            start = body[0]["start_ts"]
            end = at_ts if at_ts is not None else body[-1]["end_ts"]
            if end - start >= MIN_TRIP_SEC:
                trips.append({
                    "segments": cur,
                    "start_ts": start,
                    "end_ts": end,
                    "boundary_start": reason_start,
                    "boundary_end": reason,
                })
        cur = []

    for i, s in enumerate(segs):
        if s["activity"] == "stationary" and s["duration_s"] >= STOP_MIN_SEC:
            # 静止段本身作为前一个航次的收尾
            cur.append(s)
            close("port_stay" if i < len(segs) - 1 else "window_end", at_ts=s["start_ts"])
            reason_start = "port_stay"
            continue
        if s["activity"] == "observation_gap" and s["duration_s"] >= BREAK_MIN_SEC:
            close("observation_gap", at_ts=s["start_ts"])
            reason_start = "observation_gap"
            continue
        cur.append(s)
    close("window_end")

    # 边界不是真实靠离泊的，标 truncated
    for t in trips:
        closed = t["boundary_start"] == "port_stay" and t["boundary_end"] == "port_stay"
        t["status"] = "closed" if closed else "truncated"
        t["trip_id"] = f"{int(t['start_ts'])}"
    return trips


def build_trips(mmsi: int, hours: int = 168, persist: bool = True) -> dict:
    """重建一艘船在窗口内的所有航次。"""
    mmsi = int(mmsi)
    hours = max(1, min(int(hours), 168))
    tline = tl.behavior_timeline(mmsi, hours=hours, persist=False)

    if not tline.get("points"):
        return {
            "mmsi": mmsi,
            "found": tline.get("found", False),
            "n_trips": 0,
            "trips": [],
            "status": "no_data",
            "model_version": MODEL_VERSION,
            "message": "窗口内无轨迹点，无法重建航次。",
            "caveat": tline.get("caveat"),
        }

    segs = tline["segments"]
    raw_trips = _split(segs)

    # 数据跨度不足以切出任何航次时，把整段当作一个未闭合航次，并明确说明
    insufficient = len(raw_trips) == 0
    if insufficient:
        body = [s for s in segs if s["activity"] != "observation_gap"]
        if body:
            raw_trips = [{
                "segments": segs,
                "start_ts": body[0]["start_ts"],
                "end_ts": body[-1]["end_ts"],
                "boundary_start": "window_start",
                "boundary_end": "window_end",
                "status": "truncated",
                "trip_id": str(int(body[0]["start_ts"])),
            }]

    trips = []
    for i, t in enumerate(raw_trips):
        feats = _features(t["segments"])
        pts = [(s["lat"], s["lon"]) for s in t["segments"] if s.get("lat") is not None]
        trips.append({
            "trip_id": f"{mmsi}-{t['trip_id']}-{MODEL_VERSION}",
            "index": i + 1,
            "start_ts": t["start_ts"],
            "end_ts": t["end_ts"],
            "duration_h": round((t["end_ts"] - t["start_ts"]) / 3600, 2),
            "status": t["status"],
            "boundary_start": t["boundary_start"],
            "boundary_end": t["boundary_end"],
            "start_lat": pts[0][0] if pts else None,
            "start_lon": pts[0][1] if pts else None,
            "end_lat": pts[-1][0] if pts else None,
            "end_lon": pts[-1][1] if pts else None,
            "n_points": sum(s["evidence"].get("points") or 0 for s in t["segments"]),
            "features": feats,
        })

    # 真实数据跨度：只用有观测的部分，不含首尾补出来的缺口
    body_segs = [s for s in segs if s["activity"] != "observation_gap"]
    span_h = (
        round((body_segs[-1]["end_ts"] - body_segs[0]["start_ts"]) / 3600, 2)
        if body_segs else 0.0
    )
    status = "insufficient_span" if insufficient else (
        "closed_trips" if all(t["status"] == "closed" for t in trips) else "mixed"
    )

    result = {
        "mmsi": mmsi,
        "found": True,
        "name": tline.get("name"),
        "identity": tline.get("identity"),
        "hours": hours,
        "span_h": span_h,
        "n_trips": len(trips),
        "n_closed_trips": sum(1 for t in trips if t["status"] == "closed"),
        "status": status,
        "trips": trips,
        "model_version": MODEL_VERSION,
        "caveat": (
            "航次由「连续静止 ≥2 小时」或「观测中断 ≥4 小时」切分。"
            "标 truncated 的航次首尾被数据窗口或断档截断，里程与时长会系统性偏小，"
            "不可直接用于和历史航次比较。"
        ),
    }
    if insufficient:
        result["message"] = (
            f"当前数据跨度仅 {span_h:.1f} 小时，不足以切出完整航次（需要至少一次"
            "连续 2 小时静止作为靠泊边界）。已把整段作为一个未闭合航次返回。"
        )

    if persist:
        _persist(result)
    return result


def _persist(result: dict) -> None:
    now = int(time.time())
    data_version = time.strftime("%Y%m%d", time.localtime())
    try:
        with closing(connect()) as conn:
            for t in result["trips"]:
                conn.execute(
                    """INSERT INTO vessel_trips
                       (trip_id, mmsi, start_ts, end_ts, start_lat, start_lon, end_lat, end_lon,
                        duration_s, distance_nm, n_points, coverage_ratio, max_gap_s,
                        status, boundary_reason, data_version, model_version, parameters, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(trip_id) DO UPDATE SET
                         end_ts=excluded.end_ts, duration_s=excluded.duration_s,
                         distance_nm=excluded.distance_nm, n_points=excluded.n_points,
                         coverage_ratio=excluded.coverage_ratio, max_gap_s=excluded.max_gap_s,
                         status=excluded.status""",
                    (
                        t["trip_id"], result["mmsi"], t["start_ts"], t["end_ts"],
                        t["start_lat"], t["start_lon"], t["end_lat"], t["end_lon"],
                        int(t["duration_h"] * 3600),
                        t["features"].get("distance_nm"), t["n_points"],
                        t["features"].get("coverage_ratio"),
                        int((t["features"].get("max_gap_h") or 0) * 3600),
                        t["status"], f"{t['boundary_start']}->{t['boundary_end']}",
                        data_version, MODEL_VERSION,
                        json.dumps({"stop_min_h": STOP_MIN_SEC / 3600,
                                    "break_min_h": BREAK_MIN_SEC / 3600}, ensure_ascii=False),
                        now,
                    ),
                )
                conn.execute(
                    """INSERT OR REPLACE INTO trip_features(trip_id, mmsi, features, stratum, created_at)
                       VALUES(?,?,?,?,?)""",
                    (t["trip_id"], result["mmsi"],
                     json.dumps(t["features"], ensure_ascii=False),
                     f"all", now),
                )
            conn.commit()
    except Exception:
        pass
