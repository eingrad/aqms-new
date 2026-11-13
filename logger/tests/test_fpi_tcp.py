"""FPI analyzers over TCP (fpi_protocol + FpiTcpAdapter), using the real replies the TNBJ-AQMS1
analyzers sent on 2026-10-16. The fake analyzer answers over a socketpair (no network)."""
import logging
import os
import socket
import struct
import threading
import time
from datetime import datetime, timezone

import pytest

from aqms_logger import fpi_protocol as fpi
from aqms_logger.adapters.fpi_tcp import MODELS, FpiTcpAdapter, parameters, status_parameters
from aqms_logger.aggregator import MinuteAggregator
from aqms_logger.config import DeviceConfig, load_config
from aqms_logger.main import _configured_parameters
from aqms_logger.models import (
    STATUS_CALIBRATION, STATUS_COMM_FAIL, STATUS_OUT_OF_RANGE, STATUS_VALID,
)

NOW = datetime(2026, 10, 16, 5, 0, 5, tzinfo=timezone.utc)

# full frames exactly as received on site
SITE_FRAMES = {
    "O3 0x2C": "7d 7b 01 f2 01 10 2c aa 00 0c 40 57 74 85 42 19 a1 b7 40 57 8d 68 4f ff 7d 7d",
    "CO 0x2C": "7d 7b 01 f2 01 10 2c aa 00 21 3f 32 85 78 02 44 45 59 87 41 60 14 25 42 3f fc 01 42"
               " 88 01 07 3a c1 d2 92 42 48 00 00 c1 a0 4a ea 4f 02 7d 7d",
    "SO2 0x57": "7d 7b 01 f2 01 10 57 aa 00 19 40 9b 3b 1d 01 44 34 f3 de 41 4e 7d 31 42 48 0d f0 3c"
                " c6 ea 2b 43 fa 00 00 9a 3e 7d 7d",
    "NOX 0x3F": "7d 7b 01 f2 01 10 3f aa 00 2d 40 96 3a 36 40 5a ea 30 41 01 d7 a7 01 44 0c 25 de 41"
                " 53 4d e9 3f e6 75 4e 42 49 de 56 3e 8b 79 c3 43 fa 00 00 43 fa 00 00 ff ff ff ff"
                " 10 8e 7d 7d",
}
DATA = {k: bytes.fromhex(v)[10:-4] for k, v in SITE_FRAMES.items()}
O3_28 = struct.pack(">fB", 44.2031, 1)            # O3 0x28 reply (44.2031 ppb seen on site)
STATUS_OK = b"\x00"

TABLES = {
    "AQMS-300": {0x28: O3_28, 0x2C: DATA["O3 0x2C"], 0x27: STATUS_OK},
    "AQMS-400": {0x2C: DATA["CO 0x2C"], 0x27: STATUS_OK},
    "AQMS-500": {0x57: DATA["SO2 0x57"], 0x27: STATUS_OK},
    "AQMS-600": {0x3F: DATA["NOX 0x3F"], 0x27: STATUS_OK},
}


def reply(cmd: int, data: bytes) -> bytes:
    body = bytes([0x01, 0xF2, 0x01, 0x10, cmd, 0xAA]) + struct.pack(">H", len(data)) + data
    return fpi.HEAD + body + struct.pack("<H", fpi.crc16_modbus(body)) + fpi.TAIL


class FakeAnalyzer:
    """Answers FPI read requests from `table` (cmd -> data). Unknown commands get no reply,
    like the real analyzers. Each connect() is a new socketpair served by a thread."""

    def __init__(self, table, noise=b"", split=False, close_after=None, refuse=False):
        self.table = dict(table)
        self.noise, self.split, self.close_after, self.refuse = noise, split, close_after, refuse
        self.connects = 0
        self.requests = []

    def connect(self):
        self.connects += 1
        if self.refuse:
            raise ConnectionRefusedError("analyzer off")
        a, b = socket.socketpair()
        threading.Thread(target=self._serve, args=(b,), daemon=True).start()
        return a

    def _serve(self, conn):
        served, buf = 0, b""
        with conn:
            while True:
                try:
                    chunk = conn.recv(64)
                except OSError:
                    return
                if not chunk:
                    return
                buf += chunk
                while len(buf) >= 14:                    # read requests are 14-byte frames
                    req, buf = buf[:14], buf[14:]
                    self.requests.append(req[6])
                    data = self.table.get(req[6])
                    if data is None:
                        continue
                    out = self.noise + reply(req[6], data)
                    if self.split:                       # frame arrives in two TCP segments
                        conn.sendall(out[:9])
                        time.sleep(0.005)
                        conn.sendall(out[9:])
                    else:
                        conn.sendall(out)
                    served += 1
                    if self.close_after and served >= self.close_after:
                        return


def device(model, **options):
    return DeviceConfig(name=f"fpi_{model[-3:]}", type="fpi_tcp", host="127.0.0.1", port=10000,
                        timeout=0.5, options={"model": model, **options})


def adapter(model, fake, clock=None, **options):
    return FpiTcpAdapter(device(model, **options), "TEST", connect=fake.connect,
                         clock=clock or time.monotonic)


def by_param(readings):
    return {r.parameter: r for r in readings}


# ---- protocol -------------------------------------------------------------------
def test_request_frames_match_manual():
    for cmd, crc in {0x27: "B2A6", 0x2C: "B082", 0x2B: "B1F6", 0x20: "B3D2"}.items():
        assert fpi.build_frame(cmd) == bytes.fromhex(f"7D7B011001F2{cmd:02X}550000{crc}7D7D")


def test_crc_matches_real_site_replies():
    for name, hexframe in SITE_FRAMES.items():
        frame = bytes.fromhex(hexframe)
        assert struct.unpack("<H", frame[-4:-2])[0] == fpi.crc16_modbus(frame[2:-4]), name


def test_decoders_on_site_replies():
    co = fpi.decode_co_2c(DATA["CO 0x2C"])
    assert round(co["conc"], 4) == 0.6973 and fpi.UNITS[co["unit"]] == "ppm"
    assert round(co["flow"], 1) == 789.4 and round(co["gfc_temp"], 1) == 68.0
    so2 = fpi.decode_so2_57(DATA["SO2 0x57"])
    assert round(so2["conc"], 3) == 4.851 and fpi.UNITS[so2["unit"]] == "ppb"
    nox = fpi.decode_nox_3f(DATA["NOX 0x3F"])            # 45 bytes: 41 documented + ff ff ff ff
    assert abs(nox["no"] + nox["no2"] - nox["nox"]) < 1e-4
    assert round(nox["nox"], 3) == 8.115 and round(nox["cell_temp"], 1) == 50.5
    o3 = fpi.decode_o3_2c(DATA["O3 0x2C"])
    assert round(o3["meas_v"], 4) == 3.3665 and round(o3["ref_v"], 3) == 3.368
    assert round(fpi.decode_conc5(O3_28)["conc"], 4) == 44.2031


def test_short_reply_is_rejected():
    with pytest.raises(ValueError):
        fpi.decode_nox_3f(DATA["NOX 0x3F"][:40])


def test_read_reply_skips_noise_and_stale_replies():
    a, b = socket.socketpair()
    with a, b:
        b.sendall(b"\x00\x7d\x13" + reply(0x27, b"\x00") + reply(0x57, DATA["SO2 0x57"])[:8])
        b.sendall(reply(0x57, DATA["SO2 0x57"])[8:])
        assert fpi.read_reply(a, 0x57, 1.0) == DATA["SO2 0x57"]


# ---- adapter ------------------------------------------------------------------------
# The site replies carry O3 / SO2 / NOx in ppb (unit code 1) and CO in ppm (2): the same units
# the logger stores by default, so the values pass straight through. CO keeps 6 significant
# digits (0.69735, not 0.6973).
@pytest.mark.parametrize("model,unit,expect", [
    ("AQMS-300", "ppb", {"O3": 44.2031, "O3_MEAS_V": 3.3665, "O3_REF_V": 3.368}),
    ("AQMS-400", "ppm", {"CO": 0.69735, "CO_FLOW": 789.3989, "CO_PRESSURE": 14.0049,
                         "CO_BENCH_TEMP": 47.9961, "CO_GFC_TEMP": 68.002,
                         "CO_SENSOR_TEMP": -20.0366}),
    ("AQMS-500", "ppb", {"SO2": 4.851, "SO2_FLOW": 723.8104, "SO2_PRESSURE": 12.9056,
                         "SO2_GAS_TEMP": 50.0136}),
    ("AQMS-600", "ppb", {"NO": 4.6946, "NO2": 3.4205, "NOX": 8.1151, "NOX_FLOW": 560.5917,
                         "NOX_SAMPLE_PRESSURE": 13.2065, "NOX_REACTION_PRESSURE": 1.8005,
                         "NOX_CELL_TEMP": 50.4671}),
])
def test_reads_each_model(model, unit, expect):
    fake = FakeAnalyzer(TABLES[model], noise=b"\x00\x13", split=True)
    ad = adapter(model, fake)
    out = by_param(ad.read(NOW))
    assert set(out) == set(parameters(device(model)))
    for param, value in expect.items():
        assert out[param].value == pytest.approx(value, rel=1e-4), param
        assert out[param].status == STATUS_VALID
        assert out[param].time == NOW
    assert {out[gas].unit for gas in MODELS[model].gas.values()} == {unit}
    status_param = [p for p in out if p.endswith("_STATUS")][0]
    assert out[status_param].value == 0.0
    ad.read(NOW)                                  # second sample reuses the same connection
    assert fake.connects == 1
    ad.close()


def test_calibration_blanks_gas_but_keeps_status_and_diagnostics():
    fake = FakeAnalyzer({**TABLES["AQMS-600"], 0x27: b"\x0c"})      # Mo converter calibration
    out = by_param(adapter("AQMS-600", fake).read(NOW))
    for gas in ("NO", "NO2", "NOX"):
        assert out[gas].value is None and out[gas].status == STATUS_CALIBRATION
    assert out["NOX_STATUS"].value == 12.0
    assert out["NOX_FLOW"].value == pytest.approx(560.5917, abs=1e-3)


def test_ppm_and_ppb_are_converted_and_logged_once(caplog):
    caplog.set_level(logging.INFO)
    ad = adapter("AQMS-400", FakeAnalyzer(TABLES["AQMS-400"]), unit="ppb")     # reply is ppm
    out = by_param(ad.read(NOW))
    assert out["CO"].value == pytest.approx(697.3, rel=1e-4) and out["CO"].unit == "ppb"
    assert out["CO"].status == STATUS_VALID
    ad.read(NOW)
    assert sum("converting to ppb" in r.getMessage() for r in caplog.records) == 1
    caplog.clear()
    out = by_param(adapter("AQMS-500", FakeAnalyzer(TABLES["AQMS-500"]), unit="ppm").read(NOW))
    assert out["SO2"].value == pytest.approx(0.004851, rel=1e-4)               # reply is ppb
    assert any("analyzer reports ppb; converting to ppm" in r.getMessage() for r in caplog.records)


def test_unit_that_cannot_be_converted_blanks_gas(caplog):
    co = bytearray(DATA["CO 0x2C"])
    co[4] = 4                                                     # analyzer switched to mg/m3
    ad = adapter("AQMS-400", FakeAnalyzer({**TABLES["AQMS-400"], 0x2C: bytes(co)}))
    out = by_param(ad.read(NOW))
    assert out["CO"].value is None and out["CO"].status == STATUS_OUT_OF_RANGE
    assert out["CO_STATUS"].value == 0.0 and out["CO_FLOW"].status == STATUS_VALID
    ad.read(NOW)
    assert sum("can't be converted to ppm" in r.getMessage() for r in caplog.records) == 1


def test_analyzer_down_gives_comm_fail_and_backs_off():
    t = [1000.0]
    fake = FakeAnalyzer(TABLES["AQMS-500"], refuse=True)
    ad = adapter("AQMS-500", fake, clock=lambda: t[0])
    out = ad.read(NOW)
    assert all(r.value is None and r.status == STATUS_COMM_FAIL for r in out)
    assert len(out) == len(parameters(device("AQMS-500")))
    t[0] += 10
    ad.read(NOW)                                  # still inside the 30 s back-off: not contacted
    assert fake.connects == 1
    fake.refuse = False
    t[0] += 25
    assert by_param(ad.read(NOW))["SO2"].value == pytest.approx(4.851, rel=1e-4)
    assert fake.connects == 2


def test_dropped_connection_reconnects_in_the_same_sample():
    fake = FakeAnalyzer(TABLES["AQMS-400"], close_after=2)        # drops after each sample
    ad = adapter("AQMS-400", fake)
    assert by_param(ad.read(NOW))["CO"].status == STATUS_VALID
    out = by_param(ad.read(NOW))                                  # stale socket -> fresh one
    assert out["CO"].value == pytest.approx(0.6973, abs=1e-3)
    assert fake.connects == 2


def test_missing_optional_diagnostics_keeps_o3():
    t = [0.0]
    table = {k: v for k, v in TABLES["AQMS-300"].items() if k != 0x2C}
    fake = FakeAnalyzer(table)
    ad = adapter("AQMS-300", fake, clock=lambda: t[0])
    out = by_param(ad.read(NOW))
    assert out["O3"].value == pytest.approx(44.2031, rel=1e-4)
    assert out["O3_MEAS_V"].value is None and out["O3_MEAS_V"].status == STATUS_OUT_OF_RANGE
    fake.requests.clear()
    ad.read(NOW)                                  # 0x2C is skipped for a while, no waiting
    assert 0x2C not in fake.requests


def test_diagnostics_can_be_turned_off():
    out = by_param(adapter("AQMS-500", FakeAnalyzer(TABLES["AQMS-500"]), diagnostics=False).read(NOW))
    assert set(out) == {"SO2", "SO2_STATUS"}


def test_bad_model_or_unit_is_rejected():
    with pytest.raises(ValueError):
        FpiTcpAdapter(device("AQMS-999"), "TEST")
    with pytest.raises(ValueError):
        FpiTcpAdapter(device("AQMS-400", unit="ppt"), "TEST")


# ---- logger wiring ----------------------------------------------------------------
def test_status_is_max_and_gas_mean_over_measuring_samples():
    fake = FakeAnalyzer(TABLES["AQMS-600"])
    ad = adapter("AQMS-600", fake)
    agg = MinuteAggregator(max_params=status_parameters([device("AQMS-600")]))
    agg.add(ad.read(NOW))                          # measuring
    fake.table[0x27] = b"\x02"                     # span calibration for one sample
    agg.add(ad.read(NOW))
    fake.table[0x27] = b"\x00"
    agg.add(ad.read(NOW))
    out = by_param(agg.build(NOW.replace(second=0)))
    assert out["NOX_STATUS"].value == 2.0          # the calibration is visible for the minute
    assert out["NO"].value == pytest.approx(4.6946, rel=1e-4)    # only measuring samples
    assert out["NOX_FLOW"].value == pytest.approx(560.5917, abs=1e-3)


def test_config_example_creates_fpi_columns(monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "x")
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.example.yaml"))
    params = _configured_parameters(cfg.devices)
    for p in ("O3", "CO", "SO2", "NO", "NO2", "NOX", "NOX_STATUS", "NOX_REACTION_PRESSURE",
              "PM2_5", "STATUS"):
        assert p in params
    assert len({p.lower() for p in params}) == len(params)      # no clashes (SQLite ignores case)
    units = {d.name: d.options["unit"] for d in cfg.devices if d.type == "fpi_tcp"}
    assert units == {"fpi_o3": "ppb", "fpi_co": "ppm", "fpi_so2": "ppb", "fpi_nox": "ppb"}
