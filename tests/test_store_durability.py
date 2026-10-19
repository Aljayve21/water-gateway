"""Durability tests for the MySQL buffer.

The buffer's whole reason to exist is surviving a restart. These tests open a
completely separate connection to the same database to prove rows really
landed on disk rather than only in the writing process's view.
"""

from __future__ import annotations

from gateway.store import SCHEMA_VERSION, SQLiteBuffer

DATABASE = "gateway_test"


def _reopen(buffer_dsn):
    """A completely separate connection, standing in for a restart."""
    buf = SQLiteBuffer(**buffer_dsn)
    return buf


def test_values_survive_reopen(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    buf.initialize()
    buf.upsert_normalized("WTR-001", "totalizer", 12.5, "m3")
    buf.upsert_normalized("WTR-002", "totalizer", 20.0, "m3")
    buf.close()

    again = _reopen(buffer_dsn)
    try:
        rows = again.list_normalized_meters()
        assert sorted(r["meter_id"] for r in rows) == ["WTR-001", "WTR-002"]
        assert again.get_normalized("WTR-001")["totalizer"]["value"] == 12.5
    finally:
        again.close()


def test_snapshots_and_watermark_survive_reopen(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    buf.initialize()
    buf.upsert_snapshot("WTR-001", {"meter_id": "WTR-001", "analysis": {"amount": 7.0}})
    buf.save_watermark("receive_packet", 4242)
    buf.close()

    again = _reopen(buffer_dsn)
    try:
        assert again.get_watermark("receive_packet") == 4242
        snap = again.get_snapshot("WTR-001")
        assert snap is not None
        assert snap["payload"]["analysis"]["amount"] == 7.0
    finally:
        again.close()


def test_event_lifecycle_survives_reopen(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    buf.initialize()
    buf.stage_event("receive_packet", "key-1", {"amount": 1.0})
    buf.mark_consumed("key-1")
    buf.close()

    again = _reopen(buffer_dsn)
    try:
        assert again.counts()["events"].get("consumed") == 1
        assert again.pending_events() == []
    finally:
        again.close()


def test_buffer_uses_autocommit(buffer_dsn):
    """Guards the regression: deferred transactions silently dropped writes."""
    buf = SQLiteBuffer(**buffer_dsn)
    try:
        assert buf._conn.get_autocommit() is True
    finally:
        buf.close()


def test_schema_version_is_current(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    try:
        buf.initialize()
        with buf._conn.cursor() as cur:
            cur.execute("SELECT value FROM buffer_meta WHERE `key` = 'schema_version'")
            row = cur.fetchone()
        assert int(row["value"]) == SCHEMA_VERSION
    finally:
        buf.close()


def test_initialize_creates_schema_when_database_is_empty(buffer_dsn):
    """On MySQL the database starts empty; initialize must build everything."""
    buf = SQLiteBuffer(**buffer_dsn)
    try:
        buf.initialize()
        with buf._conn.cursor() as cur:
            cur.execute("SHOW TABLES")
            tables = {list(r.values())[0] for r in cur.fetchall()}
        expected = {
            "buffer_meta",
            "watermark",
            "stage_events",
            "meter_snapshot",
            "normalized_value",
            "point_history",
            "anomaly_finding",
            "daily_summary",
        }
        assert expected <= tables
    finally:
        buf.close()


def test_list_normalized_meters_counts_points(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    buf.initialize()
    try:
        buf.upsert_normalized("WTR-001", "totalizer", 1.0, "m3")
        buf.upsert_normalized("WTR-001", "pressure", 2.0, "kg/cm2")
        buf.upsert_normalized("WTR-002", "totalizer", 3.0, "m3")
        rows = buf.list_normalized_meters()
        assert rows[0]["meter_id"] == "WTR-001"
        assert rows[0]["points"] == 2
        assert rows[1]["points"] == 1
    finally:
        buf.close()


def test_list_normalized_meters_empty_when_no_rows(buffer_dsn):
    buf = SQLiteBuffer(**buffer_dsn)
    buf.initialize()
    try:
        assert buf.list_normalized_meters() == []
    finally:
        buf.close()
