# AQMS-New — Modbus/Fidas → Local Logger → MQTT → TimescaleDB

Replacement data pipeline for the TNBJ air-quality station. Instruments are read digitally
(no more 4–20 mA), buffered locally, and streamed to a server over MQTT.

```
 Fidas 200 ──────────── Modbus-TCP :502 ─────────────────┐
                                                         ▼
 FPI AQMS-300/400/500/600 ─► AQMS-Plus DAS ─► MySQL ─(A) read ─┐   (phase 5)
                             (Windows)        └─(B) HJ212 push ─┤
                                                                ▼
        ┌──────── STATION LOGGER (Python) ────────┐  MQTT/TLS   ┌─── SERVER ───┐
        │ collect → SQLite (#1) → publish JSON     │  QoS1 :8883 │ ingest       │
        │ → MQTT ; store-and-forward buffer        │ ──────────► │ → TimescaleDB│
        └──────────────────────────────────────────┘             │ → hourly agg │
                                                                  └──────────────┘
```

## Two components
- **`logger/`** — runs at the station. Polls devices every minute, writes a local SQLite copy
  (storage #1), publishes each reading to MQTT with store-and-forward (nothing lost if the link drops).
- **`ingest/`** — runs on the server. Subscribes to MQTT and upserts into TimescaleDB (storage #2),
  which computes hourly averages via a continuous aggregate.

## Status (build phases)
- [x] 1 — scaffold, config, models, SQLite storage
- [x] 2 — Modbus-TCP adapter + decoder, Fidas 200 map
- [x] 3 — MQTT publisher, server ingest, TimescaleDB schema
- [x] 4 — store-and-forward + backfill
- [~] 5 — FPI gas source: `fpi_mysql` adapter **built + tested** (needs real AQMS-Plus schema values in config); `hj212_receiver` fallback not built (only if MySQL is unreachable)
- [ ] 6 — hourly continuous aggregate + 75% validity + retention (schema.sql ready; enable on server)
- [ ] 7 — service packaging (NSSM / systemd), docs

## Quick start (logger)
```bash
cd logger
python -m venv .venv && .venv/Scripts/activate    # Windows;  source .venv/bin/activate on Linux
pip install -r requirements.txt
cp config.example.yaml config.yaml                # edit device IPs, station_id
cp .env.example .env                              # put real MQTT/DB secrets here (NOT in git, NOT in OneDrive)
python -m aqms_logger.main --config config.yaml
```

**Commissioning the Fidas** — follow [docs/commissioning-fidas.md](docs/commissioning-fidas.md). Two helpers:
```bash
python -m aqms_logger.probe --host <FIDAS_IP> --port 502 --unit 1   # find the float word order
python -m aqms_logger.main --config config.yaml --once --no-mqtt    # verify decoding, SQLite only
```

## Quick start (ingest, on the server)
```bash
cd ingest
pip install -r requirements.txt
psql "$TSDB_URL" -f schema.sql                    # create hypertable + hourly aggregate
cp config.example.yaml config.yaml && cp .env.example .env
python -m aqms_ingest.main --config config.yaml
```

## Important
- **Secrets** (MQTT password, DB password, TLS keys) live only in `.env` on the target machine —
  never committed, never inside a cloud-synced folder (this repo currently sits under OneDrive;
  move it, or at minimum keep `.env` outside OneDrive). This is a deliberate fix for the old
  system, which hard-coded `sa` credentials everywhere.
- The Fidas float **word/byte order** (`word_order` in config) must be verified once against the
  instrument's on-screen values; default is `ABCD`, common alternative is `CDAB`.
