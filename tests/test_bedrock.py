"""Unit tests for the Bedrock wrapper.

Every test is fully mocked: no AWS call is made and no credentials are needed,
so this suite runs offline even with ``AI_ENABLED=true`` in the environment.
"""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest

from gateway.ai.bedrock import (
    BedrockClient,
    BedrockError,
    CircuitBreaker,
    CircuitOpen,
    content_blocks,
    first_text,
    strip_thinking,
    tool_uses,
)
from gateway.config import Settings


# ─────────────────────────────────────────────────────────────────────────
# helpers
# ─────────────────────────────────────────────────────────────────────────
def _settings(**overrides) -> Settings:
    base = {
        "ai_enabled": True,
        "ai_bind_host": "127.0.0.1",
        "ai_bearer_token": "x" * 48,
        "aws_region": "ap-southeast-2",
        "bedrock_model_id": "amazon.nova-pro-v1:0",
        "aws_access_key_id": "AKIAIOSFODNN7EXAMPLE",
        "aws_secret_access_key": "wJalrXUtnFEMI/K7MDENG/bPxRfiCYEXAMPLEKEY",
        "_env_file": None,
    }
    base.update(overrides)
    return Settings(**base)


def _response(blocks: list[dict]) -> dict:
    return {"output": {"message": {"role": "assistant", "content": blocks}}}


class _FakeClient:
    """Stands in for ``boto3.client('bedrock-runtime')``."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls: list[dict] = []

    def converse(self, **kwargs):
        self.calls.append(kwargs)
        item = self._responses.pop(0) if self._responses else _response([{"text": "ok"}])
        if isinstance(item, Exception):
            raise item
        return item


# ─────────────────────────────────────────────────────────────────────────
# response-shape helpers
# ─────────────────────────────────────────────────────────────────────────
class TestResponseShapes:
    def test_first_text_finds_plain_text(self):
        assert first_text([{"text": "hello"}]) == "hello"

    def test_first_text_skips_leading_reasoning_block(self):
        """Regression: indexing content[0] breaks when reasoning comes first."""
        blocks = [
            {"reasoningContent": {"reasoningText": {"text": "thinking..."}}},
            {"text": "the answer"},
        ]
        assert first_text(blocks) == "the answer"

    def test_first_text_returns_none_for_tool_only_turn(self):
        blocks = [{"toolUse": {"toolUseId": "t1", "name": "list_meters", "input": {}}}]
        assert first_text(blocks) is None

    def test_first_text_handles_empty_and_none(self):
        assert first_text([]) is None
        assert first_text(None) is None

    # Regression: Nova inlines <think>...</think> inside the text block rather
    # than returning a separate reasoningContent block, so operators were being
    # shown the model's scratch work instead of its answer.
    def test_strip_thinking_removes_inline_tags(self):
        raw = "<think> The question is not related. </think>\n\nI cannot help."
        assert strip_thinking(raw) == "I cannot help."

    def test_strip_thinking_is_case_insensitive(self):
        assert strip_thinking("<THINK>x</THINK>done") == "done"

    # Regression: live /ai/ask replies came back tagged <thinking>, not <think>.
    # The pattern only matched the short form, so the model's scratch work was
    # shown to the operator verbatim.
    def test_strip_thinking_handles_the_long_tag(self):
        assert strip_thinking("<thinking>reasoning</thinking>\n\nAnswer.") == "Answer."

    def test_strip_thinking_handles_unterminated_long_tag(self):
        assert strip_thinking("<thinking>still reasoning") == ""

    def test_strip_thinking_handles_mismatched_tag_names(self):
        assert strip_thinking("<thinking>a</think>b") == "b"
        assert strip_thinking("<think>a</thinking>b") == "b"

    def test_strip_thinking_keeps_a_plain_answer_intact(self):
        assert strip_thinking("Meter A is highest.") == "Meter A is highest."

    def test_strip_thinking_spans_newlines(self):
        raw = "<think>\nline one\nline two\n</think>\nanswer"
        assert strip_thinking(raw) == "answer"

    def test_strip_thinking_drops_unterminated_tag(self):
        assert strip_thinking("<think>never closed") == ""

    def test_strip_thinking_drops_truncated_tail_after_answer(self):
        """A cut-off thought must not leak even when a real answer preceded it."""
        assert strip_thinking("Meter A is highest.<think>now let me rea") == "Meter A is highest."

    def test_strip_thinking_leaves_clean_text_alone(self):
        assert strip_thinking("Meter 123 has 45 m3.") == "Meter 123 has 45 m3."

    def test_first_text_strips_thinking_from_answer(self):
        blocks = [{"text": "<think>weighing options</think>\n\nMeter A is highest."}]
        assert first_text(blocks) == "Meter A is highest."

    def test_first_text_returns_none_when_block_is_only_thinking(self):
        blocks = [{"text": "<think>still deciding</think>"}]
        assert first_text(blocks) is None

    def test_tool_uses_collected_in_order(self):
        blocks = [
            {"text": "let me look"},
            {"toolUse": {"toolUseId": "a", "name": "list_meters", "input": {}}},
            {"toolUse": {"toolUseId": "b", "name": "get_meter", "input": {"meter_id": "WTR-001"}}},
        ]
        uses = tool_uses(blocks)
        assert [u["name"] for u in uses] == ["list_meters", "get_meter"]
        assert [u["toolUseId"] for u in uses] == ["a", "b"]

    def test_tool_uses_empty_when_absent(self):
        assert tool_uses([{"text": "hi"}]) == []

    def test_content_blocks_is_defensive(self):
        assert content_blocks({}) == []
        assert content_blocks({"output": {}}) == []
        assert content_blocks({"output": {"message": {"content": "nope"}}}) == []


# ─────────────────────────────────────────────────────────────────────────
# circuit breaker
# ─────────────────────────────────────────────────────────────────────────
class TestCircuitBreaker:
    def test_allows_until_threshold(self):
        breaker = CircuitBreaker(threshold=3)
        for _ in range(3):
            assert breaker.allow() is True
            breaker.record_failure()
        assert breaker.allow() is False
        assert breaker.is_open is True

    def test_success_resets_counter(self):
        breaker = CircuitBreaker(threshold=3)
        breaker.record_failure()
        breaker.record_failure()
        breaker.record_success()
        breaker.record_failure()
        assert breaker.is_open is False

    def test_half_open_allows_exactly_one_probe(self):
        breaker = CircuitBreaker(threshold=1, reset_seconds=0.0)
        breaker.record_failure()
        assert breaker.allow() is True  # first probe
        assert breaker.allow() is False  # no second probe while it is in flight
        breaker.record_success()
        assert breaker.allow() is True

    def test_failed_probe_reopens(self):
        breaker = CircuitBreaker(threshold=1, reset_seconds=0.0)
        breaker.record_failure()
        assert breaker.allow() is True
        breaker.record_failure()
        assert breaker.is_open is True


# ─────────────────────────────────────────────────────────────────────────
# client
# ─────────────────────────────────────────────────────────────────────────
class TestBedrockClient:
    def test_credentials_and_region_passed_explicitly(self, monkeypatch):
        """pydantic does not export .env to os.environ, so keys must be explicit."""
        settings = _settings()
        fake = _FakeClient([_response([{"text": "pong"}])])
        captured = {}

        def fake_boto3_client(**kwargs):
            captured.update(kwargs)
            return fake

        import boto3

        monkeypatch.setattr(boto3, "client", fake_boto3_client)
        client = BedrockClient(settings)
        asyncio.run(client.converse([{"role": "user", "content": [{"text": "ping"}]}]))

        assert captured["region_name"] == "ap-southeast-2"
        assert captured["aws_access_key_id"] == settings.aws_access_key_id
        assert captured["aws_secret_access_key"] == settings.aws_secret_access_key
        assert captured["service_name"] == "bedrock-runtime"

    def test_tool_path_uses_greedy_temperature_and_headroom(self):
        settings = _settings()
        fake = _FakeClient([_response([{"text": "done"}])])
        client = BedrockClient(settings)
        client._client = fake

        asyncio.run(
            client.converse(
                [{"role": "user", "content": [{"text": "hi"}]}],
                system="be brief",
                tools=[{"name": "list_meters", "description": "d", "inputSchema": {"type": "object", "properties": {}}}],
            )
        )
        cfg = fake.calls[0]["inferenceConfig"]
        assert cfg["temperature"] == 0.0
        assert cfg["maxTokens"] == 2048
        assert fake.calls[0]["modelId"] == "amazon.nova-pro-v1:0"
        assert fake.calls[0]["toolConfig"]["toolChoice"] == {"auto": {}}

    def test_chat_path_defaults(self):
        fake = _FakeClient([_response([{"text": "hi"}])])
        client = BedrockClient(_settings())
        client._client = fake
        asyncio.run(client.converse([{"role": "user", "content": [{"text": "hi"}]}]))
        cfg = fake.calls[0]["inferenceConfig"]
        assert cfg["maxTokens"] == 1024
        assert "toolConfig" not in fake.calls[0]

    def test_model_error_becomes_bedrock_error(self):
        fake = _FakeClient([RuntimeError("boom")])
        client = BedrockClient(_settings())
        client._client = fake
        with pytest.raises(BedrockError):
            asyncio.run(client.converse([{"role": "user", "content": [{"text": "x"}]}]))

    def test_credentials_are_redacted_from_errors(self):
        """Both the configured key id and any other AKIA-shaped id are masked."""
        settings = _settings()
        other = "AKIAZZZZZZZZZZZZZZZZ"
        fake = _FakeClient([RuntimeError(f"denied for AKIAIOSFODNN7EXAMPLE and {other}")])
        client = BedrockClient(settings)
        client._client = fake
        with pytest.raises(BedrockError) as excinfo:
            asyncio.run(client.converse([{"role": "user", "content": [{"text": "x"}]}]))
        text = str(excinfo.value)
        assert "AKIAIOSFODNN7EXAMPLE" not in text
        assert other not in text
        assert "AKIA***" in text
        assert "***" in text

    def test_secret_is_redacted_from_error_text(self):
        settings = _settings()
        secret = settings.aws_secret_access_key
        fake = _FakeClient([RuntimeError(f"boom {secret}")])
        client = BedrockClient(settings)
        client._client = fake
        with pytest.raises(BedrockError) as excinfo:
            asyncio.run(client.converse([{"role": "user", "content": [{"text": "x"}]}]))
        assert secret not in str(excinfo.value)

    def test_timeout_becomes_bedrock_error(self):
        def slow(**kwargs):
            time.sleep(0.5)
            return _response([{"text": "late"}])

        client = BedrockClient(_settings(ai_timeout_seconds=0.05))
        client._client = SimpleNamespace(converse=slow)
        with pytest.raises(BedrockError):
            asyncio.run(client.converse([{"role": "user", "content": [{"text": "x"}]}]))

    def test_repeated_failures_open_the_circuit(self):
        fake = _FakeClient([RuntimeError("boom") for _ in range(5)])
        client = BedrockClient(_settings())
        client._client = fake
        msg = [{"role": "user", "content": [{"text": "x"}]}]
        for _ in range(5):
            with pytest.raises(BedrockError):
                asyncio.run(client.converse(msg))
        assert client.breaker.is_open is True
        with pytest.raises(CircuitOpen):
            asyncio.run(client.converse(msg))

    def test_success_after_failures_closes_circuit(self):
        fake = _FakeClient([RuntimeError("boom"), _response([{"text": "ok"}])])
        client = BedrockClient(_settings())
        client._client = fake
        msg = [{"role": "user", "content": [{"text": "x"}]}]
        with pytest.raises(BedrockError):
            asyncio.run(client.converse(msg))
        result = asyncio.run(client.converse(msg))
        assert first_text(content_blocks(result)) == "ok"
        assert client.breaker.is_open is False

    def test_missing_credentials_refuse_to_build_client(self):
        client = BedrockClient(_settings(aws_access_key_id=""))
        with pytest.raises(BedrockError):
            asyncio.run(client.converse([{"role": "user", "content": [{"text": "x"}]}]))

    def test_ping_reports_reachability(self):
        client = BedrockClient(_settings())
        client._client = _FakeClient([_response([{"text": "pong"}])])
        assert asyncio.run(client.ping())["ok"] is True

    def test_ping_reports_failure_without_raising(self):
        client = BedrockClient(_settings())
        client._client = _FakeClient([RuntimeError("nope")])
        result = asyncio.run(client.ping())
        assert result["ok"] is False
        assert "nope" in result["error"]

    def test_status_never_exposes_credentials(self):
        settings = _settings()
        client = BedrockClient(settings)
        status = client.status()
        blob = repr(status)
        assert settings.aws_access_key_id not in blob
        assert settings.aws_secret_access_key not in blob
        assert settings.ai_bearer_token not in blob
        assert status["model_id"] == "amazon.nova-pro-v1:0"
        assert status["region"] == "ap-southeast-2"
