"""Mock oBIX server - a stand-in for NeoView's Niagara 4 oBIX interface.

Serves the gateway's current readings as spec-shaped oBIX XML so the client in
gateway/obix.py can be exercised end-to-end without a real BMS, credentials, or
network. It is a test fixture, not a production component.

    GET /obix/Ai/<meter>/<point>/value   single point
    GET /obix/Ai/<meter>/?refs           folder listing
    PUT  /obix/Ai/<meter>/<point>/value   write (echoes back)

Run:
    .\\.venv\\Scripts\\python.exe scripts\\obix_mock.py --port 8080
"""

from __future__ import annotations

import argparse
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from gateway.config import load_all  # noqa: E402
from gateway.map import MappingEngine  # noqa: E402

# Values are deliberately non-uniform so a swapped wiring or a parsing bug
# shows up immediately instead of looking plausible.
SAMPLE = {
    "totalizer": 1234.5,
    "pressure": 2.75,
    "reverse": 0.0,
    "pulse1": 12.0,
    "pulse2": 3.5,
    "battery": 3.92,
    "signal": -71.0,
    "online": True,
    "alarm": False,
}

STATE: dict[str, dict[str, Any]] = {}
from typing import Any  # noqa: E402  (kept local: only used for STATE typing)


class Handler(BaseHTTPRequestHandler):
    server_version = "obix-mock/1.0"

    def log_message(self, fmt, *args):  # noqa: D102 - quieter output
        print(f"  [mock] {fmt % args}")

    def _send(self, code: int, body: str) -> None:
        payload = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/xml; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        self.end_headers()
        self.wfile.write(payload)

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        listing = "refs" in self.path
        parts = [p for p in path.strip("/").split("/") if p]
        # expect /obix/Ai/<meter>/<point>[/value]
        if len(parts) < 3 or parts[0] != "obix" or parts[1] != "Ai":
            self._send(404, '<err is="obix:Error" xmlns="http://obix.org/" '
                            'code="404" reason="unknown path"/>')
            return
        meter = parts[2]
        point = parts[3] if len(parts) > 3 else None
        meter_state = STATE.get(meter)
        if meter_state is None:
            self._send(404, '<err is="obix:Error" xmlns="http://obix.org/" '
                            'code="404" reason="no such folder"/>')
            return

        if listing or point is None:
            refs = "".join(
                f'<ref is="obix:{ "Bool" if isinstance(v, bool) else "Real"}" '
                f'name="{name}" val="{str(v).lower() if isinstance(v, bool) else v}"/>'
                for name, v in sorted(meter_state.items())
            )
            self._send(200, f'<obj is="obix:Folder" xmlns="http://obix.org/" '
                            f'name="Ai/{meter}">{refs}</obj>')
            return

        if point not in meter_state:
            self._send(404, '<err is="obix:Error" xmlns="http://obix.org/" '
                            'code="404" reason="no such point"/>')
            return

        value = meter_state[point]
        tag = "bool" if isinstance(value, bool) else "real"
        val = str(value).lower() if isinstance(value, bool) else f"{value:.6g}"
        unit = ' unit="obix:units/cubic-meters"' if point == "totalizer" else ""
        self._send(
            200,
            f'<val xmlns="http://obix.org/" is="obix:Point" '
            f'name="Ai/{meter}/{point}"><{tag} val="{val}"{unit}/></val>',
        )

    def do_PUT(self) -> None:  # noqa: N802
        parts = [p for p in self.path.split("?")[0].strip("/").split("/") if p]
        if len(parts) < 4:
            self._send(400, '<err is="obix:Error" xmlns="http://obix.org/" '
                            'code="400" reason="bad path"/>')
            return
        meter, point = parts[2], parts[3]
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode("utf-8", errors="replace")
        import re

        match = re.search(r'val="([^"]*)"', body)
        if not match:
            self._send(400, '<err is="obix:Error" xmlns="http://obix.org/" '
                            'code="400" reason="no val attribute"/>')
            return
        raw = match.group(1)
        value: Any = raw.lower() == "true" if raw.lower() in ("true", "false") else float(raw)
        STATE.setdefault(meter, {})[point] = value
        self._send(200, f'<val xmlns="http://obix.org/" is="obix:Point" '
                        f'name="Ai/{meter}/{point}" />')


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--host", default="127.0.0.1")
    args = parser.parse_args()

    _s, _c, base_points, meters = load_all()
    engine = MappingEngine(meters, base_points)
    for meter_id, meter in engine.map.meters.items():
        STATE[meter_id] = {
            pid: SAMPLE.get(pid, 0.0) for pid in meter.points
        }

    server = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"oBIX mock serving {len(STATE)} meter folder(s) on "
          f"http://{args.host}:{args.port}/obix")
    print(f"  e.g. http://{args.host}:{args.port}/obix/Ai/"
          f"{list(STATE)[0]}/totalizer/value")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nstopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
