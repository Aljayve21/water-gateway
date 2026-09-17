"""Gateway bootstrap + graceful-shutdown loop.

Loads configuration, opens the local SQLite buffer, and if real MySQL
credentials are configured runs extraction cycles on the poll interval with
exponential backoff on errors. All office-database access is SELECT-only (see
``gateway.extractor``); every write goes to the local SQLite buffer.

With placeholder credentials the process runs in idle mode (waits for a
shutdown signal without connecting anywhere). Protocol servers (Modbus/BACnet)
and the normalizer are wired in later phases.
"""

from __future__ import annotations

import argparse
import asyncio
import signal
from pathlib import Path
from typing import Any

from .backoff import ExponentialBackoff
from .bacnet_server import BACnetServer
from .config import load_all
from .extractor import Extractor
from .health import HealthMonitor, MonitorState
from .logging_setup import setup_logging
from .map import MappingEngine
from .modbus_server import ModbusTCPServer
from .normalizer import Normalizer
from .service import run_cycle
from .store import SQLiteBuffer


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Enye water-meter gateway")
    parser.add_argument(
        "-c",
        "--config",
        type=Path,
        default=None,
        help="alternate directory containing gateway.yaml/base_points.yaml/meters.yaml",
    )
    return parser.parse_args()


def _create_stop_event(log) -> asyncio.Event:
    """Windows-safe signal bridge: SIGINT/SIGTERM/SIGBREAK set the event.

    ``loop.add_signal_handler`` is unavailable on Windows, so a plain
    ``signal.signal`` handler is registered on the main thread instead.
    NSSM / ``Stop-Process`` can deliver SIGTERM/SIGBREAK.
    """
    loop = asyncio.get_running_loop()
    stop = asyncio.Event()

    def _on_signal(signum: int, _frame) -> None:
        try:
            name = signal.Signals(signum).name
        except ValueError:
            name = str(signum)
        log.info("received %s, shutting down", name)
        loop.call_soon_threadsafe(stop.set)

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        try:
            signal.signal(getattr(signal, name), _on_signal)
        except (OSError, ValueError, AttributeError):
            continue
    return stop


async def _wait_or_stop(stop: asyncio.Event, timeout_s: float) -> None:
    try:
        await asyncio.wait_for(stop.wait(), timeout=timeout_s)
    except asyncio.TimeoutError:
        pass


async def _sync_protocols(
    logger,
    engines: MappingEngine,
    servers: tuple,
    buffer: SQLiteBuffer,
    state: MonitorState,
) -> None:
    try:
        live = engines.live(buffer)
        for server in servers:
            server.sync(live)
        state.record_sync(len(live))
        logger.info("servers synced: %d meters live", len(live))
    except Exception as exc:  # noqa: BLE001 - per-cycle resilience
        state.record_error(f"protocol sync: {exc}")
        logger.error("protocol sync failed: %s", exc)


async def _extraction_loop(
    logger,
    stop: asyncio.Event,
    buffer: SQLiteBuffer,
    extractor: Any,
    normalizer: Any,
    engines: MappingEngine,
    servers: tuple,
    pool_interval_s: float,
    batch_size: int,
    retention_hours: float,
    state: MonitorState,
) -> None:
    backoff = ExponentialBackoff(
        initial_seconds=1.0,
        max_seconds=float(pool_interval_s * 4) if pool_interval_s > 0 else 300.0,
        multiplier=2.0,
    )

    while not stop.is_set():
        try:
            result = run_cycle(buffer, extractor, batch_size, retention_hours)
            state.record_cycle(result)
            logger.info(
                "cycle done: new_packets=%d snapshots=%d watermark=%d elapsed=%.3fs",
                result.new_packets,
                result.snapshots,
                result.watermark_value,
                result.elapsed_s,
            )
            norm = normalizer.run(buffer)
            state.record_normalize(norm)
            logger.info(
                "normalized: meters=%d points=%d events_consumed=%d failed=%d elapsed=%.3fs",
                norm.meters,
                norm.point_values,
                norm.events_consumed,
                norm.events_failed,
                norm.elapsed_s,
            )
            await _sync_protocols(logger, engines, servers, buffer, state)
            backoff.reset()
            delay = pool_interval_s
        except Exception as exc:  # noqa: BLE001 - survive per-cycle failures
            state.record_error(str(exc))
            logger.error("extraction cycle failed: %s", exc)
            delay = backoff.next()
        if stop.is_set():
            break
        await _wait_or_stop(stop, delay)


async def amain(config_dir: Path | None) -> int:
    settings, gateway_config, base_points, meters = load_all(config_dir)
    logger = setup_logging(
        level=gateway_config.logging.level,
        log_dir=config_dir.joinpath(gateway_config.logging.dir) if config_dir else gateway_config.logging.dir,
        max_bytes=gateway_config.logging.max_bytes,
        backup_count=gateway_config.logging.backup_count,
        context=settings.fingerprint(),
    )
    logger.info("gateway starting")
    logger.info(
        "loaded: %d base points, %d meters, poll %.1fs",
        len(base_points.points),
        len(meters.meters),
        gateway_config.extraction.poll_interval_seconds,
    )

    buffer = SQLiteBuffer(gateway_config.buffer.path)
    buffer.initialize()
    logger.info("buffer: %s", buffer.counts())

    engine = MappingEngine(meters, base_points)
    logger.info(
        "map: %d meter(s), %d modbus unit(s)",
        len(meters.meters),
        len(engine.map.modbus_unit_ids()),
    )

    bms = BACnetServer(
        settings.bacnet_bind_ip,
        settings.bacnet_bind_port,
        settings.bacnet_device_instance,
        engine,
    )
    mbs = ModbusTCPServer(
        settings.modbus_bind_host, settings.modbus_bind_port, engine
    )
    servers = (mbs, bms)

    health_state = MonitorState()
    health_state.mode = "idle" if settings.is_placeholder() else "live"
    health = HealthMonitor(
        settings.health_bind_host,
        settings.health_bind_port,
        health_state,
        settings.fingerprint(),
        buffer_probe=buffer.counts,
        protocol_probe=lambda: {
            "modbus": {
                "listening": mbs.listening,
                "bind": f"{settings.modbus_bind_host}:{settings.modbus_bind_port}",
            },
            "bacnet": {
                "listening": bms.listening,
                "bind": (
                    f"{settings.bacnet_bind_ip}:{settings.bacnet_bind_port}"
                ),
                "device_instance": settings.bacnet_device_instance,
            },
        },
    )
    stop = _create_stop_event(logger)

    try:
        await mbs.start()
        await bms.start()
        await health.start()
        await _sync_protocols(logger, engine, servers, buffer, health_state)
    except Exception as exc:  # noqa: BLE001 - server start failures are fatal
        logger.error("protocol server startup failed: %s", exc)
        await health.stop()
        await mbs.stop()
        await bms.stop()
        buffer.close()
        return 1

    try:
        if settings.is_placeholder():
            logger.warning(
                "placeholder MySQL credentials in .env - idle mode (no office connection). "
                "Set a read-only account in .env to begin extraction."
            )
            await stop.wait()
        else:
            extractor = Extractor(settings)
            normalizer = Normalizer(
                online_hours=gateway_config.staleness.online_hours,
                watch_hours=gateway_config.staleness.watch_hours,
            )
            await _extraction_loop(
                logger,
                stop,
                buffer,
                extractor,
                normalizer,
                engine,
                servers,
                gateway_config.extraction.poll_interval_seconds,
                gateway_config.extraction.batch_size,
                gateway_config.buffer.retention_hours,
                health_state,
            )
    finally:
        await health.stop()
        await mbs.stop()
        await bms.stop()
        buffer.close()
        logger.info("gateway stopped")
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())