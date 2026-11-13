"""FPI analyzer protocol (AQMS-300/400/500/600) - the analyzers' own binary protocol on TCP port 10000.

Frame (FPI manuals ch.6 + Appendix B):

    7D 7B | ADDR(4) | CMD | EXT | LEN(2, big-endian) | DATA(LEN) | CRC(2) | 7D 7D

    ADDR  request 01 10 01 F2, reply 01 F2 01 10
    EXT   0x55 read request, 0xAA read reply
    CRC   CRC-16/MODBUS over ADDR..DATA, low byte first. Derived from the manuals' example
          frames; matches real replies from all four TNBJ-AQMS1 analyzers.

Floats are IEEE-754 big-endian. The analyzer only answers requests (pull); it never sends by
itself. Per the manuals (section 4.3.1.2) it recalculates its concentration every second, as a
moving average set on the analyzer.

Read commands as verified on site (2026-10-16):

    AQMS-300 O3   0x28  5 bytes   conc, unit
                  0x2C  12 bytes  photometer measuring V, conc before T/P correction, reference V
    AQMS-400 CO   0x2C  33 bytes  conc, unit, flow, pressure, bench T, GFC wheel T, deviation,
                                  range, sensor T
    AQMS-500 SO2  0x57  25 bytes  conc, unit, flow, pressure, gas T, deviation, range
    AQMS-600 NOx  0x3F  45 bytes  NO, NO2, NOx, unit, flow, sampling P, reaction P, gas cell T,
                                  NO deviation, NO range, NO2/NOx range + 4 extra bytes
                                  (manual says 41; firmware AQMS-600.2520A.U0012 sends 45)
    all           0x27  1 byte    status code (STATUS_TEXT)

Decoders accept replies longer than documented (newer firmware appends fields) and raise
ValueError for shorter ones. Standard library only.
"""
from __future__ import annotations

import math
import socket
import struct
import time

HEAD = b"\x7D\x7B"
TAIL = b"\x7D\x7D"
ADDR_REQUEST = b"\x01\x10\x01\xF2"
EXT_READ = 0x55
EXT_READ_REPLY = 0xAA
MAX_DATA_LEN = 512          # longer LEN fields are treated as a false header (resync)

CMD_STATUS = 0x27
CMD_VERSION = 0x25

UNITS = {1: "ppb", 2: "ppm", 3: "ug/m3", 4: "mg/m3"}

STATUS_TEXT = {
    0x00: "measurement", 0x01: "manual zero cal", 0x02: "manual span cal",
    0x03: "auto zero cal", 0x04: "auto span cal", 0x05: "flow cal", 0x06: "pressure cal",
    0x07: "diagnostic mode", 0x08: "failure mode", 0x09: "4-20 mA output cal",
    0x0A: "4-20 mA input cal", 0x0B: "multi-point / dark current cal", 0x0C: "Mo converter cal",
}


# ---- frames -----------------------------------------------------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_frame(cmd: int, ext: int = EXT_READ, data: bytes = b"") -> bytes:
    body = ADDR_REQUEST + bytes([cmd, ext]) + struct.pack(">H", len(data)) + data
    return HEAD + body + struct.pack("<H", crc16_modbus(body)) + TAIL


def read_reply(sock: socket.socket, cmd: int, timeout: float) -> bytes:
    """Return the DATA of the first valid read reply to `cmd`.

    Resynchronises on 7D 7B, validates length + CRC + tail, and skips noise, stale replies to
    other commands, and anything else arriving on the connection.
    """
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        while True:
            i = buf.find(HEAD)
            if i < 0:
                del buf[:-1]                    # keep the last byte: it may start a header
                break
            del buf[:i]
            if len(buf) < 10:
                break                           # wait for the rest of the header
            n = struct.unpack(">H", buf[8:10])[0]
            if n > MAX_DATA_LEN:
                del buf[:1]                     # not a real header
                continue
            if len(buf) < n + 14:
                break                           # wait for the rest of the frame
            frame = bytes(buf[:n + 14])
            if frame[-2:] != TAIL or struct.unpack("<H", frame[-4:-2])[0] != crc16_modbus(frame[2:-4]):
                del buf[:1]                     # corrupt or misaligned: resync one byte later
                continue
            del buf[:n + 14]
            if frame[6] == cmd and frame[7] == EXT_READ_REPLY:
                return frame[10:10 + n]
            # a stale reply to an earlier command: skip it
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no reply to 0x{cmd:02X}")
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue
        if not chunk:
            raise ConnectionError("analyzer closed the connection")
        buf += chunk


def request(sock: socket.socket, cmd: int, timeout: float) -> bytes:
    """Send one read request and return the reply DATA."""
    sock.sendall(build_frame(cmd))
    return read_reply(sock, cmd, timeout)


# ---- decoders -----------------------------------------------------------------
def _finite(v: float) -> float | None:
    return v if math.isfinite(v) else None      # ff ff ff ff etc. -> None


def _need(d: bytes, n: int, what: str) -> None:
    if len(d) < n:
        raise ValueError(f"{what}: expected {n} bytes, got {len(d)}")


def decode_conc5(d: bytes) -> dict:
    """0x28 (AQMS-300 O3): concentration + unit."""
    _need(d, 5, "0x28")
    conc, unit = struct.unpack_from(">fB", d, 0)
    return {"conc": _finite(conc), "unit": unit}


def decode_o3_2c(d: bytes) -> dict:
    """0x2C on the AQMS-300: measuring voltage, conc before T/P correction, reference voltage."""
    _need(d, 12, "O3 0x2C")
    meas_v, conc_2c, ref_v = struct.unpack_from(">3f", d, 0)
    return {"meas_v": _finite(meas_v), "conc_2c": _finite(conc_2c), "ref_v": _finite(ref_v)}


def decode_co_2c(d: bytes) -> dict:
    """0x2C (AQMS-400 CO)."""
    _need(d, 33, "CO 0x2C")
    conc, unit = struct.unpack_from(">fB", d, 0)
    flow, pres, bench, gfc, dev, rng, sensor = struct.unpack_from(">7f", d, 5)
    return {"conc": _finite(conc), "unit": unit, "flow": _finite(flow), "pressure": _finite(pres),
            "bench_temp": _finite(bench), "gfc_temp": _finite(gfc), "conc_dev": _finite(dev),
            "range": _finite(rng), "sensor_temp": _finite(sensor)}


def decode_so2_57(d: bytes) -> dict:
    """0x57 (AQMS-500 SO2)."""
    _need(d, 25, "SO2 0x57")
    conc, unit = struct.unpack_from(">fB", d, 0)
    flow, pres, gas_t, dev, rng = struct.unpack_from(">5f", d, 5)
    return {"conc": _finite(conc), "unit": unit, "flow": _finite(flow), "pressure": _finite(pres),
            "gas_temp": _finite(gas_t), "conc_dev": _finite(dev), "range": _finite(rng)}


def decode_nox_3f(d: bytes) -> dict:
    """0x3F (AQMS-600 NOx). The manual calls the flow "span gas flow"."""
    _need(d, 41, "NOx 0x3F")
    no, no2, nox = struct.unpack_from(">3f", d, 0)
    unit = d[12]
    flow, samp_p, react_p, cell_t, no_dev, no_rng, nox_rng = struct.unpack_from(">7f", d, 13)
    return {"no": _finite(no), "no2": _finite(no2), "nox": _finite(nox), "unit": unit,
            "flow": _finite(flow), "sample_pressure": _finite(samp_p),
            "reaction_pressure": _finite(react_p), "cell_temp": _finite(cell_t),
            "no_dev": _finite(no_dev), "no_range": _finite(no_rng), "nox_range": _finite(nox_rng)}
