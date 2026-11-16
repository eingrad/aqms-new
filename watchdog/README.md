# AQMS watchdog (alerts)

A small container on the server that watches the stations' MQTT messages and sends a
**Telegram and / or email** message when something stops, a reminder every 6 h while it
lasts, and a message when it is fixed. Once a day (08:00) it sends an "all OK / N problems"
summary, so silence never means the watchdog itself is dead.

| Alert | When |
|---|---|
| **Station OFFLINE** | nothing from the station for 5 min: PC off, power cut, internet down, logger service stopped. The message says which, from the logger's online/offline flag. |
| **Instrument - no data** | the Fidas / an FPI analyzer / the GC stops answering for 10-15 min while the station is online |
| **Value - no data** | an instrument answers but sends no valid value for hours (stuck in calibration, failure mode, wrong unit, GC stopped) |
| **MQTT broker unreachable** | the watchdog lost the broker for 5 min (server problem) |

One fault gives one alert: while a station is offline its instrument checks are held, and a
value check is held while its instrument is not answering (`needs:` in `watchdog.yaml`).
Thresholds and checks are in [watchdog.yaml](watchdog.yaml); change and restart to apply.

## Setup (on the server)

It uses the **same Telegram bot and SMTP2GO account as the dashboard** (TNB---AQMS-01), with
the same variable names - nothing new to create. Only the recipients are its own.

### 1. Copy the dashboard's bot + SMTP values
```bash
docker exec aqms-web-api printenv | grep -E 'TELEGRAM|SMTP'
```

### 2. Fill `server/.env` (next to this stack's docker-compose.yml)
```bash
AQMS_STATION_ID=...           # the logger's station_id
AQMS_STATION_NAME=...         # optional friendly name

# copied from step 1
TELEGRAM_BOT_TOKEN=...
SMTP_HOST=...
SMTP_PORT=...
SMTP_USER=...
SMTP_PASSWORD=...
SMTP_FROM=...

# who gets the watchdog alerts (several: comma separated)
ALERT_TELEGRAM_CHAT_ID=...    # your chat id (as in the dashboard's notification settings) or a group id
ALERT_EMAIL_TO=...
```
For a team, a Telegram **group** works well: add the bot to the group, post one message, then
open `https://api.telegram.org/bot<TOKEN>/getUpdates` and copy the group's `"chat":{"id": -...}`.

### 3. Test, then start
```bash
cd server
docker compose build aqms-watchdog
docker compose run --rm --no-deps aqms-watchdog python -m aqms_watchdog.main --config /app/watchdog.yaml --test-message
docker compose up -d --no-deps aqms-watchdog
docker compose logs -f aqms-watchdog      # "MQTT connected; watching aqms/+/readings ..."
```
`--no-deps` creates only the watchdog; the running broker / database are not touched.
The test message must arrive before you rely on it. Every alert is also written to the
container log, so `docker compose logs aqms-watchdog` shows the history.

### 4. Try it
Stop the logger service on the station PC (`nssm stop AQMS-Logger`): within ~6 min an
"OFFLINE ... service was stopped" message arrives; start it again for "back online".

## Notes
- It connects as the existing read-only `aqms_ingest` MQTT user; no broker change needed.
- The station side needs the logger version that publishes `aqms/<station_id>/logger`
  (the online/offline flag). An older logger still works, but the OFFLINE message then
  can't say *why* the station went quiet.
- Several stations: add more entries under `stations:` in `watchdog.yaml`.
- Tests: `pip install -r requirements.txt pytest && python -m pytest` in this folder.
