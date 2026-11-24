"""证据卡（Evidence Card）：让每个结论都能被追问、被反驳。

设计目标
--------
系统里每个「事件」都不应该只有一句结论，而应该是一张可以被审阅的卡片：
- 观察到什么（可复核的事实）
- 还有什么解释（替代假设）
- 哪些部分解释不了（明确的不确定性）
- 数据本身可不可靠
- 谁、以什么状态复核过

智能体引用事件时必须引用这张卡里的字段，而不是自由发挥。
"""
from __future__ import annotations

import json
import statistics
import time
from contextlib import closing

from core.db import connect

from . import reliability as rel
from . import timeline as tl

MODEL_VERSION = "evidence-v1"

REVIEW_STATUS = {
    "unreviewed": "待核实",
    "partially_reviewed": "部分核实",
    "reviewed": "已核实",
    "unverifiable": "无法核实",
}


def _regional_history(conn, lat: float, lon: float, cell: float = 1.0) -> dict:
    """该区域历史上的接收情况：每小时报文数与船数。

    用来回答「这个格子本来接收就好不好」——如果平时就很稀疏，
    那么一次缺口更可能是覆盖问题而不是单船问题。
    """
    import math
    gy = math.floor(lat / cell) * cell
    gx = math.floor(lon / cell) * cell
    rows = conn.execute(
        """SELECT ts / 3600 AS h, COUNT(*) n, COUNT(DISTINCT mmsi) m
           FROM ais_positions
           WHERE lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
           GROUP BY h""",
        (gy, gy + cell, gx, gx + cell),
    ).fetchall()
    if not rows:
        return {"hours_observed": 0, "median_points_per_hour": None,
                "median_vessels_per_hour": None,
                "verdict": "该区域无历史记录，无法评估常规覆盖水平"}
    pts = [r["n"] for r in rows]
    ves = [r["m"] for r in rows]
    med_p = statistics.median(pts)
    return {
        "hours_observed": len(rows),
        "median_points_per_hour": round(med_p, 1),
        "median_vessels_per_hour": round(statistics.median(ves), 1),
        "verdict": (
            "该区域常规接收较密集" if med_p >= 50
            else "该区域常规接收稀疏，单船缺口更可能是覆盖问题" if med_p < 10
            else "该区域常规接收中等"
        ),
    }


def gap_evidence_card(mmsi: int, gap_start: int, gap_end: int | None = None) -> dict:
    """AIS 观测中断的证据卡。

    两种数据来源：
      1. `ais_gap_events` 里已有事件行（判据严格的离线事件）
      2. 没有事件行，但位置表里确实存在断档（时间线里的 observation_gap 就是这样
         即时算出来的）——此时按报文边界重建缺口，绝不因为「表里没记录」就否认缺口存在
    """
    mmsi = int(mmsi)
    gap_start = int(gap_start)
    with closing(connect()) as conn:
        row = conn.execute(
            """SELECT * FROM ais_gap_events
               WHERE mmsi=? AND gap_start=? LIMIT 1""",
            (mmsi, gap_start),
        ).fetchone()

        if row:
            g_end = row["gap_end"] or gap_end or int(time.time())
        else:
            prev0 = conn.execute(
                """SELECT ts, lat, lon FROM ais_positions
                   WHERE mmsi=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
                (mmsi, gap_start),
            ).fetchone()
            nxt0 = conn.execute(
                """SELECT ts, lat, lon FROM ais_positions
                   WHERE mmsi=? AND ts>? ORDER BY ts ASC LIMIT 1""",
                (mmsi, gap_start),
            ).fetchone()
            if not nxt0:
                return {"found": False, "mmsi": mmsi, "message": "未找到该中断记录"}
            # 窗口一开始就是断档时没有「中断前位置」：宁可把 lat/lon 置空，
            # 也不能拿断点后的位置冒充断点前的，否则会得出「位置连续」的假结论。
            row = {
                "gap_start": gap_start,
                "gap_end": int(gap_end or nxt0["ts"]),
                "lat": prev0["lat"] if prev0 else None,
                "lon": prev0["lon"] if prev0 else None,
            }
            g_end = row["gap_end"]

        prev = conn.execute(
            """SELECT ts, lat, lon, sog, cog FROM ais_positions
               WHERE mmsi=? AND ts<=? ORDER BY ts DESC LIMIT 1""",
            (mmsi, row["gap_start"]),
        ).fetchone()
        nxt = conn.execute(
            """SELECT ts, lat, lon, sog FROM ais_positions
               WHERE mmsi=? AND ts>=? ORDER BY ts ASC LIMIT 1""",
            (mmsi, g_end),
        ).fetchone()
        name = conn.execute(
            "SELECT ship_name FROM vessels WHERE mmsi=?", (mmsi,)
        ).fetchone()

    attr = rel.attribute_gap(
        mmsi, row["gap_start"], g_end,
        prev["lat"] if prev else row["lat"],
        prev["lon"] if prev else row["lon"],
        nxt["lat"] if nxt else None,
        nxt["lon"] if nxt else None,
    )

    with closing(connect()) as conn:
        hist = _regional_history(conn, prev["lat"], prev["lon"]) if prev else {
            "verdict": "缺少中断前位置"
        }

    dur_h = attr["duration_h"]
    down_sec = attr["upstream"].get("max_down_sec") or 0
    down_txt = "不足 1 分钟" if down_sec < 60 else f"约 {down_sec / 60:.0f} 分钟"
    evidence = [
        {
            "claim": "观测缺口",
            "value": f"{dur_h:.1f} 小时",
            "detail": "该时段内没有收到该 MMSI 的任何报文",
            "kind": "observed",
        },
        {
            "claim": "中断前最后位置与航速",
            "value": f"{prev['lat']:.3f}, {prev['lon']:.3f} / "
                     f"{(round(prev['sog'], 1) if prev and prev['sog'] is not None else '—')} kn",
            "detail": "缺口开始前的最后一个有效报文",
            "kind": "observed",
        } if prev else {
            "claim": "中断前最后位置",
            "value": None,
            "detail": "缺口前没有可用报文，无法判断起点状态",
            "kind": "missing",
        },
        {
            "claim": "上游链路状态",
            "value": attr["upstream"]["verdict"],
            "detail": (
                f"在线比例 {attr['upstream'].get('ok_ratio')}，"
                f"最长连续断连{down_txt}"
            ),
            "kind": "observed",
        },
        {
            "claim": "周边船舶同期表现",
            "value": attr["neighborhood"]["verdict"],
            "detail": (
                f"同网格内 {attr['neighborhood'].get('neighbor_vessels', 0)} 艘其他船有报文"
            ),
            "kind": "observed",
        },
        {
            "claim": "该区域历史接收水平",
            "value": hist.get("verdict"),
            "detail": (
                f"历史每小时中位 {hist['median_points_per_hour']} 条报文"
                if hist.get("median_points_per_hour") is not None
                else "该区域历史报文太少，算不出接收基线"
            ),
            "kind": "inferred",
        },
    ]

    if attr.get("position_plausibility"):
        p = attr["position_plausibility"]
        evidence.append({
            "claim": "重新出现的位置是否合理",
            "value": p["verdict"],
            "detail": f"位移 {p['drift_nm']} 海里，{dur_h:.1f} 小时内物理可达上限约 {p['max_plausible_nm']} 海里",
            "kind": "observed",
        })

    alternatives = [
        "船舶主动关闭或静默 AIS",
        "AIS 设备故障、断电或天线受损",
        "卫星/岸基接收覆盖在该海域该时段不足",
        "我们自己的上游数据链路中断",
        "船舶更换 MMSI 或存在 MMSI 冲突",
        "地形遮蔽或恶劣海况导致信号丢失",
    ]

    unexplained = []
    if attr["attribution"] == "vessel_side_likely":
        unexplained.append("无法从 AIS 数据区分：主动关闭、设备故障、断电、遮蔽")
    if attr["attribution"] == "coverage_unclear":
        unexplained.append("无法区分区域覆盖不足与船舶侧原因")
    if not prev:
        unexplained.append("缺口前无报文，起点状态未知")
    if not nxt:
        unexplained.append("缺口后尚未恢复报文，事件仍未闭合")
    if hist.get("median_points_per_hour") is None:
        unexplained.append("缺少该区域历史接收基线")

    review = "unverifiable" if attr["attribution"] == "coverage_unclear" else "unreviewed"

    return {
        "found": True,
        "card_id": f"gap-{mmsi}-{int(gap_start)}",
        "event_type": "ais_gap",
        "mmsi": mmsi,
        "ship_name": name["ship_name"] if name else None,
        "start_ts": int(row["gap_start"]),
        "end_ts": int(g_end),
        "duration_h": dur_h,
        "headline": f"AIS 观测缺口 {dur_h:.1f} 小时",
        "status": REVIEW_STATUS[review],
        "review_status": review,
        "attribution": attr["attribution"],
        "attribution_reason": attr["reason"],
        "evidence": [e for e in evidence if e],
        "alternative_explanations": alternatives,
        "unexplained": unexplained,
        "data_quality": {
            "upstream_ok_ratio": attr["upstream"].get("ok_ratio"),
            "upstream_max_down_sec": attr["upstream"].get("max_down_sec"),
            "neighbors_present": attr["neighborhood"].get("neighbor_vessels", 0),
            "regional_baseline": hist.get("median_points_per_hour"),
        },
        "model_version": MODEL_VERSION,
        "caveat": (
            "本卡片只陈述可复核的观测事实与统计对比，不对中断原因下结论。"
            "「更可能来自船舶侧」仍不等于「故意关闭 AIS」。"
        ),
    }


def fishing_evidence_card(mmsi: int, start_ts: int) -> dict:
    """疑似捕捞候选的证据卡。"""
    mmsi = int(mmsi)
    with closing(connect()) as conn:
        row = conn.execute(
            "SELECT * FROM fishing_events WHERE mmsi=? AND start_ts=? LIMIT 1",
            (mmsi, int(start_ts)),
        ).fetchone()
        if not row:
            return {"found": False, "mmsi": mmsi, "message": "未找到该疑似捕捞片段"}

        ev = json.loads(row["evidence"]) if row["evidence"] else {}
        info = conn.execute(
            "SELECT ship_name, ship_type_text, fishing_identity, length FROM vessels WHERE mmsi=?",
            (mmsi,),
        ).fetchone()
        # 该船历史同类片段数（用于说明这不是偶发）
        hist_n = conn.execute(
            "SELECT COUNT(*) c FROM fishing_events WHERE mmsi=?", (mmsi,)
        ).fetchone()["c"]
        region = _regional_history(conn, row["lat"], row["lon"]) if row["lat"] else {}
        # 同一时段周边有多少船也在低速（群体性作业 vs 单船异常）
        peer = conn.execute(
            """SELECT COUNT(DISTINCT mmsi) m FROM ais_positions
               WHERE ts BETWEEN ? AND ? AND lat BETWEEN ? AND ? AND lon BETWEEN ? AND ?
                 AND sog BETWEEN 0.4 AND 6.0 AND mmsi != ?""",
            (row["start_ts"], row["end_ts"], row["lat"] - 0.5, row["lat"] + 0.5,
             row["lon"] - 0.5, row["lon"] + 0.5, mmsi),
        ).fetchone()["m"]

    dur_h = (row["duration_s"] or 0) / 3600
    evidence = [
        {"claim": "持续时间", "value": f"{dur_h:.1f} 小时", "detail": ev.get("duration_min"),
         "kind": "observed"},
        {"claim": "平均航速", "value": f"{ev.get('avg_sog')} 节",
         "detail": f"速度变异系数 {ev.get('sog_cv')}", "kind": "observed"},
        {"claim": "航迹形态", "value": f"直线度 {ev.get('straightness')}",
         "detail": "直线度越低说明越像在固定渔区往复作业", "kind": "inferred"},
        {"claim": "转向活动", "value": f"{ev.get('turn_rate_deg_min')} 度/分钟",
         "detail": "作业通常伴随持续小幅转向", "kind": "inferred"},
        {"claim": "同期周边低速船数", "value": f"{peer} 艘",
         "detail": "同一时段同一网格内还有多少船处于低速状态", "kind": "observed"},
        {"claim": "该船历史疑似片段", "value": f"{hist_n} 段",
         "detail": "仅统计本系统已存储的事件", "kind": "observed"},
    ]

    alternatives = [
        "正常捕捞作业（本卡片无法确认渔具和渔法）",
        "漂流等待或休整，未在作业",
        "锚泊但 AIS 速度报告有偏差",
        "避风、避让或等待进港",
        "低速航行通过渔场（赶路但速度低）",
        "养殖区、定置网等固定设施附近活动",
    ]
    unexplained = [
        "无法确认渔具类型（缺少渔具登记与作业特征库）",
        "无法确认是否实际下网/起网",
        "规则得分未经标注数据校准，不能解读为概率",
    ]
    if peer == 0:
        unexplained.append("周边没有其他低速船，无法用群体行为佐证")

    return {
        "found": True,
        "card_id": f"fishing-{mmsi}-{int(start_ts)}",
        "event_type": "fishing_candidate",
        "mmsi": mmsi,
        "ship_name": info["ship_name"] if info else None,
        "ship_type_text": info["ship_type_text"] if info else None,
        "identity": info["fishing_identity"] if info else None,
        "start_ts": int(row["start_ts"]),
        "end_ts": int(row["end_ts"]),
        "duration_h": round(dur_h, 2),
        "lat": row["lat"],
        "lon": row["lon"],
        "headline": f"疑似捕捞候选 {dur_h:.1f} 小时",
        "status": REVIEW_STATUS["unreviewed"],
        "review_status": "unreviewed",
        "rule_score": row["confidence"],   # 沿用旧字段名，语义是未校准规则分
        "confidence": None,                 # 明确：没有校准过的概率
        "confidence_status": "uncalibrated",
        "evidence": [e for e in evidence if e["value"] is not None],
        "alternative_explanations": alternatives,
        "unexplained": unexplained,
        "data_quality": {"regional_baseline": region.get("median_points_per_hour")},
        "model_version": MODEL_VERSION,
        "caveat": (
            "「疑似捕捞候选」是规则从速度与航迹形态推断的结果，不是确认的捕捞行为，"
            "也不能作为执法依据。rule_score 未经概率校准，不代表发生概率。"
        ),
    }


def gap_cards(mmsi: int, hours: int = 24, limit: int = 10) -> list[dict]:
    """批量取出某船最近的若干中断证据卡。

    优先用 `ais_gap_events`；没有事件行时回退到行为时间线的 observation_gap 片段。
    否则会出现「时间线明明画着一段中断，证据卡却说没有」的自相矛盾。
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT gap_start FROM ais_gap_events
               WHERE mmsi=? AND gap_start>=? ORDER BY gap_start DESC LIMIT ?""",
            (int(mmsi), int(time.time()) - hours * 3600, limit),
        ).fetchall()
        if rows:
            return [gap_evidence_card(mmsi, r["gap_start"]) for r in rows]

    from . import timeline as tl  # 局部导入，避免模块级循环依赖

    try:
        t = tl.behavior_timeline(int(mmsi), hours=hours, persist=False)
    except Exception:
        return []

    cards: list[dict] = []
    for seg in (t.get("segments") or []):
        if seg.get("activity") != "observation_gap":
            continue
        card = gap_evidence_card(mmsi, seg["start_ts"], seg.get("end_ts"))
        if card.get("found"):
            cards.append(card)
        if len(cards) >= limit:
            break
    return cards


def fishing_cards(mmsi: int, hours: int = 24, limit: int = 10) -> list[dict]:
    """批量取出某船最近的疑似捕捞证据卡。

    只认 `fishing_events` 里的片段：没有记录就是没有候选，不从速度曲线临时造一个，
    否则「证据」会变成「随手凑的解释」。
    """
    with closing(connect()) as conn:
        rows = conn.execute(
            """SELECT start_ts FROM fishing_events
               WHERE mmsi=? AND start_ts>=? ORDER BY start_ts DESC LIMIT ?""",
            (int(mmsi), int(time.time()) - hours * 3600, limit),
        ).fetchall()
    cards = []
    for r in rows:
        c = fishing_evidence_card(mmsi, r["start_ts"])
        if c.get("found"):
            cards.append(c)
    return cards
