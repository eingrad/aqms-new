"""Write wide readings into TimescaleDB with idempotent upserts.

One row per (time, station_id); each parameter is its own column. Columns are
created on demand, so a new parameter never needs a manual migration (though the
hourly continuous aggregate must be extended for it — see schema.sql).
"""
from __future__ import annotations

import logging

import psycopg2
from psycopg2 import sql

log = logging.getLogger(__name__)


def parse_payload(payload: dict) -> tuple[str, str, dict]:
    """(ts, station_id, {parameter: value}). Raises on malformed input."""
    return payload["ts"], payload["station_id"], dict(payload["values"])


class TimescaleWriter:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None
        self._known_columns: set[str] = set()
        self._connect()

    def _connect(self) -> None:
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True
        self._known_columns = self._load_columns()
        log.info("connected to TimescaleDB (%d reading columns)", len(self._known_columns))

    def _load_columns(self) -> set[str]:
        with self.conn.cursor() as cur:
            cur.execute(
                "SELECT column_name FROM information_schema.columns WHERE table_name = 'readings'"
            )
            return {r[0] for r in cur.fetchall()}

    def _ensure_columns(self, params) -> None:
        for p in params:
            if p not in self._known_columns:
                with self.conn.cursor() as cur:
                    cur.execute(sql.SQL("ALTER TABLE readings ADD COLUMN IF NOT EXISTS {} double precision")
                                .format(sql.Identifier(p)))
                self._known_columns.add(p)
                log.info("added reading column %r", p)

    def upsert(self, ts: str, station_id: str, values: dict) -> int:
        if not values:
            return 0
        for attempt in (1, 2):
            try:
                self._ensure_columns(values.keys())
                cols = list(values.keys())
                insert_cols = [sql.Identifier("time"), sql.Identifier("station_id")] + \
                              [sql.Identifier(c) for c in cols]
                placeholders = sql.SQL(",").join(sql.Placeholder() * (len(cols) + 2))
                updates = sql.SQL(",").join(
                    sql.SQL("{c}=EXCLUDED.{c}").format(c=sql.Identifier(c)) for c in cols
                )
                query = sql.SQL(
                    "INSERT INTO readings ({cols}) VALUES ({vals}) "
                    "ON CONFLICT (time, station_id) DO UPDATE SET {updates}"
                ).format(cols=sql.SQL(",").join(insert_cols), vals=placeholders, updates=updates)
                with self.conn.cursor() as cur:
                    cur.execute(query, [ts, station_id, *[values[c] for c in cols]])
                return 1
            except (psycopg2.OperationalError, psycopg2.InterfaceError) as exc:
                log.warning("DB write failed (attempt %d): %s; reconnecting", attempt, exc)
                try:
                    self._connect()
                except Exception as reconnect_exc:
                    log.error("reconnect failed: %s", reconnect_exc)
                    if attempt == 2:
                        raise
        return 0

    def close(self) -> None:
        if self.conn:
            self.conn.close()
