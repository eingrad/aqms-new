"""SQLite storage — WIDE table (one row per timestamp, a column per parameter).

The table is `readings(time, station_id, synced, created_at, <param columns...>)`.
Parameter columns are created automatically from the configured parameters
(`ensure_param_columns`), so the physical table is wide (like the old ASMA_RAWDATA)
while still not requiring a manual schema edit when a parameter is added.

An invalid/comm-fail reading is stored as NULL in its column (so a gap is explicit;
NULLs are ignored by AVG and don't count toward hourly completeness).
"""
from __future__ import annotations

import sqlite3
from datetime import datetime, timedelta, timezone

from .models import Reading

BASE_COLUMNS = {"time", "station_id", "synced", "created_at"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS readings (
    time        TEXT    NOT NULL,               -- ISO-8601 UTC, e.g. 2026-08-24T10:30:00Z
    station_id  TEXT    NOT NULL,
    synced      INTEGER NOT NULL DEFAULT 0,
    created_at  TEXT    NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    PRIMARY KEY (time, station_id)
);
CREATE INDEX IF NOT EXISTS idx_readings_unsynced ON readings(time) WHERE synced = 0;
"""


def _iso(ts: datetime) -> str:
    return ts.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Storage:
    def __init__(self, path: str):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL;")
        self.conn.execute("PRAGMA synchronous=NORMAL;")
        self.conn.executescript(_SCHEMA)
        self.conn.commit()

    def _columns(self) -> set[str]:
        return {row["name"] for row in self.conn.execute("PRAGMA table_info(readings)")}

    def ensure_param_columns(self, parameters) -> None:
        """Add a REAL column for each parameter that doesn't have one yet."""
        existing = self._columns()
        for p in parameters:
            if p not in existing:
                self.conn.execute(f'ALTER TABLE readings ADD COLUMN "{p}" REAL')
                existing.add(p)
        self.conn.commit()

    def param_columns(self) -> list[str]:
        return [c for c in self._columns() if c not in BASE_COLUMNS]

    def insert_readings(self, readings: list[Reading]) -> int:
        """Upsert readings into one wide row per (time, station_id). Returns rows touched."""
        groups: dict[tuple[str, str], dict[str, float | None]] = {}
        for r in readings:
            groups.setdefault((_iso(r.time), r.station_id), {})[r.parameter] = r.value

        touched = 0
        for (t, sid), vals in groups.items():
            cols = list(vals.keys())
            collist = ",".join(f'"{c}"' for c in cols)
            placeholders = ",".join("?" for _ in cols)
            updates = ",".join(f'"{c}"=excluded."{c}"' for c in cols)
            sql = (
                f"INSERT INTO readings(time, station_id, synced{',' + collist if cols else ''}) "
                f"VALUES(?,?,0{',' + placeholders if cols else ''}) "
                f"ON CONFLICT(time, station_id) DO UPDATE SET {updates}{',' if updates else ''} synced=0"
                if cols else
                "INSERT OR IGNORE INTO readings(time, station_id, synced) VALUES(?,?,0)"
            )
            args = [t, sid, *[vals[c] for c in cols]] if cols else [t, sid]
            self.conn.execute(sql, args)
            touched += 1
        self.conn.commit()
        return touched

    def fetch_unsynced(self, limit: int = 240) -> list[dict]:
        """Return whole unsynced rows (oldest first) as dicts."""
        cur = self.conn.execute(
            "SELECT * FROM readings WHERE synced = 0 ORDER BY time ASC LIMIT ?", (limit,)
        )
        return [dict(r) for r in cur]

    def mark_synced(self, iso_time: str, station_id: str) -> int:
        cur = self.conn.execute(
            "UPDATE readings SET synced = 1 WHERE time = ? AND station_id = ? AND synced = 0",
            (iso_time, station_id),
        )
        self.conn.commit()
        return cur.rowcount

    def count_unsynced(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM readings WHERE synced = 0").fetchone()[0]

    def prune(self, retention_days: int) -> int:
        cutoff = _iso(datetime.now(timezone.utc) - timedelta(days=retention_days))
        cur = self.conn.execute(
            "DELETE FROM readings WHERE synced = 1 AND time < ?", (cutoff,)
        )
        self.conn.commit()
        return cur.rowcount

    def close(self) -> None:
        self.conn.close()
