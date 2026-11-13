"""Core data types shared across the logger."""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

# Reading status codes (stored as smallint on both ends).
STATUS_VALID = 0
STATUS_MAINTENANCE = 1
STATUS_CALIBRATION = 2
STATUS_ALARM = 3
STATUS_COMM_FAIL = 4        # device/link failure — value is NULL, gap is explicit
STATUS_OUT_OF_RANGE = 5

STATUS_NAMES = {
    STATUS_VALID: "valid",
    STATUS_MAINTENANCE: "maintenance",
    STATUS_CALIBRATION: "calibration",
    STATUS_ALARM: "alarm",
    STATUS_COMM_FAIL: "comm_fail",
    STATUS_OUT_OF_RANGE: "out_of_range",
}


def tidy(value: float) -> float:
    """Round away float noise before storing: 4 decimals, but values below 1 keep 6 significant
    digits, so gases in ppm (SO2 0.004851) are not cut to 0.0049."""
    return round(value, 4) if abs(value) >= 1 else float(f"{value:.6g}")


@dataclass(frozen=True)
class Reading:
    """One measurement at one timestamp for one parameter of one device.

    `time` is a timezone-aware UTC datetime aligned to the poll boundary.
    `value` is None when the reading could not be obtained (see `status`).
    """
    time: datetime
    station_id: str
    device: str
    parameter: str
    value: float | None
    unit: str
    status: int = STATUS_VALID

    def to_payload(self) -> dict:
        """Compact dict for the MQTT `readings` array (time is carried once, at the envelope)."""
        return {
            "device": self.device,
            "parameter": self.parameter,
            "value": self.value,
            "unit": self.unit,
            "status": self.status,
        }
