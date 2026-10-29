"""Tests for ``GET /ai/analyze``: findings in, ranked recommendations out.

The endpoint is GET-only and side-effect free: nothing is written and the model
is consulted only when the underlying facts actually change. These tests drive
:meth:`AIServer._route` directly - no socket is opened, but auth, scoping,
routing and status codes are all real.
"""

from __future__ import annotations

import asyncio
import json

import pytest

from gateway.ai.roles import Principal, PrincipalEntry, StaticPrincipalResolver
from gateway.ai.tools import MeterTools
from gateway.ai_server import AIServer
from gateway.config import BasePoint, BasePointsConfig, MeterConfig, MetersConfig
from gateway.store import MySQLBuffer

ADMIN_TOKEN = "a" * 40
USER_TOKEN = "u" * 40


class _FakeClient:
    """Bedrock stand-in that counts calls and returns one text block."""

    def __init__(self, text: str = "Check the meter.") -> None:
        self.text = text
        self.calls = 0

    async def converse(self, messages, *, system=None, **kwargs) -> dict:  # noqa: ARG002
        self.calls += 1
        return {
            "output": {
                "message": {
                    "content": [{"text": self.text}],
                }
            }
        }

    def status(self) -> dict:
        return {}


class _Buffer:
    def __init__(self, findings) -> None:
        self._findings = findings

    def open_findings(self):
        return list(self._findings)


class _SummaryWriter:
    def __init__(self, findings) -> None:
        self.buffer = _Buffer(findings)


class _Writer:
    def __init__(self) -> None:
        self.data = b""

    def write(self, data: bytes) -> None:
        self.data += data

    async def drain(self) -> None:
        return None


def _response(writer: _Writer) -> tuple[int, dict]:
    head, _, body = writer.data.partition(b"\r\n\r\n")
    status = int(head.split(b" ")[1])
    return status, json.loads(body.decode("utf-8"))


def _call(server: AIServer, token: str, path: str, query: str = "") -> tuple[int, dict]:
    writer = _Writer()
    principal = server.resolver.resolve(token)
    asyncio.run(server._route(writer, "GET", path, b"", principal, query))
    return _response(writer)


FINDINGS_WTR001 = [
    {
        "meter_id": "WTR-001",
        "rule": "out_of_range",
        "message": "battery 11.0 V below 12.0 V",
        "severity": "critical",
        "source": "anomaly",
    },
    {
        "meter_id": "WTR-001",
        "rule": "stale",
        "message": "no capture for 90 minutes",
        "severity": "high",
        "source": "anomaly",
    },
]

FINDINGS_WTR002 = [
    {
        "meter_id": "WTR-002",
        "rule": "offline",
        "message": "meter not contacting gateway",
        "severity": "critical",
        "source": "anomaly",
    },
]


@pytest.fixture()
def buffer(buffer_dsn):
    buf = MySQLBuffer(**buffer_dsn)
    buf.initialize()
    yield buf
    buf.close()


@pytest.fixture()
def meters():
    return MetersConfig(
        meters={
            "WTR-001": MeterConfig(meter_id="WTR-001", modbus_unit_id=1),
            "WTR-002": MeterConfig(meter_id="WTR-002", modbus_unit_id=2),
        }
    )


@pytest.fixture()
def base_points():
    return BasePointsConfig(
        points={
            "totalizer": BasePoint(id="totalizer", description="Total water", unit="m3"),
            "pressure": BasePoint(id="pressure", description="Line pressure", unit="kg/cm2"),
        }
    )


@pytest.fixture()
def tools(buffer, meters, base_points):
    buffer.upsert_normalized("WTR-001", "totalizer", 100.0, "m3")
    buffer.upsert_normalized("WTR-001", "pressure", 2.5, "kg/cm2")
    buffer.upsert_normalized("WTR-002", "totalizer", 250.0, "m3")
    return MeterTools(buffer, meters, base_points)


@pytest.fixture()
def resolver():
    return StaticPrincipalResolver(
        entries=(
            PrincipalEntry(token=ADMIN_TOKEN, principal=Principal.admin("ops")),
            PrincipalEntry(
                token=USER_TOKEN, principal=Principal.consumer("maria", ("WTR-001",))
            ),
        )
    )


@pytest.fixture()
def summary_writer():
    return _SummaryWriter([])


def _server(summary_writer, tools, resolver, client=None):
    client = client or _FakeClient()
    return AIServer(
        "127.0.0.1",
        0,
        client,
        tools,
        None,
        "x" * 40,
        rate_limit_per_min=10,
        summary_writer=summary_writer,
        resolver=resolver,
        obix_store=None,
    )


class TestAnalyzeBasics:
    def test_site_wide_analysis_returns_recommendation(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        server.analyzer.client.calls = 0
        summary_writer.buffer._findings = list(FINDINGS_WTR001 + FINDINGS_WTR002)

        status, body = _call(server, ADMIN_TOKEN, "/ai/analyze")

        assert status == 200
        assert body["recommendation"] == server.analyzer.client.text
        assert body["aws_call"] is True
        assert body["cached"] is False
        assert body["findings_analyzed"] == 3
        assert body["scope"] == {"scope": "site"}
        assert server.analyzer.client.calls == 1

    def test_repeat_get_is_cached_and_spends_nothing(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        server.analyzer.client.calls = 0
        summary_writer.buffer._findings = list(FINDINGS_WTR001)

        _call(server, ADMIN_TOKEN, "/ai/analyze")
        assert server.analyzer.client.calls == 1

        status2, body2 = _call(server, ADMIN_TOKEN, "/ai/analyze")
        assert status2 == 200
        assert body2["cached"] is True
        # Same facts -> the model was not consulted again.
        assert server.analyzer.client.calls == 1

    def test_changed_facts_trigger_new_call(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        summary_writer.buffer._findings = list(FINDINGS_WTR001)

        _call(server, ADMIN_TOKEN, "/ai/analyze")
        calls_after_first = server.analyzer.client.calls

        summary_writer.buffer._findings = list(FINDINGS_WTR001 + FINDINGS_WTR002)
        status, body = _call(server, ADMIN_TOKEN, "/ai/analyze")

        assert status == 200
        assert body["cached"] is False
        assert body["findings_analyzed"] == 3
        assert server.analyzer.client.calls == calls_after_first + 1

    def test_empty_findings_means_no_aws_call(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        summary_writer.buffer._findings = []

        status, body = _call(server, ADMIN_TOKEN, "/ai/analyze")

        assert status == 200
        assert body["recommendation"] == "No open findings. Nothing needs attention."
        assert body["aws_call"] is False
        assert body["findings_analyzed"] == 0
        assert server.analyzer.client.calls == 0


class TestAnalyzeScoping:
    def test_consumer_sees_only_own_meter_findings(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        summary_writer.buffer._findings = list(FINDINGS_WTR001 + FINDINGS_WTR002)

        status, body = _call(server, USER_TOKEN, "/ai/analyze")

        assert status == 200
        assert body["findings_analyzed"] == 2
        assert body["scope"] == {"scope": "your meters", "meters": ["WTR-001"]}
        assert server.analyzer.client.calls == 1

    def test_out_of_scope_meter_is_403_without_aws_call(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        summary_writer.buffer._findings = list(FINDINGS_WTR001)

        status, body = _call(server, USER_TOKEN, "/ai/analyze", "meter_id=WTR-002")

        assert status == 403
        assert body["error"] == "access denied"
        assert body["meter_id"] == "WTR-002"
        assert server.analyzer.client.calls == 0

    def test_in_scope_meter_analyzed_alone(self, summary_writer, tools, resolver):
        server = _server(summary_writer, tools, resolver)
        summary_writer.buffer._findings = list(FINDINGS_WTR001 + FINDINGS_WTR002)

        status, body = _call(server, USER_TOKEN, "/ai/analyze", "meter_id=WTR-001")

        assert status == 200
        assert body["findings_analyzed"] == 2
        assert body["scope"] == {"meter_id": "WTR-001"}
        assert server.analyzer.client.calls == 1
