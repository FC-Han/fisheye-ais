"""SQLite 存储层。

设计说明
--------
本项目 v0.1 用 SQLite（WAL + R*Tree 空间索引）替代 PostGIS，原因是单机零运维即可跑通。
表结构与 PostGIS 版（database/init_postgis.sql）保持一致，未来迁移只需换 DAO。
多进程安全：collector 写、analytics 写、backend 只读，全部走 WAL + busy_timeout。
"""
from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from .config import settings

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- 船舶档案
CREATE TABLE IF NOT EXISTS vessels (
    mmsi              INTEGER PRIMARY KEY,
    imo               TEXT,
    call_sign         TEXT,
    ship_name         TEXT,
    ship_type         INTEGER,
    ship_type_text    TEXT,
    fishing_identity  TEXT DEFAULT 'unknown',   -- fishing / candidate / non_fishing / unknown
    identity_source   TEXT,
    length            REAL,
    beam              REAL,
    draught           REAL,
    destination       TEXT,
    first_seen        INTEGER,
    updated_at        INTEGER
);

-- 标准化轨迹点（已降采样）
CREATE TABLE IF NOT EXISTS ais_positions (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi       INTEGER NOT NULL,
    ts         INTEGER NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    sog        REAL,
    cog        REAL,
    heading    INTEGER,
    nav_status INTEGER,
    msg_type   TEXT
);
CREATE INDEX IF NOT EXISTS idx_pos_mmsi_ts ON ais_positions(mmsi, ts);
CREATE INDEX IF NOT EXISTS idx_pos_ts ON ais_positions(ts);

-- 最新状态（实时层，全量覆盖更新）
CREATE TABLE IF NOT EXISTS vessel_latest_state (
    mmsi       INTEGER PRIMARY KEY,
    ts         INTEGER NOT NULL,
    lat        REAL NOT NULL,
    lon        REAL NOT NULL,
    sog        REAL,
    cog        REAL,
    heading    INTEGER,
    nav_status INTEGER,
    msg_type   TEXT,
    ship_name  TEXT,
    ship_type  INTEGER,
    identity   TEXT DEFAULT 'unknown',
    updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_latest_ts ON vessel_latest_state(ts);

-- 空间索引（R*Tree）
CREATE VIRTUAL TABLE IF NOT EXISTS vessel_rtree USING rtree(
    mmsi, minlat, maxlat, minlon, maxlon
);

-- 疑似捕捞事件
CREATE TABLE IF NOT EXISTS fishing_events (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi        INTEGER NOT NULL,
    start_ts    INTEGER NOT NULL,
    end_ts      INTEGER NOT NULL,
    duration_s  INTEGER NOT NULL,
    avg_sog     REAL,
    lat         REAL,
    lon         REAL,
    gear        TEXT,
    confidence  REAL,
    evidence    TEXT,
    created_at  INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_fishing ON fishing_events(mmsi, start_ts);
CREATE INDEX IF NOT EXISTS idx_fishing_ts ON fishing_events(end_ts);

-- AIS 信号中断候选事件
CREATE TABLE IF NOT EXISTS ais_gap_events (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    mmsi             INTEGER NOT NULL,
    gap_start        INTEGER NOT NULL,
    gap_end          INTEGER,
    duration_s       INTEGER,
    lat              REAL,
    lon              REAL,
    expected_sec     INTEGER,
    coverage_quality TEXT,
    stream_ok        INTEGER,
    event_status     TEXT,
    evidence         TEXT,
    created_at       INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_gap ON ais_gap_events(mmsi, gap_start);
CREATE INDEX IF NOT EXISTS idx_gap_end ON ais_gap_events(gap_end);

-- 区域 / 电子围栏
CREATE TABLE IF NOT EXISTS geofences (
    id       TEXT PRIMARY KEY,
    name     TEXT NOT NULL,
    kind     TEXT,
    polygon  TEXT NOT NULL,
    props    TEXT,
    source   TEXT
);

CREATE TABLE IF NOT EXISTS geofence_events (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    zone_id    TEXT NOT NULL,
    mmsi       INTEGER NOT NULL,
    event_type TEXT NOT NULL,
    ts         INTEGER NOT NULL,
    lat        REAL,
    lon        REAL,
    created_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS uq_geo_evt ON geofence_events(zone_id, mmsi, event_type, ts);

-- 区域进入状态（用于生成 enter / exit 事件，而不是每帧刷事件）
CREATE TABLE IF NOT EXISTS geofence_state (
    zone_id TEXT NOT NULL,
    mmsi    INTEGER NOT NULL,
    inside  INTEGER NOT NULL,
    last_ts INTEGER,
    PRIMARY KEY (zone_id, mmsi)
);

-- 数据源健康
CREATE TABLE IF NOT EXISTS stream_health (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          INTEGER NOT NULL,
    connected   INTEGER NOT NULL,
    msgs_total  INTEGER,
    rate_1m     REAL,
    last_msg_ts INTEGER,
    bboxes      TEXT,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON stream_health(ts);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

-- =====================================================================
-- v0.2 分析层（Behavior Engine / Vessel DNA / Reliability）
--
-- 设计约束：
-- 1. 每张表都带 model_version + 参数快照，模型升级后写新记录而不覆盖旧记录，
--    保证历史报告可复现。
-- 2. confidence 允许为 NULL —— 未做概率校准前，不输出可解释为概率的数值。
-- 3. 行为片段在同一 model_version 下幂等覆盖（同样的输入算两次结果一致），
--    跨 model_version 并存。
-- =====================================================================

-- 航次（trip）：从「开始移动」到「重新长时间静止」的一段完整出海过程
CREATE TABLE IF NOT EXISTS vessel_trips (
    trip_id         TEXT PRIMARY KEY,        -- {mmsi}-{start_ts}-{model_version}
    mmsi            INTEGER NOT NULL,
    start_ts        INTEGER NOT NULL,
    end_ts          INTEGER,
    start_lat       REAL,
    start_lon       REAL,
    end_lat         REAL,
    end_lon         REAL,
    duration_s      INTEGER,
    distance_nm     REAL,
    n_points        INTEGER,
    coverage_ratio  REAL,                    -- 实际点数 / 期望点数
    max_gap_s       INTEGER,
    status          TEXT,                    -- open / closed / truncated
    boundary_reason TEXT,                    -- 切分依据，便于复核
    data_version    TEXT,
    model_version   TEXT,
    parameters      TEXT,
    created_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_trip_mmsi ON vessel_trips(mmsi, start_ts);

-- 行为片段：时间上连续、互不重叠、覆盖整个查询窗口
CREATE TABLE IF NOT EXISTS behavior_segments (
    seg_id            TEXT PRIMARY KEY,      -- {mmsi}-{start_ts}-{model_version}
    mmsi              INTEGER NOT NULL,
    trip_id           TEXT,
    start_ts          INTEGER NOT NULL,
    end_ts            INTEGER NOT NULL,
    duration_s        INTEGER,
    activity          TEXT NOT NULL,         -- 第一层：可直接观察/可靠推断的状态
    sub_activity      TEXT,                  -- 第二层：需上下文支持的行为候选
    gear_type         TEXT,                  -- v1 恒为 unknown，不假装区分渔具
    confidence        REAL,                  -- NULL = 未校准
    evidence          TEXT,                  -- JSON
    reliability       TEXT,                  -- JSON：good/fair/poor + flags
    model_version     TEXT,
    created_at        INTEGER
);
CREATE INDEX IF NOT EXISTS idx_seg_mmsi_ts ON behavior_segments(mmsi, start_ts);
CREATE INDEX IF NOT EXISTS idx_seg_activity ON behavior_segments(activity, start_ts);

-- 航次特征向量（用于建立指纹）
CREATE TABLE IF NOT EXISTS trip_features (
    trip_id     TEXT PRIMARY KEY,
    mmsi        INTEGER NOT NULL,
    features    TEXT NOT NULL,               -- JSON 特征向量
    stratum     TEXT,                        -- 分层键，如 season=DJF
    created_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_tf_mmsi ON trip_features(mmsi);

-- 船舶长期行为指纹：按分层保存，带时间版本
CREATE TABLE IF NOT EXISTS vessel_fingerprints (
    fp_id        TEXT PRIMARY KEY,           -- {mmsi}-{stratum}-{model_version}
    mmsi         INTEGER NOT NULL,
    stratum      TEXT NOT NULL,
    n_trips      INTEGER,
    features     TEXT NOT NULL,              -- JSON：median / mad / p10 / p90 / iqr
    coverage_start INTEGER,
    coverage_end   INTEGER,
    data_version TEXT,
    model_version TEXT,
    created_at   INTEGER,
    updated_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_fp_mmsi ON vessel_fingerprints(mmsi, stratum);

-- 行为变化点
CREATE TABLE IF NOT EXISTS behavior_changes (
    change_id    TEXT PRIMARY KEY,
    mmsi         INTEGER NOT NULL,
    feature      TEXT NOT NULL,
    current_value REAL,
    baseline_median REAL,
    robust_z     REAL,
    direction    TEXT,                       -- up / down
    severity     TEXT,                       -- notable / strong
    ts           INTEGER,
    explanation  TEXT,
    fp_id        TEXT,
    created_at   INTEGER
);
CREATE INDEX IF NOT EXISTS idx_bc_mmsi ON behavior_changes(mmsi, ts);

-- 观测质量快照（每个查询窗口一份）
CREATE TABLE IF NOT EXISTS observation_quality (
    q_id            TEXT PRIMARY KEY,        -- {mmsi}-{window_end}-{model_version}
    mmsi            INTEGER NOT NULL,
    window_start    INTEGER,
    window_end      INTEGER,
    n_points        INTEGER,
    median_interval_s INTEGER,
    coverage_ratio  REAL,
    max_gap_s       INTEGER,
    n_gaps          INTEGER,
    upstream_ok_ratio REAL,
    neighborhood_ok_ratio REAL,
    grade           TEXT,                    -- good / fair / poor
    flags           TEXT,                    -- JSON 数组
    created_at      INTEGER
);
CREATE INDEX IF NOT EXISTS idx_oq_mmsi ON observation_quality(mmsi, window_end);

-- 异步分析任务（最小实现，无需 Redis）
CREATE TABLE IF NOT EXISTS analysis_jobs (
    job_id      TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    params      TEXT,
    status      TEXT,                        -- queued / running / done / failed
    progress    REAL DEFAULT 0,
    result_ref  TEXT,
    error       TEXT,
    created_at  INTEGER,
    started_at  INTEGER,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_job_status ON analysis_jobs(status, created_at);

-- 分析结果快照（可复现报告的最小载体）
CREATE TABLE IF NOT EXISTS analysis_results (
    result_id    TEXT PRIMARY KEY,
    job_id       TEXT,
    kind         TEXT,
    payload      TEXT,
    data_version TEXT,
    model_version TEXT,
    parameters   TEXT,
    quality_flags TEXT,
    created_at   INTEGER
);

-- World Port Index 港口点位（静态参考数据，来源 NGA WPI，约 3669 个）
CREATE TABLE IF NOT EXISTS ports (
    port_id     INTEGER PRIMARY KEY,          -- WPI INDEX_NO
    name        TEXT NOT NULL,
    country     TEXT,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    region_no   INTEGER,
    harbor_size TEXT,                         -- 中文标签：很小/小/中/大
    harbor_type TEXT,                         -- 中文标签：海岸天然/河流港池/...
    harbor_size_code TEXT,                    -- WPI 原始码 V/S/M/L，逻辑判断用这个
    harbor_type_code TEXT,                    -- WPI 原始码 CN/CB/RN/...
    shelter     TEXT,
    source      TEXT DEFAULT 'WPI',
    updated_at  INTEGER
);
CREATE INDEX IF NOT EXISTS idx_ports_geo ON ports(lat, lon);

-- Ocean Engine L1：轨迹点对应的环境特征缓存（按 mmsi+ts 幂等写入）
CREATE TABLE IF NOT EXISTS ocean_features (
    mmsi        INTEGER NOT NULL,
    ts          INTEGER NOT NULL,
    lat         REAL,
    lon         REAL,
    sst_c       REAL,
    sss_psu     REAL,
    ssh_m       REAL,
    mld_m       REAL,
    u_m_s       REAL,
    v_m_s       REAL,
    chl_mg_m3   REAL,
    wind_speed  REAL,
    wind_dir_deg  REAL,      -- 风来向（正北顺时针，风向的常规约定：表示风从哪来）
    wind_u_m_s  REAL,        -- 10 m 东向风分量
    wind_v_m_s  REAL,        -- 10 m 北向风分量
    wind_basis  TEXT,        -- observation_l4（卫星散射计融合）/ reanalysis（ERA5）
    wave_height_m REAL,      -- 有效波高 VHM0（m），CMEMS WAV 产品
    wave_dir_deg  REAL,      -- 波向 VMDR（来向，正北顺时针）
    wave_period_s REAL,      -- 平均周期 VTM02（s）
    speed_kn    REAL,
    product     TEXT,        -- 最近一次写入的环境产品
    products    TEXT,        -- 实际参与过的产品缩写，逗号分隔，如 physics,waves
    chl_basis   TEXT,        -- climatology：叶绿素是历史同月气候态，不是当前值
    chl_ref     TEXT,        -- 气候态参考年份区间
    quality_json TEXT,       -- 每个产品各自的 quality / basis / source_time / lag_hours
    source_time TEXT,        -- 环境数据真实时间（日/月均，不等于 ts，禁止当成该点实测）
    lag_hours   REAL,        -- 环境时刻与轨迹时刻的偏差（小时），公开给使用者判断时效
    data_version TEXT,
    fetched_at  INTEGER,
    quality     TEXT,        -- ok / land_mask / out_of_range / missing
    PRIMARY KEY (mmsi, ts)
);
CREATE INDEX IF NOT EXISTS idx_of_ts ON ocean_features(ts);

-- Ocean Engine L2：区域-时间切片缓存（避免重复请求同一片海）
CREATE TABLE IF NOT EXISTS ocean_cache_tiles (
    tile_id     TEXT PRIMARY KEY,            -- product|variables|round(t)|lat0|lon0
    product     TEXT,
    variables   TEXT,
    t0          INTEGER,
    t1          INTEGER,
    lat_min     REAL, lat_max REAL,
    lon_min     REAL, lon_max REAL,
    path        TEXT,
    bytes       INTEGER,
    created_at  INTEGER
);
"""


def connect(readonly: bool = False) -> sqlite3.Connection:
    settings.ensure_dirs()
    if readonly:
        conn = sqlite3.connect(f"file:{settings.db_path}?mode=ro", uri=True, timeout=30, check_same_thread=False)
    else:
        conn = sqlite3.connect(settings.db_path, timeout=30, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA busy_timeout=30000")
    if not readonly:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
    return conn


# 后续版本给已有表新增的列。
# SQLite 的 CREATE TABLE IF NOT EXISTS 不会改已存在的表，所以升级时要显式 ALTER。
_MIGRATIONS: dict[str, dict[str, str]] = {
    "ports": {
        "harbor_size_code": "TEXT",
        "harbor_type_code": "TEXT",
    },
    "ocean_features": {
        "lag_hours": "REAL",
        "wave_height_m": "REAL",
        "wave_dir_deg": "REAL",
        "wave_period_s": "REAL",
        "wind_dir_deg": "REAL",
        "wind_u_m_s": "REAL",
        "wind_v_m_s": "REAL",
        "wind_basis": "TEXT",
        "products": "TEXT",
        "chl_basis": "TEXT",
        "chl_ref": "TEXT",
        "quality_json": "TEXT",
    },
}


def _migrate(conn: sqlite3.Connection) -> list[str]:
    """补齐缺失的列，返回实际执行的变更，方便启动日志里看清楚。"""
    applied: list[str] = []
    for table, columns in _MIGRATIONS.items():
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        if not rows:
            continue  # 表还不存在，SCHEMA 会建好它
        existing = {r["name"] for r in rows}
        for column, ddl in columns.items():
            if column not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {ddl}")
                applied.append(f"{table}.{column}")
    if applied:
        conn.commit()
    return applied


def init_db() -> None:
    settings.ensure_dirs()
    conn = connect()
    try:
        conn.executescript(SCHEMA)
        conn.commit()
        _migrate(conn)
    finally:
        conn.close()


@contextmanager
def session(readonly: bool = False) -> Iterator[sqlite3.Connection]:
    conn = connect(readonly=readonly)
    try:
        yield conn
        if not readonly:
            conn.commit()
    except Exception:
        if not readonly:
            conn.rollback()
        raise
    finally:
        conn.close()


def get_meta(conn: sqlite3.Connection, key: str) -> str | None:
    row = conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
    return row["value"] if row else None


def set_meta(conn: sqlite3.Connection, key: str, value: str) -> None:
    conn.execute(
        "INSERT INTO meta(key, value) VALUES(?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (key, value),
    )


def db_size_mb() -> float:
    p: Path = settings.db_path
    total = p.stat().st_size if p.exists() else 0
    for suffix in ("-wal", "-shm"):
        f = Path(str(p) + suffix)
        if f.exists():
            total += f.stat().st_size
    return round(total / 1024 / 1024, 2)
