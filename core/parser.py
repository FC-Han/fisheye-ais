"""AISStream 消息解析与标准化。

AISStream 消息形态：
{"MessageType": "...", "MetaData": {...}, "Message": {"<Type>": {...}}}

注意：上游并非每条消息都带可靠的发报时间戳，因此统一以 received_at（服务端接收时间）
为准，绝不把接收时间伪装成船舶发报时间。
"""
from __future__ import annotations

import re
from typing import Any

POSITION_TYPES = {
    "PositionReport",
    "StandardClassBPositionReport",
    "ExtendedClassBPositionReport",
    "LongRangeAisBroadcastMessage",
}

STATIC_TYPES = {"ShipStaticData", "StaticDataReport"}

SHIP_TYPE_MAP: dict[int, str] = {
    0: "未提供",
    30: "渔船",
    31: "拖船",
    32: "拖船",
    33: "疏浚船",
    35: "军用",
    36: "帆船",
    37: "游艇",
    40: "高速船",
    50: "引航船",
    51: "搜救船",
    52: "拖轮",
    53: "港口供应船",
    55: "执法船",
    60: "客船",
    70: "货船",
    80: "油轮",
    90: "其他",
}

# 货船/油轮/客船等细分（70-89 货船，80-89 油轮，60-69 客船）
_RANGES = [
    (20, 29, "地效翼船"),
    (40, 49, "高速船"),
    (60, 69, "客船"),
    (70, 79, "货船"),
    (80, 89, "油轮"),
    (90, 99, "其他"),
]

NON_FISHING_TYPES = {31, 32, 33, 35, 36, 37, 40, 50, 51, 52, 53, 55} | set(range(60, 100))

# 船名中出现这些词，在船舶类型缺失时可作为“疑似渔船”的弱证据
_FISHING_NAME_HINTS = re.compile(
    r"(FISH|FISK|PECHE|PESCA|渔|魯|鲁|浙|闽|粤|琼|辽|苏|沪|津|冀|桂|HARVEST|TRAWL|CHUAN|YU\b)",
    re.IGNORECASE,
)


def ship_type_text(code: Any) -> str:
    try:
        c = int(code)
    except (TypeError, ValueError):
        return "未知"
    if c in SHIP_TYPE_MAP:
        return SHIP_TYPE_MAP[c]
    for lo, hi, label in _RANGES:
        if lo <= c <= hi:
            return label
    return "未知"


def classify_identity(ship_type: Any, ship_name: str | None) -> tuple[str, str]:
    """返回 (fishing_identity, identity_source)。

    - fishing     : AIS 静态报明确声明类型 30（渔船）
    - candidate   : 类型缺失但船名带有渔业特征，或类型为“未提供”
    - non_fishing : AIS 明确声明为非渔船类型
    - unknown     : 无任何静态信息

    这只是弱分类，生产环境应接入船舶登记/许可名单作为权威身份源。
    """
    try:
        t = int(ship_type) if ship_type is not None else None
    except (TypeError, ValueError):
        t = None

    if t == 30:
        return "fishing", "ais_ship_type"
    if t is not None and t in NON_FISHING_TYPES:
        return "non_fishing", "ais_ship_type"
    if t is None or t == 0:
        if ship_name and _FISHING_NAME_HINTS.search(ship_name):
            return "candidate", "name_pattern"
        return "unknown", "none"
    return "unknown", "ais_ship_type"


def _as_float(v: Any) -> float | None:
    try:
        if v is None:
            return None
        f = float(v)
        # AIS 用 1023 / 511 / 3600 等表示“不可用”
        if abs(f) >= 511 and abs(f) <= 3600:
            return None
        return f
    except (TypeError, ValueError):
        return None


def _as_int(v: Any) -> int | None:
    try:
        if v is None:
            return None
        return int(v)
    except (TypeError, ValueError):
        return None


def _cog(v: Any) -> float | None:
    """COG：360 / 3600 表示不可用，不是“正北”。"""
    f = _as_float(v)
    if f is None or f >= 360 or f < 0:
        return None
    return f


def _heading(v: Any) -> int | None:
    """TrueHeading：511 表示不可用。"""
    i = _as_int(v)
    if i is None or i >= 360 or i < 0:
        return None
    return i


def _sog(v: Any) -> float | None:
    """SOG：1023 表示不可用。"""
    f = _as_float(v)
    if f is None or f >= 102.3 or f < 0:
        return None
    return f


def normalize(raw: dict) -> dict | None:
    """把一条上游消息标准化为统一结构，无法识别时返回 None。"""
    if not isinstance(raw, dict):
        return None
    mtype = raw.get("MessageType") or raw.get("message_type")
    meta = raw.get("MetaData") or raw.get("metadata") or {}
    body = raw.get("Message") or raw.get("message") or {}
    if not isinstance(body, dict) or not body:
        return None

    payload = body.get(mtype) if mtype in body else next(iter(body.values()), {})
    if not isinstance(payload, dict):
        return None

    mmsi = _as_int(meta.get("MMSI") or meta.get("mmsi") or payload.get("UserID"))
    if not mmsi:
        return None

    kind = "position" if mtype in POSITION_TYPES else ("static" if mtype in STATIC_TYPES else "other")

    lat = _as_float(payload.get("Latitude", payload.get("latitude", meta.get("latitude"))))
    lon = _as_float(payload.get("Longitude", payload.get("longitude", meta.get("longitude"))))
    if kind == "other":
        # 其他消息类型也带上位置，便于前端观察（如 SafetyMessage 等），但没有就不发
        pass

    return {
        "mmsi": mmsi,
        "msg_type": mtype,
        "kind": kind,
        "lat": lat,
        "lon": lon,
        "sog": _sog(payload.get("Sog", payload.get("SOG"))),
        "cog": _cog(payload.get("Cog", payload.get("COG"))),
        "heading": _heading(payload.get("TrueHeading")),
        "nav_status": _as_int(payload.get("NavigationalStatus")),
        "ship_name": (meta.get("ShipName") or "").strip() or None,
        # 静态信息
        "ship_type": _as_int(payload.get("Type")),
        "imo": str(payload.get("ImoNumber") or "").strip() or None,
        "call_sign": (payload.get("CallSign") or "").strip() or None,
        "destination": (payload.get("Destination") or "").strip() or None,
        "dimension": payload.get("Dimension") if isinstance(payload.get("Dimension"), dict) else None,
        "draught": _as_float(payload.get("MaximumStaticDraught")),
        "part_number": _as_int(payload.get("PartNumber")),
    }
