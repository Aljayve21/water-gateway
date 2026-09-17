"""Normalizer tests: comm bands, precedence rules, point mapping, persistence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.normalizer import (
    CHANNEL_DEFS,
    Normalizer,
    alarm_active,
    battery_volts,
    derive_comm_status,
    from_office_local,
    last_contact_utc,
    normalize_meter,
    signal_dbm,
)
from gateway.store import SQLiteBuffer


def _snapshot(**overrides):
    base = {"meter_id": "M1", "head": None, "analysis": None, "last_receive": None, "addr": None}
    base.update(overrides)
    return base


# ── time / comm ─────────────────────────────────────────────────────────
def test_from_office_local_converts_utc8_to_utc():
    dt = from_office_local("2026-09-14 08:01:00")
    assert dt is not None
    assert dt.tzinfo is not None
    assert dt.utcoffset().total_seconds() == 0
    assert dt.hour == 0 and dt.minute == 1


def test_from_office_local_none_and_garbage():
    assert from_office_local(None) is None
    assert from_office_local("") is None
    assert from_office_local("not-a-date") is None


def test_comm_status_bands():
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    on_time = now - timedelta(hours=24)
    watch = now - timedelta(hours=48)
    lagging = now - timedelta(hours=48, minutes=1)
    assert derive_comm_status(on_time, now) == "on-time"
    assert derive_comm_status(now - timedelta(minutes=5), now) == "on-time"
    assert derive_comm_status(watch, now) == "watch"
    assert derive_comm_status(lagging, now) == "lagging"
    assert derive_comm_status(now - timedelta(hours=30), now) == "watch"
    assert derive_comm_status(None, now) == "lagging"


def test_comm_status_respects_configured_bands():
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    last = now - timedelta(hours=10)
    assert derive_comm_status(last, now, online_hours=12, watch_hours=24) == "on-time"
    assert derive_comm_status(last, now, online_hours=8, watch_hours=24) == "watch"


# ── value precedence ────────────────────────────────────────────────────
def test_last_contact_precedence():
    snap = {
        "head": {"receive_time": "2026-09-14 07:00:00"},
        "analysis": {"receive_time": "2026-09-14 06:00:00", "rec_date_time": "2026-09-14 05:00:00"},
    }
    assert last_contact_utc(snap).hour == 23  # 07:00 +08 -> 23:00 prev day UTC


def test_battery_precedence():
    assert battery_volts({"head": {"bat": 3.6}, "analysis": {"bat": 3.3}}) == 3.6
    assert battery_volts({"head": None, "analysis": {"bat": 3.3}}) == 3.3
    assert battery_volts({"head": None, "analysis": {"battery": 3.2}}) == 3.2
    assert battery_volts({"head": None, "analysis": {}}) is None


def test_signal_precedence_uses_negative_rsrp():
    assert signal_dbm({"head": {"rsrp": -80, "rssi": -70}, "analysis": {}}) == -80
    # zero/positive rsrp means "not reported" -> falls back to rssi
    assert signal_dbm({"head": {"rsrp": 0, "rssi": -70}, "analysis": {}}) == -70
    assert signal_dbm({"head": {"rsrp": -80, "rssi": -70}, "analysis": {"rssi": -60}}) == -80
    assert signal_dbm({"head": {"rsrp": None, "rssi": None}, "analysis": {"rssi": -60}}) == -60
    assert signal_dbm({"head": None, "analysis": {"rssi": None}}) is None


def test_alarm_active_draft_rule():
    assert alarm_active({"analysis": {"alarm": "FLOW"}}) == 1
    assert alarm_active({"analysis": {"alarm": ""}}) == 0
    assert alarm_active({"analysis": {"alarm": None}}) == 0
    assert alarm_active({"analysis": {}}) == 0


# ── normalize_meter ─────────────────────────────────────────────────────
def test_normalize_meter_maps_channels_with_units():
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    snap = _snapshot(
        analysis={
            "meter_id": "M1",
            "amount": 1000.5,
            "pressure": 2.75,
            "r_amount": 3.0,
            "sensor1_f": 10.0,
            "sensor2_f": 20.0,
            "rec_date_time": "2026-09-14 11:00:00",
        },
        head={"receive_time": "2026-09-14 11:30:00", "bat": 3.6, "rsrp": -80},
    )
    meter = normalize_meter(snap, now=now)
    assert meter.comm_status == "on-time"
    assert meter.points["totalizer"].value == 1000.5
    assert meter.points["totalizer"].unit == "m3"
    assert meter.points["pressure"].unit == "kg/cm2"
    assert meter.points["reverse"].unit == "m3"
    assert meter.points["pulse1"].unit == "L"
    assert meter.points["pulse2"].unit == "L"
    assert meter.points["battery"].value == 3.6
    assert meter.points["signal"].value == -80
    assert meter.points["online"].value == 1
    assert set(CHANNEL_DEFS) <= set(meter.points)


def test_normalize_meter_offline_flags_online_zero():
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    snap = _snapshot(head={"receive_time": "2026-09-12 00:00:00"})  # >48h prior
    meter = normalize_meter(snap, now=now)
    assert meter.comm_status == "lagging"
    assert meter.points["online"].value == 0


def test_normalize_meter_skips_missing_channels():
    snap = _snapshot(analysis={"amount": 5.0})
    meter = normalize_meter(snap)
    assert "totalizer" in meter.points
    assert "pressure" not in meter.points
    assert meter.points["online"].value in (0, 1)


def test_normalize_meter_requires_meter_id():
    with pytest.raises(ValueError):
        normalize_meter({"meter_id": ""})


# ── Normalizer.run + buffer persistence ─────────────────────────────────
@pytest.fixture
def buffer(tmp_path):
    b = SQLiteBuffer(tmp_path / "g.db")
    b.initialize()
    yield b
    b.close()


def test_normalizer_persists_points_and_consumes_events(buffer):
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    buffer.upsert_snapshot(
        "M1",
        {
            "meter_id": "M1",
            "head": {"receive_time": "2026-09-14 11:00:00", "bat": 3.6, "rsrp": -80},
            "analysis": {"meter_id": "M1", "amount": 100.0, "pressure": 2.5, "rec_date_time": "2026-09-14 11:00:00"},
            "last_receive": None,
            "addr": None,
        },
    )
    buffer.stage_event("receive_packet", "55", {"id": 55, "meter_id": "M1"})

    result = Normalizer().run(buffer, now=now)
    assert result.meters == 1
    assert result.events_consumed == 1
    assert result.point_values >= 5  # totalizer+pressure+battery+signal+online+alarm

    values = buffer.get_normalized("M1")
    assert values["totalizer"]["value"] == 100.0
    assert values["totalizer"]["unit"] == "m3"
    assert values["battery"]["value"] == 3.6
    assert values["online"]["value"] == 1
    assert buffer.pending_events() == []


def test_normalizer_replays_events_after_failure(buffer):
    buffer._conn.execute(
        "INSERT INTO stage_events(source_table, source_key, payload, captured_utc, state)"
        " VALUES (?, ?, ?, ?, ?)",
        ("receive_packet", "1", "{not json}", "2026-09-14T00:00:00Z", "new"),
    )
    result = Normalizer().run(buffer)
    assert result.events_failed == 1
    pending = buffer.pending_events()
    assert len(pending) == 1
    assert pending[0]["attempts"] == 1
    assert pending[0]["error"]


def test_normalizer_is_idempotent_over_snapshots(buffer):
    now = datetime(2026, 9, 14, 12, 0, 0, tzinfo=timezone.utc)
    snap = {
        "meter_id": "M1",
        "head": {"receive_time": "2026-09-14 11:00:00", "bat": 3.6},
        "analysis": {"meter_id": "M1", "amount": 100.0, "rec_date_time": "2026-09-14 11:00:00"},
        "last_receive": None,
        "addr": None,
    }
    buffer.upsert_snapshot("M1", snap)
    Normalizer().run(buffer, now=now)
    count_after_first = buffer.normalized_count()
    Normalizer().run(buffer, now=now)
    assert buffer.normalized_count() == count_after_first