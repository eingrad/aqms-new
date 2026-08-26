"""Ingest configuration loader (YAML + ${VAR} from environment/.env)."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass

import yaml
from dotenv import load_dotenv

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(value):
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise KeyError(f"config references ${{{var}}} but it is not set in the environment/.env")
            return os.environ[var]
        return _VAR_RE.sub(repl, value)
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    return value


@dataclass
class MqttConfig:
    host: str
    port: int = 8883
    username: str = ""
    password: str = ""
    tls_ca: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    topic: str = "aqms/+/readings"
    qos: int = 1
    client_id: str = "aqms-ingest"


@dataclass
class IngestConfig:
    dsn: str
    mqtt: MqttConfig


def load_config(path: str) -> IngestConfig:
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(path)) or ".", ".env"))
    with open(path, "r", encoding="utf-8") as fh:
        raw = _resolve(yaml.safe_load(fh))
    return IngestConfig(dsn=raw["timescaledb"]["dsn"], mqtt=MqttConfig(**raw["mqtt"]))
