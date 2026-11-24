"""数据可靠性引擎：判断「这条结论能不能采信」。

AIS 中断最容易被误读成「船舶故意关闭 AIS」。真正要回答的是：
这条中断到底该归因于船舶，还是归因于我们自己的链路、卫星覆盖或接收站分布？

本模块给出四类证据
------------------
1. 时间行为：报文间隔分布、覆盖率、最长缺口
2. 上游链路：同一时段 AISStream 是否持续在线、消息速率是否异常
3. 周边对比：同网格内其他船在同一时段是否也在断（区分「区域无覆盖」和「单船异常」）
4. 位置合理性：重新出现的位置是否在物理可达范围内

归因结论只用三档，且**永远保留不确定性**：
- 无法归因（链路侧问题明确）
- 疑似覆盖不足（周边同期同样无数据）
- 更可能来自船舶侧（链路与周边均正常）—— 仍不能确定具体原因
"""
from __future__ import annotations

import math
import statistics
import time
from contextlib import closing

from core.db import connect
from core.geo import haversine_m

from . import timeline as tl

MODEL_VERSION = "reliability-v1"
M_PER_NM = 1852.0
MAX_PLAUSIBLE_SOG = 25.0     # 渔船物理可达上限（节），用于判断跳点
NEIGHBOR_CELL = 1.0          # 周边对比网格边长（度）


def _upstream(conn, t0: int, t1: int) -> dict:
    """上游 AIS 流在 [t0,t1] 的健康状况。

    只看「在线比例」会误判：如果链路在 1 小时里断了 5 分钟，online 比例会很难看，
    但那 5 分钟解释不了整整 1 小时的船舶缺口。所以这里额外算「最长连续断连时长」，
    用它去和缺口时长比较，才是有意义的归因依据。
    """
    rows = conn.execute(
        """SELECT ts, connected, rate_1m FROM stream_health
           WHERE ts BETWEEN ? AND ? ORDER BY ts""",
        (t0, t1),
    ).fetchall()
    if not rows:
        return {
            "samples": 0, "ok_ratio": None, "max_down_sec": None,
            "verdict": "无上游健康记录，无法判断",
        }

    ok = sum(1 for r in rows if r["connected"])
    rates = [r["rate_1m"] for r in rows if r["rate_1m"] is not None]
    med = statistics.median(rates) if rates else None

    # 采样步长（健康记录默认 15 秒一条，这里按实际间隔推算）
    if len(rows) > 1:
        step = statistics.median([b["ts"] - a["ts"] for a, b in zip(rows, rows[1:])]) or 15
    else:
        step = (t1 - t0) or 15

    max_down = cur = 0
    for r in rows:
        if r["connected"]:
            cur = 0
        else:
            cur += 1
            max_down = max(max_down, cur)
    max_down_sec = int(max_down * step)

    return {
        "samples": len(rows),
        "ok_ratio": round(ok / len(rows), 3),
        "median_rate_1m": round(med, 1) if med is not None else None,
        "max_down_sec": max_down_sec,
        "verdict": (
            # 关键区分：偶尔掉线但很快重连，和真的断了一大段，是两回事
            "上游链路正常（有短暂抖动）" if ok / len(rows) >= 0.9 or max_down_sec < 120
            else "上游链路有明显中断" if ok / len(rows) < 0.5
            else "上游链路间歇性中断"
        ),
    }


def _neighborhood(conn, mmsi: int, lat: float, lon: float, t0: int, t1: int) -> dict:
    """同一网格内其他船在中断时段是否有报文。

    有人报 → 该区域当时有接收覆盖，中断更可能是单船问题；
    没人报 → 无法排除区域覆盖不足。
    """
    gy = math.floor(lat / NEIGHBOR_CELL) * NEIGHBOR_CELL
    gx = math.floor(lon / NEIGHBOR_CELL) * NEIGHBOR_CELL
    row = conn.execute(
        """SELECT COUNT(*) n, COUNT(DISTINCT mmsi) m FROM ais_positions
           WHERE ts BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
             AND mmsi != ?""",
        (t0, t1, gy, gy + NEIGHBOR_CELL, gx, gx + NEIGHBOR_CELL, mmsi),
    ).fetchone()
    return {
        "cell": [round(gy, 2), round(gx, 2), round(gy + NEIGHBOR_CELL, 2), round(gx + NEIGHBOR_CELL, 2)],
        "neighbor_points": row["n"] or 0,
        "neighbor_vessels": row["m"] or 0,
        "verdict": (
            "周边船舶同期有报文，区域存在接收覆盖"
            if (row["m"] or 0) >= 3
            else "周边船舶同期报文很少，无法排除区域覆盖不足"
        ),
    }


def attribute_gap(mmsi: int, gap_start: int, gap_end: int,
                  lat: float | None, lon: float | None,
                  lat2: float | None = None, lon2: float | None = None) -> dict:
    """对一次中断做归因，返回证据而不是结论。"""
    t0, t1 = int(gap_start), int(gap_end)
    dur_h = (t1 - t0) / 3600

    with closing(connect()) as conn:
        up = _upstream(conn, t0, t1)
        nb = _neighborhood(conn, mmsi, lat, lon, t0, t1) if lat is not None and lon is not None else {
            "verdict": "缺少中断前位置，无法做周边对比", "neighbor_points": 0, "neighbor_vessels": 0
        }
        # 中断前的最后速度
        prev = conn.execute(
            "SELECT sog, ts FROM ais_positions WHERE mmsi=? AND ts<=? ORDER BY ts DESC LIMIT 1",
            (mmsi, t0),
        ).fetchone()
        nxt = conn.execute(
            "SELECT sog, ts, lat, lon FROM ais_positions WHERE mmsi=? AND ts>=? ORDER BY ts ASC LIMIT 1",
            (mmsi, t1),
        ).fetchone()

    # 位置合理性：按 25 节上限推算最远可达距离
    plausibility = None
    jump_nm = None
    if lat is not None and lon is not None and lat2 is not None and lon2 is not None:
        jump_nm = haversine_m(lat, lon, lat2, lon2) / M_PER_NM
        max_nm = MAX_PLAUSIBLE_SOG * dur_h
        plausibility = {
            "drift_nm": round(jump_nm, 1),
            "max_plausible_nm": round(max_nm, 1),
            "verdict": (
                "位置连续，无跳点" if jump_nm <= max_nm
                else "位置跳变超出物理可达范围，需核查是否换船或位置错误"
            ),
        }

    # 归因
    gap_sec = max(1, t1 - t0)
    max_down = up.get("max_down_sec") or 0
    upstream_explains = max_down >= 0.5 * gap_sec
    upstream_partial = (up.get("ok_ratio") or 1) < 0.9
    neighbors_silent = (nb.get("neighbor_vessels") or 0) < 3

    if upstream_explains:
        attribution = "unattributable"
        reason = (
            f"上游链路在该时段曾连续中断约 {max_down / 60:.0f} 分钟，"
            f"足以解释这次的 {gap_sec / 60:.0f} 分钟缺口，不能归因于船舶。"
        )
        confidence_note = "链路侧原因已足以解释该缺口。"
    elif upstream_partial and neighbors_silent:
        attribution = "coverage_unclear"
        reason = (
            "上游链路有间断且周边船舶同期也几乎没有报文，"
            "既可能是区域接收覆盖不足，也可能是链路侧原因。"
        )
        confidence_note = "现有数据无法区分覆盖不足与船舶侧原因。"
    elif neighbors_silent:
        attribution = "coverage_unclear"
        reason = "周边船舶同期也几乎没有报文，可能是区域接收覆盖不足。"
        confidence_note = "现有数据无法区分覆盖不足与船舶侧原因。"
    else:
        attribution = "vessel_side_likely"
        reason = (
            "上游链路在缺口时段未出现足以解释该缺口的连续中断，"
            "且周边船舶同期有报文，缺口更可能来自船舶侧。"
        )
        confidence_note = "但关闭 AIS、天线故障、断电、遮蔽等具体原因无法从 AIS 数据本身区分。"

    return {
        "mmsi": int(mmsi),
        "gap_start": t0,
        "gap_end": t1,
        "duration_h": round(dur_h, 2),
        "last_sog": round(prev["sog"], 1) if prev and prev["sog"] is not None else None,
        "upstream": up,
        "neighborhood": nb,
        "position_plausibility": plausibility,
        "attribution": attribution,
        "reason": reason,
        "confidence_note": confidence_note,
        "after_gap_sog": round(nxt["sog"], 1) if nxt and nxt["sog"] is not None else None,
        "model_version": MODEL_VERSION,
    }


def observation_quality(mmsi: int, hours: int = 24, persist: bool = True) -> dict:
    """单船在窗口内的整体观测质量。"""
    mmsi = int(mmsi)
    hours = max(1, min(int(hours), 168))
    tline = tl.behavior_timeline(mmsi, hours=hours, persist=False)
    now = int(time.time())
    window = tline["window"]

    if not tline.get("points"):
        return {
            "mmsi": mmsi, "found": tline.get("found", False), "hours": hours,
            "grade": "no_data", "coverage_ratio": 0.0,
            "message": "窗口内没有该船的任何报文，无法评估观测质量。无数据本身不等于异常。",
            "model_version": MODEL_VERSION,
        }

    segs = tline["segments"]
    real = [s for s in segs if s["activity"] != "observation_gap"]
    gaps = [s for s in segs if s["activity"] == "observation_gap"]
    s = tline["summary"]

    with closing(connect()) as conn:
        up = _upstream(conn, window["start"], window["end"])
        # 该船自己同期在其它海区的报文？不适用。改为全局同期接收量是否异常
        med_interval = s.get("median_interval_s")
        pts_n = tline["points"]

    coverage = s.get("coverage_ratio") or 0
    longest_gap_h = round(max((g["duration_s"] for g in gaps), default=0) / 3600, 2)
    window_sec = max(1, window["end"] - window["start"])
    up_max_down = up.get("max_down_sec") or 0

    flags: list[str] = []
    if coverage < 0.5:
        flags.append("low_coverage")
    if longest_gap_h >= 2:
        flags.append("long_gap")
    if med_interval and med_interval > 600:
        flags.append("sparse_reporting")
    # 只有连续断连占窗口一成以上，才值得标记为链路问题
    if up_max_down >= 0.1 * window_sec:
        flags.append("upstream_unstable")
    poor_segs = sum(1 for x in real if x["reliability"]["grade"] == "poor")
    if poor_segs:
        flags.append("poor_segments")

    if "upstream_unstable" in flags or coverage < 0.35:
        grade = "poor"
    elif len(flags) >= 2 or coverage < 0.7:
        grade = "fair"
    else:
        grade = "good"

    advice = {
        "good": "观测连续性良好，行为时间线的分段结果可直接参考。",
        "fair": "观测存在缺口，时间线中的低速/静止判读可能被缺口打断，建议结合缺口位置一起看。",
        "poor": "观测质量较差，行为分段不足以支撑强结论；请优先排查数据链路与覆盖。",
    }[grade]

    result = {
        "mmsi": mmsi,
        "found": True,
        "name": tline.get("name"),
        "hours": hours,
        "window": window,
        "grade": grade,
        "flags": flags,
        "coverage_ratio": coverage,
        "observed_hours": s.get("observed_hours"),
        "gap_hours": s.get("gap_hours"),
        "gap_count": len(gaps),
        "longest_gap_h": longest_gap_h,
        "median_report_interval_s": med_interval,
        "points": pts_n,
        "upstream": up,
        "advice": advice,
        "model_version": MODEL_VERSION,
        "caveat": (
            "覆盖率是「实观测时长 / 窗口长度」的代理指标，不是真实接收概率。"
            "报文间隔受船舶报告频率设置影响（ Class A 航行中通常 2–10 秒，锚泊 3 分钟），"
            "不同船舶之间不可直接比较。"
        ),
    }

    if persist:
        try:
            with closing(connect()) as conn:
                conn.execute(
                    """INSERT OR REPLACE INTO observation_quality
                       (q_id, mmsi, window_start, window_end, n_points, median_interval_s,
                        coverage_ratio, max_gap_s, n_gaps, upstream_ok_ratio,
                        neighborhood_ok_ratio, grade, flags, created_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (f"{mmsi}-{window['end']}-{MODEL_VERSION}", mmsi, window["start"], window["end"],
                     pts_n, med_interval, coverage, int(longest_gap_h * 3600), len(gaps),
                     up.get("ok_ratio"), None, grade,
                     __import__("json").dumps(flags, ensure_ascii=False), now),
                )
                conn.commit()
        except Exception:
            pass
    return result
