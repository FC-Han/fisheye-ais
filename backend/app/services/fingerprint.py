"""Vessel DNA：船舶行为指纹与变化检测。

行为识别回答「它在做什么」，指纹回答「它平时怎么做、现在是否不同」。

三层结构
--------
1. 航次指纹（trip_features）：每个航次一个定长特征向量。
2. 长期指纹（fingerprint）：对同船的多个**已闭合**航次做稳健聚合
   （中位数 / MAD / P10 / P90），按季节分层。
3. 变化检测（changes）：当前航次特征对基线做稳健 Z 分数。

三条纪律
--------
- **只用已闭合航次建基线**。被数据窗口截断的航次里程和时长系统性偏小，
  混进基线会把基线拉低，导致后续一切比较失真。
- **稳健统计，不用均值**。渔船夏天在 A 渔场、冬天在 B 渔场，用均值会把
  正常的季节性转移算成异常。中位数 + MAD 对这种分布漂移不敏感。
- **样本不足就说不足**。少于 MIN_TRIPS 个闭合航次时返回
  insufficient_history，绝不拿一两个航次硬凑出一个「指纹」。
- **指纹不是身份**。船可能换渔具、换船长、换 MMSI，所以指纹带时间版本，
  统计窗口外的旧航次不参与当前基线。
"""
from __future__ import annotations

import json
import statistics
import time
from contextlib import closing

from core.db import connect

from . import trips as trip_svc

MODEL_VERSION = "dna-v1"
MIN_TRIPS = 3                 # 少于 3 个闭合航次不建指纹
Z_NOTABLE = 2.0
Z_STRONG = 3.0

# 参与指纹的特征（要求方向明确、可解释；比率类单独标记）
TRACKED = (
    "duration_h",
    "distance_nm",
    "fishing_hours",
    "fishing_segments",
    "longest_fishing_h",
    "low_speed_ratio",
    "night_ratio",
    "median_sog",
    "straightness",
    "activity_radius_nm",
)

FEATURE_LABEL = {
    "duration_h": "航次时长(h)",
    "distance_nm": "航行距离(nm)",
    "fishing_hours": "疑似作业时长(h)",
    "fishing_segments": "作业片段数",
    "longest_fishing_h": "最长连续作业(h)",
    "low_speed_ratio": "低速占比",
    "night_ratio": "夜间活动占比",
    "median_sog": "中位航速(kn)",
    "straightness": "航迹直线度",
    "activity_radius_nm": "活动半径(nm)",
}


def _season(ts: int) -> str:
    m = time.gmtime(ts).tm_mon
    if m in (12, 1, 2):
        return "DJF"
    if m in (3, 4, 5):
        return "MAM"
    if m in (6, 7, 8):
        return "JJA"
    return "SON"


def _robust(values: list[float]) -> dict:
    """稳健聚合：中位数、MAD、P10、P90。MAD 为 0 时给出合理退化值。"""
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return {"median": None, "mad": None, "p10": None, "p90": None, "n": 0}

    med = statistics.median(vals)
    mad = statistics.median([abs(v - med) for v in vals]) if len(vals) > 1 else 0.0
    # 单样本或全部相同时 MAD=0，无法算 Z 分数；记录 spread=0 让调用方知道限制
    return {
        "median": round(med, 3),
        "mad": round(mad, 3),
        "p10": round(vals[max(0, int(len(vals) * 0.1))], 3),
        "p90": round(vals[min(len(vals) - 1, int(len(vals) * 0.9))], 3),
        "n": len(vals),
    }


def _robust_z(current: float, base: dict) -> float | None:
    med, mad = base.get("median"), base.get("mad")
    if med is None or current is None:
        return None
    if not mad:  # MAD 为 0：退化成绝对差比较，避免除零，也避免假装有精度
        return None
    return round((current - med) / (1.4826 * mad), 2)


def vessel_fingerprint(mmsi: int, lookback_days: int = 30, persist: bool = True) -> dict:
    """建立/读取船舶长期行为指纹。

    只用已闭合航次；不足 MIN_TRIPS 个时返回 insufficient_history 并说明还差多少。
    """
    mmsi = int(mmsi)
    lookback_days = max(1, min(int(lookback_days), 90))
    hours = lookback_days * 24

    t = trip_svc.build_trips(mmsi, hours=hours, persist=True)
    all_trips = t.get("trips") or []
    closed = [x for x in all_trips if x["status"] == "closed"]

    info = {
        "mmsi": mmsi,
        "name": t.get("name"),
        "identity": t.get("identity"),
        "lookback_days": lookback_days,
        "model_version": MODEL_VERSION,
        "n_trips_total": len(all_trips),
        "n_trips_closed": len(closed),
        "n_trips_required": MIN_TRIPS,
    }

    if len(closed) < MIN_TRIPS:
        # 已积累的观测跨度，用来告诉用户还差多少
        span_h = t.get("span_h") or 0
        info.update({
            "status": "insufficient_history",
            "fingerprint": None,
            "message": (
                f"可用于建立基线的已闭合航次只有 {len(closed)} 个，少于所需的 {MIN_TRIPS} 个"
                f"（当前数据跨度 {span_h:.1f} 小时）。"
                "行为指纹需要同一艘船多个完整航次才能形成稳定基线，"
                "现阶段不能计算「它现在和平时有什么不同」——强行算出来的数字没有统计意义。"
            ),
            "why": {
                "closed_trip_required": MIN_TRIPS,
                "closed_trip_found": len(closed),
                "reason": "truncated 航次首尾被数据窗口截断，特征系统性偏小，不能进基线",
            },
            "caveat": t.get("caveat"),
        })
        return info

    # 按季节分组，样本够就分季节，不够就合并成全季基线
    by_season: dict[str, list[dict]] = {}
    for tr in closed:
        by_season.setdefault(_season(tr["start_ts"]), []).append(tr)

    strata: dict[str, dict] = {}
    for s, trs in by_season.items():
        if len(trs) < MIN_TRIPS:
            continue
        strata[s] = {k: _robust([tr["features"].get(k) for tr in trs]) for k in TRACKED}

    # 总基线（所有闭合航次）
    overall = {k: _robust([tr["features"].get(k) for tr in closed]) for k in TRACKED}

    fp_id = f"{mmsi}-all-{MODEL_VERSION}"
    now = int(time.time())
    result = {
        **info,
        "status": "ok",
        "fp_id": fp_id,
        "n_trips_used": len(closed),
        "coverage": {
            "start": min(tr["start_ts"] for tr in closed),
            "end": max(tr["end_ts"] for tr in closed),
        },
        "baseline": overall,
        "by_season": strata,
        "season_note": (
            "按季节分层的基线仅在该季节有足够航次时才单独给出；"
            "否则请参考总体基线，并注意季节性迁移可能被误判为异常。"
        ),
        "caveat": (
            "指纹是统计基线，不是船舶身份。样本量越大越稳定；"
            "换渔具、换渔场、换船长都会让旧基线失效，需要按时间窗口重建。"
        ),
    }

    if persist:
        try:
            with closing(connect()) as conn:
                conn.execute(
                    """INSERT INTO vessel_fingerprints
                       (fp_id, mmsi, stratum, n_trips, features, coverage_start, coverage_end,
                        data_version, model_version, created_at, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(fp_id) DO UPDATE SET
                         n_trips=excluded.n_trips, features=excluded.features,
                         coverage_start=excluded.coverage_start, coverage_end=excluded.coverage_end,
                         updated_at=excluded.updated_at""",
                    (fp_id, mmsi, "all", len(closed),
                     json.dumps({"baseline": overall, "by_season": strata}, ensure_ascii=False),
                     result["coverage"]["start"], result["coverage"]["end"],
                     time.strftime("%Y%m%d", time.localtime()), MODEL_VERSION, now, now),
                )
                conn.commit()
        except Exception:
            pass
    return result


def compare_vessel_behavior(mmsi: int, lookback_days: int = 30, hours: int = 24) -> dict:
    """把「最近一个航次」和「自身历史基线」对比，输出行为差异与变化点。"""
    mmsi = int(mmsi)
    fp = vessel_fingerprint(mmsi, lookback_days=lookback_days, persist=True)
    if fp.get("status") != "ok":
        return {
            "mmsi": mmsi,
            "name": fp.get("name"),
            "status": "insufficient_history",
            "message": fp.get("message"),
            "why": fp.get("why"),
            "fingerprint_available": False,
            "caveat": fp.get("caveat"),
        }

    t = trip_svc.build_trips(mmsi, hours=max(1, min(int(hours), 168)), persist=False)
    trips = t.get("trips") or []
    if not trips:
        return {
            "mmsi": mmsi, "name": fp.get("name"), "status": "no_current_trip",
            "message": "当前窗口内没有可比较的航次。", "fingerprint_available": True,
        }

    cur = max(trips, key=lambda x: x["end_ts"])
    base = fp["baseline"]

    diffs = []
    for k in TRACKED:
        val = cur["features"].get(k)
        b = base.get(k) or {}
        z = _robust_z(val, b)
        if val is None or b.get("median") is None:
            continue
        delta = val - b["median"]
        pct = (delta / b["median"] * 100) if b["median"] else None
        severity = "strong" if z is not None and abs(z) >= Z_STRONG else (
            "notable" if z is not None and abs(z) >= Z_NOTABLE else "normal"
        )
        diffs.append({
            "feature": k,
            "label": FEATURE_LABEL.get(k, k),
            "current": round(val, 3),
            "baseline_median": b["median"],
            "p10": b.get("p10"),
            "p90": b.get("p90"),
            "delta": round(delta, 3),
            "delta_pct": round(pct, 1) if pct is not None else None,
            "robust_z": z,
            "severity": severity,
            "z_available": z is not None,
        })

    notable = [d for d in diffs if d["severity"] != "normal"]
    changes = _changes(mmsi, cur, diffs, fp["fp_id"])

    return {
        "mmsi": mmsi,
        "name": fp.get("name"),
        "status": "ok",
        "fingerprint_available": True,
        "baseline_trips": fp["n_trips_used"],
        "baseline_coverage": fp["coverage"],
        "current_trip": {
            "trip_id": cur["trip_id"],
            "start_ts": cur["start_ts"],
            "end_ts": cur["end_ts"],
            "duration_h": cur["duration_h"],
            "status": cur["status"],
        },
        "diffs": diffs,
        "notable_count": len(notable),
        "notable": notable,
        "changes": changes,
        "summary": _summary(cur, notable, fp),
        "caveat": (
            "差异是统计比较，不是因果结论。robust_z 为 null 表示该特征在历史上没有"
            "离散度（MAD=0），无法判断是否异常。当前航次若为 truncated，"
            "里程与时长偏小会被误读为「变短」。"
        ),
    }


def _changes(mmsi: int, cur: dict, diffs: list[dict], fp_id: str) -> list[dict]:
    now = int(time.time())
    out = []
    for d in diffs:
        if d["severity"] == "normal":
            continue
        out.append({
            "feature": d["feature"],
            "label": d["label"],
            "current_value": d["current"],
            "baseline_median": d["baseline_median"],
            "robust_z": d["robust_z"],
            "direction": "up" if (d["delta"] or 0) > 0 else "down",
            "severity": d["severity"],
            "ts": cur["end_ts"],
            "explanation": (
                f"{d['label']} 为 {d['current']}，历史中位 {d['baseline_median']}"
                + (f"，偏离 {d['delta_pct']:+.0f}%" if d["delta_pct"] is not None else "")
                + (f"（稳健 Z={d['robust_z']}）" if d["robust_z"] is not None else "")
            ),
        })
    if out:
        try:
            with closing(connect()) as conn:
                for c in out:
                    conn.execute(
                        """INSERT OR REPLACE INTO behavior_changes
                           (change_id, mmsi, feature, current_value, baseline_median, robust_z,
                            direction, severity, ts, explanation, fp_id, created_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                        (f"{mmsi}-{c['feature']}-{c['ts']}-{MODEL_VERSION}", mmsi, c["feature"],
                         c["current_value"], c["baseline_median"], c["robust_z"], c["direction"],
                         c["severity"], c["ts"], c["explanation"], fp_id, now),
                    )
                conn.commit()
        except Exception:
            pass
    return out


def _summary(cur: dict, notable: list[dict], fp: dict) -> str:
    if not notable:
        return (
            f"当前航次（{cur['duration_h']:.1f} 小时）与自身 {fp['n_trips_used']} 个历史航次相比，"
            "已跟踪特征均未超出正常波动范围。"
        )
    parts = [
        f"当前航次（{cur['duration_h']:.1f} 小时）与 {fp['n_trips_used']} 个历史航次相比，"
        f"有 {len(notable)} 项特征偏离："
    ]
    for d in notable[:4]:
        pct = f"{d['delta_pct']:+.0f}%" if d["delta_pct"] is not None else "—"
        parts.append(f"{d['label']} {pct}；")
    return "".join(parts).rstrip("；") + "。"


def dna_readiness() -> dict:
    """全局：当前数据够不够做 DNA。给智能体判断「能不能回答这类问题」。"""
    with closing(connect()) as conn:
        row = conn.execute("SELECT MIN(ts) a, MAX(ts) b FROM ais_positions").fetchone()
        n_closed = conn.execute(
            "SELECT COUNT(*) c FROM vessel_trips WHERE status='closed'"
        ).fetchone()["c"]
        n_vessels_closed = conn.execute(
            "SELECT COUNT(DISTINCT mmsi) c FROM vessel_trips WHERE status='closed'"
        ).fetchone()["c"]
    span_h = round(((row["b"] or 0) - (row["a"] or 0)) / 3600, 2) if row and row["a"] else 0
    return {
        "data_span_hours": span_h,
        "closed_trips": n_closed,
        "vessels_with_closed_trips": n_vessels_closed,
        "closed_trip_required_per_vessel": MIN_TRIPS,
        "ready": n_closed >= MIN_TRIPS,
        "verdict": (
            "行为指纹可用" if n_closed >= MIN_TRIPS
            else f"数据不足：需要至少 {MIN_TRIPS} 个已闭合航次，当前 {n_closed} 个"
        ),
        "what_is_needed": (
            "一艘船要形成基线，需要至少连续采集到 3 次「出海—返港」循环。"
            "按近海渔船通常 1–3 天一个航次估算，单船需累计约 3–9 天连续 AIS 覆盖。"
        ),
    }
