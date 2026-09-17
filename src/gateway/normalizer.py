"""Normalizer: snapshot payloads -> canonical point values.

Purely functional core that mirrors the confirmed EMS logic
(ems-backend telemetry.service.ts + the frontend diagnostics thresholds):

  * comm status      - on-time (<=24h), watch (<=48h), else lagging; null last
                       contact is lagging.
  * last contact     - head.receive_time ?? analysis.receive_time ??
                       analysis.rec_date_time, office-local (UTC+8) -> UTC.
  * battery volts    - head.bat ?? analysis.bat ?? analysis.battery.
  * signal dBm       - head.rsrp (<0) ?? head.rssi ?? analysis.rssi.
  * channels         - amount -> totalizer (m3), pressure -> pressure (kg/cm2),
                       r_amount -> reverse (m3), sensor1_f -> pulse1 (L),
                       sensor2_f -> pulse2 (L).
  * online (binary)  - 1 unless comm status is lagging.
  * alarm (binary)   - [DRAFT] 1 when the latest analysis carries an alarm
                       string; exact actionable-codes rule is [OWNER].

Output is persisted to the local buffer's ``normalized_value`` table (never to
the office database) and consumed by the Mapping Engine in the next phase.
"""

from __future__ import annotations

import json
import math
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional

from .store import SQLiteBuffer

OFFICE_TZ = timezone(timedelta(hours=8))

# channel id -> (source column, unit) — mirrors telemetry.service.ts CHANNEL_DEFS
CHANNEL_DEFS: dict[str, tuple[str, str]] = {
    "totalizer": ("amount", "m3"),
    "pressure": ("pressure", "kg/cm2"),
    "reverse": ("r_amount", "m3"),
    "pulse1": ("sensor1_f", "L"),
    "pulse2": ("sensor2_f", "L"),
}


def from_office_local(value: Optional[Any]) -> Optional[datetime]:
    """Parse an office-local naive DATETIME string as UTC+8, return aware UTC."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        naive = value
    else:
        text = str(value)
        try:
            naive = datetime.strptime(text, "%Y-%m-%d %H:%M:%S")
        except ValueError:
            try:
                naive = datetime.fromisoformat(text)
            except ValueError:
                return None
    if naive.tzinfo is not None:
        return naive.astimezone(timezone.utc)
    return naive.replace(tzinfo=OFFICE_TZ).astimezone(timezone.utc)


def derive_comm_status(
    last: Optional[datetime],
    now: Optional[datetime] = None,
    online_hours: float = 24.0,
    watch_hours: float = 48.0,
) -> str:
    """on-time / watch / lagging — mirror of ems-backend deriveCommStatus."""
    if last is None:
        return "lagging"
    now = now or datetime.now(timezone.utc)
    mins = max(0.0, (now - last).total_seconds() / 60.0)
    if mins <= online_hours * 60:
        return "on-time"
    if mins <= watch_hours * 60:
        return "watch"
    return "lagging"


def last_contact_utc(snapshot: dict) -> Optional[datetime]:
    head = snapshot.get("head") or {}
    analysis = snapshot.get("analysis") or {}
    raw = (
        head.get("receive_time")
        or analysis.get("receive_time")
        or analysis.get("rec_date_time")
    )
    return from_office_local(raw)


def battery_volts(snapshot: dict) -> Optional[float]:
    head = snapshot.get("head") or {}
    analysis = snapshot.get("analysis") or {}
    raw = head.get("bat")
    if raw is None:
        raw = analysis.get("bat")
    if raw is None:
        raw = analysis.get("battery")
    return None if raw is None else float(raw)


def signal_dbm(snapshot: dict) -> Optional[float]:
    head = snapshot.get("head") or {}
    analysis = snapshot.get("analysis") or {}
    rsrp = head.get("rsrp")
    if rsrp is not None and float(rsrp) < 0:
        return float(rsrp)
    raw = head.get("rssi")
    if raw is None:
        raw = analysis.get("rssi")
    return None if raw is None else float(raw)


def _channel_value(analysis: Optional[dict], column: str) -> Optional[float]:
    if not analysis:
        return None
    raw = analysis.get(column)
    return None if raw is None else float(raw)


def alarm_active(snapshot: dict) -> int:
    """[DRAFT] alarm active when the latest analysis carries an alarm string."""
    analysis = snapshot.get("analysis") or {}
    raw = analysis.get("alarm")
    active = raw not in (None, "", 0, "0", False)
    return 1 if active else 0


@dataclass
class NormalizedPoint:
    value: Any
    unit: str = ""


@dataclass
class NormalizedMeter:
    meter_id: str
    comm_status: str
    last_contact_utc: Optional[datetime]
    points: dict[str, NormalizedPoint] = field(default_factory=dict)


def normalize_meter(
    snapshot: dict,
    online_hours: float = 24.0,
    watch_hours: float = 48.0,
    now: Optional[datetime] = None,
) -> NormalizedMeter:
    meter_id = snapshot.get("meter_id")
    if not meter_id:
        raise ValueError("snapshot missing meter_id")
    analysis = snapshot.get("analysis") or {}

    last = last_contact_utc(snapshot)
    comm = derive_comm_status(last, now, online_hours, watch_hours)

    points: dict[str, NormalizedPoint] = {}
    for point_id, (column, unit) in CHANNEL_DEFS.items():
        value = _channel_value(analysis, column)
        if value is None:
            continue
        points[point_id] = NormalizedPoint(value=value, unit=unit)

    volts = battery_volts(snapshot)
    if volts is not None:
        points["battery"] = NormalizedPoint(value=volts, unit="V")

    dbm = signal_dbm(snapshot)
    if dbm is not None:
        points["signal"] = NormalizedPoint(value=dbm, unit="dBm")

    points["online"] = NormalizedPoint(value=0 if comm == "lagging" else 1, unit="binary")
    points["alarm"] = NormalizedPoint(value=alarm_active(snapshot), unit="binary")

    return NormalizedMeter(
        meter_id=meter_id,
        comm_status=comm,
        last_contact_utc=last,
        points=points,
    )


@dataclass
class NormalizeResult:
    meters: int
    point_values: int
    events_consumed: int
    events_failed: int
    elapsed_s: float


class Normalizer:
    """Consumes buffer state: snapshots + staged events -> normalized_value."""

    def __init__(self, online_hours: float = 24.0, watch_hours: float = 48.0) -> None:
        self.online_hours = online_hours
        self.watch_hours = watch_hours

    def run(
        self,
        buffer: SQLiteBuffer,
        now: Optional[datetime] = None,
    ) -> NormalizeResult:
        start = time.monotonic()

        meters = 0
        point_values = 0
        for snap in buffer.all_snapshots():
            try:
                normalized = normalize_meter(
                    snap["payload"],
                    online_hours=self.online_hours,
                    watch_hours=self.watch_hours,
                    now=now,
                )
            except (ValueError, TypeError, KeyError) as exc:
                continue
            if normalized is None:
                continue
            meters += 1
            for point_id, point in normalized.points.items():
                if point.value is None:
                    continue
                value = float(point.value)
                if not math.isfinite(value):
                    continue
                buffer.upsert_normalized(
                    normalized.meter_id, point_id, value, point.unit
                )
                point_values += 1

        events_consumed = 0
        events_failed = 0
        for event in buffer.pending_events():
            try:
                json.loads(event["payload"])
                buffer.mark_consumed(event["source_key"])
                events_consumed += 1
            except Exception as exc:  # noqa: BLE001 - per-event resilience
                buffer.mark_retry(event["source_key"], str(exc))
                events_failed += 1

        return NormalizeResult(
            meters=meters,
            point_values=point_values,
            events_consumed=events_consumed,
            events_failed=events_failed,
            elapsed_s=round(time.monotonic() - start, 3),
        )