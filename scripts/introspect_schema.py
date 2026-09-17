"""Read-only schema introspection against the office mdpf MySQL database.

Helps verify which mdpf tables/columns actually exist before the extractor
phase relies on them. Runs informational SELECTs against
``information_schema`` ONLY — it never changes the database.

Usage:
    python scripts/introspect_schema.py            # uses .env credentials
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC = PROJECT_ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def main() -> int:
    from gateway.config import Settings, load_gateway_config
    from gateway.logging_setup import setup_logging

    parser = argparse.ArgumentParser(
        description="Read-only schema introspection against office mdpf."
    )
    parser.parse_args()

    settings = Settings()
    fp = settings.fingerprint()
    setup_logging(context=fp)
    load_gateway_config()

    print(f"Target database: {settings.mysql_database!r}")
    print(f"Config fingerprint: {fp}")
    print("Read-only mode: SELECT-only introspection, no writes.")
    print("Schema introspection not yet wired to a live connection (Phase 3).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())