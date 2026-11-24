"""Ocean Engine：按需提取轨迹点的海洋环境特征。

为什么不做「下载全球栅格再处理」
--------------------------------
一艘船一天几千个点，需要的只是 E(lon(t), lat(t), t) —— 船在那个位置那个时刻的
环境值。为此下载整片海域连续多年的栅格是几十 GB 的无用功。

这里的做法是三层：
- L1 `ocean_features`：每个轨迹点匹配到的环境值，按 (mmsi, ts) 幂等缓存
- L2 `ocean_cache_tiles` + `data/ocean_cache/*.nc`：1°×1° 的日切片文件，
  同一片海同一小时只请求一次
- L3 预留：锋面距离、梯度和异常等派生特征（尚未实现）

数据源
------
`cmems_mod_glo_phy_anfc_0.083deg_PT1H-m`
  全球海洋物理分析预报（1/12°，逐时平均，含 analysis + 10 天预报）
  取表层（0–1 m）：thetao（位温）、uo/vo（流速）、zos（海面高度）

为什么不是 `..._P1D-m`
  那个数据集只有海冰、混合层、海底温度这类 2D 场，没有 thetao / uo / vo。
  实测变量列表见 docs。这是个很容易踩的坑。

时效
----
NRT 分析产品本身有约 1 天滞后（官方日志会写 "数据在 2026-10-26 之后可能未更新"）。
所以每个点都要记录 `source_time`（实际用到的环境时刻）和 `lag_hours`，
超 `MAX_LAG_HOURS` 就判为不可用，绝不把隔了三天的 SST 当成此刻的 SST。
"""
from __future__ import annotations

import json
import math
import os
import statistics
import time
from contextlib import closing
from pathlib import Path

from core.config import ROOT
from core.db import connect

from . import vessel_state as vs

MODEL_VERSION = "ocean-v1"

# 必须在 import copernicusmarine 之前清掉代理变量：
# 这台机器上残留的 proxy 会让 Copernicus 的 S3 访问 502。
for _proxy in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
               "http_proxy", "https_proxy", "all_proxy"):
    os.environ.pop(_proxy, None)
os.environ.setdefault("NO_PROXY", "*")

PRODUCTS = {
    "physics": {
        "dataset_id": "cmems_mod_glo_phy_anfc_0.083deg_PT1H-m",
        "version": "202406",
        "short": "phy",
        "label": "全球物理分析预报（表层逐时）",
        # 数据集变量名 -> ocean_features 列名
        "variables": {
            "thetao": "sst_c",
            "uo": "u_m_s",
            "vo": "v_m_s",
            "zos": "ssh_m",
        },
        "units": {"sst_c": "°C", "u_m_s": "m/s", "v_m_s": "m/s", "ssh_m": "m"},
        "depth": (0.0, 1.0),      # 用区间而不是 0.494025，深度坐标有浮点精度坑
        "resolution_deg": 0.0833,
        "temporal": "逐时（NRT 分析 + 10 天预报）",
        "basis": "analysis_forecast",
        "tile_mode": "day",
        "tile_ttl_s": 6 * 3600,
    },
    "waves": {
        # 风浪不在物理产品里，必须换到 WAV 产品。同一套凭证，无需新账号。
        "dataset_id": "cmems_mod_glo_wav_anfc_0.083deg_PT3H-i",
        "version": "202411",
        "short": "wav",
        "label": "全球海浪分析预报（有效波高/波向/周期，3 小时）",
        "variables": {
            "VHM0": "wave_height_m",     # 有效波高（谱矩 0 阶）
            "VMDR": "wave_dir_deg",      # 波向（来向，从正北顺时针）
            "VTM02": "wave_period_s",    # 平均周期（谱矩 0/2 阶）
        },
        "units": {"wave_height_m": "m", "wave_dir_deg": "°", "wave_period_s": "s"},
        "depth": None,               # 海浪是 2D 场，没有深度维
        "resolution_deg": 0.0833,
        "temporal": "3 小时（NRT 分析 + 预报）",
        "basis": "analysis_forecast",
        "tile_mode": "day",
        "tile_ttl_s": 6 * 3600,
    },
    "bgc_clim": {
        # 实时 BGC 产品只给 nppv / o2，没有 chl（实测 VariableDoesNotExistInTheDataset）。
        # 所以这里取的是「历史同月气候态」，绝不是此刻的叶绿素。
        "dataset_id": "cmems_mod_glo_bgc_my_0.25deg_P1M-m",
        "version": "202311",
        "short": "bgcc",
        "label": "全球生地化再分析（表层叶绿素，逐月，历史气候态）",
        "variables": {
            "chl": "chl_mg_m3",
        },
        "units": {"chl_mg_m3": "mg/m³"},
        # BGC 产品的第一层是 0.5057 m，不是 0 m。写 0–1 会被服务端告警「越界」，
        # 所以下限取 0.4，仍然落在第一层上（匹配时 isel(depth=0)）。
        "depth": (0.4, 1.2),
        "resolution_deg": 0.25,
        "temporal": "逐月（历史）",
        "basis": "climatology",
        "tile_mode": "clim",
        "tile_ttl_s": 180 * 86400,   # 气候态是静态的，半年过期足够
    },
    "wind": {
        # CMEMS 的卫星散射计 L4 融合风场：观测融合，不是模式输出。
        # 覆盖从 2024-06-13 起，滞后约 2 天（实测 10-27 查询止于 10-25 23:00）。
        "dataset_id": "cmems_obs-wind_glo_phy_nrt_l4_0.125deg_PT1H",
        "version": "202411",
        "short": "wind",
        "label": "全球卫星散射计融合风场（L4，逐时 0.125°）",
        # 数据集里只有 u/v 两个分量，风速风向是派生的，所以这里的「变量」是写入列，
        # 真正的请求变量单列在 request_variables，两者的键不能混用。
        "variables": {
            "wind_speed": "wind_speed",
            "wind_dir_deg": "wind_dir_deg",
            "wind_u_m_s": "wind_u_m_s",
            "wind_v_m_s": "wind_v_m_s",
        },
        "request_variables": ["eastward_wind", "northward_wind"],
        "wind_uv": {"u": "eastward_wind", "v": "northward_wind"},
        "units": {"wind_speed": "m/s", "wind_dir_deg": "°"},
        "depth": None,               # 10 m 风是 2D 场，没有深度维
        "resolution_deg": 0.125,
        "temporal": "逐时（近实时，滞后约 2 天）",
        "basis": "observation_l4",
        "channel": "cmems",
        "time_dim": "time",
        "tile_mode": "day",
        "tile_ttl_s": 6 * 3600,
        "max_lag_hours": 36.0,
    },
    "wind_gfs": {
        # NOAA GFS 0.25°：延迟只有几小时，是唯一能覆盖「今天」的风源。
        # 用 GRIB 子集接口按区域裁剪，1°×1° 一天只要几百字节。
        "dataset_id": "gfs-0p25-10m-wind",
        "version": "gfs",
        "short": "gfs",
        "label": "NOAA GFS 10 m 风场（逐 3 小时 0.25°，近实时）",
        "variables": {
            "wind_speed": "wind_speed",
            "wind_dir_deg": "wind_dir_deg",
            "wind_u_m_s": "wind_u_m_s",
            "wind_v_m_s": "wind_v_m_s",
        },
        "request_variables": ["10u", "10v"],
        "wind_uv": {"u": "u10", "v": "v10"},
        "units": {"wind_speed": "m/s", "wind_dir_deg": "°"},
        "depth": None,
        "resolution_deg": 0.25,
        "temporal": "3 小时（分析 + 短时预报，滞后约 5 小时）",
        "basis": "analysis_forecast",
        "channel": "gfs",
        "time_dim": "time",
        "tile_mode": "day",
        "tile_ttl_s": 3 * 3600,       # 当天还会陆续出新的 cycle
        "max_new_tiles": 3,
        "max_lag_hours": 9.0,         # 3 小时分辨率，超过 9 小时说明该时刻根本没有场
    },
    "wind_era5": {
        # ERA5 再分析：质量更高、空间连续无轨道间隙，但滞后约 5 天
        #（实测 10-27 查询止于 10-22 04:00）。所以它是「历史回填」，不是近实时源。
        # 一次请求要排 CDS 队列约 3 分钟，只走异步任务。
        "dataset_id": "reanalysis-era5-single-levels",
        "version": "cds",
        "short": "era5",
        "label": "ERA5 再分析 10 m 风场（逐时 0.25°）",
        "variables": {
            "wind_speed": "wind_speed",
            "wind_dir_deg": "wind_dir_deg",
            "wind_u_m_s": "wind_u_m_s",
            "wind_v_m_s": "wind_v_m_s",
        },
        "request_variables": ["10m_u_component_of_wind", "10m_v_component_of_wind"],
        "wind_uv": {"u": "u10", "v": "v10"},     # CDS 返回值里的变量名是 u10/v10
        "units": {"wind_speed": "m/s", "wind_dir_deg": "°"},
        "depth": None,
        "resolution_deg": 0.25,
        "temporal": "逐时（再分析，滞后约 5 天）",
        "basis": "reanalysis",
        "channel": "cds",
        "time_dim": "valid_time",
        "tile_mode": "day",
        "tile_ttl_s": 30 * 86400,     # 再分析历史值不会变，缓存久一点
        # CDS 一次请求要排队约 3 分钟，不能和 CMEMS 的 10 秒瓦片共用同一个预算
        "max_new_tiles": 2,
        "max_lag_hours": 36.0,
    },
}

# 气候态的参考年份区间：取「同月份」多年平均。
# 明确写死而不是用「最近 N 年」，因为再分析产品本身止于 2024，
# 用「最近」会随系统时间漂移，导致同一份数据在不同时间算出不同结果。
CLIM_YEARS = (2019, 2023)
CLIM_START = f"{CLIM_YEARS[0]}-01-01T00:00:00"
CLIM_END = f"{CLIM_YEARS[1]}-12-31T23:59:59"

# 默认一次提取哪几个产品：物理 + 海浪 + 风是同期可用的实时场，叶绿素只有气候态。
# 风默认用 GFS：它是唯一能覆盖「今天」的源（L4 观测滞后 2 天、ERA5 滞后 5 天）。
# 要看卫星观测风或更早的历史风，得显式加 wind / wind_era5。
DEFAULT_PRODUCTS = ("physics", "waves", "wind_gfs", "bgc_clim")

# 道格拉斯海况等级（Douglas sea scale），只看有效波高这一个量。
# 这是国际通用的经验分档，不是本系统的「推断」，所以可以当标签用。
SEA_STATE = (
    (0.1, "calm", "无浪"),
    (0.5, "smooth", "微浪"),
    (1.25, "slight", "小浪"),
    (2.5, "moderate", "中浪"),
    (4.0, "rough", "大浪"),
    (6.0, "very_rough", "巨浪"),
    (9.0, "high", "狂浪"),
    (14.0, "very_high", "狂涛"),
)
SEA_STATE_UNKNOWN = ("unknown", "未知")

# 蒲福风级（Beaufort scale），阈值是各级风速上界（m/s）。
# 与海况表同理：这是国际通用的经验分档，纯查表，不是本系统的推断，可以当标签用。
BEAUFORT = (
    (0.3, 0, "无风"),
    (1.6, 1, "软风"),
    (3.4, 2, "轻风"),
    (5.5, 3, "微风"),
    (8.0, 4, "和风"),
    (10.8, 5, "清劲风"),
    (13.9, 6, "强风"),
    (17.2, 7, "疾风"),
    (20.8, 8, "大风"),
    (24.5, 9, "烈风"),
    (28.5, 10, "狂风"),
    (32.7, 11, "暴风"),
    (float("inf"), 12, "飓风"),
)

# 十六方位太细，八方位对「风从哪个方向来」已经够用
_COMPASS_8 = ("北", "东北", "东", "东南", "南", "西南", "西", "西北")

CACHE_DIR = ROOT / "data" / "ocean_cache"
TILE_DEG = 1.0                 # 请求瓦片边长（度）
SAMPLE_MINUTES = 30            # 轨迹采样间隔
MAX_POINTS = 240               # 单次最多处理多少个采样点
MAX_NEW_TILES = 4              # 单次最多新建几个瓦片（每个约 10 秒）
MAX_LAG_HOURS = 36.0           # 环境时刻与轨迹时刻允许的最大偏差
CACHE_TTL_S = 30 * 86400       # L1 点级缓存有效期
# 瓦片有效期不再用统一常量：实时产品 6 小时、气候态 180 天，各产品自己写在 PRODUCTS 里

_IMPORT_ERROR: str | None = None
_xr = None


def _lazy_import():
    """延迟导入科学计算依赖：没装也能让其它分析正常工作。"""
    global _IMPORT_ERROR, _xr
    if _xr is not None:
        return _xr
    if _IMPORT_ERROR:
        return None
    try:
        import xarray as xr  # noqa: F401
        _xr = xr
        return xr
    except Exception as exc:  # pragma: no cover - 环境问题
        _IMPORT_ERROR = f"缺少科学计算依赖: {type(exc).__name__}: {exc}"
        return None


def sea_state_of(wave_height_m: float | None) -> dict:
    """按道格拉斯海况表把有效波高换成等级名。纯查表，不含任何推断。"""
    if wave_height_m is None:
        return {"code": "unknown", "label": "未知", "scale": "douglas"}
    for limit, code, label in SEA_STATE:
        if wave_height_m < limit:
            return {"code": code, "label": label,
                    "wave_height_m": round(wave_height_m, 2), "scale": "douglas"}
    return {"code": "phenomenal", "label": "怒涛",
            "wave_height_m": round(wave_height_m, 2), "scale": "douglas"}


def beaufort_of(wind_speed_m_s: float | None) -> dict:
    """按蒲福风级表把风速换成等级。纯查表，不含任何推断。"""
    if wind_speed_m_s is None:
        return {"force": None, "label": "未知", "scale": "beaufort"}
    for limit, force, label in BEAUFORT:
        if wind_speed_m_s < limit:
            return {"force": force, "label": label,
                    "wind_speed_m_s": round(wind_speed_m_s, 2), "scale": "beaufort"}
    return {"force": 12, "label": "飓风",
            "wind_speed_m_s": round(wind_speed_m_s, 2), "scale": "beaufort"}


def wind_dir_text(deg: float | None) -> str | None:
    """风向的八方位文字。约定是「来向」：270° 表示风从西边吹来。"""
    if deg is None:
        return None
    return _COMPASS_8[int((deg % 360) / 45.0 + 0.5) % 8]


def wind_from_uv(u: float | None, v: float | None) -> tuple[float | None, float | None]:
    """由东向/北向分量算 (风速, 风来向角度)。

    气象学的风向指「风从哪来」，所以取分量反方向：
    u=+1（吹向东）意味着风来自西，即 270°。
    """
    if u is None or v is None:
        return None, None
    speed = math.hypot(u, v)
    deg = (math.degrees(math.atan2(-u, -v)) + 360.0) % 360.0
    return round(speed, 3), round(deg, 1)


def capabilities() -> dict:
    """环境引擎自检：能不能用、缺什么、有哪些产品。"""
    _lazy_import()

    def _ver(name: str):
        try:
            return getattr(__import__(name), "__version__", "ok")
        except Exception:
            return None

    # cdsapi 单独看：只有 ERA5 那一路需要它，缺了不该让整个环境引擎判定为不可用
    core_deps = ("numpy", "xarray", "netCDF4", "copernicusmarine")
    deps = {name: _ver(name) for name in core_deps}
    deps["cdsapi"] = _ver("cdsapi")

    cred = Path.home() / ".copernicusmarine" / ".copernicusmarine-credentials"
    missing = [k for k in core_deps if deps[k] is None]

    if missing:
        reason = f"缺少依赖: {', '.join(missing)}"
        available = False
    elif not cred.exists():
        reason = "未找到 Copernicus Marine 登录凭证（~/.copernicusmarine/.copernicusmarine-credentials）"
        available = False
    else:
        reason = None
        available = True

    cached = 0
    try:
        with closing(connect(readonly=True)) as conn:
            cached = conn.execute("SELECT COUNT(*) FROM ocean_features").fetchone()[0]
    except Exception:
        pass

    return {
        "available": available,
        "reason": reason,
        "dependencies": deps,
        "credentials_found": cred.exists(),
        # CDS 的 key 只用于 ERA5；Copernicus Marine 与它是两套独立的凭证
        "cds_credentials_found": _cds_api_key() is not None,
        "products": [
            {
                "key": k,
                "dataset_id": p["dataset_id"],
                "label": p["label"],
                "variables": list(p["variables"].values()),
                "units": p["units"],
                "native_resolution_deg": p["resolution_deg"],
                "temporal": p["temporal"],
                # basis 是这套系统里最重要的一个字段：
                # analysis_forecast = 该时刻的模式场，可信为「当时是什么样」
                # climatology       = 多年同月平均，只能说「这个季节通常是什么样」
                "basis": p["basis"],
                "cache_ttl_hours": round(p["tile_ttl_s"] / 3600, 1),
            }
            for k, p in PRODUCTS.items()
        ],
        "climatology_reference": {
            "product": "bgc_clim",
            "years": list(range(CLIM_YEARS[0], CLIM_YEARS[1] + 1)),
            "method": "同月份多年算术平均（先在像元上按月聚合，再取最近像元）",
            "why": "BGC 近实时产品不提供 chl，只能用历史再分析构造季节性背景",
        },
        "sea_state_scale": [
            {"max_wave_height_m": limit, "code": code, "label": label}
            for limit, code, label in SEA_STATE
        ],
        "beaufort_scale": [
            {"max_wind_speed_m_s": (None if limit == float("inf") else limit),
             "force": force, "label": label}
            for limit, force, label in BEAUFORT
        ],
        "wind_sources": [
            {
                "product": "wind_gfs",
                "kind": "analysis_forecast",
                "label": "NOAA GFS 分析场 + 3 小时内短时预报",
                "lag_days_typical": 0.2,
                "coverage_starts": "实时（仅保留最近数日）",
                "default": True,
                "note": "唯一能覆盖「今天」的风源；默认使用",
            },
            {
                "product": "wind",
                "kind": "observation_l4",
                "label": "卫星散射计 L4 融合观测",
                "lag_days_typical": 2,
                "coverage_starts": "2024-06-13",
                "note": "真实观测，但滞后约 2 天，且只从 2024-06 起",
            },
            {
                "product": "wind_era5",
                "kind": "reanalysis",
                "label": "ERA5 再分析",
                "lag_days_typical": 5,
                "coverage_starts": "1940-01-01",
                "note": "质量最高、覆盖最长，但滞后约 5 天且单次排队约 3 分钟",
            },
        ],
        "not_implemented": [
            "实时叶绿素 / 硝酸盐 / 溶解氧：cmems_mod_glo_bgc-bio_anfc_0.25deg_P1D-m 实测只暴露 "
            "nppv 与 o2，请求 chl 直接报 VariableDoesNotExistInTheDataset。"
            "本系统给的是 2019–2023 同月气候态，不是当前值",
            "风场已接入两个源：wind=卫星散射计 L4 融合（滞后约 2 天，2024-06 起），"
            "wind_era5=ERA5 再分析（滞后约 5 天，历史可回溯）。"
            "**最近约 2 天两个源都覆盖不到，这是上游发布延迟，不是本系统缺功能**",
            "锋面距离、SST 梯度、环境偏好对照样本（L3 派生特征）",
        ],
        "cached_points": cached,
        "limits": {
            "sample_minutes": SAMPLE_MINUTES,
            "max_points": MAX_POINTS,
            "max_new_tiles_per_run": MAX_NEW_TILES,
            "max_lag_hours": MAX_LAG_HOURS,
            "tile_deg": TILE_DEG,
        },
        "caveat": (
            "环境值来自模式再分析/预报产品与卫星融合产品，不是该船位置上的现场实测；"
            "用于解释行为背景，不能当作现场观测。0.125°–0.25° 网格内所有船舶拿到同一个环境值。"
            "叶绿素标注为 climatology 的，只代表那个月份的历史常态。"
            "风场两个来源语义不同：wind=卫星散射计 L4 融合观测（observation_l4），"
            "wind_era5=再分析模式（reanalysis），逐点记在 wind_basis 里，不要混用。"
        ),
    }


# --------------------------------------------------------------------------
# 采样与分桶
# --------------------------------------------------------------------------
def _sample(points: list[dict], minutes: int = SAMPLE_MINUTES, max_points: int = MAX_POINTS) -> list[dict]:
    """按时间间隔抽稀，并保证首尾点在内、点数不超过上限。"""
    if not points:
        return []
    picked: list[dict] = [points[0]]
    last_ts = points[0]["ts"]
    for p in points[1:]:
        if p["ts"] - last_ts >= minutes * 60:
            picked.append(p)
            last_ts = p["ts"]
    if picked[-1]["ts"] != points[-1]["ts"]:
        picked.append(points[-1])
    if len(picked) > max_points:
        step = math.ceil(len(picked) / max_points)
        picked = picked[::step]
        if picked[-1] is not points[-1]:
            picked.append(points[-1])
    return picked


def _day_bounds(ts: int) -> tuple[int, int]:
    day0 = int(ts // 86400 * 86400)
    return day0, day0 + 86399


def _tile_of(lat: float, lon: float) -> tuple[float, float]:
    return (math.floor(lat / TILE_DEG) * TILE_DEG, math.floor(lon / TILE_DEG) * TILE_DEG)


def _group(points: list[dict]) -> dict[tuple[float, float, int], list[dict]]:
    """按 (瓦片纬度, 瓦片经度, UTC 日) 分组：一次请求覆盖一片海一天。"""
    groups: dict[tuple[float, float, int], list[dict]] = {}
    for p in points:
        tlat, tlon = _tile_of(p["lat"], p["lon"])
        day0, _ = _day_bounds(p["ts"])
        groups.setdefault((tlat, tlon, day0), []).append(p)
    return groups


# --------------------------------------------------------------------------
# 瓦片抓取
# --------------------------------------------------------------------------
def _tile_path(dataset_key: str, tlat: float, tlon: float, day0: int) -> Path:
    product = PRODUCTS[dataset_key]
    if product["tile_mode"] == "clim":
        # 气候态瓦片不按日切：一个瓦片里装 12 个月的历史平均，全天候复用
        return CACHE_DIR / f"{product['short']}_{tlat:+.1f}_{tlon:+.1f}_clim.nc"
    stamp = time.strftime("%Y%m%d", time.gmtime(day0))
    return CACHE_DIR / f"{product['short']}_{tlat:+.1f}_{tlon:+.1f}_{stamp}.nc"


def _tile_usable(dataset_key: str, tlat: float, tlon: float, day0: int) -> Path | None:
    """瓦片是否可以直接复用。

    同时接受两种情况：DB 里有登记记录（正常路径），或本地已有一个足够新的文件
    （上一轮抓取成功但登记前进程被杀，避免重复下载 30 秒）。
    气候态瓦片用产品自己的 TTL（半年），实时瓦片用 6 小时。
    """
    path = _tile_path(dataset_key, tlat, tlon, day0)
    if not path.exists() or path.stat().st_size < 1024:
        return None
    with closing(connect(readonly=True)) as conn:
        row = conn.execute(
            "SELECT created_at FROM ocean_cache_tiles WHERE tile_id=?",
            (_tile_id(dataset_key, tlat, tlon, day0),),
        ).fetchone()
    stamped = (row["created_at"] if row else None) or int(path.stat().st_mtime)
    if time.time() - stamped > PRODUCTS[dataset_key]["tile_ttl_s"]:
        return None
    return path


def _tile_id(dataset_key: str, tlat: float, tlon: float, day0: int) -> str:
    if PRODUCTS[dataset_key]["tile_mode"] == "clim":
        return f"{PRODUCTS[dataset_key]['dataset_id']}|{tlat:+.1f}|{tlon:+.1f}|clim"
    return f"{PRODUCTS[dataset_key]['dataset_id']}|{tlat:+.1f}|{tlon:+.1f}|{day0}"


def _fetch_tile(dataset_key: str, tlat: float, tlon: float, day0: int) -> Path:
    """抓一片海一天的环境数据，落成 NetCDF。

    两条通道：
      - cmems：Copernicus Marine（物理/海浪/BGC/风场 L4）
      - cds  ：Copernicus Climate Data Store（ERA5 再分析，需 CDS API key）
    """
    product = PRODUCTS[dataset_key]
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    path = _tile_path(dataset_key, tlat, tlon, day0)

    if product.get("channel") == "cds":
        _fetch_tile_cds(dataset_key, tlat, tlon, day0, path)
    elif product.get("channel") == "gfs":
        _fetch_tile_gfs(dataset_key, tlat, tlon, day0, path)
    else:
        _fetch_tile_cmems(dataset_key, tlat, tlon, day0, path)

    if not path.exists() or path.stat().st_size < 1024:
        raise RuntimeError("未产出数据文件（可能该时段产品尚未覆盖）")

    with closing(connect()) as conn:
        conn.execute(
            """INSERT OR REPLACE INTO ocean_cache_tiles
               (tile_id, product, variables, t0, t1, lat_min, lat_max, lon_min, lon_max,
                path, bytes, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                _tile_id(dataset_key, tlat, tlon, day0),
                product["dataset_id"],
                ",".join(product.get("request_variables") or product["variables"].keys()),
                day0, day0 + 86399,
                tlat, tlat + TILE_DEG, tlon, tlon + TILE_DEG,
                str(path), path.stat().st_size, int(time.time()),
            ),
        )
        conn.commit()
    return path


def _fetch_tile_cmems(dataset_key: str, tlat: float, tlon: float,
                      day0: int, path: Path) -> None:
    import copernicusmarine

    product = PRODUCTS[dataset_key]
    kwargs: dict = {
        "dataset_id": product["dataset_id"],
        # 请求变量与写入列是两回事：风场只请求 u/v，风速风向在匹配阶段派生
        "variables": list(product.get("request_variables") or product["variables"].keys()),
        "minimum_longitude": tlon,
        "maximum_longitude": tlon + TILE_DEG,
        "minimum_latitude": tlat,
        "maximum_latitude": tlat + TILE_DEG,
        "output_directory": str(CACHE_DIR),
        "output_filename": path.name,
        "overwrite": True,
        "disable_progress_bar": True,
    }
    # 海浪/风是 2D 场，传 depth 会被服务端拒绝；只有三维产品才带深度区间
    if product.get("depth"):
        kwargs["minimum_depth"] = product["depth"][0]
        kwargs["maximum_depth"] = product["depth"][1]

    if product["tile_mode"] == "clim":
        kwargs["start_datetime"] = CLIM_START
        kwargs["end_datetime"] = CLIM_END
    else:
        kwargs["start_datetime"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(day0))
        kwargs["end_datetime"] = time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(day0 + 86399))

    res = copernicusmarine.subset(**kwargs)
    status = getattr(res, "status", None)
    # copernicusmarine 返回的是 StatusCode 枚举（str() 形如 "StatusCode.SUCCESS" / "000"），
    # 不同版本表述不一致，所以只把「明确失败」当失败，成功与否最终以文件为准。
    status_text = str(getattr(status, "value", status) or "").upper()
    if status_text and status_text not in ("000", "0", "SUCCESS", "STATUSCODE.SUCCESS"):
        raise RuntimeError(
            f"Copernicus 返回异常状态 {status_text}: {getattr(res, 'message', '')}"
        )


# --------------------------------------------------------------------------
# CDS / ERA5 通道
# --------------------------------------------------------------------------
CDS_URL = "https://cds.climate.copernicus.eu/api"
# CDS 的 API key 放在仓库根目录 api.txt 的第三行（第一、二行是别的服务）。
# 只在需要时读取，且不落日志、不进环境变量以外的任何地方。
CDS_KEY_FILE = ROOT / "api.txt"
CDS_KEY_LINE = 3


def _cds_api_key() -> str | None:
    """取 CDS API key。优先环境变量，其次 api.txt 第三行。"""
    env = os.environ.get("CDSAPI_KEY")
    if env:
        return env.strip()
    try:
        lines = CDS_KEY_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return None
    if len(lines) < CDS_KEY_LINE:
        return None
    key = lines[CDS_KEY_LINE - 1].strip()
    return key or None


def _fetch_tile_cds(dataset_key: str, tlat: float, tlon: float,
                    day0: int, path: Path) -> None:
    """ERA5 单层再分析：一片海、一天、逐时。"""
    try:
        import cdsapi
    except ImportError as exc:  # pragma: no cover - 环境问题
        raise RuntimeError("缺少 cdsapi，无法访问 ERA5") from exc

    key = _cds_api_key()
    if not key:
        raise RuntimeError(f"未找到 CDS API key（{CDS_KEY_FILE} 第 {CDS_KEY_LINE} 行或 CDSAPI_KEY）")
    # cdsapi 从环境变量读凭证，这样不用写 ~/.cdsapirc
    os.environ["CDSAPI_URL"] = CDS_URL
    os.environ["CDSAPI_KEY"] = key

    product = PRODUCTS[dataset_key]
    day = time.gmtime(day0)
    client = cdsapi.Client(url=CDS_URL, key=key, quiet=True)

    # CDS 的 area 顺序是 [North, West, South, East]，和 CMEMS 的 min/max 写法不一致
    client.retrieve(
        product["dataset_id"],
        {
            "product_type": "reanalysis",
            "variable": list(product.get("request_variables") or []),
            "year": f"{day.tm_year:04d}",
            "month": f"{day.tm_mon:02d}",
            "day": f"{day.tm_mday:02d}",
            "time": [f"{h:02d}:00" for h in range(24)],
            "area": [tlat + TILE_DEG, tlon, tlat, tlon + TILE_DEG],
            "format": "netcdf",
        },
        str(path),
    )


# --------------------------------------------------------------------------
# NOAA GFS 通道（近实时 10 m 风）
# --------------------------------------------------------------------------
GFS_FILTER_URL = "https://nomads.ncep.noaa.gov/cgi-bin/filter_gfs_0p25.pl"
# GFS 每 6 小时一个 cycle，预报步长 1 小时。取该 cycle 的 f000 与 f003，
# 就能让「预报时效」不超过 3 小时 —— 再往后误差会明显大于分析场。
GFS_CYCLES = (0, 6, 12, 18)
GFS_STEPS = (0, 3)


def _gfs_grib_url(cycle_ts: int, step_h: int, tlat: float, tlon: float) -> str:
    c = time.gmtime(cycle_ts)
    return (
        f"{GFS_FILTER_URL}?dir=%2Fgfs.{c.tm_year:04d}{c.tm_mon:02d}{c.tm_mday:02d}"
        f"%2F{c.tm_hour:02d}%2Fatmos"
        f"&file=gfs.t{c.tm_hour:02d}z.pgrb2.0p25.f{step_h:03d}"
        "&var_UGRD=on&var_VGRD=on&lev_10_m_above_ground=on"
        f"&subregion=&leftlon={tlon:g}&rightlon={tlon + TILE_DEG:g}"
        f"&toplat={tlat + TILE_DEG:g}&bottomlat={tlat:g}"
    )


def _fetch_tile_gfs(dataset_key: str, tlat: float, tlon: float,
                    day0: int, path: Path) -> None:
    """把一天的风场拼成一个 NetCDF。

    GFS 的区域子集接口一次只给一个文件（一个 cycle + 一个 step），
    所以一天要 4 个 cycle × 2 个 step = 8 次小请求，总共几 KB。
    这比下载全球场（约 80 MB/天）划算三个数量级。
    """
    import urllib.error
    import urllib.request

    xr = _lazy_import()
    if xr is None:
        raise RuntimeError(_IMPORT_ERROR or "xarray 不可用")
    import numpy as np

    frames_u: list[tuple[int, "np.ndarray"]] = []
    frames_v: list[tuple[int, "np.ndarray"]] = []
    lat_ref = lon_ref = None
    failures = 0
    last_error = None
    skipped_future = 0
    fetched = 0

    for ch in GFS_CYCLES:
        cycle_ts = day0 + ch * 3600
        for step_h in GFS_STEPS:
            valid_ts = cycle_ts + step_h * 3600
            # 未来的时刻还没有场，请求必然 404，直接跳过
            if valid_ts > int(time.time()):
                skipped_future += 1
                continue
            url = _gfs_grib_url(cycle_ts, step_h, tlat, tlon)
            try:
                req = urllib.request.Request(url, headers={"User-Agent": "FishEye/0.4"})
                with urllib.request.urlopen(req, timeout=45) as resp:
                    raw = resp.read()
            except (urllib.error.HTTPError, urllib.error.URLError, OSError) as exc:
                failures += 1
                last_error = f"下载失败 {type(exc).__name__}: {exc}"
                continue
            if raw[:4] != b"GRIB":
                failures += 1
                last_error = "返回内容不是 GRIB"
                continue
            fetched += 1

            tmp = path.with_suffix(f".{cycle_ts}_{step_h}.grib2")
            tmp.write_bytes(raw)
            try:
                for sn, bucket in (("10u", frames_u), ("10v", frames_v)):
                    ds = xr.open_dataset(
                        tmp, engine="cfgrib",
                        backend_kwargs={"filter_by_keys": {"shortName": sn}, "indexpath": ""},
                    )
                    try:
                        if lat_ref is None:
                            lat_ref = ds["latitude"].values
                            lon_ref = ds["longitude"].values
                        # filter_by_keys 用的是 shortName，但 cfgrib 返回的变量名是
                        # u10 / v10（GRIB 里的 name），两者不是一回事，别拿 sn 去索引。
                        name = next(iter(ds.data_vars))
                        bucket.append((valid_ts, np.asarray(ds[name].values, dtype="float32")))
                    finally:
                        ds.close()
            except Exception as exc:
                failures += 1
                last_error = f"GRIB 解析失败 {type(exc).__name__}: {exc}"
            finally:
                tmp.unlink(missing_ok=True)

    if not frames_u or not frames_v:
        raise RuntimeError(
            f"GFS 未取到任何时次（成功下载 {fetched} 个、失败 {failures} 个、"
            f"跳过未来时刻 {skipped_future} 个）"
            + (f"；最后一次错误：{last_error}" if last_error else "")
        )

    if len(frames_u) != len(frames_v):
        raise RuntimeError("GFS 的 u/v 时次数量不一致，无法配对")

    order = sorted(range(len(frames_u)), key=lambda i: frames_u[i][0])
    # 存成 datetime64 而不是裸整数：否则读回来会被当数值坐标，
    # 和 _match_tile 里的 datetime64 相减直接报 UFuncTypeError。
    times = np.array([frames_u[i][0] for i in order], dtype="datetime64[s]")
    u = np.stack([frames_u[i][1] for i in order])
    v = np.stack([frames_v[i][1] for i in order])

    ds_out = xr.Dataset(
        {
            "u10": (("time", "latitude", "longitude"), u),
            "v10": (("time", "latitude", "longitude"), v),
        },
        coords={
            "time": times,
            "latitude": lat_ref,
            "longitude": lon_ref,
        },
    )
    ds_out.attrs["source"] = "NOAA GFS 0.25° via NOMADS filter"
    ds_out.attrs["note"] = "3 小时间隔；每个时刻由不晚于它的 cycle 起报，预报时效不超过 3 小时"
    ds_out.to_netcdf(path)
    ds_out.close()


# --------------------------------------------------------------------------
# 匹配
# --------------------------------------------------------------------------
def _month_of(ts: int) -> int:
    return int(time.gmtime(ts).tm_mon)


def _match_tile(dataset_key: str, path: Path, points: list[dict]) -> list[dict]:
    """把点匹配到最近像元、最近时刻，返回带 quality 标记的记录。

    两种模式：
      - day  ：按 UTC 日切片的实时/预报场，取最近时刻，滞后超限判 out_of_range
      - clim ：历史气候态，取「同月份」的多年平均，滞后概念不适用，lag_hours 必须为 None
    """
    xr = _lazy_import()
    if xr is None:
        raise RuntimeError(_IMPORT_ERROR or "xarray 不可用")

    product = PRODUCTS[dataset_key]
    mode = product["tile_mode"]
    # CMEMS 的时间维叫 time，CDS/ERA5 的 NetCDF 叫 valid_time。
    # 硬编码 "time" 会让 ERA5 瓦片直接被判成「没有任何时刻」。
    td = product.get("time_dim") or "time"
    ds = xr.open_dataset(path)
    try:
        if td not in ds.variables or ds[td].size == 0:
            return [
                {
                    "mmsi": p["mmsi"], "ts": p["ts"], "lat": p["lat"], "lon": p["lon"],
                    "values": {}, "source_time": None, "lag_hours": None,
                    "quality": "out_of_range", "note": "该瓦片内没有任何环境时刻",
                    "basis": product["basis"],
                }
                for p in points
            ]

        depth_dim = "depth" if "depth" in ds.dims else None

        def _scalar(sel, src: str, p: dict) -> float | None:
            if src not in sel:
                return None
            try:
                v = float(sel[src].sel(latitude=p["lat"], longitude=p["lon"], method="nearest"))
            except Exception:
                return None
            if math.isnan(v) or abs(v) > 1e6:
                return None
            return round(v, 4)

        def _wind(vals: dict, sel, p: dict) -> None:
            """把 u/v 分量派生成风速与来向。数据集只给分量，风速风向不存在原生态。"""
            uv = product.get("wind_uv")
            if not uv:
                return
            u = _scalar(sel, uv["u"], p)
            v = _scalar(sel, uv["v"], p)
            speed, deg = wind_from_uv(u, v)
            vals["wind_u_m_s"] = u
            vals["wind_v_m_s"] = v
            vals["wind_speed"] = speed
            vals["wind_dir_deg"] = deg

        if mode == "clim":
            # 先在像元上按月份聚合，再逐点取当月。这样一年只读一次文件，
            # 而不是每个点都重算一遍 60 个月的平均。
            monthly = ds.groupby(f"{td}.month").mean(dim=td, keep_attrs=True)

            def _pick_clim(p: dict) -> dict:
                m = _month_of(p["ts"])
                try:
                    sel = monthly.sel(month=m)
                except Exception:
                    return {
                        "mmsi": p["mmsi"], "ts": p["ts"], "lat": p["lat"], "lon": p["lon"],
                        "values": {}, "source_time": None, "lag_hours": None,
                        "quality": "out_of_range", "basis": product["basis"],
                        "note": f"该瓦片没有 {m} 月的气候态样本",
                    }
                if depth_dim:
                    sel = sel.isel(depth=0)
                vals: dict[str, float | None] = {}
                for src, dst in product["variables"].items():
                    vals[dst] = _scalar(sel, src, p)
                _wind(vals, sel, p)
                quality = "ok" if any(v is not None for v in vals.values()) else "land_mask"
                return {
                    "mmsi": p["mmsi"], "ts": p["ts"], "lat": p["lat"], "lon": p["lon"],
                    "values": vals,
                    "source_time": f"{CLIM_YEARS[0]}-{CLIM_YEARS[1]} 年 {m:02d} 月气候态",
                    # 气候态没有「滞后多少小时」这回事，给 null 而不是 0，避免被当成实时值
                    "lag_hours": None,
                    "quality": quality,
                    "basis": "climatology",
                    "clim_month": m,
                    "clim_years": f"{CLIM_YEARS[0]}-{CLIM_YEARS[1]}",
                    "note": None if quality == "ok" else "该像元落在海陆掩膜内，无气候态值",
                }

            return [_pick_clim(p) for p in points]

        # 时间轴统一归一化成「秒级 int64」再匹配：
        # CMEMS 给 datetime64、ERA5 给 datetime64、自拼的 GFS 瓦片可能是数值坐标，
        # 三种表示混着算必然出类型错误，先收敛到唯一表示。
        raw_t = ds[td].values
        import numpy as np

        if np.issubdtype(raw_t.dtype, np.datetime64):
            available = raw_t.astype("datetime64[s]").astype("int64")
        else:
            available = raw_t.astype("int64")
        max_lag = float(product.get("max_lag_hours") or MAX_LAG_HOURS)

        def _pick(p: dict) -> dict:
            idx = int(np.abs(available - int(p["ts"])).argmin())
            actual_ts = int(available[idx])
            lag_h = abs(actual_ts - p["ts"]) / 3600.0

            if lag_h > max_lag:
                return {
                    "mmsi": p["mmsi"], "ts": p["ts"], "lat": p["lat"], "lon": p["lon"],
                    "values": {}, "source_time": None, "lag_hours": round(lag_h, 1),
                    "quality": "out_of_range", "basis": product["basis"],
                    "note": f"最近可用环境时刻滞后 {lag_h:.0f} 小时，超过 {max_lag:.0f} 小时上限",
                }

            sel = ds.isel({td: idx})
            if depth_dim:
                sel = sel.isel(depth=0)
            vals: dict[str, float | None] = {}
            for src, dst in product["variables"].items():
                vals[dst] = _scalar(sel, src, p)
            _wind(vals, sel, p)

            quality = "ok" if any(v is not None for v in vals.values()) else "land_mask"
            note = None if quality == "ok" else "该像元落在海陆掩膜内，无环境值"
            return {
                "mmsi": p["mmsi"], "ts": p["ts"], "lat": p["lat"], "lon": p["lon"],
                "values": vals,
                "source_time": time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(actual_ts)),
                "lag_hours": round(lag_h, 1),
                "quality": quality,
                "basis": product["basis"],
                "note": note,
            }

        return [_pick(p) for p in points]
    finally:
        ds.close()


# --------------------------------------------------------------------------
# 主入口
# --------------------------------------------------------------------------
# ocean_features 里属于「环境数值」的列。写入时按列合并，不能整行覆盖：
# 物理场和海浪是两次独立请求，整行 REPLACE 会把先写的那批数值清成 NULL。
FEATURE_COLUMNS = (
    "sst_c", "sss_psu", "ssh_m", "mld_m",
    "u_m_s", "v_m_s",
    "chl_mg_m3",
    "wave_height_m", "wave_dir_deg", "wave_period_s",
    "wind_speed", "wind_dir_deg", "wind_u_m_s", "wind_v_m_s",
)


def _column_owners() -> dict[str, set[str]]:
    owners: dict[str, set[str]] = {}
    for key, prod in PRODUCTS.items():
        for col in set(prod["variables"].values()):
            owners.setdefault(col, set()).add(key)
    return owners


# 哪一列被几个产品共用。wind / wind_gfs / wind_era5 都写同一批 wind_* 列，
# 所以「这列有值」不能反过来说「这个产品跑过」——那会让元数据凭空多出两个风源。
_COLUMN_OWNERS = _column_owners()


def _has_exclusive_columns(dataset_key: str) -> bool:
    """该产品的所有列是否只有它自己会写。共享列的产品只能靠 products 字段认定。"""
    return all(
        len(_COLUMN_OWNERS.get(c, set())) == 1
        for c in PRODUCTS[dataset_key]["variables"].values()
    )


def _persist(rows: list[dict], dataset_key: str) -> None:
    """把匹配结果写进 ocean_features。

    关键点：同一个 (mmsi, ts) 可能先后被 physics / waves / bgc_clim 写过，
    所以先读出现有行，只覆盖本产品负责的列，其余原样保留。
    """
    if not rows:
        return

    product = PRODUCTS[dataset_key]
    short = product["short"]
    now = int(time.time())
    mmsi = rows[0]["mmsi"]
    ts_list = sorted({r["ts"] for r in rows})

    existing: dict[int, dict] = {}
    with closing(connect()) as conn:
        for i in range(0, len(ts_list), 400):
            chunk = ts_list[i:i + 400]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM ocean_features WHERE mmsi=? AND ts IN ({marks})",
                (mmsi, *chunk),
            ):
                existing[row["ts"]] = dict(row)

    payload = []
    for r in rows:
        cur = existing.get(r["ts"]) or {}
        vals = {c: cur.get(c) for c in FEATURE_COLUMNS}
        for k, v in (r.get("values") or {}).items():
            if v is not None:
                vals[k] = v

        try:
            quality_json = json.loads(cur.get("quality_json") or "{}") or {}
        except (TypeError, ValueError):
            quality_json = {}
        quality_json[short] = {
            "quality": r.get("quality"),
            "basis": r.get("basis") or product["basis"],
            "source_time": r.get("source_time"),
            "lag_hours": r.get("lag_hours"),
        }

        products = [p for p in (cur.get("products") or "").split(",") if p]
        # 再从实际有值的列反推一遍：历史行没有 products 字段时靠这一步补全，
        # 否则会出现「明明有 SST 却只标了 waves」的错误元数据。
        # 只对「列不与他人共享」的产品做反推：三个风产品写同一批 wind_* 列，
        # 反推会把没跑过的风源也算进来，元数据就撒慌了。
        for key, prod in PRODUCTS.items():
            if not _has_exclusive_columns(key):
                continue
            if prod["short"] and any(vals.get(c) is not None for c in prod["variables"].values()):
                if prod["short"] not in products:
                    products.append(prod["short"])
        if short not in products:
            products.append(short)

        # 顶层 source_time / lag_hours 只跟随实时产品：气候态没有「滞后」概念，
        # 写进去会把「2019–2023 年 10 月平均」伪装成「今天 03 时的值」。
        if product["basis"] == "climatology":
            source_time = cur.get("source_time")
            lag_hours = cur.get("lag_hours")
        else:
            source_time = r.get("source_time")
            lag_hours = r.get("lag_hours")

        if vals.get("chl_mg_m3") is not None and product["basis"] == "climatology":
            chl_basis = "climatology"
            chl_ref = f"{CLIM_YEARS[0]}-{CLIM_YEARS[1]}"
        else:
            chl_basis = cur.get("chl_basis")
            chl_ref = cur.get("chl_ref")

        # 风的来源必须逐点留下痕迹：L4 是卫星散射计融合观测，ERA5 是再分析模式。
        # 两者都叫「风」，混在一起就再也分不清了。
        if product.get("wind_uv") and vals.get("wind_speed") is not None:
            wind_basis = product["basis"]
        else:
            wind_basis = cur.get("wind_basis")

        quality = "ok" if any(vals[c] is not None for c in FEATURE_COLUMNS) else (
            r.get("quality") or "missing"
        )

        payload.append((
            r["mmsi"], r["ts"], r["lat"], r["lon"],
            vals["sst_c"], vals["sss_psu"], vals["ssh_m"], vals["mld_m"],
            vals["u_m_s"], vals["v_m_s"], vals["chl_mg_m3"],
            vals["wind_speed"], vals["wind_dir_deg"], vals["wind_u_m_s"], vals["wind_v_m_s"],
            vals["wave_height_m"], vals["wave_dir_deg"], vals["wave_period_s"],
            product["dataset_id"],
            source_time, lag_hours,
            f"{short}-{product['version']}",
            now, quality,
            chl_basis, chl_ref, wind_basis,
            ",".join(products),
            json.dumps(quality_json, ensure_ascii=False),
        ))

    with closing(connect()) as conn:
        conn.executemany(
            """INSERT OR REPLACE INTO ocean_features
               (mmsi, ts, lat, lon,
                sst_c, sss_psu, ssh_m, mld_m, u_m_s, v_m_s, chl_mg_m3,
                wind_speed, wind_dir_deg, wind_u_m_s, wind_v_m_s,
                wave_height_m, wave_dir_deg, wave_period_s,
                product, source_time, lag_hours, data_version, fetched_at, quality,
                chl_basis, chl_ref, wind_basis, products, quality_json)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            payload,
        )
        conn.commit()


def _row_has_product(row: dict, dataset_key: str) -> bool:
    """这一行是否已经包含某个产品的值。

    只判断「行存在」是不够的：physics 先写、waves 后写同一个 (mmsi, ts)，
    如果只看行存在，waves 会误以为全部命中缓存而一个字都不抓。
    更隐蔽的是三个风产品写同一批 wind_* 列：若靠「列有值」判断，
    跑过 ERA5 之后 GFS 会误判成「已缓存」而直接跳过。所以共享列的产品只认 products 字段。
    """
    short = PRODUCTS[dataset_key]["short"]
    recorded = [p for p in (row.get("products") or "").split(",") if p]
    if recorded:
        return short in recorded
    # 历史行没有 products 字段，只能靠列反推；共享列的产品反推不可靠，按未命中处理
    if not _has_exclusive_columns(dataset_key):
        return False
    return any(row.get(c) is not None for c in PRODUCTS[dataset_key]["variables"].values())


def _load_cached(mmsi: int, ts_list: list[int], dataset_key: str | None = None) -> dict[int, dict]:
    if not ts_list:
        return {}
    cutoff = int(time.time()) - CACHE_TTL_S
    out: dict[int, dict] = {}
    with closing(connect(readonly=True)) as conn:
        for i in range(0, len(ts_list), 500):
            chunk = ts_list[i:i + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(
                f"SELECT * FROM ocean_features WHERE mmsi=? AND ts IN ({marks}) AND fetched_at>=?",
                (mmsi, *chunk, cutoff),
            ):
                rec = dict(row)
                if dataset_key and not _row_has_product(rec, dataset_key):
                    continue
                out[row["ts"]] = rec
    return out


def _stat(vals: list[float]) -> dict | None:
    if not vals:
        return None
    return {
        "min": round(min(vals), 3),
        "median": round(statistics.median(vals), 3),
        "max": round(max(vals), 3),
        "n": len(vals),
    }


def _count_quality(rows: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in rows:
        q = r.get("quality") or "unknown"
        counts[q] = counts.get(q, 0) + 1
    return counts


def _quality_json(raw: str | None) -> dict:
    try:
        return json.loads(raw or "{}") or {}
    except (TypeError, ValueError):
        return {}


def _current_speed(r: dict) -> float | None:
    u, v = r.get("u_m_s"), r.get("v_m_s")
    if u is None or v is None:
        return None
    return round(math.hypot(u, v), 4)


def _series_row(r: dict) -> dict:
    """一行环境序列。叶绿素必须带上 basis，避免被当成实时值。"""
    return {
        "ts": r["ts"],
        "lat": round(r["lat"], 4) if r.get("lat") is not None else None,
        "lon": round(r["lon"], 4) if r.get("lon") is not None else None,
        "sst_c": r.get("sst_c"),
        "u_m_s": r.get("u_m_s"),
        "v_m_s": r.get("v_m_s"),
        "current_speed_m_s": _current_speed(r),
        "ssh_m": r.get("ssh_m"),
        "wave_height_m": r.get("wave_height_m"),
        "wave_dir_deg": r.get("wave_dir_deg"),
        "wave_period_s": r.get("wave_period_s"),
        "sea_state": sea_state_of(r.get("wave_height_m")),
        "wind_speed_m_s": r.get("wind_speed"),
        "wind_dir_deg": r.get("wind_dir_deg"),
        "wind_dir_text": wind_dir_text(r.get("wind_dir_deg")),
        "wind_u_m_s": r.get("wind_u_m_s"),
        "wind_v_m_s": r.get("wind_v_m_s"),
        "wind_beaufort": beaufort_of(r.get("wind_speed")),
        "wind_basis": r.get("wind_basis"),
        "chl_mg_m3": r.get("chl_mg_m3"),
        "chl_basis": r.get("chl_basis"),
        "chl_ref": r.get("chl_ref"),
        "source_time": r.get("source_time"),
        "lag_hours": r.get("lag_hours"),
        "quality": r.get("quality"),
        "products": [p for p in (r.get("products") or "").split(",") if p],
        "quality_by_product": _quality_json(r.get("quality_json")),
    }


def _summarize(ordered: list[dict], sampled_n: int) -> dict:
    """把序列压成概要。每个变量各自算 n，不共用「ok 行数」，
    否则一个变量缺测会把另一个变量的覆盖情况也说错。"""
    def _vals(key: str) -> list[float]:
        return [r[key] for r in ordered if r.get(key) is not None]

    waves = _vals("wave_height_m")
    sea_counts: dict[str, int] = {}
    for w in waves:
        code = sea_state_of(w)["code"]
        sea_counts[code] = sea_counts.get(code, 0) + 1
    dominant = max(sea_counts.items(), key=lambda kv: kv[1])[0] if sea_counts else None

    lags = [r.get("lag_hours") for r in ordered if r.get("lag_hours") is not None]
    chl_rows = [r for r in ordered if r.get("chl_mg_m3") is not None]

    winds = _vals("wind_speed")
    # 蒲福风级的分布，和各变量一样按「有值的点」算，不拿缺测当 0 级
    force_counts: dict[str, int] = {}
    for w in winds:
        code = str(beaufort_of(w)["force"])
        force_counts[code] = force_counts.get(code, 0) + 1
    dominant_force = max(force_counts.items(), key=lambda kv: kv[1])[0] if force_counts else None

    # 风向是循环量，取众数会骗人（350° 和 10° 只差 20°，但会算成两个方向）。
    # 所以先用 u/v 分量求平均，再反算方向。
    us = [r["wind_u_m_s"] for r in ordered if r.get("wind_u_m_s") is not None]
    vs = [r["wind_v_m_s"] for r in ordered if r.get("wind_v_m_s") is not None]
    mean_dir = None
    if us and len(us) == len(vs):
        mu, mv = statistics.fmean(us), statistics.fmean(vs)
        _, mean_dir = wind_from_uv(mu, mv)
        # 分量互相抵消说明这段时间风向转了整整一圈以上，平均值没有意义
        if math.hypot(mu, mv) < 0.3 * (statistics.fmean(winds) if winds else 0):
            mean_dir = None

    wind_rows = [r for r in ordered if r.get("wind_speed") is not None]
    wind_bases = sorted({r.get("wind_basis") for r in wind_rows if r.get("wind_basis")})

    return {
        "points_sampled": sampled_n,
        "points_matched": len(ordered),
        "sst_c": _stat(_vals("sst_c")),
        "current_speed_m_s": _stat([s for s in (_current_speed(r) for r in ordered) if s is not None]),
        "ssh_m": _stat(_vals("ssh_m")),
        "wave_height_m": _stat(waves),
        "wave_period_s": _stat(_vals("wave_period_s")),
        "sea_state": {
            "dominant": dominant,
            "dominant_label": sea_state_of(next((w for w in waves if sea_state_of(w)["code"] == dominant), None))["label"]
            if dominant else None,
            "distribution": sea_counts,
            "rule": "道格拉斯海况表，按有效波高查表，未做任何推断",
        },
        "wind_speed_m_s": _stat(winds),
        "wind_beaufort": {
            "dominant_force": int(dominant_force) if dominant_force is not None else None,
            "dominant_label": beaufort_of(next((w for w in winds if str(beaufort_of(w)["force"]) == dominant_force), None))["label"]
            if dominant_force is not None else None,
            "distribution": force_counts,
            "rule": "蒲福风级表，按风速查表，未做任何推断",
        },
        "wind_direction": {
            "mean_deg": mean_dir,
            "mean_text": wind_dir_text(mean_dir),
            "convention": "来向（风从哪个方向吹来），正北顺时针",
            "note": None if mean_dir is not None else "风向变化过大或全缺测，不给平均方向",
        },
        "wind_basis": wind_bases or None,
        "chl_mg_m3": _stat([r["chl_mg_m3"] for r in chl_rows]),
        "chl_basis": "climatology" if chl_rows else None,
        "chl_ref_years": chl_rows[0].get("chl_ref") if chl_rows else None,
        "lag_hours": {
            "median": round(statistics.median(lags), 2) if lags else None,
            "max": round(max(lags), 2) if lags else None,
        },
        "quality_counts": _count_quality(ordered),
    }


def read_series(mmsi: int, hours: int = 24) -> dict:
    """只读：把该船窗口内「已经算过」的环境特征按时间排成序列，绝不访问网络。

    前端面板与智能体都先调它；没有缓存时返回 `no_cache` 并说明要提交任务，
    不会偷偷去下载瓦片把请求挂死。
    """
    mmsi = int(mmsi)
    hours = max(1, min(int(hours), 168))

    if _lazy_import() is None:
        return {"status": "unavailable", "reason": _IMPORT_ERROR or "依赖缺失",
                "available": False}

    try:
        track = vs.vessel_track(mmsi, hours=hours, max_points=8000)
    except Exception as exc:
        return {"status": "error", "reason": f"{type(exc).__name__}: {exc}"}

    raw = track.get("track") or []
    if not raw:
        return {"status": "no_track", "mmsi": mmsi, "hours": hours,
                "message": "该船在窗口内没有轨迹点。无数据 ≠ 无活动。"}

    sampled = _sample([dict(p, mmsi=mmsi) for p in raw])
    rows = _load_cached(mmsi, [p["ts"] for p in sampled])
    if not rows:
        return {
            "status": "no_cache",
            "mmsi": mmsi, "hours": hours,
            "points_sampled": len(sampled),
            "available": False,
            "message": "该船这段时间还没有提取过环境特征，需要提交异步任务（约 20–60 秒）。",
            "suggest": f"POST /api/analysis/ocean/tracks?mmsi={mmsi}&hours={hours}",
        }

    ordered = [rows[p["ts"]] for p in sampled if p["ts"] in rows]
    summary = _summarize(ordered, len(sampled))
    return {
        "status": "ok" if summary["quality_counts"].get("ok") else "cached_but_unusable",
        "available": True,
        "mmsi": mmsi, "hours": hours,
        "model_version": MODEL_VERSION,
        "summary": summary,
        # 兼容旧字段，智能体和外部脚本可能已经在用
        "sst_c": summary["sst_c"],
        "current_speed_m_s": summary["current_speed_m_s"],
        "lag_hours": summary["lag_hours"],
        "quality_counts": summary["quality_counts"],
        "series": [_series_row(r) for r in ordered],
        "caveat": _CAVEAT,
    }


def cached_summary(mmsi: int, hours: int = 24) -> dict:
    """向后兼容入口：等价于 read_series（只读、不联网）。"""
    return read_series(mmsi, hours=hours)


_CAVEAT = (
    "环境值是再分析/预报模式与卫星融合产品在最近像元、最近时刻的取值，"
    "不是该船位置上的实测；0.083°–0.25° 网格内所有船舶共享同一个环境值。"
    "滞后时间如实记录在 lag_hours 里；超出产品覆盖时段的点标记为 out_of_range，"
    "不做插值、不拿邻近时刻顶替。"
    "叶绿素 chl_basis=climatology 表示这是多年同月平均，不是当前浓度。"
    "风要看清 wind_basis：observation_l4 是卫星散射计融合，reanalysis 是 ERA5 模式，"
    "两者数值可以差到 1–2 m/s，不要当成同一个东西。"
    "**这只能说明「船经历过的环境」，不能说明「船偏好什么环境」**——"
    "要研究偏好必须构造「可选但未使用」的对照，本接口不提供该结论。"
)


def _run_product(mmsi: int, dataset_key: str, sampled: list[dict],
                 progress=None, lo: float = 0.0, hi: float = 1.0) -> dict:
    """跑一个产品的提取：缓存命中 → 缺的按瓦片抓 → 写库。

    返回该产品的执行报告（不返回数值，数值统一从库里回读，保证合并后的视图一致）。
    """
    product = PRODUCTS[dataset_key]

    def _p(frac: float) -> None:
        if progress:
            progress(lo + (hi - lo) * max(0.0, min(1.0, frac)))

    _p(0.02)
    cached = _load_cached(mmsi, [p["ts"] for p in sampled], dataset_key)
    todo = [p for p in sampled if p["ts"] not in cached]

    groups = _group(todo)
    new_tiles = 0
    skipped_tiles = 0
    errors: list[str] = []
    fresh: list[dict] = []
    total_groups = max(1, len(groups))
    # 不同产品的单瓦片成本差一个数量级：CMEMS 约 10 秒，CDS 排队约 3 分钟。
    # 用同一个上限会让 ERA5 跑到十几分钟，所以允许产品自己收紧。
    tile_budget = int(product.get("max_new_tiles") or MAX_NEW_TILES)

    for gi, ((tlat, tlon, day0), gpts) in enumerate(sorted(groups.items(), key=lambda kv: -len(kv[1]))):
        path = _tile_usable(dataset_key, tlat, tlon, day0)
        if path is None:
            if new_tiles >= tile_budget:
                skipped_tiles += 1
                continue
            try:
                path = _fetch_tile(dataset_key, tlat, tlon, day0)
                new_tiles += 1
            except Exception as exc:
                errors.append(f"瓦片 {tlat:+.0f},{tlon:+.0f} 抓取失败: {type(exc).__name__}: {exc}")
                continue
        try:
            fresh.extend(_match_tile(dataset_key, path, gpts))
        except Exception as exc:
            errors.append(f"瓦片匹配失败: {type(exc).__name__}: {exc}")
        _p(0.10 + 0.85 * (gi + 1) / total_groups)

    if fresh:
        _persist(fresh, dataset_key)

    # 只统计本产品负责的列，避免把 physics 的可用性算到 waves 头上
    mine = tuple(product["variables"].values())
    rows_now = _load_cached(mmsi, [p["ts"] for p in sampled], dataset_key)
    have = [r for r in rows_now.values() if any(r.get(c) is not None for c in mine)]

    note = None
    if skipped_tiles:
        note = (
            f"还有 {skipped_tiles} 个 1°×1° 瓦片超出单次抓取上限（{tile_budget} 个），"
            "本次未覆盖；再跑一次会接着补。"
        )
    if errors:
        note = (note + " " if note else "") + "部分瓦片失败：" + "；".join(errors[:3])

    return {
        "key": dataset_key,
        "dataset_id": product["dataset_id"],
        "label": product["label"],
        "version": product["version"],
        "basis": product["basis"],
        "units": product["units"],
        "native_resolution_deg": product["resolution_deg"],
        "temporal": product["temporal"],
        "status": "ok" if have else ("partial" if rows_now else "unavailable"),
        "points": {
            "sampled": len(sampled),
            "with_values": len(have),
            "from_cache": len(cached),
            "newly_fetched": len(fresh),
            "tiles_fetched": new_tiles,
            "tiles_skipped": skipped_tiles,
        },
        "errors": errors,
        "note": note,
    }


def _normalize_products(products) -> list[str]:
    if products is None:
        return list(DEFAULT_PRODUCTS)
    if isinstance(products, str):
        products = [p.strip() for p in products.split(",") if p.strip()]
    out: list[str] = []
    for p in products:
        if p not in PRODUCTS:
            raise ValueError(f"未知环境产品: {p}（可选：{', '.join(PRODUCTS)}）")
        if p not in out:
            out.append(p)
    return out or list(DEFAULT_PRODUCTS)


# 给 API 层用的公开别名：上层不该直接摸下划线函数
normalize_products = _normalize_products


def features_for_track(mmsi: int, hours: int = 24, products=None,
                       dataset_key: str | None = None, progress=None) -> dict:
    """提取某船轨迹上的环境特征（供异步 worker 调用）。

    products 可以是列表或逗号分隔字符串；dataset_key 是为兼容旧调用保留的单产品参数。
    """
    mmsi = int(mmsi)
    hours = max(1, min(int(hours), 168))
    if dataset_key and not products:
        products = [dataset_key]
    keys = _normalize_products(products)

    def _p(frac: float) -> None:
        if progress:
            progress(frac)

    _p(0.02)
    track = vs.vessel_track(mmsi, hours=hours, max_points=8000)
    raw = track.get("track") or []
    if not raw:
        return {
            "status": "no_track",
            "mmsi": mmsi, "hours": hours,
            "message": "该船在窗口内没有轨迹点，无法提取环境特征。无数据 ≠ 无活动。",
            "model_version": MODEL_VERSION,
        }

    sampled = _sample([dict(p, mmsi=mmsi) for p in raw])
    _p(0.06)

    reports = []
    span = 0.88 / max(1, len(keys))
    for i, key in enumerate(keys):
        reports.append(_run_product(
            mmsi, key, sampled, progress=progress,
            lo=0.06 + span * i, hi=0.06 + span * (i + 1),
        ))

    # 所有产品写完后统一回读：这样 series 是「合并后的真实库内状态」，
    # 而不是各产品各自的临时结果拼起来的假象。
    rows_now = _load_cached(mmsi, [p["ts"] for p in sampled])
    ordered = [rows_now[p["ts"]] for p in sampled if p["ts"] in rows_now]
    summary = _summarize(ordered, len(sampled))

    ok_products = [r["key"] for r in reports if r["status"] in ("ok", "partial")]
    status = "ok" if ok_products else ("partial" if ordered else "unavailable")

    notes = [f"{PRODUCTS[r['key']]['label']}：{r['note']}" for r in reports if r.get("note")]
    missing_products = [r["key"] for r in reports if r["status"] == "unavailable"]
    if missing_products:
        notes.append("未取到任何值的产品：" + ", ".join(missing_products))

    _p(1.0)
    first = reports[0] if reports else {}
    return {
        "status": status,
        "model_version": MODEL_VERSION,
        "mmsi": mmsi,
        "hours": hours,
        # 兼容旧字段：单个产品时代的结构
        "product": {
            "dataset_id": first.get("dataset_id"),
            "label": first.get("label"),
            "version": first.get("version"),
            "units": first.get("units"),
            "native_resolution_deg": first.get("native_resolution_deg"),
        },
        "products": reports,
        "points": {
            "track": len(raw),
            "sampled": len(sampled),
            "matched": len(ordered),
            "from_cache": sum(r["points"]["from_cache"] for r in reports),
            "newly_fetched": sum(r["points"]["newly_fetched"] for r in reports),
            "tiles_fetched": sum(r["points"]["tiles_fetched"] for r in reports),
            "quality_counts": summary["quality_counts"],
        },
        "summary": summary,
        "sst_c": summary["sst_c"],
        "current_speed_m_s": summary["current_speed_m_s"],
        "wave_height_m": summary["wave_height_m"],
        "chl_mg_m3": summary["chl_mg_m3"],
        "lag_hours": summary["lag_hours"],
        "series": [_series_row(r) for r in ordered],
        "note": " ".join(notes) if notes else None,
        "caveat": _CAVEAT,
    }
