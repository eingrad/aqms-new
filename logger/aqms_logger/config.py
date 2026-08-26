"""Load and validate logger configuration from YAML, resolving ${VAR} from the environment/.env."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field

import yaml
from dotenv import load_dotenv

_VAR_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")


def _resolve(value):
    """Recursively replace ${VAR} placeholders in strings using os.environ."""
    if isinstance(value, str):
        def repl(m: re.Match) -> str:
            var = m.group(1)
            if var not in os.environ:
                raise KeyError(f"config references ${{{var}}} but it is not set in the environment/.env")
            return os.environ[var]
        return _VAR_RE.sub(repl, value)
    if isinstance(value, list):
        return [_resolve(v) for v in value]
    if isinstance(value, dict):
        return {k: _resolve(v) for k, v in value.items()}
    return value


@dataclass
class RegisterSpec:
    parameter: str
    address: int
    count: int = 2
    type: str = "float32"
    function: str = "input"          # input | holding
    word_order: str | None = None    # falls back to the device default
    scale: float = 1.0
    offset: float = 0.0
    unit: str = ""


@dataclass
class DeviceConfig:
    name: str
    type: str                        # modbus_tcp | fpi_mysql | hj212_receiver
    host: str = ""
    port: int = 502
    unit_id: int = 1
    timeout: float = 2.0
    retries: int = 3
    word_order: str = "ABCD"         # device default for 32-bit values
    registers: list[RegisterSpec] = field(default_factory=list)
    options: dict = field(default_factory=dict)   # adapter-specific (e.g. fpi_mysql)


@dataclass
class MqttConfig:
    host: str
    port: int = 8883
    username: str = ""
    password: str = ""
    tls_ca: str = ""
    tls_cert: str = ""
    tls_key: str = ""
    base_topic: str = "aqms"
    qos: int = 1
    client_id: str = "aqms-logger"


@dataclass
class LoggerConfig:
    station_id: str
    mqtt: MqttConfig
    devices: list[DeviceConfig]
    poll_interval_seconds: int = 60
    sqlite_path: str = "aqms_logger.db"
    retention_days: int = 30

    @property
    def topic(self) -> str:
        return f"{self.mqtt.base_topic}/{self.station_id}/readings"


def load_config(path: str) -> LoggerConfig:
    """Read YAML at `path`, load a sibling .env if present, resolve ${VAR}, and build dataclasses."""
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(path)) or ".", ".env"))
    with open(path, "r", encoding="utf-8") as fh:
        raw = yaml.safe_load(fh)
    raw = _resolve(raw)

    mqtt = MqttConfig(**raw["mqtt"])

    devices: list[DeviceConfig] = []
    for d in raw.get("devices", []):
        regs = [RegisterSpec(**r) for r in d.pop("registers", [])]
        devices.append(DeviceConfig(registers=regs, **d))

    return LoggerConfig(
        station_id=raw["station_id"],
        mqtt=mqtt,
        devices=devices,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        sqlite_path=raw.get("sqlite_path", "aqms_logger.db"),
        retention_days=int(raw.get("retention_days", 30)),
    )
