"""Modbus TCP client test: proves a real BMS can read the gateway over Modbus.

The Modbus server is the other half of the protocol and has no client test yet.
This drives it the way a BMS driver does, straight down the wire with hand-built
MBAP/PDU frames rather than a library, so the framing itself is under test and
not just the library's idea of it.

Covers:
  1. FC03 read holding registers on every unit id (1-12)
  2. float32 decode against the value the server was fed
  3. FC01 read coils (online / alarm)
  4. a foreign unit id -> exception, and a bad address -> exception
  5. write to a read-only point -> exception (proves the map is enforced)

Exit codes: 0 = all pass, 1 = failures.
"""

from __future__ import annotations

import asyncio
import socket
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import load_all  # noqa: E402
from gateway.map import MappingEngine  # noqa: E402
from gateway.modbus_server import ModbusTCPServer  # noqa: E402

SCRATCH_PORT = 41502
PASSED = 0
FAILED = 0


def check(name: str, ok: bool, detail: str = "") -> None:
    global PASSED, FAILED
    if ok:
        PASSED += 1
        print(f"  PASS {name}")
    else:
        FAILED += 1
        print(f"  FAIL {name}{f' :: {detail}' if detail else ''}")


def request(unit: int, pdu: bytes, txn: int = 1) -> tuple[int, bytes]:
    """Send one Modbus TCP request. Returns (function_code, response_pdu)."""
    frame = struct.pack(">HHHB", txn, 0, len(pdu) + 1, unit) + pdu
    with socket.create_connection(("127.0.0.1", SCRATCH_PORT), timeout=5) as sock:
        sock.sendall(frame)
        header = _recv_exact(sock, 7)
        _tid, _pid, length, _unit = struct.unpack(">HHHB", header)
        pdu = _recv_exact(sock, length - 1)
        return pdu[0], pdu


def _recv_exact(sock: socket.socket, count: int) -> bytes:
    buf = b""
    while len(buf) < count:
        chunk = sock.recv(count - len(buf))
        if not chunk:
            raise ConnectionError("connection closed mid-frame")
        buf += chunk
    return buf


def read_holding(unit: int, address: int, quantity: int):
    fc, pdu = request(unit, struct.pack(">BHH", 0x03, address, quantity))
    if fc & 0x80:
        return None, pdu[1]
    byte_count = pdu[1]
    words = struct.unpack(f">{byte_count // 2}H", pdu[2 : 2 + byte_count])
    return words, None


def read_coils(unit: int, address: int, quantity: int):
    fc, pdu = request(unit, struct.pack(">BHH", 0x01, address, quantity))
    if fc & 0x80:
        return None, pdu[1]
    byte_count = pdu[1]
    return pdu[2 : 2 + byte_count], None


# The server runs in this event loop, so the blocking socket work has to happen
# off-thread or it would stall the loop and the server could never reply.
async def a_read_holding(unit: int, address: int, quantity: int):
    return await asyncio.to_thread(read_holding, unit, address, quantity)


async def a_read_coils(unit: int, address: int, quantity: int):
    return await asyncio.to_thread(read_coils, unit, address, quantity)


async def a_request(unit: int, pdu: bytes):
    return await asyncio.to_thread(request, unit, pdu)


def decode_float(high: int, low: int) -> float:
    """ABCD, big byte order + big word order - as meters.yaml declares."""
    return struct.unpack(">f", struct.pack(">HH", high, low))[0]


async def main() -> int:
    _settings, _cfg, base_points, meters = load_all()
    engine = MappingEngine(meters, base_points)
    meter_ids = list(meters.meters)

    server = ModbusTCPServer("127.0.0.1", SCRATCH_PORT, engine)
    await server.start()

    # Feed every meter distinctive values so a mis-routed unit is obvious:
    # unit N gets totalizer = 1000 + N.
    live = {}
    for i, meter_id in enumerate(meter_ids, start=1):
        live[meter_id] = {
            "totalizer": 1000.0 + i,
            "pressure": 0.5 * i,
            "reverse": 0.0,
            "pulse1": 1.0 * i,
            "pulse2": 2.0 * i,
            "battery": 3.9,
            "signal": -70.0 - i,
            "online": 1.0,
            "alarm": 0.0,
        }
    server.sync(live)

    print(f"modbus server on 127.0.0.1:{SCRATCH_PORT}, {len(meter_ids)} unit(s)\n")

    # ---- 1) every unit id answers on the same register block ----
    print("[1] read holding registers (FC03) on every unit")
    for i, meter_id in enumerate(meter_ids, start=1):
        unit = engine.map.meters[meter_id].modbus_unit_id
        words, exc = await a_read_holding(unit, 0, 14)
        if words is None:
            check(f"unit {unit} ({meter_id}) responds", False, f"exception 0x{exc:02x}")
            continue
        totalizer = decode_float(words[0], words[1])
        ok = abs(totalizer - (1000.0 + i)) < 0.01
        check(
            f"unit {unit} totalizer = {1000.0 + i}",
            ok,
            f"got {totalizer}",
        )
    print()

    # ---- 2) full decode of one meter's block ----
    print("[2] decode every float on unit 1")
    words, _exc = await a_read_holding(1, 0, 14)
    if words is None:
        check("unit 1 block readable", False)
    else:
        expected = {
            0: ("totalizer", 1001.0),
            2: ("pressure", 0.5),
            4: ("reverse", 0.0),
            6: ("pulse1", 1.0),
            8: ("pulse2", 2.0),
            10: ("battery", 3.9),
            12: ("signal", -71.0),
        }
        for offset, (label, want) in expected.items():
            got = decode_float(words[offset], words[offset + 1])
            check(f"{label} = {want}", abs(got - want) < 0.01, f"got {got}")
    print()

    # ---- 3) coils ----
    print("[3] read coils (FC01)")
    bits, exc = await a_read_coils(1, 0, 2)
    if bits is None:
        check("coils readable", False, f"exception 0x{exc:02x}")
    else:
        online = bool(bits[0] & 0x01)
        alarm = bool(bits[0] & 0x02)
        check("coil 0 online = active", online)
        check("coil 1 alarm = inactive", not alarm)
    print()

    # ---- 4) error handling ----
    print("[4] error handling")
    _words, exc = await a_read_holding(99, 0, 2)
    check(
        "unknown unit id -> exception 0x02",
        exc == 0x02,
        f"got {exc}",
    )
    _words, exc = await a_read_holding(1, 900, 2)
    check(
        "out-of-range address -> exception 0x02",
        exc == 0x02,
        f"got {exc}",
    )
    _bits, exc = await a_read_coils(1, 0, 9999)
    check("too many coils -> exception 0x03", exc == 0x03, f"got {exc}")
    print()

    # ---- 5) read-only enforcement ----
    print("[5] read-only enforcement")
    fc, pdu = await a_request(1, struct.pack(">BHH", 0x06, 0, 0x1234))
    check(
        "write to read-only register -> exception 0x02",
        bool(fc & 0x80) and pdu[1] == 0x02,
        f"fc=0x{fc:02x} code={pdu[1] if len(pdu) > 1 else None}",
    )

    await server.stop()

    print(f"\n==== {PASSED} passed, {FAILED} failed ====")
    return 1 if FAILED else 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
