"""Mock BACnet device: answers Who-Is with a correct I-Am.

Test helper only. Lets us positively verify scripts/office_probe.ps1 without
any real Innotech hardware, so a silent scan on the office server is
unambiguously a network problem rather than a broken script.

Usage:
    .\\.venv\\Scripts\\python.exe scripts\\mock_bacnet_device.py --instance 2101

Then, from another shell:
    powershell -ExecutionPolicy Bypass -File scripts\\office_probe.ps1
"""

import argparse
import socket
import struct
import sys

BACNET_PORT = 47808

# Unconfirmed-Request (0x10), I-Am (0x00), then the I-Am payload:
#   device object identifier: 0xC4 tag, type 0x0008 (device), instance
#   segmentation support (enumerated, 1 byte)
#   max APDU length accepted (unsigned, 2 bytes)
#   segmentation supported (boolean, 1 byte)
#   vendor ID (unsigned, 2 bytes)
I_AM_HEADER = bytes([0x10, 0x00, 0xC4, 0x00, 0x08])
I_AM_TAIL = bytes([0x02]) + struct.pack(">H", 1024) + bytes([0xC1, 0x01, 0x21]) + struct.pack(">H", 0)

# Unconfirmed-Request (0x10), service Who-Is (0x08).
WHO_IS = bytes([0x10, 0x08])


def build_i_am(instance: int) -> bytes:
    return I_AM_HEADER + struct.pack(">H", instance) + I_AM_TAIL


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--instance", type=int, default=2101, help="device instance to advertise")
    ap.add_argument("--port", type=int, default=BACNET_PORT)
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--count", type=int, default=0, help="exit after N Who-Is (0 = run forever)")
    ap.add_argument("--quiet", action="store_true", help="do not print each request")
    args = ap.parse_args()

    i_am = build_i_am(args.instance)
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    # Allow a broadcast Who-Is from another host to be received.
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind((args.host, args.port))

    print(f"mock BACnet device listening on {args.host}:{args.port}")
    print(f"  advertising device instance {args.instance}, max APDU 1024")
    print(f"  I-Am ({len(i_am)} bytes): {i_am.hex(' ')}")
    print("  waiting for Who-Is (ctrl+c to stop)")

    seen = 0
    try:
        while True:
            data, addr = sock.recvfrom(2048)
            is_whois = data[:2] == WHO_IS
            if not args.quiet:
                tag = "Who-Is" if is_whois else f"other service {data[:2].hex(' ')}"
                print(f"  <- {tag} from {addr[0]}:{addr[1]} ({len(data)} bytes)")
            if not is_whois:
                continue
            sock.sendto(i_am, addr)
            seen += 1
            if not args.quiet:
                print(f"  -> replied I-Am instance {args.instance} to {addr[0]}")
            if args.count and seen >= args.count:
                print(f"done after {seen} request(s)")
                return 0
    except KeyboardInterrupt:
        print(f"\nstopped after {seen} request(s)")
    finally:
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
