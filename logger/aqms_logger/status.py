"""Station health check: is the logger running, and is it getting data from every instrument?

    python -m aqms_logger.status --config C:\\aqms\\logger\\config.yaml

Reads config.yaml and the logger's SQLite (read only, safe while the logger runs) and shows:
  - the Windows service state (AQMS-Logger; --service "" to skip)
  - how old the last stored minute is (the logger stores one row per minute)
  - how many rows still wait to be sent to the server (internet or MQTT down)
  - per instrument: its state in the last stored minute, how much of the last hour it
    answered, and its main values

Exit code: 0 all OK, 1 something needs attention, 2 the logger's data can't be read.
Calibration is routine, so an analyzer that is calibrating does not count as a problem.
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime, timedelta, timezone

from . import fpi_protocol as fpi
from . import synspec_gc_map as gc
from .adapters import device_parameters
from .adapters.fpi_tcp import model_for
from .adapters.synspec_gc import STATUS_PARAM as GC_STATUS_PARAM
from .adapters.synspec_gc import options_for
from .config import DeviceConfig, LoggerConfig, load_config

STALE_SECONDS = 180     # last stored minute older than this: the logger is not storing
BACKLOG_ROWS = 3        # more unsent rows than this: the data is not reaching the server
_ISO = "%Y-%m-%dT%H:%M:%SZ"
_CONC_UNITS = {"ug/m3", "mg/m3", "ppb", "ppm"}

# states that need attention; CALIBRATING / DIAGNOSTIC / MAINTENANCE / OTHER CHANNEL are
# shown but are someone working on the instrument or routine
PROBLEM_STATES = {"NO ANSWER", "FAILURE", "INVALID", "STALE", "PARTIAL"}
_FPI_STATE = {0x07: "DIAGNOSTIC", 0x08: "FAILURE"}        # any other non-zero code: calibrating
_GC_STATE = {gc.STALE: "STALE", gc.OTHER_CHANNEL: "OTHER CHANNEL", gc.MAINTENANCE: "MAINTENANCE",
             gc.CALIBRATION: "CALIBRATING", gc.INVALID: "INVALID"}


def main_parameters(device: DeviceConfig) -> list[str]:
    """The values worth showing for a device (gas / PM concentrations, GC results)."""
    if device.type == "fpi_tcp":
        return list(model_for(device).gas.values())
    if device.type == "synspec_gc":
        o = options_for(device)
        return [c.parameter for c in o.components] + [d.parameter for d in o.derived]
    if device.type == "fpi_mysql":
        return list(device.options.get("columns", {}).keys())
    conc = [r.parameter for r in device.registers if r.unit in _CONC_UNITS]
    return conc or [r.parameter for r in device.registers][:5]


def device_state(device: DeviceConfig, row: dict) -> tuple[str, str]:
    """(STATE, detail) of one device from the last stored row."""
    if all(row.get(p) is None for p in device_parameters(device)):
        return "NO ANSWER", "no value from this instrument in the last stored minute"
    if device.type == "fpi_tcp":
        code = row.get(f"{model_for(device).prefix}_STATUS")
        if code:
            c = int(code)
            return (_FPI_STATE.get(c, "CALIBRATING"),
                    f"status {c} = {fpi.STATUS_TEXT.get(c, 'unknown')}; gas values blank meanwhile")
    if device.type == "synspec_gc":
        code = row.get(GC_STATUS_PARAM)
        if code:
            c = int(code)
            return _GC_STATE.get(c, "INVALID"), f"GC_STATUS {c} = {gc.STATE_TEXT.get(c, 'unknown')}"
    blank = [p for p in main_parameters(device) if row.get(p) is None]
    if blank:
        return "PARTIAL", "blank: " + ", ".join(blank)
    return "OK", ""


def _fmt(value) -> str:
    if value is None:
        return "-"
    return f"{value:.0f}" if abs(value) >= 10000 else f"{value:.4g}"


def _ago(seconds: float) -> str:
    seconds = max(0, int(seconds))
    if seconds < 120:
        return f"{seconds} s ago"
    if seconds < 7200:
        return f"{seconds // 60} min ago"
    if seconds < 172800:
        return f"{seconds // 3600} h {seconds % 3600 // 60:02d} min ago"
    return f"{seconds // 86400} days ago"


def _hour_pct(conn: sqlite3.Connection, params: list[str], since: str) -> int | None:
    if not params:
        return None
    total = conn.execute("SELECT COUNT(*) FROM readings WHERE time > ?", (since,)).fetchone()[0]
    if not total:
        return None
    cond = " OR ".join(f'"{p}" IS NOT NULL' for p in params)
    got = conn.execute(f"SELECT COUNT(*) FROM readings WHERE time > ? AND ({cond})",
                       (since,)).fetchone()[0]
    return round(100 * got / total)


def service_state(name: str) -> str | None:
    """State of a Windows service (RUNNING, STOPPED, NOT INSTALLED ...), None if not checked."""
    if not name or os.name != "nt":
        return None
    try:
        out = subprocess.run(["sc", "query", name], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError) as exc:
        return f"unknown ({exc})"
    m = re.search(r"STATE\s*:\s*\d+\s+(\w+)", out.stdout)
    if m:
        return m.group(1)
    return "NOT INSTALLED" if "1060" in out.stdout else "unknown"


def report(cfg: LoggerConfig, conn: sqlite3.Connection, now: datetime,
           service: tuple[str, str] | None = None) -> tuple[list[str], list[str]]:
    """(lines to print, problems). `service` is (name, state) or None."""
    lines: list[str] = []
    problems: list[str] = []
    if service is not None:
        name, state = service
        lines.append(f"Service {name}:".ljust(28) + state)
        if state != "RUNNING":
            problems.append(f"service {name} is {state}")

    conn.row_factory = sqlite3.Row
    columns = {r[1] for r in conn.execute("PRAGMA table_info(readings)")}
    last_row = conn.execute("SELECT * FROM readings ORDER BY time DESC LIMIT 1").fetchone()
    if last_row is None:
        lines.append("Last stored minute:".ljust(28) + "none yet")
        problems.append("the logger has not stored any data yet")
        return lines, problems
    row = dict(last_row)

    last = datetime.strptime(row["time"], _ISO).replace(tzinfo=timezone.utc)
    age = (now - last).total_seconds()
    fresh = age <= STALE_SECONDS
    lines.append("Last stored minute:".ljust(28)
                 + f"{last.astimezone():%Y-%m-%d %H:%M}  ({_ago(age)})  {'OK' if fresh else 'LATE'}")
    if not fresh:
        problems.append(f"no new data since {_ago(age)} - the logger is stopped or stuck")

    since = (last - timedelta(minutes=60)).strftime(_ISO)
    stored = conn.execute("SELECT COUNT(*) FROM readings WHERE time > ?", (since,)).fetchone()[0]
    lines.append("Minutes stored, last hour:".ljust(28) + f"{stored}/60")

    backlog = conn.execute("SELECT COUNT(*) FROM readings WHERE synced = 0").fetchone()[0]
    sending = backlog <= BACKLOG_ROWS
    lines.append("Waiting to send to server:".ljust(28)
                 + f"{backlog} row(s)  {'OK' if sending else 'NOT REACHING THE SERVER'}")
    if not sending:
        problems.append(f"{backlog} rows not sent to the server yet - internet or MQTT down "
                        "(they are kept and sent when the link is back)")

    lines += ["", f"{'Instrument':<16}{'State':<15}{'Last hour':<11}Values (last stored minute)"]
    for device in cfg.devices:
        state, detail = device_state(device, row)
        params = [p for p in device_parameters(device) if p in columns]
        pct = _hour_pct(conn, params, since)
        values = "  ".join(f"{p}={_fmt(row.get(p))}" for p in main_parameters(device))
        lines.append(f"{device.name:<16}{state:<15}{('-' if pct is None else f'{pct}%'):<11}{values}")
        if detail and state != "NO ANSWER":
            lines.append(f"{'':<16}-> {detail}")
        if state in PROBLEM_STATES:
            problems.append(f"{device.name}: {state.lower()}" + (f" ({detail})" if detail else ""))
    return lines, problems


def _db_path(config_path: str, cfg: LoggerConfig) -> str:
    """sqlite_path from the config; relative paths are next to config.yaml (the service's folder)."""
    path = cfg.sqlite_path
    if os.path.isabs(path):
        return path
    return os.path.join(os.path.dirname(os.path.abspath(config_path)), path)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="AQMS logger health check (read only)")
    ap.add_argument("--config", default="config.yaml", help="the logger's config.yaml")
    ap.add_argument("--db", help="SQLite file (default: sqlite_path of the config, next to it)")
    ap.add_argument("--service", default="AQMS-Logger", help='Windows service name ("" to skip)')
    args = ap.parse_args(argv)

    try:
        cfg = load_config(args.config)
    except Exception as exc:                         # missing file, bad YAML, missing .env var
        print(f"Cannot read {args.config}: {exc}")
        return 2
    db = args.db or _db_path(args.config, cfg)
    if not os.path.exists(db):
        print(f"No logger database at {db} - has the logger run with this config?")
        return 2

    state = service_state(args.service)
    service = (args.service, state) if state is not None else None
    conn = sqlite3.connect(db, timeout=5)
    try:
        conn.execute("PRAGMA query_only = ON")
        lines, problems = report(cfg, conn, datetime.now(timezone.utc), service)
    except sqlite3.Error as exc:
        print(f"Cannot read {db}: {exc}")
        return 2
    finally:
        conn.close()

    print(f"AQMS logger status - station {cfg.station_id} - {datetime.now():%Y-%m-%d %H:%M:%S}")
    print()
    print("\n".join(lines))
    print()
    if problems:
        print("RESULT: NEEDS ATTENTION")
        for p in problems:
            print(f"  - {p}")
        return 1
    print("RESULT: OK - the logger is running and every instrument is answering")
    return 0


if __name__ == "__main__":
    sys.exit(main())
