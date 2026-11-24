-- FishEye PostGIS 版建表（v0.1 用 SQLite，此文件供数据量增长后迁移）
-- psql -f database/init_postgis.sql
CREATE EXTENSION IF NOT EXISTS postgis;

CREATE TABLE IF NOT EXISTS vessels (
    mmsi              BIGINT PRIMARY KEY,
    imo               TEXT,
    call_sign         TEXT,
    ship_name         TEXT,
    ship_type         INTEGER,
    ship_type_text    TEXT,
    fishing_identity  TEXT DEFAULT 'unknown',
    identity_source   TEXT,
    length            DOUBLE PRECISION,
    beam              DOUBLE PRECISION,
    draught           DOUBLE PRECISION,
    destination       TEXT,
    first_seen        TIMESTAMPTZ,
    updated_at        TIMESTAMPTZ
);

-- 轨迹按接收时间月分区
CREATE TABLE IF NOT EXISTS ais_positions (
    id          BIGSERIAL,
    mmsi        BIGINT NOT NULL,
    received_at TIMESTAMPTZ NOT NULL,
    lat         DOUBLE PRECISION NOT NULL,
    lon         DOUBLE PRECISION NOT NULL,
    sog         REAL,
    cog         REAL,
    heading     SMALLINT,
    nav_status  SMALLINT,
    msg_type    TEXT,
    geom        GEOMETRY(Point, 4326),
    PRIMARY KEY (id, received_at)
) PARTITION BY RANGE (received_at);

CREATE INDEX IF NOT EXISTS idx_pos_mmsi_time ON ais_positions (mmsi, received_at DESC);
CREATE INDEX IF NOT EXISTS idx_pos_geom ON ais_positions USING GIST (geom);

CREATE TABLE IF NOT EXISTS vessel_latest_state (
    mmsi       BIGINT PRIMARY KEY,
    received_at TIMESTAMPTZ NOT NULL,
    geom       GEOMETRY(Point, 4326),
    sog        REAL,
    cog        REAL,
    heading    SMALLINT,
    nav_status SMALLINT,
    identity   TEXT DEFAULT 'unknown',
    updated_at TIMESTAMPTZ DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_latest_geom ON vessel_latest_state USING GIST (geom);

CREATE TABLE IF NOT EXISTS fishing_events (
    id         BIGSERIAL PRIMARY KEY,
    mmsi       BIGINT NOT NULL,
    start_ts   TIMESTAMPTZ NOT NULL,
    end_ts     TIMESTAMPTZ NOT NULL,
    duration_s INTEGER NOT NULL,
    avg_sog    REAL,
    geom       GEOMETRY(Point, 4326),
    gear       TEXT,
    confidence REAL,
    evidence   JSONB,
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (mmsi, start_ts)
);

CREATE TABLE IF NOT EXISTS ais_gap_events (
    id               BIGSERIAL PRIMARY KEY,
    mmsi             BIGINT NOT NULL,
    gap_start        TIMESTAMPTZ NOT NULL,
    gap_end          TIMESTAMPTZ,
    duration_s       INTEGER,
    geom             GEOMETRY(Point, 4326),
    expected_sec     INTEGER,
    coverage_quality TEXT,
    stream_ok        BOOLEAN,
    event_status     TEXT,
    evidence         JSONB,
    created_at       TIMESTAMPTZ DEFAULT now(),
    UNIQUE (mmsi, gap_start)
);

CREATE TABLE IF NOT EXISTS geofences (
    id      TEXT PRIMARY KEY,
    name    TEXT NOT NULL,
    kind    TEXT,
    geom    GEOMETRY(Polygon, 4326),
    props   JSONB,
    source  TEXT
);

CREATE TABLE IF NOT EXISTS geofence_events (
    id         BIGSERIAL PRIMARY KEY,
    zone_id    TEXT NOT NULL REFERENCES geofences(id),
    mmsi       BIGINT NOT NULL,
    event_type TEXT NOT NULL,
    ts         TIMESTAMPTZ NOT NULL,
    geom       GEOMETRY(Point, 4326),
    created_at TIMESTAMPTZ DEFAULT now(),
    UNIQUE (zone_id, mmsi, event_type, ts)
);

CREATE TABLE IF NOT EXISTS stream_health (
    id          BIGSERIAL PRIMARY KEY,
    ts          TIMESTAMPTZ NOT NULL,
    connected   BOOLEAN NOT NULL,
    msgs_total  BIGINT,
    rate_1m     REAL,
    last_msg_ts TIMESTAMPTZ,
    bboxes      JSONB,
    note        TEXT
);
CREATE INDEX IF NOT EXISTS idx_health_ts ON stream_health (ts DESC);
