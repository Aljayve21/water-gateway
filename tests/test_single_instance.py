"""Tests for the single-instance guard.

Two gateways sharing BACnet/Modbus ports is a genuinely confusing failure:
one binds, the other half-works, and the symptoms look like bad data. These
tests pin down the guard so a regression is caught here rather than in
production.
"""

from __future__ import annotations

import os

from gateway.main import SingleInstance


def test_first_acquire_succeeds_and_records_pid(tmp_path):
    lock = SingleInstance(tmp_path / "gateway.lock")
    assert lock.acquire() is True
    assert lock.path.exists()
    assert lock.holder_pid() == str(os.getpid())
    lock.release()


def test_second_acquire_is_refused(tmp_path):
    path = tmp_path / "gateway.lock"
    first = SingleInstance(path)
    assert first.acquire() is True
    try:
        second = SingleInstance(path)
        assert second.acquire() is False
        # The refused instance must report the real owner, not itself.
        assert second.holder_pid() == str(os.getpid())
    finally:
        first.release()


def test_release_allows_restart(tmp_path):
    path = tmp_path / "gateway.lock"
    first = SingleInstance(path)
    assert first.acquire() is True
    first.release()
    assert not path.exists()

    second = SingleInstance(path)
    assert second.acquire() is True
    second.release()


def test_release_is_idempotent(tmp_path):
    lock = SingleInstance(tmp_path / "gateway.lock")
    assert lock.acquire() is True
    lock.release()
    lock.release()  # must not raise
    assert not lock.path.exists()


def test_unwritable_location_does_not_block_startup(tmp_path):
    """A lock we cannot create must not stop a valid gateway from running."""
    blocker = tmp_path / "blocker"
    blocker.mkdir()
    lock = SingleInstance(blocker / "nested" / "gateway.lock")
    # Parent is a directory in the way of mkdir/parents, so this is unwritable.
    assert lock.acquire() is True
    lock.release()


def test_context_manager_releases(tmp_path):
    path = tmp_path / "gateway.lock"
    with SingleInstance(path) as guard:
        assert guard.acquire() is True
        assert path.exists()
    assert not path.exists()