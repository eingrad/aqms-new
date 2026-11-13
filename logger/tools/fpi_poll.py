"""
fpi_poll.py - Poll FPI AQMS analyzers over TCP/IP (FPI protocol, port 10000).

Standalone low-level check tool (raw hex replies, --scan). The logger itself reads the analyzers
with aqms_logger/fpi_protocol.py + aqms_logger/adapters/fpi_tcp.py, not with this file.
Stop the logger before using it: an analyzer may accept only one TCP client at a time.

Frame layout (manual ch.6 + Appendix B):
  7D 7B | ADDR(4) | CMD | EXT | LEN(2) | DATA(LEN) | CRC(2) | 7D 7D
  ADDR : send 01 10 01 F2, reply 01 F2 01 10
  EXT  : 0x55 read request, 0xAA read reply, 0x66 write, 0x99 write reply
  LEN  : big-endian
  CRC  : CRC-16/MODBUS over ADDR..DATA, low byte first (derived from the manual's example frames)
  Floats: IEEE-754 big-endian

Concentration command per model (MODELS below):
  AQMS-400 CO  : 0x2C, 33 bytes  - CO manual Appendix B, verified on site
  AQMS-300 O3  : 0x28, 5 bytes   - conc + unit (O3 manual Appendix B). --once also shows 0x2C
                                   (12 bytes, not documented for O3) for comparison
  AQMS-500 SO2 : 0x57, 25 bytes  - SO2 manual Appendix B p.67, verified on site
  AQMS-600 NOx : 0x3F, 41 bytes  - NOx manual Appendix B p.109; the site firmware sends 45
                                   (4 extra bytes ff ff ff ff), NO + NO2 = NOx checks out
The parsers accept replies longer than the manual says and keep the extra bytes as "extra" (hex).

Run:
  python fpi_poll.py --once                 one round on the ANALYZERS list: raw replies + firmware, then exit
  python fpi_poll.py --scan 192.168.13.1    try every documented read command on one analyzer, show replies
  python fpi_poll.py                        poll the ANALYZERS list continuously
  python fpi_poll.py --selftest             check CRC + parser against the manual's frames
Only uses the Python standard library.
"""
import argparse
import json
import math
import socket
import struct
import time

# ---- CONFIG -----------------------------------------------------------------
ANALYZERS = [
    {"name": "O3",  "model": "AQMS-300", "ip": "192.168.13.40", "port": 10000},
    {"name": "CO",  "model": "AQMS-400", "ip": "192.168.13.70", "port": 10000},
    {"name": "SO2", "model": "AQMS-500", "ip": "192.168.13.10", "port": 10000},
    {"name": "NOX", "model": "AQMS-600", "ip": "192.168.13.1",  "port": 10000},
]
POLL_SEC = 5       # analyzer updates every 1 s; build 1-min averages downstream
TIMEOUT_SEC = 3

# ---- PROTOCOL CONSTANTS -----------------------------------------------------
HEAD, TAIL = b"\x7D\x7B", b"\x7D\x7D"
ADDR_TX = b"\x01\x10\x01\xF2"
CMD_STATUS, CMD_VERSION, CMD_UNIT = 0x27, 0x25, 0x46

UNITS = {1: "ppb", 2: "ppm", 3: "ug/m3", 4: "mg/m3"}
STATUS = {
    0x00: "Measurement", 0x01: "Manual zero cal", 0x02: "Manual span cal",
    0x03: "Auto zero cal", 0x04: "Auto span cal", 0x05: "Flow cal",
    0x06: "Pressure cal", 0x07: "Diagnostic", 0x08: "Failure",
    0x09: "4-20mA output cal", 0x0A: "4-20mA input cal", 0x0B: "Multi-point / dark current cal",
    0x0C: "Mo converter cal",
}


def _r(v: float):
    return round(v, 4) if math.isfinite(v) else None     # NaN/inf (e.g. ff ff ff ff) -> null in JSON


def raw(d: bytes) -> dict:
    return {"raw_len": len(d), "raw": d.hex(" ")}


def _extra(rec: dict, d: bytes, n: int) -> dict:
    """Newer firmware may append fields the manual doesn't list: keep them as hex."""
    if len(d) > n:
        rec["extra"] = d[n:].hex(" ")
    return rec


def parse_co33(d: bytes) -> dict:
    """AQMS-400 0x2C (Appendix B): conc, unit, then 7 floats."""
    if len(d) < 33:
        return raw(d)
    conc, unit = struct.unpack_from(">fB", d, 0)
    flow, pres, bench, gfc, dev, rng, sensor = struct.unpack_from(">7f", d, 5)
    return _extra({"conc": _r(conc), "unit": UNITS.get(unit, unit), "flow": _r(flow), "pressure": _r(pres),
                   "bench_temp": _r(bench), "gfc_temp": _r(gfc), "conc_dev": _r(dev), "range": _r(rng),
                   "sensor_temp": _r(sensor)}, d, 33)


def parse_o3_12(d: bytes) -> dict:
    """AQMS-300 0x2C as seen on site (the O3 manual shows the CO table here): 3 floats.
    Outer ones look like the measuring/reference photometer voltages. The middle one read ~15% below
    0x28 on site - about the size of the manual's temperature/pressure correction (O3 manual 2.3.1),
    so it is probably the value before that correction. Shown in --once for comparison only."""
    if len(d) != 12:
        return raw(d)
    meas_v, conc, ref_v = struct.unpack(">3f", d)
    return {"meas_v": _r(meas_v), "conc_2c": _r(conc), "ref_v": _r(ref_v)}


def parse_so2_25(d: bytes) -> dict:
    """AQMS-500 0x57 (SO2 manual Appendix B): conc, unit, flow, pressure, gas temp, deviation, range."""
    if len(d) < 25:
        return raw(d)
    conc, unit = struct.unpack_from(">fB", d, 0)
    flow, pres, gas_t, dev, rng = struct.unpack_from(">5f", d, 5)
    return _extra({"conc": _r(conc), "unit": UNITS.get(unit, unit), "flow": _r(flow), "pressure": _r(pres),
                   "gas_temp": _r(gas_t), "conc_dev": _r(dev), "range": _r(rng)}, d, 25)


def parse_nox_41(d: bytes) -> dict:
    """AQMS-600 0x3F (NOx manual Appendix B p.109): NO, NO2, NOx, unit, then 7 floats.
    The site firmware (AQMS-600.2520A.U0012) sends 45 bytes: these 41 + ff ff ff ff."""
    if len(d) < 41:
        return raw(d)
    no, no2, nox = struct.unpack_from(">3f", d, 0)
    unit = d[12]
    flow, samp_p, react_p, cell_t, no_dev, no_rng, nox_rng = struct.unpack_from(">7f", d, 13)
    return _extra({"no": _r(no), "no2": _r(no2), "nox": _r(nox), "unit": UNITS.get(unit, unit),
                   "flow": _r(flow),                        # manual calls it "span gas flow"
                   "sample_pressure": _r(samp_p), "reaction_pressure": _r(react_p), "cell_temp": _r(cell_t),
                   "no_dev": _r(no_dev), "no_range": _r(no_rng), "nox_range": _r(nox_rng)}, d, 41)


def parse_conc5(d: bytes) -> dict:
    """0x28 in the O3/CO manuals: 4 bytes concentration + 1 byte unit."""
    if len(d) < 5:
        return raw(d)
    conc, unit = struct.unpack_from(">fB", d, 0)
    return _extra({"conc": _r(conc), "unit": UNITS.get(unit, unit)}, d, 5)


# "check": extra commands that --once also reads, for comparison
MODELS = {
    "AQMS-400": {"cmd": 0x2C, "parse": parse_co33},
    "AQMS-300": {"cmd": 0x28, "parse": parse_conc5, "check": {0x2C: parse_o3_12}},
    "AQMS-500": {"cmd": 0x57, "parse": parse_so2_25},
    "AQMS-600": {"cmd": 0x3F, "parse": parse_nox_41},
}
DEFAULT_MODEL = {"cmd": 0x2C, "parse": parse_co33}  # targets given on the command line

# documented read commands that need no request data, plus 0x50-0x5F (0x57 = SO2 concentration)
SCAN_CODES = [0x20, 0x21, 0x25, 0x27, 0x28, 0x29, 0x2A, 0x2B, 0x2C, 0x2D, 0x2F, 0x31, 0x33,
              0x36, 0x37, 0x3E, 0x3F, 0x40, 0x41, 0x42, 0x46, 0x4F] + list(range(0x50, 0x60))


# ---- FRAME HANDLING ---------------------------------------------------------
def crc16_modbus(data: bytes) -> int:
    crc = 0xFFFF
    for b in data:
        crc ^= b
        for _ in range(8):
            crc = (crc >> 1) ^ 0xA001 if crc & 1 else crc >> 1
    return crc


def build_frame(cmd: int, ext: int = 0x55, data: bytes = b"") -> bytes:
    body = ADDR_TX + bytes([cmd, ext]) + struct.pack(">H", len(data)) + data
    return HEAD + body + struct.pack("<H", crc16_modbus(body)) + TAIL


def read_reply(sock: socket.socket, cmd: int, timeout: float, log=None) -> bytes:
    """Return the first complete, CRC-valid read reply to `cmd` (whole frame).
    TCP may split or merge packets; anything else on the line (junk, stale replies,
    data the analyzer sends on its own) is skipped - and shown if `log` is given."""
    buf = bytearray()
    deadline = time.monotonic() + timeout
    while True:
        while True:
            i = buf.find(HEAD)
            if i < 0:
                if log and len(buf) > 1:
                    log(f"skipped {len(buf) - 1} bytes: {bytes(buf[:-1]).hex(' ')}")
                del buf[:-1]            # keep the last byte: it may start a header
                break
            if i > 0:
                if log:
                    log(f"skipped {i} bytes: {bytes(buf[:i]).hex(' ')}")
                del buf[:i]
            if len(buf) < 10:
                break
            n = struct.unpack(">H", buf[8:10])[0]
            if n > 512:                 # not a real header: resync one byte later
                del buf[:1]
                continue
            if len(buf) < n + 14:
                break
            frame = bytes(buf[:n + 14])
            if frame[-2:] != TAIL or struct.unpack("<H", frame[-4:-2])[0] != crc16_modbus(frame[2:-4]):
                del buf[:1]             # corrupt or misaligned: resync one byte later
                continue
            del buf[:n + 14]
            if frame[6] == cmd and frame[7] == 0xAA:
                return frame
            if log:
                log(f"skipped frame: {frame.hex(' ')}")
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(f"no reply to 0x{cmd:02X}"
                               + (f" (received: {bytes(buf).hex(' ')})" if buf else ""))
        sock.settimeout(remaining)
        try:
            chunk = sock.recv(4096)
        except socket.timeout:
            continue                    # loop back: deadline check raises with details
        if not chunk:
            raise ConnectionError("analyzer closed the connection")
        buf += chunk


def request(sock: socket.socket, cmd: int, log=None, timeout: float = TIMEOUT_SEC) -> bytes:
    """Send a read request; return the reply frame."""
    sock.sendall(build_frame(cmd))
    return read_reply(sock, cmd, timeout, log)


def data_of(frame: bytes) -> bytes:
    return frame[10:-4]


def versions(d: bytes) -> str:
    """0x25 reply: one to three 32-byte strings (mainboard, bench, CPLD)."""
    parts = [d[i:i + 32].split(b"\x00")[0].decode("ascii", "replace").strip() for i in range(0, len(d), 32)]
    return " | ".join(p for p in parts if p) or d.hex(" ")


# ---- POLLING ----------------------------------------------------------------
def read_conc(sock: socket.socket, m: dict, log=None) -> tuple:
    """Run the model's concentration command; returns (raw frame, parsed dict)."""
    fr = request(sock, m["cmd"], log)
    rec = m["parse"](data_of(fr))
    if "unit_cmd" in m and "conc" in rec:
        u = data_of(request(sock, m["unit_cmd"], log))
        rec["unit"] = UNITS.get(u[0], u[0]) if u else None
    return fr, rec


def poll(sock: socket.socket, a: dict) -> dict:
    m = MODELS.get(a.get("model"), DEFAULT_MODEL)
    _, conc = read_conc(sock, m)
    st_data = data_of(request(sock, CMD_STATUS))
    if len(st_data) != 1:
        raise ValueError(f"unexpected status length {len(st_data)}")
    st = st_data[0]
    return {
        "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "analyzer": a["name"],
        "status": st,
        "status_text": STATUS.get(st, "Unknown"),
        "valid": st == 0x00 and ("conc" in conc or "nox" in conc),   # only Measurement mode goes into averages
        **conc,
    }


def output(rec: dict) -> None:
    # Replace with your push: MQTT publish, HTTP POST, or DB insert
    print(json.dumps(rec), flush=True)


def run(analyzers: list, interval: float) -> None:
    active = []
    for a in analyzers:
        if MODELS.get(a.get("model"), DEFAULT_MODEL)["cmd"] is None:
            print(f"# {a['name']}: concentration command not mapped yet - skipped "
                  f"(run: python fpi_poll.py --scan {a['ip']})", flush=True)
        else:
            active.append(a)
    socks = {}
    while True:
        t0 = time.time()
        for a in active:
            name = a["name"]
            try:
                if name not in socks:
                    socks[name] = socket.create_connection((a["ip"], a["port"]), timeout=TIMEOUT_SEC)
                rec = poll(socks[name], a)
            except (OSError, ValueError) as e:
                # drop the socket so leftover bytes can't desync the next read
                if name in socks:
                    socks.pop(name).close()
                rec = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), "analyzer": name,
                       "valid": False, "error": str(e) or type(e).__name__}
            output(rec)
        time.sleep(max(0.0, interval - (time.time() - t0)))


# ---- DIAGNOSTICS ------------------------------------------------------------
def once(analyzers: list) -> None:
    """One diagnostic round per analyzer: raw replies, parsed values, status, firmware."""
    def log(msg):
        print(f"  !  {msg}")

    for a in analyzers:
        m = MODELS.get(a.get("model"), DEFAULT_MODEL)
        print(f"== {a['name']} {a.get('model', '')}  {a['ip']}:{a['port']}")
        try:
            sock = socket.create_connection((a["ip"], a["port"]), timeout=TIMEOUT_SEC)
        except OSError as e:
            print(f"  connect failed: {e}")
            continue
        with sock:
            if m["cmd"] is None:
                print(f"  conc     : command not mapped yet - run: python fpi_poll.py --scan {a['ip']}")
            else:
                c = f"0x{m['cmd']:02X}"
                try:
                    fr, rec = read_conc(sock, m, log)
                    print(f"  {c} raw : {fr.hex(' ')}")
                    print(f"  {c}     : {json.dumps(rec)}")
                except (OSError, ValueError) as e:
                    print(f"  {c}     : FAILED - {e}")
            for code, parse in m.get("check", {}).items():
                try:
                    d = data_of(request(sock, code, log, timeout=1.5))
                    print(f"  0x{code:02X}     : {json.dumps(parse(d))}   (comparison only)")
                except (OSError, ValueError) as e:
                    print(f"  0x{code:02X}     : no answer - {e}   (comparison only)")
            try:
                st = data_of(request(sock, CMD_STATUS, log))
                print("  0x27     : " + (f"{st[0]} = {STATUS.get(st[0], 'Unknown')}" if st else "empty"))
            except (OSError, ValueError) as e:
                print(f"  0x27     : FAILED - {e}")
            try:
                print(f"  0x25     : {versions(data_of(request(sock, CMD_VERSION, log)))}")
            except (OSError, ValueError) as e:
                print(f"  0x25     : FAILED - {e}")


def scan(target: dict) -> None:
    """Try read commands one by one and show every reply - to map an undocumented model."""
    print(f"== scan {target['ip']}:{target['port']}")
    with socket.create_connection((target["ip"], target["port"]), timeout=TIMEOUT_SEC) as sock:
        sock.settimeout(2)              # first, listen: does it send anything by itself?
        try:
            unsolicited = sock.recv(4096)
            print(f"  sent by itself within 2 s: {unsolicited.hex(' ') or '(connection closed)'}")
        except socket.timeout:
            print("  sent nothing by itself within 2 s")
        for code in SCAN_CODES:
            try:
                d = data_of(request(sock, code, timeout=0.8))
                text = d.split(b"\x00")[0]
                asc = f'  "{text.decode()}"' if len(text) >= 4 and all(32 <= b < 127 for b in text) else ""
                print(f"  0x{code:02X}  len {len(d):3}  {d.hex(' ')}{asc}")
            except TimeoutError:
                print(f"  0x{code:02X}  -")


def selftest() -> None:
    sends = {0x2B: "7D7B011001F22B550000B1F67D7D", 0x27: "7D7B011001F227550000B2A67D7D",
             0x2C: "7D7B011001F22C550000B0827D7D", 0x20: "7D7B011001F220550000B3D27D7D"}
    for cmd, frame in sends.items():
        assert build_frame(cmd) == bytes.fromhex(frame), f"send 0x{cmd:02X} mismatch"
    hb_reply = bytes.fromhex(
        "7D7B01F201102CAA0021"
        "00000000" "02" "00000000" "00000000" "3DCCCCC6" "42C80000" "00000000" "42480000" "00000000"
        "14787D7D")
    st_reply = bytes.fromhex("7D7B01F2011027AA0001008FE27D7D")
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"\x00\x13" + st_reply + hb_reply[:7])   # junk + an unrelated reply + half a frame
        b.sendall(hb_reply[7:])
        hb = parse_co33(data_of(read_reply(a, 0x2C, 2)))
        b.sendall(st_reply)
        st = data_of(read_reply(a, 0x27, 2))
    print("send frames OK (CRC matches manual)")
    print("0x2C parsed:", json.dumps(hb))
    print("0x27 status:", st[0], STATUS[st[0]])


def parse_target(s: str) -> dict:
    """'192.168.13.10', 'O3=192.168.13.10' or 'O3=192.168.13.10:10000'. Known IPs keep their model."""
    name, _, addr = s.rpartition("=")
    host, _, port = addr.partition(":")
    known = next((a for a in ANALYZERS if a["ip"] == host), {})
    return {"name": name or known.get("name", host), "model": known.get("model"),
            "ip": host, "port": int(port) if port else 10000}


def main() -> None:
    ap = argparse.ArgumentParser(description="Poll FPI AQMS analyzers over TCP (FPI protocol, port 10000).")
    ap.add_argument("targets", nargs="*", help="analyzers: IP, NAME=IP or NAME=IP:PORT (default: ANALYZERS list)")
    ap.add_argument("--once", action="store_true", help="one diagnostic round with raw replies, then exit")
    ap.add_argument("--scan", metavar="IP", help="try every read command on one analyzer and show the replies")
    ap.add_argument("--interval", type=float, default=POLL_SEC, help=f"seconds between polls (default {POLL_SEC})")
    ap.add_argument("--selftest", action="store_true", help="check CRC and parser against the manual's frames")
    args = ap.parse_args()

    if args.selftest:
        selftest()
    elif args.scan:
        scan(parse_target(args.scan))
    else:
        analyzers = [parse_target(t) for t in args.targets] or ANALYZERS
        once(analyzers) if args.once else run(analyzers, args.interval)


if __name__ == "__main__":
    main()
