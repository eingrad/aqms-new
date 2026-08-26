# Architecture & operational notes

## Data model (both ends)
Long/narrow — one row per (time, device, parameter). Adding a parameter or a whole
device never changes the schema.

| column | type | notes |
|--------|------|-------|
| time | UTC timestamp | minute-aligned poll boundary |
| station_id | text | e.g. `TNBJ-AQMS1` |
| device | text | e.g. `fidas200`, `fpi_gases` |
| parameter | text | e.g. `PM2_5`, `O3` |
| value | float / NULL | NULL when `status != 0` |
| unit | text | e.g. `ug/m3` |
| status | smallint | see below |

Uniqueness key `(time, station_id, device, parameter)` is enforced on both SQLite and
TimescaleDB, which is what makes re-sends idempotent.

## Status codes
`0` valid · `1` maintenance · `2` calibration · `3` alarm · `4` comm-fail · `5` out-of-range.
A communication failure writes a row with `value=NULL, status=4` so an outage is an explicit
gap, not silently missing data. Only `status=0` samples feed hourly averages.

## Store-and-forward
1. Each minute the logger polls every device and inserts rows into SQLite with `synced=0`.
2. It publishes each timestamp's readings to MQTT (QoS 1, TLS). On broker acknowledgement the
   rows are marked `synced=1`.
3. If the broker is unreachable, rows accumulate; the next successful cycle replays them in
   timestamp order (oldest first). SQLite is both storage #1 and the durable send queue.
4. Synced rows older than `retention_days` are pruned; unsynced rows are never pruned.

## Hourly averages (server)
A TimescaleDB continuous aggregate (`readings_hourly`) averages only valid samples per hour.
`readings_hourly_valid` applies the 75% completeness rule (≥45 of 60 minutes) — below that the
hourly value is reported as NULL/invalid.

## Security posture (deliberate contrast with the old system)
- No hard-coded credentials. All secrets live in `.env` on the target machine only.
- TLS + auth to the broker; least-privilege DB role for ingest (INSERT/SELECT only).
- Keep the deployed folder outside OneDrive so secrets/DB are never cloud-synced.

## What's not built yet
- **FPI gas source (phase 5):** `fpi_mysql` (read AQMS-Plus MySQL, preferred) or
  `hj212_receiver` (fallback). Selected after the MySQL reachability check.
- Live-infra verification (real broker + TimescaleDB) — code paths are covered by unit,
  end-to-end Modbus socket, and cross-module contract tests.
