"""Modbus TCP server tests: read/write/exception paths over a live socket."""

from __future__ import annotations

import asyncio
import struct

import pytest

from gateway.config import (
    BacnetPointMap,
    MetersConfig,
    MeterConfig,
    ModbusPointMap,
    PointMapping,
    load_all,
)
from gateway.map import MappingEngine, decode_float32
from gateway.modbus_server import ModbusTCPServer
from gateway.store import SQLiteBuffer

real_engine = MappingEngine(load_all()[3], load_all()[2])
FIRST_METER = "026026060001"


def _engine_fixture(meters_cfg):
    return MappingEngine(meters_cfg, load_all()[2])


def _two_meter_engine():
    m0 = MeterConfig(
        meter_id="A",
        modbus_unit_id=10,
        points={
            "totalizer": PointMapping(
                modbus=ModbusPointMap(register_type="holding", address=40001),
                bacnet=BacnetPointMap(object="AI", instance=1),
            )
        },
    )
    m1 = MeterConfig(
        meter_id="B",
        modbus_unit_id=20,
        points={
            "totalizer": PointMapping(
                modbus=ModbusPointMap(register_type="holding", address=40001),
                bacnet=BacnetPointMap(object="AI", instance=2),
            )
        },
    )
    return _engine_fixture(MetersConfig(meters={"A": m0, "B": m1}))


async def _request(port, unit, fc, body, tid=1):
    reader, writer = await asyncio.open_connection("127.0.0.1", port)
    writer.write(struct.pack(">HHHB", tid, 0, 2 + len(body), unit) + bytes([fc]) + body)
    await writer.drain()
    header = await asyncio.wait_for(reader.readexactly(7), 3)
    length = struct.unpack(">H", header[4:6])[0]
    pdu = await asyncio.wait_for(reader.readexactly(length - 1), 3)
    writer.close()
    return header, pdu


def _expect_exception(pdu, fc, code):
    assert pdu[0] == fc | 0x80
    assert pdu[1] == code


def _real_registers(pdu, word_order="big", byte_order="big"):
    assert pdu[0] == 0x03
    byte_count = pdu[1]
    words = tuple(struct.unpack(f">{byte_count // 2}H", pdu[2 : 2 + byte_count]))
    assert len(words) * 2 == byte_count
    return decode_float32(words, byte_order, word_order)


def _read_bits(pdu, quantity):
    assert pdu[0] == 0x01
    byte_count = pdu[1]
    assert byte_count == (quantity + 7) // 8
    bits = []
    for i in range(quantity):
        bits.append(bool(pdu[2 + i // 8] & (1 << (i % 8))))
    return bits


@pytest.fixture(scope="module")
def seeded_engine():
    buffer = SQLiteBuffer(real_engine_placeholder_db())
    buffer.initialize()
    buffer.upsert_normalized(FIRST_METER, "totalizer", 42.5, "m3")
    buffer.upsert_normalized(FIRST_METER, "battery", 3.63, "V")
    buffer.upsert_normalized(FIRST_METER, "online", 1.0, "binary")
    buffer.upsert_normalized(FIRST_METER, "alarm", 0.0, "binary")
    engine = real_engine
    live = engine.live(buffer)
    buffer.close()
    return engine, live


def real_engine_placeholder_db() -> str:
    # reuse the module real DB path but in a temp location to stay read-only
    import tempfile

    return f"{tempfile.gettempdir()}/gateway-modbus-test.db"


async def _run_server(engine, live):
    server = ModbusTCPServer("127.0.0.1", 0, engine)
    await server.start()
    port = server._server.sockets[0].getsockname()[1]
    server.sync(live)
    return server, port


def test_read_holding_float32(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x03, struct.pack(">HH", 0, 2))
            assert _real_registers(pdu) == pytest.approx(42.5)
        finally:
            await server.stop()

    asyncio.run(case())


def test_read_battery_applies_scale(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            # 40011 -> pdu 10 (battery, scale 0.001 -> 3.63 V encodes 0.00363)
            _, pdu = await _request(port, 1, 0x03, struct.pack(">HH", 10, 2))
            assert _real_registers(pdu) == pytest.approx(0.00363)
        finally:
            await server.stop()

    asyncio.run(case())


def test_read_coils(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x01, struct.pack(">HH", 0, 2))
            assert _read_bits(pdu, 2) == [True, False]
        finally:
            await server.stop()

    asyncio.run(case())


def test_write_to_r_point_rejected(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x06, struct.pack(">HH", 0, 7))
            _expect_exception(pdu, 0x06, 0x02)
            _, pdu = await _request(port, 1, 0x05, struct.pack(">HH", 0, 0xFF00))
            _expect_exception(pdu, 0x05, 0x02)
        finally:
            await server.stop()

    asyncio.run(case())


def test_write_coil_bad_value(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x05, struct.pack(">HH", 0, 0x0001))
            _expect_exception(pdu, 0x05, 0x03)
        finally:
            await server.stop()

    asyncio.run(case())


def test_read_out_of_range(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x03, struct.pack(">HH", 100, 2))
            _expect_exception(pdu, 0x03, 0x02)
        finally:
            await server.stop()

    asyncio.run(case())


def test_unsupported_function(seeded_engine):
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 1, 0x04, struct.pack(">HH", 0, 2))
            _expect_exception(pdu, 0x04, 0x01)
        finally:
            await server.stop()

    asyncio.run(case())


def test_real_config_unknown_unit_rejected(seeded_engine):
    # the live config is multi-unit (12 meters) - a bogus unit id must be
    # rejected rather than echo-served to the wrong meter
    engine, live = seeded_engine

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 77, 0x03, struct.pack(">HH", 0, 2))
            _expect_exception(pdu, 0x03, 0x02)
        finally:
            await server.stop()

    asyncio.run(case())


def test_multi_unit_routing_and_unknown_unit():
    engine = _two_meter_engine()
    live = {"A": {"totalizer": 111.0}, "B": {"totalizer": 222.0}}

    async def case():
        server, port = await _run_server(engine, live)
        try:
            _, pdu = await _request(port, 10, 0x03, struct.pack(">HH", 0, 2))
            assert _real_registers(pdu) == pytest.approx(111.0)
            _, pdu = await _request(port, 20, 0x03, struct.pack(">HH", 0, 2))
            assert _real_registers(pdu) == pytest.approx(222.0)
            _, pdu = await _request(port, 30, 0x03, struct.pack(">HH", 0, 2))
            _expect_exception(pdu, 0x03, 0x02)
        finally:
            await server.stop()

    asyncio.run(case())