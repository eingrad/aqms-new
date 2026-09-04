"""Best-effort Redis live cache, written by the ingest service.

Redis is server-internal, so the remote logger can't reach it — instead the ingest
(which is on the server's Docker network) writes the latest values to Redis on every
received MQTT message. The dashboard `GET`s the key (~every 10 s); the TTL is the
freshness signal. Redis errors never interrupt DB ingestion.
"""
from __future__ import annotations

import json
import logging

from .config import RedisConfig

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
                log.info("Redis live cache -> %s:%s", cfg.host, cfg.port)
            except Exception as exc:
                log.error("Redis disabled (init failed): %s", exc)
                self._client = None

    @property
    def enabled(self) -> bool:
        return self._client is not None

    def publish(self, station_id: str, ts: str, values: dict) -> bool:
        if not self._client:
            return False
        mapped = {self.cfg.fields.get(k, k): v for k, v in values.items()}
        key = self.cfg.key_template.format(station_id=station_id)
        payload = json.dumps({"station_id": station_id, "ts": ts, "values": mapped})
        try:
            self._client.set(key, payload, ex=self.cfg.ttl_seconds)
            return True
        except Exception as exc:
            log.warning("Redis set %s failed: %s", key, exc)
            return False

    def close(self) -> None:
        try:
            if self._client is not None:
                self._client.close()
        except Exception:
            pass
