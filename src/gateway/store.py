"""Persistent MySQL buffer (local database, autocommit).

Owns all state the gateway must survive restarts with:

  * ``watermark``      - durable per-stream cursor (e.g. ``receive_packet.id``)
  * ``stage_events``   - raw rows pulled by the extractor, awaiting the
    normalizer. Deduped by ``(source_table, source_key)``; state machine
    ``new -> staged -> consumed`` with an attempt counter and error text.
  * ``meter_snapshot`` - latest per-meter raw view (heads + latest analysis
    + last_receive + addr) refreshed each poll.
  * ``normalized_value`` - latest canonical point per meter (schema 3).
  * ``point_history``    - hourly rollups per meter/point, so consumption can
    be compared over time instead of only against the newest reading.
  * ``anomaly_finding``  - rule hits, deduplicated per rule+meter+hour so a
    meter that stays offline does not spam a finding every poll.
  * ``daily_summary``    - one AI-written narrative per local day.

The buffer lives in its own database (``BUFFER_MYSQL_DATABASE``, e.g.
``gateway_buffer``) on the local MySQL server, with its own least-privilege
account. It shares no tables with the ems-backend schema, so neither side's
migrations can collide, and the office ``mdpf`` database stays strictly
read-only — the office database is never touched by this module.
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Optional

import pymysql
import pymysql.cursors

# Event lifecycle states
STATE_NEW = "new"
STATE_STAGED = "staged"
STATE_CONSUMED = "consumed"

SCHEMA_VERSION = 4  # public: tests assert the current schema version


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _dumps(payload: dict) -> str:
    return json.dumps(payload, ensure_ascii=False, default=str)


class MySQLBuffer:
    """Buffer backed by a local MySQL database. Single-threaded use."""

    def __init__(
        self,
        host,
        port: int = 3306,
        user: str = "",
        password: str = "",
        database: str = "",
        autocommit: bool = True,
    ) -> None:
        # Legacy shim: tests and older call sites construct the buffer from a
        # path-like value (e.g. tmp_path / "gateway.db"). Any such value maps
        # to the BUFFER_MYSQL_* connection, so one database serves the suite
        # and existing call sites keep working unchanged.
        if isinstance(host, Path) or (
            isinstance(host, str)
            and (host.endswith(".db") or "/" in host or "\\" in host)
        ):
            from .config import get_buffer_mysql_settings

            settings = get_buffer_mysql_settings()
            host = settings["host"] or "127.0.0.1"
            port = settings["port"] or 3306
            user = settings["user"]
            password = settings["password"]
            database = settings["database"] or "gateway_buffer"
        if not database:
            raise ValueError(
                "buffer database is required: set BUFFER_MYSQL_DATABASE in .env"
            )
        self.host = host
        self.port = port
        self.user = user
        self.database = database
        self._conn = pymysql.connect(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            autocommit=autocommit,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
        )

    # ── schema ────────────────────────────────────────────────────────────
    def initialize(self) -> None:
        cur = self._conn.cursor()
        # ``key`` is a MySQL reserved word and must stay backticked.
        cur.execute(
            "CREATE TABLE IF NOT EXISTS buffer_meta ("
            "`key` VARCHAR(64) PRIMARY KEY, value VARCHAR(255) NOT NULL)"
            " CHARACTER SET utf8mb4"
        )
        self._ensure_schema_version(cur)

    def _ensure_schema_version(self, cur) -> None:
        cur.execute("SELECT value FROM buffer_meta WHERE `key` = 'schema_version'")
        row = cur.fetchone()
        version = int(row["value"]) if row else 0
        if version == SCHEMA_VERSION:
            return

        statements = [
            """
            CREATE TABLE IF NOT EXISTS watermark (
                kind         VARCHAR(128) PRIMARY KEY,
                value        BIGINT NOT NULL,
                updated_utc  VARCHAR(32) NOT NULL
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS stage_events (
                id           BIGINT PRIMARY KEY AUTO_INCREMENT,
                source_table VARCHAR(64) NOT NULL,
                source_key   VARCHAR(128) NOT NULL,
                payload      MEDIUMTEXT NOT NULL,
                captured_utc VARCHAR(32) NOT NULL,
                state        VARCHAR(16) NOT NULL DEFAULT 'new',
                attempts     INT NOT NULL DEFAULT 0,
                error        TEXT NULL
            ) CHARACTER SET utf8mb4
            """,
            "CREATE UNIQUE INDEX ux_stage_events_source ON stage_events(source_table, source_key)",
            "CREATE INDEX ix_stage_events_state ON stage_events(state)",
            """
            CREATE TABLE IF NOT EXISTS meter_snapshot (
                meter_id     VARCHAR(64) PRIMARY KEY,
                payload      MEDIUMTEXT NOT NULL,
                captured_utc VARCHAR(32) NOT NULL,
                updated_utc  VARCHAR(32) NOT NULL
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS normalized_value (
                meter_id    VARCHAR(64) NOT NULL,
                point_id    VARCHAR(64) NOT NULL,
                value       DOUBLE NULL,
                unit        VARCHAR(32) NOT NULL DEFAULT '',
                updated_utc VARCHAR(32) NOT NULL,
                PRIMARY KEY (meter_id, point_id)
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS point_history (
                meter_id    VARCHAR(64) NOT NULL,
                point_id    VARCHAR(64) NOT NULL,
                bucket_utc  VARCHAR(32) NOT NULL,
                avg_value   DOUBLE NULL,
                min_value   DOUBLE NULL,
                max_value   DOUBLE NULL,
                last_value  DOUBLE NULL,
                samples     INT NOT NULL DEFAULT 0,
                PRIMARY KEY (meter_id, point_id, bucket_utc)
            ) CHARACTER SET utf8mb4
            """,
            "CREATE INDEX ix_point_history_lookup ON point_history(point_id, bucket_utc)",
            """
            CREATE TABLE IF NOT EXISTS anomaly_finding (
                id           BIGINT PRIMARY KEY AUTO_INCREMENT,
                rule         VARCHAR(64) NOT NULL,
                meter_id     VARCHAR(64) NOT NULL DEFAULT '',
                severity     VARCHAR(16) NOT NULL DEFAULT 'info',
                message      TEXT NOT NULL,
                detail       TEXT NOT NULL,
                bucket_utc   VARCHAR(32) NOT NULL,
                created_utc  VARCHAR(32) NOT NULL,
                resolved_utc VARCHAR(32) NULL
            ) CHARACTER SET utf8mb4
            """,
            # One finding per rule+meter+hour: a meter offline for a week must
            # not produce seven identical rows an hour each. meter_id uses ''
            # (not NULL) so a plain unique index works on any MySQL version.
            "CREATE UNIQUE INDEX ux_anomaly_dedupe ON anomaly_finding(rule, meter_id, bucket_utc)",
            "CREATE INDEX ix_anomaly_open ON anomaly_finding(resolved_utc, created_utc)",
            """
            CREATE TABLE IF NOT EXISTS daily_summary (
                local_date    VARCHAR(10) PRIMARY KEY,
                body          MEDIUMTEXT NOT NULL,
                findings      INT NOT NULL DEFAULT 0,
                `usage`       MEDIUMTEXT NOT NULL,
                generated_utc VARCHAR(32) NOT NULL
            ) CHARACTER SET utf8mb4
            """,
        ]
        for stmt in statements:
            try:
                cur.execute(stmt)
            except pymysql.err.OperationalError as exc:
                # A previous partial init may have left some indexes/tables
                # behind; idempotent upsert of the rest still succeeds.
                code = exc.args[0] if exc.args else None
                if code in (1050, 1061):  # table/index already exists
                    continue
                raise

        if version < SCHEMA_VERSION:
            cur.execute(
                "INSERT INTO buffer_meta (`key`, value) VALUES ('schema_version', %s)"
                " ON DUPLICATE KEY UPDATE value = VALUES(value)",
                (str(SCHEMA_VERSION),),
            )

    # ── watermark ─────────────────────────────────────────────────────────
    def get_watermark(self, kind: str) -> Optional[int]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT value FROM watermark WHERE kind = %s", (kind,))
            row = cur.fetchone()
            return None if row is None else int(row["value"])

    def save_watermark(self, kind: str, value: int) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO watermark(kind, value, updated_utc) VALUES (%s, %s, %s)"
                " ON DUPLICATE KEY UPDATE value = VALUES(value), updated_utc = VALUES(updated_utc)",
                (kind, int(value), _utcnow()),
            )

    def init_watermark_if_absent(self, kind: str, value: int) -> bool:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO watermark(kind, value, updated_utc) VALUES (%s, %s, %s)",
                (kind, int(value), _utcnow()),
            )
            return cur.rowcount == 1

    # ── stage_events ──────────────────────────────────────────────────────
    def stage_event(
        self,
        source_table: str,
        source_key: str,
        payload: dict,
        captured_utc: Optional[str] = None,
    ) -> bool:
        """Insert a raw event row. Returns False if it already exists."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO stage_events"
                "(source_table, source_key, payload, captured_utc, state)"
                " VALUES (%s, %s, %s, %s, %s)",
                (source_table, source_key, _dumps(payload), captured_utc or _utcnow(), STATE_NEW),
            )
            return cur.rowcount == 1

    def mark_staged(self, source_key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE stage_events SET state = %s, attempts = attempts + 1"
                " WHERE source_key = %s AND state = %s",
                (STATE_STAGED, source_key, STATE_NEW),
            )

    def mark_consumed(self, source_key: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE stage_events SET state = %s, attempts = attempts + 1"
                " WHERE source_key = %s AND state IN (%s, %s)",
                (STATE_CONSUMED, source_key, STATE_NEW, STATE_STAGED),
            )

    def mark_retry(self, source_key: str, error: str) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE stage_events SET attempts = attempts + 1, error = %s, state = %s"
                " WHERE source_key = %s",
                (error[:500], STATE_NEW, source_key),
            )

    def pending_events(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT id, source_table, source_key, payload, captured_utc, attempts, error"
                " FROM stage_events WHERE state = %s ORDER BY id ASC",
                (STATE_NEW,),
            )
            return list(cur.fetchall())

    def prune_consumed(self, older_than_utc: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM stage_events WHERE state = %s AND captured_utc < %s",
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
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO meter_snapshot(meter_id, payload, captured_utc, updated_utc)"
                " VALUES (%s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE"
                " payload = VALUES(payload), captured_utc = VALUES(captured_utc),"
                " updated_utc = VALUES(updated_utc)",
                (meter_id, _dumps(payload), captured_utc or now, now),
            )

    def get_snapshot(self, meter_id: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT payload, captured_utc FROM meter_snapshot WHERE meter_id = %s",
                (meter_id,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        return {"payload": json.loads(row["payload"]), "captured_utc": row["captured_utc"]}

    def snapshot_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM meter_snapshot")
            return int(cur.fetchone()["c"])

    def all_snapshots(self) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT meter_id, payload, captured_utc FROM meter_snapshot ORDER BY meter_id"
            )
            return [
                {
                    "meter_id": r["meter_id"],
                    "payload": json.loads(r["payload"]),
                    "captured_utc": r["captured_utc"],
                }
                for r in cur.fetchall()
            ]

    # ── normalized_value ─────────────────────────────────────────────────
    def upsert_normalized(self, meter_id: str, point_id: str, value: float, unit: str = "") -> None:
        """Persist one canonical point value. ``value`` must be finite."""
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO normalized_value(meter_id, point_id, value, unit, updated_utc)"
                " VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE"
                " value = VALUES(value), unit = VALUES(unit), updated_utc = VALUES(updated_utc)",
                (meter_id, point_id, float(value), unit, _utcnow()),
            )

    def get_normalized(self, meter_id: str) -> dict[str, dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT point_id, value, unit, updated_utc FROM normalized_value"
                " WHERE meter_id = %s ORDER BY point_id",
                (meter_id,),
            )
            rows = cur.fetchall()
        return {
            r["point_id"]: {
                "value": r["value"],
                "unit": r["unit"],
                "updated_utc": r["updated_utc"],
            }
            for r in rows
        }

    def normalized_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM normalized_value")
            return int(cur.fetchone()["c"])

    def list_normalized_meters(self) -> list[dict]:
        """One summary row per meter that currently has normalized values."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT meter_id, COUNT(*) AS points, MAX(updated_utc) AS updated_utc"
                " FROM normalized_value GROUP BY meter_id ORDER BY meter_id"
            )
            rows = cur.fetchall()
        return [
            {
                "meter_id": r["meter_id"],
                "points": int(r["points"]),
                "updated_utc": r["updated_utc"],
            }
            for r in rows
        ]

    # ── point_history ────────────────────────────────────────────────────
    def record_history(
        self,
        meter_id: str,
        point_id: str,
        value: float,
        bucket_utc: str,
    ) -> None:
        """Fold one reading into its hourly bucket.

        Buckets keep min/max/last as well as the mean: consumption only ever
        rises, so a leak shows up as max far above last within one hour.
        """
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO point_history"
                "(meter_id, point_id, bucket_utc, avg_value, min_value, max_value, last_value, samples)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 1)"
                " ON DUPLICATE KEY UPDATE"
                "   avg_value = ((point_history.avg_value * point_history.samples) + VALUES(avg_value))"
                "               / (point_history.samples + 1),"
                "   min_value = LEAST(point_history.min_value, VALUES(min_value)),"
                "   max_value = GREATEST(point_history.max_value, VALUES(max_value)),"
                "   last_value = VALUES(last_value),"
                "   samples = point_history.samples + 1",
                (meter_id, point_id, bucket_utc, value, value, value, value),
            )

    def history(
        self,
        meter_id: str,
        point_id: Optional[str] = None,
        since_utc: Optional[str] = None,
        limit: int = 240,
    ) -> list[dict]:
        sql = (
            "SELECT meter_id, point_id, bucket_utc, avg_value, min_value, max_value,"
            " last_value, samples FROM point_history WHERE meter_id = %s"
        )
        params: list[Any] = [meter_id]
        if point_id:
            sql += " AND point_id = %s"
            params.append(point_id)
        if since_utc:
            sql += " AND bucket_utc >= %s"
            params.append(since_utc)
        sql += " ORDER BY bucket_utc DESC LIMIT %s"
        params.append(int(limit))
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def history_span(
        self,
        meter_id: str,
        point_id: str,
        since_utc: str,
        until_utc: Optional[str] = None,
    ) -> dict[str, Any]:
        """min/max/last/count for one point of one meter across a window.

        ``meter_id`` is required: pooling every meter's totalizer into a single
        min/max makes each one look like it consumed the difference between the
        smallest and largest meter on site.
        """
        # Spread must span min_value..max_value across buckets, not last_value:
        # a totalizer that jumped and settled inside one hour would otherwise
        # report zero usage and hide the very spike we are looking for.
        sql = (
            "SELECT MIN(min_value) AS lo, MAX(max_value) AS hi,"
            " SUM(samples) AS n, COUNT(*) AS buckets"
            " FROM point_history WHERE meter_id = %s AND point_id = %s AND bucket_utc >= %s"
        )
        params: list[Any] = [meter_id, point_id, since_utc]
        if until_utc:
            sql += " AND bucket_utc <= %s"
            params.append(until_utc)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            row = cur.fetchone()
            last_sql = (
                "SELECT last_value FROM point_history"
                " WHERE meter_id = %s AND point_id = %s AND bucket_utc >= %s"
                + (" AND bucket_utc <= %s" if until_utc else "")
                + " ORDER BY bucket_utc DESC LIMIT 1"
            )
            cur.execute(last_sql, params)
            last = cur.fetchone()
        return {
            "min": row["lo"],
            "max": row["hi"],
            "samples": int(row["n"] or 0),
            "buckets": int(row["buckets"] or 0),
            "last": None if last is None else last["last_value"],
        }

    def prune_history(self, older_than_utc: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "DELETE FROM point_history WHERE bucket_utc < %s", (older_than_utc,)
            )
            return cur.rowcount

    # ── anomaly_finding ──────────────────────────────────────────────────
    def add_finding(
        self,
        rule: str,
        meter_id: Optional[str],
        severity: str,
        message: str,
        bucket_utc: str,
        detail: Optional[dict] = None,
        created_utc: Optional[str] = None,
    ) -> bool:
        """Insert a finding. False when this rule+meter already fired this hour.

        ``created_utc`` defaults to now but is injectable so callers - and tests
        - can place a finding on a known timeline instead of wherever the wall
        clock happens to be.
        """
        meter_id = meter_id or ""  # NULL-free dedupe column (see schema)
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT IGNORE INTO anomaly_finding"
                "(rule, meter_id, severity, message, detail, bucket_utc, created_utc)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)",
                (
                    rule,
                    meter_id,
                    severity,
                    message,
                    _dumps(detail or {}),
                    bucket_utc,
                    created_utc or _utcnow(),
                ),
            )
            return cur.rowcount == 1

    def open_findings(self, since_utc: Optional[str] = None, limit: int = 100) -> list[dict]:
        sql = (
            "SELECT id, rule, meter_id, severity, message, detail, bucket_utc, created_utc"
            " FROM anomaly_finding WHERE resolved_utc IS NULL"
        )
        params: list[Any] = []
        if since_utc:
            sql += " AND created_utc >= %s"
            params.append(since_utc)
        sql += " ORDER BY created_utc DESC LIMIT %s"
        params.append(int(limit))
        rows = []
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            for r in cur.fetchall():
                item = dict(r)
                item["detail"] = json.loads(item["detail"] or "{}")
                rows.append(item)
        return rows

    def findings_between(self, start_utc: str, end_utc: str) -> list[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT rule, meter_id, severity, message, created_utc FROM anomaly_finding"
                " WHERE created_utc >= %s AND created_utc <= %s"
                " ORDER BY created_utc ASC",
                (start_utc, end_utc),
            )
            return list(cur.fetchall())

    def findings_count_between(self, start_utc: str, end_utc: str) -> int:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) AS c FROM anomaly_finding"
                " WHERE created_utc >= %s AND created_utc <= %s",
                (start_utc, end_utc),
            )
            return int(cur.fetchone()["c"])

    def resolve_findings_older_than(self, cutoff_utc: str) -> int:
        """Close findings that stopped firing, so /ai/findings stays current."""
        with self._conn.cursor() as cur:
            cur.execute(
                "UPDATE anomaly_finding SET resolved_utc = %s"
                " WHERE resolved_utc IS NULL AND bucket_utc < %s",
                (_utcnow(), cutoff_utc),
            )
            return cur.rowcount

    # ── daily_summary ────────────────────────────────────────────────────
    def save_summary(
        self,
        local_date: str,
        body: str,
        findings: int = 0,
        usage: Optional[dict] = None,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO daily_summary"
                "(local_date, body, findings, `usage`, generated_utc) VALUES (%s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE body = VALUES(body), findings = VALUES(findings),"
                " `usage` = VALUES(`usage`), generated_utc = VALUES(generated_utc)",
                (local_date, body, int(findings), _dumps(usage or {}), _utcnow()),
            )

    def latest_summary(self) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT local_date, body, findings, `usage`, generated_utc"
                " FROM daily_summary ORDER BY local_date DESC LIMIT 1"
            )
            row = cur.fetchone()
        if row is None:
            return None
        item = dict(row)
        item["usage"] = json.loads(item["usage"] or "{}")
        return item

    def summary_for(self, local_date: str) -> Optional[dict]:
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT local_date, body, findings, `usage`, generated_utc"
                " FROM daily_summary WHERE local_date = %s",
                (local_date,),
            )
            row = cur.fetchone()
        if row is None:
            return None
        item = dict(row)
        item["usage"] = json.loads(item["usage"] or "{}")
        return item

    def summary_count(self) -> int:
        with self._conn.cursor() as cur:
            cur.execute("SELECT COUNT(*) AS c FROM daily_summary")
            return int(cur.fetchone()["c"])

    def counts(self) -> dict[str, Any]:
        with self._conn.cursor() as cur:
            cur.execute("SELECT state, COUNT(*) AS c FROM stage_events GROUP BY state")
            by_state = {r["state"]: r["c"] for r in cur.fetchall()}
            cur.execute("SELECT COUNT(*) AS c FROM point_history")
            history = int(cur.fetchone()["c"])
            cur.execute(
                "SELECT COUNT(*) AS c FROM anomaly_finding WHERE resolved_utc IS NULL"
            )
            open_findings = int(cur.fetchone()["c"])
            cur.execute("SELECT kind, value FROM watermark")
            watermarks = {r["kind"]: r["value"] for r in cur.fetchall()}
        return {
            "events": by_state,
            "snapshots": self.snapshot_count(),
            "normalized": self.normalized_count(),
            "history": history,
            "open_findings": open_findings,
            "summaries": self.summary_count(),
            "watermarks": watermarks,
        }

    def close(self) -> None:
        try:
            self._conn.commit()
        except Exception:  # noqa: BLE001 - autocommit connection, nothing pending
            pass
        self._conn.close()


# Backwards-compatible alias: existing call sites (tests, tools) import
# SQLiteBuffer; the MySQL buffer keeps the same method surface.
SQLiteBuffer = MySQLBuffer
