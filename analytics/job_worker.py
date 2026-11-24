#!/usr/bin/env python3
"""异步分析任务 worker。

为什么是独立进程而不是后台线程
------------------------------
- 任务里有远程 IO（Copernicus 取数，单个瓦片约 10 秒），跑在 API 进程里会
  拖慢甚至卡住 HTTP/WebSocket
- 独立进程可以被单独重启、单独看日志（`logs/worker.log`）
- 不需要 Redis：SQLite 的 `UPDATE ... WHERE status='queued'` 加 rowcount 就够用

用法
----
    .venv/Scripts/python.exe analytics/job_worker.py            # 常驻轮询
    .venv/Scripts/python.exe analytics/job_worker.py --once     # 只跑一轮（调试）
    .venv/Scripts/python.exe analytics/job_worker.py --drain    # 跑到队列空就退出

run.py 会自动把它当第四个子进程拉起来。
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from backend.app.services import jobs as job_svc  # noqa: E402
from backend.app.services import ocean as ocean_svc  # noqa: E402
from core.config import settings  # noqa: E402
from core.db import init_db  # noqa: E402

log = logging.getLogger("worker")
_running = True


def _setup_logging() -> None:
    settings.ensure_dirs()
    handler = logging.FileHandler(ROOT / "logs" / "worker.log", encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(message)s"))
    logging.basicConfig(level=getattr(logging, settings.log_level, logging.INFO),
                        handlers=[handler, console])


def _stop(signum, frame):  # noqa: ARG001
    global _running
    log.info("收到退出信号，处理完当前任务后停止")
    _running = False


def run_job(job: dict) -> dict | None:
    """按 kind 分发。返回值会写进 analysis_results。"""
    kind = job["kind"]
    params = job["params"]
    job_id = job["job_id"]

    if kind == "ocean_track":
        mmsi = int(params["mmsi"])
        hours = int(params.get("hours", 24))
        products = params.get("products")
        log.info("开始环境提取 mmsi=%s hours=%s products=%s", mmsi, hours, products)
        result = ocean_svc.features_for_track(
            mmsi, hours=hours, products=products,
            progress=lambda f: job_svc.set_progress(job_id, f),
        )
        log.info("环境提取完成 mmsi=%s status=%s matched=%s",
                 mmsi, result.get("status"), (result.get("points") or {}).get("matched"))
        return result

    raise ValueError(f"未实现的 task kind: {kind}")


def _meta_for(job: dict, result: dict | None) -> dict:
    product = (result or {}).get("product") or {}
    return {
        "kind": job["kind"],
        "data_version": product.get("version") or product.get("dataset_id"),
        "model_version": (result or {}).get("model_version"),
        "parameters": job["params"],
        "quality_flags": (result or {}).get("points", {}).get("quality_counts"),
    }


def handle_one() -> bool:
    """抢一个任务并执行。返回是否真的处理了任务。"""
    job = job_svc.claim_next()
    if not job:
        return False
    started = time.time()
    try:
        result = run_job(job)
        job_svc.finish(job["job_id"], result=result, meta=_meta_for(job, result))
        log.info("任务完成 %s（%.1fs）", job["job_id"], time.time() - started)
    except Exception as exc:
        log.exception("任务失败 %s", job["job_id"])
        job_svc.finish(job["job_id"], error=f"{type(exc).__name__}: {exc}")
    return True


def main() -> int:
    ap = argparse.ArgumentParser(description="FishEye 异步任务 worker")
    ap.add_argument("--once", action="store_true", help="只跑一轮（有任务就执行一个）")
    ap.add_argument("--drain", action="store_true", help="跑到队列空就退出")
    ap.add_argument("--interval", type=float, default=3.0, help="轮询间隔（秒）")
    args = ap.parse_args()

    _setup_logging()
    init_db()
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    recovered = job_svc.recover_stale()
    if recovered:
        log.warning("回收了 %d 个卡住的 running 任务", recovered)

    log.info("worker 启动（间隔 %.1fs）", args.interval)

    if args.once:
        handle_one()
        return 0

    idle = 0
    while _running:
        worked = handle_one()
        if worked:
            idle = 0
            continue
        if args.drain:
            log.info("队列已空，退出")
            break
        idle += 1
        if idle % 20 == 1:
            log.debug("空闲中，队列 %s", job_svc.queue_depth())
        time.sleep(args.interval)

    log.info("worker 退出")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
