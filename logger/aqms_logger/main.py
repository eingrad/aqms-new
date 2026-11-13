"""Logger entry point.

Continuous mode: sample every `sample_interval_seconds`, average per parameter over each
minute (MinuteAggregator), then at the minute boundary store the 1-minute average to SQLite,
publish over MQTT (store-and-forward), and write the live value to Redis for the dashboard.

`--once`: a single spot read (no averaging) for quick commissioning decode checks.
"""
from __future__ import annotations

import argparse
import logging
import signal
import time
from datetime import datetime, timezone

from . import forwarder
from .adapters import build_adapter
from .adapters.fpi_tcp import parameters as fpi_tcp_parameters
from .adapters.fpi_tcp import status_parameters as fpi_tcp_status_parameters
from .adapters.synspec_gc import parameters as synspec_gc_parameters
from .adapters.synspec_gc import reductions as synspec_gc_reductions
from .aggregator import MinuteAggregator
from .config import load_config
from .live_redis import LivePublisher
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
        elif d.type == "fpi_tcp":
            params += fpi_tcp_parameters(d)
        elif d.type == "synspec_gc":
            params += synspec_gc_parameters(d)
    seen, ordered = set(), []
    for p in params:
        if p not in seen:
            seen.add(p)
            ordered.append(p)
    return ordered


def _handle_signal(signum, frame):
    global _running
    log.info("signal %s received; shutting down after this minute", signum)
    _running = False


def _print_readings(readings) -> None:
    print(f"\n{'device':<12}{'parameter':<22}{'value':>14}  {'unit':<9}status")
    print("-" * 63)
    for r in readings:
        cell = "(no data)".rjust(14) if r.value is None else f"{r.value:>14.4g}"
        print(f"{r.device:<12}{r.parameter:<22}{cell}  {r.unit:<9}{r.status}")


def _read_all(adapters, ts):
    readings = []
    for adapter in adapters:
        readings += adapter.read(ts)
    return readings


def _finalize_minute(minute_dt, averaged, storage, publisher, live, cfg):
    """Store + publish + cache one completed minute's averaged readings."""
    if not averaged:
        return
    storage.insert_readings(averaged)
    valid = sum(1 for r in averaged if r.value is not None)
    log.info("%s: 1-min average — %d params (%d valid) → SQLite", minute_dt.isoformat(),
             len(averaged), valid)
    if publisher is not None:
        forwarder.flush(storage, publisher, cfg.topic)
    live.publish(cfg.station_id, minute_dt, averaged)
    if minute_dt.minute == 0:
        pruned = storage.prune(cfg.retention_days)
        if pruned:
            log.info("pruned %d old synced rows", pruned)


def run(config_path: str, once: bool = False, no_mqtt: bool = False) -> None:
    cfg = load_config(config_path)
    log.info("station=%s devices=%d sample=%ss store=1min", cfg.station_id, len(cfg.devices),
             cfg.sample_interval_seconds)

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

    live = LivePublisher(cfg.redis)

    try:
        if once:
            # single spot read (no averaging) for commissioning
            ts = datetime.fromtimestamp((time.time() // 60) * 60, tz=timezone.utc)
            readings = _read_all(adapters, ts)
            storage.insert_readings(readings)
            _print_readings(readings)
            live.publish(cfg.station_id, ts, readings)
            if publisher is not None:
                time.sleep(1.0)  # let the network loop ack
                forwarder.flush(storage, publisher, cfg.topic)
            return

        sample_interval = max(1, cfg.sample_interval_seconds)
        # FPI <GAS>_STATUS and GC_STATUS codes: max per minute, so a calibration anywhere in the
        # minute shows. GC results: last per minute (one result per GC run, never blended).
        gc_last, gc_max = synspec_gc_reductions(cfg.devices)
        agg = MinuteAggregator(cfg.circular_params, cfg.last_params | gc_last,
                               cfg.max_params | fpi_tcp_status_parameters(cfg.devices) | gc_max)
        current_minute = (int(time.time()) // 60) * 60
        next_sample = time.time()
        while _running:
            now = time.time()
            this_minute = (int(now) // 60) * 60
            if this_minute > current_minute:
                # finalize the minute that just ended (from the samples gathered in it)
                minute_dt = datetime.fromtimestamp(current_minute, tz=timezone.utc)
                _finalize_minute(minute_dt, agg.build(minute_dt), storage, publisher, live, cfg)
                current_minute = this_minute

            agg.add(_read_all(adapters, datetime.fromtimestamp(now, tz=timezone.utc)))

            next_sample += sample_interval
            while _running and time.time() < next_sample:
                time.sleep(min(0.5, next_sample - time.time()))
    finally:
        for adapter in adapters:
            adapter.close()
        if publisher is not None:
            publisher.close()
        live.close()
        storage.close()
        log.info("stopped")


def main() -> None:
    parser = argparse.ArgumentParser(description="AQMS station logger")
    parser.add_argument("--config", default="config.yaml", help="path to config.yaml")
    parser.add_argument("--once", action="store_true", help="single spot read + exit (commissioning)")
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
