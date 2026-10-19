"""BACnet client self-test: proves a real BMS can read the gateway's data.

This is the *client* half of the protocol. ``whois_check.py`` only proves the
gateway answers a discovery ping; a BMS commissioning tool needs much more:

  1. Who-Is            -> find device instance 1001
  2. ReadProperty      -> object-list, to prove the 108 objects are published
  3. ReadProperty      -> present-value on real analog/binary inputs, to prove
                          the data path decodes to sane numbers

It acts as an ordinary BACnet client using the same bacpypes3 stack a BMS
driver uses, so anything that passes here will interoperate with NeoView or
any other BACnet supervisor.

Run from the project root:
    .\\.venv\\Scripts\\python.exe scripts\\bacnet_client_test.py

Exit codes: 0 = full read path OK, 1 = discovery failed, 2 = device found but
reads failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bacpypes3.ipv4.app import NormalApplication  # noqa: E402
from bacpypes3.local.device import DeviceObject  # noqa: E402
from bacpypes3.basetypes import ObjectIdentifier, PropertyIdentifier  # noqa: E402
from bacpypes3.pdu import Address  # noqa: E402
from bacpypes3.primitivedata import Boolean, Real  # noqa: E402

from gateway.bacnet_server import BACnetServer  # noqa: E402
from gateway.config import load_all  # noqa: E402
from gateway.map import MappingEngine  # noqa: E402

CLIENT_INSTANCE = 599997
CLIENT_PORT = 47811

# Object type -> the property list a BMS reads after discovery.
OBJECT_LIST = "objectList"


def _client() -> NormalApplication:
    device = DeviceObject(
        objectIdentifier=("device", CLIENT_INSTANCE),
        objectName="bacnet-client-test",
        vendorIdentifier=15,
    )
    return NormalApplication(device, Address(("127.0.0.1", CLIENT_PORT)))


async def main() -> int:
    settings, _cfg, base_points, meters = load_all()
    host = settings.bacnet_bind_ip
    port = settings.bacnet_bind_port
    instance = settings.bacnet_device_instance

    engine = MappingEngine(meters, base_points)
    server = BACnetServer(host, port, instance, engine)

    # The server only publishes presentValue once a sync cycle has run, so feed
    # it a plausible reading per point. This exercises the real encode path -
    # the same engine call the poll cycle makes - rather than the zero defaults.
    sample = {}
    for meter_id, points in engine.map.meters.items():
        values = {}
        for point_id in points.points:
            if point_id == "totalizer":
                values[point_id] = 1234.5
            elif point_id == "pressure":
                values[point_id] = 2.75
            elif point_id == "battery":
                values[point_id] = 3.92
            elif point_id == "signal":
                values[point_id] = -71.0
            elif point_id in ("online", "alarm"):
                values[point_id] = 1.0 if point_id == "online" else 0.0
            else:
                values[point_id] = 0.0
        sample[meter_id] = values
    await server.start()
    await asyncio.sleep(0.2)
    server.sync(sample)
    await asyncio.sleep(0.4)

    expected_objects = len(engine.map.bacnet_entries())
    print(
        f"gateway: {host}:{port} instance {instance} "
        f"({expected_objects} objects published)\n"
    )

    client = _client()
    try:
        # ---- 1) discovery -------------------------------------------------
        # Unicast the Who-Is explicitly: the client is loopback-bound, so
        # bacpypes3 cannot derive a subnet broadcast address for it. A real BMS
        # on the LAN broadcasts instead (covered by whois_check.py).
        iams = await client.who_is(
            instance,
            instance,
            address=Address(f"127.0.0.1:{port}"),
            timeout=4,
        )
        if not iams:
            print("FAIL discovery: no I-Am - a BMS cannot find this device")
            return 1
        apdu = iams[0]
        device_addr = Address(apdu.pduSource)
        print(
            f"PASS discovery: device {apdu.iAmDeviceIdentifier[1]} "
            f"responded from {device_addr}"
        )

        # ---- 2) object list ----------------------------------------------
        objlist = await client.read_property(
            device_addr, ObjectIdentifier(("device", instance)), OBJECT_LIST
        )
        entries = list(objlist) if not isinstance(objlist, int) else []
        print(
            f"{'PASS' if len(entries) >= expected_objects else 'FAIL'} "
            f"object-list: device advertises {len(entries)} objects "
            f"(expected >= {expected_objects})"
        )

        # ---- 3) read real present values ---------------------------------
        analog = ObjectIdentifier(("analogInput", 1))   # 026026060001:totalizer -> 1234.5
        analog2 = ObjectIdentifier(("analogInput", 2))  # 026026060001:pressure  -> 2.75
        binary = ObjectIdentifier(("binaryInput", 8))   # 026026060001:online    -> active
        binary2 = ObjectIdentifier(("binaryInput", 9))  # 026026060001:alarm     -> inactive

        checks = [
            (analog, Real(1234.5), "totalizer (float)"),
            (analog2, Real(2.75), "pressure (float)"),
            (binary, Boolean(True), "online (binary)"),
            (binary2, Boolean(False), "alarm (binary)"),
        ]
        failures = 0
        for objid, want, label in checks:
            try:
                got = await client.read_property(
                    device_addr, objid, PropertyIdentifier("presentValue")
                )
                ok = abs(float(got) - float(want)) < 0.01
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL presentValue {objid} ({label}): {exc}")
                failures += 1
                continue
            status = "PASS" if ok else "FAIL"
            print(f"{status} presentValue {objid} ({label}): got {got}, want {want}")
            if not ok:
                failures += 1

        # ---- 4) object naming (what a BMS point list will show) ----------
        try:
            name = await client.read_property(
                device_addr, analog, PropertyIdentifier("objectName")
            )
            print(f"     objectName of {analog}: {name!r}")
        except Exception as exc:  # noqa: BLE001
            print(f"     objectName read failed: {exc}")

        if failures:
            print(f"\n{failures} read(s) failed - device found but data path broken")
            return 2
        print(
            "\nAll reads OK: a standards-compliant BACnet client can discover "
            "this gateway and pull decoded values."
        )
        return 0
    finally:
        client.close()
        await server.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
