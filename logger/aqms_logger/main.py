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


def _configured_parameters(devices) -> list[str]:
    """All parameter names across configured devices (for wide-table columns)."""
    params: list[str] = []
    for d in devices:
        params += [r.parameter for r in d.registers]
        if d.type == "fpi_mysql":
            params += list(d.options.get("columns", {}).keys())
    # de-duplicate, preserve order
    seen, ordered = set(), []
    for p in params:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


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
    if publisher is not None:
        forwarder.flush(storage, publisher, topic)
    return readings


def _print_readings(readings) -> None:
    print(f"\n{'device':<12}{'parameter':<12}{'value':>14}  {'unit':<8}status")
    print("-" * 52)
    for r in readings:
        cell = "(no data)".rjust(14) if r.value is None else f"{r.value:>14.4g}"
        print(f"{r.device:<12}{r.parameter:<12}{cell}  {r.unit:<8}{r.status}")


def run(config_path: str, once: bool = False, no_mqtt: bool = False) -> None:
    cfg = load_config(config_path)
    log.info("station=%s devices=%d interval=%ss", cfg.station_id, len(cfg.devices), cfg.poll_interval_seconds)

    storage = Storage(cfg.sqlite_path)
    storage.ensure_param_columns(_configured_parameters(cfg.devices))

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

    publisher = None
    if no_mqtt:
        log.info("MQTT disabled (--no-mqtt): readings are written to SQLite only")
    else:
        publisher = MqttPublisher(cfg.mqtt)
        publisher.connect()

    interval = cfg.poll_interval_seconds
    try:
        if once:
            ts = datetime.fromtimestamp((time.time() // interval) * interval, tz=timezone.utc)
            readings = _cycle(adapters, storage, publisher, cfg.topic, ts)
            _print_readings(readings)
            if publisher is not None:
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
        if publisher is not None:
            publisher.close()
        storage.close()
        log.info("stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="AQMS station logger")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="run a single cycle and exit (testing)")
    parser.add_argument("--no-mqtt", action="store_true",
                        help="do not publish; write to SQLite only (Fidas commissioning)")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    signal.signal(signal.SIGINT, _handle_signal)
    signal.signal(signal.SIGTERM, _handle_signal)

    run(args.config, once=args.once, no_mqtt=args.no_mqtt)


if __name__ == "__main__":
    main()
