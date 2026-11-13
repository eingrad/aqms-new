"""Source adapters. Each device `type` in config maps to one adapter class."""
from __future__ import annotations

from ..config import DeviceConfig
from .base import SourceAdapter
from .fpi_mysql import FpiMysqlAdapter
from .fpi_tcp import FpiTcpAdapter
from .modbus_tcp import ModbusTcpAdapter


def build_adapter(device: DeviceConfig, station_id: str) -> SourceAdapter:
    """Factory: instantiate the adapter for a device based on its `type`."""
    if device.type == "modbus_tcp":
        return ModbusTcpAdapter(device, station_id)
    if device.type == "fpi_tcp":
        return FpiTcpAdapter(device, station_id)
    if device.type == "fpi_mysql":
        return FpiMysqlAdapter(device, station_id)
    if device.type == "hj212_receiver":
        raise NotImplementedError(
            "device type 'hj212_receiver' is the phase-5 fallback — build it only if "
            "AQMS-Plus MySQL is unreachable"
        )
    raise ValueError(f"unknown device type {device.type!r}")
