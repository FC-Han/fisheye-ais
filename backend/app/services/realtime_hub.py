"""实时推送：按视野 bbox 增量推送船舶状态。

客户端连上后先收到一帧全量（limit 上限），之后每 interval 秒只收到 ts 增长的船。
这样全球视图下即使有大量船舶，也不会每个 tick 都重传全量。
"""
from __future__ import annotations

import asyncio
import logging
import time

from fastapi import WebSocket, WebSocketDisconnect

from . import vessel_state as vs

log = logging.getLogger("ws")


async def vessel_stream(ws: WebSocket) -> None:
    await ws.accept()
    try:
        raw = ws.query_params.get("bbox", "-90,-180,90,180")
        minlat, minlon, maxlat, maxlon = (float(x) for x in raw.split(","))
        interval = max(1.0, min(10.0, float(ws.query_params.get("interval", 2))))
        identity = ws.query_params.get("identity", "all")
        limit = max(100, min(8000, int(ws.query_params.get("limit", 4000))))
        bbox = (minlat, minlon, maxlat, maxlon)
    except (ValueError, TypeError):
        await ws.close(code=1008, reason="bad params")
        return

    cursor = 0
    last_full = 0.0
    await ws.send_json({"type": "hello", "interval": interval, "bbox": bbox})

    try:
        while True:
            t0 = time.time()
            now = time.time()
            full = now - last_full > 60
            try:
                rows = await asyncio.to_thread(
                    vs.vessels_in_bbox, bbox, limit, identity, 3600, None, None if full else cursor
                )
            except Exception as e:
                log.warning("实时查询失败：%s", e)
                rows = []

            if rows:
                cursor = max(r["ts"] for r in rows)
            if full:
                cursor = max(cursor, int(now))
                last_full = now
            await ws.send_json({
                "type": "vessels",
                "full": full,
                "ts": int(now),
                "count": len(rows),
                "truncated": len(rows) >= limit,
                "vessels": rows,
            })
            elapsed = time.time() - t0
            await asyncio.sleep(max(0.2, interval - elapsed))
    except WebSocketDisconnect:
        pass
    except Exception as e:
        log.info("WS 关闭：%s", e)
    finally:
        try:
            await ws.close()
        except Exception:
            pass
