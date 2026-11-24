"""捕捞行为估计（规则引擎 v0.1）。

设计原则
--------
1. 只对“渔船 / 疑似渔船 / 身份未知”的船做估计，绝不把货轮的低速当成捕捞。
2. 输出的是【疑似作业片段 + 证据】，不是“已确认捕捞”。
3. 不做渔具级判断：没有渔具信息时统一标注 gear=unknown，只给“低速作业候选”。
   拖网 / 围网 / 延绳钓 / 刺网的阈值差异很大，v0.1 不假装能区分。
"""
from __future__ import annotations

import json
import math
import sqlite3
import time
from statistics import mean, pstdev

from core.geo import angle_diff, haversine_m

MAX_POINT_GAP = 1800      # 超过 30 分钟视为轨迹断裂
SPEED_MIN = 0.4           # 低于此速度视为停泊/漂流，不计入作业
SPEED_MAX = 6.0           # 高于此速度视为航行
MIN_SEGMENT_SEC = 1800    # 作业片段最短 30 分钟
MIN_POINTS = 6
CONF_THRESHOLD = 0.50


def _segments(pts: list[tuple]) -> list[list[tuple]]:
    segs, cur = [], []
    for p in pts:
        ts, lat, lon, sog, cog = p
        ok = sog is not None and SPEED_MIN <= sog <= SPEED_MAX
        if not ok:
            if len(cur) >= MIN_POINTS:
                segs.append(cur)
            cur = []
            continue
        if cur and ts - cur[-1][0] > MAX_POINT_GAP:
            if len(cur) >= MIN_POINTS:
                segs.append(cur)
            cur = []
        cur.append(p)
    if len(cur) >= MIN_POINTS:
        segs.append(cur)
    return segs


def _score(seg: list[tuple], identity: str) -> tuple[float, dict]:
    ts = [p[0] for p in seg]
    sogs = [p[3] for p in seg]
    cogs = [p[4] for p in seg]
    duration = ts[-1] - ts[0]
    if duration <= 0:
        return 0.0, {}

    avg_sog = mean(sogs)
    sd = pstdev(sogs) if len(sogs) > 1 else 0.0
    cv = sd / avg_sog if avg_sog > 0 else 1.0

    # 航行里程 vs 直线位移：拖网作业会明显绕行
    path = 0.0
    for a, b in zip(seg, seg[1:]):
        path += haversine_m(a[1], a[2], b[1], b[2])
    disp = haversine_m(seg[0][1], seg[0][2], seg[-1][1], seg[-1][2])
    straightness = disp / path if path > 0 else 1.0

    # 转向活动：单位时间平均航向变化（度/分钟）
    turns = [angle_diff(a, b) for a, b in zip(cogs, cogs[1:]) if a is not None and b is not None]
    turn_rate = (sum(turns) / len(turns)) / max(1e-6, duration / 60 / max(1, len(turns))) if turns else 0.0

    s_duration = min(1.0, duration / 7200)                       # 2h 封顶
    s_speed = max(0.0, 1 - abs(avg_sog - 2.8) / 3.0)             # 2.8kn 附近最典型
    s_stable = max(0.0, 1 - min(1.0, cv / 0.8))
    s_turn = max(0.0, 1 - min(1.0, abs(turn_rate - 3.0) / 12.0))
    s_path = max(0.0, min(1.0, (1 - straightness) * 2.5))

    raw = 0.30 * s_duration + 0.25 * s_speed + 0.18 * s_stable + 0.15 * s_turn + 0.12 * s_path
    factor = {"fishing": 1.0, "candidate": 0.85, "unknown": 0.7}.get(identity, 0.7)
    conf = round(min(0.95, raw * factor), 3)

    evidence = {
        "duration_min": round(duration / 60, 1),
        "avg_sog": round(avg_sog, 2),
        "sog_cv": round(cv, 3),
        "path_km": round(path / 1000, 2),
        "net_displacement_km": round(disp / 1000, 2),
        "straightness": round(straightness, 3),
        "turn_rate_deg_min": round(turn_rate, 2),
        "points": len(seg),
        "factors": {
            "duration": round(s_duration, 3),
            "speed_fit": round(s_speed, 3),
            "speed_stability": round(s_stable, 3),
            "turning": round(s_turn, 3),
            "path_shape": round(s_path, 3),
        },
        "identity_factor": factor,
        "rule_version": "v0.1-rule",
    }
    return conf, evidence


def run(conn: sqlite3.Connection, vessels: list[sqlite3.Row], positions: dict[int, list[tuple]]) -> int:
    """对给定船舶运行规则引擎，返回新增/更新事件数。"""
    now = int(time.time())
    count = 0
    for v in vessels:
        mmsi = v["mmsi"]
        pts = positions.get(mmsi) or []
        if len(pts) < MIN_POINTS:
            continue
        identity = (v["fishing_identity"] or v["identity"] or "unknown")
        if identity == "non_fishing":
            continue
        for seg in _segments(pts):
            if seg[-1][0] - seg[0][0] < MIN_SEGMENT_SEC:
                continue
            conf, ev = _score(seg, identity)
            if conf < CONF_THRESHOLD:
                continue
            # 用片段中点作为事件位置（更便于在地图上定位）
            mid = seg[len(seg) // 2]
            ev["identity"] = identity
            ev["ship_name"] = v["ship_name"]
            # 覆盖旧的重叠片段，避免同一次作业被切成多条重复事件
            conn.execute(
                "DELETE FROM fishing_events WHERE mmsi=? AND start_ts < ? AND end_ts > ?",
                (mmsi, seg[-1][0], seg[0][0]),
            )
            conn.execute(
                """INSERT OR REPLACE INTO fishing_events
                   (mmsi, start_ts, end_ts, duration_s, avg_sog, lat, lon, gear, confidence, evidence, created_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (mmsi, seg[0][0], seg[-1][0], int(seg[-1][0] - seg[0][0]), ev["avg_sog"],
                 mid[1], mid[2], "unknown", conf, json.dumps(ev, ensure_ascii=False), now),
            )
            count += 1
    return count
