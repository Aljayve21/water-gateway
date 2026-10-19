"""Equipment <-> point naming map.

The oBIX station gives us machine-shaped ids (``bms_enyecontrols`` /
``enyecontrolsbms_io_mezz_tr_temp_reading``). Operators, controls engineers and
the CMMS all think in equipment (``AHU-01`` / ``SpaceTemp``). This module holds
that translation so the read API can answer in either vocabulary.

The map lives in ``config/equipment_map.yaml`` and is entirely optional: a
device or point with no entry falls back to its station name, so the API keeps
working before the map is authored and after new points appear on the station.

Two directions are supported, because both are needed:

  * forward  (meter_id, point_id) -> equipment_id / equipment_point
             used to annotate every ``/bms/points*`` row.
  * reverse  equipment_id, equipment_point -> (meter_id, point_id)
             used by ``GET /bms/points/{equipment}/{point}`` so a caller can
             address a point by name.

Forward lookups are authoritative and cheap (dict). The reverse index is built
once from the forward map plus, when a store is supplied, the station's own
device/point names - so unmapped points are addressable by their station names
too, which is what makes the path form useful on day one.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import yaml

from .config import CONFIG_DIR

DEFAULT_MAP_PATH = CONFIG_DIR / "equipment_map.yaml"


def _norm(value: object) -> str:
    """Normalize an id for matching: trimmed, case-folded."""
    return str(value or "").strip().casefold()


@dataclass
class EquipmentMap:
    """Translation between station ids and equipment names.

    ``devices`` maps a meter_id to an equipment id. ``points`` maps a
    ``(meter_id, point_id)`` pair to an equipment point name. Both are
    optional; an empty map is valid and simply means "use station names".
    """

    devices: dict[str, str] = field(default_factory=dict)
    points: dict[tuple[str, str], str] = field(default_factory=dict)
    # Optional station-sourced names, used as the fallback for unmapped ids.
    station_devices: dict[str, str] = field(default_factory=dict)
    station_points: dict[tuple[str, str], str] = field(default_factory=dict)

    # ── loading ─────────────────────────────────────────────────────────
    @classmethod
    def from_dict(cls, raw: Optional[dict]) -> "EquipmentMap":
        raw = raw or {}
        devices = {
            str(k): str(v)
            for k, v in (raw.get("devices") or {}).items()
            if k and v
        }
        points: dict[tuple[str, str], str] = {}
        for key, value in (raw.get("points") or {}).items():
            meter_id, _, point_id = str(key).partition("/")
            if meter_id and point_id and value:
                points[(_norm(meter_id), _norm(point_id))] = str(value)
        return cls(devices=devices, points=points)

    @classmethod
    def load(cls, path: Optional[Path] = None) -> "EquipmentMap":
        """Load the map. A missing file yields an empty (valid) map."""
        target = Path(path or DEFAULT_MAP_PATH)
        if not target.is_file():
            return cls()
        with target.open("r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        if not isinstance(data, dict):
            raise ValueError(f"equipment map must be a YAML mapping: {target}")
        return cls.from_dict(data)

    # ── station names (fallback source) ────────────────────────────────
    def with_station_names(
        self,
        devices: Optional[Iterable[dict]] = None,
        points: Optional[Iterable[dict]] = None,
    ) -> "EquipmentMap":
        """Attach station-provided names as the fallback layer, in place."""
        for row in devices or []:
            meter_id = row.get("meter_id")
            name = row.get("name")
            if meter_id and name:
                self.station_devices[str(meter_id)] = str(name)
        for row in points or []:
            meter_id = row.get("meter_id")
            point_id = row.get("point_id")
            name = row.get("name")
            if meter_id and point_id and name:
                self.station_points[(_norm(meter_id), _norm(point_id))] = str(name)
        return self

    # ── forward lookups ────────────────────────────────────────────────
    def equipment_id(self, meter_id: str) -> str:
        """Friendly id for a device: mapped name, else station name, else id."""
        if meter_id in self.devices:
            return self.devices[meter_id]
        return self.station_devices.get(meter_id) or meter_id

    def equipment_point(self, meter_id: str, point_id: str) -> str:
        """Friendly name for a point: mapped, else station name, else id."""
        key = (_norm(meter_id), _norm(point_id))
        if key in self.points:
            return self.points[key]
        return self.station_points.get(key) or point_id

    def resolve(self, meter_id: str, point_id: str) -> dict[str, str]:
        """Both names for a point, as exposed on the read API."""
        return {
            "equipment_id": self.equipment_id(meter_id),
            "equipment_point": self.equipment_point(meter_id, point_id),
        }

    # ── reverse lookup ─────────────────────────────────────────────────
    def to_station(
        self, equipment_id: str, equipment_point: str
    ) -> Optional[tuple[str, str]]:
        """Resolve an equipment address back to (meter_id, point_id).

        Mapped entries win. When no mapping matches, station names are tried,
        so ``GET /bms/points/ENYECONTROLS/MEZZ_TR_Temp$20Reading``-style
        addresses still resolve. Returns None when nothing matches.
        """
        want_eq = _norm(equipment_id)
        want_pt = _norm(equipment_point)

        # 1. Explicit mapping: equipment_id -> meter_id.
        meter_id = next(
            (mid for mid, eq in self.devices.items() if _norm(eq) == want_eq),
            None,
        )
        if meter_id is not None:
            point_id = next(
                (
                    pid
                    for (mid, pid), name in self.points.items()
                    if mid == _norm(meter_id) and _norm(name) == want_pt
                ),
                None,
            )
            if point_id is not None:
                return meter_id, point_id

        # 2. Station names: equipment_id is the device's station name.
        meter_id = next(
            (mid for mid, name in self.station_devices.items() if _norm(name) == want_eq),
            None,
        )
        if meter_id is not None:
            point_id = next(
                (
                    pid
                    for (mid, pid), name in self.station_points.items()
                    if mid == _norm(meter_id) and _norm(name) == want_pt
                ),
                None,
            )
            if point_id is not None:
                return meter_id, point_id

        # 3. Last resort: the equipment id is itself a meter_id.
        for mid in {self.devices.get(equipment_id, ""), equipment_id}:
            if not mid:
                continue
            point_id = next(
                (
                    pid
                    for (m, pid), name in self.points.items()
                    if m == _norm(mid) and _norm(name) == want_pt
                ),
                None,
            )
            if point_id is not None:
                return mid, point_id
        return None

    def is_mapped(self, meter_id: str, point_id: str) -> bool:
        """True when this device/point has an explicit entry in the map."""
        return meter_id in self.devices or (
            _norm(meter_id), _norm(point_id)
        ) in self.points

    def summary(self) -> dict:
        """Shape summary for /bms/equipment and status output."""
        return {
            "devices_mapped": len(self.devices),
            "points_mapped": len(self.points),
            "station_devices": len(self.station_devices),
            "station_points": len(self.station_points),
            "source": str(DEFAULT_MAP_PATH.name),
        }
