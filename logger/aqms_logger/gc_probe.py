"""Commissioning helper for Synspec GCs (TNBJ-AQMS1: Alpha 115 methane / TNMHC analyser).

Reads, once, everything the logger uses and prints it next to what to compare it with: the
nameplate (machine type, serial number), Help > About (software version) and the GC screen
(clock, last run, pressure). Then the component list - names, units, last values - and a
`components:` block to paste into config.yaml. Read-only: it never writes to the GC.

    python -m aqms_logger.gc_probe --host 192.168.13.50 --port 9882 --unit 1
"""
from __future__ import annotations

import argparse
import re
from typing import Callable, Optional

from pymodbus.client import ModbusTcpClient

from . import synspec_gc_map as gc
from .adapters.synspec_gc import MAX_WORDS_PER_READ, unit_kwarg
from .decode import decode_value

FAILED = "(read failed)"
HINT = ("Check: the GC's IP address and cable; MODBUS ticked in the TCP column of the GC's "
        "Datalogger Settings, and that port; this PC's IP on a TCP-IPn line of the GC's ID file "
        "(an unlisted IP shows up as 'Hacker alarm' in the GC's ALARM LOG.TXT); the GC PC's "
        "firewall.")

Read = Callable[[int, int], Optional[list]]


class Reader:
    """read(address, count) -> registers, or None if the read failed (the error is kept).
    After two failures in a row it stops asking, so a silent GC doesn't take minutes."""

    def __init__(self, client, unit_id: int):
        self.client, self.unit_id = client, unit_id
        self.kw = unit_kwarg(client)
        self.errors: list[str] = []
        self._failed_in_a_row = 0

    def __call__(self, address: int, count: int) -> list[int] | None:
        if self._failed_in_a_row >= 2:
            return None
        words: list[int] = []
        try:
            while len(words) < count:
                n = min(count - len(words), MAX_WORDS_PER_READ)
                rr = self.client.read_holding_registers(address + len(words), count=n,
                                                        **{self.kw: self.unit_id})
                if rr is None or rr.isError() or len(rr.registers) < n:
                    raise ValueError(str(rr))
                words += list(rr.registers[:n])
        except Exception as exc:
            self._failed_in_a_row += 1
            self.errors.append(f"registers {address}..{address + count - 1}: {exc}")
            return None
        self._failed_in_a_row = 0
        return words


def _float(words: list[int], i: int, order: str = "ABCD") -> float:
    return decode_value(words[i:i + 2], "float32", order)


def _int16(word: int) -> int:
    return word - 0x10000 if word >= 0x8000 else word


def suggest_parameter(name: str) -> str:
    """Parameter name for a GC component name: CH4 for methane, else the name in capitals."""
    if name.strip().lower() == "methane":
        return "CH4"
    return re.sub(r"[^A-Z0-9]+", "_", name.upper()).strip("_") or "COMPONENT"


def _status_lines(status: list[int]) -> list[str]:
    lines = []
    for i, address in enumerate(range(gc.STATUS, gc.OUTPUT_CHANNEL)):
        if address == 182:                     # unused word
            continue
        word = status[i]
        names = [("*" if gc.blanks_values(address, bit) else "")
                 + gc.STATUS_BITS.get(address, {}).get(bit, f"bit {bit}")
                 for bit in range(16) if word >> bit & 1]
        lines.append(f"  {address}  0x{word:04X}  {', '.join(names) or '-'}")
    lines.append(f"  channel of the data on the outputs: {status[6]}  (options.channel; "
                 "1 = no stream selector, 0 = first run after a GC restart)")
    code = gc.state_code(status, channel=1)
    lines.append(f"  -> GC_STATUS {code}: {gc.STATE_TEXT[code]}")
    return lines


def report(read: Read) -> list[str]:
    """The probe's output lines; `read(address, count)` returns registers or None."""
    lines: list[str] = []
    add = lines.append

    add("Identity (compare with the nameplate and Help > About)")
    w = read(gc.MANUFACTURER, 10)
    add("  manufacturer       " + (gc.decode_text(w) if w else FAILED))
    w = read(gc.MACHINE_TYPE, 1)
    add("  machine type       " + (f"{w[0]} = {gc.MACHINE_TYPES.get(w[0], 'unknown')}" if w else FAILED))
    w = read(gc.SOFTWARE_VERSION, 4)
    add("  software version   " + (".".join(str(x) for x in w) if w else FAILED))
    w = read(gc.SERIAL_NUMBER, 2)
    add("  serial number      " + (str(w[0] << 16 | w[1]) if w else FAILED))

    add("")
    add("Clock and last run (compare with the GC screen)")
    clock = read(gc.CLOCK, 7)
    run = read(gc.RUN_TIME, 7)
    for label, words in (("GC clock", clock), ("last run", run)):
        if words is None:
            add(f"  {label:<18} {FAILED}")
            continue
        dt = gc.decode_datetime(words)
        shown = f"{dt[0]:%d-%m-%Y %H:%M:%S} ({dt[1]})" if dt else "can't decode"
        add(f"  {label:<18} {shown}   registers {' '.join(f'{x:04X}' for x in words)}")
    age = gc.data_age_seconds(clock, run) if clock and run else None
    add("  data age           " + (f"{age:.0f} s ({age / 60:.1f} min)" if age is not None else "unknown"))
    w = read(gc.CYCLE_TIME, 2)
    if w is None:
        add(f"  run in progress    {FAILED}")
    elif _int16(w[0]) < 0 or _int16(w[1]) < 0:
        add("  run in progress    no")
    else:
        past, left = _int16(w[0]), _int16(w[1])
        cycle = past + left
        add(f"  run in progress    {past} s done, {left} s to go: cycle about {cycle} s "
            f"-> max_age_minutes of {max(1, round(3 * cycle / 60))} or more")

    add("")
    add("Status (* = blanks the values)")
    status = read(gc.STATUS, gc.STATUS_WORDS)
    if status is None:
        add(f"  {FAILED}")
    else:
        lines += _status_lines(status)

    add("")
    add("Pressure and flow (compare with the GC screen - this also checks the word order)")
    w = read(gc.INLET_PRESSURE, 4)
    if w is None:
        add(f"  {FAILED}")
    else:
        add(f"  inlet pressure     {_float(w, 0):.4g} bar   (word order CDAB would read "
            f"{_float(w, 0, 'CDAB'):.4g})")
        add(f"  column flow        {_float(w, 2):.4g} ccm")

    add("")
    add("Components (those with a name on the GC; retention 0 s = not detected in the last run)")
    names = read(gc.NAMES, gc.NAME_WORDS * gc.MAX_COMPONENTS)
    units = read(gc.UNITS, gc.MAX_COMPONENTS)
    values = read(gc.VALUES, 2 * gc.MAX_COMPONENTS)
    retention = read(gc.RETENTION_TIMES, gc.MAX_COMPONENTS)
    found: list[tuple[int, str, str | None]] = []
    if names is None or units is None or values is None:
        add(f"  {FAILED}")
    else:
        add(f"  {'no':>3}  {'name':<20} {'unit':<15} {'last value':>12}  retention")
        for n in range(1, gc.MAX_COMPONENTS + 1):
            name = gc.decode_text(names[gc.NAME_WORDS * (n - 1):gc.NAME_WORDS * n])
            if not name:
                continue
            code = units[n - 1]
            unit = gc.UNIT_CODES.get(code)
            unit_text = unit or ("not calibrated" if code == 0 else f"code {code}")
            rt = f"{retention[n - 1]} s" if retention else "?"
            add(f"  {n:>3}  {name:<20} {unit_text:<15} {_float(values, 2 * (n - 1)):>12.6g}  {rt}")
            found.append((n, name, unit))
        if not found:
            add("  (no component has a name)")

    if found:
        add("")
        add("For config.yaml - keep the names CH4 / TNMHC / THC, those are the hourly-average columns:")
        add("      components:")
        for n, name, unit in found:
            line = f"        {n}: {{ parameter: {suggest_parameter(name)}, unit: {unit or 'ppm'} }}"
            add(line + ("" if unit else "    # not calibrated on the GC - check"))
    return lines


def main() -> None:
    ap = argparse.ArgumentParser(description="Read a Synspec GC once over Modbus TCP (read-only)")
    ap.add_argument("--host", required=True, help="the GC's IP address")
    ap.add_argument("--port", type=int, default=502,
                    help="port of the TCP column with MODBUS ticked in Datalogger Settings (TNBJ: 9882)")
    ap.add_argument("--unit", type=int, default=1, help="'Adress' in Datalogger Settings (default 1)")
    ap.add_argument("--timeout", type=float, default=3.0)
    args = ap.parse_args()

    client = ModbusTcpClient(host=args.host, port=args.port, timeout=args.timeout, retries=1)
    if not client.connect():
        raise SystemExit(f"ERROR: no TCP connection to {args.host}:{args.port}.\n{HINT}")
    try:
        read = Reader(client, args.unit)
        print(f"Synspec GC at {args.host}:{args.port}, unit {args.unit}\n")
        print("\n".join(report(read)))
        if read.errors:
            print("\nFailed reads:")
            for err in read.errors:
                print(f"  {err}")
            print(HINT)
    finally:
        client.close()


if __name__ == "__main__":
    main()
