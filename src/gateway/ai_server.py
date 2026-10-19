"""AI assistant HTTP endpoint: minimal stdlib-only asyncio server.

Runs as a *second* listener alongside the health monitor so the Modbus/BACnet
path is never entangled with it. Routes:

    GET  /ai/status            integration state (secret-free)
    GET  /ai/meters            current meters + values, no model involved
    GET  /ai/meters/{id}       one meter's values
    POST /ai/ask               {"question": "..."} -> natural-language answer
    GET  /ai/summary/latest    reserved for phase 4
    GET  /ai/summary/run       reserved for phase 4
    GET  /ai/findings          reserved for phase 4

Every route except ``/ai/status`` requires ``Authorization: Bearer <token>``,
compared with :func:`hmac.compare_digest`. Requests are rate limited per client
address. Bedrock failures answer 503 and never propagate: the gateway's BMS
pipeline is independent of this server.
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import time
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, unquote, urlparse

from .ai.agent import Assistant
from .ai.bedrock import BedrockClient, BedrockError, CircuitOpen
from .ai.roles import Principal, PrincipalResolver, Role, StaticPrincipalResolver
from .ai.tools import MeterTools
from .obix_store import ObixStore, build_obix_store
from .obix_validation import SEVERITY_ORDER, ObixValidator, load_requirements

# An ``Authorization: Bearer`` value starting with this is a signed identity
# assertion from ems-backend rather than a static token. They are routed to
# different resolvers so a stolen service token cannot be replayed as a user,
# and so the audit log can say which kind authenticated.
_ASSERTION_PREFIX = "v1."


class _ChainedResolver:
    """Try each resolver in turn; first non-None wins.

    Keeps the static-token path and the assertion path independent: the static
    resolver walks its registry with a constant-time compare, the assertion
    resolver verifies an HMAC, and neither has to know about the other.
    """

    def __init__(self, resolvers: list) -> None:
        self.resolvers = [r for r in resolvers if r is not None]

    def resolve(self, token: str) -> Optional[Principal]:
        value = str(token or "").strip()
        if not value:
            return None
        for resolver in self.resolvers:
            principal = resolver.resolve(value)
            if principal is not None:
                return principal
        return None

    def describe(self) -> dict:
        described = []
        for resolver in self.resolvers:
            fn = getattr(resolver, "describe", None)
            described.append(fn() if callable(fn) else {"source": type(resolver).__name__})
        return {"sources": described, "count": len(self.resolvers)}

log = logging.getLogger("gateway.ai")

_MAX_HEAD = 64 * 1024
_MAX_BODY = 256 * 1024
_READ_TIMEOUT = 10

_REASONS = {
    200: "OK",
    400: "Bad Request",
    401: "Unauthorized",
    403: "Forbidden",
    404: "Not Found",
    405: "Method Not Allowed",
    409: "Conflict",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}

# Pagination guardrails for the /bms/* list endpoints. A model or Lambda that
# asks for everything still gets a bounded page.
_BMS_DEFAULT_LIMIT = 200
_BMS_MAX_LIMIT = 1000


def _query_str(query: str, key: str) -> Optional[str]:
    values = parse_qs(query or "").get(key)
    if not values:
        return None
    value = str(values[0]).strip()
    return value or None


def _query_int(
    query: str,
    key: str,
    default: Optional[int],
    minimum: int = 0,
    maximum: int = _BMS_MAX_LIMIT,
) -> Optional[int]:
    raw = _query_str(query, key)
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, value))


def _query_limit(query: str, default: int = _BMS_DEFAULT_LIMIT) -> Optional[int]:
    # limit=0 means "no cap" (used by in-process callers that know the size).
    raw = _query_str(query, "limit")
    if raw is None:
        return default
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    if value <= 0:
        return None
    return min(value, _BMS_MAX_LIMIT)


class _RateLimiter:
    """Fixed-window limiter keyed by client address."""

    def __init__(self, per_minute: int) -> None:
        self.limit = max(1, int(per_minute))
        self._hits: dict[str, list[float]] = {}

    def allow(self, key: str) -> bool:
        now = time.monotonic()
        window = [t for t in self._hits.get(key, ()) if now - t < 60.0]
        if len(window) >= self.limit:
            self._hits[key] = window
            return False
        window.append(now)
        self._hits[key] = window
        # Opportunistic cleanup so the dict cannot grow without bound.
        if len(self._hits) > 256:
            cutoff = now - 60.0
            self._hits = {k: v for k, v in self._hits.items() if any(t >= cutoff for t in v)}
        return True


class AIServer:
    """HTTP/1.1 JSON API in front of the Bedrock assistant."""

    def __init__(
        self,
        host: str,
        port: int,
        client: BedrockClient,
        tools: MeterTools,
        assistant: Assistant,
        bearer_token: str,
        rate_limit_per_min: int = 10,
        summary_writer=None,
        resolver: Optional[PrincipalResolver] = None,
        obix_store: Optional[ObixStore] = None,
        obix_validator: Optional[ObixValidator] = None,
    ) -> None:
        self.host = host
        self.port = port
        self.client = client
        self.tools = tools
        self.assistant = assistant
        self.bearer_token = bearer_token
        self.summary_writer = summary_writer
        self.obix_store = obix_store
        # Requirements-based validator for BMS points. Built from the authored
        # config/obix_requirements.yaml unless one is injected; None means no
        # requirements, so /bms/alarms simply shows the water-meter findings.
        self.obix_validator = obix_validator
        if self.obix_validator is None and obix_store is not None:
            try:
                self.obix_validator = ObixValidator(load_requirements())
            except Exception:  # noqa: BLE001 - a bad config must not stop the server
                self.obix_validator = None
        self.limiter = _RateLimiter(rate_limit_per_min)
        self._server: Optional[asyncio.AbstractServer] = None
        self._started_monotonic: Optional[float] = None
        self._summary_lock = asyncio.Lock()

        # Prefer an explicit principal registry. Falling back to the legacy
        # single token would silently hand every caller ADMIN rights, so the
        # fallback principal is scoped to nothing: an unresolvable token must
        # fail closed, never open.
        self.resolver: PrincipalResolver = resolver or StaticPrincipalResolver(
            entries=(),
            _fallback=(
                Principal(subject="legacy", role=Role.USER, meters=())
                if bearer_token
                else None
            ),
        )
        self._legacy_token = bearer_token or ""

    # ── lifecycle ───────────────────────────────────────────────────────
    async def start(self) -> None:
        self._server = await asyncio.start_server(self._handle, self.host, self.port)
        self._started_monotonic = time.monotonic()
        log.info("ai http listening on %s:%d", self.host, self.port)

    async def stop(self) -> None:
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    @property
    def listening(self) -> bool:
        return self._server is not None

    # ── auth ────────────────────────────────────────────────────────────
    def _principal_for(self, header: Optional[str]) -> Optional[Principal]:
        """Resolve the bearer token to a Principal, or None if unauthorized.

        The legacy ``AI_BEARER_TOKEN`` is checked first with a constant-time
        compare so an existing single-token deployment keeps working. When a
        principal registry is configured it is authoritative: a request bearing
        the legacy token resolves to a principal with no meter access rather
        than to an administrator.
        """
        if not header or not header.startswith("Bearer "):
            return None
        supplied = header[len("Bearer ") :].strip()
        if not supplied:
            return None
        # The resolver is the single decision point, and it always fails closed.
        # Legacy single-token deployments work because the fallback resolver
        # installed in __init__ accepts AI_BEARER_TOKEN.
        return self.resolver.resolve(supplied)

    # ── request handling ────────────────────────────────────────────────
    async def _handle(self, reader, writer) -> None:
        try:
            peer = writer.get_extra_info("peername")
            client_ip = str(peer[0]) if isinstance(peer, tuple) and peer else "unknown"

            request_line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
            if not request_line:
                return
            method, raw_path, _version = request_line.decode("ascii", "replace").split(" ", 2)
            headers = await self._read_headers(reader)

            path = urlparse(raw_path).path.rstrip("/") or "/"
            query = urlparse(raw_path).query

            # /ai/status is open so monitoring needs no secret, and it costs
            # nothing (no Bedrock call, no token spend). Charging it against the
            # ask budget meant a few health probes could starve real questions.
            is_free_endpoint = path == "/ai/status"

            # /ai/status is reached without a token, so no Principal exists yet.
            # It is the only route that reads this while unauthenticated.
            principal: Optional[Principal] = None

            if not is_free_endpoint:
                # Charged before auth so that unauthenticated flooding is capped
                # too. The tradeoff: a burst of bad tokens can consume the
                # window for a legitimate client on the same address.
                if not self.limiter.allow(client_ip):
                    await self._json(writer, 429, {"error": "rate limit exceeded"})
                    return

                principal = self._principal_for(headers.get("authorization"))
                if principal is None:
                    await self._json(writer, 401, {"error": "unauthorized"})
                    return

            body = b""
            if method in ("POST", "PUT", "PATCH"):
                length = int(headers.get("content-length") or 0)
                if length > _MAX_BODY:
                    await self._json(writer, 413, {"error": "body too large"})
                    return
                if length:
                    body = await asyncio.wait_for(reader.readexactly(length), _READ_TIMEOUT)

            await self._route(writer, method, path, body, principal, query)
        except (
            asyncio.IncompleteReadError,
            asyncio.LimitOverrunError,
            asyncio.TimeoutError,
            ValueError,
            ConnectionResetError,
        ):
            pass
        except Exception as exc:  # noqa: BLE001 - never kill the handler task
            log.warning("ai request failed: %s", exc)
        finally:
            writer.close()
            try:
                await writer.wait_closed()
            except Exception:  # noqa: BLE001
                pass

    async def _read_headers(self, reader) -> dict[str, str]:
        headers: dict[str, str] = {}
        total = 0
        while True:
            line = await asyncio.wait_for(reader.readline(), _READ_TIMEOUT)
            if not line:
                break
            total += len(line)
            if line in (b"\r\n", b"\n") or total > _MAX_HEAD:
                break
            try:
                name, _, value = line.decode("latin-1").partition(":")
            except ValueError:
                continue
            headers[name.strip().lower()] = value.strip()
        return headers

    async def _route(
        self,
        writer,
        method: str,
        path: str,
        body: bytes,
        principal: Optional[Principal],
        query: str = "",
    ) -> None:
        # Every data route below reads through this bound view, so a consumer
        # can only ever see meters in their own allow-list. Only /ai/status
        # arrives without a principal.
        tools = self.tools.for_principal(principal)

        if path == "/ai/status":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._json(writer, 200, self.status())
            return

        if path == "/ai/meters":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._json(writer, 200, json.loads(tools.dispatch("list_meters", {})))
            return

        if path.startswith("/ai/meters/"):
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            meter_id = unquote(path[len("/ai/meters/") :]).strip()
            if not meter_id:
                await self._json(writer, 400, {"error": "meter id required"})
                return
            result = json.loads(tools.dispatch("get_meter", {"meter_id": meter_id}))
            # The AI tool protocol reports denials as 200 + {"error": ...} so the
            # model can read them, but this is a direct REST endpoint: surface
            # out-of-scope access as HTTP 403 (and malformed ids as 400) instead
            # of an ok-looking body.
            if isinstance(result, dict) and result.get("error"):
                status = 403 if result["error"] == "access denied" else 400
                await self._json(writer, status, result)
                return
            await self._json(writer, 200, result)
            return

        if path == "/ai/ask":
            if method != "POST":
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._handle_ask(writer, body, principal)
            return

        if path == "/ai/findings":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._json(writer, 200, self._findings(principal))
            return

        if path == "/ai/summary/latest":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            latest = self.summary_writer.buffer.latest_summary() if self.summary_writer else None
            await self._json(writer, 200, {"summary": self._scope_summary(latest, principal)})
            return

        # ── BMS REST endpoints (read-only) ──────────────────────────────────
        if path == "/bms/points":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            result = self._bms_points(principal, query)
            await self._json(writer, 403 if result.get("error") else 200, result)
            return

        if path.startswith("/bms/points/"):
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            # Split the *encoded* path so a %2F inside an id is not mistaken
            # for a path separator, then decode each segment.
            segments = [s for s in path[len("/bms/points/") :].split("/") if s]
            if len(segments) == 1:
                result = self._bms_points_for(unquote(segments[0]).strip(), principal, query)
                status = 403 if result.get("error") else 200
            elif len(segments) == 2:
                result = self._bms_point(
                    unquote(segments[0]).strip(), unquote(segments[1]).strip(), principal
                )
                status = 200 if "error" not in result else (
                    403 if result["error"] == "access denied" else 404
                )
            else:
                await self._json(writer, 400, {"error": "invalid point path"})
                return
            await self._json(writer, status, result)
            return

        if path == "/bms/devices":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._json(writer, 200, self._bms_devices(principal))
            return

        if path == "/bms/status":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            result = self._bms_status(principal)
            await self._json(writer, 403 if result.get("error") == "forbidden" else 200, result)
            return

        if path == "/bms/alarms":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._json(writer, 200, self._bms_alarms(principal))
            return

        if path == "/bms/history":
            if method not in ("GET", "HEAD"):
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            result = self._bms_history(principal, query)
            if "error" in result:
                status = 403 if result["error"] == "access denied" else 400
            else:
                status = 200
            await self._json(writer, status, result)
            return

        if path == "/ai/summary/run":
            if method != "POST":
                await self._json(writer, 405, {"error": "method not allowed"})
                return
            await self._handle_summary_run(writer, principal)
            return

        await self._json(writer, 404, {"error": "not found"})

    def _findings(self, principal: Principal) -> dict[str, Any]:
        """Findings the caller is allowed to see.

        A consumer gets only their own meters' findings. Anyone may generate a
        summary on demand, but the fact set is built site-wide and would
        otherwise leak other meters through the narrative.
        """
        if self.summary_writer is None:
            return {"findings": [], "count": 0, "available": False}
        rows = self.summary_writer.buffer.open_findings()
        visible = principal.filter_findings(rows)
        payload: dict[str, Any] = {"findings": visible, "count": len(visible), "available": True}
        if not principal.sees_all_meters:
            payload["scope"] = "your meters only"
        return payload

    def _scope_summary(self, summary: Optional[dict], principal: Principal) -> Optional[dict]:
        """Hide a site-wide summary from non-privileged callers.

        The stored summary is one narrative about the whole estate; there is no
        per-user version of it, and regenerating one on every read would cost a
        Bedrock call. So a consumer gets an explicit refusal rather than a
        redacted document that could still leak meter names.
        """
        if summary is None or principal.sees_all_meters:
            return summary
        return {
            "restricted": True,
            "reason": "the daily summary covers the whole site",
            "date": summary.get("summary_date"),
            "findings_visible_to_you": self._findings(principal)["count"],
            "hint": "ask the assistant about your own meters, or call /ai/findings",
        }

    async def _handle_summary_run(self, writer, principal: Principal) -> None:
        """Generate a summary on demand. Locked: one AWS call at a time."""
        if self.summary_writer is None:
            await self._json(
                writer, 503, {"error": "unavailable", "detail": "summary writer not configured"}
            )
            return
        # Generating spends an AWS call and writes a site-wide narrative, so it
        # stays an operator action. Consumers read /ai/findings and /ai/ask.
        if not principal.sees_all_meters:
            await self._json(
                writer,
                403,
                {
                    "error": "forbidden",
                    "detail": "generating the site summary requires an admin or technician account",
                    "role": principal.role.value,
                },
            )
            return
        if self._summary_lock.locked():
            await self._json(writer, 409, {"error": "a summary is already being generated"})
            return
        async with self._summary_lock:
            try:
                result = await self.summary_writer.write(datetime.now(timezone.utc))
            except CircuitOpen as exc:
                await self._json(writer, 503, {"error": "ai_unavailable", "detail": str(exc)})
                return
            except BedrockError as exc:
                await self._json(writer, 503, {"error": "ai_unavailable", "detail": str(exc)})
                return
            except Exception as exc:  # noqa: BLE001
                log.warning("summary run failed: %s", type(exc).__name__)
                await self._json(writer, 503, {"error": "ai_unavailable"})
                return
        result.pop("facts", None)
        await self._json(writer, 200, result)

    async def _handle_ask(self, writer, body: bytes, principal: Principal) -> None:
        try:
            payload = json.loads(body.decode("utf-8") or "{}")
        except (UnicodeDecodeError, ValueError):
            await self._json(writer, 400, {"error": "body must be valid JSON"})
            return
        if not isinstance(payload, dict):
            await self._json(writer, 400, {"error": "body must be a JSON object"})
            return
        question = payload.get("question")
        if not isinstance(question, str) or not question.strip():
            await self._json(writer, 400, {"error": "'question' is required"})
            return
        if len(question) > 2000:
            await self._json(writer, 400, {"error": "'question' is too long"})
            return

        history = Assistant.parse_history(payload.get("history"))
        try:
            result = await self.assistant.ask(
                question, history, tools=self.tools.for_principal(principal), principal=principal
            )
        except CircuitOpen as exc:
            await self._json(writer, 503, {"error": "ai_unavailable", "detail": str(exc)})
            return
        except BedrockError as exc:
            await self._json(writer, 503, {"error": "ai_unavailable", "detail": str(exc)})
            return
        except Exception as exc:  # noqa: BLE001 - never leak a traceback
            log.warning("ai ask failed: %s", type(exc).__name__)
            await self._json(writer, 503, {"error": "ai_unavailable"})
            return
        await self._json(writer, 200, result)

    # ── status ──────────────────────────────────────────────────────────
    def status(self) -> dict[str, Any]:
        state = self.client.status()
        state["listening"] = self.listening
        state["bind"] = f"{self.host}:{self.port}"
        state["uptime_s"] = (
            round(time.monotonic() - self._started_monotonic, 2)
            if self._started_monotonic is not None
            else 0.0
        )
        state["tools"] = [spec["name"] for spec in self.tools.specs]
        state["rate_limit_per_min"] = self.limiter.limit
        state["max_tool_hops"] = self.assistant.max_hops
        describe = getattr(self.resolver, "describe", None)
        if callable(describe):
            state["auth"] = describe()
        else:
            state["auth"] = {"source": type(self.resolver).__name__}
        return state

    # ── BMS API helpers ─────────────────────────────────────────────────
    def _visible_meters(self, principal: Principal) -> Optional[list[str]]:
        """``None`` for a privileged caller (no restriction), else the scope."""
        if principal.sees_all_meters:
            return None
        return [str(m) for m in (getattr(principal, "meters", ()) or ())]

    def _bms_points(self, principal: Principal, query: str = "") -> dict[str, Any]:
        meter_id = _query_str(query, "meter_id")
        if meter_id and not principal.can_access(meter_id):
            return {"error": "access denied", "meter_id": meter_id}
        limit = _query_limit(query)
        offset = _query_int(query, "offset", 0, 0, 1_000_000) or 0
        scope = self._visible_meters(principal)

        if self.obix_store is not None:
            rows = [dict(r) for r in self.obix_store.latest(
                meter_id=meter_id, meter_ids=scope, limit=limit, offset=offset
            )]
            total = self.obix_store.latest_count(meter_id=meter_id, meter_ids=scope)
        else:
            rows = []
            meter_ids = [meter_id] if meter_id else list(self.tools.meters.meters)
            for mid in meter_ids:
                if not principal.can_access(mid):
                    continue
                for point_id, value in (self.tools.buffer.get_normalized(mid) or {}).items():
                    rows.append(
                        {
                            "meter_id": mid,
                            "point_id": point_id,
                            "value": value.get("value"),
                            "text_value": None if value.get("value") is None else str(value["value"]),
                            "unit": value.get("unit") or "",
                            "status": "ok",
                            "captured_utc": value.get("updated_utc"),
                        }
                    )
            total = len(rows)
            rows = rows[offset:] if limit is None else rows[offset : offset + limit]

        return {
            "meters": sorted({r["meter_id"] for r in rows}),
            "points": rows,
            "count": len(rows),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _bms_points_for(
        self, meter_id: str, principal: Principal, query: str = ""
    ) -> dict[str, Any]:
        if not principal.can_access(meter_id):
            return {"error": "access denied", "meter_id": meter_id}
        limit = _query_limit(query)
        offset = _query_int(query, "offset", 0, 0, 1_000_000) or 0
        if self.obix_store is not None:
            points = [dict(r) for r in self.obix_store.points(meter_id, limit=limit, offset=offset)]
            total = self.obix_store.latest_count(meter_id=meter_id)
        else:
            all_points = [
                {
                    "meter_id": meter_id,
                    "point_id": point_id,
                    "name": point_id,
                    "value": value.get("value"),
                    "text_value": None if value.get("value") is None else str(value["value"]),
                    "unit": value.get("unit") or "",
                    "status": "ok",
                    "captured_utc": value.get("updated_utc"),
                }
                for point_id, value in (self.tools.buffer.get_normalized(meter_id) or {}).items()
            ]
            total = len(all_points)
            points = all_points[offset:] if limit is None else all_points[offset : offset + limit]
        return {
            "meter_id": meter_id,
            "points": points,
            "count": len(points),
            "total": total,
            "limit": limit,
            "offset": offset,
        }

    def _bms_point(
        self, meter_id: str, point_id: str, principal: Principal
    ) -> dict[str, Any]:
        if not principal.can_access(meter_id):
            return {"error": "access denied", "meter_id": meter_id}
        if self.obix_store is None:
            return {"error": "not found", "meter_id": meter_id, "point_id": point_id}
        row = self.obix_store.point(meter_id, point_id)
        if row is None:
            return {"error": "not found", "meter_id": meter_id, "point_id": point_id}
        return dict(row)

    def _bms_devices(self, principal: Principal) -> dict[str, Any]:
        devs: list[dict[str, Any]] = []
        if self.obix_store is not None:
            try:
                devs = [dict(r) for r in self.obix_store.devices()]
            except Exception:  # noqa: BLE001 - never leak a DB error
                devs = []
        if not devs:
            for meter_id in self.tools.meters.meters:
                devs.append(
                    {"meter_id": meter_id, "device_path": "", "name": meter_id, "point_count": 0}
                )
        visible = [d for d in devs if principal.can_access(d.get("meter_id") or "")]
        return {"devices": visible, "count": len(visible)}

    def _bms_status(self, principal: Principal) -> dict[str, Any]:
        """Data freshness: last read pass and row counts (privileged only)."""
        if not principal.sees_all_meters:
            return {"error": "forbidden", "detail": "site status requires an operator account"}
        if self.obix_store is None:
            return {"enabled": False, "detail": "oBIX database not configured"}
        try:
            fresh = self.obix_store.freshness()
        except Exception:  # noqa: BLE001 - never leak a DB error
            return {"enabled": True, "error": "status unavailable"}
        fresh["enabled"] = True
        return fresh

    def _bms_alarms(self, principal: Principal) -> dict[str, Any]:
        """Water-meter anomaly findings (buffer) plus BMS requirement findings.

        Two sources feed one list: the deterministic water-meter anomalies the
        gateway already records, and the oBIX points that violate their
        configured requirements (``config/obix_requirements.yaml``). Both are
        facts about the data, so they belong together behind one endpoint.
        """
        alarms: list[dict[str, Any]] = []
        if self.summary_writer is not None:
            alarms.extend(
                principal.filter_findings(self.summary_writer.buffer.open_findings())
            )
        alarms.extend(self._obix_alarm_findings(principal))
        alarms.sort(key=lambda a: -SEVERITY_ORDER.get(str(a.get("severity")), -1))
        return {"alarms": alarms, "count": len(alarms)}

    def _obix_alarm_findings(self, principal: Principal) -> list[dict[str, Any]]:
        """Requirement violations across the caller's BMS points, worst first."""
        if self.obix_store is None or self.obix_validator is None:
            return []
        meter_ids = self._visible_meters(principal)
        if meter_ids is not None and not meter_ids:
            return []
        try:
            rows = [
                dict(r)
                for r in self.obix_store.latest(meter_ids=meter_ids, limit=None)
            ]
            findings = self.obix_validator.scan(rows)
        except Exception:  # noqa: BLE001 - a DB/config error must not break alarms
            return []
        out: list[dict[str, Any]] = []
        for finding in findings:
            if not principal.can_access(finding.meter_id):
                continue
            item = finding.as_dict()
            item["source"] = "obix_validation"
            out.append(item)
        return out

    def _bms_history(self, principal: Principal, query: str) -> dict[str, Any]:
        meter_id = _query_str(query, "meter_id") or ""
        point_id = _query_str(query, "point_id") or ""
        since = _query_str(query, "since")
        until = _query_str(query, "until")
        if not meter_id or not point_id:
            return {"error": "meter_id and point_id are required"}
        if not principal.can_access(meter_id):
            return {"error": "access denied", "meter_id": meter_id}
        limit = _query_limit(query)
        offset = _query_int(query, "offset", 0, 0, 1_000_000) or 0
        hist: list[dict[str, Any]] = []
        if self.obix_store is not None:
            try:
                hist = [
                    dict(r)
                    for r in self.obix_store.history(
                        meter_id,
                        point_id,
                        since_utc=since,
                        until_utc=until,
                        limit=limit,
                        offset=offset,
                    )
                ]
            except Exception:  # noqa: BLE001 - never leak a DB error
                hist = []
        return {
            "meter_id": meter_id,
            "point_id": point_id,
            "since": since,
            "until": until,
            "history": hist,
            "count": len(hist),
            "offset": offset,
        }

    async def _json(self, writer, code: int, payload: dict) -> None:
        body = json.dumps(payload, indent=2, default=str).encode("utf-8")
        reason = _REASONS.get(code, "Error")
        head = (
            f"HTTP/1.1 {code} {reason}\r\n"
            "Content-Type: application/json\r\n"
            f"Content-Length: {len(body)}\r\n"
            "Connection: close\r\n"
            "\r\n"
        )
        writer.write(head.encode("ascii"))
        writer.write(body)
        await writer.drain()
