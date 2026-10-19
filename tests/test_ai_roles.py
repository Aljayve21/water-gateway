"""Per-role isolation for the AI assistant.

The requirement under test: *user1 cannot access user2's data*. These tests
assert that at the layer where it actually matters - the tool payload - rather
than trusting the model to behave.

A consumer's numbers must never be present in a Bedrock request, so the strong
assertion is made by feeding the dispatched tool output into a fake Bedrock
client and checking the recorded conversation.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.ai.agent import Assistant
from gateway.ai.registry import parse_principals
from gateway.ai.roles import Principal, Role, StaticPrincipalResolver
from gateway.ai.tools import MeterTools
from gateway.config import BasePoint, BasePointsConfig, MeterConfig, MetersConfig
from gateway.store import SQLiteBuffer

M1 = "026026060001"  # one consumer's meter
M2 = "026026060003"  # the other consumer's meter
M3 = "057A26060001"

# Distinct totalizers so any cross-meter leak is unambiguous in an assertion.
TOTALS = {M1: 111.5, M2: 222.5, M3: 333.5}


@pytest.fixture()
def buffer(tmp_path):
    buf = SQLiteBuffer(tmp_path / "roles.db")
    buf.initialize()
    yield buf
    buf.close()


@pytest.fixture()
def base_points_cfg():
    return BasePointsConfig(
        points={
            "totalizer": BasePoint(id="totalizer", description="Total water consumed", unit="m3"),
        }
    )


@pytest.fixture()
def meters_cfg():
    return MetersConfig(
        meters={
            m: MeterConfig(meter_id=m, modbus_unit_id=i)
            for i, m in enumerate((M1, M2, M3), start=1)
        }
    )


def _seed(buffer):
    """Three meters with distinct totalizers so a leak is unambiguous."""
    for meter_id, total in TOTALS.items():
        buffer.upsert_normalized(meter_id, "totalizer", total, "m3")


@pytest.fixture()
def tools(buffer, meters_cfg, base_points_cfg):
    _seed(buffer)
    return MeterTools(buffer, meters_cfg, base_points_cfg)


# ── Principal semantics ─────────────────────────────────────────────────
def test_admin_sees_every_meter():
    p = Principal.admin()
    assert p.can_access(M1) and p.can_access(M2)
    assert p.sees_all_meters


def test_consumer_sees_only_its_own():
    p = Principal.consumer("maria", [M2])
    assert p.can_access(M2)
    assert not p.can_access(M1)
    assert not p.can_access(M3)
    assert not p.sees_all_meters


def test_empty_allow_list_grants_nothing():
    """The critical fail-closed case.

    A consumer with no meters configured must see zero meters, never 'all'.
    If this ever inverted, a misconfigured account would read the estate.
    """
    p = Principal(subject="broken", role=Role.USER, meters=())
    assert not p.can_access(M1)
    assert not p.sees_all_meters
    assert p.scope([M1, M2]) == []


def test_scope_preserves_order_and_dedupes():
    p = Principal.consumer("maria", [M2])
    assert p.scope([M1, M2, M2, M3]) == [M2]


def test_unknown_role_falls_back_to_user_not_admin():
    """A typo in config must never create an administrator."""
    p = Principal(subject="x", role="superuser", meters=[M1])
    assert p.role is Role.USER
    assert not p.sees_all_meters


def test_technician_is_privileged_like_admin():
    assert Role.TECHNICIAN.is_privileged
    assert Principal(subject="t", role=Role.TECHNICIAN).can_access(M3)


def test_describe_never_includes_meter_ids_or_tokens():
    p = Principal.consumer("maria", [M1, M2], label="Maria")
    blob = json.dumps(p.describe())
    assert "111.5" not in blob
    assert M1 not in blob and M2 not in blob
    assert p.describe()["meter_count"] == 2


# ── registry parsing ────────────────────────────────────────────────────
def test_parse_principals_reads_roles_and_scopes():
    t_admin = "a" * 40
    t_maria = "b" * 40
    r = parse_principals(f"ops:ADMIN:*:{t_admin}\nmaria:USER:{M2},{M1}:{t_maria}")
    assert r.resolve(t_admin).role is Role.ADMIN
    maria = r.resolve(t_maria)
    assert maria.role is Role.USER
    assert maria.can_access(M2) and maria.can_access(M1)
    assert not maria.can_access(M3)


def test_star_on_a_user_line_grants_nothing():
    """`maria:USER:*` is a typo waiting to happen. It must not open the estate."""
    r = parse_principals(f"maria:USER:*:{'c' * 40}")
    p = r.resolve("c" * 40)
    assert not p.can_access(M1)
    assert p.meters == ()


def test_short_token_line_is_rejected():
    r = parse_principals("ops:ADMIN:*:tooshort")
    assert r.resolve("tooshort") is None


def test_semicolon_separated_entries_all_load():
    """Regression: .env values are one line.

    A newline-only parser read `a:ADMIN:*:tokA;b:USER:m2:tokB` as a single
    principal whose token was the whole tail, so every caller after the first
    was silently rejected with a live gateway.
    """
    ta, tb = "g" * 40, "h" * 40
    r = parse_principals(f"ops:ADMIN:*:{ta};maria:USER:{M2}:{tb}")
    assert r.resolve(ta).subject == "ops"
    assert r.resolve(tb).subject == "maria"


def test_mixed_separators_and_comment_prefix():
    ta, tb = "i" * 40, "j" * 40
    raw = f"# leading comment\nops:ADMIN:*:{ta}\n# another\nmaria:USER:{M2}:{tb}"
    r = parse_principals(raw)
    assert r.resolve(tb).subject == "maria"


def test_trailing_separator_does_not_create_an_empty_principal():
    r = parse_principals(f"ops:ADMIN:*:{'k' * 40};")
    assert r.describe()["count"] == 1


def test_malformed_line_is_skipped_but_rest_survives():
    good = "d" * 40
    r = parse_principals(f"garbage\nops:ADMIN:*:{good}")
    assert r.resolve(good) is not None


def test_unknown_token_resolves_to_none():
    r = parse_principals(f"ops:ADMIN:*:{'e' * 40}")
    assert r.resolve("f" * 40) is None
    assert r.resolve("") is None


def test_describe_shows_registry_shape_without_secrets():
    tok = "f" * 40
    r = parse_principals(f"ops:ADMIN:*:{tok}")
    blob = json.dumps(r.describe())
    assert tok not in blob
    assert r.describe()["count"] == 1


# ── tool-level enforcement (the real control) ───────────────────────────
def test_list_meters_is_filtered_for_a_consumer(tools):
    scoped = tools.for_principal(Principal.consumer("maria", [M2]))
    out = json.loads(scoped.dispatch("list_meters", {}))
    assert [m["meter_id"] for m in out["meters"]] == [M2]
    # M1/M3 must not merely be absent from the list - no trace of them.
    assert M1 not in json.dumps(out)
    assert M3 not in json.dumps(out)
    assert out["scope"] == "your meters only"


def test_admin_list_still_shows_everything(tools):
    out = json.loads(tools.for_principal(Principal.admin()).dispatch("list_meters", {}))
    assert len(out["meters"]) == 3
    assert "scope" not in out


def test_get_meter_refuses_another_consumers_meter(tools):
    scoped = tools.for_principal(Principal.consumer("maria", [M2]))
    out = json.loads(scoped.dispatch("get_meter", {"meter_id": M1}))
    assert out["error"] == "access denied"
    # Crucially: not the value, and not a hint that M1 exists with data.
    assert "111.5" not in json.dumps(out)
    assert "values" not in out


def test_get_meter_returns_own_meter(tools):
    scoped = tools.for_principal(Principal.consumer("maria", [M2]))
    out = json.loads(scoped.dispatch("get_meter", {"meter_id": M2}))
    assert out["meter_id"] == M2
    assert any("222.5" in json.dumps(v) for v in out["values"])


def test_compare_rejects_the_whole_request_if_one_meter_is_foreign(tools):
    """Silently dropping the foreign id would leak that it exists."""
    scoped = tools.for_principal(Principal.consumer("maria", [M2]))
    out = json.loads(scoped.dispatch("compare_meters", {"meter_ids": [M2, M1]}))
    assert out["error"] == "access denied"
    assert "222.5" not in json.dumps(out)
    assert "111.5" not in json.dumps(out)


def test_consumer_may_compare_two_meters_it_owns(tools):
    """A multi-meter account comparing within its own allow-list is fine; the
    rejection above is specifically about a foreign id."""
    scoped = tools.for_principal(Principal.consumer("landlord", [M1, M2]))
    out = json.loads(scoped.dispatch("compare_meters", {"meter_ids": [M1, M2]}))
    assert "error" not in out
    assert set(out["meters"]) == {M1, M2}


def test_for_principal_does_not_mutate_the_shared_instance(tools):
    tools.for_principal(Principal.consumer("maria", [M2])).dispatch("list_meters", {})
    # The original must still be unrestricted: concurrent requests share it.
    out = json.loads(tools.dispatch("list_meters", {}))
    assert len(out["meters"]) == 3


def test_default_tools_stay_privileged(tools):
    """Agent-level callers with no principal keep the pre-RBAC behaviour."""
    assert len(json.loads(tools.dispatch("list_meters", {}))["meters"]) == 3


# ── end-to-end: what actually reaches Bedrock ──────────────────────────
class _RecordingClient:
    """Fake Bedrock client that records every prompt it is handed.

    Mirrors the real Converse response shape (``output.message.content``) so the
    agent parses it the same way it parses an AWS reply.
    """

    def __init__(self, texts):
        self._texts = list(texts)
        self.prompts: list[list[dict]] = []
        self.systems: list[str] = []

    async def converse(self, messages, system=None, tools=None):
        # Copy: the agent keeps mutating the same list across hops.
        self.prompts.append([dict(m) for m in messages])
        self.systems.append(system or "")
        text = self._texts.pop(0) if self._texts else "done"
        return {
            "output": {"message": {"role": "assistant", "content": [{"text": text}]}},
            "usage": {"totalTokens": 1},
        }

    def status(self):
        return {"enabled": True}


def test_foreign_meter_never_reaches_bedrock(tools):
    """The end-to-end guarantee.

    The model asks for a meter the consumer does not own. Whatever the model
    says next, M1's value must not appear anywhere in the Bedrock payload.
    """
    client = _RecordingClient(["I cannot access that meter.", "It is not in your account."])
    assistant = Assistant(client, tools)
    principal = Principal.consumer("maria", [M2])

    result = asyncio.run(
        assistant.ask(
            "What is the totalizer on meter " + M1 + "?",
            tools=tools.for_principal(principal),
            principal=principal,
        )
    )

    # The question itself echoes M1 back, so only the tool results are checked:
    # that is where a real reading would appear.
    tool_text = json.dumps(
        [
            block
            for turn in client.prompts
            for message in turn
            for block in message.get("content", [])
            if isinstance(block, dict) and "toolResult" in block
        ]
    )
    assert "111.5" not in tool_text
    assert M1 not in tool_text
    # And the value must never have been handed to a tool call either.
    assert json.dumps(result["tool_calls"]) == "[]"
    assert result["answer"]


def test_own_meter_value_does_reach_bedrock(tools):
    """Sanity check on the test above: scoping must not simply blank the tools.

    Without this, a bug that emptied every consumer's tool output would still
    pass the leak test.
    """
    principal = Principal.consumer("maria", [M2])
    scoped = tools.for_principal(principal)

    # list_meters carries counts, not values.
    assert "222.5" not in scoped.dispatch("list_meters", {})
    assert "222.5" in scoped.dispatch("get_meter", {"meter_id": M2})


def test_system_prompt_names_the_scope_for_a_consumer(tools):
    client = _RecordingClient(["ok"])
    principal = Principal.consumer("maria", [M2])
    asyncio.run(
        Assistant(client, tools).ask("hi", tools=tools.for_principal(principal), principal=principal)
    )
    system = client.systems[0]
    assert M2 in system
    assert "not available" in system.lower()


def test_admin_prompt_has_no_consumer_scope(tools):
    client = _RecordingClient(["ok"])
    asyncio.run(Assistant(client, tools).ask("hi", tools=tools.for_principal(Principal.admin())))
    assert "may only access" not in client.systems[0]


# ── findings scoping ────────────────────────────────────────────────────
def test_findings_are_filtered_to_the_consumers_meters():
    rows = [
        {"rule": "signal_weak", "meter_id": M1},
        {"rule": "meter_offline", "meter_id": M2},
        {"rule": "alarm_active", "meter_id": M3},
    ]
    out = Principal.consumer("maria", [M2]).filter_findings(rows)
    assert [r["meter_id"] for r in out] == [M2]


def test_admin_sees_all_findings():
    rows = [{"meter_id": M1}, {"meter_id": M2}]
    assert len(Principal.admin().filter_findings(rows)) == 2


def test_filter_findings_ignores_junk_rows():
    assert Principal.admin().filter_findings([None, "x", 5, {"meter_id": M1}]) == [{"meter_id": M1}]
