"""Synspec gas chromatographs (GCAlpha, GC955, GCDelta, ...) - Modbus register map and decoders.

Source: Syntech/Synspec tech note E066 "Implementation of MODBUS/J-BUS protocol in GCs"
(2025-10-02). Addresses are 0-based protocol addresses (Modscan shows them +40001). The GC
answers function 3 and 4 identically; the logger only uses function 3 and never writes.
Values wider than one register are sent most significant register first, so floats are
IEEE-754 in word order ABCD.

Only the registers the logger and gc_probe use are listed; all of them exist from GCAlpha
V3.4.7.12 / GC955 V6.4.7.12 on (TNBJ-AQMS1: Alpha 115 methane / TNMHC, GCAlpha V3.4.8.1).
Standard library only - no network code here (see adapters/synspec_gc.py and gc_probe.py).
"""
from __future__ import annotations

from datetime import datetime

# ---- register map -----------------------------------------------------------------------
MANUFACTURER = 0         # 10 words text, always "Synspec BV."
CLOCK = 30               # 7 words text: GC date DDMMYYYY (4 words) + time HHMMSS (3 words)
STATUS = 181             # 181-184 internal status, 185-186 system errors (bit words) ...
OUTPUT_CHANNEL = 187     # ... and the stream-selector channel of the data on the outputs
STATUS_WORDS = 7         # 181..187 in one read
SOFTWARE_VERSION = 920   # 4 words: major, minor, release, build
SERIAL_NUMBER = 924      # uint32, high word first
VALUES = 1526            # float32 per component: component n at VALUES + 2(n - 1)
CYCLE_TIME = 1609        # int16 seconds since the run started, int16 seconds to go (-1 = no run)
INLET_PRESSURE = 1611    # float32, bar
COLUMN_FLOW = 1613       # float32, ccm
NAMES = 1638             # 10 words (20 ANSI characters) per component; all spaces = not in use
NAME_WORDS = 10
UNITS = 2038             # 1 word per component, see UNIT_CODES
RETENTION_TIMES = 2078   # 1 word per component, seconds (0 = substance not detected)
MACHINE_TYPE = 2118      # see MACHINE_TYPES
RUN_TIME = 2135          # 7 words: date DDMMYYYY (4) + time HHMMSS (3) of the run whose data is
                         # on the outputs. E066 calls the format "BCD" without details, so the
                         # digits are decoded flexibly (decode_datetime).
MAX_COMPONENTS = 40

# ---- codes --------------------------------------------------------------------------------
UNIT_CODES = {1: "ug/m3", 2: "ppb", 3: "mg/m3", 4: "ppm", 5: "vol%", 6: "g/m3"}  # 0 = not calibrated

MACHINE_TYPES = {
    1: "GC955", 2: "GC955 (Demo)", 3: "GCAlpha", 4: "GCAlpha (Demo)", 5: "GCAlpha SCA",
    6: "GCAlpha SCA (Demo)", 7: "GCDelta", 8: "GCDelta (Demo)", 9: "GCDelta SCA",
    10: "GCDelta SCA (Demo)", 11: "Coditheen", 12: "Coditheen (Demo)", 13: "GCDelta CEMS",
    14: "GCDelta CEMS (Demo)", 15: "SAM GC-500", 16: "SAM GC-500 (Demo)", 17: "GC3000-315",
    18: "GC3000-315 (Demo)", 19: "GC3000-110", 20: "GC3000-110 (Demo)", 21: "GCAlpha 401",
    22: "GCAlpha 401 (Demo)", 23: "GC1055", 24: "GC1055 (Demo)", 25: "VOC-100",
    26: "VOC-100 (Demo)", 27: "VOC-100-A", 28: "VOC-100-A (Demo)", 29: "GC Ctrl",
    30: "GC Ctrl (Demo)",
}

STATUS_BITS = {
    181: {8: "waiting for oven temperature", 9: "zeroing detector(s)",
          10: "waiting for clearance to start run", 11: "internal pump active",
          12: "taking sample", 13: "run synchronization", 14: "output data invalid",
          15: "external pump active"},
    183: {0: "new run start", 1: "system alarm", 2: "component or system alarm",
          3: "calibration mode", 5: "normal run mode", 6: "maintenance active",
          7: "GC software is starting the machine", 8: "start button down",
          9: "stop button down", 10: "abort button down",
          11: "LED error (red): unrecoverable error", 12: "LED running (amber)",
          13: "LED idle (green)", 14: "abort run - cooling down oven",
          15: "waiting for realtime synchronisation / zeroing"},
    184: {0: "combination 2", 1: "combination 3", 2: "combination 4",
          3: "low component alarm", 4: "high component alarm", 5: "new data available",
          6: "injection mode active", 7: "machine available for normal measurements",
          8: "span gas valve open", 9: "zero gas valve open", 10: "sample pump disabled",
          11: "span gas data at output", 12: "zero gas data at output",
          13: "auto validation active", 14: "waiting for remote start", 15: "combination 1"},
    185: {0: "hard disk full", 1: "FID flame error", 2: "any system alarm",
          3: "PID lamp error", 4: "hard disk < 30 MB free", 5: "stream selector error",
          6: "sample error", 7: "TSTRIP board error", 8: "MX board error",
          9: "internal temperature > 40 C", 10: "actuator pressure too high",
          11: "oven temperature error", 12: "preconcentration temperature error",
          13: "actuator pressure < 2 bar", 14: "flow too high", 15: "flow too low"},
    186: {8: "STEP board error", 9: "TCD board(s) error"},
}

# ---- GC_STATUS: why the component values are blank (higher wins) ---------------------------
OK, STALE, OTHER_CHANNEL, MAINTENANCE, CALIBRATION, INVALID = range(6)
STATE_TEXT = {
    OK: "ok - ambient data",
    STALE: "stale - no new run for max_age_minutes",
    OTHER_CHANNEL: "data from another stream-selector channel",
    MAINTENANCE: "maintenance",
    CALIBRATION: "calibration / validation / span or zero gas data on the outputs",
    INVALID: "output invalid - GC says so, starting up, FID flame error or first run after a restart",
}

# status bits that make the values on the outputs unusable: {address: mask}
INVALID_BITS = {181: 1 << 14, 183: 1 << 7, 185: 1 << 1}   # output invalid, starting up, FID flame
CALIBRATION_BITS = {183: 1 << 3, 184: (1 << 11) | (1 << 12) | (1 << 13)}  # cal, span/zero out, validation
MAINTENANCE_BITS = {183: 1 << 6}


def _hit(words: dict[int, int], bits: dict[int, int]) -> bool:
    return any(words[address] & mask for address, mask in bits.items())


def state_code(status: list[int], channel: int = 1, stale: bool = False) -> int:
    """GC_STATUS for one sample from the status block (STATUS .. OUTPUT_CHANNEL, 7 words).

    `channel` is the stream-selector channel carrying ambient air (1 when there is no selector).
    Output channel 0 means the data comes from the first run after a GC restart, whose sample
    is of unknown origin.
    """
    words = dict(zip(range(STATUS, STATUS + STATUS_WORDS), status))
    out_channel = words[OUTPUT_CHANNEL]
    if out_channel == 0 or _hit(words, INVALID_BITS):
        return INVALID
    if _hit(words, CALIBRATION_BITS):
        return CALIBRATION
    if _hit(words, MAINTENANCE_BITS):
        return MAINTENANCE
    if out_channel != channel:
        return OTHER_CHANNEL
    return STALE if stale else OK


def blanks_values(address: int, bit: int) -> bool:
    """True if this status bit blanks the component values."""
    return any(bits.get(address, 0) & (1 << bit)
               for bits in (INVALID_BITS, CALIBRATION_BITS, MAINTENANCE_BITS))


def bit_names(address: int, word: int) -> list[str]:
    names = STATUS_BITS.get(address, {})
    return [names.get(bit, f"bit {bit}") for bit in range(16) if word >> bit & 1]


# ---- decoding --------------------------------------------------------------------------------
def decode_text(words: list[int]) -> str:
    """Text fields: two ANSI characters per register, the first one in the high byte."""
    raw = b"".join(bytes(((w >> 8) & 0xFF, w & 0xFF)) for w in words)
    return raw.decode("cp1252", errors="replace").replace("\x00", " ").strip()


DATETIME_FORMATS = ("text", "text_swapped", "bcd", "binary")


def _two_digits(word: int, fmt: str) -> str | None:
    hi, lo = (word >> 8) & 0xFF, word & 0xFF
    if fmt in ("text", "text_swapped"):
        pair = (hi, lo) if fmt == "text" else (lo, hi)
        return bytes(pair).decode() if all(0x30 <= b <= 0x39 for b in pair) else None
    if fmt == "bcd":                                   # one BCD byte, in either half
        b = lo if hi == 0 else (hi if lo == 0 else -1)
        return f"{b >> 4}{b & 0xF}" if b >= 0 and b >> 4 <= 9 and b & 0xF <= 9 else None
    return f"{word:02d}" if word <= 99 else None       # binary 0..99


def decode_datetime(words: list[int]) -> tuple[datetime, str] | None:
    """7 registers DD MM YY YY HH MM SS (two digits each) -> (naive GC-local datetime, format).

    E066 says "Alpha" for the clock and "BCD" for the run time without spelling out the layout,
    so each format is tried in turn; the first giving a real date in 2000-2099 wins. The two
    year registers make the choice unambiguous (2026 as BCD reads as year 3238 in binary, as
    binary it is not valid BCD, and neither is valid text). None if no format fits (for
    example all zeros before the first run).
    """
    if len(words) < 7:
        return None
    for fmt in DATETIME_FORMATS:
        digits = [_two_digits(w, fmt) for w in words[:7]]
        if None in digits:
            continue
        s = "".join(digits)
        try:
            dt = datetime(int(s[4:8]), int(s[2:4]), int(s[0:2]),
                          int(s[8:10]), int(s[10:12]), int(s[12:14]))
        except ValueError:
            continue
        if 2000 <= dt.year <= 2099:
            return dt, fmt
    return None


def data_age_seconds(clock: list[int], run_time: list[int]) -> float | None:
    """Seconds from the run whose data is on the outputs to the GC's own clock. Both come from
    the GC, so its time zone and clock drift don't matter. None if either can't be decoded."""
    now, run = decode_datetime(clock), decode_datetime(run_time)
    if now is None or run is None:
        return None
    return (now[0] - run[0]).total_seconds()


# ---- units -----------------------------------------------------------------------------------
# exact conversions only: volume ratios among themselves, mass concentrations among themselves
_VOLUME = {"ppb": 1.0, "ppm": 1e3, "vol%": 1e7}
_MASS = {"ug/m3": 1.0, "mg/m3": 1e3, "g/m3": 1e6}
STORE_UNITS = frozenset(_VOLUME) | frozenset(_MASS)


def unit_factor(gc_unit: str | None, unit: str) -> float | None:
    """Factor from the GC's unit to `unit`; None if not calibrated (gc_unit None) or if the
    conversion needs a molecular weight and temperature (ppm <-> mg/m3)."""
    if gc_unit is None:
        return None
    if gc_unit == unit:
        return 1.0
    for scale in (_VOLUME, _MASS):
        if gc_unit in scale and unit in scale:
            return scale[gc_unit] / scale[unit]
    return None
