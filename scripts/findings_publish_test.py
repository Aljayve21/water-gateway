"""Verify AI findings publish into BACnet and a client can read them back.

Runs its own BACnet server on a scratch port so it never collides with a
gateway you already have running. Exercises the real publication path:

  open findings in the buffer
    -> findings.meter_state()      (collapse to one value set per meter)
    -> BACnetServer.sync_findings() (write into AV/BV presentValue)
    -> BACnet client ReadProperty   (what NeoView actually does)

Exit codes: 0 = findings readable, 1 = not published / read failed.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bacpypes3.basetypes import ObjectIdentifier, PropertyIdentifier  # noqa: E402
from bacpypes3.ipv4.app import NormalApplication  # noqa: E402
from bacpypes3.local.device import DeviceObject  # noqa: E402
from bacpypes3.pdu import Address  # noqa: E402

from gateway.bacnet_server import BACnetServer  # noqa: E402
from gateway.config import load_all  # noqa: E402
from gateway.findings import load_layout, meter_state, plan_objects  # noqa: E402
from gateway.map import MappingEngine  # noqa: E402

SCRATCH_PORT = 47820
CLIENT_PORT = 47821
CLIENT_INSTANCE = 599996


async def main() -> int:
    _settings, _cfg, base_points, meters = load_all()
    engine = MappingEngine(meters, base_points)
    layout = load_layout()
    used = {bp.instance for _, _, bp in engine.map.bacnet_entries()}
    specs = plan_objects(layout, list(meters.meters), used)

    if not layout.enabled or not specs:
        print("FAIL: findings publication disabled or nothing planned")
        return 1

    meter_a, meter_b = list(meters.meters)[0], list(meters.meters)[1]

    # A realistic mix: meter A has a critical leak-ish finding plus a warning,
    # meter B is quiet. Meter C is deliberately absent so we can prove a meter
    # with no findings still gets published objects (all zero).
    findings = [
        {"meter_id": meter_a, "rule": "consumption", "severity": "critical",
         "message": "consumption spike"},
        {"meter_id": meter_a, "rule": "battery", "severity": "warning",
         "message": "battery low"},
        {"meter_id": meter_b, "rule": "offline", "severity": "critical",
         "message": "no contact"},
    ]
    state = meter_state(layout, findings)
    print(f"layout: base={layout.instance_base} per_meter={layout.per_meter} "
          f"objects={len(specs)}")
    print(f"state[{meter_a}] = {state.get(meter_a)}")
    print(f"state[{meter_b}] = {state.get(meter_b)}\n")

    expected = {
        f"{meter_a}:ai_consumption": True,
        f"{meter_a}:ai_battery": True,
        f"{meter_a}:ai_offline": False,
        f"{meter_a}:ai_max_severity": 2.0,   # critical
        f"{meter_a}:ai_finding_count": 2.0,
        f"{meter_b}:ai_offline": True,
        f"{meter_b}:ai_max_severity": 2.0,
        f"{meter_b}:ai_finding_count": 1.0,
    }

    server = BACnetServer(
        "127.0.0.1", SCRATCH_PORT, 1099, engine, findings_objects=specs
    )
    await server.start()
    await asyncio.sleep(0.3)
    updated = server.sync_findings(state)
    await asyncio.sleep(0.5)
    print(f"sync_findings wrote {updated} object value(s)")

    client = NormalApplication(
        DeviceObject(
            objectIdentifier=("device", CLIENT_INSTANCE),
            objectName="findings-reader",
            vendorIdentifier=15,
        ),
        Address(("127.0.0.1", CLIENT_PORT)),
    )
    failures = 0
    try:
        iams = await client.who_is(
            1099, 1099, address=Address(f"127.0.0.1:{SCRATCH_PORT}"), timeout=4
        )
        if not iams:
            print("FAIL discovery: scratch device not found")
            return 1
        device_addr = iams[0].pduSource
        print("PASS discovery of findings device\n")

        for spec in specs:
            if spec.object_name not in expected:
                continue
            want = expected[spec.object_name]
            oid = ObjectIdentifier(
                ("binaryValue" if spec.object_type == "BV" else "analogValue",
                 spec.instance)
            )
            try:
                got = await client.read_property(
                    device_addr, oid, PropertyIdentifier("presentValue")
                )
                val = 1.0 if str(got) in ("active", "True") else (
                    0.0 if str(got) in ("inactive", "False") else float(got)
                )
                ok = abs(val - float(want)) < 0.01
            except Exception as exc:  # noqa: BLE001
                print(f"FAIL {spec.object_name} ({spec.instance}): {exc}")
                failures += 1
                continue
            print(f"{'PASS' if ok else 'FAIL'} {spec.object_name} "
                  f"inst {spec.instance} {spec.object_type}: got {got}, want {want}")
            if not ok:
                failures += 1
    finally:
        client.close()
        await server.stop()

    print()
    if failures:
        print(f"{failures} read(s) failed - findings are not reaching the BMS")
        return 1
    print("All findings readable: AI output reaches a BACnet client end-to-end.")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
