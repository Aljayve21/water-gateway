"""Read-only tools exposed to the Bedrock model.

These are the *only* way the assistant can see meter data. Every tool is a
fixed, hand-written read against the local SQLite buffer — the model never
generates SQL, never receives a connection string, and never writes anything.
Each returns a small JSON-serialisable dict that is fed straight back to the
model as a ``toolResult``.

Threading note: :class:`gateway.store.MySQLBuffer` is thread-affine (see its
module docstring), so these functions run on the gateway event loop and read
synchronously. Only the Bedrock call itself is pushed to a worker thread.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import Any, Callable, Optional

from ..config import BasePointsConfig, MetersConfig
from ..normalizer import derive_comm_status, last_contact_utc
from ..obix_validation import SEVERITY_ORDER, ObixValidator, load_requirements
from ..store import MySQLBuffer
from .roles import Principal

# Every tool result is clamped before it reaches the model so a single call
# cannot blow the context window.
_MAX_RESULT_CHARS = 6000
_MAX_COMPARE_METERS = 12
_MAX_BMS_POINTS = 300
_DEFAULT_BMS_POINTS = 100
_MAX_BMS_HISTORY = 720
_DEFAULT_BMS_HISTORY = 168
_MAX_BMS_FINDINGS = 200
_DEFAULT_BMS_FINDINGS = 50


def _clip(payload: Any) -> str:
    text = json.dumps(payload, ensure_ascii=False, default=str)
    if len(text) <= _MAX_RESULT_CHARS:
        return text
    # Truncate to a valid JSON prefix: dropping the tail of a serialised object
    # and pasting " [truncated]" after it produced output the model could not
    # parse, which it then "fixed" by inventing plausible values.
    head = text[:_MAX_RESULT_CHARS]
    cut = max(head.rfind("},"), head.rfind("],"), head.rfind('},"'))
    if cut > 0:
        return head[: cut + 1] + ',"_truncated":true}'
    return '{"_truncated":true,"_note":"result exceeded the size limit"}'


class MeterTools:
    """Registry of allow-listed read-only tools plus their dispatch."""

    def __init__(
        self,
        buffer: MySQLBuffer,
        meters: MetersConfig,
        base_points: BasePointsConfig,
        online_hours: float = 24.0,
        watch_hours: float = 48.0,
        obix_store: Optional[Any] = None,
        obix_validator: Optional[Any] = None,
    ) -> None:
        self.buffer = buffer
        self.meters = meters
        self.base_points = base_points
        self.online_hours = online_hours
        self.watch_hours = watch_hours
        # Optional NeoView/oBIX store. When present, two extra read-only tools
        # expose the catalog and hourly history from the dedicated oBIX database
        # (the buffer only carries the latest value of each point).
        self.obix_store = obix_store
        # Requirements-based validator for BMS points, built from the authored
        # config/obix_requirements.yaml unless one is injected. None disables the
        # findings tool - there is nothing to validate against.
        self.obix_validator = obix_validator
        if self.obix_validator is None and obix_store is not None:
            try:
                self.obix_validator = ObixValidator(load_requirements())
            except Exception:  # noqa: BLE001 - a bad config must not break tools
                self.obix_validator = None
        # Overridden per request via :meth:`for_principal`. Defaults to a
        # privileged principal so the agent, tests and any non-HTTP caller keep
        # working exactly as before; the HTTP layer always sets it explicitly.
        self._principal = Principal.admin(subject="internal")

        # Converse wraps the JSON Schema under inputSchema.json (a tagged
        # union — "type"/"properties" at that level is rejected outright).
        # Inside that "json" object Nova allows only type/properties/required:
        # no $schema, description, title or additionalProperties.
        self.specs: list[dict] = [
            {
                "name": "list_meters",
                "description": (
                    "List every water meter the gateway knows about, with its "
                    "communication status and how many points have current values. "
                    "Use this first when the user does not name a specific meter."
                ),
                "inputSchema": {"json": {"type": "object", "properties": {}}},
            },
            {
                "name": "get_meter",
                "description": (
                    "Get the current value of every point on one meter, including "
                    "units and the timestamp each reading was last updated. "
                    "Always call this before quoting a meter value."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "meter_id": {
                                "type": "string",
                                "description": "Meter identifier, for example WTR-001.",
                            }
                        },
                        "required": ["meter_id"],
                    }
                },
            },
            {
                "name": "compare_meters",
                "description": (
                    "Compare the current point values of two or more meters side "
                    "by side. Use this for questions about differences between meters."
                ),
                "inputSchema": {
                    "json": {
                        "type": "object",
                        "properties": {
                            "meter_ids": {
                                "type": "array",
                                "items": {"type": "string"},
                                "description": "Two or more meter identifiers.",
                            }
                        },
                        "required": ["meter_ids"],
                    }
                },
            },
        ]
        if self.obix_store is not None:
            # Only advertised when the oBIX database is actually wired: a tool
            # the model can call but that always fails would just waste a hop.
            self.specs.extend(
                [
                    {
                        "name": "list_bms_points",
                        "description": (
                            "List the NeoView/BMS points on one device (for example "
                            "bms_enyecontrols) with the latest value and unit of each. "
                            "Use this to discover point ids for get_bms_history."
                        ),
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {
                                    "meter_id": {
                                        "type": "string",
                                        "description": "BMS device/pseudo-meter id.",
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "description": "Maximum points to return (default 100).",
                                    },
                                },
                                "required": ["meter_id"],
                            }
                        },
                    },
                    {
                        "name": "get_bms_history",
                        "description": (
                            "Get the hourly history (average, minimum, maximum and "
                            "last value per hour) of one BMS point. Use this for "
                            "trends and 'over the last N hours' questions."
                        ),
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {
                                    "meter_id": {"type": "string"},
                                    "point_id": {"type": "string"},
                                    "since": {
                                        "type": "string",
                                        "description": "Earliest UTC hour, e.g. 2026-10-15T00:00:00+00:00.",
                                    },
                                    "until": {
                                        "type": "string",
                                        "description": "Latest UTC hour (optional).",
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "description": "Maximum hourly buckets (default 168).",
                                    },
                                },
                                "required": ["meter_id", "point_id"],
                            }
                        },
                    },
                    {
                        "name": "list_bms_findings",
                        "description": (
                            "List BMS/Neoview points whose latest value violates its "
                            "configured requirements (out of range, wrong unit, stale, "
                            "or invalid state). Use this to answer 'which BMS points "
                            "look wrong / out of spec?'. Read-only."
                        ),
                        "inputSchema": {
                            "json": {
                                "type": "object",
                                "properties": {
                                    "meter_id": {
                                        "type": "string",
                                        "description": "Optional: limit to one BMS device id.",
                                    },
                                    "min_severity": {
                                        "type": "string",
                                        "description": "info, warning (default) or critical.",
                                    },
                                    "limit": {
                                        "type": "integer",
                                        "description": "Maximum findings to return (default 50).",
                                    },
                                },
                            }
                        },
                    },
                ]
            )
        # Name -> unbound method name. Handlers are looked up per call so that a
        # per-principal view resolves against *itself*: storing bound methods
        # here would make every view call the original instance's methods and
        # silently ignore the view's scope.
        self._handler_names = {
            "list_meters": "_list_meters",
            "get_meter": "_get_meter",
            "compare_meters": "_compare_meters",
            "list_bms_points": "_list_bms_points",
            "get_bms_history": "_get_bms_history",
            "list_bms_findings": "_list_bms_findings",
        }

    # ── principal scoping ───────────────────────────────────────────────
    @property
    def principal(self) -> Principal:
        """The caller whose access this instance enforces."""
        return self._principal

    def for_principal(self, principal: Optional[Principal]) -> "MeterTools":
        """Return a view of these tools bound to ``principal``.

        A shallow copy is returned rather than mutating ``self``: the AI server
        handles requests concurrently on one event loop, so rebinding shared
        state per request would let two callers observe each other's scope.
        Binding the principal once at tool construction and never changing it
        would make every request privileged instead, which is worse.
        """
        clone = object.__new__(type(self))
        clone.__dict__.update(self.__dict__)
        clone._principal = principal or Principal.admin(subject="anonymous")
        return clone

    def _denied(self, meter_id: str) -> Optional[dict]:
        """Return an error payload when ``meter_id`` is out of scope.

        The reply deliberately does not confirm the meter exists: a consumer
        probing ids should not be able to distinguish "no such meter" from "not
        yours".
        """
        if self._principal.can_access(meter_id):
            return None
        return {
            "error": "access denied",
            "meter_id": str(meter_id or ""),
            "detail": "this meter is not in your account",
        }

    # ── helpers ─────────────────────────────────────────────────────────
    def _label(self, meter_id: str) -> str:
        point = self.base_points.points.get(meter_id)
        if point is not None and point.description:
            return point.description
        config = self.meters.meters.get(meter_id)
        if config is not None:
            return f"meter {meter_id} (modbus unit {config.modbus_unit_id})"
        return f"meter {meter_id}"

    def _describe_point(self, point_id: str) -> str:
        point = self.base_points.points.get(point_id)
        return point.description if point is not None and point.description else point_id

    def _comm(self, meter_id: str) -> tuple[str, Optional[str]]:
        """Re-derive comm status: only point values are persisted, not status."""
        try:
            snap = self.buffer.get_snapshot(meter_id)
        except Exception:  # noqa: BLE001 - never let a tool crash the request
            return "unknown", None
        if not snap:
            return "unknown", None
        last = last_contact_utc(snap.get("payload") or {})
        status = derive_comm_status(last, online_hours=self.online_hours, watch_hours=self.watch_hours)
        return status, None if last is None else last.isoformat(timespec="seconds")

    def _points_for(self, meter_id: str) -> dict[str, Any]:
        try:
            return self.buffer.get_normalized(meter_id)
        except Exception:  # noqa: BLE001
            return {}

    # ── handlers ────────────────────────────────────────────────────────
    def _list_meters(self, _args: dict) -> Any:
        rows = self.buffer.list_normalized_meters()
        out = []
        skipped = 0
        for row in rows:
            meter_id = row["meter_id"]
            # Filtering here is the actual access control: a consumer's list is
            # built from their own meters, so no other meter id or status ever
            # reaches the model.
            if not self._principal.can_access(meter_id):
                skipped += 1
                continue
            status, last = self._comm(meter_id)
            out.append(
                {
                    "meter_id": meter_id,
                    "description": self._label(meter_id),
                    "comm_status": status,
                    "last_contact_utc": last,
                    "points_with_values": row["points"],
                    "values_updated_utc": row["updated_utc"],
                }
            )
        payload = {"meters": out, "count": len(out)}
        if skipped and not self._principal.sees_all_meters:
            # Tell the model the list is deliberately partial so it does not
            # claim the estate only has these meters.
            payload["scope"] = "your meters only"
        return payload

    def _get_meter(self, args: dict) -> Any:
        meter_id = str(args.get("meter_id") or "").strip()
        if not meter_id:
            return {"error": "meter_id is required"}
        denied = self._denied(meter_id)
        if denied is not None:
            return denied
        points = self._points_for(meter_id)
        status, last = self._comm(meter_id)
        values = [
            {
                "point_id": pid,
                "description": self._describe_point(pid),
                "value": data["value"],
                "unit": data["unit"],
                "updated_utc": data["updated_utc"],
            }
            for pid, data in sorted(points.items())
        ]
        return {
            "meter_id": meter_id,
            "description": self._label(meter_id),
            "comm_status": status,
            "last_contact_utc": last,
            "values": values,
        }

    def _compare_meters(self, args: dict) -> Any:
        raw = args.get("meter_ids") or []
        if isinstance(raw, str):
            raw = [raw]
        requested = [str(m).strip() for m in raw if str(m).strip()]
        if len(requested) < 2:
            return {"error": "provide at least two meter_ids to compare"}

        # Compare is the easiest way to smuggle a foreign meter into context, so
        # the whole request is validated up front: one out-of-scope id rejects
        # the call rather than silently dropping it, which would otherwise let a
        # caller infer that the dropped meter exists.
        if not self._principal.sees_all_meters:
            denied = [m for m in requested if not self._principal.can_access(m)]
            if denied:
                return {
                    "error": "access denied",
                    "requested_count": len(requested),
                    "detail": "one or more of those meters are not in your account",
                }
        ids = requested[:_MAX_COMPARE_METERS]

        merged: dict[str, Any] = {}
        for meter_id in ids:
            status, last = self._comm(meter_id)
            merged[meter_id] = {
                "comm_status": status,
                "last_contact_utc": last,
                "points": {
                    pid: {"value": d["value"], "unit": d["unit"], "updated_utc": d["updated_utc"]}
                    for pid, d in sorted(self._points_for(meter_id).items())
                },
            }
        point_ids = sorted({pid for m in merged.values() for pid in m["points"]})
        rows = []
        for pid in point_ids:
            rows.append(
                {
                    "point_id": pid,
                    "description": self._describe_point(pid),
                    "values": {
                        meter_id: (data["points"].get(pid) or {}).get("value")
                        for meter_id, data in merged.items()
                    },
                    "unit": next(
                        (
                            data["points"][pid]["unit"]
                            for data in merged.values()
                            if pid in data["points"]
                        ),
                        "",
                    ),
                }
            )
        return {"meters": ids, "comm_status": {k: v["comm_status"] for k, v in merged.items()}, "comparison": rows}

    # ── BMS/oBIX handlers ───────────────────────────────────────────────
    def _list_bms_points(self, args: dict) -> Any:
        if self.obix_store is None:
            return {"error": "BMS data store is not available"}
        meter_id = str(args.get("meter_id") or "").strip()
        if not meter_id:
            return {"error": "meter_id is required"}
        denied = self._denied(meter_id)
        if denied is not None:
            return denied
        try:
            limit = int(args.get("limit") or _DEFAULT_BMS_POINTS)
        except (TypeError, ValueError):
            limit = _DEFAULT_BMS_POINTS
        limit = max(1, min(limit, _MAX_BMS_POINTS))
        rows = self.obix_store.points(meter_id, limit=limit)
        points = [
            {
                "point_id": r["point_id"],
                "name": r.get("name") or r["point_id"],
                "value": r.get("value"),
                "text_value": r.get("text_value"),
                "unit": r.get("unit") or "",
                "is_type": r.get("is_type") or "",
                "updated_utc": r.get("captured_utc"),
            }
            for r in rows
        ]
        return {
            "meter_id": meter_id,
            "points": points,
            "count": len(points),
            "total_points": self.obix_store.points_count(meter_id),
            "note": "use get_bms_history with a point_id for its trend",
        }

    def _get_bms_history(self, args: dict) -> Any:
        if self.obix_store is None:
            return {"error": "BMS data store is not available"}
        meter_id = str(args.get("meter_id") or "").strip()
        point_id = str(args.get("point_id") or "").strip()
        if not meter_id or not point_id:
            return {"error": "meter_id and point_id are required"}
        denied = self._denied(meter_id)
        if denied is not None:
            return denied
        try:
            limit = int(args.get("limit") or _DEFAULT_BMS_HISTORY)
        except (TypeError, ValueError):
            limit = _DEFAULT_BMS_HISTORY
        limit = max(1, min(limit, _MAX_BMS_HISTORY))
        since = args.get("since") or None
        until = args.get("until") or None
        rows = self.obix_store.history(
            meter_id, point_id, since_utc=since, until_utc=until, limit=limit
        )
        return {
            "meter_id": meter_id,
            "point_id": point_id,
            "since": since,
            "until": until,
            "samples": len(rows),
            "history": [dict(r) for r in rows],
        }

    def _list_bms_findings(self, args: dict) -> Any:
        """Points whose latest value violates its configured requirements."""
        if self.obix_store is None:
            return {"error": "BMS data store is not available"}
        if self.obix_validator is None:
            return {"error": "BMS requirements are not configured"}
        meter_id = str(args.get("meter_id") or "").strip()
        if meter_id:
            denied = self._denied(meter_id)
            if denied is not None:
                return denied

        min_severity = str(args.get("min_severity") or "warning").strip().lower()
        threshold = SEVERITY_ORDER.get(min_severity, SEVERITY_ORDER["warning"])
        try:
            limit = int(args.get("limit") or _DEFAULT_BMS_FINDINGS)
        except (TypeError, ValueError):
            limit = _DEFAULT_BMS_FINDINGS
        limit = max(1, min(limit, _MAX_BMS_FINDINGS))

        # Scope: a consumer sees only their own meters, and a consumer with no
        # meters must see nothing - an empty scope must not mean "all meters".
        meter_ids = None
        if not self._principal.sees_all_meters:
            meter_ids = [str(m) for m in (self._principal.meters or ()) if str(m)]
            if not meter_ids:
                return {
                    "findings": [],
                    "count": 0,
                    "total": 0,
                    "scanned_points": 0,
                    "min_severity": min_severity,
                }

        rows = [
            dict(r)
            for r in self.obix_store.latest(
                meter_id=meter_id or None, meter_ids=meter_ids, limit=None
            )
        ]
        findings = [
            f
            for f in self.obix_validator.scan(rows)
            if SEVERITY_ORDER.get(f.severity, 0) >= threshold
        ]
        findings.sort(
            key=lambda f: (-SEVERITY_ORDER.get(f.severity, 0), f.meter_id, f.point_id)
        )
        out = [f.as_dict() for f in findings[:limit]]
        return {
            "findings": out,
            "count": len(out),
            "total": len(findings),
            "scanned_points": len(rows),
            "min_severity": min_severity,
            "note": "requirements: config/obix_requirements.yaml",
        }

    # ── dispatch ────────────────────────────────────────────────────────
    def dispatch(self, name: str, arguments: Optional[dict]) -> str:
        """Run one tool and return the JSON string handed to the model."""
        handler_name = self._handler_names.get(name)
        if handler_name is None:
            return _clip({"error": f"unknown tool: {name}"})
        try:
            result = getattr(self, handler_name)(arguments or {})
        except Exception as exc:  # noqa: BLE001 - a bad tool must not kill the turn
            return _clip({"error": f"{type(exc).__name__}: {exc}"})
        return _clip(result)

    @staticmethod
    def utc_now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")
