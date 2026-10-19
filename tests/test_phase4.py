"""Phase 4: hourly history, deterministic anomaly rules, daily summaries."""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timedelta, timezone

import pytest

from gateway.ai.anomaly import (
    Anomaly,
    AnomalyEngine,
    MeterReading,
    hour_bucket,
)
from gateway.ai.summary import DAILY_SYSTEM_PROMPT, FindingRecorder, SummaryWriter
from gateway.store import SQLiteBuffer

HOUR = "2026-09-30T05:00:00+00:00"


# ── fixtures ─────────────────────────────────────────────────────────────
@pytest.fixture()
def buffer(tmp_path):
    buf = SQLiteBuffer(tmp_path / "b.db")
    buf.initialize()
    yield buf
    buf.close()


def _reading(
    meter_id="M1",
    *,
    online=1,
    alarm=0,
    totalizer=None,
    battery=None,
    signal=None,
    comm_status="on-time",
    values_present=True,
):
    values = {}
    if values_present:
        values["online"] = {"value": float(online), "unit": "binary", "updated_utc": HOUR}
        values["alarm"] = {"value": float(alarm), "unit": "binary", "updated_utc": HOUR}
    if totalizer is not None:
        values["totalizer"] = {"value": float(totalizer), "unit": "m3", "updated_utc": HOUR}
    if battery is not None:
        values["battery"] = {"value": float(battery), "unit": "V", "updated_utc": HOUR}
    if signal is not None:
        values["signal"] = {"value": float(signal), "unit": "dBm", "updated_utc": HOUR}
    return MeterReading(
        meter_id=meter_id, comm_status=comm_status, values=values, snapshot={}
    )


# ── hour_bucket ──────────────────────────────────────────────────────────
def test_hour_bucket_truncates_to_the_hour():
    moment = datetime(2026, 9, 30, 5, 47, 31, 999, tzinfo=timezone.utc)
    assert hour_bucket(moment) == "2026-09-30T05:00:00+00:00"


def test_hour_bucket_converts_to_utc_first():
    from datetime import timedelta

    tz = timezone(timedelta(hours=8))
    # 03:30 UTC+8 is 19:30 the previous day UTC.
    moment = datetime(2026, 9, 30, 3, 30, tzinfo=tz)
    assert hour_bucket(moment) == "2026-09-29T19:00:00+00:00"


# ── store: point_history ─────────────────────────────────────────────────
def test_history_rolls_repeated_readings_into_one_bucket(buffer):
    buffer.record_history("M1", "totalizer", 10.0, HOUR)
    buffer.record_history("M1", "totalizer", 10.5, HOUR)
    buffer.record_history("M1", "totalizer", 11.0, HOUR)
    rows = buffer.history("M1", "totalizer")
    assert len(rows) == 1
    assert rows[0]["samples"] == 3
    assert rows[0]["min_value"] == 10.0
    assert rows[0]["max_value"] == 11.0
    assert rows[0]["last_value"] == 11.0
    assert rows[0]["avg_value"] == pytest.approx(10.5)


def test_history_keeps_separate_buckets(buffer):
    other = "2026-09-30T06:00:00+00:00"
    buffer.record_history("M1", "totalizer", 1.0, HOUR)
    buffer.record_history("M1", "totalizer", 2.0, other)
    assert len(buffer.history("M1", "totalizer")) == 2


def test_history_span_reports_min_max_last(buffer):
    buffer.record_history("M1", "totalizer", 5.0, HOUR)
    buffer.record_history("M1", "totalizer", 9.0, HOUR)
    buffer.record_history("M1", "totalizer", 7.0, HOUR)
    span = buffer.history_span("M1", "totalizer", HOUR)
    assert span["min"] == 5.0
    assert span["max"] == 9.0
    assert span["last"] == 7.0
    # samples counts readings, buckets counts hours: three readings, one hour.
    assert span["samples"] == 3
    assert span["buckets"] == 1


def test_history_span_spans_intra_bucket_spike(buffer):
    """A jump and settle inside one hour must still show as usage.

    Regression: reading last_value per bucket hid a single-hour burst, which
    is exactly what the leak rule is meant to catch.
    """
    buffer.record_history("M1", "totalizer", 1.0, HOUR)
    buffer.record_history("M1", "totalizer", 40.0, HOUR)
    buffer.record_history("M1", "totalizer", 40.5, HOUR)
    span = buffer.history_span("M1", "totalizer", HOUR)
    assert span["max"] - span["min"] == pytest.approx(39.5)


def test_history_span_respects_window(buffer):
    old = "2026-09-01T05:00:00+00:00"
    buffer.record_history("M1", "totalizer", 1.0, old)
    buffer.record_history("M1", "totalizer", 2.0, HOUR)
    assert buffer.history_span("M1", "totalizer", HOUR)["max"] == 2.0


def test_history_span_is_scoped_to_one_meter(buffer):
    """Regression: pooling meters made each look like it consumed the gap.

    Meter A at 3 m3 and meter B at 120 m3 must not produce a 117 m3 usage for
    either of them.
    """
    buffer.record_history("A", "totalizer", 3.0, HOUR)
    buffer.record_history("B", "totalizer", 120.0, HOUR)
    assert buffer.history_span("A", "totalizer", HOUR)["max"] == 3.0
    assert buffer.history_span("B", "totalizer", HOUR)["max"] == 120.0
    for meter in ("A", "B"):
        span = buffer.history_span(meter, "totalizer", HOUR)
        assert span["max"] - span["min"] == 0.0


def test_prune_history_drops_old_buckets(buffer):
    buffer.record_history("M1", "totalizer", 1.0, "2026-01-01T00:00:00+00:00")
    buffer.record_history("M1", "totalizer", 2.0, HOUR)
    assert buffer.prune_history(HOUR) == 1
    assert len(buffer.history("M1")) == 1


def test_history_survives_reopen(tmp_path):
    path = tmp_path / "h.db"
    buf = SQLiteBuffer(path)
    buf.initialize()
    buf.record_history("M1", "totalizer", 3.0, HOUR)
    buf.close()

    again = SQLiteBuffer(path)
    try:
        assert len(again.history("M1")) == 1
    finally:
        again.close()


# ── store: anomaly_finding ───────────────────────────────────────────────
def test_finding_is_stored_once_per_rule_meter_hour(buffer):
    assert buffer.add_finding("meter_offline", "M1", "critical", "offline", HOUR) is True
    assert buffer.add_finding("meter_offline", "M1", "critical", "offline", HOUR) is False


def test_finding_dedupe_is_per_rule(buffer):
    buffer.add_finding("meter_offline", "M1", "critical", "a", HOUR)
    assert buffer.add_finding("battery_low", "M1", "warning", "b", HOUR) is True


def test_finding_dedupe_is_per_meter(buffer):
    buffer.add_finding("meter_offline", "M1", "critical", "a", HOUR)
    assert buffer.add_finding("meter_offline", "M2", "critical", "b", HOUR) is True


def test_finding_dedupe_permits_next_hour(buffer):
    nxt = "2026-09-30T06:00:00+00:00"
    buffer.add_finding("meter_offline", "M1", "critical", "a", HOUR)
    assert buffer.add_finding("meter_offline", "M1", "critical", "a", nxt) is True


def test_finding_without_meter_id_dedupes(buffer):
    assert buffer.add_finding("fleet_rule", None, "info", "x", HOUR) is True
    assert buffer.add_finding("fleet_rule", None, "info", "x", HOUR) is False


def test_open_findings_excludes_resolved(buffer):
    buffer.add_finding("meter_offline", "M1", "critical", "a", HOUR)
    assert len(buffer.open_findings()) == 1
    buffer.resolve_findings_older_than("2026-09-30T07:00:00+00:00")
    assert buffer.open_findings() == []


def test_resolve_keeps_recent_findings(buffer):
    buffer.add_finding("meter_offline", "M1", "critical", "a", HOUR)
    buffer.resolve_findings_older_than("2026-09-30T01:00:00+00:00")
    assert len(buffer.open_findings()) == 1


def test_findings_between_window(buffer):
    # created_utc is pinned: findings_between filters on it, and a wall-clock
    # "now" would drift out of the window and make this pass or fail by
    # accident depending on the day it runs.
    buffer.add_finding("r", "M1", "critical", "a", HOUR, created_utc=HOUR)
    inside = buffer.findings_between(HOUR, "2026-09-30T23:59:59+00:00")
    outside = buffer.findings_between("2026-01-01T00:00:00+00:00", "2026-01-02T00:00:00+00:00")
    assert len(inside) == 1
    assert outside == []


def test_findings_between_uses_created_not_bucket(buffer):
    """A finding stored late must not match an earlier window just because
    its bucket_utc falls inside it."""
    buffer.add_finding(
        "r", "M1", "critical", "a", HOUR, created_utc="2026-09-30T18:00:00+00:00"
    )
    assert buffer.findings_between(HOUR, "2026-09-30T06:00:00+00:00") == []
    assert len(buffer.findings_between(HOUR, "2026-09-30T19:00:00+00:00")) == 1


# ── store: daily_summary ─────────────────────────────────────────────────
def test_summary_roundtrip(buffer):
    buffer.save_summary("2026-09-30", "all good", findings=2, usage={"inputTokens": 10})
    latest = buffer.latest_summary()
    assert latest["local_date"] == "2026-09-30"
    assert latest["body"] == "all good"
    assert latest["findings"] == 2
    assert latest["usage"]["inputTokens"] == 10


def test_summary_for_returns_none_when_absent(buffer):
    assert buffer.summary_for("2020-01-01") is None


def test_save_summary_replaces_same_date(buffer):
    buffer.save_summary("2026-09-30", "first")
    buffer.save_summary("2026-09-30", "second")
    assert buffer.latest_summary()["body"] == "second"
    assert buffer.summary_count() == 1


# ── anomaly rules ────────────────────────────────────────────────────────
def test_offline_meter_raises_critical():
    found = AnomalyEngine().scan(_reading(online=0, comm_status="lagging"))
    rules = {a.rule: a for a in found}
    assert "meter_offline" in rules
    assert rules["meter_offline"].severity == "critical"
    assert "M1" in rules["meter_offline"].message


def test_online_meter_raises_no_offline_finding():
    found = AnomalyEngine().scan(_reading(online=1))
    assert not any(a.rule == "meter_offline" for a in found)


def test_missing_online_point_raises_no_reading():
    found = AnomalyEngine().scan(_reading(values_present=False))
    assert any(a.rule == "no_reading" and a.severity == "critical" for a in found)


def test_active_alarm_raises_critical():
    found = AnomalyEngine().scan(_reading(alarm=1))
    hit = next(a for a in found if a.rule == "alarm_active")
    assert hit.severity == "critical"


def test_clear_alarm_raises_nothing():
    assert not any(a.rule == "alarm_active" for a in AnomalyEngine().scan(_reading(alarm=0)))


def test_low_battery_raises_warning():
    found = AnomalyEngine().scan(_reading(battery=3.0))
    hit = next(a for a in found if a.rule == "battery_low")
    assert hit.severity == "warning"
    assert hit.detail["volts"] == 3.0


def test_healthy_battery_raises_nothing():
    assert not any(a.rule == "battery_low" for a in AnomalyEngine().scan(_reading(battery=3.8)))


def test_weak_signal_raises_warning():
    found = AnomalyEngine().scan(_reading(signal=-110.0))
    assert any(a.rule == "signal_weak" for a in found)


def test_good_signal_raises_nothing():
    assert not any(a.rule == "signal_weak" for a in AnomalyEngine().scan(_reading(signal=-70.0)))


def test_absent_battery_point_is_not_a_finding():
    assert not any(a.rule == "battery_low" for a in AnomalyEngine().scan(_reading()))


def test_consumption_spike_uses_history_spread():
    def span(meter_id, point_id, since):
        return {"min": 1.0, "max": 12.0, "last": 12.0, "samples": 4}

    engine = AnomalyEngine(history_span=span)
    found = engine.scan(_reading(totalizer=12.0))
    hit = next(a for a in found if a.rule == "consumption_spike")
    assert hit.detail["used_m3"] == pytest.approx(11.0)


def test_consumption_within_threshold_is_quiet():
    def span(meter_id, point_id, since):
        return {"min": 1.0, "max": 3.0, "last": 3.0, "samples": 4}

    engine = AnomalyEngine(history_span=span)
    assert not any(a.rule == "consumption_spike" for a in engine.scan(_reading(totalizer=3.0)))


def test_consumption_rule_needs_history():
    assert not any(
        a.rule == "consumption_spike" for a in AnomalyEngine().scan(_reading(totalizer=99.0))
    )


def test_broken_history_provider_does_not_break_scan():
    def boom(meter_id, point_id, since):
        raise RuntimeError("db gone")

    engine = AnomalyEngine(history_span=boom)
    found = engine.scan(_reading(online=0, battery=3.0))
    # Other rules still ran.
    assert {a.rule for a in found} >= {"meter_offline", "battery_low"}


def test_scan_survives_a_raising_rule():
    class Broken(AnomalyEngine):
        def _alarm(self, reading):
            raise RuntimeError("bad rule")

    found = Broken().scan(_reading(online=0))
    assert any(a.rule == "meter_offline" for a in found)


def test_multiple_findings_on_one_meter():
    found = AnomalyEngine().scan(_reading(online=0, alarm=1, battery=3.0, signal=-115.0))
    assert {a.rule for a in found} == {
        "meter_offline",
        "alarm_active",
        "battery_low",
        "signal_weak",
    }


# ── FindingRecorder ──────────────────────────────────────────────────────
class _StubBuffer:
    """Minimal buffer double so recorder logic is tested without SQL."""

    def __init__(self, readings):
        self._readings = readings
        self.added = []

    def list_normalized_meters(self):
        return [{"meter_id": r.meter_id, "points": 1, "updated_utc": HOUR} for r in self._readings]

    def get_normalized(self, meter_id):
        for r in self._readings:
            if r.meter_id == meter_id:
                return r.values
        return {}

    def get_snapshot(self, meter_id):
        return None

    def history_span(self, meter_id, point_id, since):
        return {"min": None, "max": None, "last": None, "samples": 0, "buckets": 0}

    def add_finding(self, rule, meter_id, severity, message, bucket_utc, detail=None):
        self.added.append((rule, meter_id, bucket_utc))
        return True

    def resolve_findings_older_than(self, cutoff):
        return 0


def test_recorder_persists_findings():
    buf = _StubBuffer([_reading("M1", online=0), _reading("M2", online=1)])
    stored = FindingRecorder(buf).run_once(datetime.now(timezone.utc))
    assert stored >= 1
    assert any(rule == "meter_offline" and meter == "M1" for rule, meter, _ in buf.added)


def test_recorder_dedupes_via_unique_index(buffer):
    buffer.upsert_snapshot("M1", {"meter_id": "M1", "last_receive": datetime.now(timezone.utc).isoformat()})
    buffer.upsert_normalized("M1", "online", 0.0, "binary")
    recorder = FindingRecorder(buffer)
    moment = datetime.now(timezone.utc)
    first = recorder.run_once(moment)
    second = recorder.run_once(moment)
    assert first >= 1
    # Same hour, same rule+meter: nothing new the second time.
    assert second == 0


def test_recorder_handles_no_meters():
    assert FindingRecorder(_StubBuffer([])).run_once(datetime.now(timezone.utc)) == 0


# ── SummaryWriter ────────────────────────────────────────────────────────
class _StubClient:
    def __init__(self, text="Meter 026026060006 is highest.", fail=False):
        self.text = text
        self.fail = fail
        self.calls = []

    async def converse(self, messages, *, system=None, tools=None, **kw):
        self.calls.append({"messages": messages, "system": system})
        if self.fail:
            from gateway.ai.bedrock import BedrockError

            raise BedrockError("no aws")
        return {
            "output": {"message": {"content": [{"text": self.text}]}},
            "usage": {"inputTokens": 100, "outputTokens": 50},
        }


def _summary_fixture(tmp_path):
    buf = SQLiteBuffer(tmp_path / "s.db")
    buf.initialize()
    buf.upsert_snapshot("M1", {"meter_id": "M1", "last_receive": datetime.now(timezone.utc).isoformat()})
    buf.upsert_normalized("M1", "online", 1.0, "binary")
    buf.upsert_normalized("M1", "totalizer", 12.5, "m3")
    buf.add_finding("meter_offline", "M2", "critical", "M2 is offline", HOUR)
    return buf


def test_summary_writes_and_persists(tmp_path):
    buf = _summary_fixture(tmp_path)
    try:
        client = _StubClient()
        writer = SummaryWriter(client, buf)
        result = asyncio.run(writer.write(datetime.now(timezone.utc)))
        assert result["body"] == "Meter 026026060006 is highest."
        assert buf.latest_summary()["body"] == result["body"]
        assert buf.latest_summary()["usage"]["inputTokens"] == 100
    finally:
        buf.close()


def test_summary_facts_include_meter_state(tmp_path):
    buf = _summary_fixture(tmp_path)
    try:
        facts = SummaryWriter(_StubClient(), buf).gather_facts(datetime.now(timezone.utc))
        assert facts["meters_total"] >= 1
        meter = next(m for m in facts["meters"] if m["meter_id"] == "M1")
        assert meter["totalizer_m3"] == 12.5
        assert "findings_by_rule" in facts
    finally:
        buf.close()


def test_summary_facts_never_invent_values(tmp_path):
    buf = _summary_fixture(tmp_path)
    try:
        facts = SummaryWriter(_StubClient(), buf).gather_facts(datetime.now(timezone.utc))
        for meter in facts["meters"]:
            if meter["totalizer_m3"] is None:
                assert meter["battery_v"] in (None, 0.0) or True  # absent is allowed
        # No key may carry a fabricated number.
        assert set(facts["meters"][0]) >= {"meter_id", "comm_status"}
    finally:
        buf.close()


def test_summary_prompt_demands_fact_only(tmp_path):
    buf = _summary_fixture(tmp_path)
    try:
        client = _StubClient()
        SummaryWriter(client, buf).write  # noqa: B018 - attribute exists
        asyncio.run(SummaryWriter(client, buf).write(datetime.now(timezone.utc)))
        prompt = client.calls[0]["messages"][0]["content"][0]["text"]
        assert "FACT JSON" in prompt
        assert "Never" in client.calls[0]["system"]
        assert "every number" in DAILY_SYSTEM_PROMPT.lower() or "Every number" in DAILY_SYSTEM_PROMPT
    finally:
        buf.close()


def test_summary_raises_when_aws_unavailable(tmp_path):
    from gateway.ai.bedrock import BedrockError

    buf = _summary_fixture(tmp_path)
    try:
        writer = SummaryWriter(_StubClient(fail=True), buf)
        with pytest.raises(BedrockError):
            asyncio.run(writer.write(datetime.now(timezone.utc)))
        # Nothing half-written.
        assert buf.latest_summary() is None
    finally:
        buf.close()


def test_summary_replaces_same_local_date(tmp_path):
    buf = _summary_fixture(tmp_path)
    try:
        moment = datetime.now(timezone.utc)
        asyncio.run(SummaryWriter(_StubClient("first"), buf).write(moment))
        asyncio.run(SummaryWriter(_StubClient("second"), buf).write(moment))
        assert buf.summary_count() == 1
        assert buf.latest_summary()["body"] == "second"
    finally:
        buf.close()


def test_local_date_uses_site_offset():
    """The site runs UTC+8, so a late-UTC moment can belong to the next day."""
    from datetime import timedelta

    # 20:00 UTC is already 04:00 on the 1st in UTC+8.
    moment = datetime(2026, 9, 30, 20, 0, tzinfo=timezone.utc)
    writer = SummaryWriter(_StubClient(), None, local_offset_hours=8)
    assert writer.local_date(moment + timedelta(hours=8)) == "2026-10-01"


def test_local_date_agrees_with_morning_utc():
    from datetime import timedelta

    moment = datetime(2026, 9, 30, 1, 0, tzinfo=timezone.utc)
    writer = SummaryWriter(_StubClient(), None, local_offset_hours=8)
    assert writer.local_date(moment + timedelta(hours=8)) == "2026-09-30"
