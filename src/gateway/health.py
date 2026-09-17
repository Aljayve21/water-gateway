"""Health monitor: minimal stdlib-only asyncio HTTP endpoint for the gateway.

Exposes a JSON snapshot of gateway state on ``GET /health`` (and ``GET /``):
mode, uptime, buffer counts, protocol server listening state, last cycle /
normalize / sync results and the most recent error. Uses ``asyncio.start_server``
so it needs no third-party web framework and sits naturally next to the Modbus
and BACnet servers in the same event loop.

The endpoint never returns credentials; the ``config`` section is the
non-sensitive :meth:`Settings.fingerprint`.
"""

from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Callable, Optional

log = logging.getLogger("gateway.health")

# Rough upper bound for the request head; anything larger is rejected.
_MAX_HEAD = 64 * 1024

_UTCNOW_TS = lambda: datetime.now(timezone.utc).isoformat(  # noqa: E731
    timespec="seconds"
)


class MonitorState:
    """Plain shared status record updated by the gateway loop as it runs.

    All fields are written from the same event-loop thread that the health
    handler reads on, so no locking is needed.
    """

    def __init__(self) -> None:
        self.started_utc: str = _UTCNOW_TS()
        self.mode: str = "idle"
        self.last_cycle: Optional[dict[str, Any]] = None
        self.last_normalize: Optional[dict[str, Any]] = None
        self.last_sync_utc: Optional[str] = None
        self.meters_live: int = 0
        self.last_error: Optional[str] = None
        self.last_error_utc: Optional[str] = None

    def record_cycle(self, result: Any) -> None:
        self.last_cycle = {
            "new_packets": int(result.new_packets),
            "snapshots": int(result.snapshots),
            "watermark": int(result.watermark_value),
            "elapsed_s": float(result.elapsed_s),
            "utc": _UTCNOW_TS(),
        }

    def record_normalize(self, result: Any) -> None:
        self.last_normalize = {
            "meters": int(result.meters),
            "point_values": int(result.point_values),
            "events_consumed": int(result.events_consumed),
            "events_failed": int(result.events_failed),
            "elapsed_s": float(result.elapsed_s),
            "utc": _UTCNOW_TS(),
        }

    def record_sync(self, meters_live: int) -> None:
        self.meters_live = int(meters_live)
        self.last_sync_utc = _UTCNOW_TS()

    def record_error(self, message: str) -> None:
        self.last_error = str(message)
        self.last_error_utc = _UTCNOW_TS()


class HealthMonitor:
    """HTTP/1.1 server answering an instance of :class:`MonitorState` as JSON."""

    def __init__(
        self,
        host: str,
        port: int,
        state: MonitorState,
        fingerprint: dict[str, Any],
        buffer_probe: Optional[Callable[[], dict[str, Any]]] = None,
        protocol_probe: Optional[Callable[[], dict[str, Any]]] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.state = state
        self.fingerprint = fingerprint
        self._buffer_probe = buffer_probe or (lambda: {})
        self._protocol_probe = protocol_probe or (lambda: {})
        self._server: asyncio.AbstractServer | None = None
        self._started_monotonic: Optional[float] = None

    # ── lifecycle ────────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(
            self._handle_connection, self.host, self.port
        )
        self._started_monotonic = time.monotonic()
        log.info("health http listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    # ── request handling ─────────────────────────────────────────────────
    async def _handle_connection(self, reader, writer) -> None:
        try:
            request_line = await asyncio.wait_for(reader.readline(), 5)
            if not request_line:
                return
            method, path, _version = request_line.decode(
                "ascii", "replace"
            ).split(" ", 2)
            await self._drain_headers(reader)
            if method not in ("GET", "HEAD"):
                await self._respond(writer, 405, "text/plain", b"method not allowed\n")
                return
            if path not in ("/", "/health"):
                await self._respond(writer, 404, "text/plain", b"not found\n")
                return
            body = json.dumps(self.snapshot(), indent=2, default=str).encode("utf-8")
            await self._respond(writer, 200, "application/json", body)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            ValueError,
            ConnectionResetError,
        ):
            pass
        except Exception as exc:  # noqa: BLE001 - never kill the handler task
            log.warning("health request failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _drain_headers(self, reader) -> None:
        total = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), 5)
            if not line:
                break
            total += len(line)
            if line in (b"\r\n", b"\n") or total > _MAX_HEAD:
                break

    async def _respond(self, writer, code: int, ctype: str, body: bytes) -> None:
        reason = {200: "OK", 404: "Not Found", 405: "Method Not Allowed"}.get(
            code, "Error"
        )
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            f"Content-Type: {ctype}\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode("ascii"))
        writer.write(body)
        await writer.drain()

    # ── snapshot ─────────────────────────────────────────────────────────
    def snapshot(self) -> dict[str, Any]:
        status = "starting"
        if self.state.mode == "live" and self.state.last_cycle is not None:
            status = "ok"
        if self.state.last_error is not None:
            status = "degraded"
        buffer_counts: dict[str, Any]
        try:
            buffer_counts = self._buffer_probe() or {}
        except Exception as exc:  # noqa: BLE001 - report, never raise
            buffer_counts = {"error": str(exc)}
            status = "degraded"
        protocols: dict[str, Any]
        try:
            protocols = self._protocol_probe() or {}
        except Exception as exc:  # noqa: BLE001 - report, never raise
            protocols = {"error": str(exc)}
            status = "degraded"
        return {
            "service": "water-gateway",
            "status": status,
            "mode": self.state.mode,
            "started_utc": self.state.started_utc,
            "uptime_s": round(time.monotonic() - self._started_monotonic, 2)
            if self._started_monotonic is not None
            else 0.0,
            "buffer": buffer_counts,
            "protocols": protocols,
            "last_cycle": self.state.last_cycle,
            "last_normalize": self.state.last_normalize,
            "last_sync_utc": self.state.last_sync_utc,
            "meters_live": self.state.meters_live,
            "last_error": self.state.last_error,
            "last_error_utc": self.state.last_error_utc,
            "config": self.fingerprint,
        }