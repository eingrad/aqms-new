"""Load watchdog.yaml; ${VAR} placeholders come from the environment (server/.env), "" if unset."""
from __future__ import annotations

import os
import re

import yaml

from .monitor import Check, Station
from .notify import Email, Telegram

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(value):
    if isinstance(value, str):
        return _VAR_RE.sub(lambda m: os.environ.get(m.group(1), ""), value)
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    return value


def load(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as fh:
        return _resolve(yaml.safe_load(fh) or {})


def _split(value) -> list[str]:
    """A list, or one comma-separated string -> list of non-empty strings."""
    if value is None:
        return []
    items = value if isinstance(value, list) else str(value).split(",")
    return [str(v).strip() for v in items if str(v).strip()]


def stations_from(raw: dict) -> list[Station]:
    stations = []
    for s in raw.get("stations") or []:
        sid = str(s.get("id") or "").strip()
        if not sid:
            raise ValueError("every station needs an id (AQMS_STATION_ID in server/.env)")
        checks = []
        for c in s.get("checks") or []:
            any_of = _split(c.get("any_of"))
            if not c.get("name") or not any_of:
                raise ValueError(f"station {sid}: every check needs a name and any_of")
            needs = str(c.get("needs") or "").strip()
            if needs and needs not in [k.name for k in checks]:
                raise ValueError(f"station {sid}: check {c['name']!r} needs {needs!r}, which "
                                 "must be the name of a check listed above it")
            checks.append(Check(name=str(c["name"]), any_of=any_of,
                                minutes=float(c.get("minutes", 10)), hint=str(c.get("hint") or ""),
                                needs=needs))
        names = [c.name for c in checks]
        if len(set(names)) != len(names):
            raise ValueError(f"station {sid}: check names must be unique")
        stations.append(Station(id=sid, name=str(s.get("name") or "").strip(),
                                offline_minutes=float(s.get("offline_minutes", 5)), checks=checks))
    if not stations:
        raise ValueError("no stations configured")
    return stations


def channels_from(notify: dict) -> list:
    channels: list = []
    tg = notify.get("telegram") or {}
    token = str(tg.get("bot_token") or "").strip()
    if token:
        channels += [Telegram(token, chat) for chat in _split(tg.get("chat_id"))]
    em = notify.get("email") or {}
    host, to = str(em.get("smtp_host") or "").strip(), _split(em.get("to"))
    if host and to:
        username = str(em.get("username") or "").strip()
        # the dashboard's env names it SMTP_PASS, so lines copied from it work as they are
        password = str(em.get("password") or "") or os.environ.get("SMTP_PASS", "")
        channels.append(Email(host=host, port=int(em.get("smtp_port") or 587),
                              security=str(em.get("security") or "starttls").strip().lower(),
                              username=username, password=password,
                              sender=str(em.get("from") or "").strip() or username, to=to))
    return channels
