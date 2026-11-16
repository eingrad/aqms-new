"""Alert logic: from the stations' MQTT messages, decide when to send a problem / recovered message.

Inputs (the MQTT thread):
    aqms/<id>/readings   one message per minute, {"station_id", "ts", "values": {param: value}};
                         a parameter is left out when the instrument gave no value
    aqms/<id>/logger     the logger's retained online/offline flag (MQTT last will)

What is checked (`evaluate`, every check_interval_seconds):
    station OFFLINE   nothing arrived from the station for offline_minutes of WALL-CLOCK time
                      on this server (the station PC clock doesn't matter)
    instrument check  none of the check's parameters arrived for `minutes` of DATA time, i.e.
                      compared with the newest timestamp from that station. The backlog a station
                      replays after an outage is therefore judged minute by minute, and the
                      station clock doesn't matter either.
    MQTT broker       the watchdog itself lost the broker for BROKER_DOWN_MINUTES

While a station is offline its instrument checks are held, so an outage gives ONE alert, not
one per instrument. Every problem gets a message when it starts, a reminder every
repeat_hours while it lasts, and a message when it is over.
"""
from __future__ import annotations

import threading
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone

BROKER_DOWN_MINUTES = 5
_BROKER = ("__broker__",)
_NEVER = float("-inf")


@dataclass
class Check:
    name: str
    any_of: list[str]               # OK while ANY of these parameters keeps arriving
    minutes: float = 10
    hint: str = ""
    needs: str = ""                 # name of an earlier check; held while that one is failing


@dataclass
class Station:
    id: str
    name: str = ""
    offline_minutes: float = 5
    checks: list[Check] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.name} ({self.id})" if self.name and self.name != self.id else self.id


@dataclass
class Message:
    subject: str
    body: str


@dataclass
class _Alarm:
    active: bool = False
    last_good: float = 0.0          # when it was last OK (wall clock: station; data time: checks)
    sent_at: float = 0.0            # wall clock of the last message, for reminders


@dataclass
class _Seen:
    last_rx: float | None = None    # wall clock of the last readings message
    first_ts: float | None = None   # data time of the first message since the watchdog started
    max_ts: float | None = None     # newest data time
    params: dict = field(default_factory=dict)      # parameter -> newest data time with a value
    logger: dict | None = None      # last aqms/<id>/logger message
    rebase: set = field(default_factory=set)        # checks that were held: restart their clock
    since: dict = field(default_factory=dict)       # check -> data time its clock restarted


def parse_ts(value: str) -> float:
    """'2026-11-16T08:15:00Z' -> epoch seconds."""
    return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()


def duration(seconds: float) -> str:
    minutes = max(0, int(round(seconds / 60)))
    if minutes < 120:
        return f"{minutes} min"
    hours, minutes = divmod(minutes, 60)
    if hours < 48:
        return f"{hours} h {minutes:02d} min"
    return f"{hours // 24} days {hours % 24} h"


class Monitor:
    def __init__(self, stations: list[Station], utc_offset_hours: float = 8.0,
                 repeat_hours: float = 6.0, now: float = 0.0):
        self.stations = {s.id: s for s in stations}
        self.tz = timezone(timedelta(hours=utc_offset_hours))
        self.repeat = repeat_hours * 3600
        self._seen = {s.id: _Seen() for s in stations}
        self._alarms: dict[tuple, _Alarm] = {}
        self._lock = threading.Lock()
        self._link_since = now              # broker (re)connected: grace before "no data"
        self._broker_down_since: float | None = None
        self.unknown: set[str] = set()      # station ids seen on MQTT but not configured

    # --- input (MQTT thread) ------------------------------------------------------------
    def on_readings(self, payload: dict, now: float) -> bool:
        sid = payload.get("station_id")
        if sid not in self.stations:
            self.unknown.add(str(sid))
            return False
        ts = parse_ts(payload["ts"])
        with self._lock:
            s = self._seen[sid]
            s.last_rx = now
            if s.first_ts is None:
                s.first_ts = ts
            s.max_ts = ts if s.max_ts is None else max(s.max_ts, ts)
            for p in payload.get("values") or {}:
                if ts > s.params.get(p, _NEVER):
                    s.params[p] = ts
        return True

    def on_logger(self, station_id: str, payload: dict) -> None:
        if station_id in self.stations:
            with self._lock:
                self._seen[station_id].logger = payload

    # --- evaluation (main thread) ---------------------------------------------------------
    def evaluate(self, now: float, broker_connected: bool = True) -> list[Message]:
        with self._lock:
            if not broker_connected:
                if self._broker_down_since is None:
                    self._broker_down_since = now
                down_since = self._broker_down_since
                # without the broker nothing can be said about the stations
                return self._step(_BROKER, now - down_since > BROKER_DOWN_MINUTES * 60, now,
                                  down_since, self._broker_problem, self._broker_ok)
            if self._broker_down_since is not None:
                self._link_since = now
                self._broker_down_since = None
            out = self._step(_BROKER, False, now, now, self._broker_problem, self._broker_ok)
            for st in self.stations.values():
                out += self._station(st, now)
            return out

    def _step(self, key, failing: bool, now: float, last_good: float, problem, ok) -> list[Message]:
        a = self._alarms.setdefault(key, _Alarm())
        if failing:
            if not a.active:
                a.active, a.last_good, a.sent_at = True, last_good, now
                return [problem(a, False)]
            if self.repeat and now - a.sent_at >= self.repeat:
                a.sent_at = now
                return [problem(a, True)]
            return []
        if a.active:
            a.active = False
            return [ok(a)]
        return []

    def _station(self, st: Station, now: float) -> list[Message]:
        s = self._seen[st.id]
        ref = max(s.last_rx or _NEVER, self._link_since)
        offline = now - ref > st.offline_minutes * 60
        out = self._step((st.id,), offline, now, ref,
                         lambda a, again: self._offline_msg(st, s, now, again),
                         lambda a: self._online_msg(st, now - a.last_good))
        if offline or s.max_ts is None:     # hold the instrument checks while offline
            s.rebase.update(c.name for c in st.checks)
            return out
        for chk in st.checks:
            alarm = self._alarms.setdefault((st.id, chk.name), _Alarm())
            if chk.needs and self._alarms.get((st.id, chk.needs), _Alarm()).active:
                s.rebase.add(chk.name)      # its instrument is down: that alert says it all
                continue
            if chk.name in s.rebase:
                # held until now: give it its full time again from here (a value that is
                # briefly blank while an analyzer warms up must not alarm at once), unless
                # it was already failing before the hold
                s.rebase.discard(chk.name)
                if not alarm.active:
                    s.since[chk.name] = s.max_ts
            seen = max(s.params.get(p, _NEVER) for p in chk.any_of)
            good = max(seen, s.first_ts, s.since.get(chk.name, _NEVER))
            out += self._step((st.id, chk.name), s.max_ts - good > chk.minutes * 60, now, good,
                              lambda a, again, chk=chk, seen=seen: self._check_msg(st, chk, s, seen,
                                                                                   again),
                              lambda a, chk=chk: self._check_ok_msg(st, chk, s.max_ts - a.last_good))
        return out

    # --- messages -------------------------------------------------------------------------
    def _t(self, epoch: float) -> str:
        return datetime.fromtimestamp(epoch, self.tz).strftime("%d %b %H:%M")

    @staticmethod
    def _logger_text(s: _Seen) -> str:
        lg = s.logger or {}
        if lg.get("state") == "offline" and lg.get("reason") == "logger stopped":
            return "The logger said goodbye: its service was stopped, or the PC shut down / restarted."
        if lg.get("state") == "offline":
            return ("The logger dropped off without a goodbye: power cut, PC crash, or the "
                    "internet is down.")
        if lg.get("state") == "online":
            return ("The logger is still connected but sends no data: it may be stuck - check "
                    "C:\\aqms\\logger\\logs\\logger.log on the station PC.")
        return ""

    def _offline_msg(self, st: Station, s: _Seen, now: float, again: bool) -> Message:
        if s.last_rx is None:
            last = f"No data since the watchdog started ({self._t(self._link_since)})."
        else:
            last = f"No data for {duration(now - s.last_rx)} (last received {self._t(s.last_rx)})."
        lines = [f"🔴 {'STILL ' if again else ''}OFFLINE: {st.label}", "", last]
        if self._logger_text(s):
            lines.append(self._logger_text(s))
        lines.append("Check the station PC (power, internet) and the AQMS-Logger service.")
        return Message(f"[AQMS] {st.label}: station OFFLINE{' (still)' if again else ''}",
                       "\n".join(lines))

    def _online_msg(self, st: Station, outage: float) -> Message:
        return Message(f"[AQMS] {st.label}: station back online",
                       f"✅ ONLINE again: {st.label}\n\nData is arriving again after "
                       f"{duration(outage)} offline. What the station PC stored meanwhile is "
                       "being sent now.")

    def _check_msg(self, st: Station, chk: Check, s: _Seen, seen: float, again: bool) -> Message:
        if seen == _NEVER:
            last = "No data from it since the watchdog started, while the station itself is online."
        else:
            last = (f"No data for {duration(s.max_ts - seen)} (last value {self._t(seen)}), "
                    "while the station itself is online.")
        lines = [f"🔴 {'STILL: ' if again else ''}{chk.name} - no data", f"Station: {st.label}", "",
                 last, f"Watched values: {', '.join(chk.any_of)}"]
        if chk.hint:
            lines.append(chk.hint)
        return Message(f"[AQMS] {st.label}: {chk.name} - no data{' (still)' if again else ''}",
                       "\n".join(lines))

    def _check_ok_msg(self, st: Station, chk: Check, outage: float) -> Message:
        return Message(f"[AQMS] {st.label}: {chk.name} OK again",
                       f"✅ {chk.name} OK again\nStation: {st.label}\n\n"
                       f"Data is back after {duration(outage)}.")

    def _broker_problem(self, a: _Alarm, again: bool) -> Message:
        return Message("[AQMS] watchdog cannot reach the MQTT broker",
                       f"🔴 {'STILL: ' if again else ''}the watchdog cannot reach the MQTT broker "
                       f"(since {self._t(a.last_good)}).\n\nThe dashboard is probably not "
                       "receiving data either. Check the aqms-mqtt container on the server.")

    def _broker_ok(self, a: _Alarm) -> Message:
        return Message("[AQMS] MQTT broker reachable again",
                       "✅ The watchdog is connected to the MQTT broker again.")

    # --- daily report ---------------------------------------------------------------------
    def summary(self, now: float) -> Message:
        with self._lock:
            lines = [f"AQMS daily check - {datetime.fromtimestamp(now, self.tz):%d %b %Y %H:%M}"]
            problems = 0
            if self._alarms.get(_BROKER, _Alarm()).active:
                problems += 1
                lines += ["", "🔴 the watchdog cannot reach the MQTT broker"]
            for st in self.stations.values():
                s = self._seen[st.id]
                last = "no data yet" if s.last_rx is None else f"last data {self._t(s.last_rx)}"
                active = []
                if self._alarms.get((st.id,), _Alarm()).active:
                    active.append("station OFFLINE")
                active += [c.name for c in st.checks
                           if self._alarms.get((st.id, c.name), _Alarm()).active]
                problems += len(active)
                lines.append("")
                if active:
                    lines.append(f"🔴 {st.label}: {len(active)} problem(s), {last}")
                    lines += [f"   - {name}" for name in active]
                else:
                    lines.append(f"✅ {st.label}: all OK ({len(st.checks)} checks), {last}")
            subject = "all OK" if not problems else f"{problems} problem(s)"
            return Message(f"[AQMS] daily check: {subject}", "\n".join(lines))


class DailyReport:
    """Says when the once-a-day summary is due (local time `at`, e.g. "08:00"; "" = off)."""

    def __init__(self, at, tz: timezone, now: float):
        self.tz = tz
        self.at = self._parse(at)
        local = datetime.fromtimestamp(now, tz)
        # started after today's time: first report tomorrow, not right away
        self.last_day = local.date() if self.at and (local.hour, local.minute) >= self.at else None

    @staticmethod
    def _parse(at) -> tuple[int, int] | None:
        if at in (None, "", False):
            return None
        if isinstance(at, int):              # YAML reads an unquoted 8:00 as 480 (base 60)
            return divmod(at, 60)
        hh, mm = str(at).strip().split(":")
        return int(hh), int(mm)

    def due(self, now: float) -> bool:
        if not self.at:
            return False
        local = datetime.fromtimestamp(now, self.tz)
        if (local.hour, local.minute) >= self.at and local.date() != self.last_day:
            self.last_day = local.date()
            return True
        return False
