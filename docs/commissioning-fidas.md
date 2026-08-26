# Fidas 200 commissioning runbook

Bring up the **Fidas half** end-to-end (PM + met). The FPI gases are added later
(they haven't arrived) — leave the `fpi_gases` device commented out in config.

Do these in order; each step gates the next.

## 0. Install (station PC — a folder OUTSIDE OneDrive, e.g. C:\aqms\logger)
```
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
copy config.example.yaml config.yaml
copy .env.example .env
```

## 1. Network reachability
Confirm the station PC can reach the Fidas over Modbus-TCP:
```
Test-NetConnection <FIDAS_IP> -Port 502
```
`TcpTestSucceeded : True` is required before continuing. Then set `host:` (and confirm
`unit_id:`) for the `fidas200` device in `config.yaml`.

## 2. Determine the float word order (one-time)
```
.venv\Scripts\python -m aqms_logger.probe --host <FIDAS_IP> --port 502 --unit 1
```
It prints every parameter decoded four ways (ABCD/CDAB/BADC/DCBA). **Compare TEMP /
PRESSURE / HUMIDITY to the Fidas screen** — the column that matches reality is correct.
Set `word_order:` on the `fidas200` device (default `ABCD`).

## 3. Verify decoding (no broker yet)
```
.venv\Scripts\python -m aqms_logger.main --config config.yaml --once --no-mqtt
```
It prints the decoded readings and writes them to SQLite only. Check:
- PM values are in **µg/m³** (config applies ×1000 from the Fidas mg/m³) and look right.
- TEMP / PRESSURE / HUMIDITY / WSP / WDIR match the instrument.
- `status` is `0` (valid) for every row. `4` (comm-fail) means the read failed — recheck IP/unit/word_order.

## 4. Prepare the server (once)
On the TimescaleDB server:
```
psql "$TSDB_DSN" -f ingest/schema.sql          # hypertable + hourly aggregate + validity view
```
Create a least-privilege ingest role (INSERT/SELECT on `readings`) and put its DSN in
`ingest/.env`. Start the ingest service (see `ingest/deploy/aqms-ingest.service`).

## 5. Fill broker secrets and test end-to-end
Put the real MQTT host/port, username/password, and CA cert path in the logger `.env`.
Then one live cycle:
```
.venv\Scripts\python -m aqms_logger.main --config config.yaml --once
```
On the server, confirm the row arrived:
```
psql "$TSDB_DSN" -c "SELECT time, parameter, value, unit FROM readings ORDER BY time DESC LIMIT 20;"
```

## 6. Run continuously / install the service
Foreground soak test:
```
.venv\Scripts\python -m aqms_logger.main --config config.yaml
```
Watch a few minutes of `N readings (M valid) → SQLite` and `forwarded K timestamp(s)`.
Then install as a Windows service — see `logger/deploy/nssm.md`.

## 7. Confirm hourly averages (after ~1 h)
```
psql "$TSDB_DSN" -c "SELECT * FROM readings_hourly_valid ORDER BY bucket DESC LIMIT 20;"
```
`is_valid` is true once an hour has ≥45 of 60 valid minutes.

## Resilience check (optional but recommended)
Pull the network for a few minutes during step 6: readings keep landing in SQLite
(`synced=0`); when the link returns they backfill in order with no duplicates.
