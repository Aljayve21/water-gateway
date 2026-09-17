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
from pydantic import BaseModel, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

# Project root = three levels up from src/gateway/config.py
PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
CONFIG_DIR = PROJECT_ROOT / "config"


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

    def is_placeholder(self) -> bool:
        """True when the MySQL section still holds placeholder values."""
        return self.mysql_password == "change_me"

    def fingerprint(self) -> dict[str, object]:
        """Non-sensitive projection for logs (never includes the password)."""
        return {
            "mysql_host": self.mysql_host,
            "mysql_database": self.mysql_database,
            "mysql_user": self.mysql_user,
            "modbus_bind": f"{self.modbus_bind_host}:{self.modbus_bind_port}",
            "bacnet_bind": f"{self.bacnet_bind_ip}:{self.bacnet_bind_port}",
            "bacnet_device_instance": self.bacnet_device_instance,
            "health_bind": f"{self.health_bind_host}:{self.health_bind_port}",
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
    type: Literal["sqlite"] = "sqlite"
    path: str = "./buffer/gateway.db"
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