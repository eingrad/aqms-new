"""Telegram / email sending, retries, and watchdog.yaml loading."""
import json
import os

import pytest

from aqms_watchdog.config import channels_from, load, stations_from
from aqms_watchdog.monitor import Message
from aqms_watchdog.notify import Email, Notifier, Telegram

MSG = Message("[AQMS] subject", "🔴 body")
YAML = os.path.join(os.path.dirname(__file__), "..", "watchdog.yaml")


class _Resp:
    def __init__(self, reply):
        self.reply = reply

    def read(self):
        return json.dumps(self.reply).encode()

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_telegram_send():
    sent = []

    def opener(req, timeout):
        sent.append((req.full_url, json.loads(req.data), timeout))
        return _Resp({"ok": True})

    Telegram("TOKEN", "-100123", opener=opener).send(MSG)
    url, data, _ = sent[0]
    assert url == "https://api.telegram.org/botTOKEN/sendMessage"
    assert data["chat_id"] == "-100123" and data["text"] == "🔴 body"


def test_telegram_refusal_raises():
    with pytest.raises(RuntimeError, match="chat not found"):
        Telegram("T", "1", opener=lambda r, timeout: _Resp({"ok": False,
                                                            "description": "chat not found"})).send(MSG)


class _FakeSmtp:
    log = []

    class SMTP:
        def __init__(self, host, port, timeout):
            _FakeSmtp.log.append(("connect", host, port))

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def starttls(self, context):
            _FakeSmtp.log.append(("starttls",))

        def login(self, user, password):
            _FakeSmtp.log.append(("login", user))

        def send_message(self, m):
            _FakeSmtp.log.append(("send", m["Subject"], m["To"], m.get_content().strip()))


def test_email_starttls():
    _FakeSmtp.log = []
    Email("smtp.example.com", 587, "starttls", "me@example.com", "pw", "me@example.com",
          ["a@example.com", "b@example.com"], smtp=_FakeSmtp).send(MSG)
    assert _FakeSmtp.log == [("connect", "smtp.example.com", 587), ("starttls",),
                             ("login", "me@example.com"),
                             ("send", "[AQMS] subject", "a@example.com, b@example.com", "🔴 body")]


class _Flaky:
    name = "flaky"

    def __init__(self, failures):
        self.failures = failures
        self.sent = []

    def send(self, msg):
        if self.failures:
            self.failures -= 1
            raise OSError("network down")
        self.sent.append(msg)


def test_failed_send_is_retried():
    ch = _Flaky(failures=2)
    n = Notifier([ch])
    assert n.send([MSG]) is False
    n.retry()
    assert ch.sent == []
    n.retry()
    assert ch.sent == [MSG]
    n.retry()
    assert ch.sent == [MSG]                                 # not sent twice


def test_retries_give_up():
    ch = _Flaky(failures=10)
    n = Notifier([ch], max_attempts=3)
    n.send([MSG])
    for _ in range(5):
        n.retry()
    assert ch.sent == [] and not n._outbox


def test_watchdog_yaml_loads(monkeypatch):
    for var in ("TELEGRAM_BOT_TOKEN", "ALERT_TELEGRAM_CHAT_ID", "SMTP_HOST", "SMTP_PORT",
                "SMTP_USER", "ALERT_EMAIL_TO"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("AQMS_STATION_ID", "S1")
    monkeypatch.setenv("AQMS_STATION_NAME", "Test site")
    raw = load(YAML)
    (st,) = stations_from(raw)
    assert st.id == "S1" and st.label == "Test site (S1)" and st.offline_minutes == 5
    names = [c.name for c in st.checks]
    assert all(c.needs in names for c in st.checks if c.needs)
    assert {"O3_STATUS", "CO_STATUS", "SO2_STATUS", "NOX_STATUS", "GC_STATUS"} <= {
        p for c in st.checks for p in c.any_of}
    assert channels_from(raw["notify"]) == []               # nothing set: log only

    # the dashboard's variable names for the bot + SMTP2GO, ALERT_* for the recipients
    monkeypatch.setenv("TELEGRAM_BOT_TOKEN", "T")
    monkeypatch.setenv("ALERT_TELEGRAM_CHAT_ID", "111, -222")
    monkeypatch.setenv("SMTP_HOST", "mail.smtp2go.com")
    monkeypatch.setenv("SMTP_PORT", "2525")
    monkeypatch.setenv("SMTP_USER", "aqms")
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.com")
    channels = channels_from(load(YAML)["notify"])
    assert [c.name for c in channels] == ["telegram:111", "telegram:-222", "email"]
    email = channels[2]
    assert (email.port, email.security, email.username) == (2525, "starttls", "aqms")


def test_email_password_also_read_as_smtp_pass(monkeypatch):
    """Lines copied verbatim from the dashboard's env (SMTP_PASS, not SMTP_PASSWORD)."""
    monkeypatch.delenv("SMTP_PASSWORD", raising=False)
    monkeypatch.setenv("SMTP_PASS", "from-dashboard")
    monkeypatch.setenv("SMTP_HOST", "mail.smtp2go.com")
    monkeypatch.setenv("ALERT_EMAIL_TO", "a@example.com")
    (email,) = [c for c in channels_from(load(YAML)["notify"]) if c.name == "email"]
    assert email.password == "from-dashboard"
    monkeypatch.setenv("SMTP_PASSWORD", "explicit")
    (email,) = [c for c in channels_from(load(YAML)["notify"]) if c.name == "email"]
    assert email.password == "explicit"


def test_station_id_is_required(monkeypatch):
    monkeypatch.delenv("AQMS_STATION_ID", raising=False)
    with pytest.raises(ValueError, match="AQMS_STATION_ID"):
        stations_from(load(YAML))


def test_needs_must_name_an_earlier_check():
    raw = {"stations": [{"id": "S1", "checks": [
        {"name": "value", "any_of": ["SO2"], "needs": "analyzer"},
        {"name": "analyzer", "any_of": ["SO2_STATUS"]}]}]}
    with pytest.raises(ValueError, match="listed above it"):
        stations_from(raw)
