"""Live per-role registry synced from the ems-backend database.

ems-backend is the authority on *who exists* and *what they may see*:

  * ``users.role``        - ADMIN / TECHNICIAN see the whole estate,
                            USER is restricted to their own meters.
  * ``consumer_meters``   - the meter allow-list for a consumer.
  * ``users.logger_id``   - fallback meter for records created before the
                            join table was populated (same rule the backend's
                            ``AiService.resolveScope`` applies).

Access is strictly read-only: exactly two SELECTs, on ``users`` and
``consumer_meters``, using a dedicated account granted SELECT on those tables
only. The gateway never writes to the ems database, and a user that is
deleted, deactivated or suspended disappears from the registry on the next
refresh - the AI assistant can never outlive its account.

The map is merged with the operator-managed ``AI_PRINCIPALS`` entries
(static wins on conflict), then fed to the signed-assertion resolver, so an
EMS user's identity is proven by ems-backend and their scope comes from the
same database the backend itself reads.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Iterator, Mapping
from typing import Any, Optional

import pymysql

from .roles import Principal, Role

log = logging.getLogger("gateway.ai.ems_sync")

_USERS_SQL = (
    "SELECT id, email, role, logger_id FROM users"
    " WHERE deleted_at IS NULL AND status = 'ACTIVE'"
)
_METERS_SQL = "SELECT user_id, meter_id FROM consumer_meters"


class EmsUserSync:
    """Loads EMS users as principals from the ems-backend MySQL (read-only)."""

    def __init__(
        self,
        host: str,
        port: int,
        user: str,
        password: str,
        database: str,
        ttl_seconds: float = 60.0,
    ) -> None:
        self._conn_kwargs = dict(
            host=host,
            port=int(port),
            user=user,
            password=password,
            database=database,
            charset="utf8mb4",
            cursorclass=pymysql.cursors.DictCursor,
            connect_timeout=5,
        )
        self.database = database
        self.ttl_seconds = max(5.0, float(ttl_seconds))
        self._cache: dict[str, Principal] = {}
        self._loaded_at: float = 0.0
        self._last_error: Optional[str] = None

    # ── loading ───────────────────────────────────────────────────────────
    def _fetch(self) -> dict[str, Principal]:
        conn = pymysql.connect(**self._conn_kwargs)
        try:
            with conn.cursor() as cur:
                cur.execute(_USERS_SQL)
                users = cur.fetchall()
                cur.execute(_METERS_SQL)
                meter_rows = cur.fetchall()
        finally:
            conn.close()

        meters_by_user: dict[str, list[str]] = {}
        for row in meter_rows:
            uid = str(row["user_id"] or "").strip()
            mid = str(row["meter_id"] or "").strip()
            if uid and mid:
                meters_by_user.setdefault(uid, []).append(mid)

        out: dict[str, Principal] = {}
        for row in users:
            subject = str(row["id"] or "").strip()
            if not subject:
                continue
            email = str(row["email"] or "").strip()
            role = Role.parse(row["role"], default=Role.USER)
            if role is Role.USER:
                # A consumer's allow-list: the join table plus the legacy
                # logger_id fallback - identical to the backend's own
                # resolveScope(), so the two layers can never disagree.
                meters = list(dict.fromkeys(
                    meters_by_user.get(subject, []) + [str(row["logger_id"] or "").strip()]
                ))
                out[subject] = Principal.consumer(subject, meters, label=email)
            else:
                out[subject] = Principal.admin(subject, label=email)
        return out

    def load(self, force: bool = False) -> dict[str, Principal]:
        """Return the current subject -> principal map, refreshing if stale.

        On a failed refresh the last-good map is kept (so a database blip
        does not lock everybody out mid-conversation) and the error is logged.
        """
        now = time.monotonic()
        if force or not self._cache or (now - self._loaded_at) >= self.ttl_seconds:
            try:
                self._cache = self._fetch()
                self._loaded_at = now
                self._last_error = None
            except Exception as exc:  # noqa: BLE001 - keep last-good map
                self._last_error = f"{type(exc).__name__}: {exc}"
                log.error("ems user sync failed, keeping previous registry: %s", self._last_error)
        return self._cache

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def describe(self) -> dict[str, Any]:
        """Registry shape for /ai/status - counts only, never identities."""
        users = self.load()
        return {
            "source": "ems-database",
            "database": self.database,
            "users": len(users),
            "admins": sum(1 for p in users.values() if p.sees_all_meters),
            "consumers": sum(1 for p in users.values() if not p.sees_all_meters),
            "ttl_seconds": self.ttl_seconds,
            "last_error": self._last_error,
        }


class MergedSubjectMap(Mapping):
    """subject -> Principal: operator ``AI_PRINCIPALS`` first, EMS users second.

    Implements the Mapping protocol so :class:`SignedAssertionResolver` can
    consult it unchanged; every access refreshes the EMS view if its TTL has
    expired, which keeps newly created or deactivated accounts current without
    a restart. Static operator entries always win - a hand-configured line is
    a deliberate act and must not be silently overridden by the database.
    """

    def __init__(self, static: Mapping[str, Principal], sync: EmsUserSync) -> None:
        self._static = static
        self._sync = sync

    def _ems(self) -> Mapping[str, Principal]:
        return self._sync.load()

    def __getitem__(self, key: str) -> Principal:
        if key in self._static:
            return self._static[key]
        return self._ems()[key]

    def __contains__(self, key: object) -> bool:
        if key in self._static:
            return True
        return key in self._ems()

    def __iter__(self) -> Iterator[str]:
        seen = set(self._static)
        yield from self._static
        for subject in self._ems():
            if subject not in seen:
                yield subject

    def __len__(self) -> int:
        return sum(1 for _ in self)

    def get(self, key: str, default: Any = None) -> Any:
        try:
            return self[key]
        except KeyError:
            return default
