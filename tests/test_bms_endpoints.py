"""Tests for the read-only ``/bms/*`` REST endpoints and their scoping.

The endpoint helpers read from the oBIX store when one is wired in and fall
back to the buffer otherwise, so both paths are exercised here. The HTTP layer
is driven directly through :meth:`AIServer._route` with a capturing writer -
no socket is opened, but auth, routing and status codes are all real.
"""

from __future__ import annotations

import asyncio
import json
import os

import pymysql
import pytest

from gateway.ai.roles import Principal, PrincipalEntry, StaticPrincipalResolver
from gateway.ai.tools import MeterTools
from gateway.ai_server import AIServer
from gateway.config import BasePoint, BasePointsConfig, MeterConfig, MetersConfig
from gateway.obix_store import ObixStore
from gateway.obix_validation import ObixRequirements, ObixValidator
from gateway.store import MySQLBuffer

ADMIN_TOKEN = "a" * 40
USER_TOKEN = "u" * 40


class _StubClient:
    def status(self) -> dict:
        return {}


class _StubAssistant:
    max_hops = 5


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
def tools(buffer, meters, base_points):
    buffer.upsert_normalized("WTR-001", "totalizer", 100.0, "m3")
    buffer.upsert_normalized("WTR-001", "pressure", 2.5, "kg/cm2")
    buffer.upsert_normalized("WTR-002", "totalizer", 250.0, "m3")
    return MeterTools(buffer, meters, base_points)


@pytest.fixture()
def server(tools, resolver):
    return AIServer(
        "127.0.0.1",
        0,
        _StubClient(),
        tools,
        _StubAssistant(),
        "x" * 40,
        rate_limit_per_min=10,
        summary_writer=None,
        resolver=resolver,
        obix_store=None,
    )


class TestBmsBufferFallback:
    def test_points_admin_sees_every_meter(self, server):
        status, body = _call(server, ADMIN_TOKEN, "/bms/points")
        assert status == 200
        assert body["count"] == 3
        assert body["meters"] == ["WTR-001", "WTR-002"]

    def test_points_consumer_is_scoped(self, server):
        status, body = _call(server, USER_TOKEN, "/bms/points")
        assert status == 200
        assert body["meters"] == ["WTR-001"]
        assert {p["meter_id"] for p in body["points"]} == {"WTR-001"}

    def test_points_for_one_meter(self, server):
        status, body = _call(server, ADMIN_TOKEN, "/bms/points/WTR-002")
        assert status == 200
        assert body["count"] == 1
        assert body["points"][0]["meter_id"] == "WTR-002"

    def test_cross_meter_read_is_forbidden(self, server):
        status, body = _call(server, USER_TOKEN, "/bms/points/WTR-002")
        assert status == 403
        assert body["error"] == "access denied"

    def test_devices_lists_meters(self, server):
        status, body = _call(server, ADMIN_TOKEN, "/bms/devices")
        assert status == 200
        assert {d["meter_id"] for d in body["devices"]} == {"WTR-001", "WTR-002"}

    def test_devices_are_scoped(self, server):
        status, body = _call(server, USER_TOKEN, "/bms/devices")
        assert status == 200
        assert [d["meter_id"] for d in body["devices"]] == ["WTR-001"]

    def test_alarms_endpoint_without_summary_writer(self, server):
        status, body = _call(server, ADMIN_TOKEN, "/bms/alarms")
        assert status == 200
        assert body == {"alarms": [], "count": 0}

    def test_history_requires_parameters(self, server):
        status, body = _call(server, ADMIN_TOKEN, "/bms/history")
        assert status == 400
        assert "meter_id" in body["error"]

    def test_history_denies_other_meters(self, server):
        status, body = _call(
            server, USER_TOKEN, "/bms/history", "meter_id=WTR-002&point_id=totalizer"
        )
        assert status == 403
        assert body["error"] == "access denied"

    def test_unknown_route_is_404(self, server):
        status, _body = _call(server, ADMIN_TOKEN, "/bms/nope")
        assert status == 404

    def test_requires_a_bearer_token(self, server):
        # The auth gate lives in _handle (before _route), so an anonymous
        # request never reaches the BMS helpers. Assert the gate itself.
        assert server._principal_for(None) is None
        assert server._principal_for("Bearer") is None
        assert server._principal_for("Bearer wrong-token") is None
        assert server._principal_for(f"Bearer {ADMIN_TOKEN}") is not None


def _obix_store() -> ObixStore:
    try:
        store = ObixStore(
            host=os.environ.get("BUFFER_MYSQL_HOST", "127.0.0.1"),
            port=int(os.environ.get("BUFFER_MYSQL_PORT", "3306")),
            user=os.environ.get("BUFFER_MYSQL_USER", "gateway_buf"),
            password=os.environ.get("BUFFER_MYSQL_PASSWORD", ""),
            database="obix_test",
        )
    except pymysql.err.OperationalError as exc:
        pytest.skip(f"oBIX test database unavailable: {exc}")
    store.initialize()
    return store


@pytest.fixture()
def obix(buffer_dsn):
    store = _obix_store()
    with store._conn.cursor() as cur:
        for table in (
            "obix_device",
            "obix_point_value",
            "obix_point_history",
            "obix_read_run",
            "obix_point",
        ):
            cur.execute(f"TRUNCATE TABLE {table}")
    yield store
    store.close()


@pytest.fixture()
def obix_server(tools, resolver, obix):
    return AIServer(
        "127.0.0.1",
        0,
        _StubClient(),
        tools,
        _StubAssistant(),
        "x" * 40,
        rate_limit_per_min=10,
        summary_writer=None,
        resolver=resolver,
        obix_store=obix,
    )


class TestBmsWithObixStore:
    def _seed(self, obix):
        obix.sync_device(
            "bms_enyecontrols",
            "/config/Drivers/BacnetNetwork/ENYECONTROLS/points",
            [
                {"rel_path": "zone1", "href": "z1", "is_folder": True, "depth": 0},
                {
                    "rel_path": "zone1/temp",
                    "href": "z1/temp",
                    "point_id": "temp",
                    "name": "temp",
                    "is_folder": False,
                    "depth": 1,
                },
                {
                    "rel_path": "zone1/flow",
                    "href": "z1/flow",
                    "point_id": "flow",
                    "name": "flow",
                    "unit": "m3",
                    "is_folder": False,
                    "depth": 1,
                },
            ],
            name="ENYECONTROLS",
        )
        obix.record_values(
            {("bms_enyecontrols", "temp"): (21.5, "degC"), ("bms_enyecontrols", "flow"): (3.2, "m3")},
            captured_utc="2026-10-15T01:00:00+00:00",
        )
        obix.record_values(
            {("bms_enyecontrols", "temp"): (22.5, "degC")},
            captured_utc="2026-10-15T02:00:00+00:00",
        )
        obix.record_run(
            "2026-10-15T02:00:00+00:00", "2026-10-15T02:00:05+00:00", 2, 0, 5000
        )

    def test_points_come_from_obix_store(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points")
        assert status == 200
        assert body["count"] == 2
        assert body["total"] == 2
        temp = next(p for p in body["points"] if p["point_id"] == "temp")
        assert temp["value"] == 22.5
        assert temp["unit"] == "degC"

    def test_points_pagination(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points", "limit=1&offset=0")
        assert status == 200
        assert body["count"] == 1
        assert body["total"] == 2
        first = body["points"][0]["point_id"]

        _status, page2 = _call(obix_server, ADMIN_TOKEN, "/bms/points", "limit=1&offset=1")
        assert page2["count"] == 1
        assert page2["points"][0]["point_id"] != first

    def test_points_for_one_device(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points/bms_enyecontrols")
        assert status == 200
        assert body["count"] == 2
        assert {p["point_id"] for p in body["points"]} == {"temp", "flow"}

    def test_point_detail(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points/bms_enyecontrols/temp")
        assert status == 200
        assert body["point_id"] == "temp"
        assert body["value"] == 22.5
        assert body["unit"] == "degC"

    def test_point_detail_exposes_requirements(self, tools, resolver, obix):
        self._seed(obix)  # temp unit degC
        server = self._server_with_requirements(
            tools,
            resolver,
            obix,
            ObixRequirements(
                points={
                    "bms_enyecontrols/temp": {"min": 15.0, "max": 35.0, "unit": "degC"}
                }
            ),
        )
        status, body = _call(server, ADMIN_TOKEN, "/bms/points/bms_enyecontrols/temp")
        assert status == 200
        assert body["requirements"] == {
            "unit": "degC",
            "min": 15.0,
            "max": 35.0,
            "tolerance": None,
            "allowed": None,
            "max_age_seconds": None,
        }

    def test_point_detail_includes_requirements_block(self, obix_server, obix):
        # With an oBIX store the server auto-builds a validator from
        # config/obix_requirements.yaml, so every point read carries the block
        # (possibly all-null when nothing is configured for that point).
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points/bms_enyecontrols/temp")
        assert status == 200
        assert set(body["requirements"]) == {
            "unit",
            "min",
            "max",
            "tolerance",
            "allowed",
            "max_age_seconds",
        }

    def test_points_list_exposes_requirements(self, tools, resolver, obix):
        self._seed(obix)
        server = self._server_with_requirements(
            tools,
            resolver,
            obix,
            ObixRequirements(points={"bms_enyecontrols/temp": {"min": 15.0, "max": 35.0}}),
        )
        status, body = _call(server, ADMIN_TOKEN, "/bms/points")
        assert status == 200
        temp = next(p for p in body["points"] if p["point_id"] == "temp")
        assert temp["requirements"]["min"] == 15.0
        assert temp["requirements"]["max"] == 35.0
        # A point with no configured band still carries the block, with nulls.
        flow = next(p for p in body["points"] if p["point_id"] == "flow")
        assert flow["requirements"]["min"] is None
        assert flow["requirements"]["max"] is None

    def test_missing_point_is_404(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/points/bms_enyecontrols/nope")
        assert status == 404
        assert body["error"] == "not found"

    def test_devices_come_from_obix_store(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/devices")
        assert status == 200
        assert body["devices"][0]["meter_id"] == "bms_enyecontrols"
        assert body["devices"][0]["point_count"] == 2

    def test_status_reports_freshness(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, ADMIN_TOKEN, "/bms/status")
        assert status == 200
        assert body["enabled"] is True
        assert body["last_run"]["points_ok"] == 2

    def test_status_forbidden_for_consumer(self, obix_server, obix):
        status, body = _call(obix_server, USER_TOKEN, "/bms/status")
        assert status == 403
        assert body["error"] == "forbidden"

    def test_history_returns_hourly_rollup(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(
            obix_server,
            ADMIN_TOKEN,
            "/bms/history",
            "meter_id=bms_enyecontrols&point_id=temp",
        )
        assert status == 200
        assert body["count"] == 2
        assert body["history"][-1]["last_value"] == 22.5

    def test_history_window(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(
            obix_server,
            ADMIN_TOKEN,
            "/bms/history",
            "meter_id=bms_enyecontrols&point_id=temp"
            "&since=2026-10-15T00:00:00+00:00&until=2026-10-15T01:30:00+00:00",
        )
        assert status == 200
        assert body["count"] == 1
        assert body["history"][0]["last_value"] == 21.5

    def test_history_limit(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(
            obix_server,
            ADMIN_TOKEN,
            "/bms/history",
            "meter_id=bms_enyecontrols&point_id=temp&limit=1",
        )
        assert status == 200
        assert body["count"] == 1

    def test_user_without_the_pseudo_meter_sees_nothing(self, obix_server, obix):
        self._seed(obix)
        status, body = _call(obix_server, USER_TOKEN, "/bms/points")
        assert status == 200
        assert body["count"] == 0

    def _server_with_requirements(self, tools, resolver, obix, requirements):
        return AIServer(
            "127.0.0.1",
            0,
            _StubClient(),
            tools,
            _StubAssistant(),
            "x" * 40,
            rate_limit_per_min=10,
            summary_writer=None,
            resolver=resolver,
            obix_store=obix,
            obix_validator=ObixValidator(requirements),
        )

    def test_alarms_include_obix_validation_findings(self, tools, resolver, obix):
        self._seed(obix)  # temp = 22.5
        server = self._server_with_requirements(
            tools,
            resolver,
            obix,
            ObixRequirements(
                points={"bms_enyecontrols/temp": {"min": 18.0, "max": 22.0}}
            ),
        )
        status, body = _call(server, ADMIN_TOKEN, "/bms/alarms")
        assert status == 200
        assert body["count"] == 1
        alarm = body["alarms"][0]
        assert alarm["source"] == "obix_validation"
        assert alarm["rule"] == "out_of_range"
        assert alarm["point_id"] == "temp"

    def test_alarms_obix_findings_respect_scope(self, tools, resolver, obix):
        self._seed(obix)
        server = self._server_with_requirements(
            tools,
            resolver,
            obix,
            ObixRequirements(
                points={"bms_enyecontrols/temp": {"min": 18.0, "max": 22.0}}
            ),
        )
        status, body = _call(server, USER_TOKEN, "/bms/alarms")
        assert status == 200
        assert body["count"] == 0


class TestBmsTools:
    def _tools(self, tools, obix):
        return MeterTools(
            tools.buffer,
            tools.meters,
            tools.base_points,
            obix_store=obix,
        ).for_principal(Principal.admin("ops"))

    def _seed(self, obix):
        obix.sync_device(
            "bms_enyecontrols",
            "/points",
            [
                {
                    "rel_path": "temp",
                    "href": "temp",
                    "point_id": "temp",
                    "name": "temp",
                    "is_folder": False,
                    "depth": 0,
                }
            ],
        )
        obix.record_values({("bms_enyecontrols", "temp"): (21.5, "degC")})

    def test_tools_are_only_advertised_with_a_store(self, tools):
        assert [s["name"] for s in tools.specs] == [
            "list_meters",
            "get_meter",
            "compare_meters",
        ]

    def test_list_bms_points_tool(self, tools, obix):
        self._seed(obix)
        bound = self._tools(tools, obix)
        assert "list_bms_points" in [s["name"] for s in bound.specs]
        payload = json.loads(bound.dispatch("list_bms_points", {"meter_id": "bms_enyecontrols"}))
        assert payload["count"] == 1
        assert payload["points"][0]["point_id"] == "temp"

    def test_get_bms_history_tool(self, tools, obix):
        self._seed(obix)
        bound = self._tools(tools, obix)
        payload = json.loads(
            bound.dispatch(
                "get_bms_history",
                {"meter_id": "bms_enyecontrols", "point_id": "temp"},
            )
        )
        assert payload["samples"] == 1
        assert payload["history"][0]["last_value"] == 21.5

    def test_bms_tools_respect_scope(self, tools, obix):
        self._seed(obix)
        bound = MeterTools(
            tools.buffer, tools.meters, tools.base_points, obix_store=obix
        ).for_principal(Principal.consumer("maria", ("WTR-001",)))
        payload = json.loads(bound.dispatch("list_bms_points", {"meter_id": "bms_enyecontrols"}))
        assert payload["error"] == "access denied"

    def _findings_tools(self, tools, obix, requirements):
        return MeterTools(
            tools.buffer,
            tools.meters,
            tools.base_points,
            obix_store=obix,
            obix_validator=ObixValidator(requirements),
        )

    def test_list_bms_findings_tool(self, tools, obix):
        self._seed(obix)  # temp = 21.5
        bound = self._findings_tools(
            tools,
            obix,
            ObixRequirements(
                points={"bms_enyecontrols/temp": {"min": 18.0, "max": 21.0}}
            ),
        ).for_principal(Principal.admin("ops"))
        assert "list_bms_findings" in [s["name"] for s in bound.specs]
        payload = json.loads(bound.dispatch("list_bms_findings", {}))
        assert payload["count"] == 1
        assert payload["findings"][0]["rule"] == "out_of_range"
        assert payload["findings"][0]["point_id"] == "temp"
        assert payload["scanned_points"] == 1

    def test_list_bms_findings_severity_filter(self, tools, obix):
        self._seed(obix)
        bound = self._findings_tools(
            tools,
            obix,
            ObixRequirements(
                default_severity={"out_of_range": "warning"},
                points={"bms_enyecontrols/temp": {"min": 18.0, "max": 21.0}},
            ),
        ).for_principal(Principal.admin("ops"))
        critical = json.loads(
            bound.dispatch("list_bms_findings", {"min_severity": "critical"})
        )
        assert critical["count"] == 0
        warnings = json.loads(
            bound.dispatch("list_bms_findings", {"min_severity": "warning"})
        )
        assert warnings["count"] == 1

    def test_list_bms_findings_respect_scope(self, tools, obix):
        self._seed(obix)
        bound = self._findings_tools(
            tools,
            obix,
            ObixRequirements(
                points={"bms_enyecontrols/temp": {"min": 18.0, "max": 21.0}}
            ),
        ).for_principal(Principal.consumer("maria", ("WTR-001",)))
        payload = json.loads(bound.dispatch("list_bms_findings", {}))
        assert payload["count"] == 0
