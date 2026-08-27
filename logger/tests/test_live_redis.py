"""LivePublisher: SET key with JSON + TTL, field mapping, None handling, error-swallowing."""
import json
from datetime import datetime, timezone

from aqms_logger.config import RedisConfig
from aqms_logger.live_redis import LivePublisher
from aqms_logger.models import Reading, STATUS_COMM_FAIL, STATUS_VALID

M = datetime(2026, 8, 24, 10, 0, tzinfo=timezone.utc)


class FakeRedis:
    def __init__(self, fail=False):
        self.calls = []
        self.fail = fail

    def set(self, key, value, ex=None):
        if self.fail:
            raise ConnectionError("redis down")
        self.calls.append((key, value, ex))

    def close(self):
        pass


def R(param, value, unit="ug/m3", status=STATUS_VALID):
    return Reading(M, "TNBJ-AQMS1", "fidas200", param, value, unit, status)


def _cfg(**kw):
    return RedisConfig(enabled=True, key_template="aqms:{station_id}:live", ttl_seconds=120, **kw)


def test_set_key_json_and_ttl():
    fake = FakeRedis()
    pub = LivePublisher(_cfg(), client=fake)
    assert pub.publish("TNBJ-AQMS1", M, [R("PM2_5", 12.3), R("PM10", 30.0)]) is True
    key, value, ex = fake.calls[0]
    assert key == "aqms:TNBJ-AQMS1:live"
    assert ex == 120
    doc = json.loads(value)
    assert doc["station_id"] == "TNBJ-AQMS1"
    assert doc["ts"] == "2026-08-24T10:00:00Z"
    assert doc["values"] == {"PM2_5": 12.3, "PM10": 30.0}


def test_none_excluded_and_field_map():
    fake = FakeRedis()
    pub = LivePublisher(_cfg(fields={"PM2_5": "pm25"}), client=fake)
    pub.publish("TNBJ-AQMS1", M, [R("PM2_5", 12.3), R("PM10", None, status=STATUS_COMM_FAIL)])
    doc = json.loads(fake.calls[0][1])
    assert doc["values"] == {"pm25": 12.3}          # renamed, None dropped


def test_redis_error_is_swallowed():
    pub = LivePublisher(_cfg(), client=FakeRedis(fail=True))
    assert pub.publish("TNBJ-AQMS1", M, [R("PM2_5", 12.3)]) is False   # no exception raised


def test_disabled_is_noop():
    pub = LivePublisher(RedisConfig(enabled=False))
    assert pub.enabled is False
    assert pub.publish("X", M, [R("PM2_5", 1.0)]) is False
