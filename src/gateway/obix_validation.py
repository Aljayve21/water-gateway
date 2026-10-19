"""Validate live oBIX point values against configured requirements.

This is the deterministic half of "AI validates data vs Neoview config": every
rule is a plain comparison against requirements the operator owns
(``config/obix_requirements.yaml``). No Bedrock is involved here - a finding is
a fact about the data, and the AI only explains and recommends on top of it
(same principle as :mod:`gateway.ai.anomaly`).

Rules applied to one point:

  * ``no_value``      - the point has no latest value at all
  * ``unit_mismatch`` - reported unit differs from the required unit
  * ``out_of_range``  - numeric value outside ``[min, max]``
  * ``enum_invalid``  - text value not in the allowed set
  * ``stale``         - ``captured_utc`` older than ``max_age_seconds``
  * ``frozen``        - identical value across N hourly buckets (opt-in)

Requirements resolve in four layers (later wins): ``defaults`` ->
``unit_bounds`` (by the point's reported unit) -> live Neoview ``facets`` ->
an explicit ``points`` entry. The facet layer comes from
:mod:`gateway.obix_facets`; when the station cannot be read it is empty and the
authored file drives everything.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Iterable, Optional

import yaml

from .config import CONFIG_DIR

log = logging.getLogger("gateway.obix_validation")

SEVERITY_INFO = "info"
SEVERITY_WARNING = "warning"
SEVERITY_CRITICAL = "critical"

SEVERITY_ORDER = {SEVERITY_INFO: 0, SEVERITY_WARNING: 1, SEVERITY_CRITICAL: 2}

RULES = (
    "no_value",
    "unit_mismatch",
    "out_of_range",
    "enum_invalid",
    "stale",
    "frozen",
)

_UNIT_PREFIX = "obix:units/"


def _norm_unit(unit: Optional[str]) -> str:
    """Normalise an oBIX unit so ``obix:units/celsius`` == ``celsius``.

    ``null``/empty both mean "no unit declared".
    """
    text = (unit or "").strip()
    if text.lower().startswith(_UNIT_PREFIX):
        text = text[len(_UNIT_PREFIX):]
    text = text.strip()
    if text.lower() in ("", "null", "none"):
        return ""
    return text.casefold()


def _parse_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    try:
        moment = datetime.fromisoformat(str(value))
    except (TypeError, ValueError):
        return None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=timezone.utc)
    return moment.astimezone(timezone.utc)


def _as_float(value: Any) -> Optional[float]:
    """Coerce to float, treating bool/None/blank as 'not numeric'."""
    if value is None or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


@dataclass(frozen=True)
class Requirement:
    """The resolved requirements for one point, after layer merging."""

    unit: Optional[str] = None
    min_value: Optional[float] = None
    max_value: Optional[float] = None
    # Slack applied to the bounds, from a facet ``precision``. Keeps a reading
    # that lands exactly on a rounded limit from reading as out of range.
    tolerance: float = 0.0
    allowed: tuple[str, ...] = ()
    max_age_seconds: Optional[float] = None
    severity: dict[str, str] = field(default_factory=dict)
    frozen_enabled: bool = False
    frozen_buckets: int = 0

    def severity_for(self, rule: str) -> str:
        return self.severity.get(rule) or SEVERITY_WARNING


@dataclass(frozen=True)
class Finding:
    """One rule hit on one point."""

    meter_id: str
    point_id: str
    rule: str
    severity: str
    message: str
    value: Optional[float] = None
    text_value: Optional[str] = None
    unit: str = ""
    detail: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        return {
            "meter_id": self.meter_id,
            "point_id": self.point_id,
            "rule": self.rule,
            "severity": self.severity,
            "message": self.message,
            "value": self.value,
            "text_value": self.text_value,
            "unit": self.unit,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class ObixRequirements:
    """The whole requirements document plus a resolver."""

    default_severity: dict[str, str] = field(default_factory=dict)
    default_max_age_seconds: Optional[float] = None
    unit_bounds: dict[str, dict] = field(default_factory=dict)  # keyed by normalised unit
    points: dict[str, dict] = field(default_factory=dict)
    # Live Neoview facets keyed by "meter_id/point_id" (see gateway.obix_facets).
    # They slot in above unit_bounds and below an explicit `points` entry.
    facets: dict[str, dict] = field(default_factory=dict)
    facets_trust_unit: bool = True
    facets_use_precision: bool = True
    frozen_enabled: bool = False
    frozen_buckets: int = 0

    def resolve(
        self, meter_id: str, point_id: str, unit: Optional[str] = None
    ) -> Requirement:
        severity = dict(self.default_severity)
        min_value: Optional[float] = None
        max_value: Optional[float] = None
        tolerance = 0.0
        allowed: tuple[str, ...] = ()
        required_unit: Optional[str] = None
        max_age = self.default_max_age_seconds

        bounds = self.unit_bounds.get(_norm_unit(unit)) or {}
        if "min" in bounds:
            min_value = _as_float(bounds["min"])
        if "max" in bounds:
            max_value = _as_float(bounds["max"])

        # Live Neoview facets override the generic unit guesses: the station's
        # own point config beats a hand-written bound for the unit.
        facet = self.facets.get(f"{meter_id}/{point_id}") or {}
        if facet:
            if self.facets_trust_unit and facet.get("unit"):
                required_unit = str(facet["unit"])
            facet_min = _as_float(facet.get("min"))
            facet_max = _as_float(facet.get("max"))
            # Neoview reports 0/0 for a point whose engineering range was never
            # set; that is not a usable bound (it would flag every reading), so
            # treat it as "no bound" rather than a zero-width range.
            if (
                facet_min is not None
                and facet_max is not None
                and facet_min == facet_max
            ):
                facet_min = facet_max = None
            if facet_min is not None:
                min_value = facet_min
            if facet_max is not None:
                max_value = facet_max
            precision = _as_float(facet.get("precision"))
            if self.facets_use_precision and precision is not None and precision > 0:
                tolerance = precision

        spec = self.points.get(f"{meter_id}/{point_id}") or self.points.get(point_id) or {}
        if spec.get("unit"):
            required_unit = str(spec["unit"])
        if "min" in spec:
            min_value = _as_float(spec["min"])
        if "max" in spec:
            max_value = _as_float(spec["max"])
        if spec.get("allowed"):
            allowed = tuple(str(a) for a in spec["allowed"])
        if "max_age_seconds" in spec:
            raw_age = spec["max_age_seconds"]
            max_age = float(raw_age) if raw_age else None
        severity.update(
            {str(k): str(v) for k, v in (spec.get("severity") or {}).items()}
        )

        return Requirement(
            unit=required_unit,
            min_value=min_value,
            max_value=max_value,
            tolerance=tolerance,
            allowed=allowed,
            max_age_seconds=max_age,
            severity=severity,
            frozen_enabled=self.frozen_enabled,
            frozen_buckets=self.frozen_buckets,
        )


def load_requirements(
    path: Optional[Path] = None, *, facets: Optional[dict[str, dict]] = None
) -> ObixRequirements:
    """Read the requirements file, tolerating absence by disabling checks.

    ``facets`` are the live Neoview facets keyed by ``meter_id/point_id`` (see
    :mod:`gateway.obix_facets`). Pass them explicitly to bypass the cache; omit
    them to auto-load the cache file named by the YAML ``facets:`` section
    (unless that section disables facets).
    """
    target = Path(path) if path else CONFIG_DIR / "obix_requirements.yaml"
    if target.exists():
        raw = yaml.safe_load(target.read_text(encoding="utf-8-sig")) or {}
    else:
        log.warning(
            "requirements file %s not found - oBIX validation will not run", target
        )
        raw = {}

    defaults = raw.get("defaults") or {}
    frozen = raw.get("frozen") or {}
    facet_cfg = raw.get("facets") or {}
    raw_age = defaults.get("max_age_seconds")

    if facets is None and facet_cfg.get("enabled", True):
        from .obix_facets import load_facets_cache

        facets = load_facets_cache(facet_cfg.get("cache"))

    return ObixRequirements(
        default_severity={
            str(k): str(v) for k, v in (defaults.get("severity") or {}).items()
        },
        default_max_age_seconds=float(raw_age) if raw_age else None,
        unit_bounds={
            _norm_unit(k): dict(v or {})
            for k, v in (raw.get("unit_bounds") or {}).items()
        },
        points={str(k): dict(v or {}) for k, v in (raw.get("points") or {}).items()},
        facets={str(k): dict(v or {}) for k, v in (facets or {}).items()},
        facets_trust_unit=bool(facet_cfg.get("trust_unit", True)),
        facets_use_precision=bool(facet_cfg.get("use_precision", True)),
        frozen_enabled=bool(frozen.get("enabled", False)),
        frozen_buckets=int(frozen.get("buckets") or 0),
    )


class ObixValidator:
    """Applies the rules to oBIX latest-value rows."""

    def __init__(self, requirements: ObixRequirements) -> None:
        self.requirements = requirements

    # ── public API ──────────────────────────────────────────────────────
    def check(
        self,
        row: dict,
        *,
        history_for: Optional[Callable[[str, str], Iterable[dict]]] = None,
        now: Optional[datetime] = None,
    ) -> list[Finding]:
        """Every rule against one latest-value row."""
        moment = now or datetime.now(timezone.utc)
        req = self.requirements.resolve(
            str(row.get("meter_id") or ""),
            str(row.get("point_id") or ""),
            row.get("unit"),
        )
        found: list[Finding] = []
        for rule in (self._no_value, self._unit_mismatch, self._out_of_range, self._enum_invalid):
            hit = rule(row, req)
            if hit is not None:
                found.append(hit)
        hit = self._stale(row, req, moment)
        if hit is not None:
            found.append(hit)
        if self.requirements.frozen_enabled and history_for is not None:
            hit = self._frozen(row, req, history_for)
            if hit is not None:
                found.append(hit)
        return found

    def scan(
        self,
        rows: Iterable[dict],
        *,
        history_for: Optional[Callable[[str, str], Iterable[dict]]] = None,
        now: Optional[datetime] = None,
    ) -> list[Finding]:
        """Every rule against every row. A broken rule cannot hide the others."""
        moment = now or datetime.now(timezone.utc)
        found: list[Finding] = []
        for row in rows:
            found.extend(self.check(row, history_for=history_for, now=moment))
        return found

    # ── individual rules ────────────────────────────────────────────────
    def _no_value(self, row: dict, req: Requirement) -> Optional[Finding]:
        if row.get("value") is not None or row.get("text_value") not in (None, ""):
            return None
        return self._finding(row, req, "no_value", f"{row.get('point_id')} has no stored value")

    def _unit_mismatch(self, row: dict, req: Requirement) -> Optional[Finding]:
        if not req.unit:
            return None
        if _norm_unit(row.get("unit")) == _norm_unit(req.unit):
            return None
        return self._finding(
            row,
            req,
            "unit_mismatch",
            f"{row.get('point_id')} reports unit "
            f"'{row.get('unit') or 'none'}', expected '{req.unit}'",
            detail={"expected_unit": req.unit, "reported_unit": row.get("unit") or ""},
        )

    def _out_of_range(self, row: dict, req: Requirement) -> Optional[Finding]:
        if req.min_value is None and req.max_value is None:
            return None
        value = _as_float(row.get("value"))
        if value is None:
            return None
        below = req.min_value is not None and value < req.min_value - req.tolerance
        above = req.max_value is not None and value > req.max_value + req.tolerance
        if not (below or above):
            return None
        return self._finding(
            row,
            req,
            "out_of_range",
            f"{row.get('point_id')} value {value:g} is outside "
            f"[{req.min_value}, {req.max_value}]",
            value=value,
            detail={"min": req.min_value, "max": req.max_value, "tolerance": req.tolerance},
        )

    def _enum_invalid(self, row: dict, req: Requirement) -> Optional[Finding]:
        if not req.allowed:
            return None
        text = row.get("text_value")
        if text in (None, ""):
            return None
        allowed = {a.casefold() for a in req.allowed}
        if str(text).casefold() in allowed:
            return None
        return self._finding(
            row,
            req,
            "enum_invalid",
            f"{row.get('point_id')} value '{text}' is not one of {list(req.allowed)}",
            text_value=str(text),
            detail={"allowed": list(req.allowed)},
        )

    def _stale(self, row: dict, req: Requirement, now: datetime) -> Optional[Finding]:
        if not req.max_age_seconds:
            return None
        captured = _parse_utc(row.get("captured_utc"))
        if captured is None:
            return self._finding(
                row, req, "stale", f"{row.get('point_id')} has no capture timestamp"
            )
        age = (now - captured).total_seconds()
        if age <= req.max_age_seconds:
            return None
        return self._finding(
            row,
            req,
            "stale",
            f"{row.get('point_id')} is stale: last captured {age:.0f}s ago "
            f"(max {req.max_age_seconds:.0f}s)",
            detail={"age_seconds": round(age, 1), "max_age_seconds": req.max_age_seconds},
        )

    def _frozen(
        self,
        row: dict,
        req: Requirement,
        history_for: Callable[[str, str], Iterable[dict]],
    ) -> Optional[Finding]:
        if not req.frozen_enabled or req.frozen_buckets <= 0:
            return None
        try:
            buckets = list(history_for(str(row.get("meter_id") or ""), str(row.get("point_id") or "")) or [])
        except Exception:  # noqa: BLE001 - a broken lookup must not hide other rules
            return None
        recent = buckets[-req.frozen_buckets:]
        if len(recent) < req.frozen_buckets:
            return None
        values: list[float] = []
        for bucket in recent:
            lo = _as_float(bucket.get("min_value"))
            hi = _as_float(bucket.get("max_value"))
            last = _as_float(bucket.get("last_value"))
            if lo is None and hi is None and last is None:
                return None
            if lo is not None and hi is not None and abs(lo - hi) > 1e-9:
                return None
            pick = last if last is not None else lo
            if pick is None:
                return None
            values.append(pick)
        if len(set(values)) != 1:
            return None
        return self._finding(
            row,
            req,
            "frozen",
            f"{row.get('point_id')} has held {values[0]:g} for "
            f"{len(recent)} hourly buckets",
            value=values[0],
            detail={"buckets": len(recent)},
        )

    # ── helper ──────────────────────────────────────────────────────────
    def _finding(
        self,
        row: dict,
        req: Requirement,
        rule: str,
        message: str,
        *,
        detail: Optional[dict] = None,
        value: Optional[float] = None,
        text_value: Optional[str] = None,
    ) -> Finding:
        return Finding(
            meter_id=str(row.get("meter_id") or ""),
            point_id=str(row.get("point_id") or ""),
            rule=rule,
            severity=req.severity_for(rule),
            message=message,
            value=value,
            text_value=text_value,
            unit=str(row.get("unit") or ""),
            detail=detail or {},
        )
