#!/usr/bin/env python3
"""ask.py - ask the water-meter assistant one question from the command line.

    python scripts\\ask.py "which meter used the most today?"
    python scripts\\ask.py --as maria "what is my totalizer?"

Reads the bearer token from AI_PRINCIPALS (matching --as, or the single
AI_BEARER_TOKEN), so nothing secret needs to be typed or passed on the command
line. Costs one Bedrock call per question.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

from gateway.config import Settings  # noqa: E402


def _principals(settings: Settings) -> dict:
    """subject -> token, built from AI_PRINCIPALS."""
    out = {}
    raw = (settings.ai_principals or "").replace("\r\n", "\n").replace("\r", "\n")
    for chunk in raw.replace("\n", ";").split(";"):
        line = chunk.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(":", 3)
        if len(parts) != 4:
            continue
        subject, _role, _meters, token = (p.strip() for p in parts)
        if subject and token:
            out[subject] = token
    return out


def resolve_token(settings: Settings, subject: str | None) -> tuple:
    """Return (token, description) for --as, or the legacy shared token."""
    principals = _principals(settings)
    if subject:
        if subject in principals:
            return principals[subject], f"principal '{subject}'"
        return None, f"unknown principal '{subject}'"
    if principals:
        first = next(iter(principals))
        return principals[first], f"principal '{first}' (only one configured)"
    if settings.ai_bearer_token:
        return settings.ai_bearer_token, "AI_BEARER_TOKEN"
    return None, "no token configured"


def ask(base: str, token: str, question: str) -> tuple:
    req = urllib.request.Request(
        base + "/ai/ask",
        method="POST",
        data=json.dumps({"question": question}).encode(),
        headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=180) as resp:
            return resp.status, json.loads(resp.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"error": raw[:300]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": f"{type(exc).__name__}: {exc}"}


def main() -> int:
    parser = argparse.ArgumentParser(description="Ask the water-meter assistant.")
    parser.add_argument("question", nargs="+", help="the question to ask")
    parser.add_argument("--as", dest="subject", default=None, help="principal to authenticate as")
    parser.add_argument("--url", default=None, help="AI base URL (default from config)")
    args = parser.parse_args()

    settings = Settings()
    base = (args.url or f"http://127.0.0.1:{settings.ai_bind_port}").rstrip("/")

    token, description = resolve_token(settings, args.subject)
    if not token:
        print(f"error: {description}", file=sys.stderr)
        print("       set AI_PRINCIPALS or AI_BEARER_TOKEN in .env", file=sys.stderr)
        return 2

    question = " ".join(args.question).strip()
    code, body = ask(base, token, question)

    if code == 0:
        print(f"error: {body.get('error')}", file=sys.stderr)
        print(f"       is the gateway running on {base}? try scripts\\start_gateway.ps1", file=sys.stderr)
        return 1
    if code == 401:
        print("error: unauthorized (token does not match any principal)", file=sys.stderr)
        return 1
    if code != 200:
        print(f"error: HTTP {code}: {json.dumps(body)[:300]}", file=sys.stderr)
        return 1

    print(body.get("answer") or "(no answer)")
    calls = body.get("tool_calls") or []
    meta = [f"hops={body.get('hops', 0)}"]
    if calls:
        meta.append("tools=" + ",".join(c.get("tool", "?") for c in calls))
    if body.get("truncated"):
        meta.append("truncated")
    usage = body.get("usage") or {}
    if usage.get("totalTokens"):
        meta.append(f"tokens={usage['totalTokens']}")
    print(f"\n[{description} | {' | '.join(meta)}]", file=sys.stderr)
    return 0


if __name__ == "__main__":
    sys.exit(main())
