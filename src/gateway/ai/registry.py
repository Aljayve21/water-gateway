"""Parse the ``AI_PRINCIPALS`` config line into a resolver.

Format (one caller per entry, separated by newlines or ``;``; ``#`` starts a
comment)::

    <subject>:<role>:<meters|*>:<token>

Separators are both accepted because ``.env`` values are always a single line,
so a newline-only parser would silently read everything as one principal.

* ``subject`` - stable id, matched to an ems-backend ``users.id`` later.
* ``role`` - ``ADMIN`` / ``TECHNICIAN`` / ``USER``; anything unrecognised
  becomes ``USER``, so a typo cannot accidentally create an administrator.
* ``meters`` - comma-separated ids, or ``*`` for "no restriction". ``*`` only
  applies to privileged roles; on a ``USER`` line it is treated as *no meters*
  rather than *all meters*, so ``user:maria:USER:*:<token>`` locks maria down
  instead of opening the estate.
* ``token`` - the secret, at least ``MIN_TOKEN_LEN`` characters.

Deliberately strict: a malformed line is skipped with a warning and the rest of
the registry still loads. One typo should not take the assistant offline, but it
must not silently widen access either.
"""

from __future__ import annotations

import logging
import re
from typing import Iterable, Optional

from .roles import (
    MIN_TOKEN_LEN,
    Principal,
    PrincipalEntry,
    Role,
    StaticPrincipalResolver,
)

log = logging.getLogger("gateway.ai.auth")

# Entries may be separated by newlines or semicolons; either may precede a
# comment.
_COMMENT = "#"
_ENTRY_SEPARATORS = re.compile(r"[\n\r;]+")


def principals_by_subject(resolver: StaticPrincipalResolver) -> dict:
    """subject -> Principal, for the signed-assertion resolver to consult.

    When a subject is defined twice the narrower of the two wins: a repeated
    line must never be able to widen that person's access. For meters that
    means the intersection; for role it means USER unless both agree on the same
    privileged role.
    """
    out: dict = {}
    for entry in resolver.entries:
        principal = getattr(entry, "principal", None)
        if principal is None or not principal.subject:
            continue
        existing = out.get(principal.subject)
        if existing is None or existing == principal:
            out[principal.subject] = principal
            continue

        log.warning(
            "AI_PRINCIPALS subject %s is defined twice with different roles or meters "
            "- keeping the narrower definition",
            principal.subject,
        )
        if existing.sees_all_meters and principal.sees_all_meters:
            # Both are unrestricted, so there is no narrowing to apply; the role
            # label differing (ADMIN vs TECHNICIAN) is not a conflict, and
            # collapsing it to USER would lock out a legitimate operator.
            role, meters = existing.role, existing.meters
        elif existing.sees_all_meters and not principal.sees_all_meters:
            role, meters = principal.role, principal.meters
        elif principal.sees_all_meters and not existing.sees_all_meters:
            role, meters = existing.role, existing.meters
        else:
            role = existing.role if existing.role == principal.role else Role.USER
            meters = tuple(sorted(set(existing.meters) & set(principal.meters)))
        out[principal.subject] = Principal(
            subject=principal.subject,
            role=role,
            meters=meters,
            label=existing.label or principal.label,
        )
    return out


def parse_principals(raw: str) -> StaticPrincipalResolver:
    """Build a resolver from the raw ``AI_PRINCIPALS`` value."""
    entries = []
    skipped: list[str] = []
    for lineno, line in enumerate(_iter_lines(raw), start=1):
        text = line.strip()
        if not text or text[0] == _COMMENT:
            continue
        parts = text.split(":", 3)
        if len(parts) != 4:
            skipped.append(f"line {lineno}: expected 4 colon-separated fields")
            continue
        subject, role_raw, meters_raw, token = (p.strip() for p in parts)

        if not subject:
            skipped.append(f"line {lineno}: empty subject")
            continue
        if len(token) < MIN_TOKEN_LEN:
            skipped.append(f"line {lineno}: token shorter than {MIN_TOKEN_LEN} characters")
            continue

        role = Role.parse(role_raw, default=None)
        if role is None:
            skipped.append(f"line {lineno}: unknown role {role_raw!r}, treated as USER")
            role = Role.USER

        if meters_raw == "*":
            # "*" means "unrestricted" only for a privileged role. On a USER
            # line it resolves to an empty allow-list - i.e. no access - so the
            # most likely typo cannot open the whole estate.
            meters: tuple = ()
            if not role.is_privileged:
                skipped.append(
                    f"line {lineno}: '*' ignored for a {role.value} account, so it sees no meters"
                )
        else:
            meters = tuple(m for m in (x.strip() for x in meters_raw.split(",")) if m)

        entries.append(
            PrincipalEntry(
                token=token,
                principal=Principal(subject=subject, role=role, meters=meters, label=subject),
            )
        )

    for reason in skipped:
        log.warning("AI_PRINCIPALS %s", reason)

    resolver = StaticPrincipalResolver(entries=tuple(entries))
    if not entries:
        log.warning(
            "AI_PRINCIPALS is set but produced no usable principals - every AI request "
            "will be rejected unless AI_BEARER_TOKEN is also configured"
        )
    else:
        for entry in entries:
            p = entry.principal
            log.info(
                "ai principal registered: subject=%s role=%s meters=%s",
                p.subject,
                p.role.value,
                "all" if p.sees_all_meters else (",".join(p.meters) or "none"),
            )
    return resolver


def _iter_lines(raw: Optional[str]) -> Iterable[str]:
    if not raw:
        return []
    normalized = raw.replace("\r\n", "\n").replace("\r", "\n")
    return _ENTRY_SEPARATORS.split(normalized)
