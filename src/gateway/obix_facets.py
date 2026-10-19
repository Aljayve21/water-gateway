"""Read Neoview point facets over oBIX and fold them into validation.

The validator's ideal source of truth is the station's *own* point config -
Niagara publishes ``min``/``max``/``precision``/``unit`` as facets on each
numeric point. This module reads those facets live and caches them, so the
authored ``config/obix_requirements.yaml`` becomes a fallback rather than the
only input:

  * reachable station  -> facet bounds win over the generic ``unit_bounds``
    guesses (an explicit per-point entry in the YAML still wins, so an operator
    can always tighten or loosen a single point);
  * unreachable station -> the last cached facets are reused, and if there is
    no cache at all the authored requirements apply unchanged.

Everything here is best-effort and read-only: a point that cannot be read is
recorded and skipped, and a station that answers nothing aborts the probe early
rather than waiting out a per-point timeout 140 times.
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator, Optional

from .config import CONFIG_DIR
from .obix import ObixClient, ObixError

log = logging.getLogger("gateway.obix_facets")

DEFAULT_CACHE_NAME = "obix_facets.json"

# If this many points in a row fail with a transport error the station is
# almost certainly unreachable - stop rather than pay the timeout 140 times.
_ABORT_AFTER_ERRORS = 3


def iter_read_points(
    point_map: Optional[dict[str, Any]], *, meter_id: Optional[str] = None
) -> Iterator[tuple[str, str, str]]:
    """Yield ``(meter_id, point_id, href)`` for every configured read point."""
    read = (point_map or {}).get("read") or {}
    for mid, points in read.items():
        if meter_id and str(mid) != meter_id:
            continue
        for pid, href in (points or {}).items():
            if href:
                yield str(mid), str(pid), str(href)


@dataclass
class FacetReport:
    """Outcome of one probe pass, suitable for logging and the cache file."""

    probed: int = 0
    with_facets: int = 0
    without_facets: int = 0
    aborted: bool = False
    errors: dict[str, str] = field(default_factory=dict)
    # "meter_id/point_id" -> {"unit": ..., "min": ..., "max": ..., "precision": ...}
    points: dict[str, dict] = field(default_factory=dict)

    def summary(self) -> str:
        return (
            f"probed={self.probed} with_facets={self.with_facets} "
            f"without_facets={self.without_facets} errors={len(self.errors)}"
            + (" aborted" if self.aborted else "")
        )


def probe_facets(
    client: ObixClient,
    point_map: Optional[dict[str, Any]],
    *,
    meter_id: Optional[str] = None,
    logger: Optional[logging.Logger] = None,
    abort_after_errors: int = _ABORT_AFTER_ERRORS,
) -> FacetReport:
    """Read facets for every configured point. Never raises on a bad point.

    A run of ``abort_after_errors`` consecutive transport failures stops the
    pass early *while nothing has been read yet*, so an offline station does not
    pay a per-point timeout for every point. Once a facet has been read the
    station is clearly up, so bad points are simply recorded and skipped.
    """
    sink = logger or log
    report = FacetReport()
    consecutive_errors = 0
    for mid, pid, href in iter_read_points(point_map, meter_id=meter_id):
        report.probed += 1
        key = f"{mid}/{pid}"
        try:
            facets = client.get_facets(href)
        except ObixError as exc:
            report.errors[key] = str(exc)
            consecutive_errors += 1
            if (
                abort_after_errors
                and report.with_facets == 0
                and consecutive_errors >= abort_after_errors
            ):
                report.aborted = True
                sink.warning(
                    "obix facet probe aborted after %d consecutive errors; "
                    "station looks unreachable",
                    consecutive_errors,
                )
                break
            continue
        consecutive_errors = 0
        entry = facets.as_dict()
        if entry:
            report.points[key] = entry
            report.with_facets += 1
        else:
            report.without_facets += 1
    return report


# ── cache ────────────────────────────────────────────────────────────────
def _resolve_cache_path(path: Optional[str]) -> Path:
    if not path:
        return CONFIG_DIR / DEFAULT_CACHE_NAME
    candidate = Path(path)
    return candidate if candidate.is_absolute() else CONFIG_DIR / candidate


def save_facets_cache(
    points: dict[str, dict], path: Optional[str] = None, *, source: Optional[str] = None
) -> Path:
    """Write the facet cache (atomically enough for a single writer)."""
    target = _resolve_cache_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source": source or "",
        "points": points,
    }
    tmp = target.with_suffix(target.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
    tmp.replace(target)
    return target


def load_facets_cache(path: Optional[str] = None) -> dict[str, dict]:
    """Load the facet cache, returning ``{}`` when absent or unreadable.

    An empty result means "no facets known", which the validator treats as
    "fall back to the authored requirements" - never as an error.
    """
    target = _resolve_cache_path(path)
    if not target.exists():
        return {}
    try:
        data = json.loads(target.read_text(encoding="utf-8-sig"))
    except (OSError, ValueError) as exc:
        log.warning("facet cache %s could not be read: %s", target, exc)
        return {}
    points = data.get("points") if isinstance(data, dict) else None
    if not isinstance(points, dict):
        return {}
    return {str(k): dict(v) for k, v in points.items() if isinstance(v, dict)}
