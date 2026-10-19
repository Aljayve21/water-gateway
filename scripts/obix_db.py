"""Manage the oBIX MySQL database (separate from the gateway buffer).

Commands:
    init      - create tables in OBIX_MYSQL_DATABASE
    show      - show latest values, points count, etc.
    sync      - pull from the live station (same as the point map) and store catalog + values
    runs      - show the last few read runs
    clear     - truncate the history tables (keep device/point catalog)

Reads OBIX_* from .env. Defaults to ``obix`` database, reusing BUFFER_MYSQL_* if not set.
"""

from __future__ import annotations

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import Settings, get_obix_mysql_settings  # noqa: E402
from gateway.obix import ObixClient, ObixError  # noqa: E402
from gateway.obix_store import ObixStore, build_obix_store  # noqa: E402
from gateway.obix_sync import load_point_map, ObixBridge  # noqa: E402
from gateway.store import MySQLBuffer  # noqa: E402
from gateway.config import get_buffer_mysql_settings  # noqa: E402


def _fmt(d):
    return d.astimezone(timezone.utc).isoformat(timespec="seconds") if d else "-"


def cmd_init(store: ObixStore) -> int:
    store.initialize()
    print("obix DB initialised:", store.database)
    print(store.counts())
    return 0


def cmd_show(store: ObixStore) -> int:
    c = store.counts()
    for k in sorted(c):
        print(f"{k}: {c[k]}")
    lat = store.latest()
    print(f"\nlatest values: {len(lat)}")
    if lat:
        for r in lat[:10]:
            v = r["value"] if r["value"] is not None else r["text_value"]
            print(f"{r['meter_id']:<24} {r['point_id']:<38} = {v} {r['unit']} @ {r['captured_utc']}")
        if len(lat) > 10:
            print("... and", len(lat) - 10, "more")
    with store._conn.cursor() as cur:
        cur.execute("SELECT * FROM obix_read_run ORDER BY id DESC LIMIT 5")
        runs = cur.fetchall()
        if runs:
            print("\nread runs:")
            for rr in runs:
                print(rr)
    return 0


def cmd_runs(store: ObixStore) -> int:
    with store._conn.cursor() as cur:
        cur.execute("SELECT * FROM obix_read_run ORDER BY id DESC LIMIT 20")
        runs = cur.fetchall()
    if not runs:
        print("no runs")
        return 0
    for rr in runs:
        print(rr)
    return 0


def cmd_clear(store: ObixStore) -> int:
    with store._conn.cursor() as cur:
        for t in ("obix_point_value", "obix_point_history", "obix_read_run"):
            cur.execute(f"TRUNCATE TABLE {t}")
    print("cleared values, history, runs")
    return 0


def cmd_sync(store: ObixStore) -> int:
    settings = Settings()
    if not settings.obix_base_url:
        print("OBIX_BASE_URL is empty")
        return 2
    client = ObixClient(
        settings.obix_base_url,
        username=settings.obix_username or None,
        password=settings.obix_password or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=settings.obix_verify_tls,
        auth=settings.obix_auth,
    )
    pm = load_point_map(settings.obix_point_map)
    bridge = ObixBridge(client, engine=None, point_map=pm)
    started = datetime.now(timezone.utc)
    values = bridge.fetch()
    dur_ms = int((datetime.now(timezone.utc) - started).total_seconds() * 1000)
    if pm.get("read"):
        for meter_id, points in pm["read"].items():
            entries = []
            for pid, href in (points or {}).items():
                display = pid.replace("_", " ").strip()
                entries.append(
                    {
                        "rel_path": pid,
                        "href": href,
                        "name": pid,
                        "display_name": display,
                        "is_folder": False,
                        "is_type": "",
                        "depth": 0,
                        "point_id": pid,
                    }
                )
            store.sync_device(meter_id, pm_path := settings.obix_point_map, entries=entries, name=meter_id)
    store.record_values(values)
    store.record_run(
        started.isoformat(timespec="seconds"),
        datetime.now(timezone.utc).isoformat(timespec="seconds"),
        len(values),
        0,
        dur_ms,
    )
    print(f"synced: {len(values)} points in {dur_ms}ms; db={store.database}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("cmd", choices=["init", "show", "sync", "runs", "clear"])
    args = parser.parse_args()

    store = build_obix_store(Settings())
    if store is None:
        print("obix database not configured (empty OBIX_MYSQL_DATABASE?)")
        return 2
    try:
        if args.cmd == "init":
            return cmd_init(store)
        if args.cmd == "show":
            return cmd_show(store)
        if args.cmd == "sync":
            return cmd_sync(store)
        if args.cmd == "runs":
            return cmd_runs(store)
        if args.cmd == "clear":
            return cmd_clear(store)
    finally:
        store.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
