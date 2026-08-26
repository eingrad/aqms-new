"""Common interface every source adapter implements."""
from __future__ import annotations

from datetime import datetime

from ..models import Reading


class SourceAdapter:
    """A source of Readings for one device.

    Implementations must be resilient: `read()` should never raise for an
    expected field/communication failure — it returns comm-fail Readings
    (value=None) instead, so gaps are explicit rather than missing.
    """

    name: str

    def read(self, now: datetime) -> list[Reading]:
        raise NotImplementedError

    def close(self) -> None:
        """Release sockets/handles. Safe to call multiple times."""
