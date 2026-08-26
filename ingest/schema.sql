-- AQMS server schema (TimescaleDB / PostgreSQL) — storage #2.
-- Run once:  psql "$TSDB_URL" -f schema.sql
-- Requires the timescaledb extension.

CREATE EXTENSION IF NOT EXISTS timescaledb;

-- Raw 1-minute readings (long/narrow: variable parameter set needs no schema change).
CREATE TABLE IF NOT EXISTS readings (
    time        timestamptz      NOT NULL,
    station_id  text             NOT NULL,
    device      text             NOT NULL,
    parameter   text             NOT NULL,
    value       double precision,           -- NULL when status <> 0
    unit        text,
    status      smallint         NOT NULL DEFAULT 0
);

SELECT create_hypertable('readings', 'time', if_not_exists => TRUE);

-- Idempotency: re-sent rows (store-and-forward) never duplicate.
CREATE UNIQUE INDEX IF NOT EXISTS readings_uniq
    ON readings (time, station_id, device, parameter);

CREATE INDEX IF NOT EXISTS readings_lookup
    ON readings (station_id, device, parameter, time DESC);

-- Hourly averages via a continuous aggregate. Only valid samples (status = 0)
-- feed the average; valid_count lets us apply the completeness rule below.
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    station_id,
    device,
    parameter,
    avg(value)   FILTER (WHERE status = 0) AS avg_value,
    min(value)   FILTER (WHERE status = 0) AS min_value,
    max(value)   FILTER (WHERE status = 0) AS max_value,
    count(*)     FILTER (WHERE status = 0) AS valid_count,
    count(*)                               AS total_count,
    max(unit)                              AS unit
FROM readings
GROUP BY bucket, station_id, device, parameter
WITH NO DATA;

-- Keep the aggregate current (recompute the last few hours periodically).
SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists     => TRUE);

-- Completeness rule: an hourly average is "valid" only with >= 75% (45/60) valid minutes.
CREATE OR REPLACE VIEW readings_hourly_valid AS
SELECT
    bucket, station_id, device, parameter,
    CASE WHEN valid_count >= 45 THEN avg_value ELSE NULL END AS avg_value,
    min_value, max_value, valid_count, total_count,
    (valid_count >= 45) AS is_valid,
    unit
FROM readings_hourly;

-- Optional: drop raw data older than a year (hourly aggregate is retained).
-- SELECT add_retention_policy('readings', INTERVAL '365 days', if_not_exists => TRUE);
