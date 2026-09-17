"""Mapping engine tests: compile, validation, float32 packing, live projection."""

from __future__ import annotations

import pytest

from gateway.config import (
    BacnetPointMap,
    BasePointsConfig,
    MetersConfig,
    MeterConfig,
    ModbusPointMap,
    PointMapping,
    load_all,
)
from gateway.map import (
    MappingEngine,
    decode_float32,
    encode_float32,
    modbus_pdu_address,
)
from gateway.store import SQLiteBuffer


@pytest.fixture(scope="module")
def real_engine():
    _, _, base_points, meters = load_all()
    return MappingEngine(meters, base_points)


# ── address normalization ───────────────────────────────────────────────
def test_modbus_pdu_address_conventions():
    assert modbus_pdu_address("holding", 40001) == 0
    assert modbus_pdu_address("holding", 40003) == 2
    assert modbus_pdu_address("input", 30001) == 0
    assert modbus_pdu_address("coil", 1) == 0
    assert modbus_pdu_address("coil", 2) == 1
    with pytest.raises(ValueError):
        modbus_pdu_address("holding", 1)


# ── compiled map from real (DRAFT) config ───────────────────────────────
FIRST_METER = "026026060001"


def test_compiles_real_config(real_engine):
    m = real_engine.map
    assert FIRST_METER in m.meters
    meter = m.meters[FIRST_METER]
    assert meter.modbus_unit_id == 1
    assert m.unit_to_meter[1] == FIRST_METER
    assert meter.points["totalizer"].modbus.pdu_address == 0
    assert meter.points["pressure"].modbus.pdu_address == 2
    assert meter.points["online"].modbus.pdu_address == 0
    assert meter.points["online"].modbus.register_type == "coil"
    assert m.bacnet_device == 1
    # all 12 live meters, each with the full 9-point template
    assert len(m.bacnet_entries()) == 12 * len(meter.points)


def test_bacnet_points_mapped_with_units(real_engine):
    meter = real_engine.map.meters[FIRST_METER]
    assert meter.points["totalizer"].bacnet.object_type == "AI"
    assert meter.points["totalizer"].bacnet.units == "cubicMeters"
    assert meter.points["online"].bacnet.object_type == "BI"
    assert meter.points["battery"].bacnet.units == "volts"
    assert meter.points["signal"].bacnet.units == "noUnits"  # BACnet has no dBm


def test_bacnet_instances_unique_across_meters(real_engine):
    seen = set()
    for _, point_id, bp in real_engine.map.bacnet_entries():
        key = (bp.object_type, bp.instance)
        assert key not in seen, f"duplicate {key}"
        seen.add(key)
    assert len(seen) == 108


def test_modbus_unit_ids_unique_across_meters(real_engine):
    assert len(real_engine.map.unit_to_meter) == 12
    assert real_engine.map.modbus_unit_ids() == list(range(1, 13))


# ── validation errors ───────────────────────────────────────────────────
def _engine_for(meters, base=None):
    base_cfg = base or load_all()[2]
    return MappingEngine(meters, base_cfg)


def test_duplicate_modbus_span_raises():
    m0 = MeterConfig(
        meter_id="M0",
        modbus_unit_id=10,
        points={
            "totalizer": PointMapping(
                modbus=ModbusPointMap(register_type="holding", address=40001)
            ),
            "pressure": PointMapping(
                modbus=ModbusPointMap(register_type="holding", address=40002)
            ),
        },
    )
    with pytest.raises(ValueError, match="overlap"):
        _engine_for(MetersConfig(meters={"M0": m0}))


def test_duplicate_unit_id_raises():
    m0 = MeterConfig(meter_id="A", modbus_unit_id=5)
    m1 = MeterConfig(meter_id="B", modbus_unit_id=5)
    with pytest.raises(ValueError, match="duplicate modbus unit id"):
        _engine_for(MetersConfig(meters={"A": m0, "B": m1}))


def test_duplicate_bacnet_instance_raises():
    m0 = MeterConfig(
        meter_id="A",
        modbus_unit_id=5,
        points={
            "online": PointMapping(
                bacnet=BacnetPointMap(object="BI", instance=1)
            )
        },
    )
    m1 = MeterConfig(
        meter_id="B",
        modbus_unit_id=6,
        points={
            "online": PointMapping(
                bacnet=BacnetPointMap(object="BI", instance=1)
            )
        },
    )
    with pytest.raises(ValueError, match="duplicate BACnet object"):
        _engine_for(MetersConfig(meters={"A": m0, "B": m1}))


def test_unknown_point_raises():
    m0 = MeterConfig(
        meter_id="A",
        modbus_unit_id=5,
        points={"not-a-point": PointMapping()},
    )
    with pytest.raises(ValueError, match="not-a-point"):
        _engine_for(MetersConfig(meters={"A": m0}))


def test_mixed_bacnet_device_raises():
    m0 = MeterConfig(meter_id="A", modbus_unit_id=5, bacnet_device=1)
    m1 = MeterConfig(meter_id="B", modbus_unit_id=6, bacnet_device=2)
    with pytest.raises(ValueError, match="one BACnet device"):
        _engine_for(MetersConfig(meters={"A": m0, "B": m1}))


def test_bool_dtype_requires_coil():
    m0 = MeterConfig(
        meter_id="A",
        modbus_unit_id=5,
        points={
            "online": PointMapping(
                modbus=ModbusPointMap(
                    register_type="holding",
                    address=40001,
                    dtype="bool",
                )
            )
        },
    )
    with pytest.raises(ValueError, match="bool dtype requires coil"):
        _engine_for(MetersConfig(meters={"A": m0}))


# ── float32 packing ─────────────────────────────────────────────────────
def test_encode_float32_big_big():
    assert encode_float32(3.5, "big", "big") == (0x4060, 0x0000)
    assert encode_float32(1.0, "big", "big") == (0x3F80, 0x0000)


def test_encode_float32_big_little():
    assert encode_float32(3.5, "big", "little") == (0x0000, 0x4060)


def test_encode_float32_little_byte_order():
    assert encode_float32(3.5, "little", "big") == (0x0000, 0x6040)
    assert encode_float32(3.5, "little", "little") == (0x6040, 0x0000)


@pytest.mark.parametrize(
    "value,byte_order,word_order",
    [
        (0.0, o, w) for o in ("big", "little") for w in ("big", "little")
    ]
    + [
        (1234.5, "big", "big"),
        (-3.75, "big", "little"),
        (0.001, "little", "big"),
        (-1e-9, "little", "little"),
    ],
)
def test_float32_roundtrip(value, byte_order, word_order):
    words = encode_float32(value, byte_order, word_order)
    assert decode_float32(words, byte_order, word_order) == pytest.approx(value)


# ── live projection + scaling ───────────────────────────────────────────
def test_live_projection_from_buffer(real_engine, tmp_path):
    buffer = SQLiteBuffer(tmp_path / "m.db")
    buffer.initialize()
    buffer.upsert_normalized(FIRST_METER, "totalizer", 12.5, "m3")
    buffer.upsert_normalized(FIRST_METER, "online", 1.0, "binary")
    live = real_engine.live(buffer)
    assert live[FIRST_METER]["totalizer"] == 12.5
    assert live[FIRST_METER]["online"] == 1.0
    assert "pressure" not in live[FIRST_METER]
    buffer.close()


def test_modbus_int_applies_scale(real_engine):
    point = real_engine.map.meters[FIRST_METER].points["totalizer"].modbus
    words = real_engine.modbus_int(point, 10.0)
    out = decode_float32(words, point.byte_order, point.word_order)
    assert out == 10.0
    battery = real_engine.map.meters[FIRST_METER].points["battery"].modbus
    words = real_engine.modbus_int(battery, 3.63)  # scale 0.001 [DRAFT]
    out = decode_float32(words, battery.byte_order, battery.word_order)
    assert out == pytest.approx(0.00363)