"""Read-only MySQL extractor toward the office ``mdpf`` database.

Hard contract: this module performs SELECT / WITH queries ONLY. Every
statement is constructed from constants, verified by :func:`_check_read_only`
at execution time, and parameterized with ``%s`` placeholders (never string
interpolated). It never writes to the office database; all staging state
lives in the local SQLite buffer (``gateway.store``).
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Iterator, Optional

import pymysql
import pymysql.cursors

from .config import Settings

_RO_HEAD_RE = re.compile(r"^\s*(SELECT|WITH)\b", re.IGNORECASE)

# Verified against ems-backend prisma/legacy.schema.prisma.
_SQL_NEW_PACKETS = (
    "SELECT id, meter_id, version, file_type, receive_time, status"
    " FROM receive_packet WHERE id > %s ORDER BY id ASC LIMIT %s"
)
_SQL_MAX_PACKET_ID = "SELECT COALESCE(MAX(id), 0) FROM receive_packet"
_SQL_HEADS = (
    "SELECT meter_id, bat, rsrp, rssi, record_date_time, receive_time"
    " FROM analy_head"
)
_SQL_LATEST_ANALYSIS = """
SELECT r.meter_id, r.rec_date_time, r.amount, r.pressure, r.r_amount,
       r.sensor1_f, r.sensor2_f, r.battery, r.alarm, r.rsrp, r.rssi
FROM analy_result r
INNER JOIN (
    SELECT meter_id, MAX(rec_date_time) AS mdt
    FROM analy_result GROUP BY meter_id
) g ON r.meter_id = g.meter_id AND r.rec_date_time = g.mdt
"""
_SQL_LAST_RECEIVE = "SELECT meter_id, receive_time FROM last_receive"
_SQL_METER_ADDRS = "SELECT meter_id, addr FROM meter_status"


class ReadOnlyViolation(RuntimeError):
    """Raised when a non-SELECT statement would be executed."""


def _check_read_only(sql: str) -> None:
    if not _RO_HEAD_RE.match(sql):
        stmt = sql.lstrip()[:32]
        raise ReadOnlyViolation(
            f"extractor is SELECT-only; refused statement starting with {stmt!r}"
        )


def _jsonable(row: dict) -> dict:
    """Coerce MySQL cell types to JSON-safe values (Decimal stays numeric)."""
    out: dict[str, Any] = {}
    for key, value in row.items():
        if isinstance(value, Decimal):
            out[key] = float(value)
        elif isinstance(value, (datetime, date)):
            out[key] = value.isoformat(sep=" ", timespec="seconds")
        elif isinstance(value, (bytes, bytearray)):
            out[key] = value.decode("utf-8", errors="replace")
        else:
            out[key] = value
    return out


class Extractor:
    """Thin, SELECT-only wrapper over PyMySQL (DictCursor)."""

    def __init__(self, settings: Settings, connection: Any = None) -> None:
        self.settings = settings
        self._connection = connection  # injectable for tests

    @contextmanager
    def session(self) -> Iterator["Extractor"]:
        """Open one connection for the duration of a cycle (closed on exit)."""
        if self._connection is not None:
            self._cursor = self._connection.cursor(pymysql.cursors.DictCursor)
            try:
                yield self
            finally:
                self._cursor.close()
            return
        conn = pymysql.connect(
            host=self.settings.mysql_host,
            port=self.settings.mysql_port,
            user=self.settings.mysql_user,
            password=self.settings.mysql_password,
            database=self.settings.mysql_database,
            charset="utf8mb4",
            autocommit=True,
            connect_timeout=10,
            read_timeout=30,
            cursorclass=pymysql.cursors.DictCursor,
        )
        try:
            self._cursor = conn.cursor()
            try:
                yield self
            finally:
                self._cursor.close()
        finally:
            conn.close()

    def _query(self, sql: str, args: Optional[tuple] = None) -> list[dict]:
        _check_read_only(sql)
        self._cursor.execute(sql, args)
        return [_jsonable(row) for row in self._cursor.fetchall()]

    # ── stream (watermark) ────────────────────────────────────────────────
    def max_receive_packet_id(self) -> int:
        rows = self._query(_SQL_MAX_PACKET_ID)
        return int(rows[0]["COALESCE(MAX(id), 0)"] or 0)

    def fetch_new_packets(self, after_id: int, limit: int) -> list[dict]:
        return self._query(_SQL_NEW_PACKETS, (after_id, limit))

    # ── per-cycle snapshots ───────────────────────────────────────────────
    def fetch_heads(self) -> dict[str, dict]:
        return {row["meter_id"]: row for row in self._query(_SQL_HEADS)}

    def fetch_latest_analyses(self) -> dict[str, dict]:
        return {row["meter_id"]: row for row in self._query(_SQL_LATEST_ANALYSIS)}

    def fetch_last_receive(self) -> dict[str, dict]:
        return {row["meter_id"]: row for row in self._query(_SQL_LAST_RECEIVE)}

    def fetch_meter_addrs(self) -> dict[str, dict]:
        return {row["meter_id"]: row for row in self._query(_SQL_METER_ADDRS)}