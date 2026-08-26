"""FpiMysqlAdapter: mapping, freshness guard, null handling, and DB-error → comm-fail.
Uses an injected fake DB connection (no MySQL required)."""
from datetime import datetime, timedelta, timezone

from aqms_logger.adapters.fpi_mysql import FpiMysqlAdapter
from aqms_logger.config import DeviceConfig
from aqms_logger.models import STATUS_COMM_FAIL, STATUS_VALID

NOW = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)   # poll boundary passed to read()


class _FakeCursor:
    def __init__(self, row):
        self.row = row

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql):
        self.sql = sql

    def fetchone(self):
        return self.row


class _FakeConn:
    def __init__(self, row):
        self.row = row

    def cursor(self):
        return _FakeCursor(self.row)

    def close(self):
        pass


def _device():
    return DeviceConfig(
        name="fpi_gases", type="fpi_mysql",
        options={
            "database": "aqms", "user": "ro", "password": "x",
            "table": "realtime_data", "time_column": "data_time",
            "columns": {"O3": "o3_conc", "SO2": "so2_conc", "CO": "co_conc", "NOX": "nox_conc"},
            "units": {"O3": "ug/m3", "SO2": "ug/m3", "CO": "mg/m3", "NOX": "ug/m3"},
            "max_age_seconds": 120,
        },
    )


def _adapter(row=None, connect=None):
    conn_factory = connect or (lambda: _FakeConn(row))
    return FpiMysqlAdapter(_device(), "TEST", connect=conn_factory)


def test_fresh_row_maps_all_gases():
    row = {"data_time": datetime.now(), "o3_conc": 5.1, "so2_conc": 2.3,
           "co_conc": 0.4, "nox_conc": 10.0}
    out = {r.parameter: r for r in _adapter(row).read(NOW)}
    assert out["O3"].value == 5.1 and out["O3"].unit == "ug/m3" and out["O3"].status == STATUS_VALID
    assert out["CO"].unit == "mg/m3"
    assert all(r.time == NOW for r in out.values())      # aligned to the poll boundary


def test_stale_row_marks_comm_fail():
    row = {"data_time": datetime.now() - timedelta(minutes=10), "o3_conc": 5.1,
           "so2_conc": 2.3, "co_conc": 0.4, "nox_conc": 10.0}
    out = _adapter(row).read(NOW)
    assert all(r.value is None and r.status == STATUS_COMM_FAIL for r in out)


def test_null_value_is_comm_fail_per_param():
    row = {"data_time": datetime.now(), "o3_conc": None, "so2_conc": 2.3,
           "co_conc": 0.4, "nox_conc": 10.0}
    out = {r.parameter: r for r in _adapter(row).read(NOW)}
    assert out["O3"].value is None and out["O3"].status == STATUS_COMM_FAIL
    assert out["SO2"].value == 2.3 and out["SO2"].status == STATUS_VALID


def test_db_error_marks_all_comm_fail():
    def boom():
        raise ConnectionError("mysql down")
    out = _adapter(connect=boom).read(NOW)
    assert len(out) == 4
    assert all(r.value is None and r.status == STATUS_COMM_FAIL for r in out)
