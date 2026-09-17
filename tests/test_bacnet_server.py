"""BACnet/IP server tests: object tree, presentValue sync, device instance."""

from __future__ import annotations

import asyncio

from bacpypes3.object import DeviceObject  # noqa: F401  (proves stack import)
from bacpypes3.primitivedata import ObjectIdentifier

from gateway.bacnet_server import BACnetServer
from gateway.map import MappingEngine
from gateway.store import SQLiteBuffer
from gateway.config import load_all

FIRST_METER = "026026060001"


def _engine_and_buffer(tmp_path):
    buffer = SQLiteBuffer(tmp_path / "bac.db")
    buffer.initialize()
    buffer.upsert_normalized(FIRST_METER, "totalizer", 12.75, "m3")
    buffer.upsert_normalized(FIRST_METER, "online", 1.0, "binary")
    buffer.upsert_normalized(FIRST_METER, "alarm", 0.0, "binary")
    engine = MappingEngine(load_all()[3], load_all()[2])
    return engine, buffer


def test_object_tree_and_device(tmp_path):
    engine, buffer = _engine_and_buffer(tmp_path)

    async def case():
        server = BACnetServer("127.0.0.1", 47990, 601, engine)
        await server.start()
        try:
            assert len(server.objects) == 108  # 12 meters x 9 points
            assert f"{FIRST_METER}:totalizer" in server.objects
            assert f"{FIRST_METER}:online" in server.objects
            dev_id = server._device.objectIdentifier
            assert isinstance(dev_id, ObjectIdentifier)
            assert dev_id[1] == 601
        finally:
            await server.stop()
            server.sync({})

    asyncio.run(case())


def test_present_value_sync_updates_objects(tmp_path):
    engine, buffer = _engine_and_buffer(tmp_path)
    live = engine.live(buffer)

    async def case():
        server = BACnetServer("127.0.0.1", 47991, 601, engine)
        await server.start()
        try:
            server.sync(live)
            await asyncio.sleep(0.4)
            totalizer = server.objects[f"{FIRST_METER}:totalizer"]
            online = server.objects[f"{FIRST_METER}:online"]
            alarm = server.objects[f"{FIRST_METER}:alarm"]
            assert float(await totalizer.read_property("presentValue")) == 12.75
            assert str(await online.read_property("presentValue")) == "active"
            assert str(await alarm.read_property("presentValue")) == "inactive"
        finally:
            await server.stop()

    asyncio.run(case())


def test_stop_closes_cleanly(tmp_path):
    engine, buffer = _engine_and_buffer(tmp_path)

    async def case():
        server = BACnetServer("127.0.0.1", 47992, 601, engine)
        await server.start()
        await server.stop()

    asyncio.run(case())