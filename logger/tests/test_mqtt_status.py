"""Retained online/offline flag on aqms/<station>/logger for the server watchdog."""
import json

from aqms_logger import mqtt_publisher
from aqms_logger.config import LoggerConfig, MqttConfig


class _Info:
    def wait_for_publish(self, timeout):
        pass


class _FakeClient:
    def __init__(self, *args, **kwargs):
        self.will = None
        self.published = []
        self.connected = True
        self.calls = []

    def ws_set_options(self, **kw): pass
    def username_pw_set(self, *a): pass
    def tls_set(self, **kw): pass
    def reconnect_delay_set(self, **kw): pass
    def connect_async(self, *a, **kw): pass
    def loop_start(self): pass

    def will_set(self, topic, payload, qos, retain):
        self.will = (topic, json.loads(payload), qos, retain)

    def publish(self, topic, payload, qos=0, retain=False):
        self.published.append((topic, json.loads(payload), qos, retain))
        return _Info()

    def is_connected(self):
        return self.connected

    def disconnect(self):
        self.calls.append("disconnect")

    def loop_stop(self):
        self.calls.append("loop_stop")


def _publisher(monkeypatch, status_topic="aqms/TEST/logger"):
    monkeypatch.setattr(mqtt_publisher.mqtt, "Client", _FakeClient)
    return mqtt_publisher.MqttPublisher(MqttConfig(host="x"), status_topic=status_topic,
                                        station_id="TEST")


def test_status_topic_name():
    cfg = LoggerConfig(station_id="TEST", mqtt=MqttConfig(host="x", base_topic="aqms"), devices=[])
    assert cfg.status_topic == "aqms/TEST/logger"


def test_last_will_says_connection_lost(monkeypatch):
    pub = _publisher(monkeypatch)
    topic, payload, qos, retain = pub._client.will
    assert topic == "aqms/TEST/logger" and qos == 1 and retain
    assert payload == {"station_id": "TEST", "state": "offline", "reason": "connection lost"}


def test_online_on_connect_and_stopped_on_close(monkeypatch):
    pub = _publisher(monkeypatch)
    client = pub._client
    pub._on_connect(client, None, None, 0)
    topic, payload, _, retain = client.published[-1]
    assert topic == "aqms/TEST/logger" and retain
    assert payload["state"] == "online" and payload["since"].endswith("Z")

    pub.close()
    topic, payload, _, retain = client.published[-1]
    assert payload["state"] == "offline" and payload["reason"] == "logger stopped" and retain
    assert client.calls == ["disconnect", "loop_stop"]     # the offline flag goes out first


def test_no_status_without_topic(monkeypatch):
    pub = _publisher(monkeypatch, status_topic=None)       # --once spot reads
    pub._on_connect(pub._client, None, None, 0)
    pub.close()
    assert pub._client.will is None and pub._client.published == []
