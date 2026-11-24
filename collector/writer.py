"""写入线程：把解析后的消息批量落库 + 原始归档。

关键设计
--------
1. 实时状态层全量更新（每条位置消息都更新 vessel_latest_state）
2. 轨迹层降采样（同船 30s 或移动 100m 才写一条），全球订阅也不会撑爆磁盘
3. 原始消息按天写 JSONL 归档，带大小上限滚动
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from core.config import settings
from core.db import connect
from core.geo import haversine_m
from core.parser import classify_identity, ship_type_text

log = logging.getLogger("writer")


class Writer(threading.Thread):
    def __init__(self, q: "queue.Queue", stats: dict, lock: threading.Lock):
        super().__init__(name="writer", daemon=True)
        self.q = q
        self.stats = stats
        self.lock = lock

        self.latest: dict[int, dict] = {}
        self.dirty: set[int] = set()
        self.pos_buffer: list[tuple] = []
        self.vessel_buffer: dict[int, tuple] = {}
        self.last_saved: dict[int, tuple[int, float, float]] = {}

        self._running = True
        self._conn = None
        self._archive_fp = None
        self._archive_day = ""
        self._archive_bytes = 0
        self._archive_path: Path | None = None

    # ---------- 归档 ----------
    def _archive_write(self, raw: dict) -> None:
        if not settings.archive_enabled:
            return
        day = datetime.now(timezone.utc).strftime("%Y%m%d")
        if day != self._archive_day or self._archive_fp is None:
            self._rotate_archive(day)
        line = json.dumps(raw, ensure_ascii=False, separators=(",", ":")) + "\n"
        data = line.encode("utf-8")
        if self._archive_bytes + len(data) > settings.archive_max_mb * 1024 * 1024:
            self._rotate_archive(day, suffix="_b")
        self._archive_fp.write(data)
        self._archive_bytes += len(data)

    def _rotate_archive(self, day: str, suffix: str = "") -> None:
        if self._archive_fp:
            try:
                self._archive_fp.close()
            except Exception:
                pass
        settings.archive_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc).strftime("%H%M%S")
        name = f"ais-{day}-{stamp}{suffix}.jsonl"
        self._archive_path = settings.archive_dir / name
        self._archive_fp = open(self._archive_path, "ab", buffering=1024 * 256)
        self._archive_day = day
        self._archive_bytes = self._archive_path.stat().st_size if self._archive_path.exists() else 0
        log.info("归档文件切换到 %s", name)

    # ---------- 业务处理 ----------
    def _process(self, msg: dict) -> None:
        now = msg["received_at"]
        mmsi = msg["mmsi"]

        if msg["kind"] == "static":
            name = msg.get("ship_name")
            stype = msg.get("ship_type")
            identity, source = classify_identity(stype, name)
            dim = msg.get("dimension") or {}
            length = None
            try:
                if dim:
                    length = float(dim.get("A", 0)) + float(dim.get("B", 0))
            except (TypeError, ValueError):
                length = None
            self.vessel_buffer[mmsi] = (
                mmsi,
                msg.get("imo"),
                msg.get("call_sign"),
                name,
                stype,
                ship_type_text(stype) if stype is not None else None,
                identity,
                source,
                length,
                msg.get("draught"),
                msg.get("destination"),
                now,
                now,
            )
            # 静态信息也可能带位置（部分实现），有则一并更新
            if msg.get("lat") is not None and msg.get("lon") is not None:
                self._update_latest(msg)
            return

        if msg["kind"] == "position":
            if msg.get("lat") is None or msg.get("lon") is None:
                return
            self._update_latest(msg)
            # 降采样判定
            prev = self.last_saved.get(mmsi)
            save = True
            if prev is not None:
                pts, plat, plon = prev
                if now - pts < settings.min_save_interval:
                    save = haversine_m(plat, plon, msg["lat"], msg["lon"]) >= settings.min_save_distance_m
            if save:
                self.pos_buffer.append(
                    (mmsi, now, msg["lat"], msg["lon"], msg.get("sog"), msg.get("cog"),
                     msg.get("heading"), msg.get("nav_status"), msg["msg_type"])
                )
                self.last_saved[mmsi] = (now, msg["lat"], msg["lon"])

    def _update_latest(self, msg: dict) -> None:
        mmsi = msg["mmsi"]
        prev = self.latest.get(mmsi)
        identity = prev["identity"] if prev else None
        if identity is None:
            identity, _ = classify_identity(msg.get("ship_type"), msg.get("ship_name"))
        self.latest[mmsi] = {
            "mmsi": mmsi,
            "ts": msg["received_at"],
            "lat": msg["lat"],
            "lon": msg["lon"],
            "sog": msg.get("sog"),
            "cog": msg.get("cog"),
            "heading": msg.get("heading"),
            "nav_status": msg.get("nav_status"),
            "msg_type": msg["msg_type"],
            "identity": identity,
        }
        self.dirty.add(mmsi)

    # ---------- 落库 ----------
    def _flush(self) -> None:
        if not (self.dirty or self.pos_buffer or self.vessel_buffer):
            return
        conn = self._conn
        now = int(time.time())
        try:
            if self.vessel_buffer:
                conn.executemany(
                    """INSERT INTO vessels(mmsi, imo, call_sign, ship_name, ship_type, ship_type_text,
                                          fishing_identity, identity_source, length, draught, destination,
                                          first_seen, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(mmsi) DO UPDATE SET
                         imo=COALESCE(excluded.imo, vessels.imo),
                         call_sign=COALESCE(excluded.call_sign, vessels.call_sign),
                         ship_name=COALESCE(excluded.ship_name, vessels.ship_name),
                         ship_type=COALESCE(excluded.ship_type, vessels.ship_type),
                         ship_type_text=COALESCE(excluded.ship_type_text, vessels.ship_type_text),
                         fishing_identity=CASE WHEN excluded.fishing_identity='unknown'
                                               THEN vessels.fishing_identity ELSE excluded.fishing_identity END,
                         identity_source=COALESCE(excluded.identity_source, vessels.identity_source),
                         length=COALESCE(excluded.length, vessels.length),
                         draught=COALESCE(excluded.draught, vessels.draught),
                         destination=COALESCE(excluded.destination, vessels.destination),
                         updated_at=excluded.updated_at""",
                    list(self.vessel_buffer.values()),
                )
                self.vessel_buffer.clear()

            if self.dirty:
                rows = [self.latest[m] for m in self.dirty if m in self.latest]
                conn.executemany(
                    """INSERT INTO vessel_latest_state(mmsi, ts, lat, lon, sog, cog, heading, nav_status,
                                                       msg_type, identity, updated_at)
                       VALUES(?,?,?,?,?,?,?,?,?,?,?)
                       ON CONFLICT(mmsi) DO UPDATE SET
                         ts=excluded.ts, lat=excluded.lat, lon=excluded.lon, sog=excluded.sog,
                         cog=excluded.cog, heading=excluded.heading, nav_status=excluded.nav_status,
                         msg_type=excluded.msg_type, identity=excluded.identity, updated_at=excluded.updated_at""",
                    [(r["mmsi"], r["ts"], r["lat"], r["lon"], r["sog"], r["cog"], r["heading"],
                      r["nav_status"], r["msg_type"], r["identity"], now) for r in rows],
                )
                conn.executemany(
                    "INSERT OR REPLACE INTO vessel_rtree(mmsi, minlat, maxlat, minlon, maxlon) VALUES(?,?,?,?,?)",
                    [(r["mmsi"], r["lat"], r["lat"], r["lon"], r["lon"]) for r in rows],
                )
                self.dirty.clear()

            if self.pos_buffer:
                conn.executemany(
                    "INSERT INTO ais_positions(mmsi, ts, lat, lon, sog, cog, heading, nav_status, msg_type)"
                    " VALUES(?,?,?,?,?,?,?,?,?)",
                    self.pos_buffer,
                )
                self.pos_buffer.clear()
            conn.commit()
        except Exception:
            log.exception("落库失败，丢弃本批次 %d 条位置点", len(self.pos_buffer))
            self.pos_buffer.clear()
            self.vessel_buffer.clear()
            self.dirty.clear()
            try:
                conn.rollback()
            except Exception:
                pass

    def run(self) -> None:
        self._conn = connect()
        log.info("写入线程启动")
        last_flush = time.time()
        while self._running:
            timeout = max(0.05, settings.flush_interval - (time.time() - last_flush))
            try:
                item = self.q.get(timeout=timeout)
            except queue.Empty:
                item = None
            if item is not None:
                batch = [item]
                while len(batch) < settings.max_batch:
                    try:
                        nxt = self.q.get_nowait()
                    except queue.Empty:
                        break
                    if nxt is None:
                        self._running = False
                        break
                    batch.append(nxt)
                for m in batch:
                    try:
                        self._process(m)
                        self._archive_write(m["raw"])
                    except Exception:
                        log.exception("处理单条消息失败，跳过")
            now = time.time()
            if now - last_flush >= settings.flush_interval or self.q.empty():
                self._flush()
                last_flush = now

        self._flush()
        if self._archive_fp:
            try:
                self._archive_fp.close()
            except Exception:
                pass
        if self._conn:
            self._conn.close()
        log.info("写入线程退出")

    def stop(self) -> None:
        self._running = False
        self.q.put(None)

    def snapshot(self) -> dict:
        return {
            "known_vessels": len(self.latest),
            "archiving": settings.archive_enabled,
            "archive_file": self._archive_path.name if self._archive_path else None,
        }
