"""Find BACnet devices on a subnet with unicast Who-Is.

Broadcast discovery does not cross routers, so a device on another subnet is
invisible to a Who-Is broadcast. Sending Who-Is straight at each address does
work, which is how commissioning tools find equipment on a routed network.

Read-only: Who-Is is a query and produces no side effects on the target.

    .\\.venv\\Scripts\\python.exe scripts\\bacnet_subnet_scan.py \\
        --subnet 192.168.0 --timeout 2 --concurrency 32

Then, for each device found:

    .\\.venv\\Scripts\\python.exe scripts\\bacnet_subnet_scan.py \\
        --subnet 192.168.0 --inventory 192.168.0.120
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bacpypes3.basetypes import ObjectIdentifier, PropertyIdentifier  # noqa: E402
from bacpypes3.ipv4.app import NormalApplication  # noqa: E402
from bacpypes3.local.device import DeviceObject  # noqa: E402
from bacpypes3.pdu import Address  # noqa: E402

BACNET_PORT = 47808
CLIENT_INSTANCE_BASE = 598000


class Prober:
    """One BACnet application reused across many unicast probes."""

    def __init__(self, bind_port: int, index: int):
        self._app = NormalApplication(
            DeviceObject(
                objectIdentifier=("device", CLIENT_INSTANCE_BASE + index),
                objectName="subnet-scanner",
                vendorIdentifier=15,
            ),
            Address(("0.0.0.0", bind_port)),
        )

    async def who_is(self, host: str, timeout: float, low: int, high: int):
        return await self._app.who_is(
            low, high, address=Address(f"{host}:{BACNET_PORT}"), timeout=timeout
        )

    async def read(self, host: str, objid, prop):
        return await self._app.read_property(
            Address(f"{host}:{BACNET_PORT}"), objid, prop
        )

    def close(self) -> None:
        self._app.close()


async def scan(args) -> int:
    prober = Prober(args.client_port, 1)
    sem = asyncio.Semaphore(args.concurrency)
    found: dict[str, dict] = {}

    async def probe(last_octet: int) -> None:
        host = f"{args.subnet}.{last_octet}"
        async with sem:
            try:
                iams = await prober.who_is(host, args.timeout, 0, 4194302)
            except Exception:
                return
            for apdu in iams or []:
                found[host] = {
                    "ip": host,
                    "device_instance": apdu.iAmDeviceIdentifier[1],
                    "source": str(apdu.pduSource),
                    "max_apdu": getattr(apdu, "maxAPDULengthAccepted", None),
                    "vendor_id": getattr(apdu, "vendorIdentifier", None),
                }
                print(f"  FOUND {host}  device {apdu.iAmDeviceIdentifier[1]}")
                return

    print(f"scanning {args.subnet}.1-{args.subnet}.254 on UDP {BACNET_PORT} "
          f"({args.concurrency} at a time, {args.timeout}s timeout)")
    await asyncio.gather(*(probe(o) for o in range(1, 255)))
    prober.close()

    print(f"\n{len(found)} BACnet device(s) answered\n")
    for item in sorted(found.values(), key=lambda d: d["ip"]):
        print(f"  {item['ip']:<16} instance={item['device_instance']:<8} "
              f"vendor={item['vendor_id']} apdu={item['max_apdu']}")

    if args.out:
        Path(args.out).write_text(
            json.dumps({"subnet": args.subnet, "devices": list(found.values())}, indent=2),
            encoding="utf-8",
        )
        print(f"\nwrote {args.out}")
    return 0 if found else 1


async def inventory(args) -> int:
    """Read one device's object list with names, types and units."""
    prober = Prober(args.client_port, 2)
    host = args.inventory
    try:
        iams = await prober.who_is(host, args.timeout, 0, 4194302)
        if not iams:
            print(f"{host}: no I-Am - not a BACnet device, or wrong address")
            return 1
        instance = iams[0].iAmDeviceIdentifier[1]
        print(f"{host}: device {instance}\n")

        objlist = await prober.read(
            host, ObjectIdentifier(("device", instance)), "objectList"
        )
        entries = list(objlist) if not isinstance(objlist, int) else []
        print(f"object list: {len(entries)} entries\n")

        objects = []
        for entry in entries:
            # bacpypes3 ObjectIdentifier is a Sequence of (ObjectType, instance)
            # and exposes neither .type/.instance nor .objIdentifier in this
            # version, so index it rather than reaching for attributes.
            try:
                obj_type, obj_instance = entry[0], entry[1]
            except (TypeError, IndexError, KeyError):
                continue
            type_text = str(obj_type).split()[-1].strip(">")  # "<ObjectType: ai>"
            if type_text == "device":
                continue  # the device object itself
            objid = ObjectIdentifier((type_text, obj_instance))
            row = {"type": type_text, "instance": obj_instance}
            # `units` exists only on analog objects. Asking a binary object for
            # it returns unknown-property, which is expected, not a fault.
            wanted = [("name", "objectName"), ("value", "presentValue")]
            if type_text in ("ai", "av"):
                wanted.insert(1, ("units", "units"))
            for label, prop in wanted:
                try:
                    val = await prober.read(host, objid, PropertyIdentifier(prop))
                    row[label] = str(val)
                except Exception as exc:  # noqa: BLE001
                    row[label] = None
                    row.setdefault("errors", []).append(f"{prop}: {exc}")
            objects.append(row)
            shown = row.get("value")
            print(f"  {row['type']:<12} {row['instance']:<7} {row.get('name','')}"
                  f" = {shown} {row.get('units','') or ''}")

        if args.out:
            Path(args.out).write_text(json.dumps(
                {"ip": host, "device_instance": instance, "objects": objects},
                indent=2), encoding="utf-8")
            print(f"\nwrote {args.out}")
        return 0
    finally:
        prober.close()


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--subnet", default="192.168.0")
    parser.add_argument("--timeout", type=float, default=2.0)
    parser.add_argument("--concurrency", type=int, default=32)
    parser.add_argument("--client-port", type=int, default=47830)
    parser.add_argument("--inventory", default=None,
                        help="read one device's object inventory instead of scanning")
    parser.add_argument("--out", default=None)
    args = parser.parse_args()

    if args.inventory:
        return await inventory(args)
    return await scan(args)


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
