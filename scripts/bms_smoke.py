#!/usr/bin/env python3
"""bms_smoke.py - exercise the read-only /bms/* REST endpoints.

    python scripts\\bms_smoke.py
    python scripts\\bms_smoke.py --as ops

Starts nothing: point it at a running gateway. The bearer token is read from
AI_PRINCIPALS (matching --as, or the first principal), so no secret needs to be
typed on the command line. Read-only and free (no Bedrock call).
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.config import Settings  # noqa: E402


def _principals(settings: Settings) -> dict:
    """subject -> token, built from AI_PRINCIPALS (entries split on newline/semicolon)."""
    out = {}
    raw = settings.ai_principals or ""
    for chunk in re.split(r"[\n\r;]+", raw):
        text = chunk.strip()
        if not text or text.startswith("#"):
            continue
        parts = text.split(":", 3)
        if len(parts) != 4:
            continue
        subject, _role, _meters, token = (p.strip() for p in parts)
        if subject and token:
            out[subject] = token
    return out


def _resolve_token(settings: Settings, subject: str | None) -> tuple:
    principals = _principals(settings)
    if subject:
        if subject in principals:
            return principals[subject], f"principal '{subject}'"
        return None, f"unknown principal '{subject}'"
    if principals:
        first = next(iter(principals))
        return principals[first], f"principal '{first}' (first configured)"
    if settings.ai_bearer_token:
        return settings.ai_bearer_token, "AI_BEARER_TOKEN"
    return None, "no token configured"


def _call(base: str, token: str, path: str) -> tuple:
    req = urllib.request.Request(base + path, headers={"Authorization": f"Bearer {token}"})
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        try:
            return exc.code, json.loads(exc.read().decode())
        except ValueError:
            return exc.code, {}
    except urllib.error.URLError as exc:
        return 0, {"error": str(exc.reason)}


def main() -> int:
    parser = argparse.ArgumentParser(description="Smoke-test the /bms/* endpoints.")
    parser.add_argument("--as", dest="subject", default=None, help="principal to authenticate as")
    parser.add_argument("--url", default=None, help="AI base URL (default from config)")
    args = parser.parse_args()

    settings = Settings()
    base = (args.url or f"http://127.0.0.1:{settings.ai_bind_port}").rstrip("/")
    token, description = _resolve_token(settings, args.subject)
    if not token:
        print(f"error: {description}", file=sys.stderr)
        print("       set AI_PRINCIPALS or AI_BEARER_TOKEN in .env", file=sys.stderr)
        return 2

    print(f"target {base}  auth {description}")
    failures = 0

    for path in ("/bms/points", "/bms/devices", "/bms/alarms", "/bms/status"):
        code, body = _call(base, token, path)
        count = body.get("count") if isinstance(body, dict) else None
        print(f"  GET {path:<16} {code}  count={count}")
        failures += code != 200

    code, body = _call(base, token, "/bms/points")
    meters = body.get("meters", []) if isinstance(body, dict) else []
    total = body.get("total") if isinstance(body, dict) else None
    print(f"  meters: {meters}  total points: {total}")

    # Pagination: a limit=1 page must return one row but still report the total.
    code, page = _call(base, token, "/bms/points?limit=1&offset=0")
    ok = code == 200 and page.get("count") == 1 and page.get("total") == total
    print(f"  GET /bms/points?limit=1  {code}  count={page.get('count')} total={page.get('total')}")
    failures += not ok

    if meters:
        meter = meters[0]
        code, body = _call(base, token, f"/bms/points/{meter}")
        print(f"  GET /bms/points/{meter}  {code}  count={body.get('count')}")
        failures += code != 200
        points = body.get("points", [])
        if points:
            point_id = points[0]["point_id"]
            code, detail = _call(base, token, f"/bms/points/{meter}/{point_id}")
            print(f"  GET /bms/points/{meter}/{point_id}  {code}  value={detail.get('value')}")
            failures += code != 200
            code, body = _call(
                base, token, f"/bms/history?meter_id={meter}&point_id={point_id}"
            )
            print(f"  GET /bms/history  {code}  count={body.get('count')}")
            failures += code != 200

    code, body = _call(base, token, "/bms/history")
    print(f"  GET /bms/history (no params)  {code}  {body.get('error')}")
    failures += code != 400

    print("OK" if not failures else f"{failures} check(s) failed")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(main())
