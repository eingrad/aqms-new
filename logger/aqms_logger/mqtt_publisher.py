"""MQTT publisher over TLS with username/password auth (paho-mqtt v2).

Publishing is best-effort per call: if the broker is unreachable, `publish_json`
returns False and the caller leaves the rows unsynced for a later retry
(store-and-forward). The network loop runs in a background thread and
auto-reconnects.
"""
from __future__ import annotations

import json
import logging
import ssl

import paho.mqtt.client as mqtt

from .config import MqttConfig

log = logging.getLogger(__name__)


class MqttPublisher:
    def __init__(self, cfg: MqttConfig):
        self.cfg = cfg
        self._client = mqtt.Client(
            mqtt.CallbackAPIVersion.VERSION2,
            client_id=cfg.client_id,
            clean_session=False,          # keep session so QoS1 survives brief drops
            transport=cfg.transport,      # "tcp" or "websockets"
        )
        if cfg.transport == "websockets":
            self._client.ws_set_options(path=cfg.ws_path)
        if cfg.username:
            self._client.username_pw_set(cfg.username, cfg.password)
        if cfg.tls_ca:
            # self-hosted broker with a private CA
            self._client.tls_set(
                ca_certs=cfg.tls_ca,
                certfile=cfg.tls_cert or None,
                keyfile=cfg.tls_key or None,
                tls_version=ssl.PROTOCOL_TLS_CLIENT,
            )
        elif cfg.tls:
            # public edge cert (e.g. Cloudflare on :443) — trust the system CA store
            self._client.tls_set(tls_version=ssl.PROTOCOL_TLS_CLIENT)
        self._client.on_connect = self._on_connect
        self._client.on_disconnect = self._on_disconnect
        # Reconnect backoff between 1s and 2 minutes.
        self._client.reconnect_delay_set(min_delay=1, max_delay=120)

    # --- lifecycle -----------------------------------------------------------
    def connect(self) -> None:
        log.info("MQTT connecting to %s:%s", self.cfg.host, self.cfg.port)
        self._client.connect_async(self.cfg.host, self.cfg.port, keepalive=60)
        self._client.loop_start()

    def close(self) -> None:
        self._client.loop_stop()
        self._client.disconnect()

    @property
    def connected(self) -> bool:
        return self._client.is_connected()

    # --- publishing ----------------------------------------------------------
    def publish_json(self, topic: str, payload: dict, timeout: float = 5.0) -> bool:
        """Publish one JSON message at the configured QoS; return True once acknowledged."""
        if not self.connected:
            return False
        info = self._client.publish(topic, json.dumps(payload, default=str), qos=self.cfg.qos)
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            log.warning("publish rc=%s to %s", info.rc, topic)
            return False
        try:
            info.wait_for_publish(timeout)
        except (ValueError, RuntimeError) as exc:
            log.warning("wait_for_publish failed: %s", exc)
            return False
        return info.is_published()

    # --- callbacks -----------------------------------------------------------
    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("MQTT connected")
        else:
            log.warning("MQTT connect failed: %s", reason_code)

    def _on_disconnect(self, client, userdata, *args):
        # signature differs slightly across paho builds; reason code is the last positional
        reason = args[-1] if args else "unknown"
        log.warning("MQTT disconnected (%s); auto-reconnecting", reason)
