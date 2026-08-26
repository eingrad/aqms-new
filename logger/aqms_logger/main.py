"""Logger entry point: poll every interval → SQLite (#1) → MQTT (store-and-forward)."""
from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timezone

from . import forwarder
from .adapters import build_adapter
from .config import load_config
from .mqtt_publisher import MqttPublisher
from .storage import Storage

log = logging.getLogger("aqms_logger")

_running = True


def _handle_signal(signum, frame):
    global _running
    log.info("signal %s received; shutting down after this cycle", signum)
    _running = False


def _cycle(adapters, storage, publisher, topic, ts):
    readings = []
    for adapter in adapters:
        readings += adapter.read(ts)
    inserted = storage.insert_readings(readings)
    valid = sum(1 for r in readings if r.value is not None)
    log.info("%s: %d readings (%d valid), %d new → SQLite", ts.isoformat(), len(readings), valid, inserted)
    forwarder.flush(storage, publisher, topic)


def run(config_path: str, once: bool = False) -> None:
    cfg = load_config(config_path)
    log.info("station=%s devices=%d interval=%ss", cfg.station_id, len(cfg.devices), cfg.poll_interval_seconds)

    storage = Storage(cfg.sqlite_path)

    adapters = []
    for device in cfg.devices:
        try:
            adapters.append(build_adapter(device, cfg.station_id))
        except NotImplementedError as exc:
            log.warning("skipping device %s: %s", device.name, exc)
    if not adapters:
        log.error("no usable devices configured; exiting")
        storage.close()
        return

    publisher = MqttPublisher(cfg.mqtt)
    publisher.connect()

    interval = cfg.poll_interval_seconds
    try:
        if once:
            ts = datetime.fromtimestamp((time.time() // interval) * interval, tz=timezone.utc)
            _cycle(adapters, storage, publisher, cfg.topic, ts)
            time.sleep(1.0)  # give the network loop a moment to ack
            forwarder.flush(storage, publisher, cfg.topic)
            return

        next_ts = (int(time.time()) // interval) * interval
        while _running:
            ts = datetime.fromtimestamp(next_ts, tz=timezone.utc)
            _cycle(adapters, storage, publisher, cfg.topic, ts)
            if ts.minute == 0:
                pruned = storage.prune(cfg.retention_days)
                if pruned:
                    log.info("pruned %d old synced rows", pruned)
            next_ts += interval
            while _running and time.time() < next_ts:
                time.sleep(min(1.0, next_ts - time.time()))
    finally:
        for adapter in adapters:
            adapter.close()
        publisher.close()
        storage.close()
        log.info("stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="AQMS station logger")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit (testing)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run(args.config, once=args.once)


if __name__ == "__main__":
    main()
