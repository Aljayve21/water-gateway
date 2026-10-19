"""OS-level UDP broadcast isolation test (no bacpypes3).

Verifies whether Windows itself delivers broadcast datagrams on this machine:
  - a plain listener bound 0.0.0.0:47808 (what the gateway's BACnet stack does)
  - a plain sender: unicast to 127.0.0.1, unicast to LAN IP, broadcast to
    <LAN>/255

Run:  .\\.venv\\Scripts\\python.exe scripts\\udp_broadcast_probe.py
"""

from __future__ import annotations

import asyncio
import socket
import sys

PORT = 47808


def lan_ip() -> str | None:
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))
        return s.getsockname()[0]
    except OSError:
        return None
    finally:
        s.close()


async def main() -> int:
    ip = lan_ip()
    broadcast = ".".join(ip.split(".")[:-1] + ["255"]) if ip else None
    print(f"LAN IP: {ip}  broadcast: {broadcast}")

    loop = asyncio.get_running_loop()
    received: list[tuple[str, bytes]] = []

    class Proto(asyncio.DatagramProtocol):
        def datagram_received(self, data: bytes, addr) -> None:
            received.append((str(addr), data))
            print(f"  <- got {len(data)}B from {addr}")

    listener, _ = await loop.create_datagram_endpoint(
        Proto, local_addr=("0.0.0.0", PORT)
    )
    print(f"listening on 0.0.0.0:{PORT}")

    def send(dest: str, payload: bytes, bcast: bool) -> None:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        s.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1 if bcast else 0)
        try:
            s.sendto(payload, (dest, PORT))
            print(f"  -> sent to {dest}:{PORT} ({len(payload)}B)")
        except OSError as exc:
            print(f"  !! send to {dest} failed: {exc}")
        finally:
            s.close()

    print("1) unicast 127.0.0.1")
    send("127.0.0.1", b"uni-loopback", False)
    await asyncio.sleep(0.5)

    if ip:
        print("2) unicast own LAN IP")
        send(ip, b"uni-lan", False)
        await asyncio.sleep(0.5)

        print("3) broadcast subnet")
        send(broadcast, b"bcast-lan", True)
        await asyncio.sleep(0.5)

        print("4) broadcast 255.255.255.255")
        send("255.255.255.255", b"bcast-global", True)
        await asyncio.sleep(0.5)

    listener.close()
    print(f"total received: {len(received)}")
    ok_broadcast = any(b"bcast" in d for _, d in received)
    if any(b"uni" in d for _, d in received) and ok_broadcast:
        print("PASS: OS delivers broadcast datagrams")
        return 0
    if any(b"uni" in d for _, d in received):
        print("FAIL: unicast OK but broadcast NOT delivered locally")
        return 2
    print("FAIL: nothing received at all")
    return 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
