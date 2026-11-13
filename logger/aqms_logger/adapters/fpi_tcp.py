"""FPI gas analyzers (AQMS-300 O3, AQMS-400 CO, AQMS-500 SO2, AQMS-600 NOx) read directly over
TCP port 10000 with FPI's own protocol - no AQMS Plus / MySQL in between.

One device entry per analyzer in config.yaml:

    - name: fpi_o3
      type: fpi_tcp
      host: 192.168.13.40
      port: 10000
      timeout: 1.5              # seconds to wait for each reply
      options:
        model: AQMS-300         # AQMS-300 | AQMS-400 | AQMS-500 | AQMS-600
        unit: ppb               # unit the gas values are stored and sent in (default: ppb, CO ppm)
        diagnostics: true       # also send flow / pressure / temperatures (default true)
        retry_seconds: 30       # after a failure, leave the analyzer alone this long

Each logger sample sends 2-3 small requests over one persistent TCP connection: the
concentration command, 0x27 status, and for the O3 (diagnostics on) 0x2C.

Units: every concentration reply carries the analyzer's unit code (1 ppb, 2 ppm, 3 ug/m3,
4 mg/m3). Values are converted to `unit` when that is exact: ppb <-> ppm and ug/m3 <-> mg/m3
(factor 1000), logged once. ppb/ppm <-> ug/m3 needs the molecular weight and a reference
temperature, so then the gas values are left blank and the log says which unit the analyzer
reports. The MQTT payload carries no units, so a column never mixes ppb and ppm.

Parameters (names never clash with the Fidas TEMP / PRESSURE / FLOW / STATUS):

    O3 | CO | SO2 | NO, NO2, NOX   gas value in `unit` - sent only while the analyzer is measuring
                                   (status 0). Blank during calibration / maintenance / failure,
                                   so cal gas never reaches the 1-minute average.
    <GAS>_STATUS                   FPI status code (0 measurement, 1-6/11/12 calibrations,
                                   7 diagnostic, 8 failure, 9/10 4-20 mA cal). Reduced with max
                                   per minute, so any non-zero code in the minute is kept.
    <GAS>_FLOW ... _TEMP           diagnostics (cc/min, psia, degC; O3: photometer volts),
                                   sent in every state, like the Fidas health values.

If an analyzer does not answer, its parameters are blank (comm-fail) for that sample and it is
not contacted again for `retry_seconds`, so a dead analyzer cannot slow the other devices.
"""
from __future__ import annotations

import logging
import socket
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable

from .. import fpi_protocol as fpi
from ..config import DeviceConfig
from ..models import (
    Reading, STATUS_ALARM, STATUS_CALIBRATION, STATUS_COMM_FAIL, STATUS_MAINTENANCE,
    STATUS_OUT_OF_RANGE, STATUS_VALID, tidy,
)
from .base import SourceAdapter

log = logging.getLogger(__name__)

EXTRA_RETRY_SECONDS = 600       # an optional diagnostics command that fails is skipped this long

# exact conversions only (same quantity, factor 1000)
_CONVERT = {("ppb", "ppm"): 1e-3, ("ppm", "ppb"): 1e3,
            ("ug/m3", "mg/m3"): 1e-3, ("mg/m3", "ug/m3"): 1e3}

_CALIBRATION_CODES = {0x01, 0x02, 0x03, 0x04, 0x05, 0x06, 0x0B, 0x0C}


@dataclass(frozen=True)
class Model:
    prefix: str                         # used for <prefix>_STATUS
    default_unit: str                   # used when options.unit is not set
    command: int                        # concentration command
    decode: Callable[[bytes], dict]
    gas: dict                           # decoder field -> parameter
    diagnostics: dict                   # decoder field -> (parameter, unit)
    extra: tuple = field(default=())    # optional diagnostics commands: ((cmd, decode), ...)


MODELS: dict[str, Model] = {
    "AQMS-300": Model(
        prefix="O3", default_unit="ppb", command=0x28, decode=fpi.decode_conc5,
        gas={"conc": "O3"},
        diagnostics={"meas_v": ("O3_MEAS_V", "V"), "ref_v": ("O3_REF_V", "V")},
        extra=((0x2C, fpi.decode_o3_2c),),
    ),
    "AQMS-400": Model(
        prefix="CO", default_unit="ppm", command=0x2C, decode=fpi.decode_co_2c,
        gas={"conc": "CO"},
        diagnostics={"flow": ("CO_FLOW", "cc/min"), "pressure": ("CO_PRESSURE", "psia"),
                     "bench_temp": ("CO_BENCH_TEMP", "degC"), "gfc_temp": ("CO_GFC_TEMP", "degC"),
                     "sensor_temp": ("CO_SENSOR_TEMP", "degC")},
    ),
    "AQMS-500": Model(
        prefix="SO2", default_unit="ppb", command=0x57, decode=fpi.decode_so2_57,
        gas={"conc": "SO2"},
        diagnostics={"flow": ("SO2_FLOW", "cc/min"), "pressure": ("SO2_PRESSURE", "psia"),
                     "gas_temp": ("SO2_GAS_TEMP", "degC")},
    ),
    "AQMS-600": Model(
        prefix="NOX", default_unit="ppb", command=0x3F, decode=fpi.decode_nox_3f,
        gas={"no": "NO", "no2": "NO2", "nox": "NOX"},
        diagnostics={"flow": ("NOX_FLOW", "cc/min"),
                     "sample_pressure": ("NOX_SAMPLE_PRESSURE", "psia"),
                     "reaction_pressure": ("NOX_REACTION_PRESSURE", "psia"),
                     "cell_temp": ("NOX_CELL_TEMP", "degC")},
    ),
}


def model_for(device: DeviceConfig) -> Model:
    name = str((device.options or {}).get("model", "")).upper()
    if name not in MODELS:
        raise ValueError(f"device {device.name!r}: options.model must be one of "
                         f"{sorted(MODELS)}, got {name or 'nothing'!r}")
    return MODELS[name]


def _diagnostics_on(device: DeviceConfig) -> bool:
    return bool((device.options or {}).get("diagnostics", True))


def parameters(device: DeviceConfig) -> list[str]:
    """Parameter names this device produces (used to create the SQLite columns)."""
    m = model_for(device)
    names = list(m.gas.values()) + [f"{m.prefix}_STATUS"]
    if _diagnostics_on(device):
        names += [p for p, _ in m.diagnostics.values()]
    return names


def status_parameters(devices) -> set[str]:
    """<GAS>_STATUS parameters of all fpi_tcp devices (reduced with max per minute)."""
    return {f"{model_for(d).prefix}_STATUS" for d in devices if d.type == "fpi_tcp"}


def _logger_status(code: int) -> int:
    if code in _CALIBRATION_CODES:
        return STATUS_CALIBRATION
    if code == 0x08:
        return STATUS_ALARM
    return STATUS_MAINTENANCE            # 0x07 diagnostic, 0x09/0x0A 4-20 mA cal, unknown


class FpiTcpAdapter(SourceAdapter):
    def __init__(self, device: DeviceConfig, station_id: str,
                 connect: Callable[[], socket.socket] | None = None,
                 clock: Callable[[], float] = time.monotonic):
        o = device.options or {}
        self.cfg = device
        self.name = device.name
        self.station_id = station_id
        self.model = model_for(device)
        self.unit = str(o.get("unit", self.model.default_unit))
        if self.unit not in fpi.UNITS.values():
            raise ValueError(f"device {device.name!r}: options.unit must be one of "
                             f"{sorted(fpi.UNITS.values())}, got {self.unit!r}")
        self.diagnostics = _diagnostics_on(device)
        self.retry_seconds = float(o.get("retry_seconds", 30))
        self.timeout = float(device.timeout)
        self.status_param = f"{self.model.prefix}_STATUS"
        self.parameters = parameters(device)
        self._connect = connect or self._default_connect     # injectable for tests
        self._clock = clock
        self._sock: socket.socket | None = None
        self._retry_at = 0.0
        self._down = False
        self._extra_retry_at: dict[int, float] = {}
        self._unit_seen: object = -1           # last unit code reported (logged once per change)

    # --- connection ------------------------------------------------------------
    def _default_connect(self) -> socket.socket:
        sock = socket.create_connection((self.cfg.host, self.cfg.port), timeout=self.timeout)
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        return sock

    def _close_sock(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    # --- polling -----------------------------------------------------------------
    def _poll(self, sock: socket.socket) -> dict:
        m = self.model
        fields = m.decode(fpi.request(sock, m.command, self.timeout))
        status = fpi.request(sock, fpi.CMD_STATUS, self.timeout)
        if not status:
            raise ValueError("empty status reply")
        fields["status"] = status[0]
        if self.diagnostics:
            for cmd, decode in m.extra:
                if self._clock() < self._extra_retry_at.get(cmd, 0.0):
                    continue
                try:
                    fields.update(decode(fpi.request(sock, cmd, self.timeout)))
                except (TimeoutError, ValueError) as exc:   # optional: keep the gas values
                    log.warning("%s: diagnostics 0x%02X failed (%s); skipping it for %d s",
                                self.name, cmd, exc, EXTRA_RETRY_SECONDS)
                    self._extra_retry_at[cmd] = self._clock() + EXTRA_RETRY_SECONDS
        return fields

    def read(self, now: datetime) -> list[Reading]:
        if self._clock() < self._retry_at:
            return self._comm_fail(now)
        for attempt in (1, 2):
            reused = self._sock is not None
            try:
                if self._sock is None:
                    self._sock = self._connect()
                fields = self._poll(self._sock)
                break
            except (OSError, ValueError) as exc:
                self._close_sock()
                if reused and attempt == 1:
                    continue            # connection went stale: retry once on a fresh one
                self._retry_at = self._clock() + self.retry_seconds
                if not self._down:
                    log.warning("%s: no answer from %s:%s (%s); retrying every %.0f s",
                                self.name, self.cfg.host, self.cfg.port, exc, self.retry_seconds)
                    self._down = True
                return self._comm_fail(now)
        if self._down:
            log.info("%s: answering again", self.name)
            self._down = False
        return self._readings(now, fields)

    # --- readings ----------------------------------------------------------------
    def _unit_factor(self, unit_code) -> float | None:
        """Factor from the analyzer's unit to `self.unit`, or None if it can't be converted."""
        reported = fpi.UNITS.get(unit_code)
        factor = 1.0 if reported == self.unit else _CONVERT.get((reported, self.unit))
        if unit_code != self._unit_seen:
            self._unit_seen = unit_code
            if factor is None:
                log.warning("%s: analyzer reports %s, which can't be converted to %s - gas values "
                            "left blank (change the analyzer's Unit Setting or options.unit)",
                            self.name, reported or f"unit code {unit_code}", self.unit)
            elif factor != 1.0:
                log.info("%s: analyzer reports %s; converting to %s", self.name, reported,
                         self.unit)
        return factor

    def _readings(self, now: datetime, fields: dict) -> list[Reading]:
        code = fields["status"]
        factor = self._unit_factor(fields.get("unit"))
        if code != 0:
            gas_status = _logger_status(code)
        elif factor is None:
            gas_status = STATUS_OUT_OF_RANGE
        else:
            gas_status = STATUS_VALID

        out: list[Reading] = []
        for key, param in self.model.gas.items():
            value = fields.get(key) if gas_status == STATUS_VALID else None
            status = gas_status
            if gas_status == STATUS_VALID and value is None:
                status = STATUS_OUT_OF_RANGE            # analyzer sent NaN / ff ff ff ff
            out.append(Reading(now, self.station_id, self.name, param,
                               None if value is None else tidy(value * factor), self.unit,
                               status))
        out.append(Reading(now, self.station_id, self.name, self.status_param,
                           float(code), "code", STATUS_VALID))
        if self.diagnostics:
            for key, (param, unit) in self.model.diagnostics.items():
                value = fields.get(key)
                out.append(Reading(now, self.station_id, self.name, param,
                                   None if value is None else round(value, 4), unit,
                                   STATUS_VALID if value is not None else STATUS_OUT_OF_RANGE))
        return out

    def _comm_fail(self, now: datetime) -> list[Reading]:
        units = {p: self.unit for p in self.model.gas.values()}
        units[self.status_param] = "code"
        units.update({p: u for p, u in self.model.diagnostics.values()})
        return [Reading(now, self.station_id, self.name, p, None, units[p], STATUS_COMM_FAIL)
                for p in self.parameters]

    def close(self) -> None:
        self._close_sock()
