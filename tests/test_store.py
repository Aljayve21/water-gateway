"""SQLite buffer behavior tests (offline, temp database)."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from gateway.store import (
    STATE_CONSUMED,
    STATE_NEW,
    STATE_STAGED,
    SQLiteBuffer,
)


@pytest.fixture
def buffer(tmp_path):
    b = SQLiteBuffer(tmp_path / "gateway.db")
    b.initialize()
    yield b
    b.close()


def test_wal_mode(buffer):
    row = buffer._conn.execute("PRAGMA journal_mode").fetchone()
    assert str(row[0]).lower() == "wal"


def test_initialize_is_idempotent(buffer):
    buffer.initialize()
    buffer.initialize()


def test_watermark_roundtrip(buffer):
    assert buffer.get_watermark("k") is None
    assert buffer.init_watermark_if_absent("k", 100) is True
    assert buffer.init_watermark_if_absent("k", 200) is False
    assert buffer.get_watermark("k") == 100
    buffer.save_watermark("k", 250)
    assert buffer.get_watermark("k") == 250


def test_stage_event_dedupes_on_source_key(buffer):
    assert buffer.stage_event("receive_packet", "1", {"a": 1}) is True
    assert buffer.stage_event("receive_packet", "1", {"a": 2}) is False
    pending = buffer.pending_events()
    assert len(pending) == 1
    assert pending[0]["payload"]  # non-empty JSON blob present


def test_state_transition_and_attempts(buffer):
    buffer.stage_event("t", "k", {"v": 1})
    buffer.mark_staged("k")
    buffer.mark_consumed("k")
    row = buffer._conn.execute(
        "SELECT state, attempts FROM stage_events WHERE source_key = 'k'"
    ).fetchone()
    assert row["state"] == STATE_CONSUMED
    assert row["attempts"] == 2


def test_mark_retry_resets_to_new_with_error(buffer):
    buffer.stage_event("t", "k", {"v": 1})
    buffer.mark_retry("k", "boom")
    (pending,) = buffer.pending_events()
    assert pending["attempts"] == 1
    assert pending["error"] == "boom"


def test_prune_removes_only_consumed(buffer):
    old = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat(timespec="seconds")
    buffer.stage_event("t", "a", {"v": 1}, captured_utc=old)
    buffer.stage_event("t", "b", {"v": 1}, captured_utc=old)
    buffer.mark_consumed("a")
    pruned = buffer.prune_consumed(
        (datetime.now(timezone.utc) - timedelta(hours=24)).isoformat(timespec="seconds")
    )
    assert pruned == 1
    assert [p["source_key"] for p in buffer.pending_events()] == ["b"]


def test_snapshot_upsert_replaces_payload(buffer):
    buffer.upsert_snapshot("M1", {"a": 1})
    buffer.upsert_snapshot("M1", {"a": 2})
    assert buffer.snapshot_count() == 1
    got = buffer.get_snapshot("M1")
    assert got["payload"]["a"] == 2


def test_counts_shape(buffer):
    buffer.save_watermark("w", 1)
    buffer.stage_event("t", "k", {"v": 1})
    counts = buffer.counts()
    assert counts["snapshots"] == 0
    assert counts["events"].get(STATE_NEW) == 1
    assert counts["watermarks"].get("w") == 1