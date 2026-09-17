"""Minimal Modbus TCP server for the gateway data map.

The installed pymodbus datastore is mid-migration (the legacy device context
returns ``DEVICE_BUSY`` and the simulator context packs coils 16-per-register),
so the gateway ships its own small, spec-exact Modbus TCP server instead. It
implements the PDU subset the BMS needs:

  * FC01 read coils
  * FC03 read holding registers (float32 spans -> 2 registers)
  * FC05 write single coil
  * FC06 write single register
  * FC16 write multiple registers

Requests are demultiplexed by MBAP unit id (one unit per meter). Cross-table
reads (FC02/FC04) return ``ILLEGAL_FUNCTION``. Writes to read-only points
(currently every point is ``rw: R``) return ``ILLEGAL_DATA_ADDRESS``. The
``image`` is refreshed from the local buffer each extraction cycle; nothing in
this module touches the office database.
"""

from __future__ import annotations

import asyncio
import logging
import struct

from .map import CompiledMap, MappingEngine, decode_float32

log = logging.getLogger("gateway.modbus")

_ILLEGAL_FUNCTION = 0x01
_ILLEGAL_DATA_ADDRESS = 0x02
_ILLEGAL_DATA_VALUE = 0x03

FC_READ_COILS = 0x01
FC_READ_DISCRETE = 0x02
FC_READ_HOLDING = 0x03
FC_READ_INPUT = 0x04
FC_WRITE_COIL = 0x05
FC_WRITE_REGISTER = 0x06
FC_WRITE_MULTI_REGISTERS = 0x16

_MAX_READ_COILS = 2000
_MAX_READ_HOLDING = 125

_DEBUG_REQUESTS = False


class ModbusError(Exception):
    def __init__(self, code: int) -> None:
        self.code = code
        super().__init__(f"modbus exception 0x{code:02x}")


class ModbusTCPServer:
    """Asyncio Modbus TCP server backed by a compiled :class:`MappingEngine`."""

    def __init__(
        self, host: str, port: int, engine: MappingEngine
    ) -> None:
        self.host = host
        self.port = port
        self.engine = engine
        self.map: CompiledMap = engine.map

        # PDU-address image: unit -> {"holding"|"coil": {pdu_address: value}}
        self.holding: dict[int, dict[int, int]] = {}
        self.coils: dict[int, dict[int, bool]] = {}
        for unit_id, meter_id in self.map.unit_to_meter.items():
            self.holding[unit_id] = {}
            self.coils[unit_id] = {}
            for point in self.map.meters[meter_id].points.values():
                if point.modbus is None:
                    continue
                mb = point.modbus
                if mb.register_type == "coil":
                    self.coils[unit_id].setdefault(mb.pdu_address, False)
                elif mb.register_type == "holding":
                    size = 2 if mb.dtype in ("float32", "int32", "uint32") else 1
                    for i in range(size):
                        self.holding[unit_id].setdefault(mb.pdu_address + i, 0)

        self._server: asyncio.AbstractServer | None = None
        self._connections: set[asyncio.Task] = set()

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        log.info("modbus tcp listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        for task in list(self._connections):
            task.cancel()
        if self._connections:
            await asyncio.gather(*self._connections, return_exceptions=True)
            self._connections.clear()

    @property
    def listening(self) -> bool:
        return self._server is not None

    def sync(self, live: dict[str, dict[str, float]]) -> None:
        """Refresh the register/coil image from canonical live values."""
        for meter_id, values in live.items():
            meter = self.map.meters.get(meter_id)
            if meter is None:
                continue
            unit = meter.modbus_unit_id
            for point_id, value in values.items():
                point = meter.points.get(point_id)
                if point is None or point.modbus is None:
                    continue
                mb = point.modbus
                encoded = self.engine.modbus_int(mb, value)
                if mb.register_type == "coil":
                    self.coils[unit][mb.pdu_address] = bool(encoded)
                elif mb.register_type == "holding":
                    if mb.dtype in ("float32", "int32", "uint32"):
                        a, b = encoded  # type: ignore[misc]
                        self.holding[unit][mb.pdu_address] = a
                        self.holding[unit][mb.pdu_address + 1] = b
                    else:
                        self.holding[unit][mb.pdu_address] = int(encoded)

    # ── connection / framing ─────────────────────────────────────────────
    async def _handle_connection(self, reader, writer) -> None:
        task = asyncio.current_task()
        if task is not None:
            self._connections.add(task)
        try:
            while True:
                header = await reader.readexactly(7)
                tid, _pid, length, unit_id = struct.unpack(">HHHB", header)
                if _DEBUG_REQUESTS:
                    log.info("req header %s len=%d unit=%d", header.hex(" "), length, unit_id)
                if length < 2 or length > 254:
                    return
                pdu = await reader.readexactly(length - 1)
                response = self._serve(unit_id, pdu, tid)
                if response is None:
                    return
                writer.write(response)
                await writer.drain()
        except (asyncio.IncompleteReadError, ConnectionResetError):
            pass
        except asyncio.CancelledError:
            pass
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass
            if task is not None:
                self._connections.discard(task)

    # ── request dispatch ─────────────────────────────────────────────────
    def _serve(self, unit_id: int, pdu: bytes, tid: int) -> bytes | None:
        fc = pdu[0]
        data = pdu[1:]
        try:
            if fc in (FC_READ_COILS, FC_READ_HOLDING):
                body = self._read(fc, unit_id, data)
            elif fc in (FC_WRITE_COIL, FC_WRITE_REGISTER, FC_WRITE_MULTI_REGISTERS):
                body = self._write(fc, unit_id, data)
            else:
                raise ModbusError(_ILLEGAL_FUNCTION)
            pdu_out = bytes([fc]) + body
        except ModbusError as exc:
            pdu_out = bytes([fc | 0x80, exc.code])
        return struct.pack(">HHHB", tid, 0, 1 + len(pdu_out), unit_id) + pdu_out

    def _resolve_unit(self, unit_id: int) -> int:
        """Echo a single configured unit for any requested id (common when the
        gateway appears as one Modbus device); otherwise require membership."""
        if len(self.map.unit_to_meter) == 1:
            return next(iter(self.map.unit_to_meter))
        if unit_id not in self.map.unit_to_meter:
            raise ModbusError(_ILLEGAL_DATA_ADDRESS)
        return unit_id

    def _read(self, fc: int, unit_id: int, data: bytes) -> bytes:
        unit = self._resolve_unit(unit_id)
        address, quantity = struct.unpack(">HH", data)
        if fc == FC_READ_HOLDING:
            if quantity == 0 or quantity > _MAX_READ_HOLDING:
                raise ModbusError(_ILLEGAL_DATA_VALUE)
            block = self.holding[unit]
            if address + quantity > (max(block, default=-1) + 1):
                raise ModbusError(_ILLEGAL_DATA_ADDRESS)
            payload = b"".join(
                struct.pack(">H", block.get(address + i, 0)) for i in range(quantity)
            )
            return bytes([len(payload)]) + payload
        # FC1 read coils
        if quantity == 0 or quantity > _MAX_READ_COILS:
            raise ModbusError(_ILLEGAL_DATA_VALUE)
        block = self.coils[unit]
        if address + quantity > (max(block, default=-1) + 1):
            raise ModbusError(_ILLEGAL_DATA_ADDRESS)
        bits = [block.get(address + i, False) for i in range(quantity)]
        payload = bytearray((quantity + 7) // 8)
        for i, bit in enumerate(bits):
            if bit:
                payload[i // 8] |= 1 << (i % 8)
        return bytes([len(payload)]) + bytes(payload)

    def _write(self, fc: int, unit_id: int, data: bytes) -> bytes:
        unit = self._resolve_unit(unit_id)
        if fc == FC_WRITE_COIL:
            address, raw = struct.unpack(">HH", data)
            if raw not in (0x0000, 0xFF00):
                raise ModbusError(_ILLEGAL_DATA_VALUE)
            if address not in self.coils[unit] or self._rw(unit, address) != "RW":
                raise ModbusError(_ILLEGAL_DATA_ADDRESS)
            self.coils[unit][address] = raw == 0xFF00
            return struct.pack(">HH", address, raw)
        if fc == FC_WRITE_REGISTER:
            address, value = struct.unpack(">HH", data)
            if address not in self.holding[unit] or self._rw(unit, address) != "RW":
                raise ModbusError(_ILLEGAL_DATA_ADDRESS)
            self.holding[unit][address] = value
            return struct.pack(">HH", address, value)
        # FC16 write multiple registers
        address, quantity, byte_count = struct.unpack(">HHB", data[:5])
        if byte_count != quantity * 2:
            raise ModbusError(_ILLEGAL_DATA_VALUE)
        values = struct.unpack(f">{quantity}H", data[5 : 5 + byte_count])
        for i, value in enumerate(values):
            addr = address + i
            if addr not in self.holding[unit] or self._rw(unit, addr) != "RW":
                raise ModbusError(_ILLEGAL_DATA_ADDRESS)
        for i, value in enumerate(values):
            self.holding[unit][address + i] = value
        return struct.pack(">HH", address, quantity)

    def _rw(self, unit: int, pdu_address: int) -> str:
        meter = self.map.unit_to_meter[unit]
        for point in self.map.meters[meter].points.values():
            if point.modbus is not None and point.modbus.pdu_address == pdu_address:
                return point.modbus.rw
        return "R"