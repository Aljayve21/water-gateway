#!/usr/bin/env python3
"""chat.py - interactive chat with the water-meter assistant.

    python scripts\\chat.py                        # chat as the default principal
    python scripts\\chat.py --as ops               # chat as a named principal
    python scripts\\chat.py --user <ems-user-id>   # chat as an EMS user (signed
                                                   # assertion, like ems-backend)
    python scripts\\chat.py "one quick question"   # one-shot, no conversation

Conversation context is kept client-side and sent with every question, so
follow-ups ("and the lowest?") just work. Requires the gateway to be running
(scripts\\start_gateway.ps1). Each question costs one Bedrock call; the rate
limit (AI_RATE_LIMIT_PER_MIN) applies, so pace yourself.

In-chat commands:  /new  clear conversation context
                   /who  show the identity you are chatting as
                   /help  list commands
                   exit | quit | Ctrl+C  leave
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from gateway.config import Settings  # noqa: E402
from ask import _principals, resolve_token  # noqa: E402

MAX_CLIENT_TURNS = 10          # turns of context sent with each question
_MAX_TEXT = 1500               # clamp each stored turn so payloads stay small

_BANNER = """=" * 62
 water-meter AI assistant  -  type a question, 'exit' to leave
 /new clears context  -  /who shows your identity  -  /help
=" * 62"""


def _sign_assertion(secret: str, subject: str) -> str:
    """Mirror of ems-backend's identity proof (see gateway.ai.assertion)."""
    from gateway.ai.assertion import sign_assertion

    return sign_assertion(secret, subject, ttl_seconds=120)


def send(base: str, token: str, question: str, history: list) -> tuple:
    body = {"question": question}
    if history:
        body["history"] = json.dumps(history)
    req = urllib.request.Request(
        base + "/ai/ask",
        method="POST",
        data=json.dumps(body).encode(),
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


def _print_answer(code: int, body: dict, who: str, history: list, question: str) -> int:
    if code == 0:
        print(f"\n! cannot reach the AI endpoint: {body.get('error')}", file=sys.stderr)
        return 1
    if code == 401:
        print("\n! unauthorized - the token/identity was rejected by the gateway", file=sys.stderr)
        return 1
    if code == 429:
        print("\n! rate limit hit - wait about a minute, then ask again", file=sys.stderr)
        return 1
    if code != 200:
        print(f"\n! HTTP {code}: {json.dumps(body)[:300]}", file=sys.stderr)
        return 1

    answer = body.get("answer") or "(no answer)"
    print(f"\nassistant: {answer}")

    calls = body.get("tool_calls") or []
    meta = []
    if calls:
        meta.append("tools=" + ",".join(c.get("tool", "?") for c in calls))
    meta.append(f"hops={body.get('hops', 0)}")
    usage = body.get("usage") or {}
    if usage.get("totalTokens"):
        meta.append(f"tokens={usage['totalTokens']}")
    print(f"   [{who} | {' | '.join(meta)}]", file=sys.stderr)

    # remember the exchange for the next question's context
    history.append({"role": "user", "text": question[:_MAX_TEXT]})
    history.append({"role": "assistant", "text": answer[:_MAX_TEXT]})
    del history[:-MAX_CLIENT_TURNS]
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="Chat with the water-meter assistant.")
    parser.add_argument("question", nargs="*", help="one-shot question (no conversation)")
    parser.add_argument("--as", dest="subject", default=None, help="principal to authenticate as")
    parser.add_argument("--user", dest="ems_user", default=None, help="EMS users.id - authenticate with a signed assertion, exactly like ems-backend does")
    parser.add_argument("--url", default=None, help="AI base URL (default from config)")
    args = parser.parse_args()

    settings = Settings()
    base = (args.url or f"http://127.0.0.1:{settings.ai_bind_port}").rstrip("/")

    if args.ems_user:
        if not settings.ai_assertion_secret:
            print("error: AI_ASSERTION_SECRET is not set - cannot mint assertions", file=sys.stderr)
            return 2
        token = _sign_assertion(settings.ai_assertion_secret, args.ems_user)
        who = f"EMS user {args.ems_user} (signed assertion)"
    else:
        token, description = resolve_token(settings, args.subject)
        if not token:
            print(f"error: {description}", file=sys.stderr)
            print("       check AI_PRINCIPALS / AI_BEARER_TOKEN in .env", file=sys.stderr)
            return 2
        who = description

    history: list = []

    # one-shot mode
    if args.question:
        question = " ".join(args.question).strip()
        code, body = send(base, token, question, history)
        return _print_answer(code, body, who, history, question)

    print("=" * 62)
    print(" water-meter AI assistant - type a question, 'exit' to leave")
    print(f" chatting as: {who}")
    print(" /new clears context   /who shows identity   /help")
    print("=" * 62)

    while True:
        try:
            line = input("\nyou> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            return 0
        if not line:
            continue
        if line.lower() in ("exit", "quit"):
            return 0
        if line == "/new":
            history.clear()
            print("(conversation context cleared)")
            continue
        if line == "/who":
            print(f"chatting as: {who}")
            continue
        if line == "/help":
            print("/new  /who  /help  exit - each question uses your meter tools")
            continue

        code, body = send(base, token, line, history)
        if _print_answer(code, body, who, history, line) != 0 and code == 0:
            return 1


if __name__ == "__main__":
    sys.exit(main())
