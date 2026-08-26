"""Modbus-TCP source adapter (Fidas 200 and any Modbus device).

Reads the configured registers for a device in as few requests as possible:
registers are grouped by function code, then each contiguous span is read in
125-register chunks and sliced per register spec. A whole-group read failure
yields comm-fail Readings (value=None) so the outage is explicit downstream.
"""
from __future__ import annotations

import inspect
import logging
from datetime import datetime

from pymodbus.client import ModbusTcpClient

from ..config import DeviceConfig
from ..decode import decode_value
from ..models import Reading, STATUS_COMM_FAIL, STATUS_OUT_OF_RANGE, STATUS_VALID
from .base import SourceAdapter

log = logging.getLogger(__name__)

MAX_REGS_PER_READ = 125  # Modbus protocol limit for a single read


class ModbusTcpAdapter(SourceAdapter):
    def __init__(self, device: DeviceConfig, station_id: str):
        self.cfg = device
        self.name = device.name
        self.station_id = station_id
        self.client = ModbusTcpClient(host=device.host, port=device.port, timeout=device.timeout)
        # The unit/slave kwarg was renamed `slave` -> `device_id` across pymodbus 3.x.
        params = inspect.signature(self.client.read_input_registers).parameters
        self._unit_kw = "device_id" if "device_id" in params else "slave"

    def _read_regs(self, function: str, address: int, count: int):
        kwargs = {"count": count, self._unit_kw: self.cfg.unit_id}
        if function == "input":
            return self.client.read_input_registers(address, **kwargs)
        return self.client.read_holding_registers(address, **kwargs)

    def _read_range(self, function: str, start: int, span: int) -> dict[int, int] | None:
        """Read `span` registers from `start`, chunked; return {address: word} or None on failure."""
        for attempt in range(1, self.cfg.retries + 1):
            try:
                if not self.client.connect():
                    log.warning("%s: connect to %s:%s failed (attempt %d)",
                                self.name, self.cfg.host, self.cfg.port, attempt)
                    continue
                acc: dict[int, int] = {}
                addr, remaining, ok = start, span, True
                while remaining > 0:
                    n = min(remaining, MAX_REGS_PER_READ)
                    rr = self._read_regs(function, addr, n)
                    if rr is None or rr.isError():
                        log.warning("%s: read %s@%d+%d error: %s", self.name, function, addr, n, rr)
                        ok = False
                        break
                    for i, word in enumerate(rr.registers):
                        acc[addr + i] = word
                    addr += n
                    remaining -= n
                if ok:
                    return acc
            except Exception as exc:  # socket reset, timeout, protocol error
                log.warning("%s: modbus read exception (attempt %d): %s", self.name, attempt, exc)
                self.client.close()  # force a clean reconnect next attempt
        return None

    def read(self, now: datetime) -> list[Reading]:
        readings: list[Reading] = []

        groups: dict[str, list] = {}
        for reg in self.cfg.registers:
            groups.setdefault(reg.function, []).append(reg)

        for function, regs in groups.items():
            start = min(r.address for r in regs)
            end = max(r.address + r.count for r in regs)
            block = self._read_range(function, start, end - start)

            if block is None:
                readings += [
                    Reading(now, self.station_id, self.name, r.parameter, None, r.unit, STATUS_COMM_FAIL)
                    for r in regs
                ]
                continue

            for r in regs:
                try:
                    words = [block[r.address + i] for i in range(r.count)]
                    word_order = r.word_order or self.cfg.word_order
                    value = decode_value(words, r.type, word_order, r.scale, r.offset)
                    readings.append(
                        Reading(now, self.station_id, self.name, r.parameter,
                                round(value, 4), r.unit, STATUS_VALID)
                    )
                except (KeyError, ValueError) as exc:
                    log.warning("%s.%s: decode failed: %s", self.name, r.parameter, exc)
                    readings.append(
                        Reading(now, self.station_id, self.name, r.parameter, None, r.unit, STATUS_OUT_OF_RANGE)
                    )
        return readings

    def close(self) -> None:
        try:
            self.client.close()
        except Exception:
            pass
