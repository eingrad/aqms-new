# Synspec Alpha 115 GC (CH4 / TNMHC) commissioning runbook

The Alpha 115 is a gas chromatograph with its own Windows PC running the GCAlpha software
(V3.4.8.1 at TNBJ-AQMS1, serial 51143). The logger reads it over **Modbus TCP, read-only**
(adapter `synspec_gc`). Register map: Synspec tech note E066; the registers used are listed in
[register-map-template.md](register-map-template.md).

Do these in order; each step gates the next.

## 1. On the GC: Modbus on the network port (done 2026-11-13)
GCAlpha → *Program Edit → Options → Datalogger communication* ("Datalogger Settings"):
- **MODBUS** ticked in the **TCP:9882** column (Hessen/Bayern is no longer used there).
- **Adress 001** → the logger's `unit_id: 1`.

## 2. On the GC: allow the logger PC
The GC answers only the IP addresses listed in its ID file (`GCxxx_ID.TXT` in the GC program
folder). Open it and check the TCP lines (the first two should already be there):
```
TCPACTIVE = TRUE
TCP-PORT = 9882
TCP-IP1 = 192.168.13.xxx      <- the logger PC's IP (ipconfig on the logger PC); TCP-IP1..TCP-IP4
```
Add the logger PC on a free `TCP-IPn` line if it is missing, save, and **restart the GC
software** so it re-reads the file. This interrupts the current run; the first run after a
restart is of unknown origin and the logger keeps it blank (GC_STATUS 5).
Optional while commissioning: `SHOWTCP = TRUE` opens a TCP spy window on the GC.

An unlisted PC shows up in the GC's `ALARM LOG.TXT` as `< Unregistered IP : x.x.x.x > Hacker alarm`.

## 3. Network reachability (logger PC)
```
Test-NetConnection <GC_IP> -Port 9882
```
`TcpTestSucceeded : True` is required. If it fails:
- If the Cat6 goes straight from the logger PC to the GC (no switch), give both ends a fixed IP
  in the same subnet.
- Check the Windows firewall on the GC PC allows inbound TCP 9882.

## 4. Probe the GC (read-only)
```
.venv\Scripts\python -m aqms_logger.gc_probe --host <GC_IP> --port 9882 --unit 1
```
Compare:
- machine type `3 = GCAlpha`, software version `3.4.8.1`, serial number `51143` (nameplate):
  proves the link, the unit id and the register map.
- **GC clock** = the time on the GC screen. **last run** decodes (not "can't decode") and the
  **data age** is less than about two cycles.
- **inlet pressure** ≈ the pressure on the GC screen (2.88 bar when photographed): proves the
  float word order (ABCD).
- status: `GC_STATUS 0` during normal measurement, channel `1`.
- **run in progress** prints the cycle length and a suggested `max_age_minutes`.
- **components**: numbers, names, units and the last values, plus a `components:` block to
  paste in step 5.

## 5. Config
Copy the `gc_alpha115` block from `config.example.yaml` into `config.yaml`, then set:
- `host:` the GC's IP.
- `options.components:` the numbers and units the probe printed. Keep the names `CH4` /
  `TNMHC` / `THC`: those are the hourly-average columns on the server. The unit must be the
  GC's unit or an exact conversion of it (ppb ↔ ppm). ppm ↔ mg/m³ needs a molecular weight, so
  the value would stay blank.
- `max_age_minutes:` what the probe suggested (3 cycles or more).
- `channel:` leave `1` unless the probe showed another channel for the ambient data.

## 6. Verify one read (no broker)
```
.venv\Scripts\python -m aqms_logger.main --config config.yaml --once --no-mqtt
```
- `CH4` / `TNMHC` equal the last results on the GC screen, status `0`.
- `GC_STATUS` 0 and `GC_DATA_AGE` in seconds.
- `4` (comm-fail) on every row: recheck host / port / unit and step 2.

## 7. Restart the logger service
```
nssm restart AQMS-Logger
```
The log shows `gc_alpha115: GC component 1 'Methane' -> CH4 (ppm)` once per connection.

## 8. Server
The ingest adds the `CH4` / `TNMHC` / `GC_*` columns by itself on the first message.
`ingest/schema.sql` includes CH4 and TNMHC in the hourly aggregate. A server whose
`readings_hourly` was created from an older schema.sql must recreate that view (TimescaleDB
can't add columns to an existing continuous aggregate).

## How the GC data is stored
- The GC delivers **one result per run** and keeps it on its outputs until the next run ends.
  Each minute stores the result on the outputs at the end of that minute (`last`, never a mean
  that would blend two runs), so the 45-of-60-minutes hourly rule works as for the other
  analyzers.
- The timestamp is the logger's minute, i.e. when the result was available: about one cycle
  after the air was sampled. `GC_DATA_AGE` (seconds, on the GC's own clock) records that lag.
- **Read-only**: the logger only uses function 3 (read) and never writes. The map also has
  registers that start / stop runs and set the GC clock.
- CH4 / TNMHC are **blank** while `GC_STATUS` is not 0 (max per minute, so a calibration
  anywhere in the minute shows):

| GC_STATUS | meaning | from |
|-----------|---------|------|
| 0 | ok - ambient data, values stored | |
| 1 | stale - no new run for `max_age_minutes` (a stopped GC keeps showing its last result) | last-run time vs GC clock |
| 2 | data from another stream-selector channel | 187 ≠ `options.channel` |
| 3 | maintenance | 183 bit 6 |
| 4 | calibration, auto validation, or span / zero gas data on the outputs | 183 bit 3, 184 bits 11-13 |
| 5 | output invalid, starting up, FID flame error, or first run after a restart | 181 bit 14, 183 bit 7, 185 bit 1, channel 0 |

Diagnostics: `GC_STATUS_181/183/184` and `GC_ERRORS_185/186` (raw bit words: the probe decodes
them, meanings in `aqms_logger/synspec_gc_map.py`), `GC_DATA_AGE` (s), `GC_INLET_PRESSURE` (bar),
`GC_COLUMN_FLOW` (ccm).

## Troubleshooting
- **No TCP connection**: step 3, then the port in step 1.
- **Connects but every read fails**: the logger PC is not listed (step 2), or the unit id is not
  the GC's *Adress*.
- **GC_STATUS 1 all the time**: `max_age_minutes` is shorter than two cycles, or the GC is not
  running (check its screen).
- **GC_STATUS 2**: set `options.channel` to the channel the probe shows.
- **Log "can't be converted" / "not calibrated"**: fix the unit in `options.components` (or the
  calibration on the GC).
- **Log "can't decode the GC clock / run time"**: E066 does not fully specify that layout. Keep
  the `registers` lines from the probe output for a decoder fix. Meanwhile the stale check falls
  back to "last-run registers unchanged for `max_age_minutes`".
