"""Watchdog entry point: subscribe to the stations' MQTT topics and send alerts.

    python -m aqms_watchdog.main --config watchdog.yaml
    python -m aqms_watchdog.main --config watchdog.yaml --test-message   # check Telegram / email
"""
from __future__ import annotations

import argparse
import json
import logging
import signal
import sys
import time

import paho.mqtt.client as mqtt

from .config import channels_from, load, stations_from
from .monitor import DailyReport, Message, Monitor
from .notify import Notifier

log = logging.getLogger("aqms_watchdog")
_running = True


def _stop(signum, frame):
    global _running
    _running = False


def _client(m: dict, monitor: Monitor) -> mqtt.Client:
    base = m.get("base_topic", "aqms")
    client = mqtt.Client(mqtt.CallbackAPIVersion.VERSION2,
                         client_id=m.get("client_id", "aqms-watchdog"))
    if m.get("username"):
        client.username_pw_set(m["username"], m.get("password", ""))

    def on_connect(c, userdata, flags, reason_code, properties=None):
        if reason_code == 0:
            log.info("MQTT connected; watching %s/+/readings and %s/+/logger", base, base)
            c.subscribe([(f"{base}/+/readings", 1), (f"{base}/+/logger", 1)])
        else:
            log.warning("MQTT connect failed: %s", reason_code)

    def on_message(c, userdata, msg):
        parts = msg.topic.split("/")
        try:
            payload = json.loads(msg.payload.decode("utf-8")) if msg.payload else {}
            if parts[-1] == "readings":
                if not monitor.on_readings(payload, time.time()):
                    log.debug("ignoring station %r (not in watchdog.yaml)", payload.get("station_id"))
            elif parts[-1] == "logger" and len(parts) >= 2:
                monitor.on_logger(parts[-2], payload)
        except (ValueError, KeyError, TypeError, AttributeError) as exc:
            log.warning("ignoring malformed message on %s: %s", msg.topic, exc)

    client.on_connect = on_connect
    client.on_message = on_message
    client.reconnect_delay_set(min_delay=1, max_delay=60)
    return client


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AQMS watchdog (alerts)")
    ap.add_argument("--config", default="watchdog.yaml")
    ap.add_argument("--test-message", action="store_true",
                    help="send one test message on every channel and exit")
    ap.add_argument("--log-level", default="INFO")
    args = ap.parse_args(argv)
    logging.basicConfig(level=args.log_level.upper(),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    raw = load(args.config)
    stations = stations_from(raw)
    notifier = Notifier(channels_from(raw.get("notify") or {}))
    if not notifier.channels:
        log.warning("no Telegram or email configured - alerts only appear in this log")
    else:
        log.info("alerts go to: %s", ", ".join(c.name for c in notifier.channels))

    if args.test_message:
        ok = notifier.send([Message("[AQMS] watchdog test message",
                                    "✅ Test message from the AQMS watchdog. "
                                    "Station alerts will arrive here.")])
        return 0 if ok and notifier.channels else 1

    now = time.time()
    monitor = Monitor(stations, utc_offset_hours=float(raw.get("utc_offset_hours", 8)),
                      repeat_hours=float(raw.get("repeat_hours", 6)), now=now)
    daily = DailyReport(raw.get("daily_report"), monitor.tz, now)
    m = raw.get("mqtt") or {}
    client = _client(m, monitor)
    client.connect_async(m.get("host", "aqms-mqtt"), int(m.get("port", 1883)), keepalive=60)
    client.loop_start()
    log.info("watching %s", ", ".join(f"{s.label} ({len(s.checks)} checks)" for s in stations))

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    interval = max(5.0, float(raw.get("check_interval_seconds", 30)))
    try:
        while _running:
            now = time.time()
            messages = monitor.evaluate(now, client.is_connected())
            if daily.due(now):
                messages.append(monitor.summary(now))
            notifier.send(messages)
            notifier.retry()
            if monitor.unknown:
                log.info("messages from stations not in watchdog.yaml: %s",
                         ", ".join(sorted(monitor.unknown)))
                monitor.unknown.clear()
            deadline = now + interval
            while _running and time.time() < deadline:
                time.sleep(0.5)
    finally:
        client.disconnect()
        client.loop_stop()
        log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())
