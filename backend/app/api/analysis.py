"""分析类接口：行为时间线 / 航次 / 指纹 / 差异 / 观测质量 / 证据卡。

与 /api/vessels/{mmsi}/behavior 的区别：
- behavior 是 v0.1 的快速状态摘要，字段面向「现在在干什么」
- 本模块是 v0.2 的结构化分析层，字段面向「这段时间怎么过的、能不能信、
  和它自己平时比有什么不同」，所有结果都带 model_version 与可信度标记

这些接口的设计目标是：既给自家智能体调用，也能被外部脚本直接使用。
"""
from __future__ import annotations

import time

from fastapi import APIRouter, HTTPException, Query

from ..services import evidence as evidence_svc
from ..services import fingerprint as fp_svc
from ..services import jobs as job_svc
from ..services import ocean as ocean_svc
from ..services import reliability as rel_svc
from ..services import timeline as tl_svc
from ..services import trips as trip_svc

router = APIRouter(prefix="/api", tags=["analysis"])


@router.get("/vessels/{mmsi}/timeline")
def get_timeline(
    mmsi: int,
    hours: int = Query(24, ge=1, le=168, description="回看小时数"),
    compact: bool = Query(False, description="true 时返回精简版（供智能体使用）"),
):
    """行为时间线：时间连续、互不重叠的行为片段序列。

    片段标签分两层：activity（可直接观察）与 sub_activity（需上下文推断的候选）。
    confidence 恒为 null —— 未做概率校准前不给「概率」。
    """
    r = tl_svc.behavior_timeline(mmsi, hours=hours)
    if not r.get("found") and not r.get("points"):
        raise HTTPException(404, "无该 MMSI 的记录")
    return tl_svc.compact(r) if compact else r


@router.get("/vessels/{mmsi}/trips")
def get_trips(mmsi: int, hours: int = Query(168, ge=1, le=168)):
    """航次重建：按停泊与数据断裂切分，含每个航次的特征向量。"""
    return trip_svc.build_trips(mmsi, hours=hours)


@router.get("/vessels/{mmsi}/fingerprint")
def get_fingerprint(
    mmsi: int,
    days: int = Query(30, ge=1, le=90),
):
    """长期行为指纹。历史航次不足时返回 insufficient_history 并说明还差多少。"""
    return fp_svc.vessel_fingerprint(mmsi, lookback_days=days)


@router.get("/vessels/{mmsi}/compare")
def get_compare(
    mmsi: int,
    days: int = Query(30, ge=1, le=90),
    hours: int = Query(24, ge=1, le=168),
):
    """当前航次 vs 自身历史基线的差异，含稳健 Z 分数与变化点。"""
    return fp_svc.compare_vessel_behavior(mmsi, lookback_days=days, hours=hours)


@router.get("/vessels/{mmsi}/quality")
def get_quality(mmsi: int, hours: int = Query(24, ge=1, le=168)):
    """观测质量：覆盖率、报文间隔、缺口、上游链路状态与分级建议。"""
    return rel_svc.observation_quality(mmsi, hours=hours)


@router.get("/vessels/{mmsi}/evidence")
def get_evidence(
    mmsi: int,
    kind: str = Query("gap", pattern="^(gap|fishing)$"),
    ts: int | None = Query(None, description="事件起始时间戳；缺省取最近一条"),
    hours: int = Query(24, ge=1, le=168),
    limit: int = Query(5, ge=1, le=20),
):
    """事件证据卡：观测事实、替代解释、无法解释的部分、复核状态。

    列表模式下「没有事件」是正常情况，返回 200 + 空列表；
    只有指定了具体 `ts` 却查不到时才 404（那是真的找不到这张卡）。
    """
    if kind == "gap":
        if ts is not None:
            card = evidence_svc.gap_evidence_card(mmsi, ts)
            if not card.get("found"):
                raise HTTPException(404, card.get("message") or "未找到该中断记录")
            return card
        cards = evidence_svc.gap_cards(mmsi, hours=hours, limit=limit)
        return {"count": len(cards), "cards": cards}

    if ts is not None:
        card = evidence_svc.fishing_evidence_card(mmsi, ts)
        if not card.get("found"):
            raise HTTPException(404, card.get("message") or "未找到该疑似捕捞片段")
        return card
    cards = evidence_svc.fishing_cards(mmsi, hours=hours, limit=limit)
    return {"count": len(cards), "cards": cards}


@router.get("/analysis/readiness")
def analysis_readiness():
    """当前数据够不够做行为指纹 / 环境分析。供智能体先判断能力边界。"""
    from core.db import connect
    from contextlib import closing

    with closing(connect()) as conn:
        row = conn.execute("SELECT MIN(ts) a, MAX(ts) b, COUNT(*) n FROM ais_positions").fetchone()
        segs = conn.execute("SELECT COUNT(*) c FROM behavior_segments").fetchone()["c"]
        trips_c = conn.execute("SELECT COUNT(*) c FROM vessel_trips WHERE status='closed'").fetchone()["c"]

    span_h = round(((row["b"] or 0) - (row["a"] or 0)) / 3600, 2) if row and row["a"] else 0
    dna = fp_svc.dna_readiness()
    ocean_caps = ocean_svc.capabilities()

    return {
        "now": int(time.time()),
        "data": {
            "span_hours": span_h,
            "positions": row["n"],
            "behavior_segments_cached": segs,
            "closed_trips": trips_c,
        },
        "capabilities": {
            "behavior_timeline": {
                "available": True,
                "note": "单船行为时间线已可用；窗口越长，分段越完整",
            },
            "trip_reconstruction": {
                "available": True,
                "note": "可按停泊/断档切分航次，但需要至少一次连续 2 小时静止作为边界",
            },
            "vessel_dna": {
                "available": dna["ready"],
                "note": dna["verdict"],
                "what_is_needed": dna["what_is_needed"],
            },
            "ocean_environment": {
                "available": ocean_caps["available"],
                "note": (
                    "已接通五块：表层温度/流速/海面高度（逐时）、有效波高/波向/周期（3 小时）、"
                    "10 m 风场（GFS 近实时，默认）、卫星散射计观测风（L4，滞后 2 天）、"
                    "叶绿素（2019–2023 同月气候态，非实时）。全部按需提取、经异步任务执行"
                    if ocean_caps["available"]
                    else ocean_caps["reason"]
                ),
                "wind_sources": ocean_caps.get("wind_sources"),
                "products": ocean_caps["products"],
                "not_implemented": ocean_caps["not_implemented"],
            },
            "sar_verification": {
                "available": False,
                "note": "未接入 SAR 影像检索与目标检测，属后续版本",
            },
        },
        "data_span_hours": span_h,
    }


# --------------------------------------------------------------------------
# Ocean Engine（异步）
# --------------------------------------------------------------------------
@router.get("/analysis/ocean/capabilities")
def ocean_capabilities():
    """环境引擎自检：依赖、凭证、产品、以及明确没做什么。"""
    return ocean_svc.capabilities()


@router.post("/analysis/ocean/tracks")
def submit_ocean_track(
    mmsi: int = Query(..., description="船舶 MMSI"),
    hours: int = Query(24, ge=1, le=168, description="回看小时数"),
    products: str = Query(
        "",
        description=(
            "逗号分隔的产品；留空用默认 physics,waves,wind_gfs,bgc_clim。"
            "可选：physics（SST/流/海高）、waves（浪高/波向/周期）、"
            "wind_gfs（近实时风，覆盖今天）、wind（卫星观测风 L4，滞后约 2 天）、"
            "wind_era5（ERA5 再分析风，滞后约 5 天，单次排队约 3 分钟）、"
            "bgc_clim（叶绿素气候态）"
        ),
    ),
):
    """提交「轨迹环境特征提取」任务，立刻返回 job_id。

    走异步是有原因的：一次瓦片请求约 10 秒，一条轨迹可能要好几个瓦片，
    同步跑会把连接挂死。轮询 `GET /api/analysis/jobs/{job_id}` 取结果。
    """
    caps = ocean_svc.capabilities()
    if not caps["available"]:
        raise HTTPException(503, f"环境引擎不可用：{caps['reason']}")

    try:
        keys = ocean_svc.normalize_products(products)
    except ValueError as exc:
        raise HTTPException(400, str(exc))

    job = job_svc.enqueue("ocean_track", {
        "mmsi": int(mmsi), "hours": int(hours), "products": keys,
    })
    return {
        "job_id": job["job_id"],
        "status": job["status"],
        "deduped": job["deduped"],
        "products": keys,
        "poll": f"/api/analysis/jobs/{job['job_id']}?with_result=true",
        "note": "任务已入队，由 worker 执行；同一参数重复提交会复用已有任务。",
    }


@router.get("/analysis/ocean/series")
def ocean_series(
    mmsi: int = Query(..., description="船舶 MMSI"),
    hours: int = Query(24, ge=1, le=168),
):
    """只读：把已算过的环境特征按时间排成序列（前端面板用，绝不联网）。

    没缓存时返回 `no_cache`，前端据此显示「一键提取」按钮，而不是干等。
    """
    return ocean_svc.read_series(mmsi, hours=hours)


@router.get("/analysis/jobs")
def list_jobs(limit: int = Query(20, ge=1, le=100), status: str | None = Query(None)):
    """最近的异步任务列表。"""
    return {
        "depth": job_svc.queue_depth(),
        "jobs": job_svc.list_jobs(limit=limit, status=status),
        "kinds": job_svc.KINDS,
    }


@router.get("/analysis/jobs/{job_id}")
def get_job(job_id: str, with_result: bool = Query(True, description="是否带上完整结果")):
    """查询单个任务。done 且 with_result=true 时返回分析结果全文。"""
    job = job_svc.get(job_id, with_result=with_result)
    if not job:
        raise HTTPException(404, "任务不存在")
    return job
