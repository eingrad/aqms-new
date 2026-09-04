# AQMS server stack (dedicated)

Five containers, fully separate from `aquamind-prod-*`, on their own `aqms-net`:

| Container | Image | Role |
|---|---|---|
| `aqms-db` | timescale/timescaledb:pg15 | TimescaleDB (`aqms` database, storage #2) |
| `aqms-mqtt` | eclipse-mosquitto:2 | broker — `1883` internal (ingest) + `9001` websockets (tunnel) |
| `aqms-redis` | redis:7-alpine | live-value cache for the dashboard |
| `aqms-ingest` | built from `../ingest` | MQTT → TimescaleDB + Redis |
| `aqms-cloudflared` | cloudflare/cloudflared | tunnel exposing `mqtt.myaqms.com` (WSS) |

Nothing is published to the host. The station logger reaches the broker only through the
Cloudflare tunnel as `wss://mqtt.myaqms.com`.

## Deploy

1. **Secrets**
   ```bash
   cp .env.example .env      # then edit: DB/redis/mqtt passwords + tunnel token
   ```

2. **Create the Cloudflare tunnel** (Zero Trust → Networks → Tunnels → *Create*, name e.g. `aqms`):
   - Copy the **token** into `.env` as `AQMS_TUNNEL_TOKEN`.
   - Add a **Public Hostname**: `mqtt.myaqms.com` → type **HTTP** → URL `aqms-mqtt:9001`.
     (cloudflared upgrades the WebSocket automatically; Cloudflare serves the TLS for `mqtt.myaqms.com`.)

3. **Create the broker users** (before first full start), then the passwd file exists for the mount:
   ```bash
   mkdir -p mosquitto && touch mosquitto/passwd
   docker run --rm -v "$PWD/mosquitto/passwd:/passwd" eclipse-mosquitto:2 \
     mosquitto_passwd -b /passwd aqms_ingest  "$AQMS_MQTT_INGEST_PASSWORD"
   docker run --rm -v "$PWD/mosquitto/passwd:/passwd" eclipse-mosquitto:2 \
     mosquitto_passwd -b /passwd aqms_station "$AQMS_MQTT_STATION_PASSWORD"
   ```

4. **Start**
   ```bash
   docker compose up -d --build
   docker compose logs -f aqms-ingest      # should connect to DB + broker and wait for messages
   ```

5. **Verify** once the logger publishes:
   ```bash
   docker exec -it aqms-db psql -U aqms_ingest -d aqms -c \
     "SELECT time, station_id, \"PM2_5\", \"PM10\" FROM readings ORDER BY time DESC LIMIT 5;"
   docker exec -it aqms-redis redis-cli -a "$AQMS_REDIS_PASSWORD" GET aqms:TNBJ-AQMS1:live
   ```

## Notes
- The DB schema (`../ingest/schema.sql`) auto-loads on the **first** `aqms-db` start only. To
  re-apply after changes, run it manually with `psql`.
- `aqms-ingest` reads `../ingest/config.example.yaml` (mounted as `/app/config.yaml`); it uses
  container names (`aqms-db`, `aqms-mqtt`, `aqms-redis`) and pulls passwords from the env.
- The station **logger** connects with: `host: mqtt.myaqms.com`, `port: 443`,
  `transport: websockets`, `tls: true`, `username: aqms_station`.
