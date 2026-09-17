"""Mapping Engine: canonical points (base_points) -> wire protocol maps.

Compiles the DRAFT per-meter descriptors in config/meters.yaml into an
address-safe, protocol-ready representation and converts normalized point
values (from the buffer's ``normalized_value`` table) into wire values.

Only structural integrity is enforced here (address spans, unit ids, BACnet
instances). The actual register/coil addresses and object instances remain
[DRAFT] until the official BMS point list arrives - swapping that list in is a
pure config change (config/meters.yaml).
"""

from __future__ import annotations

import struct
from dataclasses import dataclass, field
from typing import Optional

from .config import BasePointsConfig, MetersConfig
from .store import SQLiteBuffer

# Microsoft-style float32: 2 x 16-bit words.
_FLOAT32_WORDS = 2
_INT32_WORDS = 2
_STD_TYPES: dict[str, int] = {
    "bool": 1,      # coils only
    "int16": 1,
    "uint16": 1,
    "int32": _INT32_WORDS,
    "uint32": _INT32_WORDS,
    "float32": _FLOAT32_WORDS,
}


def modbus_pdu_address(register_type: str, address: int) -> int:
    """Map a user-facing (5-digit, 1-based) Modbus address to a 0-based PDU
    address. Follows the conventional readout:

      * holding     40001 ... 49999 -> 0 ... 9998
      * input       30001 ... 39999 -> 0 ... 9998
      * coil         0xxxx (1-based) -> 0 ... 9998

    DRAFT convention documented in config/meters.yaml; the final BMS spec may
    dictate 0-based addresses, in which case only this helper changes.
    """
    base = {"holding": 40001, "input": 30001, "coil": 1}[register_type]
    if address < base:
        raise ValueError(
            f"address {address} below {register_type} convention base {base}"
        )
    return address - base


@dataclass
class ModbusPoint:
    register_type: str
    address: int              # as-written in meters.yaml (1-based convention)
    pdu_address: int          # 0-based wire address
    dtype: str
    scale: float
    byte_order: str
    word_order: str
    rw: str


@dataclass
class BacnetPoint:
    object_type: str          # AI | BI (config meters.yaml)
    instance: int
    units: str = "noUnits"    # BACnet EngineeringUnits label (mapped)
    units_note: str = ""


@dataclass
class PointMap:
    point_id: str
    modbus: Optional[ModbusPoint] = None
    bacnet: Optional[BacnetPoint] = None


@dataclass
class MeterMap:
    meter_id: str
    modbus_unit_id: int
    bacnet_device: int
    points: dict[str, PointMap] = field(default_factory=dict)


@dataclass
class CompiledMap:
    meters: dict[str, MeterMap] = field(default_factory=dict)
    unit_to_meter: dict[int, str] = field(default_factory=dict)
    bacnet_device: int = 1

    def modbus_unit_ids(self) -> list[int]:
        return sorted(self.unit_to_meter)

    def bacnet_entries(self) -> list[tuple[str, str, BacnetPoint]]:
        out: list[tuple[str, str, BacnetPoint]] = []
        for meter in self.meters.values():
            for point in meter.points.values():
                if point.bacnet is not None:
                    out.append((meter.meter_id, point.point_id, point.bacnet))
        return out


# unit label mapping from base_points unit text -> BACnet EngineeringUnits
_BACNET_UNIT_LABELS: dict[str, str] = {
    "m3": "cubicMeters",
    "kg/cm2": "bars",          # 1 kg/cm2 ~= 0.980665 bar [OWNER: label only]
    "L": "liters",
    "V": "volts",
    "dBm": "noUnits",          # BACnet has no dBm EngineeringUnit [OWNER]
    "binary": "noUnits",
}
_BACNET_UNITS_NOTES: dict[str, str] = {
    "kg/cm2": "[OWNER] label mapped to bars; confirm scale rule",
    "dBm": "[OWNER] BACnet has no dBm unit; presentValue is the dBm number",
}
_DEFAULT_UNITS_NOTE = "[OWNER] unknown unit label"


class MappingEngine:
    """Compile + validate the protocol map, and project values onto it."""

    def __init__(
        self, meters: MetersConfig, base_points: BasePointsConfig
    ) -> None:
        self.base_points = base_points
        self.map = self._compile(meters)

    # ── compilation / validation ─────────────────────────────────────────
    def _compile(self, meters: MetersConfig) -> CompiledMap:
        compiled = CompiledMap()
        seen_units: set[int] = set()
        seen_bacnet: set[tuple[str, int]] = set()
        bacnet_device: Optional[int] = None

        for meter_id, config in meters.meters.items():
            if config.modbus_unit_id in seen_units:
                raise ValueError(
                    f"duplicate modbus unit id {config.modbus_unit_id}"
                )
            seen_units.add(config.modbus_unit_id)
            if bacnet_device is None:
                bacnet_device = config.bacnet_device
            elif config.bacnet_device != bacnet_device:
                raise ValueError(
                    f"all meters must share one BACnet device (S4); "
                    f"{meter_id} uses {config.bacnet_device}"
                )

            meter = MeterMap(
                meter_id=meter_id,
                modbus_unit_id=config.modbus_unit_id,
                bacnet_device=config.bacnet_device,
            )
            self._validate_meter_points(meter_id, config.points, seen_bacnet)
            for point_id, mapping in config.points.items():
                point = PointMap(point_id=point_id)
                if mapping.modbus is not None:
                    point.modbus = ModbusPoint(
                        register_type=mapping.modbus.register_type,
                        address=mapping.modbus.address,
                        pdu_address=modbus_pdu_address(
                            mapping.modbus.register_type, mapping.modbus.address
                        ),
                        dtype=mapping.modbus.dtype,
                        scale=mapping.modbus.scale,
                        byte_order=mapping.modbus.byte_order,
                        word_order=mapping.modbus.word_order,
                        rw=mapping.modbus.rw,
                    )
                if mapping.bacnet is not None:
                    label = self.base_points.points.get(point_id)
                    unit_text = label.unit if label else None
                    bacnet_units = _BACNET_UNIT_LABELS.get(unit_text, "noUnits")
                    point.bacnet = BacnetPoint(
                        object_type=mapping.bacnet.object,
                        instance=mapping.bacnet.instance,
                        units=bacnet_units,
                        units_note=(
                            _BACNET_UNITS_NOTES.get(unit_text, "")
                            or (
                                _DEFAULT_UNITS_NOTE
                                if unit_text not in _BACNET_UNIT_LABELS
                                else ""
                            )
                        ),
                    )
                    key = (point.bacnet.object_type, point.bacnet.instance)
                    if key in seen_bacnet:
                        raise ValueError(f"duplicate BACnet object {key}")
                    seen_bacnet.add(key)
                meter.points[point_id] = point
            compiled.meters[meter_id] = meter
            compiled.unit_to_meter[config.modbus_unit_id] = meter_id

        if bacnet_device is not None:
            compiled.bacnet_device = bacnet_device
        return compiled

    def _validate_meter_points(
        self,
        meter_id: str,
        points: dict,
        seen_bacnet: set[tuple[str, int]],
    ) -> None:
        known = set(self.base_points.points)
        unknown = [p for p in points if p not in known]
        if unknown:
            raise ValueError(
                f"{meter_id}: unknown point ids {unknown} (not in base_points)"
            )
        spans: dict[str, list[tuple[int, int]]] = {}
        for point_id, mapping in points.items():
            mb = mapping.modbus
            if mb is None:
                continue
            if mb.dtype not in _STD_TYPES:
                raise ValueError(f"{point_id}: unsupported dtype {mb.dtype}")
            if mb.dtype == "bool" and mb.register_type != "coil":
                raise ValueError(f"{point_id}: bool dtype requires coil")
            size = _STD_TYPES[mb.dtype]
            start = modbus_pdu_address(mb.register_type, mb.address)
            span = (start, start + size - 1)
            for other in spans.get(mb.register_type, []):
                if max(span[0], other[0]) <= min(span[1], other[1]):
                    raise ValueError(
                        f"{meter_id}:{point_id} modbus {mb.register_type} "
                        f"address span {span} overlaps {other}"
                    )
            spans.setdefault(mb.register_type, []).append(span)

    # ── live values ──────────────────────────────────────────────────────
    def live(self, buffer: SQLiteBuffer) -> dict[str, dict[str, float]]:
        """Project current normalized values per meter (canonical values,
        not yet scaled/encoded)."""
        live: dict[str, dict[str, float]] = {}
        for meter in self.map.meters.values():
            stored = buffer.get_normalized(meter.meter_id)
            values: dict[str, float] = {}
            for point_id in meter.points:
                row = stored.get(point_id)
                if row is not None and row["value"] is not None:
                    values[point_id] = float(row["value"])
            live[meter.meter_id] = values
        return live

    # ── wire encoding ────────────────────────────────────────────────────
    def modbus_int(
        self, point: ModbusPoint, value: float
    ) -> bool | int | tuple[int, int]:
        """Convert a canonical value to the modbus wire representation."""
        scaled = float(value) * point.scale
        dtype = point.dtype
        if dtype == "bool":
            return bool(scaled)
        if dtype == "float32":
            return encode_float32(scaled, point.byte_order, point.word_order)
        if dtype in ("uint16", "int16", "uint32", "int32"):
            return _clamp(scaled, dtype)
        raise ValueError(f"unsupported dtype {dtype}")


# ── float32 word packing ─────────────────────────────────────────────────
def encode_float32(
    value: float, byte_order: str = "big", word_order: str = "big"
) -> tuple[int, int]:
    """Pack a float32 into (high, low) 16-bit words honouring the config's
    byte/word ordering (config/meters.yaml)::

        byte_order   struct endianness of the raw 4-byte IEEE-754 picture
        word_order   where the high word lands on the wire
    """
    fmt = ">" if byte_order == "big" else "<"
    raw = struct.pack(fmt + "f", float(value))
    high = int.from_bytes(raw[:2], "big")
    low = int.from_bytes(raw[2:], "big")
    if word_order == "little":
        return low, high
    return high, low


def decode_float32(
    words: tuple[int, int], byte_order: str = "big", word_order: str = "big"
) -> float:
    """Inverse of :func:`encode_float32`."""
    high, low = words
    if word_order == "little":
        high, low = low, high
    raw = high.to_bytes(2, "big") + low.to_bytes(2, "big")
    fmt = ">" if byte_order == "big" else "<"
    return struct.unpack(fmt + "f", raw)[0]


def _clamp(value: float, dtype: str) -> int:
    bounds = {
        "int16": (-32768, 32767),
        "uint16": (0, 65535),
        "int32": (-(2**31), 2**31 - 1),
        "uint32": (0, 2**32 - 1),
    }
    lo, hi = bounds[dtype]
    return max(lo, min(hi, int(round(value))))