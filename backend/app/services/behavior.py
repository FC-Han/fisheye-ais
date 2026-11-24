"""单船运动状态分析：把一条轨迹翻译成「这艘船最近在干什么」。

不做行为识别模型，只做可解释的速度分段 + 几何指标：
- 速度分段：锚泊 / 低速（作业或漂流）/ 巡航 / 高速
- 几何指标：航迹里程、位移、直线度、活动半径
- 趋势：最近 3 小时与之前 3 小时的均速对比
- 与事件对齐：哪些分段落在疑似捕捞片段内、哪些地方信号中断过

所有输出都能回到具体时间点，便于用户在轨迹回放里核对。
"""
from __future__ import annotations

import time

from core.geo import haversine_m

from . import vessel_state as vs

STATE_LABEL = {
    "anchored": "锚泊/静止",
    "slow": "低速（作业或漂流）",
    "transit": "巡航航行",
    "fast": "高速航行",
    "unknown": "数据不足",
}

M_PER_NM = 1852.0
MIN_SEG_SEC = 300  # 短于 5 分钟的分段并入相邻段，避免状态抖动


def _classify(sog: float | None) -> str:
    if sog is None:
        return "unknown"
    if sog < 0.5:
        return "anchored"
    if sog < 4.5:
        return "slow"
    if sog < 12.0:
        return "transit"
    return "fast"


def _pts_with_speed(track: list[dict]) -> list[dict]:
    """补齐速度：优先用报文 SOG，缺失时用相邻点位移推算。"""
    out: list[dict] = []
    for i, p in enumerate(track):
        sog = p.get("sog")
        if sog is None and i > 0:
            q = track[i - 1]
            dt = p["ts"] - q["ts"]
            if dt > 0:
                d = haversine_m(q["lat"], q["lon"], p["lat"], p["lon"])
                sog = d / dt * 1.94384  # m/s → kn
                if sog > 60:  # 明显是跳点/位置错误
                    sog = None
        q = dict(p)
        q["sog_eff"] = round(sog, 2) if sog is not None else None
        out.append(q)
    return out


def _build_segments(pts: list[dict], fishing_events: list[dict]) -> list[dict]:
    if not pts:
        return []
    raw: list[dict] = []
    for p in pts:
        st = _classify(p["sog_eff"])
        if raw and raw[-1]["state"] == st:
            raw[-1]["pts"].append(p)
        else:
            raw.append({"state": st, "pts": [p]})

    def finish(seg: dict) -> dict:
        ps = seg["pts"]
        dist = 0.0
        for a, b in zip(ps, ps[1:]):
            dist += haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
        speeds = [p["sog_eff"] for p in ps if p["sog_eff"] is not None]
        start, end = ps[0]["ts"], ps[-1]["ts"]
        mid = ps[len(ps) // 2]
        overlapping = [
            e for e in fishing_events
            if e["start_ts"] <= end and (e["end_ts"] or end) >= start
        ]
        return {
            "state": seg["state"],
            "label": STATE_LABEL[seg["state"]],
            "points": len(ps),
            "start_ts": start,
            "end_ts": end,
            "duration_min": round((end - start) / 60.0, 1),
            "distance_nm": round(dist / M_PER_NM, 2),
            "avg_sog": round(sum(speeds) / len(speeds), 1) if speeds else None,
            "lat": round(mid["lat"], 4),
            "lon": round(mid["lon"], 4),
            "fishing": bool(overlapping),
            "confidence": max((e["confidence"] or 0) for e in overlapping) if overlapping else None,
        }

    segs = [finish(s) for s in raw]
    # 合并过短分段到相邻段（保持时间连续）
    merged: list[dict] = []
    for s in segs:
        if merged and (s["end_ts"] - s["start_ts"]) < MIN_SEG_SEC and merged[-1]["state"] != "unknown":
            prev = merged[-1]
            prev["end_ts"] = s["end_ts"]
            prev["duration_min"] = round((prev["end_ts"] - prev["start_ts"]) / 60.0, 1)
            prev["distance_nm"] = round(prev["distance_nm"] + s["distance_nm"], 2)
            prev["fishing"] = prev["fishing"] or s["fishing"]
        else:
            merged.append(dict(s))
    for s in merged:
        s.pop("_absorbed", None)
    # 同一状态被极短的缺报隔开时合并，避免出现两行相同状态的碎片
    coalesced: list[dict] = []
    for s in merged:
        if (
            coalesced
            and coalesced[-1]["state"] == s["state"]
            and (s["start_ts"] - coalesced[-1]["end_ts"]) <= 600
        ):
            prev = coalesced[-1]
            prev["end_ts"] = s["end_ts"]
            prev["duration_min"] = round((prev["end_ts"] - prev["start_ts"]) / 60.0, 1)
            prev["distance_nm"] = round(prev["distance_nm"] + s["distance_nm"], 2)
            prev["fishing"] = prev["fishing"] or s["fishing"]
        else:
            coalesced.append(dict(s))
    # 单点碎片（时长 0）并入下一段，避免时间轴出现 "0 分钟" 条目
    clean: list[dict] = []
    for i, seg in enumerate(coalesced):
        if seg["points"] <= 1 and i + 1 < len(coalesced):
            nxt = coalesced[i + 1]
            nxt["start_ts"] = min(nxt["start_ts"], seg["start_ts"])
            nxt["duration_min"] = round((nxt["end_ts"] - nxt["start_ts"]) / 60.0, 1)
            continue
        clean.append(seg)
    return clean


def _hourly(pts: list[dict]) -> list[dict]:
    buckets: dict[int, dict] = {}
    for p in pts:
        h = p["ts"] // 3600 * 3600
        b = buckets.setdefault(h, {"hour_ts": h, "speeds": [], "dist": 0.0, "n": 0})
        if p["sog_eff"] is not None:
            b["speeds"].append(p["sog_eff"])
        b["n"] += 1
    ordered = sorted(buckets.values(), key=lambda x: x["hour_ts"])
    out = []
    for b in ordered:
        avg = sum(b["speeds"]) / len(b["speeds"]) if b["speeds"] else None
        out.append({
            "hour_ts": b["hour_ts"],
            "avg_sog": round(avg, 1) if avg is not None else None,
            "max_sog": round(max(b["speeds"]), 1) if b["speeds"] else None,
            "points": b["n"],
            "state": _classify(avg),
        })
    return out


def vessel_behavior(mmsi: int, hours: int = 24) -> dict:
    hours = max(1, min(hours, 168))
    track = vs.vessel_track(int(mmsi), hours=hours, max_points=8000)
    info = vs.vessel_detail(int(mmsi))
    pts0 = track["track"]
    if not pts0:
        return {
            "mmsi": mmsi,
            "found": bool(info),
            "points": 0,
            "message": (
                "该船在所选时间窗口内没有已存储的轨迹点（可能未被 AIS 接收，或轨迹落库间隔未满足）。"
            ),
        }

    pts = _pts_with_speed(pts0)
    segs = _build_segments(pts, track["fishing_events"])
    hourly = _hourly(pts)
    now = int(time.time())

    # ---- 统计 ----
    dist = 0.0
    for a, b in zip(pts, pts[1:]):
        dist += haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    displacement = haversine_m(pts[0]["lat"], pts[0]["lon"], pts[-1]["lat"], pts[-1]["lon"])
    speeds = [p["sog_eff"] for p in pts if p["sog_eff"] is not None]
    moving = [s for s in speeds if s >= 0.5]

    span = max(1, pts[-1]["ts"] - pts[0]["ts"])
    time_by_state = {"anchored": 0.0, "slow": 0.0, "transit": 0.0, "fast": 0.0, "unknown": 0.0}
    for a, b in zip(pts, pts[1:]):
        dt = max(0, b["ts"] - a["ts"])
        if dt > 3 * 3600:  # 中间断档不算进状态时长
            continue
        time_by_state[_classify(a["sog_eff"])] += dt

    clat = sum(p["lat"] for p in pts) / len(pts)
    clon = sum(p["lon"] for p in pts) / len(pts)
    radius = max(haversine_m(clat, clon, p["lat"], p["lon"]) for p in pts) / M_PER_NM

    # 最近报告间隔（判断数据新鲜度）
    freshness = now - pts[-1]["ts"]

    # ---- 当前状态 ----
    tail = [p for p in pts[-4:] if p["sog_eff"] is not None]
    cur_sog = sum(p["sog_eff"] for p in tail) / len(tail) if tail else None
    cur_state = _classify(cur_sog)
    cur_since = None
    for s in reversed(segs):
        if s["state"] == cur_state:
            cur_since = s["start_ts"]
            break

    # ---- 趋势：最近 3h vs 前 3h ----
    cut = now - 3 * 3600
    recent = [p["sog_eff"] for p in pts if p["ts"] >= cut and p["sog_eff"] is not None]
    prev = [p["sog_eff"] for p in pts if cut - 3 * 3600 <= p["ts"] < cut and p["sog_eff"] is not None]
    r_avg = sum(recent) / len(recent) if recent else None
    p_avg = sum(prev) / len(prev) if prev else None
    if r_avg is not None and p_avg is not None:
        delta = r_avg - p_avg
        verdict = "明显加速" if delta > 2 else "略有加速" if delta > 0.6 else "明显减速" if delta < -2 else "略有减速" if delta < -0.6 else "基本平稳"
    else:
        delta, verdict = None, "对比数据不足"

    gaps = track["gap_events"]
    longest_gap = max((g["duration_s"] or 0) for g in gaps) if gaps else 0

    name = (info or {}).get("ship_name")
    identity = (info or {}).get("identity", "unknown")
    narrative = _narrative(
        name=name, mmsi=mmsi, hours=hours, points=len(pts), cur_state=cur_state,
        cur_sog=cur_sog, dist_nm=dist / M_PER_NM, span_h=span / 3600,
        slow_h=time_by_state["slow"] / 3600, fish_n=len(track["fishing_events"]),
        gap_n=len(gaps), gap_min=longest_gap / 60, verdict=verdict, delta=delta,
        freshness_min=freshness / 60,
        straightness=(displacement / dist if dist > 1 else None),
    )

    return {
        "mmsi": mmsi,
        "found": True,
        "name": name,
        "identity": identity,
        "ship_type_text": (info or {}).get("ship_type_text"),
        "hours": hours,
        "points": len(pts),
        "first_ts": pts[0]["ts"],
        "last_ts": pts[-1]["ts"],
        "span_hours": round(span / 3600, 1),
        "freshness_min": round(freshness / 60, 1),
        "current": {
            "state": cur_state,
            "label": STATE_LABEL[cur_state],
            "sog": round(cur_sog, 1) if cur_sog is not None else None,
            "since_ts": cur_since,
            "lat": pts[-1]["lat"],
            "lon": pts[-1]["lon"],
        },
        "stats": {
            "distance_nm": round(dist / M_PER_NM, 1),
            "displacement_nm": round(displacement / M_PER_NM, 1),
            "straightness": (
                round(displacement / dist, 2) if dist > 1 else None
            ),
            "avg_sog": round(sum(moving) / len(moving), 1) if moving else None,
            "max_sog": round(max(speeds), 1) if speeds else None,
            "radius_nm": round(radius, 1),
            "anchored_h": round(time_by_state["anchored"] / 3600, 1),
            "slow_h": round(time_by_state["slow"] / 3600, 1),
            "transit_h": round(time_by_state["transit"] / 3600, 1),
            "fast_h": round(time_by_state["fast"] / 3600, 1),
            "longest_gap_min": round(longest_gap / 60, 1) if longest_gap else 0,
        },
        "trend": {
            "verdict": verdict,
            "last3h_avg_sog": round(r_avg, 1) if r_avg is not None else None,
            "prev3h_avg_sog": round(p_avg, 1) if p_avg is not None else None,
            "delta_sog": round(delta, 1) if delta is not None else None,
        },
        "segments": segs[-14:],
        "hourly": hourly,
        "fishing_events": track["fishing_events"],
        "gap_events": gaps,
        "narrative": narrative,
        "caveat": (
            "状态由报文速度分段推断：低速不等于正在捕捞（可能是漂流、锚泊、避风或等待），"
            "高速也不等于运输。轨迹为降采样存储，里程与时长存在平滑误差。"
        ),
    }


def _narrative(**k) -> str:
    label = STATE_LABEL[k["cur_state"]]
    parts = [
        f"MMSI {k['mmsi']}{('（' + k['name'] + '）') if k['name'] else ''}"
        f"近 {k['hours']} 小时共 {k['points']} 个轨迹点、航迹 {k['dist_nm']:.1f} 海里"
        f"（跨度 {k['span_h']:.1f} 小时）。"
    ]
    sog_txt = f"当前航速 {k['cur_sog']:.1f} kn，" if k["cur_sog"] is not None else ""
    parts.append(f"最近状态为【{label}】，{sog_txt}低速活动累计 {k['slow_h']:.1f} 小时。")
    if k["fish_n"]:
        parts.append(f"窗口内有 {k['fish_n']} 段疑似捕捞（规则推断，需结合船舶登记核实）。")
    if k["gap_n"]:
        parts.append(f"出现 {k['gap_n']} 次 AIS 信号中断，最长约 {k['gap_min']:.0f} 分钟（中断 ≠ 关闭 AIS）。")
    if k["verdict"] and k["delta"] is not None:
        parts.append(f"与前三小时相比速度{k['verdict']}（{k['delta']:+.1f} kn）。")
    if k["straightness"] is not None and k["straightness"] < 0.15 and k["dist_nm"] > 20:
        parts.append("航迹高度回旋、直线度很低，符合在固定渔区往复作业的形态。")
    if k["freshness_min"] > 60:
        parts.append(f"注意：最后一条位置已在 {k['freshness_min']:.0f} 分钟前，结论时效性受限。")
    return "".join(parts)
