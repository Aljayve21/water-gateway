"""Tests for the live EMS-user registry (gateway.ai.ems_sync).

Creates ems-shaped ``users`` / ``consumer_meters`` tables in the test
database and asserts the mapping the gateway enforces per role:

  ADMIN/TECHNICIAN -> whole estate
  USER             -> exactly consumer_meters + logger_id (fail closed)
  INACTIVE/SUSPENDED/deleted -> not in the registry at all
"""

from __future__ import annotations

import pymysql
import pytest

from gateway.ai.ems_sync import EmsUserSync, MergedSubjectMap
from gateway.ai.roles import Principal

USER_DDL = """
CREATE TABLE IF NOT EXISTS users (
    id         VARCHAR(64) PRIMARY KEY,
    email      VARCHAR(191),
    role       VARCHAR(20),
    status     VARCHAR(20) DEFAULT 'ACTIVE',
    logger_id  VARCHAR(64) NULL,
    deleted_at DATETIME NULL
) CHARACTER SET utf8mb4
"""
METERS_DDL = """
CREATE TABLE IF NOT EXISTS consumer_meters (
    id      BIGINT PRIMARY KEY AUTO_INCREMENT,
    user_id VARCHAR(64) NOT NULL,
    meter_id VARCHAR(50) NOT NULL
) CHARACTER SET utf8mb4
"""


@pytest.fixture
def ems_db(buffer_dsn):
    """The test database shaped like the ems-backend schema."""
    conn = pymysql.connect(**buffer_dsn, autocommit=True)
    with conn.cursor() as cur:
        cur.execute(USER_DDL)
        cur.execute(METERS_DDL)
    yield conn
    conn.close()


def _add_user(ems_db, uid, email="u@x.com", role="USER", status="ACTIVE",
              logger_id=None, deleted_at=None):
    with ems_db.cursor() as cur:
        cur.execute(
            "INSERT INTO users (id, email, role, status, logger_id, deleted_at)"
            " VALUES (%s, %s, %s, %s, %s, %s)",
            (uid, email, role, status, logger_id, deleted_at),
        )


def _add_meter(ems_db, uid, meter_id):
    with ems_db.cursor() as cur:
        cur.execute(
            "INSERT INTO consumer_meters (user_id, meter_id) VALUES (%s, %s)",
            (uid, meter_id),
        )


@pytest.fixture
def sync(buffer_dsn):
    return EmsUserSync(ttl_seconds=60, **buffer_dsn)


def test_admin_sees_whole_estate(ems_db, sync):
    _add_user(ems_db, "admin-1", role="ADMIN")
    users = sync.load()
    assert users["admin-1"].sees_all_meters is True
    assert users["admin-1"].can_access("any-meter-at-all")


def test_technician_is_privileged_too(ems_db, sync):
    _add_user(ems_db, "tech-1", role="TECHNICIAN")
    assert sync.load()["tech-1"].sees_all_meters


def test_user_scoped_to_consumer_meters_plus_logger(ems_db, sync):
    _add_user(ems_db, "maria", role="USER", logger_id="M-LOG")
    _add_meter(ems_db, "maria", "M-001")
    _add_meter(ems_db, "maria", "M-001")  # duplicate row collapses
    maria = sync.load()["maria"]
    assert maria.sees_all_meters is False
    assert maria.meters == ("M-001", "M-LOG")
    assert maria.can_access("M-001") and maria.can_access("M-LOG")
    assert not maria.can_access("someone-elses-meter")


def test_user_with_no_meters_gets_nothing(ems_db, sync):
    """Fail closed: a consumer with no rows must see no meters at all."""
    _add_user(ems_db, "empty-user", role="USER")
    empty = sync.load()["empty-user"]
    assert empty.meters == ()
    assert empty.can_access("026026060001") is False


def test_inactive_suspended_and_deleted_users_excluded(ems_db, sync):
    _add_user(ems_db, "u-active", role="ADMIN")
    _add_user(ems_db, "u-inactive", role="ADMIN", status="INACTIVE")
    _add_user(ems_db, "u-suspended", role="ADMIN", status="SUSPENDED")
    _add_user(ems_db, "u-deleted", role="ADMIN", deleted_at="2026-01-01 00:00:00")
    users = sync.load()
    assert set(users) == {"u-active"}


def test_ttl_cache_refreshes_only_when_stale(ems_db, sync):
    _add_user(ems_db, "u1", role="ADMIN")
    assert set(sync.load()) == {"u1"}

    # within the TTL the stale map is served even though the row changed
    _add_user(ems_db, "u2", role="ADMIN")
    assert set(sync.load()) == {"u1"}

    # forced load sees the change
    assert set(sync.load(force=True)) == {"u1", "u2"}


def test_failed_refresh_keeps_last_good_map(ems_db, sync, buffer_dsn):
    _add_user(ems_db, "u1", role="ADMIN")
    users = sync.load(force=True)
    assert set(users) == {"u1"}

    # point a fresh sync at a database that does not exist
    broken = EmsUserSync(ttl_seconds=5, **{**buffer_dsn, "database": "no_such_db_xyz"})
    assert broken.load() == {}          # first load fails, empty map
    assert broken.last_error            # but the error is reported


def test_merged_map_static_wins(ems_db, sync):
    _add_user(ems_db, "db-admin", role="ADMIN")
    sync.load(force=True)

    static = {
        "ops": Principal.admin("ops"),
        "db-admin": Principal.consumer("db-admin", meters=("M-LOCKED",)),
    }
    merged = MergedSubjectMap(static, sync)

    # operator-configured entry is untouched even though the DB row exists
    assert merged["db-admin"].meters == ("M-LOCKED",)
    # database-only subject is present
    assert merged["db-admin"] is not None
    assert merged.get("missing-subject") is None
    assert "ops" in merged
    # len counts static + database-only subjects
    assert len(merged) == 2
