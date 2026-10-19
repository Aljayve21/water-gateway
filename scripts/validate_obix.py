#!/usr/bin/env python3
"""validate_obix.py - check live oBIX values against configured requirements.

    python scripts\\validate_obix.py
    python scripts\\validate_obix.py --meter bms_enyecontrols
    python scripts\\validate_obix.py --json
    python scripts\\validate_obix.py --fail-on warning
    python scripts\\validate_obix.py --no-facets

Reads the latest value of every point from the oBIX database and runs the
rules in gateway.obix_validation against config/obix_requirements.yaml. When a
Neoview facet cache exists (see scripts/probe_facets.py) its station-provided
bounds are used too; --no-facets ignores that cache.
Read-only: it never writes to MySQL or to Neoview.

Exit code: 1 if any finding is at least as severe as --fail-on, else 0.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.config import Settings  # noqa: E402
from gateway.obix_store import build_obix_store  # noqa: E402
from gateway.obix_validation import (  # noqa: E402
    SEVERITY_ORDER,
    ObixValidator,
    load_requirements,
)

DEFAULT_REQUIREMENTS = "config/obix_requirements.yaml"


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate oBIX values vs requirements.")
    parser.add_argument("--requirements", default=None, help="path to the requirements YAML")
    parser.add_argument("--meter", default=None, help="limit to one meter id")
    parser.add_argument("--json", action="store_true", help="emit findings as JSON")
    parser.add_argument(
        "--no-facets",
        action="store_true",
        help="ignore the Neoview facet cache and use the authored file only",
    )
    parser.add_argument(
        "--fail-on",
        choices=["info", "warning", "critical", "never"],
        default="critical",
        help="minimum severity that makes the command exit non-zero (default critical)",
    )
    args = parser.parse_args()

    settings = Settings()
    store = build_obix_store(settings)
    if store is None:
        print("error: oBIX database unavailable (check OBIX_MYSQL_*)", file=sys.stderr)
        return 2

    try:
        requirements = load_requirements(
            args.requirements, facets={} if args.no_facets else None
        )
        validator = ObixValidator(requirements)
        rows = store.latest(meter_id=args.meter, limit=None)
        history_for = (
            (lambda m, p: store.history(m, p, limit=None))
            if requirements.frozen_enabled
            else None
        )
        findings = validator.scan(rows, history_for=history_for)
        source = args.requirements or DEFAULT_REQUIREMENTS
    finally:
        store.close()

    facet_note = (
        f" [{len(requirements.facets)} Neoview facet(s)]"
        if requirements.facets
        else ""
    )
    if args.json:
        print(json.dumps([f.as_dict() for f in findings], indent=2))
    else:
        print(f"scanned {len(rows)} point value(s) against {source}{facet_note}")
        if not findings:
            print("OK - no findings")
        else:
            grouped: dict[str, list] = {}
            for finding in findings:
                grouped.setdefault(finding.severity, []).append(finding)
            for severity in sorted(
                grouped, key=lambda s: -SEVERITY_ORDER.get(s, -1)
            ):
                items = grouped[severity]
                print(f"\n{severity.upper()} ({len(items)})")
                for finding in items[:40]:
                    print(f"  [{finding.rule}] {finding.meter_id}/{finding.point_id}: {finding.message}")
                if len(items) > 40:
                    print(f"  ... and {len(items) - 40} more")

    if args.fail_on == "never":
        return 0
    threshold = SEVERITY_ORDER.get(args.fail_on, 0)
    worst = max((SEVERITY_ORDER.get(f.severity, 0) for f in findings), default=-1)
    return 1 if worst >= threshold else 0


if __name__ == "__main__":
    sys.exit(main())
