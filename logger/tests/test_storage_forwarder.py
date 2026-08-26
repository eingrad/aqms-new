"""Storage + store-and-forward: buffering, dedupe, ordered backfill."""
from datetime import datetime, timedelta, timezone

from aqms_logger import forwarder
from aqms_logger.models import Reading, STATUS_VALID
from aqms_logger.storage import Storage

T0 = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)
T1 = T0 + timedelta(minutes=1)


def _readings(ts, station="TEST"):
    return [
        Reading(ts, station, "fidas200", "PM2_5", 12.3, "ug/m3", STATUS_VALID),
        Reading(ts, station, "fidas200", "PM10", 30.0, "ug/m3", STATUS_VALID),
    ]


class _FakePublisher:
    def __init__(self, connected=True, fail_after=None):
        self.connected = connected
        self.fail_after = fail_after     # publish fails once this many succeeded
        self.sent = []

    def publish_json(self, topic, payload, timeout=5.0):
        if not self.connected:
            return False
        if self.fail_after is not None and len(self.sent) >= self.fail_after:
            return False
        self.sent.append(payload)
        return True


def _storage(tmp_path):
    return Storage(str(tmp_path / "test.db"))


def test_dedupe_on_reinsert(tmp_path):
    st = _storage(tmp_path)
    assert st.insert_readings(_readings(T0)) == 2
    assert st.insert_readings(_readings(T0)) == 0   # same (time, device, parameter) ignored
    assert st.count_unsynced() == 2


def test_offline_buffers_then_backfills_in_order(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings(_readings(T1))   # insert out of order on purpose
    st.insert_readings(_readings(T0))

    offline = _FakePublisher(connected=False)
    assert forwarder.flush(st, offline, "aqms/TEST/readings") == 0
    assert st.count_unsynced() == 4     # nothing sent while offline

    online = _FakePublisher(connected=True)
    sent = forwarder.flush(st, online, "aqms/TEST/readings")
    assert sent == 2                    # two timestamps
    assert st.count_unsynced() == 0
    # oldest first
    assert online.sent[0]["ts"].startswith("2026-08-24T10:00")
    assert online.sent[1]["ts"].startswith("2026-08-24T10:01")
    # envelope shape
    assert online.sent[0]["station_id"] == "TEST"
    assert {r["parameter"] for r in online.sent[0]["readings"]} == {"PM2_5", "PM10"}


def test_partial_failure_preserves_remaining(tmp_path):
    st = _storage(tmp_path)
    st.insert_readings(_readings(T0))
    st.insert_readings(_readings(T1))

    pub = _FakePublisher(connected=True, fail_after=1)   # first ok, second fails
    sent = forwarder.flush(st, pub, "aqms/TEST/readings")
    assert sent == 1
    assert st.count_unsynced() == 2     # the second timestamp's 2 rows remain
