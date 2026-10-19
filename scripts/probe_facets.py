#!/usr/bin/env python3
"""probe_facets.py - read Neoview point facets over oBIX and cache them.

    python scripts\\probe_facets.py
    python scripts\\probe_facets.py --meter bms_enyecontrols
    python scripts\\probe_facets.py --json
    python scripts\\probe_facets.py --no-cache
    python scripts\\probe_facets.py --require-facets

Reads the station's own point config (``min``/``max``/``precision``/``unit``
facets on each Niagara numeric point) for every point in the oBIX read map and
writes ``config/obix_facets.json``. The validator picks that cache up
automatically, so the authored ``config/obix_requirements.yaml`` becomes the
fallback instead of the only input.

Read-only: it never writes to MySQL or to Neoview. Exit code is 1 when the
station cannot be read (useful for a scheduled check), 0 otherwise.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.config import Settings  # noqa: E402
from gateway.obix import ObixClient  # noqa: E402
from gateway.obix_facets import probe_facets, save_facets_cache  # noqa: E402
from gateway.obix_sync import load_point_map  # noqa: E402


def _build_client(settings: Settings) -> ObixClient:
    return ObixClient(
        settings.obix_base_url,
        username=settings.obix_username or None,
        password=settings.obix_password or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=settings.obix_verify_tls,
        auth=settings.obix_auth,
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--meter", help="only probe this pseudo-meter id")
    parser.add_argument("--json", action="store_true", help="print facets as JSON")
    parser.add_argument("--no-cache", action="store_true", help="do not write the cache file")
    parser.add_argument(
        "--require-facets",
        action="store_true",
        help="exit 1 when the station exposes no facets at all",
    )
    parser.add_argument("--errors", action="store_true", help="list per-point read errors")
    args = parser.parse_args()

    settings = Settings()
    if not settings.obix_base_url:
        print("OBIX_BASE_URL is not set - nothing to probe", file=sys.stderr)
        return 2

    point_map = load_point_map(settings.obix_point_map)
    if not point_map.get("read"):
        print(
            "no oBIX read map configured (set OBIX_POINT_MAP to config/obix.yaml)",
            file=sys.stderr,
        )
        return 2

    client = _build_client(settings)
    report = probe_facets(client, point_map, meter_id=args.meter)
    print(
        f"probed {report.probed} point(s): {report.with_facets} with facets, "
        f"{report.without_facets} without, {len(report.errors)} error(s)"
        + (" [aborted]" if report.aborted else "")
    )

    if args.json:
        print(json.dumps(report.points, indent=2, sort_keys=True))
    else:
        for key in sorted(report.points):
            facets = report.points[key]
            pretty = ", ".join(f"{name}={value}" for name, value in sorted(facets.items()))
            print(f"  {key}: {pretty}")
        if report.errors and args.errors:
            print("\nerrors:")
            for key, message in sorted(report.errors.items()):
                print(f"  {key}: {message}")

    if not args.no_cache and report.points and not report.aborted:
        target = save_facets_cache(report.points, source=settings.obix_base_url)
        print(f"cache written: {target}")

    if report.aborted:
        return 1
    if args.require_facets and not report.points:
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
