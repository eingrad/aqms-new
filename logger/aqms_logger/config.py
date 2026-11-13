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
    average: str = "mean"            # mean | circular (circular for angular params like WDIR)


@dataclass
class DeviceConfig:
    name: str
    type: str                        # modbus_tcp | fpi_tcp | synspec_gc | fpi_mysql | hj212_receiver
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
    transport: str = "tcp"       # tcp | websockets (websockets = MQTT-over-WSS via Cloudflare)
    ws_path: str = "/mqtt"       # websocket path on the broker's WS listener
    tls: bool = False            # force TLS without a custom CA (e.g. Cloudflare edge cert on :443)


@dataclass
class RedisConfig:
    enabled: bool = False
    host: str = "127.0.0.1"
    port: int = 6379
    db: int = 0
    password: str = ""
    key_template: str = "aqms:{station_id}:live"   # {station_id} is substituted
    ttl_seconds: int = 120                          # key expiry = dashboard freshness signal
    fields: dict = field(default_factory=dict)      # optional param -> dashboard field-name map


@dataclass
class LoggerConfig:
    station_id: str
    mqtt: MqttConfig
    devices: list[DeviceConfig]
    poll_interval_seconds: int = 60
    sample_interval_seconds: int = 5               # sub-minute sampling; averaged into the stored minute
    sqlite_path: str = "aqms_logger.db"
    retention_days: int = 30
    redis: RedisConfig = field(default_factory=RedisConfig)

    @property
    def topic(self) -> str:
        return f"{self.mqtt.base_topic}/{self.station_id}/readings"

    @property
    def circular_params(self) -> set[str]:
        return {r.parameter for d in self.devices for r in d.registers if r.average == "circular"}

    @property
    def last_params(self) -> set[str]:
        """Parameters reduced to the last sample of the minute (discrete state codes)."""
        return {r.parameter for d in self.devices for r in d.registers if r.average == "last"}

    @property
    def max_params(self) -> set[str]:
        """Parameters reduced to the max sample of the minute (latching status flags)."""
        return {r.parameter for d in self.devices for r in d.registers if r.average == "max"}


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

    redis_raw = raw.get("redis")
    redis_cfg = RedisConfig(**redis_raw) if redis_raw else RedisConfig()

    return LoggerConfig(
        station_id=raw["station_id"],
        mqtt=mqtt,
        devices=devices,
        poll_interval_seconds=int(raw.get("poll_interval_seconds", 60)),
        sample_interval_seconds=int(raw.get("sample_interval_seconds", 5)),
        sqlite_path=raw.get("sqlite_path", "aqms_logger.db"),
        retention_days=int(raw.get("retention_days", 30)),
        redis=redis_cfg,
    )
