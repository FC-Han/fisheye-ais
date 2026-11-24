"""FishEye AIS 采集器。

长驻进程：连接 AISStream → 订阅区域 → 解析 → 入队 → 写入线程落库。
具备：断线重连（指数退避）、订阅确认超时、消息速率统计、数据源健康落库、优雅退出。

用法：
    python -m collector.main                      # 使用 .env 中的 bbox（默认全球）
    python -m collector.main --region east_china  # 使用 config/regions.json 中的预设区域
"""
from __future__ import annotations

import argparse
import asyncio
import json
import logging
import queue
import signal
import sys
import time
from collections import deque
from pathlib import Path

import websockets

from core.config import ROOT, settings
from core.db import connect, init_db, set_meta
from core.parser import normalize

from .writer import Writer

log = logging.getLogger("collector")

HEALTH_ENABLED = True


def load_regions() -> dict:
    p: Path = ROOT / "config" / "regions.json"
    if p.exists():
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            log.warning("config/regions.json 解析失败，忽略")
    return {}


def subscription(bboxes: list) -> dict:
    return {
        "APIKey": settings.api_key,
        "BoundingBoxes": bboxes,
        "FilterMessageTypes": [
            "PositionReport",
            "StandardClassBPositionReport",
            "ExtendedClassBPositionReport",
            "ShipStaticData",
            "StaticDataReport",
        ],
    }


class Collector:
    def __init__(self, bboxes: list, region_label: str):
        self.bboxes = bboxes
        self.region_label = region_label
        self.q: "queue.Queue" = queue.Queue(maxsize=400_000)
        self.stats = {
            "msgs_total": 0,
            "msgs_parsed": 0,
            "msgs_dropped": 0,
            "msgs_invalid": 0,
            "connected": False,
            "connects": 0,
            "last_msg_ts": 0,
            "last_error": None,
            "started_at": int(time.time()),
        }
        self._rate_q: deque[tuple[float, int]] = deque()
        self._stop = asyncio.Event()
        self.writer = Writer(self.q, self.stats, None)

    # ---------- 健康 ----------
    def _write_health(self, note: str | None = None) -> None:
        try:
            conn = connect()
            now = int(time.time())
            rate = self._rate_1m()
            conn.execute(
                "INSERT INTO stream_health(ts, connected, msgs_total, rate_1m, last_msg_ts, bboxes, note)"
                " VALUES(?,?,?,?,?,?,?)",
                (now, 1 if self.stats["connected"] else 0, self.stats["msgs_total"], rate,
                 self.stats["last_msg_ts"] or None, json.dumps(self.bboxes), note),
            )
            set_meta(conn, "collector_region", self.region_label)
            set_meta(conn, "collector_bboxes", json.dumps(self.bboxes))
            set_meta(conn, "collector_last_beat", str(now))
            conn.commit()
            conn.close()
        except Exception:
            log.exception("写健康表失败")

    def _rate_1m(self) -> float:
        now = time.time()
        while self._rate_q and now - self._rate_q[0][0] > 60:
            self._rate_q.popleft()
        if not self._rate_q:
            return 0.0
        span = max(1.0, now - self._rate_q[0][0])
        return round(sum(c for _, c in self._rate_q) / span, 2)

    async def health_loop(self) -> None:
        while not self._stop.is_set():
            self._write_health()
            log.info(
                "状态 | 已收 %s 条 | 速率 %s/s | 入队丢弃 %s | 在线 %s",
                self.stats["msgs_total"], self._rate_1m(), self.stats["msgs_dropped"],
                "是" if self.stats["connected"] else "否",
            )
            try:
                await asyncio.wait_for(self._stop.wait(), timeout=settings.health_interval)
            except asyncio.TimeoutError:
                pass

    # ---------- 主循环 ----------
    async def run(self) -> None:
        backoff = 1.0
        silent_closes = 0
        while not self._stop.is_set():
            t_connect = 0.0
            msgs_at_connect = self.stats["msgs_total"]
            try:
                self.stats["connected"] = False
                async with websockets.connect(
                    settings.ws_url,
                    compression="deflate",
                    max_size=None,
                    ping_interval=20,
                    ping_timeout=20,
                    open_timeout=15,
                ) as ws:
                    sub = subscription(self.bboxes)
                    await ws.send(json.dumps(sub))
                    self.stats["connected"] = True
                    self.stats["connects"] += 1
                    t_connect = time.time()
                    msgs_at_connect = self.stats["msgs_total"]
                    backoff = 1.0
                    log.info("已连接 AISStream，订阅区域：%s", self.region_label)
                    self._write_health("connected")

                    while not self._stop.is_set():
                        try:
                            raw = await asyncio.wait_for(ws.recv(), timeout=120)
                        except asyncio.TimeoutError:
                            log.warning("120s 未收到任何消息，主动重连")
                            break
                        if isinstance(raw, bytes):
                            raw = raw.decode("utf-8", "ignore")
                        self.stats["msgs_total"] += 1
                        self.stats["last_msg_ts"] = int(time.time())
                        self._rate_q.append((time.time(), 1))

                        try:
                            data = json.loads(raw)
                        except json.JSONDecodeError:
                            self.stats["msgs_invalid"] += 1
                            continue
                        if isinstance(data, dict) and data.get("error"):
                            err = str(data.get("error"))
                            self.stats["last_error"] = err
                            log.error("上游返回错误：%s", err)
                            if "limit" in err.lower() or "key" in err.lower() or "subscription" in err.lower():
                                self._stop.set()
                            break

                        msg = normalize(data)
                        if not msg:
                            self.stats["msgs_invalid"] += 1
                            continue
                        msg["received_at"] = int(time.time())
                        msg["raw"] = data
                        try:
                            self.q.put_nowait(msg)
                            self.stats["msgs_parsed"] += 1
                        except queue.Full:
                            self.stats["msgs_dropped"] += 1
            except asyncio.CancelledError:
                raise
            except Exception as e:  # 网络异常 / 握手失败等
                self.stats["connected"] = False
                self.stats["last_error"] = f"{type(e).__name__}: {e}"
                log.warning("连接异常：%s，%.0fs 后重试", e, backoff)
                # 上游对无效 key 的处理是「握手成功 → 立刻断开 → 不给任何说明」，
                # 于是日志里会出现「已连接」紧接「连接异常」，看起来像网络问题。
                # 连续两次「连上不到 20s 且一条消息没收到」就基本可以断定是 key 的问题。
                if t_connect and time.time() - t_connect < 20 and self.stats["msgs_total"] == msgs_at_connect:
                    silent_closes += 1
                else:
                    silent_closes = 0
                if silent_closes == 2:
                    log.error(
                        "连续两次「刚连上就被服务器断开，且一条消息都没收到」。"
                        "最常见的原因是 AISSTREAM_API_KEY 无效：到 aisstream.io 重新复制完整的 key"
                        "（40 位十六进制，注意不要带空格或引号）写进 .env 后重启。"
                        "若 key 确认无误，再检查订阅区域 BoundingBoxes 是否为空、经纬度有没有写反（先纬度后经度）。"
                    )
                self._write_health("error: 连接被立即断开（疑似 key 无效）" if silent_closes >= 2 else f"error: {type(e).__name__}")
                try:
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60)

    def request_stop(self) -> None:
        self._stop.set()


async def amain(args) -> int:
    if not settings.api_key:
        log.error("缺少 AISSTREAM_API_KEY，请检查 .env")
        return 2
    init_db()

    bboxes = settings.bboxes
    label = "全球" if bboxes == [[[-90, -180], [90, 180]]] else "自定义"
    if args.region:
        regions = load_regions()
        if args.region not in regions:
            log.error("未知区域 %s，可选：%s", args.region, ", ".join(regions.keys()) or "(无)")
            return 2
        bboxes = regions[args.region]["bboxes"]
        label = regions[args.region].get("name", args.region)

    collector = Collector(bboxes, label)
    collector.writer.start()

    if hasattr(signal, "SIGINT"):
        try:
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                try:
                    loop.add_signal_handler(sig, collector.request_stop)
                except NotImplementedError:
                    pass  # Windows 不支持，靠 KeyboardInterrupt
        except Exception:
            pass

    tasks = [asyncio.create_task(collector.run()), asyncio.create_task(collector.health_loop())]
    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        pass
    finally:
        collector.request_stop()
        for t in tasks:
            t.cancel()
        collector.writer.stop()
        collector.writer.join(timeout=15)
        collector._write_health("stopped")
        log.info("采集器已停止：共接收 %s 条消息", collector.stats["msgs_total"])
    return 0


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, settings.log_level, logging.INFO),
        format="%(asctime)s %(levelname)-5s [%(name)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    ap = argparse.ArgumentParser(description="FishEye AIS 采集器")
    ap.add_argument("--region", help="使用 config/regions.json 中的预设区域")
    args = ap.parse_args()
    try:
        return asyncio.run(amain(args))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    sys.exit(main())
