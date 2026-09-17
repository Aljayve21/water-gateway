"""Config layer tests — verify YAML templates load and fail fast."""

from __future__ import annotations

from pathlib import Path

import pytest

from gateway.config import (
    CONFIG_DIR,
    BasePoint,
    GatewayConfig,
    MetersConfig,
    load_all,
    load_base_points,
    load_gateway_config,
    load_meters,
)


@pytest.fixture(scope="module")
def configs():
    return load_all(CONFIG_DIR)


def test_gateway_yaml_loads(configs):
    _, gateway, _, meters = configs
    assert isinstance(gateway, GatewayConfig)
    assert gateway.extraction.poll_interval_seconds == 15
    assert gateway.staleness.online_hours == 24
    assert gateway.staleness.watch_hours == 48
    assert gateway.quality.battery_critical_volts == 3.15
    assert gateway.quality.signal_warning_dbm == -90
    assert gateway.buffer.max_retries >= 0


def test_base_points_cover_channel_vocabulary(configs):
    _, _, base, _ = configs
    ids = set(base.points)
    expected = {"totalizer", "pressure", "reverse", "pulse1", "pulse2",
                "battery", "signal", "online", "alarm"}
    assert expected <= ids


def test_base_point_metadata(configs):
    _, _, base, _ = configs
    assert base.points["totalizer"].unit == "m3"
    assert base.points["pressure"].unit == "kg/cm2"
    assert base.points["signal"].fallback_column == "rssi"
    assert isinstance(base.points["online"], BasePoint)


def test_meters_draft_are_flagged(configs):
    _, _, _, meters = configs
    assert isinstance(meters, MetersConfig)
    assert len(meters.meters) == 12  # all live meters, generated from DB
    first = "026026060001"
    meta = meters.meters[first]
    assert meta.modbus_unit_id == 1
    assert meta.bacnet_device == 1
    ids = set(meta.points)
    assert {"totalizer", "pressure", "reverse", "pulse1", "pulse2",
            "battery", "signal", "online", "alarm"} <= ids


def test_draft_addresses_are_placeholders(configs):
    _, _, _, meters = configs
    p = meters.meters["026026060001"].points
    assert p["totalizer"].modbus is not None
    assert p["totalizer"].bacnet is not None
    assert p["totalizer"].modbus.address > 0
    assert p["online"].modbus.register_type == "coil"


def test_bad_yaml_fails_fast(tmp_path: Path):
    bad = tmp_path / "gateway.yaml"
    bad.write_text(
        "extraction:\n  batch_size: nope\n",
        encoding="utf-8",
    )
    with pytest.raises(Exception):
        load_gateway_config(bad)


def test_missing_file_fails_fast(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        load_gateway_config(tmp_path / "does-not-exist.yaml")