"""FPI gas source (Option A): read the latest reading from FPI's AQMS-Plus MySQL.

AQMS-Plus (FPI's DAS) already polls the analyzers over their proprietary protocol,
unit-converts, and stores 1-minute values in MySQL. Rather than re-implementing the
proprietary protocol, we read the DAS table.

Everything schema-specific is in `device.options` (filled once the real AQMS-Plus DB
is inspected), so this adapter needs no code changes per site:

    options:
      host, port, database, user, password   # connection
      table:        realtime_data            # DAS table holding current values
      time_column:  data_time                # its timestamp column
      columns: { O3: o3_conc, SO2: so2_conc, CO: co_conc, NOX: nox_conc }
      units:   { O3: ug/m3, SO2: ug/m3, CO: mg/m3, NOX: ug/m3 }   # optional
      max_age_seconds: 120                    # DAS row older than this -> comm-fail
      device_time_is_utc: false               # DAS timestamps are usually local

The reading time is the logger's poll boundary (`now`) so FPI gases align to the
same minute grid as the Fidas; freshness is guarded by max_age_seconds.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from ..config import DeviceConfig
from ..models import Reading, STATUS_COMM_FAIL, STATUS_VALID
from .base import SourceAdapter

log = logging.getLogger(__name__)


class FpiMysqlAdapter(SourceAdapter):
    def __init__(self, device: DeviceConfig, station_id: str, connect=None):
        self.cfg = device
        self.name = device.name
        self.station_id = station_id
        o = device.options
        self.host = o.get("host", "127.0.0.1")
        self.port = int(o.get("port", 3306))
        self.database = o["database"]
        self.user = o["user"]
        self.password = o.get("password", "")
        self.table = o["table"]
        self.time_column = o["time_column"]
        self.columns: dict[str, str] = o["columns"]      # {parameter: db_column}
        self.units: dict[str, str] = o.get("units", {})
        self.max_age = float(o.get("max_age_seconds", 120))
        self.device_time_is_utc = bool(o.get("device_time_is_utc", False))
        self._connect = connect or self._default_connect  # injectable for tests
        self.conn = None

    def _default_connect(self):
        import pymysql
        from pymysql.cursors import DictCursor
        return pymysql.connect(
            host=self.host, port=self.port, user=self.user,
            password=self.password, database=self.database,
            connect_timeout=5, read_timeout=5, cursorclass=DictCursor,
        )

    def _ensure_conn(self):
        if self.conn is None:
            self.conn = self._connect()
        return self.conn

    def _query_latest(self) -> dict | None:
        select_cols = ", ".join({self.time_column, *self.columns.values()})
        sql = f"SELECT {select_cols} FROM {self.table} ORDER BY {self.time_column} DESC LIMIT 1"
        conn = self._ensure_conn()
        with conn.cursor() as cur:
            cur.execute(sql)
            return cur.fetchone()

    def _is_fresh(self, das_time) -> bool:
        if not isinstance(das_time, datetime):
            return True  # can't evaluate; assume fresh
        if das_time.tzinfo is None:
            ref = datetime.now(timezone.utc) if self.device_time_is_utc else datetime.now()
        else:
            ref = datetime.now(timezone.utc)
        return (ref - das_time).total_seconds() <= self.max_age

    def read(self, now: datetime) -> list[Reading]:
        def gap(status=STATUS_COMM_FAIL):
            return [
                Reading(now, self.station_id, self.name, p, None, self.units.get(p, ""), status)
                for p in self.columns
            ]
        try:
            row = self._query_latest()
        except Exception as exc:  # connection/query error
            log.warning("%s: MySQL read failed: %s", self.name, exc)
            self._close_quietly()
            return gap()

        if not row:
            log.warning("%s: DAS table empty", self.name)
            return gap()
        if not self._is_fresh(row.get(self.time_column)):
            log.warning("%s: DAS row stale (%s); marking comm-fail", self.name, row.get(self.time_column))
            return gap()

        readings: list[Reading] = []
        for param, col in self.columns.items():
            raw = row.get(col)
            value = None if raw is None else round(float(raw), 4)
            status = STATUS_VALID if value is not None else STATUS_COMM_FAIL
            readings.append(Reading(now, self.station_id, self.name, param, value,
                                    self.units.get(param, ""), status))
        return readings

    def _close_quietly(self):
        try:
            if self.conn:
                self.conn.close()
        except Exception:
            pass
        self.conn = None

    def close(self) -> None:
        self._close_quietly()
