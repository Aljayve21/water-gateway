"""Tests for the read-only AI tools and the agent's tool-calling loop.

All model traffic is mocked; the SQLite buffer used here is a real temporary
database so the tools are exercised against genuine SQL.
"""

from __future__ import annotations

import json

import pytest

from gateway.ai.agent import Assistant, SYSTEM_PROMPT
from gateway.ai.bedrock import BedrockClient, BedrockError, content_blocks, first_text
from gateway.ai.tools import MeterTools
from gateway.config import (
    BasePoint,
    BasePointsConfig,
    MetersConfig,
    MeterConfig,
    Settings,
)
from gateway.store import SQLiteBuffer

MESSAGES = [{"role": "user", "content": [{"text": "hi"}]}]


@pytest.fixture()
def buffer(tmp_path):
    buf = SQLiteBuffer(tmp_path / "buffer.db")
    buf.initialize()
    yield buf
    buf.close()


@pytest.fixture()
def base_points():
    return BasePointsConfig(
        points={
            "totalizer": BasePoint(id="totalizer", description="Total water consumed", unit="m3"),
            "pressure": BasePoint(id="pressure", description="Line pressure", unit="kg/cm2"),
            "online": BasePoint(id="online", description="Meter online flag", unit="binary"),
        }
    )


@pytest.fixture()
def meters():
    return MetersConfig(
        meters={
            "WTR-001": MeterConfig(meter_id="WTR-001", modbus_unit_id=1),
            "WTR-002": MeterConfig(meter_id="WTR-002", modbus_unit_id=2),
        }
    )


@pytest.fixture()
def tools(buffer, meters, base_points):
    for meter_id, total in (("WTR-001", 100.0), ("WTR-002", 250.0)):
        buffer.upsert_normalized(meter_id, "totalizer", total, "m3")
        buffer.upsert_normalized(meter_id, "pressure", 2.5, "kg/cm2")
        buffer.upsert_normalized(meter_id, "online", 1, "binary")
    return MeterTools(buffer, meters, base_points)


def _payload(calls):
    return json.loads(calls[0])


class TestToolSchemas:
    def test_three_tools_are_exposed(self, tools):
        assert [s["name"] for s in tools.specs] == ["list_meters", "get_meter", "compare_meters"]

    def test_schemas_use_the_json_tagged_union(self, tools):
        """Regression: Converse rejects a bare inputSchema; it needs .json."""
        for spec in tools.specs:
            assert set(spec["inputSchema"]) == {"json"}
            schema = spec["inputSchema"]["json"]
            assert schema["type"] == "object"
            # Nova allows only these three keys inside the JSON Schema.
            assert set(schema) <= {"type", "properties", "required"}

    def test_schemas_declare_no_unsupported_keys(self, tools):
        """$schema/description/title/additionalProperties break Nova tool use."""
        banned = {"$schema", "title", "additionalProperties"}
        for spec in tools.specs:
            assert not (set(spec["inputSchema"]["json"]) & banned)

    def test_dispatch_rejects_unknown_tool(self, tools):
        out = json.loads(tools.dispatch("drop_tables", {}))
        assert "unknown tool" in out["error"]


class TestListMeters:
    def test_lists_every_meter_with_status(self, tools):
        out = _payload([tools.dispatch("list_meters", {})])
        assert out["count"] == 2
        ids = sorted(m["meter_id"] for m in out["meters"])
        assert ids == ["WTR-001", "WTR-002"]
        assert out["meters"][0]["points_with_values"] == 3

    def test_empty_buffer_yields_empty_list(self, buffer, meters, base_points):
        out = _payload([MeterTools(buffer, meters, base_points).dispatch("list_meters", {})])
        assert out == {"meters": [], "count": 0}

    def test_missing_snapshot_reports_unknown_not_crash(self, tools):
        out = _payload([tools.dispatch("get_meter", {"meter_id": "GHOST"})])
        assert out["comm_status"] == "unknown"
        assert out["values"] == []


class TestGetMeter:
    def test_returns_values_units_and_descriptions(self, tools):
        out = _payload([tools.dispatch("get_meter", {"meter_id": "WTR-001"})])
        assert out["meter_id"] == "WTR-001"
        by_id = {v["point_id"]: v for v in out["values"]}
        assert by_id["totalizer"]["value"] == 100.0
        assert by_id["totalizer"]["unit"] == "m3"
        assert by_id["totalizer"]["description"] == "Total water consumed"

    def test_missing_meter_id_is_an_error_payload(self, tools):
        out = _payload([tools.dispatch("get_meter", {})])
        assert "meter_id is required" in out["error"]


class TestCompareMeters:
    def test_side_by_side_values(self, tools):
        out = _payload([tools.dispatch("compare_meters", {"meter_ids": ["WTR-001", "WTR-002"]})])
        assert out["meters"] == ["WTR-001", "WTR-002"]
        rows = {r["point_id"]: r for r in out["comparison"]}
        assert rows["totalizer"]["values"] == {"WTR-001": 100.0, "WTR-002": 250.0}

    def test_accepts_a_bare_string(self, tools):
        out = _payload([tools.dispatch("compare_meters", {"meter_ids": "WTR-001"})])
        assert "at least two" in out["error"]

    def test_requires_two_meters(self, tools):
        out = _payload([tools.dispatch("compare_meters", {"meter_ids": ["WTR-001"]})])
        assert "at least two" in out["error"]

    def test_result_is_capped(self, tools):
        ids = [f"WTR-{i:03d}" for i in range(1, 40)]
        out = _payload([tools.dispatch("compare_meters", {"meter_ids": ids})])
        assert len(out["meters"]) <= 12


class TestToolResultIsClipped:
    def test_huge_result_is_truncated(self, tools):
        for i in range(4000):
            tools.buffer.upsert_normalized("WTR-001", f"p{i}", float(i), "u")
        raw = tools.dispatch("get_meter", {"meter_id": "WTR-001"})
        assert len(raw) <= 6100
        assert "truncated" in raw


class FakeBedrock:
    """Replays canned Converse responses and records the turns it was given."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.seen: list[dict] = []

    async def converse(self, messages, **kwargs):
        # Copy: the agent keeps mutating the same list across hops.
        self.seen.append({"messages": [dict(m) for m in messages], **kwargs})
        return self._responses.pop(0)

    @property
    def turn_count(self):
        return len(self.seen)


def _resp(blocks):
    return {"output": {"message": {"role": "assistant", "content": blocks}}}


def _use(name, args=None, tid="t1"):
    return {"toolUse": {"toolUseId": tid, "name": name, "input": args or {}}}


class TestAssistantLoop:
    def test_direct_answer_without_tools(self, tools):
        fake = FakeBedrock([_resp([{"text": "Hello."}])])
        result = asyncio_run(Assistant(fake, tools).ask("hi"))
        assert result["answer"] == "Hello."
        assert result["tool_calls"] == []
        assert result["hops"] == 0

    def test_tool_is_called_then_answer_returned(self, tools):
        fake = FakeBedrock(
            [
                _resp([{"reasoningContent": {"reasoningText": {"text": "check"}}}, _use("list_meters")]),
                _resp([{"text": "You have 2 meters."}]),
            ]
        )
        result = asyncio_run(Assistant(fake, tools).ask("how many meters?"))
        assert result["answer"] == "You have 2 meters."
        assert result["tool_calls"] == [{"tool": "list_meters", "input": {}}]
        assert result["hops"] == 1

    def test_tool_result_is_sent_as_user_turn(self, tools):
        fake = FakeBedrock(
            [_resp([_use("get_meter", {"meter_id": "WTR-001"})]), _resp([{"text": "100 m3"}])]
        )
        asyncio_run(Assistant(fake, tools).ask("WTR-001 totalizer?"))
        second = fake.seen[1]["messages"]
        last = second[-1]
        assert last["role"] == "user"
        payload = last["content"][0]["toolResult"]
        assert payload["toolUseId"] == "t1"
        assert "WTR-001" in payload["content"][0]["text"]

    def test_system_prompt_is_always_supplied(self, tools):
        fake = FakeBedrock([_resp([{"text": "ok"}])])
        asyncio_run(Assistant(fake, tools).ask("hi"))
        assert fake.seen[0]["system"] == SYSTEM_PROMPT

    def test_hop_limit_forces_a_final_tool_free_answer(self, tools):
        """A model that always asks for a tool still gets stopped."""
        # max_hops=2 -> the loop runs 3 turns (0,1,2); turn 2 gets no tools,
        # then the post-loop fallback produces the answer.
        fake = FakeBedrock([_resp([_use("list_meters", tid=str(i))]) for i in range(3)])
        fake._responses.append(_resp([{"text": "Giving up."}]))
        result = asyncio_run(Assistant(fake, tools, max_hops=2).ask("loop please"))
        assert result["answer"] == "Giving up."
        assert result["truncated"] is True
        assert result["hops"] == 2
        # The last in-loop turn must not have offered tools.
        assert fake.seen[2]["tools"] is None
        # The fallback turn must not offer tools either.
        assert "tools" not in fake.seen[-1]

    def test_answer_comes_back_inside_the_budget(self, tools):
        """Ordinary case: tool on turn 0, answer on turn 1, never truncated."""
        fake = FakeBedrock([_resp([_use("list_meters")]), _resp([{"text": "Two meters."}])])
        result = asyncio_run(Assistant(fake, tools, max_hops=5).ask("how many?"))
        assert result["truncated"] is False
        assert result["answer"] == "Two meters."
        assert fake.turn_count == 2

    def test_no_text_and_no_tool_is_an_error(self, tools):
        fake = FakeBedrock([_resp([{"reasoningContent": {"reasoningText": {"text": "hm"}}}])])
        with pytest.raises(BedrockError):
            asyncio_run(Assistant(fake, tools).ask("hi"))

    def test_history_is_carried_in(self, tools):
        fake = FakeBedrock([_resp([{"text": "ok"}])])
        history = [{"role": "user", "content": [{"text": "earlier"}]}]
        asyncio_run(Assistant(fake, tools).ask("now", history))
        assert fake.seen[0]["messages"][0]["content"][0]["text"] == "earlier"

    def test_long_history_is_trimmed(self, tools):
        fake = FakeBedrock([_resp([{"text": "ok"}])])
        history = [{"role": "user", "content": [{"text": f"m{i}"}]} for i in range(50)]
        asyncio_run(Assistant(fake, tools).ask("now", history))
        # MAX_HISTORY_TURNS (12) plus the new question and the assistant reply.
        assert len(fake.seen[0]["messages"]) == 13
        assert fake.seen[0]["messages"][-1]["content"][0]["text"] == "now"

    def test_history_parser_keeps_only_valid_turns(self):
        parsed = Assistant.parse_history(
            json.dumps(
                [
                    {"role": "user", "text": "a"},
                    {"role": "bogus", "text": "b"},
                    {"role": "assistant", "text": ""},
                    {"nope": 1},
                ]
            )
        )
        assert parsed == [{"role": "user", "content": [{"text": "a"}]}]

    def test_history_parser_tolerates_garbage(self):
        assert Assistant.parse_history("not json") == []
        assert Assistant.parse_history(None) == []
        assert Assistant.parse_history('"a string"') == []


def asyncio_run(coro):
    import asyncio

    return asyncio.run(coro)
