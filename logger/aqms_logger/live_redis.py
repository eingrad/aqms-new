"""Best-effort Redis live cache for the dashboard.

Each completed minute, the current values are written to a Redis key as a JSON string
(`SET key <json> EX <ttl>`). The dashboard `GET`s that key every ~10 s; the TTL is what
tells the dashboard the data is fresh (if the logger stops, the key expires and the live
section goes stale). Redis errors NEVER block the SQLite/MQTT path.
"""
from __future__ import annotations

import json
import logging
from datetime import datetime, timezone

from .config import RedisConfig
from .models import Reading

log = logging.getLogger(__name__)


class LivePublisher:
    def __init__(self, cfg: RedisConfig, client=None):
        self.cfg = cfg
        self._client = client
        if cfg.enabled and client is None:
            try:
                import redis
                self._client = redis.Redis(
                    host=cfg.host, port=cfg.port, db=cfg.db,
                    password=cfg.password or None,
                    socket_timeout=2, socket_connect_timeout=2,
                )
                log.info("Redis live cache -> %s:%s key=%s", cfg.host, cfg.port,
                         cfg.key_template.format(station_id="<station>"))
            except Exception as exc:                       # missing lib or bad config
                log.error("Redis disabled (init failed): %s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def publish(self, station_id: str, minute: datetime, readings: list[Reading]) -> bool:
        if not self._client:
            return False
        values = {}
        for r in readings:
            if r.value is None:
                continue
            field = self.cfg.fields.get(r.parameter, r.parameter)
            values[field] = r.value
        key = self.cfg.key_template.format(station_id=station_id)
        payload = json.dumps({
            "station_id": station_id,
            "ts": minute.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "values": values,
        })
        try:
            self._client.set(key, payload, ex=self.cfg.ttl_seconds)
            return True
        except Exception as exc:                            # broker down, timeout, etc.
            log.warning("Redis set %s failed: %s", key, exc)
            return False

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
