"""Alert logic, simulated minute by minute like the logger publishes."""
from datetime import datetime, timezone

from aqms_watchdog.monitor import Check, DailyReport, Monitor, Station, duration

T0 = datetime(2026, 11, 16, 0, 0, tzinfo=timezone.utc).timestamp()     # 08:00 in Malaysia
ALL = {"MODUS": 1, "PM2_5": 12.0, "SO2_STATUS": 0, "SO2": 3.0}
NO_SO2 = {"MODUS": 1, "PM2_5": 12.0}                                  # analyzer not answering
SO2_BLANK = {"MODUS": 1, "PM2_5": 12.0, "SO2_STATUS": 0}             # answering, no valid SO2


def station():
    return Station("S1", "Test site", offline_minutes=5, checks=[
        Check("Fidas", ["MODUS", "PM2_5"], 10),
        Check("SO2 analyzer", ["SO2_STATUS"], 10, hint="Check the analyzer."),
        Check("SO2 value", ["SO2"], 180, needs="SO2 analyzer"),
    ])


def iso(t):
    return datetime.fromtimestamp(t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Sim:
    """Wall clock + a station that publishes the minute that just ended, like the logger."""

    def __init__(self, repeat_hours=6.0):
        self.m = Monitor([station()], utc_offset_hours=8, repeat_hours=repeat_hours, now=T0)
        self.now = T0
        self.sent = []

    def minutes(self, values=None, n=1, connected=True):
        """n minutes; values=None means nothing arrives from the station."""
        for _ in range(n):
            self.now += 60
            if values is not None:
                self.publish(self.now - 60, values)
            self.sent += self.m.evaluate(self.now, connected)

    def publish(self, ts, values):
        self.m.on_readings({"station_id": "S1", "ts": iso(ts), "values": values}, self.now)

    def subjects(self):
        return [m.subject for m in self.sent]


def test_quiet_while_everything_arrives():
    sim = Sim()
    sim.minutes(ALL, 300)
    assert sim.sent == []


def test_station_offline_once_then_back_online():
    sim = Sim()
    sim.minutes(ALL, 10)
    sim.minutes(None, 5)
    assert sim.sent == []                                   # 5 min is still OK
    sim.minutes(None, 1)
    assert sim.subjects() == ["[AQMS] Test site (S1): station OFFLINE"]
    assert "No data for 6 min (last received 16 Nov 08:10)" in sim.sent[0].body
    sim.minutes(None, 30)
    assert len(sim.sent) == 1                               # no repeat within repeat_hours
    sim.minutes(ALL, 1)
    assert sim.subjects()[-1] == "[AQMS] Test site (S1): station back online"
    assert "after 37 min offline" in sim.sent[-1].body
    sim.minutes(ALL, 30)
    assert len(sim.sent) == 2


def test_instrument_down_gives_one_alert_and_a_recovery():
    sim = Sim()
    sim.minutes(ALL, 5)
    sim.minutes(NO_SO2, 10)
    assert sim.sent == []
    sim.minutes(NO_SO2, 1)
    assert sim.subjects() == ["[AQMS] Test site (S1): SO2 analyzer - no data"]
    body = sim.sent[0].body
    assert "No data for 11 min" in body and "Watched values: SO2_STATUS" in body
    assert "Check the analyzer." in body
    sim.minutes(NO_SO2, 300)                                # 5 h: no "SO2 value" alert on top
    assert len(sim.sent) == 1
    sim.minutes(ALL, 1)
    assert sim.subjects()[-1] == "[AQMS] Test site (S1): SO2 analyzer OK again"
    sim.minutes(ALL, 60)
    assert len(sim.sent) == 2


def test_value_blank_for_hours_while_answering():
    sim = Sim()
    sim.minutes(ALL, 5)
    sim.minutes(SO2_BLANK, 180)
    assert sim.sent == []                                   # normal calibration length
    sim.minutes(SO2_BLANK, 1)
    assert sim.subjects() == ["[AQMS] Test site (S1): SO2 value - no data"]
    sim.minutes(ALL, 1)
    assert sim.subjects()[-1] == "[AQMS] Test site (S1): SO2 value OK again"


def test_station_outage_is_one_alert_not_one_per_instrument():
    sim = Sim()
    sim.minutes(ALL, 10)
    sim.minutes(None, 240)
    sim.minutes(ALL, 30)
    assert [s.split(": ", 1)[1] for s in sim.subjects()] == ["station OFFLINE",
                                                             "station back online"]


def test_value_gets_its_full_time_again_after_an_outage():
    sim = Sim()
    sim.minutes(ALL, 10)
    sim.minutes(None, 240)                                  # PC off 4 h, no backlog
    sim.minutes(SO2_BLANK, 60)                              # analyzer warming up after power cut
    sim.minutes(ALL, 10)
    assert [s.split(": ", 1)[1] for s in sim.subjects()] == ["station OFFLINE",
                                                             "station back online"]


def test_backlog_replay_after_internet_outage():
    sim = Sim()
    sim.minutes(ALL, 10)
    start = sim.now
    sim.minutes(None, 30)                                   # internet down; PC keeps storing
    for i in range(30):                                     # replayed oldest first in one go
        sim.publish(start + 60 * i, ALL)
    sim.minutes(ALL, 5)
    assert [s.split(": ", 1)[1] for s in sim.subjects()] == ["station OFFLINE",
                                                             "station back online"]


def test_reminder_every_repeat_hours():
    sim = Sim(repeat_hours=6)
    sim.minutes(ALL, 5)
    sim.minutes(NO_SO2, 7 * 60)
    assert sim.subjects() == ["[AQMS] Test site (S1): SO2 analyzer - no data",
                              "[AQMS] Test site (S1): SO2 analyzer - no data (still)"]
    assert sim.sent[1].body.startswith("🔴 STILL: SO2 analyzer")


def test_no_reminders_when_repeat_is_zero():
    sim = Sim(repeat_hours=0)
    sim.minutes(ALL, 5)
    sim.minutes(NO_SO2, 13 * 60)
    assert len(sim.sent) == 1


def test_no_data_at_all_since_start():
    sim = Sim()
    sim.minutes(None, 6)
    assert sim.subjects() == ["[AQMS] Test site (S1): station OFFLINE"]
    assert "No data since the watchdog started (16 Nov 08:00)" in sim.sent[0].body


def test_offline_message_says_what_the_logger_reported():
    for payload, text in [({"state": "offline", "reason": "connection lost"}, "without a goodbye"),
                          ({"state": "offline", "reason": "logger stopped"}, "service was stopped"),
                          ({"state": "online"}, "still connected but sends no data")]:
        sim = Sim()
        sim.minutes(ALL, 10)
        sim.m.on_logger("S1", payload)
        sim.minutes(None, 6)
        assert text in sim.sent[0].body


def test_broker_down_is_reported_instead_of_the_station():
    sim = Sim()
    sim.minutes(ALL, 10)
    sim.minutes(None, 6, connected=False)                   # noticed at the 1st tick: 5 min so far
    assert sim.sent == []
    sim.minutes(None, 1, connected=False)
    assert sim.subjects() == ["[AQMS] watchdog cannot reach the MQTT broker"]
    sim.minutes(None, 1)                                    # broker back, station not yet heard
    assert sim.subjects()[-1] == "[AQMS] MQTT broker reachable again"
    sim.minutes(ALL, 10)
    assert len(sim.sent) == 2                               # no false "station OFFLINE"


def test_unknown_station_is_ignored():
    m = Monitor([station()], now=T0)
    assert not m.on_readings({"station_id": "OTHER", "ts": iso(T0), "values": {}}, T0)
    assert m.unknown == {"OTHER"}


def test_daily_summary():
    sim = Sim()
    sim.minutes(ALL, 5)
    msg = sim.m.summary(sim.now)
    assert msg.subject == "[AQMS] daily check: all OK"
    assert "✅ Test site (S1): all OK (3 checks), last data 16 Nov 08:05" in msg.body
    sim.minutes(NO_SO2, 11)
    msg = sim.m.summary(sim.now)
    assert msg.subject == "[AQMS] daily check: 1 problem(s)"
    assert "   - SO2 analyzer" in msg.body


def test_daily_report_due_once_a_day():
    sim = Sim()
    tz = sim.m.tz
    hour = 3600
    report = DailyReport("08:00", tz, T0 - hour)            # started 07:00 local
    assert not report.due(T0 - 60)                          # 07:59
    assert report.due(T0)                                   # 08:00
    assert not report.due(T0 + hour)                        # once a day
    assert report.due(T0 + 24 * hour)
    late = DailyReport("08:00", tz, T0 + 2 * hour)          # started 10:00: first one tomorrow
    assert not late.due(T0 + 3 * hour) and late.due(T0 + 24 * hour)
    assert DailyReport(480, tz, T0).at == (8, 0)            # YAML 8:00 without quotes
    assert not DailyReport("", tz, T0).due(T0)


def test_duration_text():
    assert duration(6 * 60) == "6 min"
    assert duration(3 * 3600 + 5 * 60) == "3 h 05 min"
    assert duration(3 * 86400 + 7200) == "3 days 2 h"
