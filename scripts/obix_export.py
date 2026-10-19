"""Export every NeoView oBIX point (with its current value) to a CSV file.

    .\\.venv\\Scripts\\python.exe scripts\\obix_export.py
    .\\.venv\\Scripts\\python.exe scripts\\obix_export.py ^
        --folder /config/Drivers/BacnetNetwork/ENYECONTROLS/points ^
        --folder /config/Drivers/BacnetNetwork/iEM3265_0020343063/points ^
        --out obix_points.csv

Walks each --folder, reads every leaf point, and writes one row per point so
the inventory can be handed to the BMS team. Columns:

    device, meter, point_id, name, href, value, unit, type, status

`name` is the URL-decoded Niagara name (``$20`` -> space, ``$3a`` -> ``:``),
`value`/`unit` are the point's current reading, and `status` is ``ok`` or the
read error. Writes UTF-8 with BOM so Excel renders units such as ug/m3 and °C.
"""

from __future__ import annotations

import argparse
import csv
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import Settings  # noqa: E402
from gateway.obix import ObixClient, ObixError  # noqa: E402

DEFAULT_FOLDERS = [
    "/config/Drivers/BacnetNetwork/ENYECONTROLS/points",
    "/config/Drivers/BacnetNetwork/iEM3265_0020343063/points",
]

_DECODE = re.compile(r"\$([0-9A-Fa-f]{2})")
COLUMNS = ["device", "meter", "point_id", "name", "href", "value", "unit", "type", "status"]


def decode_name(name: str) -> str:
    """Turn Niagara's ``$XX`` percent-encoding back into readable text."""
    return _DECODE.sub(lambda m: chr(int(m.group(1), 16)), name or "")


def slug_name(name: str) -> str:
    text = re.sub(r"[^a-z0-9]+", "_", decode_name(name).strip().lower()).strip("_")
    return text or "point"


def meter_slug(folder: str) -> str:
    parts = [p for p in folder.strip("/").split("/") if p]
    tail = parts[-2] if len(parts) >= 2 and parts[-1] == "points" else parts[-1]
    return "bms_" + re.sub(r"[^a-z0-9]+", "_", tail.lower()).strip("_")


def unit_text(unit: str | None) -> str:
    return (unit or "").replace("obix:units/", "").strip()


def scan(client: ObixClient, folder: str, depth: int, rel: str = ""):
    """Yield ``(point_id, name, href)`` for every non-folder ref under folder."""
    try:
        refs = client.list_refs(folder)
    except ObixError as exc:
        yield ("", rel or folder, f"!{exc}")
        return
    for ref in refs:
        is_folder = "folder" in (ref.is_type or "").lower()
        child = f"{rel}/{ref.href}".strip("/") if ref.href else rel
        if is_folder and depth > 1 and ref.href:
            yield from scan(client, f"{folder.rstrip('/')}/{ref.href}", depth - 1, child)
        elif not is_folder:
            href = f"{folder.rstrip('/')}/{ref.href}" if ref.href else folder
            yield (slug_name(child.split("/")[-1]), decode_name(ref.name or child.split("/")[-1]), href)


def main() -> int:
    parser = argparse.ArgumentParser(description="Export oBIX points to CSV.")
    parser.add_argument("--folder", action="append", default=None,
                        help="oBIX folder to scan (repeatable; defaults to the two BMS devices)")
    parser.add_argument("--depth", type=int, default=3, help="folder recursion depth (default: 3)")
    parser.add_argument("--out", default="obix_points.csv", help="CSV path (default: obix_points.csv)")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--auth", default=None, choices=["auto", "basic", "scram"])
    parser.add_argument("--no-verify-tls", action="store_true")
    args = parser.parse_args()

    settings = Settings()
    base_url = args.base_url or settings.obix_base_url
    if not base_url:
        print("OBIX_BASE_URL is empty - set it in .env or pass --base-url")
        return 2

    folders = args.folder or DEFAULT_FOLDERS
    out_path = Path(args.out)
    if not out_path.is_absolute():
        out_path = ROOT / out_path

    client = ObixClient(
        base_url,
        username=(args.username if args.username is not None else settings.obix_username) or None,
        password=(args.password if args.password is not None else settings.obix_password) or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=not (args.no_verify_tls or not settings.obix_verify_tls),
        auth=args.auth or settings.obix_auth,
    )

    rows: list[dict[str, str]] = []
    for folder in folders:
        meter = meter_slug(folder)
        for point_id, name, href in scan(client, folder, args.depth):
            row = {"device": folder, "meter": meter, "point_id": point_id, "name": name,
                   "href": href, "value": "", "unit": "", "type": "", "status": "ok"}
            if href.startswith("!"):
                row["href"] = ""
                row["status"] = href[1:]
                rows.append(row)
                continue
            try:
                point = client.get_point(href, raw=True)
            except ObixError as exc:
                row["status"] = str(exc)
            else:
                row["value"] = "" if point.value is None else str(point.value)
                row["unit"] = unit_text(point.unit)
                row["type"] = (point.is_type or "").replace("obix:", "")
            rows.append(row)
        print(f"{folder} -> {sum(1 for r in rows if r['meter'] == meter)} point(s)")

    with out_path.open("w", newline="", encoding="utf-8-sig") as fh:
        writer = csv.DictWriter(fh, fieldnames=COLUMNS)
        writer.writeheader()
        writer.writerows(rows)

    failed = sum(1 for r in rows if r["status"] != "ok")
    print(f"\nwrote {len(rows)} row(s) to {out_path}")
    if failed:
        print(f"{failed} point(s) could not be read (see the status column)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
