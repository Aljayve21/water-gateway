"""Check gateway health without PowerShell's HTML error page.

Invoke-WebRequest in a non-interactive shell raises a prompt (it wants to
show an IE-style error document). urllib has no such behaviour.

Run this while the gateway is up:

    cd C:\\xampp\\htdocs\\EnyeMeteringSystem\\water-gateway\\src
    ..\\.venv\\Scripts\\python.exe -m gateway.main

then, in a second terminal:

    .\\.venv\\Scripts\\python.exe scripts\\check_health.py
"""

from __future__ import annotations

import json
import sys
import urllib.error
import urllib.request

# WinError 10061 on Windows.
_REFUSED = (10061, ConnectionRefusedError)


def _diagnose(port: int) -> None:
    """Turn a refused connection into something the user can act on."""
    print()
    print("=" * 66)
    print(f"The gateway is not listening on port {port}. It is not running.")
    print("=" * 66)
    print()
    print("Start it, and leave that terminal open:")
    print()
    print("    cd C:\\xampp\\htdocs\\EnyeMeteringSystem\\water-gateway\\src")
    print("    ..\\.venv\\Scripts\\python.exe -m gateway.main")
    print()
    print("Then run this script again from a second terminal.")
    print()
    print("If that terminal exits immediately with a traceback, the traceback")
    print("is the real error - paste it here.")


def main() -> int:
    url = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:9090/health"
    host, _, tail = url.split("//", 1)[1].partition("/")
    port = int(host.split(":")[1]) if ":" in host else 80

    try:
        with urllib.request.urlopen(url, timeout=8) as resp:
            body = json.loads(resp.read().decode())
    except urllib.error.HTTPError as exc:
        print(f"HTTP {exc.code}")
        print(exc.read().decode() or "(empty body)")
        return 1
    except urllib.error.URLError as exc:
        # urllib wraps the real socket error in .reason, so unwrap to test it.
        reason = exc.reason if isinstance(exc.reason, OSError) else exc
        if getattr(reason, "winerror", None) in _REFUSED or isinstance(reason, ConnectionRefusedError):
            _diagnose(port)
        else:
            print(f"UNREACHABLE: {type(exc).__name__}: {exc}")
        return 1
    except OSError as exc:
        print(f"UNREACHABLE: {type(exc).__name__}: {exc}")
        return 1

    print(f"HTTP {resp.status}  {url}")
    print()
    status = body.get("status", "?")
    ai = body.get("ai") or {}
    print(f"  status       {status}")
    print(f"  mode         {body.get('mode')}")
    print(f"  meters live  {body.get('meters_live')}")
    print(f"  modbus       {(body.get('protocols') or {}).get('modbus', {}).get('bind')}")
    print(f"  bacnet       {(body.get('protocols') or {}).get('bacnet', {}).get('bind')}")
    print(f"  ai enabled   {ai.get('enabled')}")
    print(f"  ai ready     {ai.get('ready')}  {ai.get('bind')}")
    print(f"  ai tools     {', '.join(ai.get('tools') or []) or 'none'}")
    print(f"  ai breaker   {'OPEN' if (ai.get('breaker') or {}).get('open') else 'closed'}")
    if body.get("last_error"):
        print(f"  last error   {body['last_error']}")
    print()

    if status != "ok":
        print(f"Gateway is serving but reports status={status}. See last_error above.")
        return 1
    print("Gateway is healthy.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
