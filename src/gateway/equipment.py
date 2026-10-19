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

Both directions are deliberately the *same* function, so an address the server
hands out always resolves back to the point it came from. Lookups are
case-insensitive and the catalog is small (a station has hundreds of points, not
millions), so a linear scan keeps the code simple and correct.
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
                points[(meter_id, point_id)] = str(value)
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
                self.station_devices.setdefault(str(meter_id), str(name))
        for row in points or []:
            meter_id = row.get("meter_id")
            point_id = row.get("point_id")
            name = row.get("name")
            if meter_id and point_id and name:
                self.station_points.setdefault(
                    (str(meter_id), str(point_id)), str(name)
                )
        return self

    def add_station_point(self, meter_id: str, point_id: str, name: str = "") -> None:
        """Record one point's station name without a full catalog read."""
        if meter_id and point_id:
            self.station_points.setdefault(
                (meter_id, point_id), name or point_id
            )

    # ── forward lookups ────────────────────────────────────────────────
    def equipment_id(self, meter_id: str) -> str:
        """Friendly id for a device: mapped name, else station name, else id."""
        wanted = _norm(meter_id)
        for mid, equipment in self.devices.items():
            if _norm(mid) == wanted:
                return equipment
        for mid, name in self.station_devices.items():
            if _norm(mid) == wanted:
                return name
        return meter_id

    def equipment_point(self, meter_id: str, point_id: str) -> str:
        """Friendly name for a point: mapped, else station name, else id."""
        wanted_meter, wanted_point = _norm(meter_id), _norm(point_id)
        for (mid, pid), name in self.points.items():
            if _norm(mid) == wanted_meter and _norm(pid) == wanted_point:
                return name
        for (mid, pid), name in self.station_points.items():
            if _norm(mid) == wanted_meter and _norm(pid) == wanted_point:
                return name
        return point_id

    def resolve(self, meter_id: str, point_id: str) -> dict[str, str]:
        """Both names for a point, as exposed on the read API."""
        return {
            "equipment_id": self.equipment_id(meter_id),
            "equipment_point": self.equipment_point(meter_id, point_id),
        }

    # ── reverse lookup ─────────────────────────────────────────────────
    def _meters_named(self, equipment_id: str) -> list[str]:
        """Every station meter_id that answers to this equipment id."""
        wanted = _norm(equipment_id)
        found: list[str] = []
        for mid, equipment in self.devices.items():
            if _norm(equipment) == wanted and mid not in found:
                found.append(mid)
        for mid, name in self.station_devices.items():
            if _norm(name) == wanted and mid not in found:
                found.append(mid)
        # The equipment id may simply be the raw meter_id.
        if equipment_id and equipment_id not in found:
            found.append(equipment_id)
        return found

    def to_station(
        self, equipment_id: str, equipment_point: str
    ) -> Optional[tuple[str, str]]:
        """Resolve an equipment address back to (meter_id, point_id).

        Uses the same names the forward direction hands out, so any address
        returned by :meth:`resolve` round-trips. Returns None when nothing
        matches.
        """
        for meter_id in self._meters_named(equipment_id):
            for (mid, pid) in list(self.points) + list(self.station_points):
                if _norm(mid) != _norm(meter_id):
                    continue
                if _norm(self.equipment_point(mid, pid)) == _norm(equipment_point):
                    return mid, pid
        return None

    def is_mapped(self, meter_id: str, point_id: str) -> bool:
        """True when this device/point has an explicit entry in the map."""
        wanted_meter, wanted_point = _norm(meter_id), _norm(point_id)
        if any(_norm(mid) == wanted_meter for mid in self.devices):
            return True
        return any(
            _norm(mid) == wanted_meter and _norm(pid) == wanted_point
            for (mid, pid) in self.points
        )

    def summary(self) -> dict:
        """Shape summary for /bms/equipment and status output."""
        return {
            "devices_mapped": len(self.devices),
            "points_mapped": len(self.points),
            "station_devices": len(self.station_devices),
            "station_points": len(self.station_points),
            "source": str(DEFAULT_MAP_PATH.name),
        }
