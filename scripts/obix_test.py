"""End-to-end test of the oBIX client against a live mock oBIX server.

Starts scripts/obix_mock.py in-process, then drives the real client through
what a BMS integration does: list a folder, read points, check units, handle a
missing point, and write a value back.

Exit codes: 0 = all pass, 1 = failures, 2 = server failed to start.
"""

from __future__ import annotations

import asyncio
import importlib.util
import sys
import threading
from http.server import ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.obix import ObixClient, ObixError  # noqa: E402

PORT = 48080
passed = 0
failed = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global passed, failed
    if ok:
        passed += 1
        print(f"  PASS {name}")
    else:
        failed += 1
        print(f"  FAIL {name}{f' :: {detail}' if detail else ''}")


def start_mock() -> ThreadingHTTPServer:
    """Boot the mock server in a thread, reusing its real request handler."""
    spec = importlib.util.spec_from_file_location(
        "obix_mock", ROOT / "scripts" / "obix_mock.py"
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules["obix_mock"] = module
    spec.loader.exec_module(module)

    # Populate its STATE from the real meter map, exactly as its main() does.
    from gateway.config import load_all
    from gateway.map import MappingEngine

    _s, _c, base_points, meters = load_all()
    engine = MappingEngine(meters, base_points)
    for meter_id, meter in engine.map.meters.items():
        module.STATE[meter_id] = {
            pid: module.SAMPLE.get(pid, 0.0) for pid in meter.points
        }

    server = ThreadingHTTPServer(("127.0.0.1", PORT), module.Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


async def main() -> int:
    server = start_mock()
    base = f"http://127.0.0.1:{PORT}/obix"
    client = ObixClient(base, timeout=5)
    from gateway.config import load_all

    _s, _c, _bp, meters = load_all()
    meter = list(meters.meters)[0]
    print(f"mock oBIX on {base}, folder Ai/{meter}\n")

    try:
        # ---- single point reads ----
        print("[1] read points")
        p = await client.fetch_point(f"Ai/{meter}/totalizer")
        check("totalizer value", p.as_float == 1234.5, f"got {p.value}")
        check(
            "totalizer unit parsed",
            p.unit == "obix:units/cubic-meters",
            f"got {p.unit}",
        )
        check("point name", p.name.endswith("totalizer"), p.name)

        b = await client.fetch_point(f"Ai/{meter}/online")
        check("bool stays bool", b.value is True, f"got {b.value!r}")
        check("bool as float", b.as_float == 1.0, f"got {b.as_float}")

        sig = await client.fetch_point(f"Ai/{meter}/signal")
        check("negative value survives", sig.as_float == -71.0, f"got {sig.value}")

        # suffix handling: /value is optional in the client
        p2 = await client.fetch_point(f"Ai/{meter}/battery")
        check("battery value", p2.as_float == 3.92, f"got {p2.value}")

        # ---- folder listing ----
        print("\n[2] list folder")
        refs = await client.fetch_refs(f"Ai/{meter}")
        names = {r.name for r in refs}
        check("listing non-empty", len(refs) > 0, f"got {len(refs)}")
        check("listing has totalizer", "totalizer" in names, str(sorted(names)))
        totalizer = next((r for r in refs if r.name == "totalizer"), None)
        check(
            "listing value matches direct read",
            totalizer is not None and totalizer.as_float == 1234.5,
            str(totalizer),
        )

        # ---- error handling ----
        print("\n[3] error handling")
        try:
            await client.fetch_point(f"Ai/{meter}/no_such_point")
            check("missing point raises", False, "no exception")
        except ObixError as exc:
            check("missing point raises ObixError", True)
            check("error hides credentials", "password" not in str(exc).lower())

        try:
            ObixClient("http://127.0.0.1:1/obix", timeout=2).get_point("Ai/x/y")
            check("unreachable raises", False, "no exception")
        except ObixError:
            check("unreachable raises ObixError", True)

        # ---- write back (AI results pushed to the BMS) ----
        print("\n[4] write back")
        await client.push_point(f"Ai/{meter}/ai_max_severity", 2)
        back = await client.fetch_point(f"Ai/{meter}/ai_max_severity")
        check("written value readable", back.as_float == 2.0, f"got {back.value}")
        await client.push_point(f"Ai/{meter}/ai_leak", True)
        back2 = await client.fetch_point(f"Ai/{meter}/ai_leak")
        check("bool write readable", back2.value is True, f"got {back2.value!r}")

        # ---- auth is actually attached ----
        print("\n[5] auth")
        authed = ObixClient(base, username="user", password="secret", timeout=5)
        p3 = await authed.fetch_point(f"Ai/{meter}/totalizer")
        check("basic-auth client still reads", p3.as_float == 1234.5)

        # ---- bridge: read mapped meters + publish findings ----
        print("\n[6] bridge (read + publish)")
        from gateway.config import load_all as _load_all
        from gateway.findings import load_layout
        from gateway.map import MappingEngine
        from gateway.obix_sync import ObixBridge

        _s, _c, base_points, meters = _load_all()
        engine = MappingEngine(meters, base_points)
        bridge = ObixBridge(client, engine, meter_root="Ai", insights_root="Ai/Insights")

        class FakeBuffer:
            def __init__(self) -> None:
                self.values: dict = {}
                self.findings: list = []

            def upsert_normalized(self, meter_id, point_id, value, unit=""):
                self.values[(meter_id, point_id)] = value

            def open_findings(self, limit=500):
                return self.findings

        fake = FakeBuffer()
        stored = bridge.read_into(fake)
        check("bridge read stores values", stored >= 4, f"got {stored}")
        check(
            "bridge read totalizer",
            fake.values.get((meter, "totalizer")) == 1234.5,
            str(fake.values.get((meter, "totalizer"))),
        )

        layout = load_layout()
        rule = layout.binary_points[0].rule
        fake.findings = [{"meter_id": meter, "rule": rule, "severity": "warning"}]
        published = bridge.publish(fake, layout)
        check("bridge publish writes values", published > 0, f"got {published}")
        echoed = await client.fetch_point(f"Ai/Insights/{meter}/{rule}")
        check(
            "bridge publish readable",
            echoed.as_float == 1.0,
            f"got {echoed.value!r}",
        )
    finally:
        server.shutdown()
        server.server_close()

    print(f"\n==== {passed} passed, {failed} failed ====")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
