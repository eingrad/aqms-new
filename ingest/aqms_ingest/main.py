"""Ingest entry point: MQTT subscribe → TimescaleDB upsert."""
from __future__ import annotations

import argparse
import logging
import signal

from .config import load_config
from .live_redis import LivePublisher
from .subscriber import Subscriber
from .timescale_writer import TimescaleWriter

log = logging.getLogger("aqms_ingest")


def main() -> None:
    parser = argparse.ArgumentParser(description="AQMS server ingest")
    parser.add_argument("--config", default="config.yaml")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args()

    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    cfg = load_config(args.config)
    writer = TimescaleWriter(cfg.dsn)
    live = LivePublisher(cfg.redis)
    subscriber = Subscriber(cfg.mqtt, writer, live)

    def _stop(signum, frame):
        log.info("signal %s; stopping", signum)
        subscriber.stop()

    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    try:
        subscriber.run()
    finally:
        live.close()
        writer.close()
        log.info("stopped")


if __name__ == "__main__":
    main()
