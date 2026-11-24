"""分析工作进程：周期性跑捕捞识别 / 中断检测 / 围栏事件 / 数据保留清理。

用法：
    python -m analytics.worker              # 常驻，每 FISHEYE_ANALYTICS_INTERVAL 秒跑一轮
    python -m analytics.worker --once       # 只跑一轮（便于测试）
"""
from __future__ import annotations

import argparse
import logging
import signal
import sys
import time

from core.config import settings
from core.db import connect, init_db, set_meta

from . import fishing_detector, gap_detector, geofence_detector
from .common import load_positions, vessels_in_focus

log = logging.getLogger("analytics")


def run_once() -> dict:
    t0 = time.time()
    conn = connect()
    try:
        seeded = geofence_detector.seed_zones(conn)
        conn.commit()

        vessels = vessels_in_focus(conn, int(time.time()) - 3600)
        since = int(time.time()) - settings.analytics_window_hours * 3600
        positions = load_positions(conn, [v["mmsi"] for v in vessels], since)

        n_fish = fishing_detector.run(conn, vessels, positions)
        n_gap_open, n_gap_closed = gap_detector.run(conn, vessels, positions)
        n_geo = geofence_detector.run(conn)
        conn.commit()

        # 保留策略
        cutoff = int(time.time()) - settings.retention_days * 86400
        old_pos = conn.execute("DELETE FROM ais_positions WHERE ts < ?", (cutoff,)).rowcount
        conn.execute("DELETE FROM vessel_latest_state WHERE ts < ?", (int(time.time()) - 7 * 86400,))
        conn.commit()

        set_meta(conn, "analytics_last_run", str(int(time.time())))
        set_meta(conn, "analytics_last_scope", f"{len(vessels)} vessels")
        conn.commit()

        result = {
            "vessels_analyzed": len(vessels),
            "fishing_events": n_fish,
            "gap_opened": n_gap_open,
            "gap_closed": n_gap_closed,
            "geofence_events": n_geo,
            "positions_pruned": old_pos,
            "zones_seeded": seeded,
            "elapsed_s": round(time.time() - t0, 2),
        }
        log.info(
            "分析完成 | 船 %s | 捕捞片段 %s | 中断 +%s/-%s | 围栏 %s | %.1fs",
            result["vessels_analyzed"], n_fish, n_gap_open, n_gap_closed, n_geo, result["elapsed_s"],
        )
        return result
    finally:
        conn.close()


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="FishEye 分析工作进程")
    ap.add_argument("--once", action="store_true", help="只跑一轮")
    args = ap.parse_args()

    init_db()
    if args.once:
        run_once()
        return 0

    stop = {"v": False}

    def _sig(*_a):
        stop["v"] = True

    for s in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(s, _sig)
        except (ValueError, OSError):
            pass

    while not stop["v"]:
        try:
            run_once()
        except Exception:
            log.exception("分析轮次失败，%ss 后重试", settings.analytics_interval)
        for _ in range(settings.analytics_interval):
            if stop["v"]:
                break
            time.sleep(1)
    return 0


if __name__ == "__main__":
    sys.exit(main())
