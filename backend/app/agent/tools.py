"""智能体可调用的只读工具集。

原则：
- 只读，绝不执行任意 SQL
- 每个工具都有结果上限、时间上限
- 返回体里必须带 covered / caveat，让模型无法把“没数据”说成“没船”
"""
from __future__ import annotations

import json
import time
from contextlib import closing

from core.db import connect

from ..services import behavior as behavior_svc
from ..services import events as events_svc
from ..services import evidence as evidence_svc
from ..services import fingerprint as fp_svc
from ..services import hotspots as hotspot_svc
from ..services import jobs as job_svc
from ..services import ocean as ocean_svc
from ..services import reliability as rel_svc
from ..services import timeline as tl_svc
from ..services import vessel_state as vs

MAX_LIMIT = 200


def _caveat(extra: str = "") -> str:
    base = (
        "数据来源为 AISStream 实时 AIS，仅反映收到的信号，不代表海域内实际船舶总数；"
        "未收到信号不等于该海域无船。"
    )
    return base + (" " + extra if extra else "")


def get_stream_health() -> dict:
    with closing(connect()) as conn:
        h = conn.execute("SELECT * FROM stream_health ORDER BY ts DESC LIMIT 1").fetchone()
        count = conn.execute("SELECT COUNT(*) c FROM vessel_latest_state").fetchone()["c"]
        pos = conn.execute("SELECT COUNT(*) c FROM ais_positions").fetchone()["c"]
    now = int(time.time())
    if not h:
        return {"connected": False, "message": "尚未收到采集器健康记录，采集器可能未启动", "tracked_vessels": count}
    return {
        "connected": bool(h["connected"]),
        "last_health_ts": h["ts"],
        "seconds_since_health": now - h["ts"],
        "last_message_ts": h["last_msg_ts"],
        "seconds_since_last_message": (now - h["last_msg_ts"]) if h["last_msg_ts"] else None,
        "message_rate_per_sec": h["rate_1m"],
        "messages_total": h["msgs_total"],
        "tracked_vessels": count,
        "stored_positions": pos,
        "subscription": json.loads(h["bboxes"]) if h["bboxes"] else None,
        "caveat": _caveat("若 seconds_since_last_message 很大，说明是我方链路问题，不应解读为船舶异常。"),
    }


def get_live_vessels(bbox: list[float] | None = None, identity: str = "all", limit: int = 50) -> dict:
    bbox = bbox or [-90, -180, 90, 180]
    rows = vs.vessels_in_bbox(tuple(bbox), limit=min(limit, MAX_LIMIT), identity=identity)
    fishing = [r for r in rows if r["identity"] in ("fishing", "candidate")]
    return {
        "count": len(rows),
        "fishing_related": len(fishing),
        "vessels": [
            {"mmsi": r["mmsi"], "name": r["ship_name"], "identity": r["identity"],
             "lat": r["lat"], "lon": r["lon"], "sog": r["sog"], "cog": r["cog"]}
            for r in rows
        ],
        "bbox": bbox,
        "caveat": _caveat(),
    }


def get_vessel_track(mmsi: int, hours: int = 24) -> dict:
    t = vs.vessel_track(int(mmsi), hours=min(hours, 168), max_points=2000)
    info = vs.vessel_detail(int(mmsi))
    if not info and not t["track"]:
        return {"mmsi": mmsi, "found": False, "message": "数据库中无该 MMSI 的记录"}
    pts = t["track"]
    speeds = [p["sog"] for p in pts if p["sog"] is not None]
    return {
        "mmsi": mmsi,
        "found": True,
        "name": info.get("ship_name") if info else None,
        "identity": info.get("identity") if info else None,
        "ship_type_text": info.get("ship_type_text") if info else None,
        "hours": hours,
        "points": len(pts),
        "first_ts": pts[0]["ts"] if pts else None,
        "last_ts": pts[-1]["ts"] if pts else None,
        "avg_sog": round(sum(speeds) / len(speeds), 2) if speeds else None,
        "fishing_events": t["fishing_events"],
        "gap_events": t["gap_events"],
        "track_sample": pts[:: max(1, len(pts) // 60)][:60],
        "caveat": _caveat("轨迹为降采样存储，不代表每一秒的实际航迹。"),
    }


def query_fishing_activity(hours: int = 24, bbox: list[float] | None = None, limit: int = 50) -> dict:
    evs = events_svc.query_events(kinds=["fishing"], hours=min(hours, 168), limit=min(limit, MAX_LIMIT))
    if bbox:
        minlat, minlon, maxlat, maxlon = bbox
        evs = [e for e in evs if e["lat"] and e["lon"] and minlat <= e["lat"] <= maxlat and minlon <= e["lon"] <= maxlon]
    vessels = {e["mmsi"] for e in evs}
    return {
        "hours": hours,
        "event_count": len(evs),
        "distinct_vessels": len(vessels),
        "events": evs,
        "bbox": bbox,
        "caveat": _caveat("“疑似捕捞”是规则推断（速度/时长/航迹形态），不是确认的捕捞行为，也不区分渔具。"),
    }


def get_gap_events(hours: int = 24, status: str | None = None, limit: int = 50) -> dict:
    evs = events_svc.query_events(kinds=["gap"], hours=min(hours, 168), limit=min(limit, MAX_LIMIT))
    if status:
        evs = [e for e in evs if e["status"] == status]
    return {
        "hours": hours,
        "count": len(evs),
        "candidates": sum(1 for e in evs if e["status"] == "candidate"),
        "events": evs,
        "caveat": _caveat(
            "信号中断 ≠ 关闭 AIS。coverage_unclear / stream_unclear 状态表示无法归因于船舶，"
            "只有 candidate 且覆盖良好时才值得进一步核实。"
        ),
    }


def query_geofence_events(hours: int = 24, zone_id: str | None = None, limit: int = 50) -> dict:
    evs = events_svc.query_events(kinds=["geofence"], hours=min(hours, 168), limit=min(limit, MAX_LIMIT), zone_id=zone_id)
    return {
        "hours": hours,
        "count": len(evs),
        "events": evs,
        "zones": events_svc.zones(),
        "caveat": _caveat(
            "围栏事件只是“进入某多边形”的观测事实。示例围栏为示意边界（source=demo），"
            "不能作为违规或违法依据。"
        ),
    }


def get_vessel_info(mmsi: int) -> dict:
    info = vs.vessel_detail(int(mmsi))
    if not info:
        return {"mmsi": mmsi, "found": False}
    return {"found": True, **info}


def query_fishing_hotspots(
    hours: int = 24,
    bbox: list[float] | None = None,
    cell: float = 1.0,
    limit: int = 12,
) -> dict:
    """渔船作业热点：按网格聚合的「低速聚集度」排序。"""
    res = hotspot_svc.fishing_hotspots(hours=min(hours, 168), bbox=bbox, cell=cell, limit=min(limit, 40))
    spots = []
    for c in res["hotspots"]:
        spots.append({
            "area": c["area"],
            "lat": c["lat"],
            "lon": c["lon"],
            "score": c["score"],
            "suspected_fishing_events": c["n_events"],
            "vessels": c["n_vessels"],
            "vessel_hours": c["vessel_hours"],
            "low_speed_vessels_now": c["low_speed_vessels"],
            "gap_candidates": c["gap_candidates"],
            "basis": c["basis"],
            "example_vessels": c["top_vessels"],
        })
    return {
        "hours": res["hours"],
        "cell_deg": res["cell_deg"],
        "hotspot_count": len(spots),
        "totals": res["totals"],
        "hotspots": spots,
        "bbox": bbox,
        "caveat": res["caveat"],
    }


def get_vessel_behavior(mmsi: int, hours: int = 24) -> dict:
    """单船运动状态：当前状态、速度分段、里程、趋势、与事件的对应。"""
    b = behavior_svc.vessel_behavior(int(mmsi), hours=min(hours, 168))
    if not b.get("found") or not b.get("points"):
        return b
    return {
        "mmsi": b["mmsi"],
        "name": b["name"],
        "identity": b["identity"],
        "ship_type_text": b["ship_type_text"],
        "hours": b["hours"],
        "points": b["points"],
        "span_hours": b["span_hours"],
        "freshness_min": b["freshness_min"],
        "current_state": b["current"],
        "stats": b["stats"],
        "trend": b["trend"],
        "segments": [
            {k: s[k] for k in ("state", "label", "start_ts", "end_ts", "duration_min",
                               "distance_nm", "avg_sog", "lat", "lon", "fishing")}
            for s in b["segments"]
        ],
        "fishing_event_count": len(b["fishing_events"]),
        "gap_event_count": len(b["gap_events"]),
        "narrative": b["narrative"],
        "caveat": b["caveat"],
    }


def find_vessels(q: str, limit: int = 10) -> dict:
    """按船名或 MMSI 片段搜索船舶，用于把用户口头说的船名解析成 MMSI。"""
    rows = vs.search(q, limit=min(limit, 30))
    return {
        "query": q,
        "count": len(rows),
        "results": rows,
        "caveat": "仅能在已接收过 AIS 静态信息的船舶中搜索；未收到静态报文的船不会出现在结果里。",
    }


def get_vessel_timeline(mmsi: int, hours: int = 24) -> dict:
    """行为时间线：把轨迹重建为「航行/低速/静止/观测中断/疑似捕捞候选」的连续片段。"""
    r = tl_svc.behavior_timeline(int(mmsi), hours=min(hours, 168))
    if not r.get("points"):
        return {
            "mmsi": mmsi, "found": r.get("found", False), "points": 0,
            "message": r.get("narrative"),
            "caveat": "无数据不等于无活动。",
        }
    return tl_svc.compact(r)


def get_observation_quality(mmsi: int, hours: int = 24) -> dict:
    """观测质量：这段数据能不能支撑结论。"""
    return rel_svc.observation_quality(int(mmsi), hours=min(hours, 168))


def get_event_evidence(mmsi: int, kind: str = "gap", ts: int | None = None) -> dict:
    """事件证据卡：观测事实、替代解释、无法解释的部分。"""
    if kind == "fishing":
        if ts is None:
            return {"error": "kind=fishing 时必须提供 ts"}
        return evidence_svc.fishing_evidence_card(int(mmsi), int(ts))
    if ts is not None:
        return evidence_svc.gap_evidence_card(int(mmsi), int(ts))
    cards = evidence_svc.gap_cards(int(mmsi), hours=48, limit=3)
    if not cards:
        return {"mmsi": mmsi, "count": 0, "message": "近 48 小时没有该船的中断事件"}
    return {"count": len(cards), "cards": cards[:2]}


def get_vessel_fingerprint(mmsi: int, days: int = 30) -> dict:
    """船舶长期行为基线（Vessel DNA）。历史航次不足时会明确返回 insufficient_history。"""
    r = fp_svc.vessel_fingerprint(int(mmsi), lookback_days=min(days, 90))
    if r.get("status") != "ok":
        return r
    return {
        "mmsi": r["mmsi"], "name": r.get("name"), "status": r["status"],
        "n_trips_used": r["n_trips_used"], "coverage": r["coverage"],
        "baseline": r["baseline"], "caveat": r.get("caveat"),
    }


def compare_vessel_behavior(mmsi: int, days: int = 30, hours: int = 24) -> dict:
    """当前航次 vs 自身历史基线，输出偏离项与稳健 Z 分数。"""
    r = fp_svc.compare_vessel_behavior(int(mmsi), lookback_days=min(days, 90), hours=min(hours, 168))
    if r.get("status") != "ok":
        return r
    return {
        "mmsi": r["mmsi"], "name": r.get("name"), "status": r["status"],
        "baseline_trips": r["baseline_trips"],
        "current_trip": r["current_trip"],
        "notable_count": r["notable_count"],
        "notable": r["notable"][:6],
        "summary": r["summary"],
        "caveat": r.get("caveat"),
    }


def get_ocean_features(mmsi: int, hours: int = 24, wait_seconds: int = 45,
                       products: str = "") -> dict:
    """轨迹点的海洋环境特征（SST / 表层流 / 海面高度 / 浪高 / 风 / 叶绿素气候态）。

    先读缓存；没有缓存就提交异步任务并最多等 wait_seconds 秒。
    等不到就如实返回 job_id 和「还没算完」，**绝不编造环境数值**。
    """
    caps = ocean_svc.capabilities()
    if not caps["available"]:
        return {
            "status": "unavailable",
            "reason": caps["reason"],
            "not_implemented": caps["not_implemented"],
            "message": "环境引擎当前不可用，不能回答与海温/海流/海浪相关的问题。",
        }

    hours = min(int(hours), 168)
    cached = ocean_svc.read_series(int(mmsi), hours)
    if cached.get("status") == "ok":
        cached["source"] = "cache"
        return cached

    keys = ocean_svc.normalize_products(products)
    job = job_svc.enqueue("ocean_track", {
        "mmsi": int(mmsi), "hours": hours, "products": keys,
    })
    deadline = time.time() + max(0, min(int(wait_seconds), 120))
    while time.time() < deadline:
        j = job_svc.get(job["job_id"])
        if j and j["status"] in ("done", "failed"):
            break
        time.sleep(1.5)

    j = job_svc.get(job["job_id"], with_result=True)
    if not j:
        return {"status": "error", "message": "任务不存在"}

    if j["status"] != "done":
        return {
            "status": j["status"],
            "job_id": job["job_id"],
            "progress": j.get("progress"),
            "message": (
                "环境提取任务已提交但还没算完（需要访问 Copernicus 远程服务，"
                "通常 20–60 秒）。请告诉用户稍后再问一次，不要凭猜测给数值。"
            ),
            "error": j.get("error"),
        }

    r = (j.get("result") or {})
    return {
        "status": r.get("status"),
        "source": "fresh",
        "mmsi": r.get("mmsi"),
        "hours": r.get("hours"),
        "product": r.get("product", {}).get("label"),
        "products": [
            {"key": p.get("key"), "label": p.get("label"), "basis": p.get("basis"),
             "status": p.get("status"), "points": p.get("points")}
            for p in (r.get("products") or [])
        ],
        "points": r.get("points"),
        "summary": r.get("summary"),
        "sst_c": r.get("sst_c"),
        "current_speed_m_s": r.get("current_speed_m_s"),
        "wave_height_m": r.get("wave_height_m"),
        "wind_speed_m_s": (r.get("summary") or {}).get("wind_speed_m_s"),
        "wind_beaufort": (r.get("summary") or {}).get("wind_beaufort"),
        "wind_direction": (r.get("summary") or {}).get("wind_direction"),
        "wind_basis": (r.get("summary") or {}).get("wind_basis"),
        "chl_mg_m3": r.get("chl_mg_m3"),
        "lag_hours": r.get("lag_hours"),
        "note": r.get("note"),
        "caveat": r.get("caveat"),
        "series_head": (r.get("series") or [])[:24],
        "job_id": job["job_id"],
    }


def get_analysis_capabilities() -> dict:
    """当前系统能做什么、不能做什么。回答「能不能做 XX 分析」前先查这里。"""
    dna = fp_svc.dna_readiness()
    ocean_caps = ocean_svc.capabilities()
    return {
        "behavior_timeline": {"available": True, "tool": "get_vessel_timeline"},
        "observation_quality": {"available": True, "tool": "get_observation_quality"},
        "event_evidence": {"available": True, "tool": "get_event_evidence"},
        "vessel_dna": {
            "available": dna["ready"],
            "tool": "get_vessel_fingerprint / compare_vessel_behavior",
            "status": dna["verdict"],
            "what_is_needed": dna["what_is_needed"],
            "data_span_hours": dna["data_span_hours"],
        },
        "ocean_environment": {
            "available": ocean_caps["available"],
            "tool": "get_ocean_features",
            "reason": ocean_caps["reason"],
            "variables": ["sst_c", "u_m_s", "v_m_s", "ssh_m",
                          "wave_height_m", "wave_dir_deg", "wave_period_s",
                          "wind_speed_m_s", "wind_dir_deg", "wind_basis",
                          "chl_mg_m3"],
            "wind_sources": ocean_caps.get("wind_sources"),
            "products": ocean_caps["products"],
            "climatology_reference": ocean_caps["climatology_reference"],
            "not_implemented": ocean_caps["not_implemented"],
            "caveat": ocean_caps["caveat"],
        },
        "sar_verification": {"available": False, "reason": "未接入 SAR 影像检索与目标检测"},
        "gear_type": {"available": False, "reason": "缺少渔具登记库与标注数据，不做渔具级判断"},
        "port_stay": {
            "available": True,
            "note": "基于 NGA World Port Index 港口点位区分港口停留与锚泊，结论为候选",
        },
        "caveat": "行为标签均为规则推断（速度阈值 + 航迹形态），非确认事实；置信度未经概率校准。",
    }


TOOLS = {
    "get_live_vessels": get_live_vessels,
    "get_vessel_track": get_vessel_track,
    "get_vessel_info": get_vessel_info,
    "get_vessel_behavior": get_vessel_behavior,
    "get_vessel_timeline": get_vessel_timeline,
    "get_observation_quality": get_observation_quality,
    "get_event_evidence": get_event_evidence,
    "get_vessel_fingerprint": get_vessel_fingerprint,
    "compare_vessel_behavior": compare_vessel_behavior,
    "get_ocean_features": get_ocean_features,
    "get_analysis_capabilities": get_analysis_capabilities,
    "query_fishing_activity": query_fishing_activity,
    "query_fishing_hotspots": query_fishing_hotspots,
    "get_gap_events": get_gap_events,
    "query_geofence_events": query_geofence_events,
    "get_stream_health": get_stream_health,
    "find_vessels": find_vessels,
}

TOOL_SCHEMAS = [
    {
        "type": "function",
        "function": {
            "name": "get_live_vessels",
            "description": "查询某个经纬度框内当前（近1小时）有 AIS 报告的船舶",
            "parameters": {
                "type": "object",
                "properties": {
                    "bbox": {"type": "array", "items": {"type": "number"},
                             "description": "[minlat, minlon, maxlat, maxlon]"},
                    "identity": {"type": "string", "enum": ["all", "fishing", "candidate", "all_fishing", "non_fishing"]},
                    "limit": {"type": "integer"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vessel_track",
            "description": "查询某艘船过去 N 小时的轨迹、疑似捕捞片段和信号中断事件",
            "parameters": {
                "type": "object",
                "properties": {"mmsi": {"type": "integer"}, "hours": {"type": "integer"}},
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vessel_info",
            "description": "查询单船档案与 24 小时统计",
            "parameters": {"type": "object", "properties": {"mmsi": {"type": "integer"}}, "required": ["mmsi"]},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vessel_behavior",
            "description": (
                "分析某艘船最近 N 小时的运动状态：当前是锚泊/低速作业/巡航/高速，"
                "速度分段与时间段、航迹里程、活动半径、航迹形态（直线度）、"
                "与前三小时相比的加速/减速趋势，以及是否与疑似捕捞片段、信号中断重合。"
                "当用户问某艘船「在干什么」「最近状态」「有没有在作业」时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer", "description": "船舶 MMSI"},
                    "hours": {"type": "integer", "description": "回看小时数，默认 24，上限 168"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_fishing_hotspots",
            "description": (
                "查询渔船作业热点：把海域按格网聚合，按「已接收 AIS 渔船的疑似捕捞时长 + "
                "低速活动密度」排序，返回海域名、得分、事件数、涉及船舶数、当前低速船数、中断线索。"
                "当用户问「哪里渔船多」「哪个海域在作业」「热点在哪」时使用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "时间窗口，默认 24，上限 168"},
                    "bbox": {"type": "array", "items": {"type": "number"},
                             "description": "[minlat, minlon, maxlat, maxlon]，可省略表示全球"},
                    "cell": {"type": "number", "description": "网格边长（度），默认 1.0，海域大时用 2-5"},
                    "limit": {"type": "integer", "description": "返回热点个数，默认 12"},
                },
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vessel_timeline",
            "description": (
                "把某艘船的轨迹重建为行为时间线：时间连续、互不重叠的片段，"
                "每个片段带 activity（航行/低速移动/静止/观测中断）、"
                "sub_activity（疑似捕捞候选）、时长、代表位置、观测可靠性。"
                "当用户问「这艘船这段时间在做什么」「过去几天怎么过的」「有没有在作业」时首选此工具。"
                "比 get_vessel_behavior 更结构化，且显式标出观测缺口。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer", "description": "船舶 MMSI"},
                    "hours": {"type": "integer", "description": "回看小时数，默认 24，上限 168（7 天）"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_observation_quality",
            "description": (
                "评估某艘船在指定窗口内的观测质量：覆盖率、报文间隔、缺口数量与最长缺口、"
                "上游链路状态，并给出 good/fair/poor 分级。"
                "在用户要求「分析某船行为」时，应先用它判断数据是否足以支撑结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer"},
                    "hours": {"type": "integer", "description": "默认 24"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_event_evidence",
            "description": (
                "获取事件证据卡：包含观测事实（缺口时长、中断前位置速度、上游链路状态、"
                "周边船舶同期表现、位置合理性）、替代解释清单、无法解释的部分、复核状态。"
                "回答「这条船为什么 AIS 消失了」「这个疑似捕捞片段可信吗」时使用。"
                "不要自己编造原因，必须引用卡片里的 alternative_explanations。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer"},
                    "kind": {"type": "string", "enum": ["gap", "fishing"], "description": "gap=信号中断，fishing=疑似捕捞"},
                    "ts": {"type": "integer", "description": "事件起始时间戳；gap 可省略取最近一条"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vessel_fingerprint",
            "description": (
                "查询某艘船的历史行为基线（Vessel DNA）：航次时长、作业时长、夜间比例等特征的"
                "中位数与分位数。历史航次不足时返回 insufficient_history，"
                "此时必须如实告诉用户数据不够，不得编造基线。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer"},
                    "days": {"type": "integer", "description": "回看天数，默认 30，上限 90"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "compare_vessel_behavior",
            "description": (
                "把当前航次与自身历史基线对比，找出显著偏离的特征项（稳健 Z 分数）。"
                "回答「这条船最近作业习惯有没有变」「和以前比有什么不同」时使用。"
                "返回 insufficient_history 时必须如实说明，不能给出差异结论。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer"},
                    "days": {"type": "integer", "description": "基线回看天数，默认 30"},
                    "hours": {"type": "integer", "description": "当前航次窗口，默认 24"},
                },
                "required": ["mmsi"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_analysis_capabilities",
            "description": (
                "查询系统当前具备哪些分析能力、哪些不具备及原因。"
                "当用户问「能不能分析环境关系」「能不能识别渔具」「能不能做行为指纹」时先调用它，"
                "然后按实际能力回答，不要承诺系统做不到的事。"
            ),
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "find_vessels",
            "description": "按船名（支持中文）或 MMSI 片段搜索船舶，把用户说的船名解析成 MMSI。查单船前若没有 MMSI 先用它。",
            "parameters": {
                "type": "object",
                "properties": {"q": {"type": "string"}, "limit": {"type": "integer"}},
                "required": ["q"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_fishing_activity",
            "description": "查询过去 N 小时的疑似捕捞事件，可按区域框过滤",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer"}, "bbox": {"type": "array", "items": {"type": "number"}},
                               "limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_gap_events",
            "description": "查询 AIS 信号中断候选事件",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer"}, "status": {"type": "string"}, "limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_geofence_events",
            "description": "查询区域进入/离开事件",
            "parameters": {
                "type": "object",
                "properties": {"hours": {"type": "integer"}, "zone_id": {"type": "string"}, "limit": {"type": "integer"}},
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_stream_health",
            "description": "查询 AIS 数据链路健康状态（是否连接、消息速率、最后接收时间）",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_ocean_features",
            "description": (
                "提取某船轨迹上的海洋环境特征：表层海温 SST、表层流速、海面高度、"
                "有效波高/波向/周期、10 m 风速风向（含蒲福风级）、"
                "叶绿素（2019–2023 同月气候态，不是当前值）。"
                "用于回答「这艘船在什么水温/什么海况/多大风的海域活动」"
                "「行为和海况有没有关系」这类问题。"
                "首次调用需要访问远程服务，约 20–60 秒；结果会缓存。"
                "风的来源必须说清楚：wind_gfs 是 GFS 近实时分析/短时预报（能覆盖今天），"
                "wind 是卫星散射计融合观测（滞后约 2 天），wind_era5 是 ERA5 再分析（滞后约 5 天）；"
                "逐点来源记在 wind_basis 里，数值可以差 1–2 m/s，不要混为一谈。"
                "注意：这只给出「船经历过的环境」，不能据此推断「船偏好什么环境」；"
                "也不能因为浪大/风大就断定船在避风，那是推断不是观测。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mmsi": {"type": "integer", "description": "船舶 MMSI"},
                    "hours": {"type": "integer", "description": "回看小时数，默认 24，上限 168"},
                    "wait_seconds": {
                        "type": "integer",
                        "description": "最多等待多少秒，默认 45。等不到会返回 job_id 而不是编造数值",
                    },
                    "products": {
                        "type": "string",
                        "description": (
                            "逗号分隔产品，默认 physics,waves,wind_gfs,bgc_clim。"
                            "想加卫星观测风用 wind，想加更早的历史风用 wind_era5（较慢）"
                        ),
                    },
                },
                "required": ["mmsi"],
            },
        },
    },
]
