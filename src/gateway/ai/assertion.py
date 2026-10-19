"""Signed identity assertions: how ems-backend proves who is asking.

The gateway must not trust a plain ``X-AI-User`` header. Anyone holding one
service token could then claim to be an administrator. Instead ems-backend
signs a short-lived assertion with a shared secret and the gateway verifies the
signature itself::

    v1.<subject-b64url>.<expires-unix>.<hmac-sha256-hex>

The HMAC covers the version, the subject and the expiry, so neither the
identity nor the lifetime can be altered in transit. The gateway then looks the
subject up in its own principal registry, which keeps role and meter ownership
in exactly one place - ems-backend asserts *who*, the gateway decides *what
they may see*.

Design points worth keeping in mind:

* Verification fails closed. A bad signature, an unknown subject, an expired or
  malformed assertion all resolve to ``None``, which the server turns into 401.
* Expiry is checked before the subject is used, so a stale assertion cannot be
  replayed. The window is short (default 120s) because ems-backend mints one per
  request.
* A constant-time compare is used for the signature, and the whole registry is
  walked rather than dict-keyed on the raw token.
* This resolver speaks the same :class:`PrincipalResolver` protocol as the
  static one, so the HTTP layer is unchanged whether or not assertions are on.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import time
from dataclasses import dataclass
from typing import Mapping, Optional

from .roles import Principal, PrincipalResolver

VERSION = "v1"
DEFAULT_TTL_SECONDS = 120
# Generous ceiling so a misconfigured EMS_ASSURANCE_TTL cannot mint assertions
# that stay valid for hours.
MAX_TTL_SECONDS = 600


def _b64e(raw: str) -> str:
    return base64.urlsafe_b64encode(raw.encode("utf-8")).decode("ascii").rstrip("=")


def _b64d(value: str) -> str:
    pad = "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(value + pad).decode("utf-8")


def sign_assertion(secret: str, subject: str, ttl_seconds: int = DEFAULT_TTL_SECONDS) -> str:
    """Mint an assertion. Used by ems-backend; mirrored here for tests and ops.

    Lives in the gateway so the exact wire format has one definition. The secret
    must never appear in either repo.
    """
    if not secret:
        raise ValueError("an assertion secret is required")
    if not subject:
        raise ValueError("subject is required")
    ttl = max(1, min(int(ttl_seconds), MAX_TTL_SECONDS))
    expires = int(time.time()) + ttl
    body = f"{VERSION}.{_b64e(subject)}.{expires}"
    sig = hmac.new(secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256).hexdigest()
    return f"{body}.{sig}"


@dataclass
class SignedAssertionResolver:
    """Verify assertions, then resolve the subject against a local registry.

    ``principals`` maps a subject (an ems-backend ``users.id``) to the Principal
    that describes its role and meters. The signature only proves the caller is
    ems-backend; the registry remains the authority on access.
    """

    secret: str
    principals: Mapping[str, Principal]
    clock_skew_seconds: int = 30

    def resolve(self, token: str) -> Optional[Principal]:
        supplied = str(token or "").strip()
        if not supplied or not self.secret:
            return None

        parts = supplied.split(".")
        if len(parts) != 4 or parts[0] != VERSION:
            return None
        _version, subject_b64, expires_raw, signature = parts

        try:
            expires = int(expires_raw)
        except ValueError:
            return None

        # Checked before the HMAC so an expired assertion is rejected cheaply,
        # and re-checked after so a forged future expiry cannot slip through.
        now = int(time.time())
        if expires < now - self.clock_skew_seconds:
            return None

        body = f"{VERSION}.{subject_b64}.{expires_raw}"
        expected = hmac.new(
            self.secret.encode("utf-8"), body.encode("ascii"), hashlib.sha256
        ).hexdigest()
        if not hmac.compare_digest(signature, expected):
            return None
        if expires < now:
            return None

        try:
            subject = _b64d(subject_b64)
        except (ValueError, UnicodeDecodeError):
            return None
        if not subject or len(subject) > 128:
            return None

        # Unknown subject = no access. A valid signature alone is not enough:
        # ems-backend must not be able to mint an identity the gateway has never
        # been told about.
        return self.principals.get(subject)

    def describe(self) -> dict:
        return {
            "source": "signed-assertion",
            "enabled": bool(self.secret),
            "registered_subjects": len(self.principals or {}),
            "ttl_seconds": DEFAULT_TTL_SECONDS,
        }
