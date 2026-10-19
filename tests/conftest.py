"""Shared pytest fixtures for the MySQL-backed buffer.

All tests run against the ``gateway_test`` database on the local MySQL server;
connection settings come from the project ``.env`` (gitignored) or the real
environment, so secrets never live in this file. An autouse fixture empties
every buffer table before each test, keeping suites order-independent without
the per-test temp files the SQLite buffer used.
"""

from __future__ import annotations

import os
from pathlib import Path

import pymysql
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TABLES = [
    "stage_events",
    "meter_snapshot",
    "normalized_value",
    "point_history",
    "anomaly_finding",
    "daily_summary",
    "watermark",
    "buffer_meta",
    "users",
    "consumer_meters",
]


def _load_env() -> None:
    """Populate os.environ with BUFFER_MYSQL_* from .env (no override)."""
    env_file = PROJECT_ROOT / ".env"
    if not env_file.is_file():
        return
    for line in env_file.read_text(encoding="utf-8-sig").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        key = key.strip()
        value = value.strip()
        if key.startswith("BUFFER_MYSQL_") and key not in os.environ:
            os.environ[key] = value


_load_env()
# Tests always target the dedicated test database, never the live buffer.
os.environ["BUFFER_MYSQL_DATABASE"] = "gateway_test"


def _connect(database: str, autocommit: bool = False):
    return pymysql.connect(
        host=os.environ.get("BUFFER_MYSQL_HOST", "127.0.0.1"),
        port=int(os.environ.get("BUFFER_MYSQL_PORT", "3306")),
        user=os.environ.get("BUFFER_MYSQL_USER", "gateway_buf"),
        password=os.environ.get("BUFFER_MYSQL_PASSWORD", ""),
        database=database,
        charset="utf8mb4",
        autocommit=autocommit,
    )


@pytest.fixture(scope="session", autouse=True)
def _require_test_database():
    """Fail fast with a clear message when the test database is unreachable."""
    try:
        conn = _connect(os.environ["BUFFER_MYSQL_DATABASE"])
    except pymysql.err.OperationalError as exc:
        pytest.exit(
            "MySQL test database unreachable "
            f"({os.environ.get('BUFFER_MYSQL_HOST', '127.0.0.1')}/{os.environ['BUFFER_MYSQL_DATABASE']}): {exc}. "
            "Start MySQL and create the gateway_test database before running the suite.",
            returncode=3,
        )
    conn.close()
    yield


@pytest.fixture(autouse=True)
def _clean_buffer_tables():
    """Empty every buffer table so each test starts from a known state."""
    conn = _connect(os.environ["BUFFER_MYSQL_DATABASE"], autocommit=True)
    yield
    with conn.cursor() as cur:
        # TRUNCATE resets AUTO_INCREMENT counters too, keeping ids stable
        # across runs (some tests assert on specific ids).
        cur.execute("SET FOREIGN_KEY_CHECKS = 0")
        for table in TABLES:
            try:
                cur.execute(f"TRUNCATE TABLE {table}")
            except pymysql.err.ProgrammingError:
                pass  # table not created yet on a fresh database
        cur.execute("SET FOREIGN_KEY_CHECKS = 1")
    conn.close()


@pytest.fixture
def buffer_dsn() -> dict:
    """Connection parameters for constructing a MySQLBuffer in-process."""
    return {
        "host": os.environ.get("BUFFER_MYSQL_HOST", "127.0.0.1"),
        "port": int(os.environ.get("BUFFER_MYSQL_PORT", "3306")),
        "user": os.environ.get("BUFFER_MYSQL_USER", "gateway_buf"),
        "password": os.environ.get("BUFFER_MYSQL_PASSWORD", ""),
        "database": os.environ["BUFFER_MYSQL_DATABASE"],
    }
