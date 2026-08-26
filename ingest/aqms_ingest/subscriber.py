"""MQTT subscriber that feeds messages into the TimescaleDB writer."""
from __future__ import annotations

import json
import logging
import ssl

import paho.mqtt.client as mqtt

from .config import MqttConfig
from .timescale_writer import TimescaleWriter, parse_payload

log = logging.getLogger(__name__)


class Subscriber:
    def __init__(self, cfg: MqttConfig, writer: TimescaleWriter):
        self.cfg = cfg
        self.writer = writer
        self._client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2, client_id=cfg.client_id)
        if cfg.username:
            self._client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls_ca:
            self._client.tls_set(
                ca_certs=cfg.tls_ca,
                certfile=cfg.tls_cert or None,
                keyfile=cfg.tls_key or None,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        self._client.on_connect = self._on_connect
        self._client.on_message = self._on_message
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    def run(self) -> None:
        self._client.connect(self.cfg.host, self.cfg.port, keepalive=60)
        self._client.loop_forever()

    def stop(self) -> None:
        self._client.disconnect()

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("connected; subscribing to %s", self.cfg.topic)
            client.subscribe(self.cfg.topic, qos=self.cfg.qos)
        else:
            log.warning("connect failed: %s", reason_code)

    def _on_message(self, client, userdata, msg):
        try:
            payload = json.loads(msg.payload.decode("utf-8"))
            rows = parse_payload(payload)
            n = self.writer.upsert(rows)
            log.info("%s: upserted %d rows (ts=%s)", msg.topic, n, payload.get("ts"))
        except (json.JSONDecodeError, KeyError, TypeError) as exc:
            log.warning("dropping malformed message on %s: %s", msg.topic, exc)
        except Exception as exc:  # DB error after retries — log and continue
            log.error("failed to persist message on %s: %s", msg.topic, exc)
