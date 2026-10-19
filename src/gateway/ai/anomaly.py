"""Deterministic anomaly detection over meter readings.

Every rule here is a plain comparison against thresholds the operator owns.
Nothing in this module calls Bedrock: a rule hit is a fact about the data, and
mixing in a model would make the alerting unauditable.

Detection is cheap and runs every poll cycle, so the cooldown bucket keeps a
meter that stays offline from producing one finding per cycle.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from ..normalizer import derive_comm_status, last_contact_utc

# Point ids produced by gateway.normalizer.
POINT_ONLINE = "online"
POINT_ALARM = "alarm"
POINT_TOTALIZER = "totalizer"
POINT_BATTERY = "battery"
POINT_SIGNAL = "signal"
POINT_PRESSURE = "pressure"

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

# A battery at or below this is a replacement warning; below half of a fresh
# lithium cell the meter will stop reporting soon.
BATTERY_LOW_VOLTS = 3.4
# dBm: -100 is floor-of-basement weak, -120 is effectively no signal.
SIGNAL_WEAK_DBM = -100.0
# A totalizer that gains more than this in an hour is a burst, not normal use.
LEAK_M3_PER_HOUR = 5.0
# Treat a "no reading" meter as needing attention after this many hours.
STALE_AFTER_HOURS = 6.0


def hour_bucket(moment: datetime) -> str:
    """Truncate to the hour so repeated polls collapse into one bucket."""
    return moment.astimezone(timezone.utc).replace(minute=0, second=0, microsecond=0).isoformat()


@dataclass(frozen=True)
class Anomaly:
    rule: str
    meter_id: Optional[str]
    severity: str
    message: str
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class MeterReading:
    """What the rules need about one meter at one moment."""

    meter_id: str
    comm_status: str
    values: dict[str, dict]
    snapshot: Optional[dict] = None


class AnomalyEngine:
    """Applies threshold rules to the current readings."""

    def __init__(
        self,
        *,
        battery_low_volts: float = BATTERY_LOW_VOLTS,
        signal_weak_dbm: float = SIGNAL_WEAK_DBM,
        leak_m3_per_hour: float = LEAK_M3_PER_HOUR,
        stale_after_hours: float = STALE_AFTER_HOURS,
        history_span=None,
    ) -> None:
        self.battery_low_volts = battery_low_volts
        self.signal_weak_dbm = signal_weak_dbm
        self.leak_m3_per_hour = leak_m3_per_hour
        self.stale_after_hours = stale_after_hours
        # Callable(meter_id, point_id, since_utc) -> {"min","max","last","samples"}.
        self._history_span = history_span

    # ── individual rules ────────────────────────────────────────────────
    def _offline(self, reading: MeterReading) -> Optional[Anomaly]:
        online = reading.values.get(POINT_ONLINE)
        if online is None:
            return Anomaly(
                rule="no_reading",
                meter_id=reading.meter_id,
                severity=SEVERITY_CRITICAL,
                message=f"{reading.meter_id} has no normalized reading at all",
                detail={"comm_status": reading.comm_status},
            )
        if float(online["value"]) >= 1:
            return None
        return Anomaly(
            rule="meter_offline",
            meter_id=reading.meter_id,
            severity=SEVERITY_CRITICAL,
            message=f"{reading.meter_id} is offline (comm status: {reading.comm_status})",
            detail={
                "comm_status": reading.comm_status,
                "last_updated_utc": online.get("updated_utc"),
            },
        )

    def _alarm(self, reading: MeterReading) -> Optional[Anomaly]:
        alarm = reading.values.get(POINT_ALARM)
        if alarm is None or float(alarm["value"]) < 1:
            return None
        return Anomaly(
            rule="alarm_active",
            meter_id=reading.meter_id,
            severity=SEVERITY_CRITICAL,
            message=f"{reading.meter_id} reports an active alarm",
            detail={"point": POINT_ALARM},
        )

    def _battery(self, reading: MeterReading) -> Optional[Anomaly]:
        battery = reading.values.get(POINT_BATTERY)
        if battery is None or battery["value"] is None:
            return None
        volts = float(battery["value"])
        if volts > self.battery_low_volts:
            return None
        return Anomaly(
            rule="battery_low",
            meter_id=reading.meter_id,
            severity=SEVERITY_WARNING,
            message=f"{reading.meter_id} battery is low at {volts:.2f} V",
            detail={"volts": volts, "threshold": self.battery_low_volts},
        )

    def _signal(self, reading: MeterReading) -> Optional[Anomaly]:
        signal = reading.values.get(POINT_SIGNAL)
        if signal is None or signal["value"] is None:
            return None
        dbm = float(signal["value"])
        if dbm > self.signal_weak_dbm:
            return None
        return Anomaly(
            rule="signal_weak",
            meter_id=reading.meter_id,
            severity=SEVERITY_WARNING,
            message=f"{reading.meter_id} signal is weak at {dbm:.1f} dBm",
            detail={"dbm": dbm, "threshold": self.signal_weak_dbm},
        )

    def _consumption_spike(self, reading: MeterReading) -> Optional[Anomaly]:
        """Compare this hour's totalizer spread against the leak threshold.

        A totalizer is monotonic, so the spread inside one bucket is the water
        that passed the meter during that hour. No history means no opinion.
        """
        if self._history_span is None:
            return None
        hour = hour_bucket(datetime.now(timezone.utc))
        try:
            span = self._history_span(reading.meter_id, POINT_TOTALIZER, hour)
        except Exception:  # noqa: BLE001 - detection must never break a poll
            return None
        if not span or not span.get("samples"):
            return None
        lo, hi = span.get("min"), span.get("max")
        if lo is None or hi is None:
            return None
        used = float(hi) - float(lo)
        if used <= self.leak_m3_per_hour:
            return None
        return Anomaly(
            rule="consumption_spike",
            meter_id=reading.meter_id,
            severity=SEVERITY_WARNING,
            message=(
                f"{reading.meter_id} used {used:.1f} m3 in the last hour "
                f"(threshold {self.leak_m3_per_hour:.1f} m3/h)"
            ),
            detail={
                "used_m3": round(used, 3),
                "threshold_m3_per_hour": self.leak_m3_per_hour,
                "hour_utc": hour,
            },
        )

    _RULES = (
        ("offline", _offline),
        ("alarm", _alarm),
        ("battery", _battery),
        ("signal", _signal),
        ("consumption", _consumption_spike),
    )

    def scan(self, reading: MeterReading) -> list[Anomaly]:
        """All rules against one meter. A broken rule cannot hide the others."""
        found: list[Anomaly] = []
        for name, rule in self._RULES:
            try:
                hit = rule(self, reading)
            except Exception:  # noqa: BLE001
                continue
            if hit is not None:
                found.append(hit)
        return found


def readings_from_buffer(buffer, meter_ids: Optional[list[str]] = None) -> list[MeterReading]:
    """Build :class:`MeterReading` objects from the live buffer state."""
    ids = meter_ids if meter_ids is not None else [r["meter_id"] for r in buffer.list_normalized_meters()]
    out: list[MeterReading] = []
    for meter_id in ids:
        snap = buffer.get_snapshot(meter_id)
        payload = (snap or {}).get("payload") or {}
        # comm status is derived from the snapshot's last contact, not stored:
        # only point values are persisted in normalized_value.
        last = last_contact_utc(payload) if payload else None
        status = derive_comm_status(last) if payload else "unknown"
        out.append(
            MeterReading(
                meter_id=meter_id,
                comm_status=status,
                values=buffer.get_normalized(meter_id),
                snapshot=payload,
            )
        )
    return out


def stale_cutoff(now: datetime, hours: float) -> str:
    return (now - timedelta(hours=hours)).astimezone(timezone.utc).isoformat(timespec="seconds")
