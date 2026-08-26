"""Store-and-forward: publish unsynced SQLite rows to MQTT, oldest first.

One MQTT message per timestamp carries all of that minute's readings. On a
successful (acknowledged) publish the timestamp's rows are marked synced. When
the broker is down, rows accumulate and are replayed in order on the next flush.
"""
from __future__ import annotations

import logging

from .mqtt_publisher import MqttPublisher
from .storage import Storage

log = logging.getLogger(__name__)


def flush(storage: Storage, publisher: MqttPublisher, topic: str,
          max_timestamps: int = 240) -> int:
    """Publish pending timestamps; return how many were acknowledged and marked synced."""
    if not publisher.connected:
        pending = storage.count_unsynced()
        if pending:
            log.info("broker offline; %d readings buffered locally", pending)
        return 0

    sent = 0
    for iso_time, station_id, readings in storage.fetch_unsynced_grouped(max_timestamps):
        payload = {"station_id": station_id, "ts": iso_time, "readings": readings}
        if publisher.publish_json(topic, payload):
            storage.mark_synced(iso_time)
            sent += 1
        else:
            # Stop on first failure; preserve order and retry next cycle.
            log.warning("publish failed at %s; will retry", iso_time)
            break

    if sent:
        log.info("forwarded %d timestamp(s) to %s", sent, topic)
    return sent
