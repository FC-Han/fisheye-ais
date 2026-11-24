"""行为时间线引擎（Behavior Engine v1，规则层）。

目标：把「一条船的 AIS 轨迹点」翻译成「时间上连续、互不重叠、覆盖整个查询窗口
的行为片段序列」，并且把不确定性明明白白写进每一段里。

核心原则
--------
1. **两层标签**：
   - 第一层 activity：可直接从 AIS 观察或相对可靠推断的状态
     transit / low_speed / stationary / observation_gap / unknown
   - 第二层 sub_activity：需要渔具、轨迹形态和上下文支持的行为候选
     v1 只输出 fishing_candidate，绝不细分到拖网/围网/延绳钓。
2. **不假装有概率**：confidence 恒为 None。规则得分放在 evidence.rule_score，
   命名上明确它「未经校准、不是概率」。只有拿到独立标注数据做过校准后，
   才允许把某个数字叫作 confidence。
3. **缺口是数据不是噪声**：相邻点间隔过大时，时间线里插入 observation_gap 段，
   而不是插值抹平。船长 4 小时没报文，时间线上就必须显示 4 小时空白。
4. **港/锚地分开但都只是候选**：静止段用 NGA World Port Index 的港口参考点判断，
   5 km 内记 `port_stay`，否则记 `anchorage_candidate`。WPI 没有泊位边界、
   也不保证收录所有小型渔码头，所以两者都只作为候选输出，不写成结论。
5. **可复现**：同一 model_version + 同一输入 → 同一输出，片段 id 稳定。
"""
from __future__ import annotations

import json
import statistics
import time
from contextlib import closing

from core.db import connect
from core.geo import angle_diff, haversine_m

from . import ports as ports_svc
from . import vessel_state as vs

MODEL_VERSION = "behavior-v1"

# --- 速度阈值（节） ---
STATIONARY_SOG = 0.5
LOW_SPEED_MAX = 4.5
TRANSIT_MAX = 12.0

# --- 时间阈值（秒） ---
GAP_FLOOR_SEC = 900          # 观测缺口下限：15 分钟
GAP_INTERVAL_FACTOR = 4      # 或 4 倍于该船的中位报文间隔，取大者
MIN_SEG_SEC = 300            # 短于此的分段并入相邻段，避免状态抖动
FISHING_MIN_SEC = 1800       # 疑似捕捞候选最短 30 分钟
EDGE_BLANK_SEC = 900         # 窗口首尾的空白超过此值才显式标缺口

M_PER_NM = 1852.0


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------
def _classify_speed(sog: float | None) -> str:
    if sog is None:
        return "unknown"
    if sog < STATIONARY_SOG:
        return "stationary"
    if sog < LOW_SPEED_MAX:
        return "low_speed"
    return "transit"


def _speed_class(sog: float | None) -> str | None:
    """更细的速度分级，仅用于证据，不参与主标签。"""
    if sog is None:
        return None
    if sog >= TRANSIT_MAX:
        return "high"
    if sog >= LOW_SPEED_MAX:
        return "cruise"
    if sog >= STATIONARY_SOG:
        return "low"
    return "idle"


def _with_effective_speed(track: list[dict]) -> list[dict]:
    """补齐速度：优先报文 SOG；缺失时用相邻点位移反推，并标出这是推断值。"""
    out: list[dict] = []
    for i, p in enumerate(track):
        sog = p.get("sog")
        inferred = False
        if sog is None and i > 0:
            q = track[i - 1]
            dt = p["ts"] - q["ts"]
            if 0 < dt <= 3600:
                d = haversine_m(q["lat"], q["lon"], p["lat"], p["lon"])
                cand = d / dt * 1.94384
                if cand <= 60:  # 超过 60 节基本是跳点或位置错误
                    sog, inferred = cand, True
        q = dict(p)
        q["sog_eff"] = round(sog, 2) if sog is not None else None
        q["sog_inferred"] = inferred
        out.append(q)
    return out


def _median_interval(pts: list[dict]) -> float:
    dts = [b["ts"] - a["ts"] for a, b in zip(pts, pts[1:]) if b["ts"] > a["ts"]]
    if not dts:
        return 0.0
    return float(statistics.median(dts))


def _gap_threshold(pts: list[dict]) -> int:
    med = _median_interval(pts)
    return int(max(GAP_FLOOR_SEC, med * GAP_INTERVAL_FACTOR))


# --------------------------------------------------------------------------
# 分段
# --------------------------------------------------------------------------
def _segment_stats(pts: list[dict]) -> dict:
    """一段轨迹点的几何与运动学特征。"""
    if not pts:
        return {}
    sogs = [p["sog_eff"] for p in pts if p["sog_eff"] is not None]
    cogs = [p.get("cog") for p in pts]
    navs = [p.get("nav_status") for p in pts if p.get("nav_status") is not None]

    path_m = 0.0
    for a, b in zip(pts, pts[1:]):
        path_m += haversine_m(a["lat"], a["lon"], b["lat"], b["lon"])
    disp_m = haversine_m(pts[0]["lat"], pts[0]["lon"], pts[-1]["lat"], pts[-1]["lon"])
    straightness = (disp_m / path_m) if path_m > 1 else 1.0

    span = max(0, pts[-1]["ts"] - pts[0]["ts"])
    turns = []
    for a, b in zip(cogs, cogs[1:]):
        if a is not None and b is not None:
            turns.append(angle_diff(a, b))
    turn_rate = (sum(turns) / len(turns)) / max(1e-6, span / 60) if turns else 0.0

    internal_gaps = [b["ts"] - a["ts"] for a, b in zip(pts, pts[1:])]
    max_internal = max(internal_gaps) if internal_gaps else 0

    return {
        "points": len(pts),
        "span_s": span,
        "median_sog": round(statistics.median(sogs), 2) if sogs else None,
        "avg_sog": round(sum(sogs) / len(sogs), 2) if sogs else None,
        "max_sog": round(max(sogs), 1) if sogs else None,
        "sog_cv": (
            round(statistics.pstdev(sogs) / (sum(sogs) / len(sogs)), 3)
            if len(sogs) > 1 and sum(sogs) > 0 else None
        ),
        "distance_nm": round(path_m / M_PER_NM, 2),
        "displacement_nm": round(disp_m / M_PER_NM, 2),
        "straightness": round(straightness, 3),
        "turn_rate_deg_min": round(turn_rate, 2),
        "max_internal_gap_s": max_internal,
        "nav_status": sorted({int(n) for n in navs}) if navs else [],
        "sog_inferred_points": sum(1 for p in pts if p.get("sog_inferred")),
    }


def _rule_score(st: dict) -> float:
    """疑似捕捞的规则得分（0-1）。

    注意：这是**未校准的规则打分**，不是概率。它只用于排序和筛选，
    不对外解释为「有 72% 的可能是捕捞」。
    """
    dur = st["span_s"] / 60.0
    avg = st["avg_sog"] or 0.0
    cv = st["sog_cv"]
    straight = st["straightness"]
    turn = st["turn_rate_deg_min"]

    s_duration = min(1.0, dur / 120.0)                       # 2 小时封顶
    s_speed = max(0.0, 1 - abs(avg - 2.8) / 3.0)             # 2.8 节附近最典型
    s_stable = max(0.0, 1 - min(1.0, (cv or 0.0) / 0.8))
    s_shape = max(0.0, min(1.0, (1 - straight) * 2.5))       # 往复作业 → 直线度低
    s_turn = max(0.0, 1 - min(1.0, abs(turn - 3.0) / 12.0))

    return round(
        0.30 * s_duration + 0.25 * s_speed + 0.18 * s_stable
        + 0.15 * s_shape + 0.12 * s_turn,
        3,
    )


def _is_fishing_candidate(st: dict, identity: str) -> bool:
    """保守的候选判定。任一强约束不满足就不标，宁可漏报不可乱报。"""
    if identity == "non_fishing":
        return False
    if st["span_s"] < FISHING_MIN_SEC:
        return False
    med = st["median_sog"]
    if med is None or not (0.4 <= med <= 6.0):
        return False
    # 必须是「在动但很慢」且不是直线赶路
    if st["straightness"] > 0.85 and st["distance_nm"] > 30:
        return False
    return _rule_score(st) >= 0.45


def _reliability(st: dict, gap_threshold: int) -> dict:
    """单段数据可靠性。用于告诉用户「这一段能不能信」。"""
    flags: list[str] = []
    grade = "good"
    if st["points"] <= 2:
        flags.append("too_few_points")
        grade = "poor"
    if st["max_internal_gap_s"] > gap_threshold:
        flags.append("internal_gap")
        grade = "poor" if st["max_internal_gap_s"] > 2 * gap_threshold else "fair"
    if st["span_s"] < MIN_SEG_SEC:
        flags.append("very_short")
        grade = "poor"
    if st["sog_inferred_points"] and st["sog_inferred_points"] * 2 >= st["points"]:
        flags.append("speed_inferred")
        grade = "fair" if grade == "good" else grade
    if not flags:
        grade = "good"
    elif grade == "good":
        grade = "fair"
    return {"grade": grade, "flags": flags}


# --------------------------------------------------------------------------
# 主流程
# --------------------------------------------------------------------------
def _raw_segments(pts: list[dict], gap_threshold: int) -> list[dict]:
    """先按「同状态 + 无跨缺口」切粗段，再在缺口处显式插空。"""
    if not pts:
        return []
    blocks: list[dict] = []
    cur: list[dict] = [pts[0]]
    for prev, p in zip(pts, pts[1:]):
        dt = p["ts"] - prev["ts"]
        if dt > gap_threshold:
            blocks.append({"kind": "obs", "pts": cur})
            blocks.append({"kind": "gap", "start": prev["ts"], "end": p["ts"]})
            cur = [p]
            continue
        if _classify_speed(p["sog_eff"]) == _classify_speed(prev["sog_eff"]):
            cur.append(p)
        else:
            blocks.append({"kind": "obs", "pts": cur})
            cur = [p]
    blocks.append({"kind": "obs", "pts": cur})
    return blocks


def _absorb_short(blocks: list[dict]) -> list[dict]:
    """把过短的可观察段并入前一段（不合并缺口段）。"""
    out: list[dict] = []
    for b in blocks:
        if b["kind"] == "obs" and b["pts"]:
            span = b["pts"][-1]["ts"] - b["pts"][0]["ts"]
            if span < MIN_SEG_SEC and out and out[-1]["kind"] == "obs":
                out[-1]["pts"].extend(b["pts"])
                continue
        out.append(b)
    return out


def _drop_zero_length(segs: list[dict]) -> list[dict]:
    """清理时长为 0 的单点段，避免时间轴上出现「0 分钟」条目。

    这类段是被缺口切出来的孤立单点，没有独立行为含义，按优先级处理：
    并入后一段的起点 → 延长前一段 → 直接丢弃。
    """
    drop: set[int] = {i for i, s in enumerate(segs)
                      if s["duration_s"] <= 0 and s["activity"] != "observation_gap"}
    out: list[dict] = []
    for i, s in enumerate(segs):
        if i in drop:
            nxt = segs[i + 1] if i + 1 < len(segs) else None
            if nxt is not None and nxt["activity"] != "observation_gap":
                nxt["start_ts"] = min(nxt["start_ts"], s["start_ts"])
                nxt["duration_s"] = int(nxt["end_ts"] - nxt["start_ts"])
                continue
            if out and out[-1]["activity"] != "observation_gap":
                out[-1]["end_ts"] = max(out[-1]["end_ts"], s["end_ts"])
                out[-1]["duration_s"] = int(out[-1]["end_ts"] - out[-1]["start_ts"])
            continue
        if out and out[-1]["activity"] == "observation_gap" and s["activity"] == "observation_gap":
            out[-1]["end_ts"] = s["end_ts"]
            out[-1]["duration_s"] = int(out[-1]["end_ts"] - out[-1]["start_ts"])
            continue
        out.append(s)
    return out


def _stitch(segs: list[dict]) -> list[dict]:
    """缝合相邻段之间的小时间缝，让时间线真正首尾相接。

    两个相邻观测段之间若只隔了「小于缺口阈值」的几秒到几分钟（就是正常的报文
    间隔），这段时间没有独立的行为含义，归入前一段保持状态延续。
    这样时间轴既不出现空洞，也不夸大观测覆盖。
    """
    for a, b in zip(segs, segs[1:]):
        if a["end_ts"] < b["start_ts"]:
            a["end_ts"] = b["start_ts"]
            a["duration_s"] = int(a["end_ts"] - a["start_ts"])
            if isinstance(a.get("evidence"), dict):
                a["evidence"]["span_s"] = a["duration_s"]
    return segs


def _build(pts: list[dict], identity: str, gap_threshold: int, window: tuple[int, int]) -> list[dict]:
    blocks = _absorb_short(_raw_segments(pts, gap_threshold))
    segs: list[dict] = []

    def push(activity, start, end, pts_, extra=None, st=None):
        # st 允许外部传入已算好的统计，避免重复计算、也避免外部对 evidence 的补充
        # （例如 rule_score、port_context）被这里重算覆盖掉。
        if st is None:
            st = _segment_stats(pts_) if pts_ else {
                "points": 0, "span_s": max(0, end - start), "median_sog": None,
                "avg_sog": None, "max_sog": None, "sog_cv": None, "distance_nm": 0.0,
                "displacement_nm": 0.0, "straightness": 1.0, "turn_rate_deg_min": 0.0,
                "max_internal_gap_s": end - start, "nav_status": [], "sog_inferred_points": 0,
            }
        seg = {
            "activity": activity,
            "start_ts": int(start),
            "end_ts": int(end),
            "duration_s": int(max(0, end - start)),
            "evidence": st,
            "sub_activity": None,
            "gear_type": None,
            "confidence": None,          # 未校准，恒为 None
            "reliability": _reliability(st, gap_threshold) if pts_ else {
                "grade": "none", "flags": ["no_observation"]
            },
        }
        if extra:
            seg.update(extra)
        segs.append(seg)

    for b in blocks:
        if b["kind"] == "gap":
            push("observation_gap", b["start"], b["end"], None)
            continue
        ps = b["pts"]
        if not ps:
            continue
        st = _segment_stats(ps)
        activity = _classify_speed(st["median_sog"])
        extra = None
        if activity == "low_speed" and _is_fishing_candidate(st, identity):
            extra = {"sub_activity": "fishing_candidate", "gear_type": "unknown"}
            st["rule_score"] = _rule_score(st)
            st["rule_version"] = MODEL_VERSION
        elif activity == "stationary":
            # 海上锚泊和在港停留的渔业意义完全不同，用 WPI 港口点位区分。
            # 取中位点而非首点，避免刚好在进港边界时被判成港内。
            mid = ps[len(ps) // 2]
            sub, ctx = ports_svc.classify_stationary(mid["lat"], mid["lon"])
            extra = {"sub_activity": sub}
            st["port_context"] = ctx
        push(activity, ps[0]["ts"], ps[-1]["ts"], ps, extra, st)

    # 窗口首尾空白：超过阈值的显式补 observation_gap，保证时间线覆盖整个窗口
    segs = _drop_zero_length(segs)
    if segs:
        w0, w1 = window
        if segs[0]["start_ts"] - w0 >= EDGE_BLANK_SEC:
            segs.insert(0, {
                "activity": "observation_gap", "start_ts": int(w0), "end_ts": segs[0]["start_ts"],
                "duration_s": int(segs[0]["start_ts"] - w0),
                "evidence": {"points": 0, "span_s": int(segs[0]["start_ts"] - w0)},
                "sub_activity": None, "gear_type": None, "confidence": None,
                "reliability": {"grade": "none", "flags": ["no_observation"]},
            })
        if w1 - segs[-1]["end_ts"] >= EDGE_BLANK_SEC:
            segs.append({
                "activity": "observation_gap", "start_ts": segs[-1]["end_ts"], "end_ts": int(w1),
                "duration_s": int(w1 - segs[-1]["end_ts"]),
                "evidence": {"points": 0, "span_s": int(w1 - segs[-1]["end_ts"])},
                "sub_activity": None, "gear_type": None, "confidence": None,
                "reliability": {"grade": "none", "flags": ["no_observation"]},
            })
    return _stitch(segs)


LABEL = {
    "transit": "航行",
    "low_speed": "低速移动",
    "stationary": "静止/近静止",
    "observation_gap": "观测中断",
    "unknown": "状态未知",
}

SUB_LABEL = {
    "fishing_candidate": "疑似捕捞候选",
    "port_stay": "港口停留候选",
    "anchorage_candidate": "锚泊/漂航候选",
}


def _narrative(mmsi: int, name: str | None, hours: int, segs: list[dict], pts: int) -> str:
    if not segs:
        return (
            f"MMSI {mmsi} 在近 {hours} 小时内没有任何已存储的 AIS 轨迹点，"
            "无法重建行为时间线。这不代表该船不存在或不活动。"
        )
    real = [s for s in segs if s["activity"] != "observation_gap"]
    gaps = [s for s in segs if s["activity"] == "observation_gap"]
    total = sum(s["duration_s"] for s in real) or 1

    parts = [f"MMSI {mmsi}{('（' + name + '）') if name else ''}近 {hours} 小时共 {pts} 个轨迹点。"]

    dist = {"transit": 0.0, "low_speed": 0.0, "stationary": 0.0, "unknown": 0.0}
    for s in real:
        dist[s["activity"]] = dist.get(s["activity"], 0.0) + s["duration_s"]
    main = max(dist, key=dist.get)
    parts.append(
        f"可观测时段共 {total / 3600:.1f} 小时，其中以【{LABEL[main]}】为主"
        f"（{dist[main] / 3600:.1f} 小时，{dist[main] / total * 100:.0f}%）。"
    )

    cands = [s for s in segs if s["sub_activity"] == "fishing_candidate"]
    if cands:
        h = sum(s["duration_s"] for s in cands) / 3600
        parts.append(f"识别出 {len(cands)} 段疑似捕捞候选，累计约 {h:.1f} 小时（规则推断，非确认捕捞）。")

    in_port = [s for s in segs if s["sub_activity"] == "port_stay"]
    if in_port:
        h = sum(s["duration_s"] for s in in_port) / 3600
        names = []
        for s in in_port:
            np_ = (s["evidence"].get("port_context") or {}).get("nearest_port")
            if np_ and np_["name"] not in names:
                names.append(np_["name"])
        parts.append(
            f"其中约 {h:.1f} 小时停在港口附近"
            + (f"（{'、'.join(names[:3])}）" if names else "")
            + "。"
        )

    for p in segs:
        if p["sub_activity"] == "anchorage_candidate" and p["duration_s"] >= 3600:
            np_ = (p["evidence"].get("port_context") or {}).get("nearest_port")
            if np_ is None:
                parts.append("另有较长时段在无已知港口的海域静止，属于锚泊或漂航候选。")
            else:
                parts.append(
                    f"另有 {p['duration_s'] / 3600:.1f} 小时在离港约 {np_['distance_km']} km 处静止，"
                    "更像锚地或漂航而非靠泊。"
                )
            break

    if gaps:
        h = sum(s["duration_s"] for s in gaps) / 3600
        longest = max(s["duration_s"] for s in gaps) / 3600
        parts.append(f"存在 {len(gaps)} 段观测中断，累计 {h:.1f} 小时，最长 {longest:.1f} 小时；中断 ≠ 关闭 AIS。")

    poor = [s for s in real if s["reliability"]["grade"] == "poor"]
    if poor:
        parts.append(f"其中 {len(poor)} 段的观测质量较差，相关结论需谨慎。")
    return "".join(parts)


def behavior_timeline(mmsi: int, hours: int = 24, persist: bool = True) -> dict:
    """把单船轨迹重建为行为时间线。"""
    mmsi = int(mmsi)
    hours = max(1, min(int(hours), 168))
    now = int(time.time())
    window = (now - hours * 3600, now)

    track = vs.vessel_track(mmsi, hours=hours, max_points=8000)
    info = vs.vessel_detail(mmsi)
    raw = track["track"]

    if not raw:
        return {
            "mmsi": mmsi,
            "found": bool(info),
            "model_version": MODEL_VERSION,
            "hours": hours,
            "window": {"start": window[0], "end": window[1]},
            "points": 0,
            "segments": [],
            "summary": None,
            "narrative": _narrative(mmsi, (info or {}).get("ship_name"), hours, [], 0),
            "caveat": "该船在窗口内无轨迹点，无法重建行为。无数据 ≠ 无活动。",
        }

    pts = _with_effective_speed(raw)
    gap_thr = _gap_threshold(pts)
    identity = (info or {}).get("identity") or "unknown"
    segs = _build(pts, identity, gap_thr, window)

    # 稳定 id：mmsi + 起始时间 + 模型版本
    for s in segs:
        s["seg_id"] = f"{mmsi}-{s['start_ts']}-{MODEL_VERSION}"
        s["label"] = LABEL.get(s["activity"], s["activity"])
        s["sub_label"] = SUB_LABEL.get(s["sub_activity"] or "", None)
        s["duration_min"] = round(s["duration_s"] / 60, 1)
        s["lat"] = None
        s["lon"] = None

    # 为每段补一个代表点（中点），方便地图定位
    idx = 0
    for s in segs:
        if s["activity"] == "observation_gap":
            continue
        while idx < len(pts) and pts[idx]["ts"] < s["start_ts"]:
            idx += 1
        lo, hi = idx, idx
        while hi < len(pts) and pts[hi]["ts"] <= s["end_ts"]:
            hi += 1
        if hi > lo:
            mid = pts[(lo + hi) // 2]
            s["lat"], s["lon"] = round(mid["lat"], 4), round(mid["lon"], 4)
        idx = max(lo, hi - 1)

    real = [s for s in segs if s["activity"] != "observation_gap"]
    gaps = [s for s in segs if s["activity"] == "observation_gap"]
    observed_s = sum(s["duration_s"] for s in real)
    cands = [s for s in segs if s["sub_activity"] == "fishing_candidate"]

    summary = {
        "window_hours": hours,
        "segments": len(segs),
        "observed_hours": round(observed_s / 3600, 2),
        "gap_hours": round(sum(s["duration_s"] for s in gaps) / 3600, 2),
        "coverage_ratio": round(min(1.0, observed_s / (hours * 3600)), 3),
        "by_activity": {
            k: round(sum(s["duration_s"] for s in real if s["activity"] == k) / 3600, 2)
            for k in ("transit", "low_speed", "stationary", "unknown")
        },
        "fishing_candidate_count": len(cands),
        "fishing_candidate_hours": round(sum(s["duration_s"] for s in cands) / 3600, 2),
        "port_stay_hours": round(
            sum(s["duration_s"] for s in segs if s["sub_activity"] == "port_stay") / 3600, 2
        ),
        "anchorage_hours": round(
            sum(s["duration_s"] for s in segs if s["sub_activity"] == "anchorage_candidate") / 3600, 2
        ),
        "distance_nm": round(sum(s["evidence"].get("distance_nm") or 0 for s in real), 1),
        "poor_reliability_segments": sum(1 for s in real if s["reliability"]["grade"] == "poor"),
        "gap_threshold_s": gap_thr,
        "median_interval_s": int(_median_interval(pts)),
    }

    result = {
        "mmsi": mmsi,
        "found": True,
        "name": (info or {}).get("ship_name"),
        "identity": identity,
        "ship_type_text": (info or {}).get("ship_type_text"),
        "hours": hours,
        "window": {"start": window[0], "end": window[1]},
        "points": len(pts),
        "model_version": MODEL_VERSION,
        "summary": summary,
        "segments": segs,
        "narrative": _narrative(mmsi, (info or {}).get("ship_name"), hours, segs, len(pts)),
        "caveat": (
            "行为标签由规则从 AIS 速度与航迹形态推断，interval 内不做插值。"
            "「疑似捕捞候选」不区分渔具、未经概率校准，不能作为执法依据。"
            "注意拖网是直线拖曳、围网与延绳钓更曲折，单一「直线度低=作业」的判断对拖网不成立，"
            "因此直线度只在「长距离+长时间」时才作为排除条件。"
            "港/锚地判定基于 NGA World Port Index 的港口参考点（5 km 内记为在港），"
            "WPI 不含泊位与锚地边界、也不保证覆盖所有小型渔码头，因此一律输出为「候选」。"
        ),
    }

    if persist:
        _persist(mmsi, segs, result)
    return result


def _persist(mmsi: int, segs: list[dict], result: dict) -> None:
    """写入行为片段缓存。同 model_version 幂等覆盖，跨版本并存。"""
    now = int(time.time())
    rows = [
        (
            s["seg_id"], mmsi, s["start_ts"], s["end_ts"], s["duration_s"],
            s["activity"], s["sub_activity"], s.get("gear_type"),
            s["confidence"], json.dumps(s["evidence"], ensure_ascii=False),
            json.dumps(s["reliability"], ensure_ascii=False), MODEL_VERSION, now,
        )
        for s in segs
    ]
    try:
        with closing(connect()) as conn:
            conn.executemany(
                """INSERT INTO behavior_segments
                   (seg_id, mmsi, start_ts, end_ts, duration_s, activity, sub_activity,
                    gear_type, confidence, evidence, reliability, model_version, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(seg_id) DO UPDATE SET
                     duration_s=excluded.duration_s, activity=excluded.activity,
                     sub_activity=excluded.sub_activity, evidence=excluded.evidence,
                     reliability=excluded.reliability, created_at=excluded.created_at""",
                rows,
            )
            conn.commit()
    except Exception:
        # 缓存写失败不影响分析结果返回
        pass


def compact(result: dict, max_segments: int = 40) -> dict:
    """给智能体用的精简版：去掉体积大的证据明细，只留可解释字段。"""
    segs = result.get("segments") or []
    trimmed = []
    for s in segs[:max_segments]:
        trimmed.append({
            "activity": s["activity"],
            "label": s["label"],
            "sub_activity": s["sub_activity"],
            "sub_label": s.get("sub_label"),
            "start_ts": s["start_ts"],
            "end_ts": s["end_ts"],
            "duration_min": s["duration_min"],
            "lat": s["lat"],
            "lon": s["lon"],
            "median_sog": s["evidence"].get("median_sog"),
            "nearest_port": (s["evidence"].get("port_context") or {}).get("nearest_port"),
            "rule_score": s["evidence"].get("rule_score"),
            "reliability": s["reliability"]["grade"],
            "flags": s["reliability"]["flags"],
        })
    return {
        "mmsi": result["mmsi"],
        "name": result.get("name"),
        "identity": result.get("identity"),
        "hours": result["hours"],
        "points": result["points"],
        "summary": result.get("summary"),
        "segments": trimmed,
        "truncated": len(segs) > max_segments,
        "narrative": result.get("narrative"),
        "caveat": result.get("caveat"),
    }
