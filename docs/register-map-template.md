# Register-map template

Fill one table per Modbus device from its manual, then translate each row into a
`registers:` entry in `logger/config.yaml`. (FPI gases are **not** Modbus — see the
phase-5 note at the bottom.)

## Config field reference
| Field       | Meaning | Example |
|-------------|---------|---------|
| `parameter` | canonical name stored/published | `PM2_5` |
| `address`   | Modbus register address (protocol address, usually 0-based) | `41` |
| `count`     | number of registers | `2` (32-bit) / `1` (16-bit) |
| `type`      | `float32` \| `int32` \| `uint32` \| `int16` \| `uint16` | `float32` |
| `function`  | `input` (FC04) or `holding` (FC03) | `input` |
| `word_order`| `ABCD` (default) \| `CDAB` \| `BADC` \| `DCBA` — per register override | `ABCD` |
| `scale`,`offset` | value = raw × scale + offset | `scale: 1000` (mg→µg) |
| `unit`      | engineering unit stored with the value | `ug/m3` |

## Fidas 200 — DONE (prefilled in config.example.yaml)
Source: Palas "Modbus Protocol" (FW ≥1.0.0.435). Float32, input registers (FC04),
PM in mg/m³ (×1000 → µg/m³). Verify `word_order` once against the display.

| Parameter | Addr | Regs | Type | Unit (source) | Scale |
|-----------|------|------|------|---------------|-------|
| Cn        | 37 | 2 | float32 | P/cm³ | 1 |
| PM1       | 39 | 2 | float32 | mg/m³ | 1000 |
| PM2_5     | 41 | 2 | float32 | mg/m³ | 1000 |
| PM4       | 43 | 2 | float32 | mg/m³ | 1000 |
| PM10      | 45 | 2 | float32 | mg/m³ | 1000 |
| TSP       | 47 | 2 | float32 | mg/m³ | 1000 |
| TEMP      | 49 | 2 | float32 | °C | 1 |
| PRESSURE  | 51 | 2 | float32 | hPa | 1 |
| HUMIDITY  | 53 | 2 | float32 | % | 1 |
| WSP       | 61 | 2 | float32 | km/h | 1 (×0.27778 for m/s) |
| WDIR      | 63 | 2 | float32 | ° | 1 |
| PRECIP    | 65 | 2 | float32 | l/m²/h | 1 |

## Synspec Alpha 115 GC (CH4 / TNMHC) — DONE (adapter `synspec_gc`, not `registers:`)
Source: Synspec tech note E066 (2025-10-02), all blocks present from GCAlpha V3.4.7.12 (TNBJ:
V3.4.8.1). Function 3 (function 4 is identical), 0-based protocol addresses, float32 word order
ABCD. The adapter reads the blocks itself; only the component numbers and units go in config
(`options.components`). Runbook: [commissioning-gc.md](commissioning-gc.md).

| What | Addr | Regs | Type | Used by / notes |
|------|------|------|------|-----------------|
| GC clock | 30 | 7 | text DDMMYYYY HHMMSS | GC_DATA_AGE |
| Internal status | 181–184 | 4 | bit words (182 unused) | GC_STATUS, GC_STATUS_181/183/184 |
| System errors | 185–186 | 2 | bit words | GC_STATUS (185 bit 1 FID flame), GC_ERRORS_185/186 |
| Channel of the output data | 187 | 1 | uint16 | 0 = first run after a restart |
| Software version | 920 | 4 | uint16 ×4 | probe |
| Serial number | 924 | 2 | uint32 | probe (nameplate) |
| Component n value | 1526 + 2(n−1) | 2 | float32 | n = 1..40 → `options.components` |
| Cycle time | 1609 | 2 | int16 ×2 | seconds done / to go, −1 = no run (probe) |
| Inlet pressure | 1611 | 2 | float32 | GC_INLET_PRESSURE, bar |
| Column flow | 1613 | 2 | float32 | GC_COLUMN_FLOW, ccm |
| Component names | 1638 + 10(n−1) | 10 | text | probe, logged once per connection |
| Component units | 2038 + (n−1) | 1 | code | 0 not calibrated, 1 µg/m³, 2 ppb, 3 mg/m³, 4 ppm, 5 vol-%, 6 g/m³ |
| Retention times | 2078 + (n−1) | 1 | uint16 s | probe (0 = not detected) |
| Machine type | 2118 | 1 | code | probe (3 = GCAlpha) |
| Last run date / time | 2135 | 7 | "BCD" per E066 | GC_DATA_AGE; layout auto-detected (text / BCD / binary) |

The status bits that blank the values are listed in commissioning-gc.md; all bit meanings are in
`logger/aqms_logger/synspec_gc_map.py` (`STATUS_BITS`).

## <device name> — blank template
| Parameter | Addr | Regs | Type | Function | Unit | Scale | Offset | Notes |
|-----------|------|------|------|----------|------|-------|--------|-------|
|           |      |      |      |          |      |       |        |       |

## FPI AQMS-300/400/500/600 — NOT Modbus (phase 5)
The FPI analyzers speak the proprietary FPI-NEW-BUS protocol; they are collected by the
**AQMS-Plus DAS (MySQL)**. Do not add them as Modbus devices. They will be sourced via the
`fpi_mysql` adapter (preferred) or `hj212_receiver` (fallback) once MySQL reachability is
confirmed. For `fpi_mysql`, document instead: MySQL table name, timestamp column, and the
column→parameter mapping (e.g. `o3_conc → O3`).
