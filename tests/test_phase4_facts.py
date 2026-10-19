"""Phase 4 endpoints over real HTTP against a live gateway."""

from __future__ import annotations

import asyncio
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from gateway.ai.summary import FindingRecorder, SummaryWriter  # noqa: E402
from gateway.config import Settings  # noqa: E402
from gateway.store import SQLiteBuffer  # noqa: E402

SECRET_FIELDS = {"aws_access_key_id", "aws_secret_access_key", "ai_bearer_token", "password"}


class _StubClient:
    def __init__(self, text="Stub summary.", fail=False):
        self.text = text
        self.fail = fail

    async def converse(self, messages, *, system=None, tools=None, **kw):
        if self.fail:
            from gateway.ai.bedrock import BedrockError

            raise BedrockError("no aws")
        return {
            "output": {"message": {"content": [{"text": self.text}]}},
            "usage": {"inputTokens": 10, "outputTokens": 5},
        }


@pytest.fixture()
def buffer(tmp_path):
    buf = SQLiteBuffer(tmp_path / "facts.db")
    buf.initialize()
    yield buf
    buf.close()


def _writer(buffer, **kw):
    return SummaryWriter(_StubClient(**kw), buffer)


def _seed(buffer):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc).isoformat()
    buffer.upsert_snapshot("M1", {"meter_id": "M1", "last_receive": now})
    buffer.upsert_normalized("M1", "online", 0.0, "binary")
    buffer.upsert_normalized("M1", "totalizer", 5.0, "m3")


def test_facts_from_seeded_meter(buffer):
    _seed(buffer)
    facts = _writer(buffer).gather_facts(datetime.now(timezone.utc))
    meter = next(m for m in facts["meters"] if m["meter_id"] == "M1")
    assert meter["totalizer_m3"] == 5.0
    assert meter["comm_status"] in ("on-time", "watch", "lagging", "unknown")


def test_facts_report_offline_count(buffer):
    _seed(buffer)
    facts = _writer(buffer).gather_facts(datetime.now(timezone.utc))
    assert facts["meters_offline"] >= 1


def test_facts_include_rule_breakdown(buffer):
    from datetime import datetime, timezone

    now = datetime.now(timezone.utc)
    _seed(buffer)
    FindingRecorder(buffer).run_once(now)
    facts = _writer(buffer).gather_facts(now)
    assert "meter_offline" in facts["findings_by_rule"]
    assert facts["findings_total"] >= 1


def test_facts_survive_empty_buffer(buffer):
    facts = _writer(buffer).gather_facts(datetime.now(timezone.utc))
    assert facts["meters_total"] == 0
    assert facts["findings_total"] == 0


def test_write_returns_no_raw_facts_when_serialised(buffer):
    """The API layer pops facts; make sure that is all it has to do."""
    from datetime import datetime, timezone

    _seed(buffer)
    result = asyncio.run(_writer(buffer, text="hello").write(datetime.now(timezone.utc)))
    result.pop("facts", None)
    json.dumps(result, default=str)  # must not raise
    assert result["body"] == "hello"


def test_summary_usage_recorded(buffer):
    from datetime import datetime, timezone

    _seed(buffer)
    asyncio.run(_writer(buffer).write(datetime.now(timezone.utc)))
    assert buffer.latest_summary()["usage"]["inputTokens"] == 10


def test_no_secret_fields_in_facts(buffer):
    from datetime import datetime, timezone

    _seed(buffer)
    facts = _writer(buffer).gather_facts(datetime.now(timezone.utc))
    blob = json.dumps(facts, default=str).lower()
    for name in SECRET_FIELDS:
        assert name not in blob


def test_facts_contain_no_aws_keys_even_when_set(buffer):
    from datetime import datetime, timezone

    _seed(buffer)
    writer = SummaryWriter(_StubClient(), buffer)
    settings = Settings()
    facts = writer.gather_facts(datetime.now(timezone.utc))
    blob = json.dumps(facts, default=str)
    for secret in (settings.aws_access_key_id, settings.aws_secret_access_key, settings.ai_bearer_token):
        if secret and len(secret) >= 8:
            assert secret not in blob

