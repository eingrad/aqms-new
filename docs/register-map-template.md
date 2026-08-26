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
