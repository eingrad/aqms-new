"""Synspec GC over Modbus TCP (synspec_gc_map + SynspecGcAdapter + gc_probe), against an in-memory
GC laid out as in tech note E066 - the TNBJ-AQMS1 Alpha 115 methane / TNMHC analyser."""
import logging
import math
import os
import struct
from datetime import datetime, timezone

import pytest

from aqms_logger import synspec_gc_map as gc
from aqms_logger.adapters import build_adapter
from aqms_logger.adapters.synspec_gc import (
    DIAGNOSTICS, SynspecGcAdapter, parameters, reductions,
)
from aqms_logger.aggregator import MinuteAggregator
from aqms_logger.config import DeviceConfig, load_config
from aqms_logger.gc_probe import Reader, report, suggest_parameter, suggest_unit
from aqms_logger.main import _configured_parameters
from aqms_logger.models import (
    STATUS_ALARM, STATUS_CALIBRATION, STATUS_COMM_FAIL, STATUS_MAINTENANCE, STATUS_OUT_OF_RANGE,
    STATUS_VALID,
)

NOW = datetime(2026, 11, 13, 14, 40, 35, tzinfo=timezone.utc)
NORMAL_183 = (1 << 5) | (1 << 12)          # normal run mode, LED running
NORMAL_184 = 1 << 7                        # machine available for normal measurements


# ---- an in-memory GC ------------------------------------------------------------------------
def text_words(s: str, n: int) -> list[int]:
    b = s.encode("cp1252").ljust(2 * n, b" ")
    return [b[i] << 8 | b[i + 1] for i in range(0, 2 * n, 2)]


def float_words(v: float) -> list[int]:
    return list(struct.unpack(">HH", struct.pack(">f", v)))


def datetime_words(s: str, fmt: str) -> list[int]:
    """'DDMMYYYYHHMMSS' as 7 registers in the given layout."""
    pairs = [s[i:i + 2] for i in range(0, 14, 2)]
    if fmt == "text":
        return [ord(p[0]) << 8 | ord(p[1]) for p in pairs]
    if fmt == "bcd":
        return [int(p[0]) << 4 | int(p[1]) for p in pairs]
    return [int(p) for p in pairs]                          # binary


def gc_memory(values=None, units=None, names=None, s181=0, s183=NORMAL_183, s184=NORMAL_184,
              s185=0, s186=0, channel=1, clock="13112026224035", run="13112026223500",
              run_fmt="bcd", inlet=2.88, flow=11.5):
    values = {1: 1.95, 2: 0.412} if values is None else values
    units = {1: 4, 2: 4} if units is None else units
    names = {1: "Methane", 2: "TNMHC"} if names is None else names
    mem = {a: 0 for a in range(2142)}

    def put(address, words):
        mem.update(zip(range(address, address + len(words)), words))

    put(gc.MANUFACTURER, text_words("Synspec BV.", 10))
    put(gc.CLOCK, datetime_words(clock, "text"))
    put(gc.STATUS, [s181, 0, s183, s184, s185, s186, channel])
    put(gc.SOFTWARE_VERSION, [3, 4, 8, 1])
    put(gc.SERIAL_NUMBER, [0, 51143])
    put(gc.CYCLE_TIME, [63, 177])
    put(gc.INLET_PRESSURE, float_words(inlet) + float_words(flow))
    put(gc.MACHINE_TYPE, [3])
    put(gc.RUN_TIME, datetime_words(run, run_fmt) if run else [0] * 7)
    for n in range(1, gc.MAX_COMPONENTS + 1):
        put(gc.NAMES + gc.NAME_WORDS * (n - 1), text_words(names.get(n, ""), gc.NAME_WORDS))
    for n, v in values.items():
        put(gc.VALUES + 2 * (n - 1), float_words(v))
    for n, code in units.items():
        put(gc.UNITS + n - 1, [code])
    put(gc.RETENTION_TIMES, [45, 71])
    return mem


class _Resp:
    def __init__(self, registers=None, error=False):
        self.registers = registers or []
        self._error = error

    def isError(self):
        return self._error


class FakeGc:
    """Answers read_holding_registers from `mem` like pymodbus' client. Write calls are recorded
    (and must never happen)."""

    def __init__(self, mem):
        self.mem = mem
        self.connected = False
        self.connects = 0
        self.refuse = False         # connect() fails
        self.silent = False         # connected, but reads time out
        self.drop_next = False      # next read: connection reset
        self.reads = []
        self.writes = []

    def connect(self):
        if self.refuse:
            return False
        if not self.connected:
            self.connects += 1
            self.connected = True
        return True

    def close(self):
        self.connected = False

    def read_holding_registers(self, address, *, count=1, device_id=1):
        self.reads.append((address, count))
        if self.drop_next:
            self.drop_next = False
            raise ConnectionResetError("connection reset by peer")
        if self.silent:
            raise TimeoutError("no response")
        try:
            return _Resp([self.mem[address + i] for i in range(count)])
        except KeyError:
            return _Resp(error=True)

    def write_register(self, *args, **kwargs):
        self.writes.append(args)

    write_registers = write_register


def device(**options):
    opts = {"components": {1: {"parameter": "CH4", "unit": "ppb"},
                           2: {"parameter": "NMHC", "unit": "ppb"}}}
    opts.update(options)
    return DeviceConfig(name="gc_alpha115", type="synspec_gc", host="127.0.0.1", port=9882,
                        unit_id=1, options=opts)


def adapter(fake, clock=None, **options):
    return SynspecGcAdapter(device(**options), "TEST", client=fake, clock=clock or (lambda: 0.0))


def by_param(readings):
    return {r.parameter: r for r in readings}


# ---- register map / decoding --------------------------------------------------------------
@pytest.mark.parametrize("fmt", ["text", "bcd", "binary"])
def test_datetime_layouts_decode_the_same(fmt):
    dt, found = gc.decode_datetime(datetime_words("13112026224035", fmt))
    assert dt == datetime(2026, 11, 13, 22, 40, 35) and found == fmt


def test_datetime_rejects_zeros_and_garbage():
    assert gc.decode_datetime([0] * 7) is None
    assert gc.decode_datetime([0xFFFF] * 7) is None
    assert gc.decode_datetime([0x13, 0x11]) is None


def test_unit_factors_are_exact_conversions_only():
    assert gc.unit_factor("ppb", "ppm") == pytest.approx(1e-3)
    assert gc.unit_factor("ppm", "ppb") == 1000.0
    assert gc.unit_factor("mg/m3", "ug/m3") == 1000.0
    assert gc.unit_factor("ppm", "mg/m3") is None            # needs a molecular weight
    assert gc.unit_factor(None, "ppm") is None               # not calibrated


def test_state_code_priorities():
    ok = [0, 0, NORMAL_183, NORMAL_184, 0, 0, 1]
    assert gc.state_code(ok) == gc.OK
    assert gc.state_code(ok, stale=True) == gc.STALE
    cal_and_maintenance = [0, 0, NORMAL_183 | 1 << 3 | 1 << 6, NORMAL_184, 0, 0, 1]
    assert gc.state_code(cal_and_maintenance) == gc.CALIBRATION
    invalid_and_cal = [1 << 14, 0, NORMAL_183 | 1 << 3, NORMAL_184, 0, 0, 1]
    assert gc.state_code(invalid_and_cal) == gc.INVALID


# ---- adapter: normal reading ------------------------------------------------------------------
def test_reads_components_and_diagnostics():
    fake = FakeGc(gc_memory())
    out = by_param(adapter(fake).read(NOW))
    assert list(out) == parameters(device())
    assert out["CH4"].value == 1950.0 and out["CH4"].unit == "ppb"    # GC: 1.95 ppm, float32
    assert out["NMHC"].value == 412.0
    assert all(out[p].status == STATUS_VALID for p in out)
    assert out["GC_STATUS"].value == 0.0
    assert out["GC_DATA_AGE"].value == 335.0                    # 22:40:35 - 22:35:00
    assert out["GC_INLET_PRESSURE"].value == pytest.approx(2.88)
    assert out["GC_COLUMN_FLOW"].value == pytest.approx(11.5)
    assert out["GC_STATUS_183"].value == float(NORMAL_183)
    assert all(r.time == NOW for r in out.values())


@pytest.mark.parametrize("fmt", ["text", "bcd", "binary"])
def test_data_age_whatever_the_run_time_layout(fmt):
    out = by_param(adapter(FakeGc(gc_memory(run_fmt=fmt))).read(NOW))
    assert out["GC_DATA_AGE"].value == 335.0 and out["GC_STATUS"].value == 0.0


def test_reads_are_few_small_and_never_write():
    fake = FakeGc(gc_memory())
    ad = adapter(fake)
    ad.read(NOW)                                   # first sample also reads the names once
    first = list(fake.reads)
    fake.reads.clear()
    ad.read(NOW)
    assert sorted(fake.reads) == [(gc.CLOCK, 7), (gc.STATUS, 7), (gc.VALUES, 4),
                                  (gc.INLET_PRESSURE, 4), (gc.UNITS, 2), (gc.RUN_TIME, 7)]
    assert (gc.NAMES, 20) in first
    assert fake.writes == [] and fake.connects == 1


def test_names_are_logged_once(caplog):
    caplog.set_level(logging.INFO)
    ad = adapter(FakeGc(gc_memory(names={1: "Methane"})))
    ad.read(NOW)
    ad.read(NOW)
    msgs = [r.getMessage() for r in caplog.records]
    assert sum("GC component 1 'Methane' -> CH4 (ppb)" in m for m in msgs) == 1
    assert sum("GC component 2 has no name" in m for m in msgs) == 1


# ---- adapter: when the values are blank -----------------------------------------------------
@pytest.mark.parametrize("memory,code,status", [
    ({"s181": 1 << 14}, gc.INVALID, STATUS_ALARM),                  # output data invalid
    ({"s183": NORMAL_183 | 1 << 7}, gc.INVALID, STATUS_ALARM),      # GC software starting
    ({"s185": 1 << 1}, gc.INVALID, STATUS_ALARM),                   # FID flame error
    ({"channel": 0}, gc.INVALID, STATUS_ALARM),                     # first run after a restart
    ({"s183": NORMAL_183 | 1 << 3}, gc.CALIBRATION, STATUS_CALIBRATION),
    ({"s184": NORMAL_184 | 1 << 11}, gc.CALIBRATION, STATUS_CALIBRATION),   # span data on outputs
    ({"s184": NORMAL_184 | 1 << 12}, gc.CALIBRATION, STATUS_CALIBRATION),   # zero data on outputs
    ({"s184": NORMAL_184 | 1 << 13}, gc.CALIBRATION, STATUS_CALIBRATION),   # auto validation
    ({"s183": NORMAL_183 | 1 << 6}, gc.MAINTENANCE, STATUS_MAINTENANCE),
    ({"channel": 2}, gc.OTHER_CHANNEL, STATUS_MAINTENANCE),
    ({"run": "13112026203500"}, gc.STALE, STATUS_COMM_FAIL),        # last run 2 h ago
])
def test_blanks_values_and_says_why(memory, code, status):
    out = by_param(adapter(FakeGc(gc_memory(**memory))).read(NOW))
    for p in ("CH4", "NMHC"):
        assert out[p].value is None and out[p].status == status
    assert out["GC_STATUS"].value == float(code)
    assert out["GC_INLET_PRESSURE"].value == pytest.approx(2.88)    # diagnostics still stored


def test_ambient_channel_can_be_configured():
    out = by_param(adapter(FakeGc(gc_memory(channel=2)), channel=2).read(NOW))
    assert out["GC_STATUS"].value == 0.0 and out["CH4"].value == 1950.0


def test_stale_without_a_decodable_run_time_uses_how_long_it_stays_unchanged():
    t = [0.0]
    mem = gc_memory(run=None)                      # all zeros: can't be decoded
    ad = adapter(FakeGc(mem), clock=lambda: t[0], max_age_minutes=10)
    out = by_param(ad.read(NOW))
    assert out["GC_STATUS"].value == 0.0 and out["GC_DATA_AGE"].value is None
    t[0] = 601.0
    out = by_param(ad.read(NOW))
    assert out["GC_STATUS"].value == float(gc.STALE) and out["CH4"].value is None
    mem[gc.RUN_TIME + 6] = 1                       # the run time changes: a new run finished
    out = by_param(ad.read(NOW))
    assert out["GC_STATUS"].value == 0.0 and out["CH4"].value == 1950.0


def test_nan_value_blanks_only_that_component():
    out = by_param(adapter(FakeGc(gc_memory(values={1: math.nan, 2: 0.412}))).read(NOW))
    assert out["CH4"].value is None and out["CH4"].status == STATUS_OUT_OF_RANGE
    assert out["NMHC"].value == 412.0 and out["GC_STATUS"].value == 0.0


# ---- adapter: units -------------------------------------------------------------------------
def test_ppm_on_the_gc_is_stored_as_ppb_and_logged_once(caplog):
    caplog.set_level(logging.INFO)
    ad = adapter(FakeGc(gc_memory()))                      # the GC reports ppm (unit code 4)
    out = by_param(ad.read(NOW))
    ad.read(NOW)
    assert out["CH4"].value == 1950.0 and out["NMHC"].value == 412.0
    assert sum("is reported in ppm; converting to ppb" in r.getMessage()
               for r in caplog.records) == 2               # once per component


def test_ppb_on_the_gc_is_stored_unchanged(caplog):
    caplog.set_level(logging.INFO)
    fake = FakeGc(gc_memory(values={1: 1950.0, 2: 412.0}, units={1: 2, 2: 2}))
    out = by_param(adapter(fake).read(NOW))
    assert out["CH4"].value == 1950.0 and out["NMHC"].value == 412.0
    assert not any("converting" in r.getMessage() for r in caplog.records)


@pytest.mark.parametrize("code,text", [(3, "can't be converted to ppb"), (0, "not calibrated")])
def test_unit_that_cannot_be_stored_blanks_the_value(caplog, code, text):
    ad = adapter(FakeGc(gc_memory(units={1: code, 2: 4})))
    out = by_param(ad.read(NOW))
    ad.read(NOW)
    assert out["CH4"].value is None and out["CH4"].status == STATUS_OUT_OF_RANGE
    assert out["NMHC"].value == 412.0
    assert sum(text in r.getMessage() for r in caplog.records) == 1


# ---- adapter: communication ---------------------------------------------------------------------
def test_gc_down_gives_comm_fail_and_backs_off():
    t = [1000.0]
    fake = FakeGc(gc_memory())
    fake.refuse = True
    ad = adapter(fake, clock=lambda: t[0])
    out = ad.read(NOW)
    assert len(out) == len(parameters(device()))
    assert all(r.value is None and r.status == STATUS_COMM_FAIL for r in out)
    fake.refuse = False
    t[0] += 10
    ad.read(NOW)                                   # inside the 30 s back-off: not contacted
    assert fake.reads == []
    t[0] += 25
    assert by_param(ad.read(NOW))["CH4"].value == 1950.0


def test_silent_gc_is_not_retried_on_a_fresh_connection():
    fake = FakeGc(gc_memory())
    fake.silent = True
    out = adapter(fake).read(NOW)
    assert all(r.status == STATUS_COMM_FAIL for r in out)
    assert len(fake.reads) == 1                    # one timeout, then the back-off


def test_dropped_connection_reconnects_in_the_same_sample():
    fake = FakeGc(gc_memory())
    ad = adapter(fake)
    ad.read(NOW)
    fake.drop_next = True
    out = by_param(ad.read(NOW))
    assert out["CH4"].value == 1950.0 and fake.connects == 2


def test_diagnostics_can_be_turned_off():
    fake = FakeGc(gc_memory())
    out = by_param(adapter(fake, diagnostics=False).read(NOW))
    assert set(out) == {"CH4", "NMHC", "GC_STATUS"}
    assert (gc.INLET_PRESSURE, 4) not in fake.reads


@pytest.mark.parametrize("options", [
    {"components": {}},
    {"components": {0: {"parameter": "CH4", "unit": "ppm"}}},
    {"components": {41: {"parameter": "CH4", "unit": "ppm"}}},
    {"components": {1: {"parameter": "CH4", "unit": "ppt"}}},
    {"components": {1: {"unit": "ppm"}}},
    {"components": {1: {"parameter": "CH4", "unit": "ppm"}, 2: {"parameter": "ch4", "unit": "ppm"}}},
    {"channel": 0},
])
def test_bad_options_are_rejected(options):
    with pytest.raises(ValueError):
        SynspecGcAdapter(device(**options), "TEST", client=FakeGc({}))


# ---- logger wiring -------------------------------------------------------------------------
def test_minute_keeps_the_last_result_and_the_worst_status():
    fake = FakeGc(gc_memory())
    ad = adapter(fake)
    last, maximum = reductions([device()])
    agg = MinuteAggregator(last_params=last, max_params=maximum)
    agg.add(ad.read(NOW))                                       # previous run: 1.95
    fake.mem.update(zip(range(gc.STATUS, gc.STATUS + 3), [0, 0, NORMAL_183 | 1 << 3]))
    agg.add(ad.read(NOW))                                       # calibration for one sample
    fake.mem.update(zip(range(gc.STATUS, gc.STATUS + 3), [0, 0, NORMAL_183]))
    fake.mem.update(zip(range(gc.VALUES, gc.VALUES + 2), float_words(2.10)))
    fake.mem.update(zip(range(gc.INLET_PRESSURE, gc.INLET_PRESSURE + 2), float_words(2.90)))
    agg.add(ad.read(NOW))                                       # new run finished: 2.10
    out = by_param(agg.build(NOW.replace(second=0)))
    assert out["CH4"].value == 2100.0     # last, not a blend; float32 2.0999999 ppm -> 2100 ppb
    assert out["GC_STATUS"].value == float(gc.CALIBRATION)      # the calibration shows
    assert out["GC_INLET_PRESSURE"].value == pytest.approx((2.88 + 2.88 + 2.90) / 3, abs=1e-4)


def test_build_adapter_knows_the_type():
    assert isinstance(build_adapter(device(), "TEST"), SynspecGcAdapter)


def test_config_example_has_the_gc_without_column_clashes(monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "x")
    cfg = load_config(os.path.join(os.path.dirname(__file__), "..", "config.example.yaml"))
    gc_dev = [d for d in cfg.devices if d.type == "synspec_gc"]
    assert len(gc_dev) == 1 and gc_dev[0].port == 9882 and gc_dev[0].unit_id == 1
    params = _configured_parameters(cfg.devices)
    for p in ["CH4", "NMHC", "GC_STATUS"] + [d[0] for d in DIAGNOSTICS]:
        assert p in params
    comps = gc_dev[0].options["components"]
    assert {c["parameter"]: c["unit"] for c in comps.values()} == {"CH4": "ppb", "NMHC": "ppb"}
    assert len({p.lower() for p in params}) == len(params)      # SQLite ignores case


# ---- probe ------------------------------------------------------------------------------------
def test_probe_report_shows_what_to_compare():
    fake = FakeGc(gc_memory())
    read = Reader(fake, 1)
    text = "\n".join(report(read))
    for expected in ("Synspec BV.", "3 = GCAlpha", "3.4.8.1", "51143",
                     "13-11-2026 22:40:35 (text)", "13-11-2026 22:35:00 (bcd)", "335 s",
                     "cycle about 240 s", "2.88 bar", "GC_STATUS 0", "Methane", "TNMHC",
                     "1: { parameter: CH4, unit: ppb }    # GC reports ppm; the logger converts",
                     "2: { parameter: NMHC, unit: ppb }    # GC reports ppm; the logger converts"):
        assert expected in text, expected
    assert read.errors == [] and fake.writes == []


def test_probe_marks_blanking_bits_and_stops_on_a_silent_gc():
    fake = FakeGc(gc_memory(s183=NORMAL_183 | 1 << 3))
    text = "\n".join(report(Reader(fake, 1)))
    assert "*calibration mode" in text and "GC_STATUS 4" in text
    silent = FakeGc(gc_memory())
    silent.silent = True
    read = Reader(silent, 1)
    report(read)
    assert len(silent.reads) == 2 and len(read.errors) == 2     # gives up after two failures


def test_suggested_parameter_names():
    assert suggest_parameter("Methane") == "CH4"
    assert suggest_parameter("TNMHC") == "NMHC" and suggest_parameter("NMHC") == "NMHC"
    assert suggest_parameter("THC") == "THC"
    assert suggest_parameter("m/p-Xylene") == "M_P_XYLENE"
    assert suggest_unit("ppm") == "ppb" and suggest_unit("ppb") == "ppb"
    assert suggest_unit("ug/m3") == "ug/m3"           # mass units can't become ppb
