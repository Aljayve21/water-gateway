"""BACnet discovery self-test for the water-gateway.

IMPORTANT: this script starts its **own** BACnet/IP server, so a PASS here
proves only that bacpypes3 can answer a Who-Is. It says **nothing** about a
gateway that is already running - which is the thing you usually care about.
Running it while a real gateway holds port 47808 is misleading, because the
server created here is the one answering.

To test the **running** gateway instead, use a client that starts nothing:

    .\\.venv\\Scripts\\python.exe scripts\\bacnet_client_test.py

This script is still useful on its own: it verifies device/point layout
end-to-end without touching live state. It sends:

  1. a **unicast** Who-Is to 127.0.0.1:<BACNET_BIND_PORT>  - proves the
     server answers on the configured port;
  2. a **broadcast** Who-Is from the LAN interface            - the same
     message a BMS discovery scan sends.

Run from the project root:
    .\\.venv\\Scripts\\python.exe scripts\\whois_check.py

Exit codes: 0 = all pass, 1 = unicast failed (server/port problem),
2 = broadcast failed (firewall/subnet problem - unicast passed).
"""

from __future__ import annotations

import asyncio
import socket
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from bacpypes3.ipv4.app import NormalApplication  # noqa: E402
from bacpypes3.local.device import DeviceObject  # noqa: E402
from bacpypes3.pdu import Address, IPv4Address  # noqa: E402

from gateway.bacnet_server import BACnetServer  # noqa: E402
from gateway.config import load_all  # noqa: E402
from gateway.map import MappingEngine  # noqa: E402

PROBE_UNICAST_INSTANCE = 599999
PROBE_BROADCAST_INSTANCE = 599998


def lan_ip() -> str | None:
    """Best-effort primary LAN IPv4 (no packets leave the machine)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


def _probe(instance: int, name: str, bind) -> NormalApplication:
    device = DeviceObject(
        objectIdentifier=("device", instance),
        objectName=name,
        vendorIdentifier=15,
    )
    # bind may be a pre-built Address/IPv4Address (CIDR-capable) or a plain
    # (host, port) tuple for the generic factory.
    return NormalApplication(device, bind if isinstance(bind, Address) else Address(bind))


def _warn_if_gateway_running(port: int) -> None:
    """Print a loud caveat when another process already holds the port.

    Without this, an operator debugging a live discovery problem runs this
    script, sees PASS, and concludes the BMS path works - when in fact the
    PASS came from a throwaway server this script created.
    """
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.settimeout(0.5)
    busy_tcp = False
    try:
        probe.connect(("127.0.0.1", port))
        busy_tcp = True
    except OSError:
        busy_tcp = False
    finally:
        probe.close()

    udp = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        udp.bind(("0.0.0.0", port))
        busy_udp = False
    except OSError:
        busy_udp = True
    finally:
        udp.close()

    if busy_tcp or busy_udp:
        print(
            f"WARNING: something is already listening on port {port}. A live\n"
            "         gateway may be running. Results below describe the\n"
            "         temporary server THIS script starts, not that gateway.\n"
            "         To test the running gateway use scripts/bacnet_client_test.py"
        )


async def main() -> int:
    settings, _gateway_config, base_points, meters = load_all()
    host = settings.bacnet_bind_ip
    port = settings.bacnet_bind_port
    instance = settings.bacnet_device_instance
    print(f".env: BACNET_BIND_IP={host} BACNET_BIND_PORT={port} BACNET_DEVICE_INSTANCE={instance}")

    # A live gateway already on this port would answer instead of us, so the
    # results would not describe the server this script starts.
    _warn_if_gateway_running(port)

    engine = MappingEngine(meters, base_points)
    server = BACnetServer(host, port, instance, engine)
    await server.start()
    try:
        # ---- 1) Unicast Who-Is: does the device answer on the port? ----
        probe = _probe(
            PROBE_UNICAST_INSTANCE, "whois-probe-local", ("127.0.0.1", 0)
        )
        try:
            iams = await probe.who_is(
                instance, instance, address=Address(f"127.0.0.1:{port}"), timeout=3
            )
            if iams:
                apdu = iams[0]
                print(
                    f"PASS unicast: I-Am from {apdu.pduSource} "
                    f"device {apdu.iAmDeviceIdentifier[1]}"
                )
            else:
                print(f"FAIL unicast: no I-Am on 127.0.0.1:{port} - server not answering")
                return 1
        finally:
            probe.close()

        # ---- 2) Broadcast Who-Is from the LAN interface (BMS discovery) ----
        # The server only ever *receives* broadcast Who-Is (it replies I-Am
        # unicast to the source), so only the probe needs a broadcast-capable
        # binding: a CIDR address gives bacpypes3 the subnet broadcast address.
        # Adjust the prefix length if your LAN is not a /24.
        ip = lan_ip()
        if not ip:
            print("SKIP broadcast: no LAN IPv4 address found")
            return 0
        cidr = f"{ip}/24"
        # A real (non-ephemeral) port is required: bacpypes3 disables broadcast
        # for port-0 bindings. 47810 keeps clear of the server's own 47808.
        probe_addr = IPv4Address(f"{cidr}:47810")
        lprobe = _probe(
            PROBE_BROADCAST_INSTANCE, "whois-probe-lan", probe_addr
        )
        try:
            # bacpypes3 broadcasts to (subnet_broadcast, OUR_PORT) by default;
            # BMS peers all sit on the gateway port, so aim the probe's
            # broadcast there (same as a real BMS on 47808 would).
            lprobe.normal.server.broadcast_address = (
                probe_addr.addrBroadcastTuple[0],
                port,
            )
            print(
                f"probe bound {cidr}:47810, broadcast target "
                f"{probe_addr.addrBroadcastTuple[0]}:{port}"
            )
            iams = await lprobe.who_is(instance, instance, timeout=3)
            if iams:
                apdu = iams[0]
                print(
                    f"PASS broadcast: I-Am from {apdu.pduSource} "
                    f"device {apdu.iAmDeviceIdentifier[1]} "
                    f"(LAN {ip} - BMS discovery will see this device)"
                )
                return 0
            print(
                f"WARN broadcast: no I-Am via {ip}. Unicast works, so config is "
                f"correct; check firewall/subnet broadcast routing."
            )
            return 2
        finally:
            lprobe.close()
    finally:
        await server.stop()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
