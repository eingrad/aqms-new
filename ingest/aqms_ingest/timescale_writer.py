"""Write readings into TimescaleDB with idempotent upserts."""
from __future__ import annotations

import logging

import psycopg2
from psycopg2.extras import execute_values

log = logging.getLogger(__name__)

_UPSERT = """
INSERT INTO readings (time, station_id, device, parameter, value, unit, status)
VALUES %s
ON CONFLICT (time, station_id, device, parameter) DO NOTHING
"""


def parse_payload(payload: dict) -> list[tuple]:
    """Turn one MQTT message into DB rows. Raises (KeyError/TypeError) on malformed input."""
    ts = payload["ts"]
    station_id = payload["station_id"]
    rows = []
    for r in payload["readings"]:
        rows.append((
            ts,
            station_id,
            r["device"],
            r["parameter"],
            r.get("value"),
            r.get("unit"),
            int(r.get("status", 0)),
        ))
    return rows


class TimescaleWriter:
    def __init__(self, dsn: str):
        self.dsn = dsn
        self.conn = None
        self._connect()

    def _connect(self) -> None:
        self.conn = psycopg2.connect(self.dsn)
        self.conn.autocommit = True
        log.info("connected to TimescaleDB")

    def upsert(self, rows: list[tuple]) -> int:
        if not rows:
            return 0
        for attempt in (1, 2):
            try:
                with self.conn.cursor() as cur:
                    execute_values(cur, _UPSERT, rows)
                return len(rows)
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
