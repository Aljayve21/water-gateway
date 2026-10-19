"""List NeoView oBIX points so config/obix.yaml can be filled accurately.

    .\\.venv\\Scripts\\python.exe scripts\\obix_points.py
    .\\.venv\\Scripts\\python.exe scripts\\obix_points.py --path /config/Drivers/BacnetNetwork/ENYECONTROLS/points --depth 1

Prints each point's name, href and current display value. Copy the href into
the read/write blocks of config/obix.yaml (Niagara keeps the $20/$2d/$3a
encoding in the path).
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import Settings  # noqa: E402
from gateway.obix import ObixClient, ObixError  # noqa: E402

DEFAULT_PATH = "/config/Drivers/BacnetNetwork/ENYECONTROLS/points"


def walk(client: ObixClient, path: str, depth: int, indent: str = "") -> int:
    """Print the points/folders under ``path``; recurse into folders."""
    try:
        refs = client.list_refs(path)
    except ObixError as exc:
        print(f"{indent}! {path}: {exc}")
        return 0

    count = 0
    for ref in refs:
        is_folder = "folder" in (ref.is_type or "").lower()
        marker = "[dir]" if is_folder else "     "
        value = ref.value if ref.value is not None else ""
        print(f"{indent}{marker} {ref.name:<40} {value}")
        if ref.href:
            print(f"{indent}       {ref.href}")
        count += 1
        if is_folder and depth > 1 and ref.href:
            count += walk(client, ref.href, depth - 1, indent + "    ")
    return count


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--auth", default=None, choices=["auto", "basic", "scram"])
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--path", default=None,
                        help=f"oBIX path to list (default: {DEFAULT_PATH})")
    parser.add_argument("--depth", type=int, default=2,
                        help="how many folder levels to descend (default: 2)")
    args = parser.parse_args()

    settings = Settings()
    base_url = args.base_url or settings.obix_base_url
    if not base_url:
        print("OBIX_BASE_URL is empty - set it in .env or pass --base-url")
        return 2

    client = ObixClient(
        base_url,
        username=(args.username if args.username is not None else settings.obix_username) or None,
        password=(args.password if args.password is not None else settings.obix_password) or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=not (args.no_verify_tls or not settings.obix_verify_tls),
        auth=args.auth or settings.obix_auth,
    )
    path = args.path or settings.obix_meter_root or DEFAULT_PATH
    print(f"{base_url}{path}\n")
    total = walk(client, path, args.depth)
    print(f"\n{total} entr(y/ies).")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
