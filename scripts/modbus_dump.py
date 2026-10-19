"""Dump everything readable from a Modbus server into JSON, for inspection.

Modbus does not describe itself: a register block is just words. To make sense
of one you have to try the plausible encodings and look for values that look
like real measurements. This writes every interpretation side by side so a human
(or a later mapping step) can see which one is right.

Strictly read-only: FC01, FC03 and FC04 only.

    .\\.venv\\Scripts\\python.exe scripts\\modbus_dump.py \\
        --host 192.168.0.120 --units 0-16 --out innotech_modbus.json

Output shape:

    {
      "source":  {"host": ..., "port": ..., "scanned_at": ...},
      "summary": {"units_queried": 17, "units_alive": 1},
      "units":   [ { "unit_id": 2, "holding": {...}, "coils": ..., ... } ]
    }
"""

from __future__ import annotations

import argparse
import asyncio
import json
import socket
import struct
from datetime import datetime, timezone
from pathlib import Path

FC_COILS = 0x01
FC_HOLDING = 0x03
FC_INPUT = 0x04

MAX_HOLDING = 125  # protocol limit per read
MAX_COILS = 2000


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("closed mid-frame")
        buf += chunk
    return buf


def read_block(host: str, port: int, unit: int, fc: int, address: int, quantity: int,
               timeout: float):
    """One read request. Returns raw bytes, exception code, or an error string."""
    pdu = struct.pack(">BHH", fc, address, quantity)
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

    if body[0] & 0x80:
        return None, body[1], None
    byte_count = body[1]
    return body[2 : 2 + byte_count], None, None


def as_uint16(raw: bytes) -> list[int]:
    return list(struct.unpack(f">{len(raw) // 2}H", raw))


def as_int16(raw: bytes) -> list[int]:
    return [v - 0x10000 if v >= 0x8000 else v for v in as_uint16(raw)]


def as_uint32(raw: bytes) -> list[int]:
    return list(struct.unpack(f">{len(raw) // 4}I", raw))


def as_float32_abcd(raw: bytes) -> list[float]:
    """Big-endian float, high word first - the common Modbus float order."""
    n = len(raw) // 4
    words = as_uint16(raw)
    return [
        struct.unpack(">f", struct.pack(">HH", words[i * 2], words[i * 2 + 1]))[0]
        for i in range(n)
    ]


def as_float32_cdab(raw: bytes) -> list[float]:
    """Swapped-word float - the other widespread order."""
    n = len(raw) // 4
    words = as_uint16(raw)
    return [
        struct.unpack(">f", struct.pack(">HH", words[i * 2 + 1], words[i * 2]))[0]
        for i in range(n)
    ]


def as_ascii(raw: bytes) -> str:
    """Printable ASCII view; control characters become dots."""
    return "".join(chr(b) if 32 <= b < 127 else "." for b in raw)


def _clean(values: list[float]) -> list[float | None]:
    """JSON cannot carry NaN/Infinity; report them as null with a marker."""
    out = []
    for v in values:
        out.append(v if v == v and v not in (float("inf"), float("-inf")) else None)
    return out


async def scan_unit(args, unit: int) -> dict | None:
    """Probe one unit across the three read function codes."""
    loop = asyncio.get_running_loop()

    def _read(fc, address, quantity):
        return read_block(args.host, args.port, unit, fc, address, quantity,
                          args.timeout)

    holding_raw, h_exc, h_err = await loop.run_in_executor(
        None, _read, FC_HOLDING, args.address, min(args.quantity, MAX_HOLDING))
    if h_err or h_exc is not None:
        return None  # dead unit: nothing to record

    entry: dict = {"unit_id": unit, "holding": {}}

    if args.full:
        blocks = []
        # Read beyond the first block so nothing is truncated.
        for start in range(args.address, args.address + args.span,
                           min(args.quantity, MAX_HOLDING)):
            raw, exc, err = await loop.run_in_executor(
                None, _read, FC_HOLDING, start, min(args.quantity, MAX_HOLDING))
            if err or exc is not None:
                break
            blocks.append((start, raw))
        merged = b"".join(raw for _s, raw in blocks)
        entry["holding"]["base_address"] = args.address
    else:
        merged = holding_raw

    entry["holding"].update({
        "byte_count": len(merged),
        "registers_uint16": as_uint16(merged),
        "registers_int16": as_int16(merged),
        "registers_uint32": as_uint32(merged),
        "float32_abcd": _clean(as_float32_abcd(merged)),
        "float32_cdab": _clean(as_float32_cdab(merged)),
        "ascii": as_ascii(merged),
    })

    coil_raw, c_exc, c_err = await loop.run_in_executor(
        None, _read, FC_COILS, 0, min(16, MAX_COILS))
    entry["coils"] = (
        {"raw_hex": coil_raw.hex(), "bits": _bits(coil_raw)} if coil_raw is not None
        else {"exception": f"0x{c_exc:02x}"} if c_exc is not None
        else {"error": c_err}
    )

    input_raw, i_exc, i_err = await loop.run_in_executor(
        None, _read, FC_INPUT, args.address, min(args.quantity, MAX_HOLDING))
    entry["input_registers"] = (
        {"registers_uint16": as_uint16(input_raw),
         "registers_int16": as_int16(input_raw),
         "float32_abcd": _clean(as_float32_abcd(input_raw)),
         "ascii": as_ascii(input_raw)} if input_raw is not None
        else {"exception": f"0x{i_exc:02x}"} if i_exc is not None
        else {"error": i_err}
    )
    return entry


def _bits(raw: bytes) -> list[bool]:
    return [bool(b & (1 << i)) for i in range(len(raw) * 8)]


def parse_units(spec: str) -> range:
    if "-" in spec:
        lo, hi = spec.split("-", 1)
        return range(int(lo), int(hi) + 1)
    if "," in spec:
        nums = [int(x) for x in spec.split(",")]
        return range(min(nums), max(nums) + 1)
    return range(int(spec), int(spec) + 1)


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="192.168.0.120")
    parser.add_argument("--port", type=int, default=502)
    parser.add_argument("--units", default="0-16", help="e.g. 0-16 or 1,2,5-9")
    parser.add_argument("--address", type=int, default=0)
    parser.add_argument("--quantity", type=int, default=40)
    parser.add_argument("--span", type=int, default=60,
                        help="total registers to read when --full")
    parser.add_argument("--full", action="store_true",
                        help="read the whole span instead of one block")
    parser.add_argument("--timeout", type=float, default=4.0)
    parser.add_argument("--concurrency", type=int, default=8)
    parser.add_argument("--out", default="modbus_dump.json")
    args = parser.parse_args()

    units = parse_units(args.units)
    print(f"scanning {args.host}:{args.port} units {units.start}-{units.stop - 1} "
          f"({args.concurrency} at a time, {args.timeout}s timeout)")

    sem = asyncio.Semaphore(args.concurrency)
    results: dict[int, dict] = {}

    async def one(unit: int) -> None:
        async with sem:
            entry = await scan_unit(args, unit)
            if entry:
                results[unit] = entry
                print(f"  unit {unit:>3}: ALIVE")

    await asyncio.gather(*(one(u) for u in units))

    ordered = [results[u] for u in sorted(results)]
    payload = {
        "source": {
            "host": args.host,
            "port": args.port,
            "scanned_at": datetime.now(timezone.utc).isoformat(),
            "read_only": True,
            "function_codes_used": ["FC01", "FC03", "FC04"],
        },
        "summary": {
            "units_queried": len(units),
            "units_alive": len(ordered),
            "alive_unit_ids": sorted(results),
        },
        "units": ordered,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    print(f"\nwrote {out_path} ({out_path.stat().st_size} bytes)")
    print(f"units alive: {sorted(results) or 'none'}")

    for entry in ordered:
        h = entry["holding"]
        print(f"\n--- unit {entry['unit_id']} ---")
        print(f"  uint16 : {h['registers_uint16'][:20]}")
        print(f"  float32 ABCD: {h['float32_abcd'][:10]}")
        print(f"  ascii  : {h['ascii'][:60]!r}")
    return 0 if ordered else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
