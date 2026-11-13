"""Accumulate sub-minute samples and emit one averaged Reading per parameter per minute.

The logger samples every `sample_interval_seconds`; each sample's Readings are fed to
`add()`. At the minute boundary `build(minute)` returns the reduced Readings and resets
for the next minute. The reduction per parameter is:

    mean      (default)  arithmetic mean               — real measurements (PM, flow, temp)
    circular             circular mean of angles       — angular params (WDIR)
    last                 most recent sample this minute — discrete state codes (MODUS, MESSAGE)
    max                  max sample this minute         — latching status flags (COINCIDENCE)

`last`/`max` exist because a numeric mean is meaningless for discrete status codes
(averaging modus 1 and 2 → 1.5). `last` reports the state at the minute boundary; `max`
ensures a brief error flag that occurred at any point in the minute is not lost.
"""
from __future__ import annotations

import math
from datetime import datetime

from .models import Reading, STATUS_COMM_FAIL, STATUS_VALID, tidy


def circular_mean(degrees: list[float]) -> float | None:
    """Mean of angles in degrees via unit vectors (so 350° and 10° average to 0°, not 180°)."""
    if not degrees:
        return None
    s = sum(math.sin(math.radians(d)) for d in degrees)
    c = sum(math.cos(math.radians(d)) for d in degrees)
    if abs(s) < 1e-12 and abs(c) < 1e-12:
        return None  # opposing vectors cancel — direction undefined
    return math.degrees(math.atan2(s, c)) % 360.0


class MinuteAggregator:
    def __init__(
        self,
        circular_params: set[str] | None = None,
        last_params: set[str] | None = None,
        max_params: set[str] | None = None,
    ):
        self.circular_params = circular_params or set()
        self.last_params = last_params or set()
        self.max_params = max_params or set()
        # key: (station_id, device, parameter) -> {"values": [...], "unit": str}
        self._acc: dict[tuple[str, str, str], dict] = {}

    def add(self, readings: list[Reading]) -> None:
        """Fold one sample's readings into the current minute's accumulator."""
        for r in readings:
            entry = self._acc.setdefault((r.station_id, r.device, r.parameter),
                                         {"values": [], "unit": r.unit})
            entry["unit"] = r.unit
            if r.value is not None:
                entry["values"].append(r.value)

    def build(self, minute: datetime) -> list[Reading]:
        """Emit averaged Readings for `minute` and reset the accumulator.

        A parameter that got at least one valid sample is averaged (VALID);
        one that got none (all comm-fail) is emitted as None/COMM_FAIL so the
        gap is explicit.
        """
        out: list[Reading] = []
        for (station_id, device, parameter), entry in self._acc.items():
            values = entry["values"]
            if not values:
                out.append(Reading(minute, station_id, device, parameter, None,
                                   entry["unit"], STATUS_COMM_FAIL))
                continue
            if parameter in self.circular_params:
                avg = circular_mean(values)
            elif parameter in self.last_params:
                avg = values[-1]          # current state at the minute boundary
            elif parameter in self.max_params:
                avg = max(values)         # latch any flag raised during the minute
            else:
                avg = sum(values) / len(values)
            if avg is None:
                out.append(Reading(minute, station_id, device, parameter, None,
                                   entry["unit"], STATUS_COMM_FAIL))
            else:
                out.append(Reading(minute, station_id, device, parameter, tidy(avg),
                                   entry["unit"], STATUS_VALID))
        self._acc.clear()
        return out

    def has_samples(self) -> bool:
        return any(e["values"] for e in self._acc.values())
