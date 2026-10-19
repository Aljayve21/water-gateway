"""Publish AI anomaly findings back to the BMS as BACnet objects.

The measurement objects (analogInput/binaryInput) are read-only by the BACnet
standard, so AI output cannot ride on them. This module turns the findings the
AnomalyEngine already stores in the local buffer into a per-meter view that can
be written into analogValue/binaryValue objects, which is the correct type for
derived values.

Layout comes from config/findings.yaml: a list of rules (binary flags) plus a
list of derived analog values, instantiated for every meter from an instance
base that sits above the measurement objects.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

import yaml

from .config import CONFIG_DIR

log = logging.getLogger("gateway.findings")


@dataclass(frozen=True)
class BinaryPoint:
    """One rule flag published per meter as a binaryValue."""

    rule: str
    label: str
    offset: int  # instance offset from the per-meter base


@dataclass(frozen=True)
class AnalogPoint:
    """One derived number published per meter as an analogValue."""

    key: str
    label: str
    offset: int


@dataclass(frozen=True)
class FindingsLayout:
    """Instance allocation for every published findings object."""

    enabled: bool
    instance_base: int
    binary_points: tuple[BinaryPoint, ...]
    analog_points: tuple[AnalogPoint, ...]
    severity_scale: dict[str, float] = field(default_factory=dict)

    @property
    def per_meter(self) -> int:
        return len(self.binary_points) + len(self.analog_points)

    def instance_for(self, meter_index: int, point_offset: int) -> int:
        return self.instance_base + (meter_index * self.per_meter) + point_offset


@dataclass(frozen=True)
class PublishedObject:
    """A single object to create on the BACnet server."""

    meter_id: str
    key: str  # stable identifier, e.g. "ai:offline" or "ai:finding_count"
    object_type: str  # "BV" | "AV"
    instance: int
    object_name: str
    label: str


def load_layout(path: Optional[Path] = None) -> FindingsLayout:
    """Read config/findings.yaml, tolerating absence by disabling publication."""
    target = path or CONFIG_DIR / "findings.yaml"
    if not target.exists():
        log.warning("findings.yaml not found - AI findings will not be published")
        return FindingsLayout(False, 0, (), (), {})

    raw = yaml.safe_load(target.read_text(encoding="utf-8-sig")) or {}
    section = raw.get("findings") or {}
    if not section.get("enabled", True):
        return FindingsLayout(False, 0, (), (), raw.get("severity_scale") or {})

    binary_points: list[BinaryPoint] = []
    for offset, entry in enumerate(section.get("binary_points") or []):
        rule = str(entry.get("rule") or "").strip()
        if not rule:
            continue
        binary_points.append(
            BinaryPoint(rule=rule, label=str(entry.get("label") or rule), offset=offset)
        )

    analog_offset = len(binary_points)
    analog_points: list[AnalogPoint] = []
    for i, entry in enumerate(section.get("analog_points") or []):
        key = str(entry.get("key") or "").strip()
        if not key:
            continue
        analog_points.append(
            AnalogPoint(
                key=key, label=str(entry.get("label") or key), offset=analog_offset + i
            )
        )

    scale = {str(k): float(v) for k, v in (raw.get("severity_scale") or {}).items()}
    return FindingsLayout(
        enabled=True,
        instance_base=int(section.get("instance_base") or 200),
        binary_points=tuple(binary_points),
        analog_points=tuple(analog_points),
        severity_scale=scale,
    )


def plan_objects(
    layout: FindingsLayout, meter_ids: list[str], used_instances: set[int]
) -> list[PublishedObject]:
    """Allocate an instance per (meter, point), refusing to collide.

    ``used_instances`` is every instance already claimed by meters.yaml. A
    collision means findings.yaml was not updated after the point list grew,
    and silently reusing an instance would overwrite a real measurement - so it
    fails loudly instead.
    """
    if not layout.enabled or layout.per_meter == 0:
        return []

    objects: list[PublishedObject] = []
    collisions: list[int] = []
    for index, meter_id in enumerate(meter_ids):
        for point in layout.binary_points:
            instance = layout.instance_for(index, point.offset)
            if instance in used_instances:
                collisions.append(instance)
                continue
            objects.append(
                PublishedObject(
                    meter_id=meter_id,
                    key=f"ai:{point.rule}",
                    object_type="BV",
                    instance=instance,
                    object_name=f"{meter_id}:ai_{point.rule}",
                    label=point.label,
                )
            )
        for point in layout.analog_points:
            instance = layout.instance_for(index, point.offset)
            if instance in used_instances:
                collisions.append(instance)
                continue
            objects.append(
                PublishedObject(
                    meter_id=meter_id,
                    key=f"ai:{point.key}",
                    object_type="AV",
                    instance=instance,
                    object_name=f"{meter_id}:ai_{point.key}",
                    label=point.label,
                )
            )

    if collisions:
        raise ValueError(
            "findings instance_base collides with meters.yaml instances "
            f"{sorted(set(collisions))[:5]}; raise instance_base"
        )
    log.info(
        "findings publication planned: %d objects across %d meters (base %d)",
        len(objects),
        len(meter_ids),
        layout.instance_base,
    )
    return objects


def meter_state(
    layout: FindingsLayout, findings: list[dict[str, Any]]
) -> dict[str, dict[str, Any]]:
    """Collapse open findings into one publishable value set per meter.

    Findings are already deduplicated and resolved by the FindingRecorder, so
    this only has to aggregate what is currently open. A meter with no findings
    is still present, with every flag inactive and a zero severity - the BMS
    should never have to distinguish "no findings" from "meter missing".
    """
    rules = [p.rule for p in layout.binary_points]
    state: dict[str, dict[str, Any]] = {}
    for finding in findings:
        meter_id = finding.get("meter_id")
        if not meter_id:
            continue  # estate-level finding; not attributable to one meter
        entry = state.setdefault(
            meter_id,
            {
                **{rule: False for rule in rules},
                "max_severity": 0.0,
                "finding_count": 0.0,
            },
        )
        rule = str(finding.get("rule") or "")
        if rule in entry:
            entry[rule] = True
        entry["finding_count"] += 1.0
        severity = str(finding.get("severity") or "info")
        scaled = layout.severity_scale.get(severity, 0.0)
        entry["max_severity"] = max(float(entry["max_severity"]), scaled)
    return state
