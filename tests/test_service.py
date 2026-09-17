"""Service cycle tests with a duck-typed fake extractor (no MySQL involved)."""

from __future__ import annotations

from contextlib import contextmanager

import pytest

from gateway.service import WATERMARK_KIND, run_cycle
from gateway.store import SQLiteBuffer


class FakeExtractor:
    def __init__(
        self,
        packets=None,
        max_id=0,
        heads=None,
        analyses=None,
        last_receive=None,
        addrs=None,
        fail_fetch=False,
    ):
        self._packets = packets or []
        self._max_id = max_id
        self._heads = heads or {}
        self._analyses = analyses or {}
        self._last = last_receive or {}
        self._addrs = addrs or {}
        self._fail_fetch = fail_fetch

    @contextmanager
    def session(self):
        yield self

    def max_receive_packet_id(self):
        return self._max_id

    def fetch_new_packets(self, after_id, limit):
        if self._fail_fetch:
            raise RuntimeError("boom")
        cand = sorted(
            (p for p in self._packets if p["id"] > after_id),
            key=lambda p: p["id"],
        )
        return cand[:limit]

    def fetch_heads(self):
        return dict(self._heads)

    def fetch_latest_analyses(self):
        return dict(self._analyses)

    def fetch_last_receive(self):
        return dict(self._last)

    def fetch_meter_addrs(self):
        return dict(self._addrs)


@pytest.fixture
def buffer(tmp_path):
    b = SQLiteBuffer(tmp_path / "g.db")
    b.initialize()
    yield b
    b.close()


def test_first_cycle_skips_history_and_seeds_watermark(buffer):
    ex = FakeExtractor(packets=[{"id": 3}, {"id": 4}], max_id=42)
    result = run_cycle(buffer, ex, batch_size=10)
    assert result.watermark_value == 42
    assert buffer.get_watermark(WATERMARK_KIND) == 42
    assert result.new_packets == 0
    assert buffer.pending_events() == []


def test_stages_new_packets_with_paging_and_advances(buffer):
    first = FakeExtractor(
        packets=[{"id": 6, "meter_id": "A"}, {"id": 7, "meter_id": "A"}], max_id=9
    )
    run_cycle(buffer, first, batch_size=10)  # seeds watermark to 9

    second = FakeExtractor(
        packets=[
            {"id": 10, "meter_id": "A"},
            {"id": 11, "meter_id": "B"},
            {"id": 12, "meter_id": "C"},
            {"id": 13, "meter_id": "A"},
        ],
        max_id=13,
    )
    result = run_cycle(buffer, second, batch_size=2)  # force 3 pages
    assert result.new_packets == 4
    assert buffer.get_watermark(WATERMARK_KIND) == 13
    assert len(buffer.pending_events()) == 4


def test_replayed_ids_are_deduped(buffer):
    buffer.save_watermark(WATERMARK_KIND, 9)
    packets = [{"id": 10, "meter_id": "A"}, {"id": 11, "meter_id": "B"}]
    run_cycle(buffer, FakeExtractor(packets=packets, max_id=11), batch_size=10)
    run_cycle(buffer, FakeExtractor(packets=packets, max_id=11), batch_size=10)
    assert len(buffer.pending_events()) == 2
    assert buffer.get_watermark(WATERMARK_KIND) == 11


def test_watermark_not_advanced_when_cycle_fails(buffer):
    buffer.save_watermark(WATERMARK_KIND, 10)
    ex = FakeExtractor(packets=[{"id": 11}], max_id=11, fail_fetch=True)
    with pytest.raises(RuntimeError):
        run_cycle(buffer, ex, batch_size=10)
    assert buffer.get_watermark(WATERMARK_KIND) == 10
    assert buffer.pending_events() == []


def test_snapshot_merges_meter_state(buffer):
    ex = FakeExtractor(
        packets=[],
        max_id=0,
        heads={"M1": {"meter_id": "M1", "bat": 3.35}},
        analyses={"M1": {"meter_id": "M1", "amount": 100.5, "pressure": 2.75}},
        last_receive={"M1": {"meter_id": "M1", "receive_time": "2026-09-14 10:00:00"}},
        addrs={"M1": {"meter_id": "M1", "addr": "Branch - City"}},
    )
    run_cycle(buffer, ex)
    snap = buffer.get_snapshot("M1")["payload"]
    assert snap["addr"] == "Branch - City"
    assert snap["head"]["bat"] == 3.35
    assert snap["analysis"]["amount"] == 100.5
    assert snap["last_receive"]["receive_time"] == "2026-09-14 10:00:00"


def test_snapshot_set_from_analysis_without_last_receive(buffer):
    ex = FakeExtractor(
        packets=[],
        max_id=0,
        analyses={"M2": {"meter_id": "M2", "amount": 1.0}},
    )
    run_cycle(buffer, ex)
    assert buffer.get_snapshot("M2") is not None
    assert buffer.get_snapshot("M2")["payload"]["last_receive"] is None