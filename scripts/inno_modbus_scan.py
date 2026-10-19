"""Read-only discovery of the Innotech Omni Modbus server.

Answers the real integration question: can we read data straight from the
Innotech platform, without oBIX and without touching NeoView's database?

A TCP connect only proves something is listening. This sweeps the unit-id
space and actually decodes register blocks, so we learn which units are alive,
which function codes are honoured, and what the numbers mean.

Strictly read-only: FC01/FC03/FC04 only, never a write.

    .\\.venv\\Scripts\\python.exe scripts\\inno_modbus_scan.py --host 192.168.0.120
"""

from __future__ import annotations

import argparse
import socket
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

FC_COILS = 0x01
FC_HOLDING = 0x03
FC_INPUT = 0x04


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("closed mid-frame")
        buf += chunk
    return buf


def request(host: str, port: int, unit: int, pdu: bytes, timeout: float):
    """Returns (words|bits, exception_code, error_string)."""
    try:
        with socket.create_connection((host, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            sock.sendall(struct.pack(">HHHB", 1, 0, len(pdu) + 1, unit) + pdu)
            header = _recv_exact(sock, 7)
            _t, _p, length, _u = struct.unpack(">HHHB", header)
            body = _recv_exact(sock, length - 1)
    except socket.timeout:
        return None, None, "timeout"
    except ConnectionError:
        return None, None, "closed"
    except OSError as exc:
        return None, None, str(exc)

    fc = body[0]
    if fc & 0x80:
        return None, body[1], None
    byte_count = body[1]
    return body[2 : 2 + byte_count], None, None


def as_floats(raw: bytes) -> list[float]:
    """Decode an even number of 16-bit words as ABCD float32 pairs."""
    count = len(raw) // 4
    if count == 0:
        return []
    words = struct.unpack(f">{count * 2}H", raw[: count * 4])
    return [
        struct.unpack(">f", struct.pack(">HH", words[i * 2], words[i * 2 + 1]))[0]
        for i in range(count)
    ]


def as_ints(raw: bytes) -> list[int]:
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.0.120")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--units", type=int, default=16)
    parser.add_argument("--start-unit", type=int, default=1)
    parser.add_argument("--address", type=int, default=0)
    parser.add_argument("--quantity", type=int, default=20)
    parser.add_argument("--timeout", type=float, default=5.0)
    args = parser.parse_args()

    print(f"scanning {args.host}:{args.port} units "
          f"{args.start_unit}-{args.start_unit + args.units - 1} "
          f"(FC03 addr {args.address} qty {args.quantity}, "
          f"{args.timeout}s timeout)\n")

    alive = []
    for unit in range(args.start_unit, args.start_unit + args.units):
        raw, exc, err = request(
            args.host, args.port, unit,
            struct.pack(">BHH", FC_HOLDING, args.address, args.quantity),
            args.timeout,
        )
        if err:
            print(f"  unit {unit:>3}: {err}")
            continue
        if exc is not None:
            print(f"  unit {unit:>3}: exception 0x{exc:02x}")
            continue

        words = as_ints(raw)
        floats = as_floats(raw)
        print(f"  unit {unit:>3}: OK  ints={words}")
        print(f"           floats={[round(f, 4) for f in floats]}")
        alive.append(unit)

        # A live unit: check which other function codes it honours.
        coils, cexc, cerr = request(
            args.host, args.port, unit, struct.pack(">BHH", FC_COILS, 0, 8),
            args.timeout,
        )
        inputs, iexc, ierr = request(
            args.host, args.port, unit,
            struct.pack(">BHH", FC_INPUT, args.address, args.quantity),
            args.timeout,
        )
        print(f"           FC01 coils: {cerr or (f'exception 0x{cexc:02x}' if cexc is not None else f'OK {coils.hex()}')}")
        print(f"           FC04 inputs: {ierr or (f'exception 0x{iexc:02x}' if iexc is not None else f'OK ints={as_ints(inputs)[:10]}')}")
        print()

    print("=" * 60)
    if alive:
        print(f"units that answered FC03: {alive}")
        print("Next: ask Innotech for the register map for these unit ids.")
    else:
        print("no unit answered. Possibilities:")
        print("  - Modbus server disabled or bound to another interface")
        print("  - units live under a different id range (try --start-unit 0)")
        print("  - this port is Innotech's Modbus *client* port, not a server")
    return 0 if alive else 1


if __name__ == "__main__":
    raise SystemExit(main())
