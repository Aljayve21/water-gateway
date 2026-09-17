"""Persistent SQLite buffer (local, WAL mode).

Owns all state the gateway must survive restarts with:

  * ``watermark``      - durable per-stream cursor (e.g. ``receive_packet.id``)
  * ``stage_events``   - raw rows pulled by the extractor, awaiting the
    normalizer. Deduped by ``(source_table, source_key)``; state machine
    ``new -> staged -> consumed`` with an attempt counter and error text.
  * ``meter_snapshot`` - latest per-meter raw view (heads + latest analysis
    + last_receive + addr) refreshed each poll.

Concern: the local SQLite buffer is OUR side. The office MySQL database is
never touched by this module.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

# Event lifecycle states
STATE_NEW = "new"
STATE_STAGED = "staged"
STATE_CONSUMED = "consumed"

_SCHEMA_VERSION = 2


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


class SQLiteBuffer:
    """Thread-affine SQLite buffer. Intended for single-threaded use."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        if self.path.parent:
            self.path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(str(self.path), timeout=5.0)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=NORMAL")
        self._conn.execute("PRAGMA busy_timeout=5000")

    # ── schema ────────────────────────────────────────────────────────────
    def initialize(self) -> None:
        cur = self._conn.cursor()
        cur.execute("PRAGMA user_version")
        if cur.fetchone()[0] == _SCHEMA_VERSION:
            return
        cur.executescript(
            """
            BEGIN;
            CREATE TABLE IF NOT EXISTS watermark (
                kind       TEXT PRIMARY KEY,
                value      INTEGER NOT NULL,
                updated_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS stage_events (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                source_table TEXT NOT NULL,
                source_key   TEXT NOT NULL,
                payload      TEXT NOT NULL,
                captured_utc TEXT NOT NULL,
                state        TEXT NOT NULL DEFAULT 'new',
                attempts     INTEGER NOT NULL DEFAULT 0,
                error        TEXT
            );
            CREATE UNIQUE INDEX IF NOT EXISTS ux_stage_events_source
                ON stage_events(source_table, source_key);
            CREATE INDEX IF NOT EXISTS ix_stage_events_state
                ON stage_events(state);
            CREATE TABLE IF NOT EXISTS meter_snapshot (
                meter_id    TEXT PRIMARY KEY,
                payload     TEXT NOT NULL,
                captured_utc TEXT NOT NULL,
                updated_utc TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS normalized_value (
                meter_id    TEXT NOT NULL,
                point_id    TEXT NOT NULL,
                value       REAL,
                unit        TEXT NOT NULL DEFAULT '',
                updated_utc TEXT NOT NULL,
                PRIMARY KEY (meter_id, point_id)
            );
            PRAGMA user_version = 2;
            COMMIT;
            """
        )

    # ── watermark ─────────────────────────────────────────────────────────
    def get_watermark(self, kind: str) -> Optional[int]:
        row = self._conn.execute(
            "SELECT value FROM watermark WHERE kind = ?", (kind,)
        ).fetchone()
        return None if row is None else int(row["value"])

    def save_watermark(self, kind: str, value: int) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO watermark(kind, value, updated_utc) VALUES (?, ?, ?)",
            (kind, value, _utcnow()),
        )

    def init_watermark_if_absent(self, kind: str, value: int) -> bool:
        if self.get_watermark(kind) is not None:
            return False
        self._conn.execute(
            "INSERT OR IGNORE INTO watermark(kind, value, updated_utc) VALUES (?, ?, ?)",
            (kind, value, _utcnow()),
        )
        return True

    # ── stage_events ──────────────────────────────────────────────────────
    def stage_event(
        self,
        source_table: str,
        source_key: str,
        payload: dict,
        captured_utc: Optional[str] = None,
    ) -> bool:
        """Insert a raw event row. Returns False if it already exists."""
        cur = self._conn.execute(
            "INSERT OR IGNORE INTO stage_events"
            "(source_table, source_key, payload, captured_utc, state)"
            " VALUES (?, ?, ?, ?, ?)",
            (source_table, source_key, _dumps(payload), captured_utc or _utcnow(), STATE_NEW),
        )
        return cur.rowcount == 1

    def mark_staged(self, source_key: str) -> None:
        self._conn.execute(
            "UPDATE stage_events SET state = ?, attempts = attempts + 1"
            " WHERE source_key = ? AND state = ?",
            (STATE_STAGED, source_key, STATE_NEW),
        )

    def mark_consumed(self, source_key: str) -> None:
        self._conn.execute(
            "UPDATE stage_events SET state = ?, attempts = attempts + 1"
            " WHERE source_key = ? AND state IN (?, ?)",
            (STATE_CONSUMED, source_key, STATE_NEW, STATE_STAGED),
        )

    def mark_retry(self, source_key: str, error: str) -> None:
        self._conn.execute(
            "UPDATE stage_events SET attempts = attempts + 1, error = ?, state = ?"
            " WHERE source_key = ?",
            (error[:500], STATE_NEW, source_key),
        )

    def pending_events(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT id, source_table, source_key, payload, captured_utc, attempts, error"
            " FROM stage_events WHERE state = ? ORDER BY id ASC",
            (STATE_NEW,),
        ).fetchall()
        return [dict(r) for r in rows]

    def prune_consumed(self, older_than_utc: str) -> int:
        cur = self._conn.execute(
            "DELETE FROM stage_events WHERE state = ? AND captured_utc < ?",
            (STATE_CONSUMED, older_than_utc),
        )
        return cur.rowcount

    # ── meter_snapshot ────────────────────────────────────────────────────
    def upsert_snapshot(
        self,
        meter_id: str,
        payload: dict,
        captured_utc: Optional[str] = None,
    ) -> None:
        now = _utcnow()
        self._conn.execute(
            "INSERT INTO meter_snapshot(meter_id, payload, captured_utc, updated_utc)"
            " VALUES (?, ?, ?, ?)"
            " ON CONFLICT(meter_id) DO UPDATE SET"
            " payload = excluded.payload, captured_utc = excluded.captured_utc,"
            " updated_utc = excluded.updated_utc",
            (meter_id, _dumps(payload), captured_utc or now, now),
        )

    def get_snapshot(self, meter_id: str) -> Optional[dict]:
        row = self._conn.execute(
            "SELECT payload, captured_utc FROM meter_snapshot WHERE meter_id = ?",
            (meter_id,),
        ).fetchone()
        if row is None:
            return None
        return {"payload": json.loads(row["payload"]), "captured_utc": row["captured_utc"]}

    def snapshot_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM meter_snapshot").fetchone()
        return int(row["c"])

    def all_snapshots(self) -> list[dict]:
        rows = self._conn.execute(
            "SELECT meter_id, payload, captured_utc FROM meter_snapshot ORDER BY meter_id"
        ).fetchall()
        return [
            {
                "meter_id": r["meter_id"],
                "payload": json.loads(r["payload"]),
                "captured_utc": r["captured_utc"],
            }
            for r in rows
        ]

    # ── normalized_value ─────────────────────────────────────────────────
    def upsert_normalized(self, meter_id: str, point_id: str, value: float, unit: str = "") -> None:
        """Persist one canonical point value. ``value`` must be finite."""
        self._conn.execute(
            "INSERT INTO normalized_value(meter_id, point_id, value, unit, updated_utc)"
            " VALUES (?, ?, ?, ?, ?)"
            " ON CONFLICT(meter_id, point_id) DO UPDATE SET"
            " value = excluded.value, unit = excluded.unit, updated_utc = excluded.updated_utc",
            (meter_id, point_id, float(value), unit, _utcnow()),
        )

    def get_normalized(self, meter_id: str) -> dict[str, dict]:
        rows = self._conn.execute(
            "SELECT point_id, value, unit, updated_utc FROM normalized_value"
            " WHERE meter_id = ? ORDER BY point_id",
            (meter_id,),
        ).fetchall()
        return {
            r["point_id"]: {
                "value": r["value"],
                "unit": r["unit"],
                "updated_utc": r["updated_utc"],
            }
            for r in rows
        }

    def normalized_count(self) -> int:
        row = self._conn.execute("SELECT COUNT(*) AS c FROM normalized_value").fetchone()
        return int(row["c"])

    def counts(self) -> dict[str, Any]:
        by_state = dict(
            self._conn.execute(
                "SELECT state, COUNT(*) FROM stage_events GROUP BY state"
            ).fetchall()
        )
        return {
            "events": dict(by_state),
            "snapshots": self.snapshot_count(),
            "normalized": self.normalized_count(),
            "watermarks": dict(
                self._conn.execute("SELECT kind, value FROM watermark").fetchall()
            ),
        }

    def close(self) -> None:
        self._conn.close()