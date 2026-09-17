"""Health monitor tests: state transitions + HTTP endpoint behavior."""

from __future__ import annotations

import asyncio
import json
from urllib import request as urlrequest

from gateway.health import HealthMonitor, MonitorState


def _send(method, path, port, host="127.0.0.1"):
    req = urlrequest.Request(
        f"http://{host}:{port}{path}", method=method, data=None
    )
    try:
        with urlrequest.urlopen(req, timeout=3) as resp:
            return resp.status, resp.headers.get("Content-Type"), resp.read()
    except urlrequest.HTTPError as err:
        return err.code, err.headers.get("Content-Type"), err.read()


def test_state_records():
    state = MonitorState()
    assert state.last_cycle is None

    class R:
        new_packets = 3
        snapshots = 5
        watermark_value = 99
        elapsed_s = 0.12

    class N:
        meters = 2
        point_values = 7
        events_consumed = 3
        events_failed = 0
        elapsed_s = 0.4

    state.record_cycle(R())
    assert state.last_cycle["watermark"] == 99
    state.record_normalize(N())
    assert state.last_normalize["point_values"] == 7
    state.record_sync(5)
    assert state.meters_live == 5
    assert state.last_sync_utc is not None
    state.record_error("boom")
    assert state.last_error == "boom"


def test_snapshot_status_lifecycle():
    state = MonitorState()
    monitor = HealthMonitor("127.0.0.1", 0, state, {"ok": True})
    assert monitor.snapshot()["status"] == "starting"

    state.mode = "live"
    assert monitor.snapshot()["status"] == "starting"  # no cycle yet

    class R:
        new_packets = 0
        snapshots = 0
        watermark_value = 0
        elapsed_s = 0.0

    state.record_cycle(R())
    assert monitor.snapshot()["status"] == "ok"
    state.record_error("x")
    assert monitor.snapshot()["status"] == "degraded"
    assert monitor.snapshot()["config"] == {"ok": True}  # no secrets


def test_http_health_endpoint():
    state = MonitorState()
    state.mode = "live"

    async def case():
        monitor = HealthMonitor(
            "127.0.0.1",
            0,
            state,
            {"modbus_bind": "127.0.0.1:502"},
            buffer_probe=lambda: {"events": {"new": 2}, "normalized": 1},
            protocol_probe=lambda: {
                "modbus": {"listening": True, "bind": "127.0.0.1:502"}
            },
        )
        await monitor.start()
        port = monitor._server.sockets[0].getsockname()[1]
        try:
            status, ctype, body = await asyncio.to_thread(
                _send, "GET", "/health", port
            )
            assert status == 200
            assert "application/json" in ctype
            payload = json.loads(body)
            assert payload["service"] == "water-gateway"
            assert payload["status"] == "starting"  # live but no cycle yet
            assert payload["buffer"] == {"events": {"new": 2}, "normalized": 1}
            assert payload["protocols"]["modbus"]["listening"] is True
            assert payload["config"] == {"modbus_bind": "127.0.0.1:502"}

            assert (await asyncio.to_thread(_send, "GET", "/", port))[0] == 200
            assert (await asyncio.to_thread(_send, "GET", "/nope", port))[0] == 404
            assert (
                await asyncio.to_thread(_send, "POST", "/health", port)
            )[0] == 405
        finally:
            await monitor.stop()

    asyncio.run(case())


def test_degraded_on_buffer_probe_failure():
    state = MonitorState()

    def bad_probe():
        raise RuntimeError("sqlite locked")

    monitor = HealthMonitor(
        "127.0.0.1", 0, state, {}, buffer_probe=bad_probe
    )
    snap = monitor.snapshot()
    assert snap["status"] == "degraded"
    assert "error" in snap["buffer"]