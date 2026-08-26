"""SQLite storage — the local copy (#1) and the store-and-forward buffer.

Rows are inserted with synced=0. After a reading is acknowledged by the broker
it is marked synced=1. If the network is down, unsynced rows accumulate and are
replayed in timestamp order when connectivity returns (backfill).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .models import Reading

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    time        TEXT    NOT NULL,               -- ISO-8601 UTC, e.g. 2026-08-24T10:30:00Z
    station_id  TEXT    NOT NULL,
    device      TEXT    NOT NULL,
    parameter   TEXT    NOT NULL,
    value       REAL,                            -- NULL when status != valid
    unit        TEXT,
    status      INTEGER NOT NULL DEFAULT 0,
    synced      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    UNIQUE(time, device, parameter)
);
CREATE INDEX IF NOT EXISTS idx_readings_unsynced ON readings(time) WHERE synced = 0;

-- Convenience wide view: one row per minute, each parameter as a column (old-system layout).
-- Storage stays long; this is just a view. Add columns here when new parameters (e.g. FPI gases) arrive.
CREATE VIEW IF NOT EXISTS readings_wide AS
SELECT
    time,
    datetime(time, '+8 hours') AS time_myt,     -- Malaysia (UTC+8) for convenience
    station_id,
    MAX(CASE WHEN parameter='Cn'       THEN value END) AS Cn,
    MAX(CASE WHEN parameter='PM1'      THEN value END) AS PM1,
    MAX(CASE WHEN parameter='PM2_5'    THEN value END) AS PM2_5,
    MAX(CASE WHEN parameter='PM4'      THEN value END) AS PM4,
    MAX(CASE WHEN parameter='PM10'     THEN value END) AS PM10,
    MAX(CASE WHEN parameter='TSP'      THEN value END) AS TSP,
    MAX(CASE WHEN parameter='TEMP'     THEN value END) AS TEMP,
    MAX(CASE WHEN parameter='PRESSURE' THEN value END) AS PRESSURE,
    MAX(CASE WHEN parameter='HUMIDITY' THEN value END) AS HUMIDITY,
    MAX(CASE WHEN parameter='WSP'      THEN value END) AS WSP,
    MAX(CASE WHEN parameter='WDIR'     THEN value END) AS WDIR,
    MAX(CASE WHEN parameter='PRECIP'   THEN value END) AS PRECIP
FROM readings
GROUP BY time, station_id;
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Storage:
    def __init__(self, path: str):
        # check_same_thread=False so the paho network thread may also touch it if needed.
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def insert_readings(self, readings: list[Reading]) -> int:
        """Insert readings; duplicates on (time, device, parameter) are ignored."""
        rows = [
            (_iso(r.time), r.station_id, r.device, r.parameter, r.value, r.unit, r.status)
            for r in readings
        ]
        cur = self.conn.executemany(
            "INSERT OR IGNORE INTO readings(time, station_id, device, parameter, value, unit, status) "
            "VALUES(?,?,?,?,?,?,?)",
            rows,
        )
        self.conn.commit()
        return cur.rowcount

    def fetch_unsynced_grouped(self, max_timestamps: int = 240) -> list[tuple[str, str, list[dict]]]:
        """Return unsynced rows grouped by timestamp, oldest first.

        Each item is (iso_time, station_id, [reading-dicts]). Limited to
        `max_timestamps` distinct timestamps per call to bound a single flush.
        """
        times = [
            row["time"]
            for row in self.conn.execute(
                "SELECT DISTINCT time FROM readings WHERE synced = 0 ORDER BY time ASC LIMIT ?",
                (max_timestamps,),
            )
        ]
        groups: list[tuple[str, str, list[dict]]] = []
        for t in times:
            cur = self.conn.execute(
                "SELECT station_id, device, parameter, value, unit, status "
                "FROM readings WHERE synced = 0 AND time = ?",
                (t,),
            )
            items = [dict(r) for r in cur]
            station_id = items[0]["station_id"] if items else ""
            readings = [
                {k: it[k] for k in ("device", "parameter", "value", "unit", "status")}
                for it in items
            ]
            groups.append((t, station_id, readings))
        return groups

    def mark_synced(self, iso_time: str) -> int:
        cur = self.conn.execute(
            "UPDATE readings SET synced = 1 WHERE time = ? AND synced = 0", (iso_time,)
        )
        self.conn.commit()
        return cur.rowcount

    def count_unsynced(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM readings WHERE synced = 0").fetchone()[0]

    def prune(self, retention_days: int) -> int:
        """Delete SYNCED rows older than retention_days (unsynced rows are always kept)."""
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=retention_days))
        cur = self.conn.execute(
            "DELETE FROM readings WHERE synced = 1 AND time < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
