-- AQMS server schema (TimescaleDB / PostgreSQL) — storage #2, WIDE table.
-- Run once:  psql "$TSDB_URL" -f schema.sql
--
-- One row per (time, station_id); each parameter is its own column (like the old
-- ASMA_RAWDATA). The ingest service can ADD COLUMN new parameters automatically,
-- but to include a new parameter in the hourly aggregate you must add its
-- avg()/count() lines below and refresh the aggregate.

CREATE EXTENSION IF NOT EXISTS timescaledb;

CREATE TABLE IF NOT EXISTS readings (
    time        timestamptz NOT NULL,
    station_id  text        NOT NULL,
    -- Fidas 200 (particulate + met)
    "Cn"        double precision,
    "PM1"       double precision,
    "PM2_5"     double precision,
    "PM4"       double precision,
    "PM10"      double precision,
    "TSP"       double precision,
    "TEMP"      double precision,
    "PRESSURE"  double precision,
    "HUMIDITY"  double precision,
    "WSP"       double precision,
    "WDIR"      double precision,
    "PRECIP"    double precision,
    -- FPI gases (populated when those analyzers arrive)
    "O3"        double precision,
    "SO2"       double precision,
    "CO"        double precision,
    "NOX"       double precision,
    "NO"        double precision,
    "NO2"       double precision,
    "THC"       double precision,
    PRIMARY KEY (time, station_id)
);

SELECT create_hypertable('readings', 'time', if_not_exists => TRUE);
CREATE INDEX IF NOT EXISTS readings_station_time ON readings (station_id, time DESC);

-- Hourly aggregate: avg of each parameter + a non-null count per parameter
-- (count >= 45 of 60 minutes = the 75% completeness rule).
CREATE MATERIALIZED VIEW IF NOT EXISTS readings_hourly
WITH (timescaledb.continuous) AS
SELECT
    time_bucket('1 hour', time) AS bucket,
    station_id,
    avg("PM1")      AS "PM1",      count("PM1")      AS "PM1_n",
    avg("PM2_5")    AS "PM2_5",    count("PM2_5")    AS "PM2_5_n",
    avg("PM4")      AS "PM4",      count("PM4")      AS "PM4_n",
    avg("PM10")     AS "PM10",     count("PM10")     AS "PM10_n",
    avg("TSP")      AS "TSP",      count("TSP")      AS "TSP_n",
    avg("Cn")       AS "Cn",       count("Cn")       AS "Cn_n",
    avg("TEMP")     AS "TEMP",     count("TEMP")     AS "TEMP_n",
    avg("PRESSURE") AS "PRESSURE", count("PRESSURE") AS "PRESSURE_n",
    avg("HUMIDITY") AS "HUMIDITY", count("HUMIDITY") AS "HUMIDITY_n",
    avg("WSP")      AS "WSP",      count("WSP")      AS "WSP_n",
    avg("WDIR")     AS "WDIR",     count("WDIR")     AS "WDIR_n",
    avg("O3")       AS "O3",       count("O3")       AS "O3_n",
    avg("SO2")      AS "SO2",      count("SO2")      AS "SO2_n",
    avg("CO")       AS "CO",       count("CO")       AS "CO_n",
    avg("NOX")      AS "NOX",      count("NOX")      AS "NOX_n",
    avg("NO")       AS "NO",       count("NO")       AS "NO_n",
    avg("NO2")      AS "NO2",      count("NO2")      AS "NO2_n",
    avg("THC")      AS "THC",      count("THC")      AS "THC_n"
FROM readings
GROUP BY bucket, station_id
WITH NO DATA;

SELECT add_continuous_aggregate_policy('readings_hourly',
    start_offset      => INTERVAL '3 hours',
    end_offset        => INTERVAL '1 hour',
    schedule_interval => INTERVAL '30 minutes',
    if_not_exists     => TRUE);

-- Local-time convenience view (Malaysia UTC+8) over the raw wide table.
CREATE OR REPLACE VIEW readings_myt AS
SELECT (time AT TIME ZONE 'Asia/Kuala_Lumpur') AS time_myt, *
FROM readings;

-- Optional: drop raw data older than a year (hourly aggregate is retained).
-- SELECT add_retention_policy('readings', INTERVAL '365 days', if_not_exists => TRUE);
