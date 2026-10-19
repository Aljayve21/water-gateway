"""Configuration loading & validation.

Two configuration surfaces, deliberately separated:

  * ``.env``                 - infrastructure + secrets (read-only MySQL
    credentials, protocol server bind addresses, BACnet device instance).
  * ``config/*.yaml``        - operational policy and protocol mapping
    templates (poll cadence, thresholds, retention, logging, base points,
    per-meter protocol descriptors).

Everything is validated through Pydantic at load time, so a malformed YAML
or missing required env var fails fast at startup rather than mid-pipeline.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Literal, Optional

import yaml
from pydantic import BaseModel, Field, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = three levels up from src/gateway/config.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"

# Bind addresses that are reachable only from the machine itself.
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "localhost", "::1"})

# Anything shorter than this is not a meaningful bearer secret; refusing to
# start is far better than exposing an AI endpoint with a guessable token.
_MIN_BEARER_TOKEN_LEN = 32


def _env_or_default(name: str, default: str) -> str:
    return os.environ.get(name, default)


# ─────────────────────────────────────────────────────────────────────────
# .env surface (infrastructure + secrets — never logged, never committed)
# ─────────────────────────────────────────────────────────────────────────
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=str(PROJECT_ROOT / ".env"),
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # Read-only MySQL (office mdpf). The gateway only ever SELECTs.
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "ems_gateway"
    mysql_password: str = "change_me"
    mysql_database: str = "mdpf"

    # Modbus TCP server bind
    modbus_bind_host: str = "127.0.0.1"
    modbus_bind_port: int = 502

    # BACnet/IP server bind
    bacnet_bind_ip: str = "127.0.0.1"
    bacnet_bind_port: int = 47808
    bacnet_device_instance: int = 1001

    # Health monitor HTTP endpoint
    health_bind_host: str = "127.0.0.1"
    health_bind_port: int = 9090

    # Local MySQL buffer (gateway's own state; separate database from both
    # the office mdpf database above and the ems-backend schema).
    buffer_mysql_host: str = "127.0.0.1"
    buffer_mysql_port: int = 3306
    buffer_mysql_user: str = "gateway_buf"
    buffer_mysql_password: str = ""
    buffer_mysql_database: str = "gateway_buffer"

    # EMS identity sync (read-only SELECT on users + consumer_meters in the
    # ems-backend database). Empty host disables the sync: the AI registry
    # then only contains the manual AI_PRINCIPALS entries.
    ems_mysql_host: str = ""
    ems_mysql_port: int = 3306
    ems_mysql_user: str = "gateway_buf"
    ems_mysql_password: str = ""
    ems_mysql_database: str = "mdpf"
    ems_sync_ttl_seconds: float = Field(default=60.0, ge=5)

    # ── AI assistant (Amazon Bedrock) ────────────────────────────────────
    # Disabled unless .env explicitly opts in, so an unconfigured deployment
    # never reaches for AWS or opens a second port.
    ai_enabled: bool = False
    ai_bind_host: str = "127.0.0.1"
    ai_bind_port: int = 9091
    # Legacy single shared token. Still honoured so an existing deployment keeps
    # working, but AI_PRINCIPALS takes precedence when it is non-empty: with
    # several humans on one token nobody can be told apart in the logs and
    # every caller sees every meter.
    ai_bearer_token: str = ""
    # Per-caller registry. Each line is
    #   <subject>:<role>:<comma-separated-meter-ids|*>:<token>
    # where role is ADMIN/TECHNICIAN/USER and "*" means "unrestricted meters"
    # (ADMIN and TECHNICIAN only; a USER line is always scoped, even with "*",
    # which is treated as "no meters" so a typo cannot open the estate).
    # Tokens must be at least 32 characters.
    ai_principals: str = ""
    # Shared secret used to verify signed identity assertions from ems-backend.
    # Leave empty to disable that path entirely (no external caller can
    # authenticate by assertion). Same >= 32 character rule as a token.
    ai_assertion_secret: str = ""
    ai_max_tool_hops: int = Field(default=5, ge=1, le=20)
    ai_timeout_seconds: float = Field(default=30.0, gt=0)
    ai_rate_limit_per_min: int = Field(default=10, ge=1)
    ai_summary_local_time: str = "08:00"
    ai_anomaly_cooldown_min: int = Field(default=60, ge=0)

    # ── oBIX (NeoView / Niagara 4) ─────────────────────────────────────
    # Base URL of the oBIX web service, e.g. http://192.168.0.120:8080/obix
    # Leave empty to keep the oBIX integration disabled; nothing else changes.
    obix_base_url: str = ""
    obix_username: str = ""
    obix_password: str = ""
    # Authentication scheme: "auto" (Basic, fall back to SCRAM on 401),
    # "basic", or "scram" (Niagara 4 web login; requires TLS).
    obix_auth: str = "auto"
    obix_timeout_seconds: float = Field(default=10.0, gt=0)
    # TLS verification stays on by default. Only set false for a self-signed
    # lab certificate you have consciously accepted.
    obix_verify_tls: bool = True
    # Folder that holds the meter points we read, e.g. Ai/Site1/Meters.
    obix_meter_root: str = ""
    # Folder we publish AI results into, e.g. Ai/Site1/AI_Insights.
    obix_insights_root: str = ""
    # Explicit NeoView<->gateway point map (config/obix.yaml). Preferred for a
    # real station whose points are flat rather than one folder per meter.
    obix_point_map: str = ""
    # The oBIX tree is slower than the poll loop, so reads run on their own
    # interval (seconds) instead of every extraction cycle.
    obix_poll_interval_seconds: float = Field(default=60.0, gt=0)
    # Optional separate MySQL database that persists the NeoView oBIX points
    # (catalog + latest values + hourly history). It lives in its own database
    # so it shares no tables with the buffer or the office schema. Host/port/
    # user/password default to the BUFFER_MYSQL_* server; set OBIX_MYSQL_DATABASE
    # to empty to switch the oBIX database off.
    obix_mysql_database: str = "obix"
    obix_mysql_host: str = ""
    obix_mysql_port: int = 0
    obix_mysql_user: str = ""
    obix_mysql_password: str = ""
    # How often to re-read Neoview point facets over oBIX (hours). 0 disables
    # the automatic refresh; the cache written by scripts/probe_facets.py is
    # used at startup either way.
    obix_facets_refresh_hours: float = Field(default=24.0, ge=0)

    # Phase 4 anomaly thresholds. These gate deterministic rules only - no AWS
    # call is made to decide whether a finding fires, so alerting stays
    # auditable and free.
    ai_leak_m3_per_hour: float = Field(default=5.0, gt=0)
    ai_battery_low_volts: float = Field(default=3.4, gt=0)
    ai_signal_weak_dbm: float = Field(default=-100.0, le=0)
    ai_history_retention_days: int = Field(default=90, ge=1)

    # Bedrock credentials. Explicitly passed to boto3: pydantic-settings reads
    # .env but does not export into os.environ, so boto3's ambient lookup
    # would find nothing and fail with empty credentials.
    aws_region: str = "ap-southeast-2"
    bedrock_model_id: str = "amazon.nova-pro-v1:0"
    aws_access_key_id: str = ""
    aws_secret_access_key: str = ""

    @model_validator(mode="after")
    def _check_ai_binding(self) -> "Settings":
        """Refuse to expose the AI port without a real bearer secret."""
        if self.ai_enabled and self.ai_bind_host not in _LOOPBACK_HOSTS:
            if len(self.ai_bearer_token) < _MIN_BEARER_TOKEN_LEN:
                raise ValueError(
                    "AI_BIND_HOST exposes the AI endpoint beyond this machine; "
                    f"AI_BEARER_TOKEN must be at least {_MIN_BEARER_TOKEN_LEN} "
                    f"characters (got {len(self.ai_bearer_token)})"
                )
        return self

    def is_placeholder(self) -> bool:
        """True when the MySQL section still holds placeholder values."""
        return self.mysql_password == "change_me"

    def ai_ready(self) -> bool:
        """True when AI is switched on *and* Bedrock is actually usable.

        A missing key degrades AI to off rather than crashing the gateway:
        the Modbus/BACnet pipeline must never depend on AWS being reachable.
        """
        return bool(
            self.ai_enabled
            and self.bedrock_model_id
            and self.aws_access_key_id
            and self.aws_secret_access_key
        )

    def fingerprint(self) -> dict[str, object]:
        """Non-sensitive projection for logs (never includes passwords/keys)."""
        return {
            "mysql_host": self.mysql_host,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "buffer_database": self.buffer_mysql_database,
            "ems_sync_database": self.ems_mysql_database if self.ems_mysql_host else None,
            "modbus_bind": f"{self.modbus_bind_host}:{self.modbus_bind_port}",
            "bacnet_bind": f"{self.bacnet_bind_ip}:{self.bacnet_bind_port}",
            "bacnet_device_instance": self.bacnet_device_instance,
            "health_bind": f"{self.health_bind_host}:{self.health_bind_port}",
            "ai_enabled": self.ai_enabled,
            "ai_ready": self.ai_ready(),
            "ai_bind": f"{self.ai_bind_host}:{self.ai_bind_port}",
            "aws_region": self.aws_region,
            "bedrock_model_id": self.bedrock_model_id,
        }


# ─────────────────────────────────────────────────────────────────────────
# config/gateway.yaml surface (operational policy)
# ─────────────────────────────────────────────────────────────────────────
class BackoffConfig(BaseModel):
    initial_seconds: float = 1.0
    max_seconds: float = 300.0
    multiplier: float = 2.0


class ExtractionConfig(BaseModel):
    poll_interval_seconds: float = 15.0
    batch_size: int = Field(default=500, ge=1)
    backoff: BackoffConfig = BackoffConfig()


class BufferConfig(BaseModel):
    # The buffer database is created automatically on startup; retention_hours
    # and max_retries apply to the staging pipeline as before.
    type: Literal["mysql"] = "mysql"
    path: str = "./buffer/gateway.db"  # legacy; ignored by the MySQL buffer
    retention_hours: float = 24.0
    max_retries: int = Field(default=3, ge=0)


class StalenessConfig(BaseModel):
    online_hours: float = 24.0
    watch_hours: float = 48.0


class QualityConfig(BaseModel):
    battery_critical_volts: float = 3.15
    battery_warning_volts: float = 3.35
    signal_critical_dbm: float = -100.0
    signal_warning_dbm: float = -90.0


class LoggingConfig(BaseModel):
    level: str = "INFO"
    dir: str = "./logs"
    max_bytes: int = 10 * 1024 * 1024
    backup_count: int = 5


class GatewayConfig(BaseModel):
    extraction: ExtractionConfig = ExtractionConfig()
    buffer: BufferConfig = BufferConfig()
    staleness: StalenessConfig = StalenessConfig()
    quality: QualityConfig = QualityConfig()
    logging: LoggingConfig = LoggingConfig()


# ─────────────────────────────────────────────────────────────────────────
# config/base_points.yaml surface (canonical point vocabulary)
# ─────────────────────────────────────────────────────────────────────────
class BasePoint(BaseModel):
    id: str
    source_table: Optional[str] = None
    source_column: Optional[str] = None
    fallback_column: Optional[str] = None
    unit: Optional[str] = None
    kind: Literal["analog", "binary"] = "analog"
    description: str = ""
    confirmed: bool = False


class BasePointsConfig(BaseModel):
    points: dict[str, BasePoint]


# ─────────────────────────────────────────────────────────────────────────
# config/meters.yaml surface (protocol mapping templates)
# ─────────────────────────────────────────────────────────────────────────
_MODBUS_DTYPES = Literal["bool", "int16", "uint16", "int32", "uint32", "float32"]
_BACNET_OBJECTS = Literal["AI", "BI", "AV", "BV"]


class ModbusPointMap(BaseModel):
    register_type: Literal["holding", "input", "coil"]
    address: int = Field(ge=0)
    dtype: _MODBUS_DTYPES = "float32"
    scale: float = 1.0
    byte_order: Literal["big", "little"] = "big"
    word_order: Literal["big", "little"] = "big"
    rw: Literal["R", "RW"] = "R"


class BacnetPointMap(BaseModel):
    object: _BACNET_OBJECTS = "AI"
    instance: int = Field(ge=0)
    units: str = ""


class PointMapping(BaseModel):
    modbus: Optional[ModbusPointMap] = None
    bacnet: Optional[BacnetPointMap] = None


class MeterConfig(BaseModel):
    meter_id: str
    modbus_unit_id: int = Field(ge=1, le=247)
    bacnet_device: int = 1
    points: dict[str, PointMapping] = {}


class MetersConfig(BaseModel):
    meters: dict[str, MeterConfig]


# ─────────────────────────────────────────────────────────────────────────
# Loaders
# ─────────────────────────────────────────────────────────────────────────
def _read_yaml(path: Path) -> dict:
    if not path.is_file():
        raise FileNotFoundError(f"Config file not found: {path}")
    with path.open("r", encoding="utf-8") as fh:
        data = yaml.safe_load(fh)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise ValueError(f"Config file must contain a YAML mapping: {path}")
    return data


def load_gateway_config(path: Optional[Path] = None) -> GatewayConfig:
    raw = _read_yaml(path or CONFIG_DIR / "gateway.yaml")
    return GatewayConfig(**raw)


def load_base_points(path: Optional[Path] = None) -> BasePointsConfig:
    raw = _read_yaml(path or CONFIG_DIR / "base_points.yaml")
    points = {
        point_id: BasePoint(id=point_id, **(fields or {}))
        for point_id, fields in (raw.get("points") or {}).items()
    }
    return BasePointsConfig(points=points)


def load_meters(path: Optional[Path] = None) -> MetersConfig:
    raw = _read_yaml(path or CONFIG_DIR / "meters.yaml")
    meters: dict[str, MeterConfig] = {}
    for meter_id, fields in (raw.get("meters") or {}).items():
        merged = dict(fields or {})
        merged.setdefault("meter_id", meter_id)
        meters[meter_id] = MeterConfig(**merged)
    return MetersConfig(meters=meters)


def get_buffer_mysql_settings(settings: Optional[Settings] = None) -> dict:
    """Buffer-database connection settings for gateway.store.MySQLBuffer.

    Prefer the loaded :class:`Settings` (which reads BUFFER_MYSQL_* from .env);
    fall back to raw environment variables so tests can point the buffer at
    the dedicated test database without touching .env. Empty user/database
    values fail fast at connect time rather than silently connecting as the
    wrong identity.
    """
    if settings is not None:
        return {
            "host": settings.buffer_mysql_host,
            "port": settings.buffer_mysql_port,
            "user": settings.buffer_mysql_user,
            "password": settings.buffer_mysql_password,
            "database": settings.buffer_mysql_database,
        }
    return {
        "host": os.environ.get("BUFFER_MYSQL_HOST", ""),
        "port": int(os.environ.get("BUFFER_MYSQL_PORT", "3306")),
        "user": os.environ.get("BUFFER_MYSQL_USER", ""),
        "password": os.environ.get("BUFFER_MYSQL_PASSWORD", ""),
        "database": os.environ.get("BUFFER_MYSQL_DATABASE", ""),
    }


def get_obix_mysql_settings(settings: Optional[Settings] = None) -> dict:
    """oBIX-database connection settings for gateway.obix_store.ObixStore.

    Host/port/user/password fall back to the BUFFER_MYSQL_* server, so a
    deployment only has to set OBIX_MYSQL_DATABASE (default ``obix``) unless the
    oBIX data lives on a different server. An empty database switches the store
    off.
    """
    if settings is None:
        settings = Settings()
    return {
        "host": settings.obix_mysql_host or settings.buffer_mysql_host,
        "port": settings.obix_mysql_port or settings.buffer_mysql_port,
        "user": settings.obix_mysql_user or settings.buffer_mysql_user,
        "password": (
            settings.obix_mysql_password
            if settings.obix_mysql_password
            else settings.buffer_mysql_password
        ),
        "database": settings.obix_mysql_database,
    }


def load_all(
    path: Optional[Path] = None,
) -> tuple[Settings, GatewayConfig, BasePointsConfig, MetersConfig]:
    """Load every config surface. Errors fail fast (raise)."""
    if path is not None:
        settings = Settings()
        gateway_config = load_gateway_config(path / "gateway.yaml")
        base_points = load_base_points(path / "base_points.yaml")
        meters = load_meters(path / "meters.yaml")
        return settings, gateway_config, base_points, meters
    return Settings(), load_gateway_config(), load_base_points(), load_meters()