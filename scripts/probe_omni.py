#!/usr/bin/env python3
"""One-off probe for the oBIX-style endpoint on 192.168.0.120.

Reusable, but intentionally scoped to a scratch script: it never touches
src/, tests/, or .env, and it does not change any gateway state.
"""

import argparse
import sys

try:
    import requests
except ImportError:
    print("Missing dependency: pip install requests", file=sys.stderr)
    sys.exit(2)

DEFAULT_URL = (
    "http://192.168.0.120/obix/config/Drivers/"
    "BacnetNetwork/EnyecontrolsBMS_IO/"
    "Total Energy (kWh)_AI11/"
)


def probe(url: str, username: str, password: str) -> int:
    print(f"GET {url}")
    try:
        response = requests.get(url, auth=(username, password), timeout=10)
    except requests.exceptions.ConnectionError as exc:
        print(f"CONNECTION ERROR: {exc}", file=sys.stderr)
        return 1
    except requests.exceptions.Timeout:
        print("TIMEOUT: no response within 10s", file=sys.stderr)
        return 1

    print(f"HTTP {response.status_code}")
    print(f"Content-Type: {response.headers.get('Content-Type', 'n/a')}")
    print(f"Content-Length: {response.headers.get('Content-Length', 'n/a')}")

    body = response.text
    if len(body) > 2000:
        body = body[:2000] + "\n... [truncated] ...\n" + body[-200:]
    print("BODY:")
    print(body)

    if response.status_code in (401, 403):
        print("AUTH FAILURE: bad credentials or server rejected the user.")
        return 1
    if response.status_code >= 400:
        print("CLIENT/SERVER ERROR: see body above.")
        return 1

    print("OK: request succeeded.")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--url",
        default=DEFAULT_URL,
        help="Target endpoint (default: %(default)s)",
    )
    parser.add_argument(
        "--user",
        default="obixuser",
        help="Basic-auth username (default: obixuser)",
    )
    parser.add_argument(
        "--password",
        default="Enyecontrols@12345",
        help="Basic-auth password (default: Enyecontrols@12345)",
    )
    args = parser.parse_args(argv)

    return probe(args.url, args.user, args.password)


if __name__ == "__main__":
    raise SystemExit(main())
