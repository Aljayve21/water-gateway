"""Extractor tests: SELECT-only contract, query constants, coercion, binding."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal

import pytest

import gateway.extractor as exmod
from gateway.config import Settings
from gateway.extractor import ReadOnlyViolation, _check_read_only, _jsonable, Extractor


def _all_sql_constants():
    return [
        value
        for name, value in vars(exmod).items()
        if name.startswith("_SQL")
    ]


def test_sql_constants_are_select_only():
    constants = _all_sql_constants()
    assert constants, "expected module-level _SQL_* constants"
    for sql in constants:
        _check_read_only(sql)


def test_read_only_guard_rejects_writes():
    for bad in (
        "INSERT INTO x VALUES (1)",
        "DELETE FROM y WHERE 1=1",
        "UPDATE t SET a=1",
        "CREATE TABLE z (id INT)",
        "DROP TABLE z",
    ):
        with pytest.raises(ReadOnlyViolation):
            _check_read_only(bad)


def test_read_only_guard_accepts_selections():
    _check_read_only("SELECT 1")
    _check_read_only("with q as (select 1) select * from q")


def test_jsonable_coerces_cells():
    row = {
        "d": Decimal("3.35000"),
        "dt": datetime(2026, 9, 14, 11, 0, 0),
        "x": None,
        "b": True,
        "i": 5,
    }
    out = _jsonable(row)
    assert out["d"] == 3.35
    assert out["dt"] == "2026-09-14 11:00:00"
    assert out["x"] is None
    assert out["b"] is True


class FakeCursor:
    def __init__(self, rows):
        self.rows = rows
        self.last_sql = None
        self.last_args = None

    def execute(self, sql, args=None):
        self.last_sql = sql
        self.last_args = args

    def fetchall(self):
        return self.rows

    def close(self):
        pass


class FakeConnection:
    def __init__(self, rows):
        self.cur = FakeCursor(rows)

    def cursor(self, *args, **kwargs):
        return self.cur


@pytest.mark.parametrize(
    ("packets", "after", "limit", "expected_args"),
    [
        ([{"id": 11}], 10, 500, (10, 500)),
        ([{"id": 21}, {"id": 22}], 20, 2, (20, 2)),
    ],
)
def test_fetch_new_packets_binds_args(packets, after, limit, expected_args):
    conn = FakeConnection([{"id": p["id"]} for p in packets])
    extractor = Extractor(Settings(), connection=conn)
    with extractor.session():
        rows = extractor.fetch_new_packets(after_id=after, limit=limit)
    assert rows == [{"id": p["id"]} for p in packets]
    assert conn.cur.last_args == expected_args
    _check_read_only(conn.cur.last_sql)


def test_extractor_queries_are_single_statements():
    for sql in _all_sql_constants():
        body = sql.strip().rstrip(";").strip()
        assert ";" not in body, f"multi-statement SQL is forbidden: {body!r}"