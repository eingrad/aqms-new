"""Commissioning helper: read the Fidas registers and print every word order side by side.

On first connection the Fidas float byte/word order (ABCD/CDAB/BADC/DCBA) is unknown.
Run this, then compare the columns to the values on the Fidas screen — the column that
matches reality is your `word_order`. TEMP / PRESSURE / HUMIDITY are the easiest to judge
(e.g. ~28 °C, ~1008 hPa, ~65 %). PM rows are shown raw in mg/m3 (×1000 for µg/m3).

    python -m aqms_logger.probe --host 10.0.0.50 --port 502 --unit 1
"""
from __future__ import annotations

import argparse
import inspect

from pymodbus.client import ModbusTcpClient

from .decode import WORD_ORDERS, decode_value

# address, parameter, register count  (Palas Fidas Modbus map, input registers / FC04)
FIDAS_MAP = [
    (37, "Cn (P/cm3)", 2), (39, "PM1 (mg/m3)", 2), (41, "PM2_5 (mg/m3)", 2),
    (43, "PM4 (mg/m3)", 2), (45, "PM10 (mg/m3)", 2), (47, "TSP (mg/m3)", 2),
    (49, "TEMP (degC)", 2), (51, "PRESSURE (hPa)", 2), (53, "HUMIDITY (%)", 2),
    (61, "WSP (km/h)", 2), (63, "WDIR (deg)", 2), (65, "PRECIP (l/m2/h)", 2),
]
ORDERS = ["ABCD", "CDAB", "BADC", "DCBA"]


def _unit_kwarg(client) -> str:
    params = inspect.signature(client.read_input_registers).parameters
    return "device_id" if "device_id" in params else "slave"


def main() -> None:
    ap = argparse.ArgumentParser(description="Probe Fidas Modbus word order")
    ap.add_argument("--host", required=True)
    ap.add_argument("--port", type=int, default=502)
    ap.add_argument("--unit", type=int, default=1, help="Modbus slave / unit id")
    ap.add_argument("--timeout", type=float, default=2.0)
    args = ap.parse_args()

    client = ModbusTcpClient(host=args.host, port=args.port, timeout=args.timeout)
    if not client.connect():
        raise SystemExit(f"ERROR: could not connect to {args.host}:{args.port}")

    kw = _unit_kwarg(client)
    start = min(a for a, _, _ in FIDAS_MAP)
    end = max(a + c for a, _, c in FIDAS_MAP)
    rr = client.read_input_registers(start, **{"count": end - start, kw: args.unit})
    if rr is None or rr.isError():
        client.close()
        raise SystemExit(f"ERROR: read failed: {rr}")
    block = {start + i: w for i, w in enumerate(rr.registers)}
    client.close()

    header = f"{'addr':>4}  {'parameter':<16}" + "".join(f"{o:>14}" for o in ORDERS)
    print(header)
    print("-" * len(header))
    for addr, name, count in FIDAS_MAP:
        words = [block[addr + i] for i in range(count)]
        cells = []
        for order in ORDERS:
            try:
                cells.append(f"{decode_value(words, 'float32', order):>14.4g}")
            except Exception:
                cells.append(f"{'err':>14}")
        print(f"{addr:>4}  {name:<16}" + "".join(cells))

    print("\nPick the column whose TEMP/PRESSURE/HUMIDITY match the Fidas display,")
    print("then set  word_order: <that column>  in config.yaml (default is ABCD).")
    print(f"(supported: {', '.join(sorted(WORD_ORDERS))})")


if __name__ == "__main__":
    main()
