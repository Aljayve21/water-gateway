"""BACnet/IP server exposing gateway points as a single device object.

One BACnet device fires up per gateway (decision S4). Each canonical point
becomes an AnalogInput or BinaryInput object using the instance numbers from
config/meters.yaml (still [DRAFT] until the official BMS point list lands).
``presentValue`` is refreshed from the local buffer each extraction cycle via
``write_property``, which keeps COV subscribers honest.

Unit labels are mapped to BACnet EngineeringUnits where one exists; dBm has no
BACnet unit so the exact dBm number is carried as presentValue with units
``noUnits`` (see map.py notes).
"""

from __future__ import annotations

import asyncio
import logging
from typing import Optional

from bacpypes3.local.analog import AnalogInputObject, AnalogValueObject
from bacpypes3.local.binary import BinaryInputObject, BinaryValueObject
# The local.device subclass supplies the required I-Am defaults
# (maxApduLengthAccepted, segmentationSupported, protocolVersion, ...).
# The abstract bacpypes3.object.DeviceObject lacks them, so I-Am responses
# fail to encode and the device can never be discovered.
from bacpypes3.local.device import DeviceObject
from bacpypes3.pdu import Address
from bacpypes3.primitivedata import Boolean, Real

from bacpypes3.ipv4.app import NormalApplication as _NormalApplication

from .map import CompiledMap, MappingEngine

log = logging.getLogger("gateway.bacnet")


class BACnetServer:
    """BACnet/IP server backed by a compiled :class:`MappingEngine`."""

    def __init__(
        self,
        host: str,
        port: int,
        device_instance: int,
        engine: MappingEngine,
        findings_objects: Optional[list] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.device_instance = device_instance
        self.engine = engine
        self.map: CompiledMap = engine.map
        # Findings objects are derived state, not measurements: they are created
        # separately (config/findings.yaml) because they must be AV/BV, which
        # are writable types, while measurements stay read-only AI/BI.
        self._findings_objects = list(findings_objects or [])

        self.objects: dict[str, AnalogInputObject | BinaryInputObject] = {}
        self.finding_objects: dict[str, object] = {}
        self._app = None
        self._device = None
        self._started = False

    async def start(self) -> None:
        """Build the device + objects and bind the BACnet/IP stack (must run
        inside the event loop)."""
        self._device = DeviceObject(
            objectIdentifier=("device", self.device_instance),
            objectName="ems-water-meter-gateway",
            vendorIdentifier=15,
            modelName="ems-water-gateway",
        )
        for meter_id, point_id, bp in self.map.bacnet_entries():
            key = _object_key(meter_id, point_id)
            name = f"{meter_id}:{point_id}"
            oid = (point_id_type(bp.object_type), bp.instance)
            if bp.object_type == "BI":
                obj = BinaryInputObject(
                    objectIdentifier=oid,
                    objectName=name,
                    presentValue="inactive",
                    statusFlags=[False, False, False, False],
                    polarity="normal",
                )
            else:
                obj = AnalogInputObject(
                    objectIdentifier=oid,
                    objectName=name,
                    units=bp.units,
                    presentValue=Real(0.0),
                    statusFlags=[False, False, False, False],
                    covIncrement=0.001,
                )
            self.objects[key] = obj

        for spec in self._findings_objects:
            oid = (point_id_type(spec.object_type), spec.instance)
            if spec.object_type == "BV":
                obj = BinaryValueObject(
                    objectIdentifier=oid,
                    objectName=spec.object_name,
                    description=spec.label,
                    presentValue=Boolean(False),
                    statusFlags=[False, False, False, False],
                )
            else:
                obj = AnalogValueObject(
                    objectIdentifier=oid,
                    objectName=spec.object_name,
                    description=spec.label,
                    presentValue=Real(0.0),
                    statusFlags=[False, False, False, False],
                    covIncrement=1.0,
                )
            self.finding_objects[f"{spec.meter_id}|{spec.key}"] = obj

        self._app = _NormalApplication(
            self._device, Address((self.host, self.port))
        )
        for obj in self.finding_objects.values():
            self._app.add_object(obj)
        for obj in self.objects.values():
            self._app.add_object(obj)
        self._started = True
        log.info(
            "bacnet/ip serving device %d on %s:%d (%d objects)",
            self.device_instance,
            self.host,
            self.port,
            len(self.objects),
        )

    def sync(self, live: dict[str, dict[str, float]]) -> None:
        """Schedule presentValue updates; binary points map 1/0 -> active/inactive."""
        if not self._started:
            return
        loop = asyncio.get_running_loop()
        for meter_id, values in live.items():
            for point_id, value in values.items():
                key = _object_key(meter_id, point_id)
                obj = self.objects.get(key)
                if obj is None:
                    continue
                if isinstance(obj, BinaryInputObject):
                    new_value = Boolean(bool(value))
                else:
                    new_value = Real(float(value))
                loop.create_task(obj.write_property("presentValue", new_value))

    def sync_findings(self, state: dict[str, dict]) -> int:
        """Write AI findings into the AV/BV objects. Returns objects updated.

        ``state`` comes from :func:`gateway.findings.meter_state`. Meters with
        no open findings still get their flags cleared, so a resolved finding
        actually clears at the BMS instead of latching on forever.
        """
        if not self._started or not self.finding_objects:
            return 0
        loop = asyncio.get_running_loop()
        updated = 0
        for meter_id, values in state.items():
            for key, value in values.items():
                obj = self.finding_objects.get(f"{meter_id}|ai:{key}")
                if obj is None:
                    continue
                if isinstance(obj, BinaryValueObject):
                    new_value = Boolean(bool(value))
                else:
                    new_value = Real(float(value))
                loop.create_task(obj.write_property("presentValue", new_value))
                updated += 1
        return updated

    async def stop(self) -> None:
        if self._app is not None:
            try:
                self._app.close()
            except Exception:  # noqa: BLE001
                pass
            self._app = None
        self._started = False

    @property
    def listening(self) -> bool:
        return self._app is not None


def point_id_type(object_type: str) -> str:
    """Map config AI/BI object labels to BACnet object type strings."""
    if object_type == "BI":
        return "binaryInput"
    if object_type == "AV":
        return "analogValue"
    if object_type == "BV":
        return "binaryValue"
    return "analogInput"


def _object_key(meter_id: str, point_id: str) -> str:
    return f"{meter_id}:{point_id}"