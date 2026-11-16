"""Health check (python -m aqms_logger.status): states per instrument, freshness, backlog."""
import sqlite3
from datetime import datetime, timedelta, timezone

from aqms_logger import status
from aqms_logger.config import DeviceConfig, LoggerConfig, MqttConfig, RegisterSpec
from aqms_logger.main import _configured_parameters
from aqms_logger.models import Reading
from aqms_logger.storage import Storage

NOW = datetime(2026, 11, 16, 8, 16, 30, tzinfo=timezone.utc)
LAST = datetime(2026, 11, 16, 8, 15, tzinfo=timezone.utc)      # the minute stored at 08:16

FIDAS = DeviceConfig(name="fidas200", type="modbus_tcp", registers=[
    RegisterSpec(parameter="PM2_5", address=41, unit="ug/m3"),
    RegisterSpec(parameter="PM10", address=45, unit="ug/m3"),
    RegisterSpec(parameter="FLOW", address=14, unit="l/min"),
])
SO2 = DeviceConfig(name="fpi_so2", type="fpi_tcp",
                   options={"model": "AQMS-500", "unit": "ppb", "diagnostics": False})
GC = DeviceConfig(name="gc", type="synspec_gc", options={
    "components": {1: {"parameter": "CH4", "unit": "ppb"}, 2: {"parameter": "NMHC", "unit": "ppb"}},
    "derived": {"THC": "CH4 + NMHC"}, "diagnostics": False})
CFG = LoggerConfig(station_id="TEST", mqtt=MqttConfig(host="x"), devices=[FIDAS, SO2, GC])

GOOD = {"PM2_5": 12.3, "PM10": 20.1, "FLOW": 4.8, "SO2": 3.2, "SO2_STATUS": 0.0,
        "CH4": 2010.5, "NMHC": 150.0, "THC": 2160.5, "GC_STATUS": 0.0}


def _db(tmp_path, rows: dict, synced=True):
    """rows: {minute datetime: {param: value}} -> an open connection to a logger database."""
    path = str(tmp_path / "logger.db")
    st = Storage(path)
    st.ensure_param_columns(_configured_parameters(CFG.devices))
    for ts, values in rows.items():
        st.insert_readings([Reading(ts, "TEST", "d", p, v, "") for p, v in values.items()])
        if synced:
            st.mark_synced(ts.strftime("%Y-%m-%dT%H:%M:%SZ"), "TEST")
    st.close()
    return sqlite3.connect(path)


def _hour(values: dict, last=LAST):
    return {last - timedelta(minutes=i): dict(values) for i in range(60)}


def test_all_ok(tmp_path):
    lines, problems = status.report(CFG, _db(tmp_path, _hour(GOOD)), NOW, ("AQMS-Logger", "RUNNING"))
    text = "\n".join(lines)
    assert problems == []
    assert "RUNNING" in text and "60/60" in text and "0 row(s)  OK" in text
    assert "PM2_5=12.3  PM10=20.1" in text            # concentrations shown, FLOW not
    assert "CH4=2010  NMHC=150  THC=2160" in text      # .4g keeps it short
    assert "100%" in text


def test_instrument_not_answering(tmp_path):
    rows = _hour(GOOD)
    for i in range(10):                                # SO2 analyzer silent for the last 10 minutes
        rows[LAST - timedelta(minutes=i)].update(SO2=None, SO2_STATUS=None)
    lines, problems = status.report(CFG, _db(tmp_path, rows), NOW)
    assert problems == ["fpi_so2: no answer (no value from this instrument in the last stored minute)"]
    so2_line = next(line for line in lines if line.startswith("fpi_so2"))
    assert "NO ANSWER" in so2_line and "83%" in so2_line and "SO2=-" in so2_line


def test_calibration_is_shown_but_not_a_problem(tmp_path):
    rows = _hour(GOOD)
    rows[LAST].update(SO2=None, SO2_STATUS=4.0, CH4=None, NMHC=None, THC=None, GC_STATUS=4.0)
    lines, problems = status.report(CFG, _db(tmp_path, rows), NOW)
    text = "\n".join(lines)
    assert problems == []
    assert "CALIBRATING" in text and "status 4 = auto span cal" in text
    assert "GC_STATUS 4 = calibration" in text


def test_fpi_failure_and_gc_invalid_are_problems(tmp_path):
    rows = _hour(GOOD)
    rows[LAST].update(SO2=None, SO2_STATUS=8.0, CH4=None, NMHC=None, THC=None, GC_STATUS=5.0)
    _, problems = status.report(CFG, _db(tmp_path, rows), NOW)
    assert [p.split(":")[0] for p in problems] == ["fpi_so2", "gc"]
    assert "failure" in problems[0] and "invalid" in problems[1]


def test_blank_gas_while_measuring_is_partial(tmp_path):
    rows = _hour(GOOD)
    rows[LAST].update(SO2=None)                        # e.g. analyzer set to ug/m3
    _, problems = status.report(CFG, _db(tmp_path, rows), NOW)
    assert problems == ["fpi_so2: partial (blank: SO2)"]


def test_logger_stopped(tmp_path):
    later = NOW + timedelta(minutes=20)
    _, problems = status.report(CFG, _db(tmp_path, _hour(GOOD)), later, ("AQMS-Logger", "STOPPED"))
    assert problems[0] == "service AQMS-Logger is STOPPED"
    assert "no new data since 21 min ago" in problems[1]


def test_backlog_not_reaching_server(tmp_path):
    lines, problems = status.report(CFG, _db(tmp_path, _hour(GOOD), synced=False), NOW)
    assert "60 row(s)  NOT REACHING THE SERVER" in "\n".join(lines)
    assert problems[0].startswith("60 rows not sent to the server yet")


def test_empty_database(tmp_path):
    lines, problems = status.report(CFG, _db(tmp_path, {}), NOW)
    assert problems == ["the logger has not stored any data yet"]


def test_main_exit_codes(tmp_path, monkeypatch):
    monkeypatch.setenv("MQTT_PASSWORD", "x")
    (tmp_path / "config.yaml").write_text(
        "station_id: TEST\nsqlite_path: logger.db\nmqtt: {host: x, password: '${MQTT_PASSWORD}'}\n"
        "devices:\n  - {name: fidas200, type: modbus_tcp, registers: "
        "[{parameter: PM2_5, address: 41, unit: ug/m3}]}\n", encoding="utf-8")
    cfg = str(tmp_path / "config.yaml")
    assert status.main(["--config", cfg, "--service", ""]) == 2          # no database yet
    _db(tmp_path, {datetime.now(timezone.utc).replace(second=0, microsecond=0): {"PM2_5": 5.0}})
    assert status.main(["--config", cfg, "--service", ""]) == 0
    assert status.main(["--config", str(tmp_path / "missing.yaml")]) == 2
