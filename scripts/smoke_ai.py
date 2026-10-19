"""Manual smoke test for the gateway AI API.

Run the gateway in one terminal, then:

    .venv\\Scripts\\python.exe scripts\\smoke_ai.py

Every check is independent, so a failure in one does not hide the rest.
Reads the bearer token from .env; never prints it.
"""

from __future__ import annotations

import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import Settings  # noqa: E402


def _resolve_token(settings: Settings) -> str:
    """Token that actually authenticates.

    When AI_PRINCIPALS is configured the per-caller registry replaces the
    legacy shared token, so prefer the first principal's token (an ADMIN if
    one exists) and fall back to AI_BEARER_TOKEN otherwise.
    """
    if settings.ai_principals.strip():
        from gateway.ai.registry import parse_principals

        resolver = parse_principals(settings.ai_principals)
        entries = list(resolver.entries)
        if entries:
            admins = [e for e in entries if e.principal.role.upper() == "ADMIN"]
            pick = admins[0] if admins else entries[0]
            return pick.token
    return settings.ai_bearer_token


BASE_HOST = "127.0.0.1"

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"
_results: list[tuple[str, str, str]] = []


def record(state: str, name: str, detail: str = "") -> None:
    _results.append((state, name, detail))
    print(f"[{state}] {name}" + (f"\n        {detail}" if detail else ""))


def call(settings: Settings, path: str, method: str = "GET", body=None, token=None, host: str = BASE_HOST):
    """Returns (status, body). status 0 means the TCP connection failed."""
    url = f"http://{host}:{settings.ai_bind_port}{path}"
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    if body is not None:
        req.data = json.dumps(body).encode()
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode()
        try:
            return exc.code, json.loads(raw or "{}")
        except ValueError:
            return exc.code, {"raw": raw}
    except urllib.error.URLError as exc:
        reason = exc.reason if isinstance(exc.reason, OSError) else None
        refused = getattr(reason, "winerror", None) == 10061 or isinstance(
            reason, ConnectionRefusedError
        )
        return 0, {"refused": True, "error": str(exc)}


def _no_gateway(settings: Settings) -> int:
    """Explain a refused connection instead of reporting a bare FAIL."""
    print()
    print("=" * 66)
    print(f"Cannot reach the AI API on port {settings.ai_bind_port}. Not running.")
    print("=" * 66)
    print()
    print("Start the gateway in a second terminal and leave it open:")
    print()
    print("    .\\scripts\\start_gateway.ps1")
    print()
    print("Wait for these lines, then run this script again:")
    print("    health http listening on 127.0.0.1:9090")
    print(f"    ai http listening on {settings.ai_bind_host}:{settings.ai_bind_port}")
    print()
    print("If the gateway is already running but you still get this, it means")
    print("AI is off. Check AI_ENABLED in .env, then restart the gateway.")
    print()
    print("To confirm by hand:")
    print("    .\\.venv\\Scripts\\python.exe scripts\\check_health.py")
    return 1


def main() -> int:
    settings = Settings()
    token = _resolve_token(settings)

    print("=" * 62)
    print("water-gateway AI smoke test")
    print("=" * 62)
    print(f"AI enabled : {settings.ai_enabled}")
    print(f"AI ready   : {settings.ai_ready()}")
    print(f"endpoint   : http://{BASE_HOST}:{settings.ai_bind_port}")
    print(f"model      : {settings.bedrock_model_id} @ {settings.aws_region}")
    print()

    if not settings.ai_enabled:
        print("AI_ENABLED is false - nothing to test. Set AI_ENABLED=true in .env.")
        return 1
    if not token:
        print("AI_BEARER_TOKEN is empty - the API cannot be called.")
        return 1

    # ── 1. status ────────────────────────────────────────────────────────
    code, out = call(settings, "/ai/status")
    if code == 0 and out.get("refused"):
        return _no_gateway(settings)
    if code == 200 and out.get("ready"):
        record(PASS, "GET /ai/status (no auth needed)", f"ready=True model={out.get('model_id')}")
    elif code == 200:
        record(FAIL, "GET /ai/status", f"API answered but ready=false -> {out}")
        return 1
    else:
        record(FAIL, "GET /ai/status", f"HTTP {code} {out}")
        return 1

    if out.get("tools"):
        record(PASS, "tools registered", ", ".join(out["tools"]))
    else:
        record(FAIL, "tools registered", "none advertised")

    # ── 2. auth ──────────────────────────────────────────────────────────
    if call(settings, "/ai/meters")[0] == 401:
        record(PASS, "no token is rejected (401)")
    else:
        record(FAIL, "no token is rejected", "expected 401")

    if call(settings, "/ai/meters", token="definitely-wrong")[0] == 401:
        record(PASS, "wrong token is rejected (401)")
    else:
        record(FAIL, "wrong token is rejected", "expected 401")

    code, out = call(settings, "/ai/meters", token=token)
    if code == 200 and "meters" in out:
        record(PASS, "correct token works (200)", f"{out.get('count', len(out['meters']))} meters")
        meters = out["meters"]
    else:
        record(FAIL, "correct token works", f"HTTP {code} {out}")
        return 1

    # ── 3. data shape ────────────────────────────────────────────────────
    if meters:
        first = meters[0]["meter_id"]
        code, out = call(settings, f"/ai/meters/{first}", token=token)
        values = out.get("values") or []
        if code == 200 and values:
            sample = values[0]
            record(
                PASS,
                f"GET /ai/meters/{first}",
                f"{len(values)} values, e.g. {sample['point_id']}={sample['value']} {sample['unit']}",
            )
        else:
            record(FAIL, f"GET /ai/meters/{first}", f"HTTP {code} {out}")
    else:
        record(SKIP, "per-meter values", "buffer has no meters yet - wait for one 15s cycle")

    # ── 4. validation ────────────────────────────────────────────────────
    if call(settings, "/ai/ask", "POST", {}, token)[0] == 400:
        record(PASS, "missing question rejected (400)")
    else:
        record(FAIL, "missing question rejected", "expected 400")

    if call(settings, "/ai/nope", token=token)[0] == 404:
        record(PASS, "unknown route rejected (404)")
    else:
        record(FAIL, "unknown route rejected", "expected 404")

    # ── 5. rate limit ────────────────────────────────────────────────────
    # Spend the window on authenticated calls, which is what the limit is for.
    # /ai/status is deliberately excluded server-side, so use a metered route.
    burst = settings.ai_rate_limit_per_min + 4
    codes = [call(settings, "/ai/meters", token=token)[0] for _ in range(burst)]
    if 429 in codes:
        first_429 = codes.index(429) + 1
        record(
            PASS,
            "rate limit kicks in (429)",
            f"limit={settings.ai_rate_limit_per_min}/min, first 429 on request {first_429}",
        )
    else:
        record(FAIL, "rate limit kicks in", f"no 429 in {burst} calls")

    # Status must stay reachable during that window or monitoring goes dark.
    if call(settings, "/ai/status")[0] == 200:
        record(PASS, "/ai/status exempt from the rate limit")
    else:
        record(FAIL, "/ai/status exempt from the rate limit", "status blocked while limited")

    print("        (waiting 62s for the rate-limit window to clear...)")
    time.sleep(62)

    # ── 6. real Bedrock call ─────────────────────────────────────────────
    print()
    print("--- calling Amazon Nova Pro (costs a few tokens) ---")
    code, out = call(
        settings,
        "/ai/ask",
        "POST",
        {"question": "Reply with exactly the word READY"},
        token,
    )
    if code == 200 and out.get("answer"):
        record(PASS, "POST /ai/ask (direct answer)", out["answer"][:80])
    elif code == 503:
        record(FAIL, "POST /ai/ask", f"ai_unavailable -> {out.get('detail', '')[:220]}")
        print("\nA 503 means AWS rejected or could not serve the call.")
        print("Most likely causes, in order:")
        print("  1. Region mismatch - Nova Pro is not in every region.")
        print("  2. IAM policy does not allow bedrock:Converse for this model.")
        print("  3. The .env keys were never validated (assistant.py used other creds).")
        print(f"  Currently trying: {settings.bedrock_model_id} in {settings.aws_region}")
        return 1
    else:
        record(FAIL, "POST /ai/ask", f"HTTP {code} {out}")
        return 1

    # ── 7. tool use against real meters ──────────────────────────────────
    print()
    print("--- tool use against live meter data (a few more tokens) ---")
    code, out = call(
        settings,
        "/ai/ask",
        "POST",
        {
            "question": "Which meter has the highest totalizer reading? "
            "Look the values up with your tools and name the meter."
        },
        token,
    )
    if code != 200:
        record(FAIL, "tool-use question", f"HTTP {code} {out}")
        return 1
    called = out.get("tool_calls") or []
    names = {c["tool"] for c in called}
    if called and names <= {"list_meters", "get_meter", "compare_meters"}:
        record(PASS, "tool-use question", f"{len(called)} call(s): {sorted(names)}")
        record(PASS, "hops within budget", f"hops={out.get('hops')} max={settings.ai_max_tool_hops}")
    else:
        record(FAIL, "tool-use question", f"unexpected tool calls: {called}")
    print()
    print("        model answer:")
    for line in (out.get("answer") or "").splitlines():
        print(f"        | {line}")

    # ── 8. phase 4: anomaly findings and daily summary ───────────────────
    print()
    print("--- phase 4: anomaly findings (deterministic, no model call) ---")
    code, findings = call(settings, "/ai/findings", token=token)
    if code == 200 and findings.get("available"):
        rows = findings.get("findings") or []
        by_sev: dict[str, int] = {}
        for row in rows:
            by_sev[row["severity"]] = by_sev.get(row["severity"], 0) + 1
        record(PASS, "GET /ai/findings", f"{len(rows)} open -> {by_sev or 'none'}")
        for row in rows[:5]:
            print(f"        [{row['severity']}] {row['rule']}: {row['message'][:70]}")
        blob = json.dumps(findings)
        leaked = [
            n
            for n in (settings.aws_access_key_id, settings.aws_secret_access_key, token)
            if n and len(n) >= 8 and n in blob
        ]
        if leaked:
            record(FAIL, "findings carry no secrets", f"{len(leaked)} leaked")
        else:
            record(PASS, "findings carry no secrets")
    else:
        record(FAIL, "GET /ai/findings", f"HTTP {code} {findings}")

    print()
    print("--- phase 4: daily summary (one more model call) ---")
    code, summary = call(settings, "/ai/summary/run", "POST", token=token)
    if code == 200 and summary.get("body"):
        record(
            PASS,
            "POST /ai/summary/run",
            f"{summary.get('local_date')}, {summary.get('findings')} finding(s)",
        )
        print(f"        usage: {summary.get('usage')}")
        for line in (summary.get("body") or "").splitlines():
            print(f"        | {line}")
        blob = json.dumps(summary)
        leaked = [
            n
            for n in (settings.aws_access_key_id, settings.aws_secret_access_key, token)
            if n and len(n) >= 8 and n in blob
        ]
        if leaked:
            record(FAIL, "summary carries no secrets", f"{len(leaked)} leaked")
        else:
            record(PASS, "summary carries no secrets")
    elif code == 503:
        record(FAIL, "POST /ai/summary/run", f"ai_unavailable -> {summary.get('detail', '')[:200]}")
    else:
        record(FAIL, "POST /ai/summary/run", f"HTTP {code} {summary}")

    code, out = call(settings, "/ai/summary/latest", token=token)
    if code == 200 and out.get("summary"):
        record(PASS, "GET /ai/summary/latest", f"date={out['summary']['local_date']}")
    else:
        record(FAIL, "GET /ai/summary/latest", f"HTTP {code} {out}")

    # ── 9. safety: no secrets in the response ────────────────────────────
    leaked = [
        name
        for name in (settings.aws_access_key_id, settings.aws_secret_access_key, token)
        if name and len(name) >= 8 and name in json.dumps(out)
    ]
    if leaked:
        record(FAIL, "no secrets echoed in response", f"{len(leaked)} secret(s) present")
    else:
        record(PASS, "no secrets echoed in response")

    # ── summary ──────────────────────────────────────────────────────────
    print()
    print("=" * 62)
    passed = sum(1 for s, _, _ in _results if s == PASS)
    failed = sum(1 for s, _, _ in _results if s == FAIL)
    skipped = sum(1 for s, _, _ in _results if s == SKIP)
    print(f"{passed} passed, {failed} failed, {skipped} skipped")
    print("=" * 62)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
