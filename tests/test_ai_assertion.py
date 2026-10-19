"""Signed identity assertions: ems-backend proving who is asking.

The threat these tests exist to stop: a caller who obtains the shared secret (or
any one static token) must not be able to act as an arbitrary user. So each
negative case asserts that verification fails *closed*, and the positive cases
assert that a correctly signed assertion still resolves through the gateway's own
registry rather than carrying its own privileges.
"""

from __future__ import annotations

import base64
import time

import pytest

from gateway.ai.assertion import (
    MAX_TTL_SECONDS,
    VERSION,
    SignedAssertionResolver,
    sign_assertion,
)
from gateway.ai.registry import parse_principals, principals_by_subject
from gateway.ai.roles import Principal, Role
from gateway.ai_server import _ChainedResolver

SECRET = "shared-secret-value-that-is-long-enough-1234567890"
M1 = "026026060001"
M2 = "026026060003"

USER_1 = "clxaaaaaaaaaaaaaa000002"  # maria
ADMIN_1 = "clxaaaaaaaaaaaaaa000001"  # admin


def _registry():
    """Registry keyed by ems-backend users.id, as production would be."""
    return parse_principals(
        f"{USER_1}:USER:{M2}:{'a' * 40}\n{ADMIN_1}:ADMIN:*:{'b' * 40}"
    )


@pytest.fixture()
def resolver():
    return SignedAssertionResolver(
        secret=SECRET, principals=principals_by_subject(_registry())
    )


# ── happy path ──────────────────────────────────────────────────────────
def test_valid_assertion_resolves_to_the_consumer(resolver):
    p = resolver.resolve(sign_assertion(SECRET, USER_1))
    assert p is not None
    assert p.subject == USER_1
    assert p.role is Role.USER
    assert p.can_access(M2)
    assert not p.can_access(M1)


def test_valid_assertion_resolves_to_an_admin(resolver):
    p = resolver.resolve(sign_assertion(SECRET, ADMIN_1))
    assert p.sees_all_meters


def test_assertion_grants_nothing_the_registry_does_not(resolver):
    """The signature proves identity only; scope still comes from the registry."""
    p = resolver.resolve(sign_assertion(SECRET, USER_1))
    assert p.meters == (M2,)


# ── signature failures ──────────────────────────────────────────────────
def test_wrong_secret_is_rejected(resolver):
    other = "a-different-secret-that-is-also-long-enough-abcdef"
    assert resolver.resolve(sign_assertion(other, ADMIN_1)) is None


def test_tampered_signature_is_rejected(resolver):
    token = sign_assertion(SECRET, ADMIN_1)
    forged = token[:-1] + ("0" if token[-1] != "0" else "1")
    assert resolver.resolve(forged) is None


def test_tampered_subject_is_rejected(resolver):
    """Swapping in an admin subject must not survive verification."""
    admin_assertion = sign_assertion(SECRET, ADMIN_1)
    parts = admin_assertion.split(".")
    parts[1] = base64.urlsafe_b64encode(ADMIN_1.encode()).decode().rstrip("=")
    parts[3] = "a" * 64
    assert resolver.resolve(".".join(parts)) is None


def test_tampered_expiry_is_rejected(resolver):
    parts = sign_assertion(SECRET, USER_1).split(".")
    parts[2] = str(int(time.time()) + 100_000)
    assert resolver.resolve(".".join(parts)) is None


def test_empty_secret_disables_the_path(resolver):
    off = SignedAssertionResolver(secret="", principals=principals_by_subject(_registry()))
    assert off.resolve(sign_assertion(SECRET, ADMIN_1)) is None


# ── expiry ──────────────────────────────────────────────────────────────
def test_expired_assertion_is_rejected(resolver):
    token = sign_assertion(SECRET, USER_1, ttl_seconds=1)
    # Age it past the window rather than sleeping.
    parts = token.split(".")
    parts[2] = str(int(time.time()) - 600)
    resolver.clock_skew_seconds = 0
    assert resolver.resolve(".".join(parts)) is None


def test_ttl_is_capped(resolver):
    """A long requested TTL must not mint a long-lived credential."""
    token = sign_assertion(SECRET, USER_1, ttl_seconds=86_400)
    expires = int(token.split(".")[2])
    assert expires - int(time.time()) <= MAX_TTL_SECONDS


# ── malformed input ─────────────────────────────────────────────────────
@pytest.mark.parametrize(
    "bad",
    [
        "",
        "   ",
        "v1",
        "v1.a.b",
        "v1.a.b.c.d",
        "v2.abc.9999999999.ffff",
        "v1.abc.notanumber.ffff",
        "v1.!!!.9999999999.ffff",
        "v1.abc.9999999999",
    ],
)
def test_malformed_assertions_are_rejected_not_crashed(resolver, bad):
    assert resolver.resolve(bad) is None


def test_unknown_subject_is_rejected(resolver):
    """A valid signature for someone the gateway was never told about."""
    assert resolver.resolve(sign_assertion(SECRET, "ghost-user-999")) is None


def test_blank_subject_is_rejected(resolver):
    with pytest.raises(ValueError):
        sign_assertion(SECRET, "")


# ── registry interaction ────────────────────────────────────────────────
def test_principals_by_subject_keys_on_subject():
    by_subject = principals_by_subject(_registry())
    assert set(by_subject) == {USER_1, ADMIN_1}
    assert by_subject[ADMIN_1].sees_all_meters


def test_duplicate_subject_keeps_the_narrower_definition():
    """A repeated line must never widen access."""
    registry = parse_principals(
        f"dup:ADMIN:*:{'c' * 40}\ndup:USER:{M1}, {M2}:{'d' * 40}"
    )
    by_subject = principals_by_subject(registry)
    assert by_subject["dup"].role is Role.USER
    assert set(by_subject["dup"].meters) == {M1, M2}
    assert not by_subject["dup"].sees_all_meters


def test_duplicate_privileged_lines_stay_privileged():
    registry = parse_principals(f"dup:ADMIN:*:{'e' * 40}\ndup:TECHNICIAN:*:{'f' * 40}")
    assert principals_by_subject(registry)["dup"].sees_all_meters


def test_empty_registry_rejects_every_signed_assertion():
    empty = SignedAssertionResolver(secret=SECRET, principals={})
    assert empty.resolve(sign_assertion(SECRET, ADMIN_1)) is None


# ── chaining with the static resolver ───────────────────────────────────
def test_chained_resolver_serves_both_token_kinds():
    static = _registry()
    chain = _ChainedResolver(
        [static, SignedAssertionResolver(secret=SECRET, principals=principals_by_subject(static))]
    )
    via_token = chain.resolve("b" * 40)
    via_assertion = chain.resolve(sign_assertion(SECRET, ADMIN_1))
    assert via_token is not None and via_token.sees_all_meters
    assert via_assertion is not None and via_assertion.sees_all_meters


def test_chained_resolver_rejects_an_unknown_token():
    chain = _ChainedResolver([_registry()])
    assert chain.resolve("z" * 40) is None
    assert chain.resolve("") is None


def test_static_token_is_not_mistaken_for_an_assertion():
    chain = _ChainedResolver([_registry()])
    p = chain.resolve("a" * 40)
    assert p is not None and p.subject == USER_1


def test_chained_describe_reports_both_sources():
    static = _registry()
    chain = _ChainedResolver(
        [static, SignedAssertionResolver(secret=SECRET, principals=principals_by_subject(static))]
    )
    described = chain.describe()
    assert described["count"] == 2
    assert {s.get("source") for s in described["sources"]} == {"static", "signed-assertion"}


# ── format stability ────────────────────────────────────────────────────
def test_wire_format_is_documented_and_stable():
    token = sign_assertion(SECRET, USER_1)
    parts = token.split(".")
    assert len(parts) == 4
    assert parts[0] == VERSION
    assert base64.urlsafe_b64decode(parts[1] + "=" * (-len(parts[1]) % 4)).decode() == USER_1
    assert int(parts[2]) > time.time()
    assert len(parts[3]) == 64  # sha256 hex
