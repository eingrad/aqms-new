"""Wide storage + store-and-forward: one row per timestamp, column per parameter,
multi-device merge, ordered backfill, NULL handling."""
from datetime import datetime, timedelta, timezone

from aqms_logger import forwarder
from aqms_logger.models import Reading, STATUS_COMM_FAIL, STATUS_VALID
from aqms_logger.storage import Storage

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)


def _fidas(ts, station="TEST"):
    return [
        Reading(ts, station, "fidas200", "PM2_5", 12.3, "ug/m3", STATUS_VALID),
        Reading(ts, station, "fidas200", "PM10", 30.0, "ug/m3", STATUS_VALID),
    ]


class _FakePublisher:
    def __init__(self, connected=True, fail_after=None):
        self.connected = connected
        self.fail_after = fail_after
        self.sent = []

    def publish_json(self, topic, payload, timeout=5.0):
        if not self.connected:
            return False
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append(payload)
        return True


def _storage(tmp_path, params=("PM2_5", "PM10", "O3")):
    st = Storage(str(tmp_path / "test.db"))
    st.ensure_param_columns(list(params))
    return st


def test_one_wide_row_per_timestamp(tmp_path):
    st = _storage(tmp_path)
    assert st.insert_readings(_fidas(T0)) == 1        # 2 params -> ONE row
    assert st.count_unsynced() == 1
    row = st.fetch_unsynced()[0]
    assert row["PM2_5"] == 12.3 and row["PM10"] == 30.0
    assert "PM2_5" in st.param_columns() and "PM10" in st.param_columns()


def test_multi_device_merge_same_timestamp(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings(_fidas(T0))
    # a second device writes O3 at the same minute -> merged into the same row
    st.insert_readings([Reading(T0, "TEST", "fpi", "O3", 5.1, "ug/m3", STATUS_VALID)])
    assert st.count_unsynced() == 1
    row = st.fetch_unsynced()[0]
    assert row["PM2_5"] == 12.3 and row["O3"] == 5.1


def test_offline_buffers_then_backfills_in_order(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings(_fidas(T1))          # inserted out of order on purpose
    st.insert_readings(_fidas(T0))

    offline = _FakePublisher(connected=False)
    assert forwarder.flush(st, offline, "aqms/TEST/readings") == 0
    assert st.count_unsynced() == 2

    online = _FakePublisher(connected=True)
    assert forwarder.flush(st, online, "aqms/TEST/readings") == 2
    assert st.count_unsynced() == 0
    assert online.sent[0]["ts"].startswith("2026-08-24T10:00")   # oldest first
    assert online.sent[1]["ts"].startswith("2026-08-24T10:01")
    assert online.sent[0]["values"] == {"PM2_5": 12.3, "PM10": 30.0}


def test_partial_failure_preserves_remaining(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings(_fidas(T0))
    st.insert_readings(_fidas(T1))
    pub = _FakePublisher(connected=True, fail_after=1)
    assert forwarder.flush(st, pub, "aqms/TEST/readings") == 1
    assert st.count_unsynced() == 1


def test_none_value_stored_null_and_excluded_from_payload(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings([
        Reading(T0, "TEST", "fidas200", "PM2_5", 12.3, "ug/m3", STATUS_VALID),
        Reading(T0, "TEST", "fidas200", "PM10", None, "ug/m3", STATUS_COMM_FAIL),
    ])
    row = st.fetch_unsynced()[0]
    assert row["PM2_5"] == 12.3 and row["PM10"] is None
    pub = _FakePublisher()
    forwarder.flush(st, pub, "aqms/TEST/readings")
    assert pub.sent[0]["values"] == {"PM2_5": 12.3}     # NULL excluded
