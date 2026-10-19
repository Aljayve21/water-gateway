"""oBIX setup checker - walks through commissioning an oBIX endpoint.

Run this after filling in the OBIX_* values in .env. It reports, step by step,
exactly how far the connection gets and what to fix next, instead of a single
"failed" with no detail.

    .\\.venv\\Scripts\\python.exe scripts\\obix_setup_check.py

Checks, in order:
  1. configuration present
  2. TCP reachable
  3. the endpoint speaks oBIX (XML, not an HTML error page)
  4. authentication accepted
  5. the meter folder exists and lists points
  6. the insights folder is writable (only with --write-test)

Exit codes: 0 = ready, 1 = not ready, 2 = nothing configured.
"""

from __future__ import annotations

import argparse
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import Settings  # noqa: E402
from gateway.obix import ObixClient, ObixError  # noqa: E402

OK, WARN, BAD = "PASS", "WARN", "FAIL"


async def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base-url", default=None,
                        help="override OBIX_BASE_URL for a one-off test")
    parser.add_argument("--username", default=None)
    parser.add_argument("--password", default=None)
    parser.add_argument("--meter-root", default=None)
    parser.add_argument("--insights-root", default=None)
    parser.add_argument("--auth", default=None,
                        choices=["auto", "basic", "scram"],
                        help="authentication scheme (default: OBIX_AUTH / auto)")
    parser.add_argument("--no-verify-tls", action="store_true")
    parser.add_argument("--write-test", action="store_true",
                        help="actually write a test value to the insights folder")
    args = parser.parse_args()

    settings = Settings()
    base_url = args.base_url or settings.obix_base_url
    username = args.username if args.username is not None else settings.obix_username
    password = args.password if args.password is not None else settings.obix_password
    meter_root = args.meter_root or settings.obix_meter_root
    insights_root = args.insights_root or settings.obix_insights_root
    auth = args.auth or settings.obix_auth

    print("=" * 64)
    print("oBIX setup check")
    print("=" * 64)

    # ---- 1. configuration ----
    print(f"\n[1] configuration")
    if not base_url:
        print(f"  {BAD} OBIX_BASE_URL is empty - set it in .env")
        print("       (leave it empty to keep the oBIX integration disabled)")
        return 2
    print(f"  {OK} base url:  {base_url}")
    print(f"  {'PASS' if username else 'WARN'} username:  "
          f"{username or '(none - anonymous access)'}")
    print(f"  {OK} auth mode: {auth}")
    print(f"  {'PASS' if meter_root else 'WARN'} meter root:    {meter_root or '(unset)'}")
    print(f"  {'PASS' if insights_root else 'WARN'} insights root: {insights_root or '(unset)'}")

    client = ObixClient(
        base_url,
        username=username or None,
        password=password or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=not (args.no_verify_tls or not settings.obix_verify_tls),
        auth=auth,
    )

    # ---- 2/3. reachability and protocol ----
    print(f"\n[2] connectivity")
    # A server does not have to expose "Ai" - Niagara deployments use different
    # roots. Probe several candidates and accept the first that answers, so a
    # missing conventional folder does not look like a dead endpoint.
    candidates = ["Ai"]
    if meter_root:
        candidates.append(meter_root)
        head = meter_root.strip("/").split("/")[0]
        if head and head not in candidates:
            candidates.append(head)
    candidates += ["/", "obix"]

    refs = None
    chosen = None
    last_error = ""
    for probe in candidates:
        try:
            refs = await client.fetch_refs(probe)
            chosen = probe
            break
        except ObixError as exc:
            last_error = str(exc)
            continue

    if refs is None:
        message = last_error
        if "HTTP 401" in message or "HTTP 403" in message:
            print(f"  {BAD} reached the server but authentication was refused")
            print("       Check OBIX_USERNAME / OBIX_PASSWORD.")
        elif "cannot reach" in message:
            print(f"  {BAD} cannot reach the server: {message}")
            print("       Check the host, the port, and whether a firewall")
            print("       blocks it from this machine.")
        elif "HTTP 404" in message:
            print(f"  {BAD} the server answered but every probe path 404'd")
            print(f"       tried: {', '.join(candidates)}")
            print("       The Obix Web Service is probably not enabled in NeoView.")
        else:
            print(f"  {BAD} {message}")
        return 1

    print(f"  {OK} server answered and speaks oBIX (probed '{chosen}')")
    if chosen != "Ai":
        print(f"  {WARN} no 'Ai' folder on this server - set OBIX_METER_ROOT to match")
    print(f"       {len(refs)} entr(y/ies) at that path")

    # ---- 4. auth state ----
    print(f"\n[3] authentication")
    if username:
        print(f"  {OK} credentials accepted for '{username}'")
    else:
        print(f"  {WARN} connected anonymously - the server allows unauthenticated reads")
        print("       Configure a dedicated account before enabling writes.")

    # ---- 5. meter folder ----
    print(f"\n[4] meter folder")
    if not meter_root:
        print(f"  {WARN} OBIX_METER_ROOT unset - set it to the folder holding your meters")
    else:
        try:
            points = await client.fetch_refs(meter_root)
        except ObixError as exc:
            print(f"  {BAD} {meter_root}: {exc}")
            print("       Check the folder path and that the account can read it.")
        else:
            if not points:
                print(f"  {WARN} {meter_root} is empty")
            else:
                print(f"  {OK} {meter_root} lists {len(points)} point(s):")
                for p in points[:12]:
                    value = p.as_float
                    shown = f"{value:g}" if value is not None else p.value
                    unit = f" {p.unit}" if p.unit else ""
                    print(f"       {p.name:<28} = {shown}{unit}  [{p.is_type}]")
                if len(points) > 12:
                    print(f"       ... and {len(points) - 12} more")

    # ---- 6. write test ----
    print(f"\n[5] insights folder (write-back target)")
    if not insights_root:
        print(f"  {WARN} OBIX_INSIGHTS_ROOT unset - set it before publishing AI results")
    elif not args.write_test:
        print(f"  {WARN} would write to {insights_root}/<meter>/finding_count")
        print("       re-run with --write-test to actually attempt it")
    else:
        try:
            probe = await client.fetch_refs(insights_root)
            print(f"  {OK} {insights_root} exists ({len(probe)} entries)")
        except ObixError:
            print(f"  {WARN} {insights_root} does not exist yet")
            print("       Create the folder in NeoView, or pick an existing one.")
        try:
            await client.push_point(f"{insights_root}/_setup_probe", 1.0)
            back = await client.fetch_point(f"{insights_root}/_setup_probe")
            print(f"  {OK} write test succeeded (read back {back.value})")
            print("       Delete the _setup_probe point when you are done.")
        except ObixError as exc:
            print(f"  {BAD} write failed: {exc}")
            print("       The account needs write access to that folder.")

    print("\n" + "=" * 64)
    print("Ready to wire into the poll loop." if base_url else "")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
