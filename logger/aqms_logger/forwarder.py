"""Store-and-forward: publish unsynced wide rows to MQTT, oldest first.

Each MQTT message carries one timestamp's wide row as a `values` map
{parameter: value}. On a successful (acknowledged) publish the row is marked
synced. When the broker is down, rows accumulate and replay in order.
"""
from __future__ import annotations

import logging

from .mqtt_publisher import MqttPublisher
from .storage import BASE_COLUMNS, Storage

log = logging.getLogger(__name__)


def _row_to_payload(row: dict) -> dict:
    values = {k: v for k, v in row.items() if k not in BASE_COLUMNS and v is not None}
    return {"station_id": row["station_id"], "ts": row["time"], "values": values}


def flush(storage: Storage, publisher: MqttPublisher, topic: str,
          max_rows: int = 240) -> int:
    """Publish pending rows; return how many were acknowledged and marked synced."""
    if not publisher.connected:
        pending = storage.count_unsynced()
        if pending:
            log.info("broker offline; %d rows buffered locally", pending)
        return 0

    sent = 0
    for row in storage.fetch_unsynced(max_rows):
        if publisher.publish_json(topic, _row_to_payload(row)):
            storage.mark_synced(row["time"], row["station_id"])
            sent += 1
        else:
            log.warning("publish failed at %s; will retry", row["time"])
            break

    if sent:
        log.info("forwarded %d row(s) to %s", sent, topic)
    return sent
