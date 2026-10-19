"""Who is asking, and which meters they are allowed to see.

The AI assistant used to have exactly one shared bearer token: whoever held it
saw every meter. That is fine for an operator with the whole estate in view and
unacceptable for a consumer who should only ever see their own meter.

This module introduces the identity layer that separates the two:

    Role      ADMIN | TECHNICIAN | USER  - mirrors ems-backend's Prisma enum
    Principal the authenticated caller: subject, role, and an explicit
              allow-list of meter ids
    PrincipalResolver  maps a bearer token -> Principal

Enforcement is deliberately *not* done in the system prompt. Telling a model
"only discuss meter X" is a hint, not a control: the model can be talked out of
it, and tool output is what actually carries the data. So scoping lives in
:meth:`gateway.ai.tools.MeterTools`, which filters rows before anything reaches
Bedrock. A USER physically cannot obtain another meter's values, because the
numbers are never placed in the model's context in the first place.

Design notes
------------
* A Principal is immutable and holds no secret. The token -> Principal mapping
  is what is sensitive, and that lives in the resolver.
* An empty ``meters`` tuple is never treated as "see everything". Callers that
  legitimately see everything use a privileged role instead. This is the
  failure mode worth being paranoid about: an empty allow-list that falls back
  to all meters turns a misconfigured consumer account into a data leak.
* ``PrincipalResolver`` is a Protocol so the static registry used today can be
  swapped for a database-backed resolver later without touching the server, the
  tools, or the tests.
"""

from __future__ import annotations

import hmac
from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable, Optional, Protocol, runtime_checkable

# A meter id longer than this is not a meter id; treat it as hostile input
# rather than passing it to a lookup.
_MAX_METER_ID_LEN = 64

# Minimum length for a per-caller token. Matches the existing
# AI_BEARER_TOKEN rule: a short secret is not a secret.
MIN_TOKEN_LEN = 32


class Role(str, Enum):
    """Caller roles. Values match ems-backend's ``Role`` enum exactly."""

    ADMIN = "ADMIN"
    TECHNICIAN = "TECHNICIAN"
    USER = "USER"

    @property
    def is_privileged(self) -> bool:
        """True for roles that may see the whole estate."""
        return self in (Role.ADMIN, Role.TECHNICIAN)

    @classmethod
    def parse(cls, raw: object, default: "Role" = None) -> "Role":
        """Lenient parse for config/env input. Unknown values fall back."""
        if isinstance(raw, Role):
            return raw
        text = str(raw or "").strip().upper()
        for role in cls:
            if role.value == text:
                return role
        return default if default is not None else Role.USER


@dataclass(frozen=True)
class Principal:
    """An authenticated caller and the meters they may read.

    ``meters`` is the complete allow-list. For a privileged principal it may be
    empty, meaning "no restriction" (see :meth:`can_access`). For a non-
    privileged principal an empty tuple grants nothing.
    """

    subject: str
    role: Role
    meters: tuple = ()
    label: str = ""

    def __post_init__(self) -> None:
        object.__setattr__(self, "subject", str(self.subject or "").strip())
        object.__setattr__(self, "role", Role.parse(self.role))
        object.__setattr__(self, "label", str(self.label or "").strip())
        cleaned: list[str] = []
        for raw in self.meters or ():
            mid = str(raw or "").strip()
            if mid and len(mid) <= _MAX_METER_ID_LEN and mid not in cleaned:
                cleaned.append(mid)
        object.__setattr__(self, "meters", tuple(cleaned))

    @property
    def sees_all_meters(self) -> bool:
        return self.role.is_privileged

    def can_access(self, meter_id: str) -> bool:
        """True when this principal may read ``meter_id``."""
        mid = str(meter_id or "").strip()
        if not mid:
            return False
        if self.sees_all_meters:
            return True
        return mid in self.meters

    def scope(self, meter_ids: Iterable[str]) -> list:
        """Filter ``meter_ids`` down to what this principal may read.

        Order is preserved and duplicates are collapsed so a filtered list still
        looks like the caller's own view of the estate.
        """
        out: list = []
        for mid in meter_ids or ():
            key = str(mid or "").strip()
            if key and self.can_access(key) and key not in out:
                out.append(key)
        return out

    def filter_findings(self, findings: Iterable[dict]) -> list:
        """Drop findings for meters this principal may not read.

        Operates on the persisted ``anomaly_finding`` rows, whose meter id is
        stored under ``meter_id``.
        """
        out = []
        for row in findings or ():
            if not isinstance(row, dict):
                continue
            if self.can_access(row.get("meter_id")):
                out.append(row)
        return out

    def describe(self) -> dict:
        """Secret-free description for /ai/status and logs."""
        return {
            "subject": self.subject,
            "role": self.role.value,
            "label": self.label,
            "sees_all_meters": self.sees_all_meters,
            "meter_count": None if self.sees_all_meters else len(self.meters),
        }

    @classmethod
    def admin(cls, subject: str = "admin", label: str = "") -> "Principal":
        return cls(subject=subject, role=Role.ADMIN, meters=(), label=label)

    @classmethod
    def consumer(cls, subject: str, meters: Iterable[str], label: str = "") -> "Principal":
        """A consumer restricted to exactly ``meters``.

        Named explicitly so the intent is obvious at every call site: a
        consumer is always a narrow allow-list, never "all but some".
        """
        return cls(subject=subject, role=Role.USER, meters=tuple(meters), label=label)


@runtime_checkable
class PrincipalResolver(Protocol):
    """Maps a presented bearer token to a Principal (or None)."""

    def resolve(self, token: str) -> Optional[Principal]:
        ...


@dataclass
class StaticPrincipalResolver:
    """Registry-backed resolver: an in-memory token -> Principal map.

    Tokens are compared with :func:`hmac.compare_digest` in constant time, and
    the whole registry is walked rather than dict-keyed so that a timing
    difference cannot reveal which prefix matched.
    """

    entries: tuple = ()
    _fallback: Optional[Principal] = field(default=None, repr=False)

    def __post_init__(self) -> None:
        self.entries = tuple(self.entries or ())

    def resolve(self, token: str) -> Optional[Principal]:
        supplied = str(token or "").strip()
        if not supplied:
            return None
        for entry in self.entries:
            principal = getattr(entry, "principal", None)
            secret = str(getattr(entry, "token", "") or "")
            if not secret or principal is None:
                continue
            if hmac.compare_digest(supplied, secret):
                return principal
        return self._fallback

    def subjects(self) -> list:
        return [getattr(e, "principal", None).subject for e in self.entries if getattr(e, "principal", None)]

    def describe(self) -> dict:
        """Registry shape for /ai/status - never includes token material."""
        rows = []
        for entry in self.entries:
            principal = getattr(entry, "principal", None)
            if principal is None:
                continue
            row = principal.describe()
            row["token_fingerprint"] = _fingerprint(str(getattr(entry, "token", "") or ""))
            rows.append(row)
        return {
            "source": "static",
            "count": len(rows),
            "principals": rows,
            "unauthenticated_fallback": self._fallback is not None,
        }


@dataclass(frozen=True)
class PrincipalEntry:
    """One row of the registry: a secret and the caller it identifies."""

    token: str
    principal: Principal


def _fingerprint(token: str) -> str:
    """Short, non-reversible tag so operators can tell tokens apart in logs."""
    import hashlib

    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]
