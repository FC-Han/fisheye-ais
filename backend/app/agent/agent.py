"""FishEye 智能体。

两种模式：
1. LLM 模式（配置 LLM_API_KEY 后启用）：OpenAI 兼容接口 + 工具调用（默认接 DeepSeek）
2. 规则模式（默认兜底，无需任何模型）：中文意图解析 → 直接调用同一批工具

两条路径共用 tools.py，保证「有模型/没模型」行为一致、结果都可回溯到工具。
"""
from __future__ import annotations

import datetime
import json
import logging
import re

import httpx

from core.config import ROOT, settings

from .tools import TOOL_SCHEMAS, TOOLS

log = logging.getLogger("agent")

SYSTEM_PROMPT = """你是 FishEye（渔眼）渔业态势分析助手，服务渔民、渔业管理者和科研人员。
你连接的是一个正在实时接收 AIS 的渔业数据库，所有结论必须来自工具返回的数据。

## 铁律
1. 只能通过工具获取数据，绝不编造船舶、坐标、数量或事件。
2. 严格区分三类表述：【观测】AIS 实际收到的数据；【推断】规则算法估计（如疑似捕捞、低速作业）；【缺失】没有数据。
3. 没有数据时说明可能是 AIS 覆盖不足或采集链路问题，不能断言"该海域没有船"。
4. 不作违法认定。IUU/违规只能表述为"值得进一步核实的线索"，并说明依据。
5. 提到船舶必须带 MMSI，便于用户在地图上定位。

## 工具选择
- 问「哪里有渔船热点 / 哪个海域在作业 / 渔船聚集在哪」→ query_fishing_hotspots
- 问某艘船「在干什么 / 最近状态 / 有没有在作业 / 动向」→ get_vessel_behavior
- 问「这艘船这段时间*具体*做了什么 / 过去几天怎么过的 / 时间线」→ get_vessel_timeline（更结构化，带观测缺口）
- 要下结论前，如果数据可能不全 → get_observation_quality 先判断能不能采信
- 问「为什么信号消失了 / 这个可疑片段可信吗」→ get_event_evidence，必须引用卡片里的替代解释
- 问「和以前比有什么不同 / 作业习惯变了吗 / 行为指纹」→ compare_vessel_behavior；
  若返回 insufficient_history，如实说明历史航次不够，**不要**编造"与历史相比如何"
- 问「能不能分析环境 / 能不能识别渔具 / 能不能做 XX」→ 先 get_analysis_capabilities，按实际能力回答
- 问「在什么水温的海域活动 / 行为和海温有没有关系 / 海流情况」→ get_ocean_features。
  首次调用要访问远程服务（20–60 秒），等不到就如实说"还在算"，**绝不编造环境数值**；
  即使拿到，也只能说「船经历过什么环境」，不能说「船偏好什么环境」
- 问「在港口还是海上锚着 / 靠港了吗」→ get_vessel_timeline，看 sub_activity 是
  port_stay 还是 anchorage_candidate，并引 nearest_port 的距离
- 只有船名没有 MMSI → 先 find_vessels 拿到 MMSI 再查
- 问「疑似捕捞有哪些」→ query_fishing_activity；问「哪些船信号消失」→ get_gap_events
- 问数据是否正常 → get_stream_health
- 用户提到"东海/黄海/南海/北海"等海域时，把对应 bbox 传给工具

## 能力边界（很重要）
- 行为标签是规则推断，不是人工确认；「疑似捕捞候选」不区分渔具。
- 置信度字段为 null 时，表示未经概率校准，不要把它说成"概率为 X%"。
- AIS 中断有多种原因：主动关闭、设备故障、覆盖不足、我们自己的链路问题。
  get_event_evidence 给的 attribution 只是证据倾向，不是结论。
- 环境分析（SST/海流/Chl-a）与 SAR 验证当前不可用，被问到要直接说明缺少什么。

## 回答格式
中文，控制在 220 字内，结构：
① 结论（带数字，一句话）② 逐条依据（每条独立一行，用「·」开头，不超过 4 条）③ 数据局限（一句话）
- 每条控制在 40 字以内，不要把多个数字堆成一段。
- 不要复述工具原始 JSON，不要写"根据工具返回"这类过程话术。
- 用户追问"这艘船/这个区域"时，结合上文最近提到的 MMSI 或海域名作答，不要反问已经说过的信息。"""

# 海域关键词 → regions.json 的 key
REGION_KEYWORDS = {
    "东海": "east_china",
    "黄海": "yellow_sea",
    "南海": "south_china_sea",
    "渤海": "bohai",
    "青岛": "qingdao",
    "西北太平洋": "northwest_pacific",
    "北海": "north_sea",
    "台湾海峡": "taiwan_strait",
}


def _region_bbox(text: str) -> list[float] | None:
    """从文本中识别预设海域，返回 [minlat, minlon, maxlat, maxlon]。"""
    from pathlib import Path

    p: Path = ROOT / "config" / "regions.json"
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None
    for kw, key in REGION_KEYWORDS.items():
        if kw in text and key in data:
            bb = data[key]["bboxes"][0]
            return [bb[0][0], bb[0][1], bb[1][0], bb[1][1]]
    return None


def _hours(text: str, default: int = 24) -> int:
    m = re.search(r"(\d+)\s*(?:个)?\s*小时", text)
    if m:
        return max(1, min(168, int(m.group(1))))
    m = re.search(r"(\d+)\s*天", text)
    if m:
        return max(1, min(168, int(m.group(1)) * 24))
    if any(k in text for k in ("一周", "7天", "七天", "本周")):
        return 168
    if any(k in text for k in ("今天", "24小时")):
        return 24
    if "最近" in text:
        return 24
    return default


HOTSPOT_WORDS = ("热点", "热区", "哪里多", "哪多", "聚集", "密集", "哪个海域", "哪片海", "渔场", "作业区", "集中")
BEHAVIOR_WORDS = ("在干什么", "干什么", "干嘛", "运动状态", "最近状态", "状态如何", "动向", "在作业吗", "有没有在作业",
                  "在捕捞吗", "跑到哪", "去哪了", "行为", "活动情况", "最近怎么样", "轨迹形态")


def _parse_intent(text: str) -> tuple[str, dict]:
    mmsi = None
    m = re.search(r"\b(\d{9})\b", text)
    if m:
        mmsi = int(m.group(1))
    hours = _hours(text)
    bbox = _region_bbox(text)
    t = text.lower()

    if any(k in text for k in ("数据源", "连接", "链路", "覆盖", "健康", "采集是否", "断线")) or "stream" in t:
        return "get_stream_health", {}

    if any(k in text for k in HOTSPOT_WORDS):
        args: dict = {"hours": hours, "limit": 8}
        if bbox:
            args["bbox"] = bbox
        return "query_fishing_hotspots", args

    if mmsi and any(k in text for k in BEHAVIOR_WORDS):
        return "get_vessel_behavior", {"mmsi": mmsi, "hours": hours}
    if mmsi and any(k in text for k in ("轨迹", "航迹", "历史", "走过", "路线")):
        return "get_vessel_track", {"mmsi": mmsi, "hours": hours}
    if mmsi and any(k in text for k in ("档案", "信息", "哪条船", "什么船", "是谁", "详情")):
        return "get_vessel_info", {"mmsi": mmsi}
    if mmsi:
        # 只给 MMSI 的模糊问题，默认给运动状态（比原始轨迹更有信息量）
        if any(k in text for k in BEHAVIOR_WORDS) or len(text) <= 14:
            return "get_vessel_behavior", {"mmsi": mmsi, "hours": hours}
        return "get_vessel_track", {"mmsi": mmsi, "hours": hours}

    if any(k in text for k in ("中断", "关闭ais", "关闭 AIS", "消失", "信号丢失", "静默", "gap")):
        return "get_gap_events", {"hours": hours}
    if any(k in text for k in ("禁渔", "保护区", "围栏", "越界")):
        return "query_geofence_events", {"hours": hours}
    if any(k in text for k in ("捕捞", "作业", "拖网", "钓鱼", "在捕")):
        args = {"hours": hours}
        if bbox:
            args["bbox"] = bbox
        return "query_fishing_activity", args

    # 只有船名：抓「XX 船 / XX 号 / 找一下 XX」这类表达
    m = re.search(r"[「“\"']?([\u4e00-\u9fa5A-Za-z0-9][\u4e00-\u9fa5A-Za-z0-9 \-]{2,24})[」”\"']?\s*(?:号|船)", text)
    if m and any(k in text for k in ("找", "查", "搜", "哪艘", "看看")):
        return "find_vessels", {"q": m.group(1).strip(), "limit": 8}

    args = {"identity": "all", "limit": 40}
    if bbox:
        args["bbox"] = bbox
        args["identity"] = "all_fishing"
    return "get_live_vessels", args


def _fmt_ts(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%m-%d %H:%M")


def _fmt_time(ts: int | None) -> str:
    if not ts:
        return "-"
    return datetime.datetime.fromtimestamp(ts).strftime("%H:%M")


def _answer_from(tool: str, res: dict) -> str:
    if tool == "get_stream_health":
        if "connected" not in res:
            return res.get("message", "无健康数据。")
        if not res.get("seconds_since_last_message"):
            return "采集器尚未收到任何消息。"
        stale = res["seconds_since_last_message"]
        head = "数据链路【正常】" if res["connected"] and stale < 300 else "数据链路【异常/停滞】"
        return (
            f"{head}：当前跟踪 {res['tracked_vessels']} 艘船，"
            f"消息速率 {res['message_rate_per_sec']}/s，"
            f"最后一条消息在 {stale} 秒前，累计接收 {res['messages_total']} 条。\n"
            f"已存轨迹点 {res['stored_positions']} 个。\n"
            f"注意：链路停滞时，任何「船舶消失」推断都不可信。"
        )

    if tool == "query_fishing_hotspots":
        spots = res.get("hotspots") or []
        if not spots:
            return (
                f"过去 {res['hours']} 小时内，在已接收的 AIS 数据里没有形成明显热点。"
                "这通常意味着覆盖稀疏、渔船未开 AIS，或作业时长未达到识别阈值（30 分钟）。"
            )
        lines = [f"过去 {res['hours']} 小时的渔船作业热点（按聚集度排序，推断非确认）："]
        for i, c in enumerate(spots[:5], 1):
            bits = []
            if c["suspected_fishing_events"]:
                bits.append(f"{c['suspected_fishing_events']} 段疑似捕捞、{c['vessel_hours']} 船·小时")
            if c["low_speed_vessels_now"]:
                bits.append(f"此刻 {c['low_speed_vessels_now']} 艘低速渔船")
            if c["gap_candidates"]:
                bits.append(f"{c['gap_candidates']} 条中断线索")
            lines.append(f"{i}. {c['area']}（{c['score']:.0f} 分）：" + "，".join(bits or ["活动较弱"]))
        ex = spots[0]["example_vessels"][:2]
        if ex:
            lines.append("代表船舶：" + "、".join(f"{v['name'] or '未知船名'}(MMSI {v['mmsi']})" for v in ex))
        lines.append("提示：冷色海域可能是 AIS 覆盖不足，不代表真的没有渔船。")
        return "\n".join(lines)

    if tool == "get_vessel_behavior":
        if not res.get("points"):
            return res.get("message") or f"MMSI {res.get('mmsi')} 在窗口内没有轨迹点。"
        return res.get("narrative") or "已完成分析。"

    if tool == "find_vessels":
        rows = res.get("results") or []
        if not rows:
            return f"没有找到与「{res.get('query')}」匹配的船舶。可能未收到该船静态信息，或船名写法不同。"
        lines = [f"找到 {len(rows)} 条匹配："]
        for r in rows[:5]:
            lines.append(f"· {r['ship_name'] or '未知船名'} MMSI {r['mmsi']}（{r.get('ship_type_text') or '类型未知'}）")
        lines.append("告诉我 MMSI 或船名，我可以继续分析它的运动状态。")
        return "\n".join(lines)

    if tool == "query_fishing_activity":
        if not res["event_count"]:
            return (
                f"过去 {res['hours']} 小时，当前分析范围内【未识别到】满足规则条件的疑似捕捞片段。"
                "这不等于没有捕捞活动——可能是 AIS 覆盖不足、渔船未开启 AIS，或作业时长未达阈值（30 分钟）。"
            )
        top = res["events"][:5]
        lines = [f"过去 {res['hours']} 小时共 {res['event_count']} 段疑似捕捞，涉及 {res['distinct_vessels']} 艘船（推断，非确认）："]
        for e in top:
            lines.append(
                f"· MMSI {e['mmsi']} {e.get('ship_name') or ''}：{_fmt_ts(e['start_ts'])} 起持续 "
                f"{round((e['end_ts'] - e['start_ts']) / 60)} 分钟，置信度 {e['confidence']}"
            )
        lines.append("依据：低速（0.4–6 kn）+ 持续时间 + 航迹形态；未区分渔具，需结合船舶登记核实。")
        return "\n".join(lines)

    if tool == "get_gap_events":
        if not res["count"]:
            return f"过去 {res['hours']} 小时无 AIS 中断候选事件。"
        lines = [
            f"过去 {res['hours']} 小时共 {res['count']} 条中断记录，其中 {res['candidates']} 条在覆盖正常时可归因为候选（其余为覆盖/链路原因）。"
        ]
        for e in res["events"][:5]:
            lines.append(f"· MMSI {e['mmsi']} {e.get('ship_name') or ''}：{_fmt_ts(e['start_ts'])} 起静默，状态 {e['status']}")
        lines.append("信号中断 ≠ 关闭 AIS，需排除覆盖空洞与采集链路故障。")
        return "\n".join(lines)

    if tool == "query_geofence_events":
        if not res["count"]:
            return f"过去 {res['hours']} 小时无区域进入/离开事件。"
        lines = [f"过去 {res['hours']} 小时共 {res['count']} 条区域事件："]
        for e in res["events"][:5]:
            lines.append(f"· {_fmt_ts(e['ts'])} MMSI {e['mmsi']} {e['detail']}")
        lines.append("仅代表进入某多边形的观测事实，示例围栏为示意边界，不构成违规判断。")
        return "\n".join(lines)

    if tool == "get_live_vessels":
        if not res["count"]:
            return "所选范围内近 1 小时没有收到 AIS 报告，可能是覆盖不足或采集范围未包含该区域。"
        return (
            f"范围内近 1 小时收到 {res['count']} 艘船的位置，其中渔船/疑似渔船 {res['fishing_related']} 艘。"
            f"（按最近报告时间排序，最多返回 200 条）"
        )

    if tool == "get_vessel_track":
        if not res.get("found"):
            return f"数据库中暂无 MMSI {res.get('mmsi')} 的记录，可能其 AIS 未被接收或不在订阅范围内。"
        return (
            f"MMSI {res['mmsi']} {res.get('name') or ''}（{res.get('ship_type_text') or '类型未知'}）："
            f"过去 {res['hours']} 小时 {res['points']} 个轨迹点，均速 {res.get('avg_sog')} kn，"
            f"疑似捕捞片段 {len(res['fishing_events'])} 段，信号中断 {len(res['gap_events'])} 次。"
        )

    if tool == "get_vessel_info":
        if not res.get("found"):
            return f"数据库中暂无 MMSI {res.get('mmsi')} 的档案。"
        s = res.get("stats_24h") or {}
        return (
            f"MMSI {res['mmsi']} {res.get('ship_name') or ''} | 类型 {res.get('ship_type_text') or '未知'} | "
            f"身份判定 {res.get('identity')}（来源 {res.get('identity_source')}）| 24h 轨迹点 {s.get('points', 0)} | "
            f"疑似捕捞 {res.get('fishing_events_24h', 0)} 段 | 中断 {res.get('gap_events_24h', 0)} 次"
        )
    return "已执行查询。"


def _map_hint(tool: str, res: dict) -> dict:
    hint: dict = {"tool": tool}
    if tool in ("get_vessel_track", "get_vessel_behavior") and (res.get("found") or res.get("points")):
        hint["focus_mmsi"] = res["mmsi"]
        if res.get("current", {}).get("lat") is not None:
            hint["focus_lat"] = res["current"]["lat"]
            hint["focus_lon"] = res["current"]["lon"]
        elif res.get("track_sample"):
            hint["focus_lat"] = res["track_sample"][-1]["lat"]
            hint["focus_lon"] = res["track_sample"][-1]["lon"]
    if tool == "query_fishing_hotspots":
        spots = res.get("hotspots") or []
        hint["hotspots"] = spots
        if spots:
            lats = [c["lat"] for c in spots[:8]]
            lons = [c["lon"] for c in spots[:8]]
            hint["bbox"] = [min(lats) - 2, min(lons) - 2, max(lats) + 2, max(lons) + 2]
    if tool == "query_fishing_activity":
        hint["mmsis"] = sorted({e["mmsi"] for e in res.get("events", [])})[:100]
        if res.get("bbox"):
            hint["bbox"] = res["bbox"]
    if tool == "get_gap_events":
        hint["mmsis"] = sorted({e["mmsi"] for e in res.get("events", [])})[:100]
        hint["highlight"] = "gap"
    if tool == "get_live_vessels" and res.get("bbox"):
        hint["bbox"] = res["bbox"]
    if tool == "find_vessels" and res.get("results"):
        hint["mmsis"] = [r["mmsi"] for r in res["results"][:20]]
    return hint


def _merge_maps(used: list[dict]) -> dict:
    """合并多次工具调用的地图提示：后面的按键覆盖，但热点与 bbox 优先级最高。"""
    out: dict = {}
    for u in used:
        m = u.get("map") or {}
        for k, v in m.items():
            if k == "tool":
                continue
            if v not in (None, [], {}):
                out[k] = v
    return out


# ---------------- LLM 模式 ----------------

def _run_llm(message: str, history: list[dict]) -> dict:
    now = datetime.datetime.now()
    messages = [
        {
            "role": "system",
            "content": SYSTEM_PROMPT
            + f"\n\n当前时间：{now.strftime('%Y-%m-%d %H:%M')}（本地时区）。数据库中的数据为实时 AIS 接收结果。",
        }
    ]
    for h in history[-8:]:
        if h.get("role") in ("user", "assistant") and h.get("content"):
            messages.append({"role": h["role"], "content": str(h["content"])[:2000]})
    messages.append({"role": "user", "content": message})

    used: list[dict] = []
    headers = {"Authorization": f"Bearer {settings.llm_api_key}", "Content-Type": "application/json"}
    url = settings.llm_base_url.rstrip("/") + "/chat/completions"

    for _ in range(5):
        payload = {
            "model": settings.llm_model,
            "messages": messages,
            "tools": TOOL_SCHEMAS,
            "temperature": 0.2,
            "max_tokens": 900,
        }
        r = httpx.post(url, headers=headers, json=payload, timeout=60)
        r.raise_for_status()
        data = r.json()
        msg = data["choices"][0]["message"]
        calls = msg.get("tool_calls") or []
        if not calls:
            return {
                "answer": (msg.get("content") or "").strip(),
                "tools": used,
                "map": _merge_maps(used),
                "model": data.get("model") or settings.llm_model,
                "mode": "llm",
            }
        messages.append(msg)
        for c in calls:
            name = c["function"]["name"]
            try:
                args = json.loads(c["function"].get("arguments") or "{}")
            except json.JSONDecodeError:
                args = {}
            fn = TOOLS.get(name)
            if not fn:
                result = {"error": f"unknown tool {name}"}
            else:
                try:
                    result = fn(**args)
                except Exception as e:  # 工具失败也要回给模型，不能让它瞎猜
                    result = {"error": f"{type(e).__name__}: {e}"}
            ok = "error" not in result
            used.append({"name": name, "args": args, "ok": ok, "map": _map_hint(name, result) if ok else {}})
            messages.append({
                "role": "tool",
                "tool_call_id": c["id"],
                "content": json.dumps(result, ensure_ascii=False, default=str)[:12000],
            })
    return {
        "answer": "分析步骤超出上限，请把问题缩小到单个海域或单艘船。",
        "tools": used,
        "map": _merge_maps(used),
        "model": settings.llm_model,
        "mode": "llm",
    }


# ---------------- 对外入口 ----------------

def chat(message: str, history: list[dict] | None = None) -> dict:
    history = history or []
    if settings.llm_enabled:
        try:
            return _run_llm(message, history)
        except Exception as e:
            log.warning("LLM 调用失败，降级为规则模式：%s", e)

    tool, args = _parse_intent(message)
    try:
        res = TOOLS[tool](**args)
    except Exception as e:
        return {"answer": f"查询失败：{type(e).__name__}: {e}", "tools": [], "map": {}, "mode": "rules"}
    return {
        "answer": _answer_from(tool, res),
        "tools": [{"name": tool, "args": args, "ok": True, "map": _map_hint(tool, res)}],
        "map": _map_hint(tool, res),
        "mode": "rules",
        "note": "未配置大模型，使用内置规则解析（结果同样来自真实数据库工具）",
    }
