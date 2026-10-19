"""Separate MySQL database for NeoView oBIX data.

Owns a dedicated database (default ``obix``) so oBIX data shares no tables with
the gateway buffer or the office schema. It persists:

  * ``obix_device``       - one row per scanned device/folder (pseudo-meter)
  * ``obix_entry``        - the full tree listing: every folder *and* point ref
                            (the "entries" seen in a listing), per device
  * ``obix_point``        - the point catalog (leaf points) per device
  * ``obix_point_value``  - the latest value of each point
  * ``obix_point_history``- hourly rollup (min/max/avg/last/samples)
  * ``obix_read_run``     - one row per gateway read pass
  * ``obix_meta``         - schema version

The connection reuses the buffer server by default (see
``config.get_obix_mysql_settings``); only ``OBIX_MYSQL_DATABASE`` has to be set.
Single-threaded use, matching the buffer.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Iterable, Optional

import pymysql
import pymysql.cursors

SCHEMA_VERSION = 1

log = logging.getLogger("gateway.obix_store")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hour_bucket(captured_utc: Optional[str] = None) -> str:
    """Truncate a capture timestamp (or now) to its UTC hour."""
    if captured_utc:
        try:
            moment = datetime.fromisoformat(captured_utc)
        except ValueError:
            moment = datetime.now(timezone.utc)
    else:
        moment = datetime.now(timezone.utc)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ).isoformat(timespec="seconds")


def _as_numeric(value: Any) -> tuple[Optional[float], Optional[str]]:
    """Split a value into its (numeric, text) representations.

    Booleans become 1.0/0.0 plus ``"true"``/``"false"`` so both the graphable
    numeric column and a readable text column are populated.
    """
    if isinstance(value, bool):
        return (1.0 if value else 0.0, "true" if value else "false")
    if isinstance(value, (int, float)):
        return (float(value), None)
    if value is None:
        return (None, None)
    return (None, str(value))


class ObixStore:
    """oBIX database. Creates its own schema; single-threaded use."""

    def __init__(
        self,
        host: str,
        port: int = 3306,
        user: str = "",
        password: str = "",
        database: str = "obix",
        autocommit: bool = True,
    ) -> None:
        if not database:
            raise ValueError("an oBIX database name is required (OBIX_MYSQL_DATABASE)")
        self.host = host
        self.port = int(port)
        self.user = user
        self._password = password
        self.database = database
        self._autocommit = autocommit
        try:
            self._conn = self._connect(database)
        except pymysql.err.OperationalError as exc:
            # 1049 = unknown database: create it, then reconnect. Needs CREATE
            # privilege; otherwise surface an actionable message.
            if exc.args and exc.args[0] == 1049:
                self._create_database()
                self._conn = self._connect(database)
            else:
                raise

    def _connect(self, database: Optional[str]):
        return pymysql.connect(
            host=self.host,
            port=self.port,
            user=self.user,
            password=self._password,
            database=database,
            charset="utf8mb4",
            autocommit=self._autocommit,
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
            read_timeout=30,
            write_timeout=30,
        )

    def _create_database(self) -> None:
        try:
            server = self._connect(None)
        except pymysql.err.OperationalError as exc:
            raise RuntimeError(
                f"cannot connect to MySQL to create database '{self.database}': {exc}"
            ) from exc
        try:
            with server.cursor() as cur:
                cur.execute(
                    f"CREATE DATABASE IF NOT EXISTS `{self.database}`"
                    " CHARACTER SET utf8mb4 COLLATE utf8mb4_general_ci"
                )
        except pymysql.err.OperationalError as exc:
            raise RuntimeError(
                f"database '{self.database}' is missing and this account cannot "
                f"create it ({exc}). As an admin run: CREATE DATABASE "
                f"`{self.database}` CHARACTER SET utf8mb4; GRANT ALL ON "
                f"`{self.database}`.* TO '{self.user}'@'localhost';"
            ) from exc
        finally:
            server.close()

    # ── schema ────────────────────────────────────────────────────────────
    def initialize(self) -> None:
        cur = self._conn.cursor()
        cur.execute(
            "CREATE TABLE IF NOT EXISTS obix_meta ("
            "`key` VARCHAR(64) PRIMARY KEY, value VARCHAR(255) NOT NULL)"
            " CHARACTER SET utf8mb4"
        )
        statements = [
            """
            CREATE TABLE IF NOT EXISTS obix_device (
                meter_id       VARCHAR(64) PRIMARY KEY,
                device_path    VARCHAR(255) NOT NULL,
                name           VARCHAR(191) NOT NULL DEFAULT '',
                entry_count    INT NOT NULL DEFAULT 0,
                point_count    INT NOT NULL DEFAULT 0,
                discovered_utc VARCHAR(32) NOT NULL,
                updated_utc    VARCHAR(32) NOT NULL
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS obix_entry (
                id           BIGINT PRIMARY KEY AUTO_INCREMENT,
                meter_id     VARCHAR(64) NOT NULL,
                rel_path     VARCHAR(255) NOT NULL DEFAULT '',
                href         VARCHAR(255) NOT NULL,
                name         VARCHAR(191) NOT NULL DEFAULT '',
                display_name VARCHAR(191) NOT NULL DEFAULT '',
                is_folder    TINYINT NOT NULL DEFAULT 0,
                is_type      VARCHAR(191) NOT NULL DEFAULT '',
                depth        INT NOT NULL DEFAULT 0,
                updated_utc  VARCHAR(32) NOT NULL
            ) CHARACTER SET utf8mb4
            """,
            "CREATE UNIQUE INDEX ux_obix_entry ON obix_entry(meter_id, href)",
            """
            CREATE TABLE IF NOT EXISTS obix_point (
                meter_id       VARCHAR(64) NOT NULL,
                point_id       VARCHAR(64) NOT NULL,
                name           VARCHAR(191) NOT NULL DEFAULT '',
                href           VARCHAR(255) NOT NULL DEFAULT '',
                unit           VARCHAR(64) NOT NULL DEFAULT '',
                is_type        VARCHAR(191) NOT NULL DEFAULT '',
                discovered_utc VARCHAR(32) NOT NULL,
                updated_utc    VARCHAR(32) NOT NULL,
                PRIMARY KEY (meter_id, point_id)
            ) CHARACTER SET utf8mb4
            """,
            "CREATE INDEX ix_obix_point_href ON obix_point(meter_id, href)",
            """
            CREATE TABLE IF NOT EXISTS obix_point_value (
                meter_id     VARCHAR(64) NOT NULL,
                point_id     VARCHAR(64) NOT NULL,
                value        DOUBLE NULL,
                text_value   VARCHAR(255) NULL,
                unit         VARCHAR(64) NOT NULL DEFAULT '',
                status       VARCHAR(64) NOT NULL DEFAULT 'ok',
                captured_utc VARCHAR(32) NOT NULL,
                PRIMARY KEY (meter_id, point_id)
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS obix_point_history (
                meter_id   VARCHAR(64) NOT NULL,
                point_id   VARCHAR(64) NOT NULL,
                bucket_utc VARCHAR(32) NOT NULL,
                avg_value  DOUBLE NULL,
                min_value  DOUBLE NULL,
                max_value  DOUBLE NULL,
                last_value DOUBLE NULL,
                samples    INT NOT NULL DEFAULT 0,
                PRIMARY KEY (meter_id, point_id, bucket_utc)
            ) CHARACTER SET utf8mb4
            """,
            """
            CREATE TABLE IF NOT EXISTS obix_read_run (
                id            BIGINT PRIMARY KEY AUTO_INCREMENT,
                started_utc   VARCHAR(32) NOT NULL,
                finished_utc  VARCHAR(32) NOT NULL,
                points_ok     INT NOT NULL DEFAULT 0,
                points_failed INT NOT NULL DEFAULT 0,
                duration_ms   INT NOT NULL DEFAULT 0
            ) CHARACTER SET utf8mb4
            """,
        ]
        for stmt in statements:
            try:
                cur.execute(stmt)
            except pymysql.err.OperationalError as exc:
                code = exc.args[0] if exc.args else None
                if code in (1050, 1061):  # table/index already exists
                    continue
                raise
        cur.execute(
            "INSERT INTO obix_meta (`key`, value) VALUES ('schema_version', %s)"
            " ON DUPLICATE KEY UPDATE value = VALUES(value)",
            (str(SCHEMA_VERSION),),
        )

    # ── catalog ───────────────────────────────────────────────────────────
    def sync_device(
        self,
        meter_id: str,
        device_path: str,
        entries: Iterable[dict],
        name: str = "",
    ) -> int:
        """Replace a device's whole entry tree and record the device row.

        ``entries`` is the full listing (folders and points), each a dict with
        ``rel_path``, ``href``, ``name``, ``display_name``, ``is_folder``,
        ``is_type`` and ``depth``. Returns the number of leaf points stored.
        """
        rows = list(entries)
        leaves = [e for e in rows if not e.get("is_folder")]
        now = _utcnow()
        with self._conn.cursor() as cur:
            cur.execute("DELETE FROM obix_entry WHERE meter_id = %s", (meter_id,))
            if rows:
                cur.executemany(
                    "INSERT INTO obix_entry"
                    "(meter_id, rel_path, href, name, display_name, is_folder, is_type, depth, updated_utc)"
                    " VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)",
                    [
                        (
                            meter_id,
                            (e.get("rel_path") or "")[:255],
                            (e.get("href") or "")[:255],
                            (e.get("name") or "")[:191],
                            (e.get("display_name") or "")[:191],
                            1 if e.get("is_folder") else 0,
                            (e.get("is_type") or "")[:191],
                            int(e.get("depth") or 0),
                            now,
                        )
                        for e in rows
                    ],
                )
            for e in leaves:
                self._upsert_point(
                    cur,
                    meter_id,
                    e.get("point_id") or "",
                    e.get("name") or "",
                    e.get("href") or "",
                    e.get("unit") or "",
                    e.get("is_type") or "",
                    now,
                )
            cur.execute(
                "INSERT INTO obix_device"
                "(meter_id, device_path, name, entry_count, point_count, discovered_utc, updated_utc)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s)"
                " ON DUPLICATE KEY UPDATE device_path = VALUES(device_path),"
                " name = VALUES(name), entry_count = VALUES(entry_count),"
                " point_count = VALUES(point_count), updated_utc = VALUES(updated_utc)",
                (meter_id, device_path[:255], (name or meter_id)[:191], len(rows), len(leaves), now, now),
            )
        return len(leaves)

    def _upsert_point(self, cur, meter_id, point_id, name, href, unit, is_type, now) -> None:
        cur.execute(
            "INSERT INTO obix_point"
            "(meter_id, point_id, name, href, unit, is_type, discovered_utc, updated_utc)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE"
            "   name = IF(VALUES(name) <> '', VALUES(name), name),"
            "   href = CASE WHEN VALUES(href) <> '' THEN VALUES(href) ELSE href END,"
            "   unit = VALUES(unit), is_type = VALUES(is_type), updated_utc = VALUES(updated_utc)",
            (
                meter_id,
                point_id[:64],
                (name or "")[:191],
                (href or "")[:255],
                (unit or "")[:64],
                (is_type or "")[:191],
                now,
                now,
            ),
        )

    def upsert_point(
        self, meter_id: str, point_id: str, name: str = "",
        href: str = "", unit: str = "", is_type: str = "",
    ) -> None:
        with self._conn.cursor() as cur:
            self._upsert_point(
                cur, meter_id, point_id, name, href, unit, is_type, _utcnow()
            )

    # ── values ────────────────────────────────────────────────────────────
    def record_values(
        self,
        values: dict[tuple[str, str], tuple[Any, str]],
        captured_utc: Optional[str] = None,
    ) -> int:
        """Store one read pass: latest value per point + hourly history."""
        now = captured_utc or _utcnow()
        bucket = _hour_bucket(now)
        with self._conn.cursor() as cur:
            for (meter_id, point_id), (value, unit) in values.items():
                self._store_value(cur, meter_id, point_id, value, unit, "ok", now, bucket)
        return len(values)

    def _store_value(self, cur, meter_id, point_id, value, unit, status, now, bucket) -> None:
        numeric, text = _as_numeric(value)
        self._upsert_point(cur, meter_id, point_id, point_id, "", unit, "", now)
        cur.execute(
            "INSERT INTO obix_point_value"
            "(meter_id, point_id, value, text_value, unit, status, captured_utc)"
            " VALUES (%s, %s, %s, %s, %s, %s, %s)"
            " ON DUPLICATE KEY UPDATE value = VALUES(value), text_value = VALUES(text_value),"
            " unit = VALUES(unit), status = VALUES(status), captured_utc = VALUES(captured_utc)",
            (meter_id, point_id, numeric, text, (unit or "")[:64], status[:64], now),
        )
        if numeric is not None:
            cur.execute(
                "INSERT INTO obix_point_history"
                "(meter_id, point_id, bucket_utc, avg_value, min_value, max_value, last_value, samples)"
                " VALUES (%s, %s, %s, %s, %s, %s, %s, 1)"
                " ON DUPLICATE KEY UPDATE"
                "   avg_value = ((obix_point_history.avg_value * obix_point_history.samples) + VALUES(avg_value))"
                "               / (obix_point_history.samples + 1),"
                "   min_value = LEAST(obix_point_history.min_value, VALUES(min_value)),"
                "   max_value = GREATEST(obix_point_history.max_value, VALUES(max_value)),"
                "   last_value = VALUES(last_value),"
                "   samples = obix_point_history.samples + 1",
                (meter_id, point_id, bucket, numeric, numeric, numeric, numeric),
            )

    def record_run(
        self,
        started_utc: str,
        finished_utc: str,
        points_ok: int,
        points_failed: int = 0,
        duration_ms: int = 0,
    ) -> None:
        with self._conn.cursor() as cur:
            cur.execute(
                "INSERT INTO obix_read_run"
                "(started_utc, finished_utc, points_ok, points_failed, duration_ms)"
                " VALUES (%s, %s, %s, %s, %s)",
                (started_utc, finished_utc, int(points_ok), int(points_failed), int(duration_ms)),
            )

    # ── reads ─────────────────────────────────────────────────────────────
    @staticmethod
    def _page(limit: Optional[int], offset: int) -> str:
        """LIMIT/OFFSET clause. ``limit`` None means 'no cap'."""
        if limit is None and not offset:
            return ""
        if limit is None:
            # MySQL requires LIMIT before OFFSET; max unsigned = no effective cap.
            return f" LIMIT 18446744073709551615 OFFSET {int(offset)}"
        return f" LIMIT {int(limit)} OFFSET {int(offset or 0)}"

    def latest(
        self,
        meter_id: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
        meter_ids: Optional[Iterable[str]] = None,
    ) -> list[dict]:
        sql = (
            "SELECT meter_id, point_id, value, text_value, unit, status, captured_utc"
            " FROM obix_point_value"
        )
        params: list[Any] = []
        clauses: list[str] = []
        if meter_id:
            clauses.append("meter_id = %s")
            params.append(meter_id)
        allowed = [str(m) for m in (meter_ids or ()) if str(m)]
        if allowed:
            clauses.append("meter_id IN (" + ",".join(["%s"] * len(allowed)) + ")")
            params.extend(allowed)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        sql += " ORDER BY meter_id, point_id" + self._page(limit, offset)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def latest_count(
        self,
        meter_id: Optional[str] = None,
        meter_ids: Optional[Iterable[str]] = None,
    ) -> int:
        sql = "SELECT COUNT(*) AS c FROM obix_point_value"
        params: list[Any] = []
        clauses: list[str] = []
        if meter_id:
            clauses.append("meter_id = %s")
            params.append(meter_id)
        allowed = [str(m) for m in (meter_ids or ()) if str(m)]
        if allowed:
            clauses.append("meter_id IN (" + ",".join(["%s"] * len(allowed)) + ")")
            params.extend(allowed)
        if clauses:
            sql += " WHERE " + " AND ".join(clauses)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()["c"])

    def devices(self) -> list[dict]:
        sql = (
            "SELECT meter_id, device_path, name, point_count, updated_utc"
            " FROM obix_device ORDER BY meter_id"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql)
            return list(cur.fetchall())

    def history(
        self,
        meter_id: str,
        point_id: str,
        since_utc: Optional[str] = None,
        until_utc: Optional[str] = None,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        sql = (
            "SELECT bucket_utc, avg_value, min_value, max_value, last_value, samples"
            " FROM obix_point_history WHERE meter_id = %s AND point_id = %s"
        )
        params: list[Any] = [meter_id, point_id]
        if since_utc:
            sql += " AND bucket_utc >= %s"
            params.append(since_utc)
        if until_utc:
            sql += " AND bucket_utc <= %s"
            params.append(until_utc)
        sql += " ORDER BY bucket_utc ASC" + self._page(limit, offset)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return list(cur.fetchall())

    def points(
        self,
        meter_id: str,
        limit: Optional[int] = None,
        offset: int = 0,
    ) -> list[dict]:
        """Point catalog joined with the latest value, for one device."""
        sql = (
            "SELECT p.meter_id, p.point_id, p.name, p.href, p.unit, p.is_type,"
            " v.value, v.text_value, v.status, v.captured_utc"
            " FROM obix_point p"
            " LEFT JOIN obix_point_value v"
            "   ON v.meter_id = p.meter_id AND v.point_id = p.point_id"
            " WHERE p.meter_id = %s"
            " ORDER BY p.point_id"
        ) + self._page(limit, offset)
        with self._conn.cursor() as cur:
            cur.execute(sql, (meter_id,))
            return list(cur.fetchall())

    def point(self, meter_id: str, point_id: str) -> Optional[dict]:
        """Catalog metadata plus the latest value for a single point."""
        sql = (
            "SELECT p.meter_id, p.point_id, p.name, p.href, p.unit, p.is_type,"
            " v.value, v.text_value, v.status, v.captured_utc"
            " FROM obix_point p"
            " LEFT JOIN obix_point_value v"
            "   ON v.meter_id = p.meter_id AND v.point_id = p.point_id"
            " WHERE p.meter_id = %s AND p.point_id = %s"
        )
        with self._conn.cursor() as cur:
            cur.execute(sql, (meter_id, point_id))
            return cur.fetchone()

    def points_count(self, meter_id: Optional[str] = None) -> int:
        sql = "SELECT COUNT(*) AS c FROM obix_point"
        params: list[Any] = []
        if meter_id:
            sql += " WHERE meter_id = %s"
            params.append(meter_id)
        with self._conn.cursor() as cur:
            cur.execute(sql, params)
            return int(cur.fetchone()["c"])

    def freshness(self) -> dict[str, Any]:
        """Last read pass plus row counts, for a freshness/status endpoint."""
        with self._conn.cursor() as cur:
            cur.execute(
                "SELECT started_utc, finished_utc, points_ok, points_failed, duration_ms"
                " FROM obix_read_run ORDER BY id DESC LIMIT 1"
            )
            last = cur.fetchone()
        out: dict[str, Any] = self.counts()
        out["last_run"] = last
        return out

    def counts(self) -> dict[str, int]:
        with self._conn.cursor() as cur:
            out = {}
            for table in (
                "obix_device",
                "obix_entry",
                "obix_point",
                "obix_point_value",
                "obix_point_history",
                "obix_read_run",
            ):
                cur.execute(f"SELECT COUNT(*) AS c FROM {table}")
                out[table] = int(cur.fetchone()["c"])
        return out

    def close(self) -> None:
        try:
            self._conn.commit()
        except Exception:  # noqa: BLE001 - autocommit connection, nothing pending
            pass
        self._conn.close()


def build_obix_store(settings, logger=None) -> Optional[ObixStore]:
    """Create and initialise the oBIX store, or None when disabled/unavailable.

    A missing/unreachable oBIX database must never take the poll loop down, so
    failures are logged and swallowed here; the extraction loop keeps running.
    """
    from .config import get_obix_mysql_settings

    logr = logger or log
    cfg = get_obix_mysql_settings(settings)
    if not cfg.get("database"):
        return None
    try:
        store = ObixStore(
            cfg["host"],
            cfg["port"],
            cfg["user"],
            cfg["password"],
            cfg["database"],
        )
        store.initialize()
        return store
    except Exception as exc:  # noqa: BLE001 - oBIX DB is optional
        logr.warning("oBIX database unavailable (%s); oBIX values will not be stored", exc)
        return None
