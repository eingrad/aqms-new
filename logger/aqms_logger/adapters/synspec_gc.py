"""Synspec gas chromatographs over Modbus TCP (TNBJ-AQMS1: Alpha 115 methane / TNMHC analyser,
GCAlpha software). Register map and decoding: synspec_gc_map.py (Synspec tech note E066).

    - name: gc_alpha115
      type: synspec_gc
      host: 192.168.13.XXX      # the GC's IP address
      port: 9882                # TCP column of the GC's Datalogger Settings, with MODBUS ticked
      unit_id: 1                # "Adress" in Datalogger Settings
      timeout: 2.0
      options:
        components:             # GC component number -> parameter + unit to store
          1: { parameter: CH4,  unit: ppb }
          2: { parameter: NMHC, unit: ppb }     # the GC calls it "TNMHC"
        channel: 1              # stream-selector channel carrying ambient air (1 = no selector)
        max_age_minutes: 60     # values blank when no new run has finished for this long
        diagnostics: true       # also store status words, data age, inlet pressure, column flow
        retry_seconds: 30       # after a failure, leave the GC alone this long

Read-only: each sample reads 5-6 small blocks with function 3 and never writes (the GC's map
also has registers that start/stop runs and set its clock).

A GC delivers one result per run and keeps it on its outputs until the next run ends. Each
stored minute therefore carries the result that was on the outputs at the end of that minute
(reduction `last`; a mean would blend two runs). The result belongs to a run that started
earlier: GC_DATA_AGE says how much earlier, in seconds on the GC's own clock.

The component values are blank, and GC_STATUS (max per minute) says why, when:

    5  invalid      the GC flags its output invalid, is starting up, has an FID flame error,
                    or the data is from the first run after a GC restart (output channel 0)
    4  calibration  calibration mode, auto validation, or span / zero gas data on the outputs
    3  maintenance  maintenance mode
    2  channel      the data is from another stream-selector channel than options.channel
    1  stale        no new run has finished for max_age_minutes (a stopped GC keeps showing
                    its last result)
    0  ok           ambient data, values stored

A component is also blank when the GC reports it "not calibrated" or in a unit that can't be
converted exactly to the configured one (ppb <-> ppm <-> vol%, ug/m3 <-> mg/m3 <-> g/m3); the
log says so once. If the GC does not answer, every parameter is blank (comm-fail) and the GC
is left alone for retry_seconds, so it cannot slow down the other devices.
"""
from __future__ import annotations

import inspect
import logging
import math
import time
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from pymodbus.client import ModbusTcpClient

from .. import synspec_gc_map as gc
from ..config import DeviceConfig
from ..decode import decode_value
from ..models import (
    Reading, STATUS_ALARM, STATUS_CALIBRATION, STATUS_COMM_FAIL, STATUS_MAINTENANCE,
    STATUS_OUT_OF_RANGE, STATUS_VALID, tidy,
)
from .base import SourceAdapter

log = logging.getLogger(__name__)

DEVICE_TYPE = "synspec_gc"
STATUS_PARAM = "GC_STATUS"
MAX_WORDS_PER_READ = 100            # per request; the Modbus limit is 125

# diagnostics: (parameter, unit, reduction per minute)
DIAGNOSTICS = (
    ("GC_STATUS_181", "bitfield", "last"),
    ("GC_STATUS_183", "bitfield", "last"),
    ("GC_STATUS_184", "bitfield", "last"),
    ("GC_ERRORS_185", "bitfield", "last"),
    ("GC_ERRORS_186", "bitfield", "last"),
    ("GC_DATA_AGE", "s", "last"),
    ("GC_INLET_PRESSURE", "bar", "mean"),
    ("GC_COLUMN_FLOW", "ccm", "mean"),
)

# what a blanked component value is marked as (shown by --once; the wide table stores the blank)
_VALUE_STATUS = {
    gc.STALE: STATUS_COMM_FAIL, gc.OTHER_CHANNEL: STATUS_MAINTENANCE,
    gc.MAINTENANCE: STATUS_MAINTENANCE, gc.CALIBRATION: STATUS_CALIBRATION,
    gc.INVALID: STATUS_ALARM,
}


@dataclass(frozen=True)
class Component:
    number: int                     # GC component number, 1..40
    parameter: str
    unit: str                       # unit the value is stored and sent in


@dataclass(frozen=True)
class Options:
    components: tuple[Component, ...]
    channel: int
    max_age_seconds: float
    diagnostics: bool
    retry_seconds: float


def options_for(device: DeviceConfig) -> Options:
    """Validate a synspec_gc device's options (raises ValueError naming the config problem)."""
    o = device.options or {}
    where = f"device {device.name!r}"
    raw = o.get("components")
    if not isinstance(raw, dict) or not raw:
        raise ValueError(f"{where}: options.components must map GC component numbers to "
                         "{ parameter: ..., unit: ... }")
    components: list[Component] = []
    for key, spec in raw.items():
        number = key if isinstance(key, int) else (int(key) if str(key).isdigit() else 0)
        if not 1 <= number <= gc.MAX_COMPONENTS:
            raise ValueError(f"{where}: component number {key!r} must be 1..{gc.MAX_COMPONENTS}")
        if not isinstance(spec, dict) or not spec.get("parameter"):
            raise ValueError(f"{where}: component {number} needs a parameter name")
        unit = str(spec.get("unit", ""))
        if unit not in gc.STORE_UNITS:
            raise ValueError(f"{where}: component {number} unit must be one of "
                             f"{sorted(gc.STORE_UNITS)}, got {unit!r}")
        components.append(Component(number, str(spec["parameter"]), unit))
    names = [c.parameter.lower() for c in components]
    if len(set(names)) != len(names):
        raise ValueError(f"{where}: two components have the same parameter name")
    channel = int(o.get("channel", 1))
    max_age = float(o.get("max_age_minutes", 60))
    if channel < 1 or max_age <= 0:
        raise ValueError(f"{where}: options.channel must be >= 1 and max_age_minutes > 0")
    return Options(
        components=tuple(sorted(components, key=lambda c: c.number)),
        channel=channel,
        max_age_seconds=max_age * 60.0,
        diagnostics=bool(o.get("diagnostics", True)),
        retry_seconds=float(o.get("retry_seconds", 30)),
    )


def parameters(device: DeviceConfig) -> list[str]:
    """Parameter names this device produces (used to create the SQLite columns)."""
    opts = options_for(device)
    names = [c.parameter for c in opts.components] + [STATUS_PARAM]
    if opts.diagnostics:
        names += [p for p, _, _ in DIAGNOSTICS]
    return names


def reductions(devices) -> tuple[set[str], set[str]]:
    """(last, max) parameter sets of all synspec_gc devices, for the minute aggregator."""
    last: set[str] = set()
    maximum: set[str] = set()
    for d in devices:
        if d.type != DEVICE_TYPE:
            continue
        opts = options_for(d)
        last |= {c.parameter for c in opts.components}
        maximum.add(STATUS_PARAM)
        if opts.diagnostics:
            last |= {p for p, _, how in DIAGNOSTICS if how == "last"}
    return last, maximum


def unit_kwarg(client) -> str:
    """The unit keyword was renamed `slave` -> `device_id` across pymodbus 3.x."""
    params = inspect.signature(client.read_holding_registers).parameters
    return "device_id" if "device_id" in params else "slave"


class GcUnavailable(Exception):
    """The GC did not answer a read (after one retry on a fresh connection)."""


class SynspecGcAdapter(SourceAdapter):
    def __init__(self, device: DeviceConfig, station_id: str, client=None,
                 clock: Callable[[], float] = time.monotonic):
        self.cfg = device
        self.name = device.name
        self.station_id = station_id
        self.opts = options_for(device)
        self.parameters = parameters(device)
        # retries=0: one attempt per request here; a dead GC is handled by the back-off below
        self.client = client or ModbusTcpClient(host=device.host, port=device.port,
                                                timeout=device.timeout, retries=0)
        self._unit_kw = unit_kwarg(self.client)
        self._clock = clock
        self._n = max(c.number for c in self.opts.components)
        self._retry_at = 0.0
        self._down = False
        self._names_logged = False
        self._unit_seen: dict[int, int] = {}
        self._run_words: list[int] | None = None
        self._run_changed_at = 0.0
        self._age_warned = False

    # --- Modbus ----------------------------------------------------------------------------
    def read_words(self, address: int, count: int) -> list[int]:
        """Registers address .. address+count-1 (function 3), in requests of <= 100."""
        words: list[int] = []
        while len(words) < count:
            n = min(count - len(words), MAX_WORDS_PER_READ)
            words += self._read_once(address + len(words), n)
        return words

    def _read_once(self, address: int, count: int) -> list[int]:
        for attempt in (1, 2):
            reused = bool(self.client.connected)
            try:
                if not self.client.connect():
                    raise ConnectionError(f"no connection to {self.cfg.host}:{self.cfg.port}")
                rr = self.client.read_holding_registers(
                    address, count=count, **{self._unit_kw: self.cfg.unit_id})
                if rr is None or rr.isError():
                    raise ValueError(f"read {address}+{count} refused: {rr}")
                if len(rr.registers) < count:
                    raise ValueError(f"read {address}+{count}: got {len(rr.registers)} registers")
                return list(rr.registers[:count])
            except Exception as exc:        # timeout, reset, refused, Modbus exception reply
                self._close_client()
                if reused and attempt == 1:
                    continue                # connection went stale: retry once on a fresh one
                raise GcUnavailable(str(exc)) from exc
        raise GcUnavailable("no answer")    # not reached

    def _close_client(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass

    # --- one sample ----------------------------------------------------------------------
    def _snapshot(self) -> dict[str, list[int]]:
        n = self._n
        snap = {
            "status": self.read_words(gc.STATUS, gc.STATUS_WORDS),
            "values": self.read_words(gc.VALUES, 2 * n),
            "units": self.read_words(gc.UNITS, n),
            "clock": self.read_words(gc.CLOCK, 7),
            "run": self.read_words(gc.RUN_TIME, 7),
        }
        if self.opts.diagnostics:
            snap["diag"] = self.read_words(gc.INLET_PRESSURE, 4)   # inlet pressure, column flow
        return snap

    def read(self, now: datetime) -> list[Reading]:
        if self._clock() < self._retry_at:
            return self._comm_fail(now)
        try:
            snap = self._snapshot()
        except GcUnavailable as exc:
            self._retry_at = self._clock() + self.opts.retry_seconds
            self._names_logged = False
            if not self._down:
                log.warning("%s: no answer from %s:%s (%s); retrying every %.0f s",
                            self.name, self.cfg.host, self.cfg.port, exc, self.opts.retry_seconds)
                self._down = True
            return self._comm_fail(now)
        if self._down:
            log.info("%s: answering again", self.name)
            self._down = False
        if not self._names_logged:
            self._log_names()
        return self._readings(now, snap)

    def _log_names(self) -> None:
        """Log which GC component each parameter is (once per connection; best effort)."""
        self._names_logged = True
        try:
            words = self.read_words(gc.NAMES, gc.NAME_WORDS * self._n)
        except GcUnavailable as exc:
            log.info("%s: could not read the component names (%s)", self.name, exc)
            return
        for c in self.opts.components:
            start = gc.NAME_WORDS * (c.number - 1)
            name = gc.decode_text(words[start:start + gc.NAME_WORDS])
            if name:
                log.info("%s: GC component %d %r -> %s (%s)",
                         self.name, c.number, name, c.parameter, c.unit)
            else:
                log.warning("%s: GC component %d has no name on the GC (not in use?) - check "
                            "options.components", self.name, c.number)

    # --- readings --------------------------------------------------------------------------
    def _float(self, words: list[int], offset: int) -> float | None:
        """float32 rounded to the 7 significant digits it carries, so 2.1 ppm (sent as
        2.0999999) becomes 2100 ppb rather than 2099.9999. None for NaN / infinity."""
        value = decode_value(words[offset:offset + 2], "float32", self.cfg.word_order)
        return float(f"{value:.7g}") if math.isfinite(value) else None

    def _unit_factor(self, c: Component, code: int) -> float | None:
        gc_unit = gc.UNIT_CODES.get(code)
        factor = gc.unit_factor(gc_unit, c.unit)
        if self._unit_seen.get(c.number) != code:          # log once per change
            self._unit_seen[c.number] = code
            if code == 0:
                log.warning("%s: GC component %d (%s) is not calibrated on the GC - its values "
                            "are left blank", self.name, c.number, c.parameter)
            elif factor is None:
                log.warning("%s: GC component %d (%s) is reported in %s, which can't be converted "
                            "to %s - its values are left blank (set its unit in "
                            "options.components to %s)", self.name, c.number, c.parameter,
                            gc_unit or f"unit code {code}", c.unit, gc_unit or "the GC's unit")
            elif factor != 1.0:
                log.info("%s: GC component %d (%s) is reported in %s; converting to %s",
                         self.name, c.number, c.parameter, gc_unit, c.unit)
        return factor

    def _stale(self, run_words: list[int], age: float | None) -> bool:
        """No new run for max_age: from the GC's own clock, or - if its date/time can't be
        decoded - from how long the run-time registers have stayed the same."""
        now = self._clock()
        if run_words != self._run_words:
            self._run_words = list(run_words)
            self._run_changed_at = now
        if age is not None:
            self._age_warned = False
            return age > self.opts.max_age_seconds
        if not self._age_warned:
            log.warning("%s: can't decode the GC clock / run time (run time registers %s); the "
                        "stale check uses how long they stay unchanged", self.name,
                        " ".join(f"{w:04X}" for w in run_words))
            self._age_warned = True
        return now - self._run_changed_at > self.opts.max_age_seconds

    def _readings(self, now: datetime, snap: dict[str, list[int]]) -> list[Reading]:
        status = snap["status"]
        age = gc.data_age_seconds(snap["clock"], snap["run"])
        code = gc.state_code(status, self.opts.channel, self._stale(snap["run"], age))

        out: list[Reading] = []
        for c in self.opts.components:
            raw = self._float(snap["values"], 2 * (c.number - 1))
            factor = self._unit_factor(c, snap["units"][c.number - 1])
            if code != gc.OK:
                value, value_status = None, _VALUE_STATUS[code]
            elif factor is None or raw is None:
                value, value_status = None, STATUS_OUT_OF_RANGE
            else:
                value, value_status = tidy(raw * factor), STATUS_VALID
            out.append(Reading(now, self.station_id, self.name, c.parameter, value, c.unit,
                               value_status))
        out.append(Reading(now, self.station_id, self.name, STATUS_PARAM, float(code), "code",
                           STATUS_VALID))

        if self.opts.diagnostics:
            diag = {
                "GC_STATUS_181": status[0], "GC_STATUS_183": status[2],
                "GC_STATUS_184": status[3], "GC_ERRORS_185": status[4],
                "GC_ERRORS_186": status[5], "GC_DATA_AGE": age,
                "GC_INLET_PRESSURE": self._float(snap["diag"], 0),
                "GC_COLUMN_FLOW": self._float(snap["diag"], 2),
            }
            for param, unit, _ in DIAGNOSTICS:
                v = diag[param]
                out.append(Reading(now, self.station_id, self.name, param,
                                   None if v is None else round(float(v), 4), unit,
                                   STATUS_VALID if v is not None else STATUS_OUT_OF_RANGE))
        return out

    def _comm_fail(self, now: datetime) -> list[Reading]:
        units = {c.parameter: c.unit for c in self.opts.components}
        units[STATUS_PARAM] = "code"
        units.update({p: u for p, u, _ in DIAGNOSTICS})
        return [Reading(now, self.station_id, self.name, p, None, units[p], STATUS_COMM_FAIL)
                for p in self.parameters]

    def close(self) -> None:
        self._close_client()
