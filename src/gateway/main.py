"""Gateway bootstrap + graceful-shutdown loop.

Loads configuration, opens the local MySQL buffer, and if real MySQL
credentials are configured runs extraction cycles on the poll interval with
exponential backoff on errors. All office-database access is SELECT-only (see
``gateway.extractor``); every write goes to the local MySQL buffer.

With placeholder credentials the process runs in idle mode (waits for a
shutdown signal without connecting anywhere).

An optional Bedrock-backed AI assistant (``AI_ENABLED=true``) is served on its
own port with bearer-token auth. It reads the same local buffer as everything
else and is deliberately isolated: AI failures never affect the Modbus/BACnet
servers or the extraction loop.
"""

from __future__ import annotations

import argparse
import asyncio
import math
import os
import signal
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from .backoff import ExponentialBackoff
from .bacnet_server import BACnetServer
from .config import load_all
from .extractor import Extractor
from .findings import load_layout, meter_state, plan_objects
from .health import HealthMonitor, MonitorState
from .logging_setup import setup_logging
from .map import MappingEngine
from .modbus_server import ModbusTCPServer
from .normalizer import Normalizer
from .obix_sync import build_bridge
from .obix_store import build_obix_store
from .service import run_cycle
from .config import get_buffer_mysql_settings
from .store import MySQLBuffer


class SingleInstance:
    """Exclusive cross-process lock so two gateways cannot share the ports.

    Two instances both bind 47808/1502; Windows then lets one win silently
    while the other half-works, which is very hard to debug. An exclusive
    create-and-hold file makes the second start fail loudly instead.

    The handle is released automatically when the process exits, so a crashed
    gateway never leaves a stale lock behind.
    """

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fh = None

    def acquire(self) -> bool:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            # O_EXCL is atomic: only one process can create the file.
            fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_RDWR)
        except FileExistsError:
            return False
        except OSError:
            # Cannot lock (read-only volume, permissions): allow startup
            # rather than refusing to run on a valid configuration.
            return True
        self._fh = os.fdopen(fd, "w")
        try:
            self._fh.write(f"{os.getpid()}\n")
            self._fh.flush()
        except OSError:
            pass
        return True

    def holder_pid(self) -> str:
        try:
            return self.path.read_text(encoding="utf-8").strip() or "unknown"
        except OSError:
            return "unknown"

    def release(self) -> None:
        if self._fh is not None:
            try:
                self._fh.close()
            except OSError:
                pass
            self._fh = None
        try:
            self.path.unlink()
        except OSError:
            pass

    def __enter__(self) -> "SingleInstance":
        return self

    def __exit__(self, *_exc) -> None:
        self.release()


def _build_ai(logger, settings, engine, buffer, base_points, meters, obix_store=None, obix_validator=None):
    """Assemble the optional AI stack.

    Returns ``None`` when AI is disabled, so the gateway behaves exactly as
    before when ``AI_ENABLED`` is false. A missing credential or a broken
    import degrades AI to off instead of aborting startup: the Modbus/BACnet
    pipeline must never depend on AWS.
    """
    from .ai.agent import Assistant
    from .ai.anomaly import AnomalyEngine
    from .ai.bedrock import BedrockClient
    from .ai.summary import FindingRecorder, SummaryWriter
    from .ai.tools import MeterTools
    from .obix_validation import ObixValidator, load_requirements

    if not settings.ai_enabled:
        logger.info("ai assistant disabled (AI_ENABLED=false)")
        return None
    if not settings.ai_ready():
        logger.warning(
            "AI_ENABLED=true but AWS credentials are incomplete - continuing with AI off"
        )
        return None
    try:
        # One validator, shared by the AI tools and the /bms/alarms endpoint so
        # both report the same requirement violations from the same config. The
        # caller may pass one in, so a live facet refresh applies to both.
        if obix_validator is None and obix_store is not None:
            obix_validator = ObixValidator(load_requirements())
        tools = MeterTools(
            buffer,
            meters,
            base_points,
            online_hours=24.0,
            watch_hours=48.0,
            obix_store=obix_store,
            obix_validator=obix_validator,
        )
        client = BedrockClient(settings, logger=logger)
        assistant = Assistant(client, tools, max_hops=settings.ai_max_tool_hops)
        summary_writer = SummaryWriter(client, buffer)
        recorder = FindingRecorder(
            buffer,
            engine=AnomalyEngine(
                leak_m3_per_hour=settings.ai_leak_m3_per_hour,
                battery_low_volts=settings.ai_battery_low_volts,
                signal_weak_dbm=settings.ai_signal_weak_dbm,
                history_span=lambda meter_id, point_id, since: buffer.history_span(
                    meter_id, point_id, since
                ),
            ),
            cooldown_hours=max(1, settings.ai_anomaly_cooldown_min // 60 or 1),
        )
    except Exception as exc:  # noqa: BLE001 - never block gateway startup
        logger.error("ai stack unavailable (%s: %s) - continuing with AI off", type(exc).__name__, exc)
        return None
    logger.info("ai assistant ready: %d tool(s), max_hops=%d", len(tools.specs), settings.ai_max_tool_hops)
    return client, tools, assistant, summary_writer, recorder, obix_validator


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
    buffer: MySQLBuffer,
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


def _prune_history(buffer: MySQLBuffer, logger, days: int) -> None:
    """Drop hourly buckets past the retention window."""
    cutoff = (
        datetime.now(timezone.utc) - timedelta(days=max(1, days))
    ).replace(minute=0, second=0, microsecond=0).isoformat()
    try:
        removed = buffer.prune_history(cutoff)
        if removed:
            logger.info("history pruned: %d bucket(s) older than %d day(s)", removed, days)
    except Exception as exc:  # noqa: BLE001 - never break a poll cycle
        logger.error("history prune failed: %s", exc)


def _record_history(buffer: MySQLBuffer, moment: datetime) -> int:
    """Fold current point values into their hourly buckets.

    Runs on the loop thread with the rest of the buffer work: the SQLite
    connection is thread-affine, so this must never move to a worker thread.
    """
    bucket = moment.astimezone(timezone.utc).replace(
        minute=0, second=0, microsecond=0
    ).isoformat()
    written = 0
    for row in buffer.list_normalized_meters():
        for point_id, point in buffer.get_normalized(row["meter_id"]).items():
            value = point.get("value")
            if value is None:
                continue
            try:
                numeric = float(value)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(numeric):
                continue
            buffer.record_history(row["meter_id"], point_id, numeric, bucket)
            written += 1
    return written


def _run_findings(logger, recorder, moment: datetime) -> None:
    """Evaluate anomaly rules. Never allowed to break the poll cycle."""
    if recorder is None:
        return
    try:
        stored = recorder.run_once(moment)
        if stored:
            logger.info("anomaly scan: %d new finding(s)", stored)
    except Exception as exc:  # noqa: BLE001
        logger.error("anomaly scan failed: %s", exc)


def _publish_findings(logger, layout, buffer: MySQLBuffer, bacnet) -> None:
    """Push the currently open findings into the BACnet AV/BV objects.

    Runs after the anomaly scan and before/with the protocol sync, so the BMS
    sees the same cycle's findings. Failure is logged and swallowed: losing an
    AI update must never take the polling loop down with it.
    """
    if not layout.enabled or bacnet is None:
        return
    try:
        state = meter_state(layout, buffer.open_findings(limit=500))
        updated = bacnet.sync_findings(state)
        open_meters = sum(1 for v in state.values() if v.get("finding_count"))
        logger.info(
            "findings published to bacnet: %d object(s), %d meter(s) with findings",
            updated,
            open_meters,
        )
    except Exception as exc:  # noqa: BLE001
        logger.error("findings publication failed: %s", exc)


def _bacnet_of(servers: tuple):
    """Return the BACnetServer from the (modbus, bacnet) servers tuple.

    The extraction loop only receives the tuple, not the individual servers,
    so findings publication picks the BACnet member out by duck-typing rather
    than by position.
    """
    for server in servers or ():
        if hasattr(server, "sync_findings"):
            return server
    return None


async def _read_obix(logger, bridge, buffer: MySQLBuffer, obix_store: Any = None) -> bool:
    """Pull NeoView point values into the buffer. Never breaks the cycle.

    The network fetch runs in a worker thread so a slow or offline station can
    never stall the event loop; the buffer write stays on this (event-loop)
    thread because the shared buffer connection is not thread-safe. When an
    oBIX database is configured the same values are also persisted there.
    Returns True when at least one value was read - the facet refresh uses that
    as a cheap "the station is up" signal.
    """
    if bridge is None:
        return False
    try:
        started = datetime.now(timezone.utc)
        values = await asyncio.to_thread(bridge.fetch)
        stored = bridge.store(buffer, values)
        if obix_store is not None:
            try:
                obix_store.record_values(values)
                obix_store.record_run(
                    started.isoformat(timespec="seconds"),
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    stored,
                    0,
                    int((datetime.now(timezone.utc) - started).total_seconds() * 1000),
                )
            except Exception as exc:  # noqa: BLE001 - oBIX DB is optional
                logger.error("obix store failed: %s", exc)
        logger.info("obix read: %d point value(s) stored", stored)
        return bool(values)
    except Exception as exc:  # noqa: BLE001
        logger.error("obix read failed: %s", exc)
        return False


async def _refresh_obix_facets(logger, bridge, obix_validator) -> None:
    """Read Neoview point facets and fold them into the live validator.

    Best-effort: on any failure the validator keeps its current requirements,
    so validation continues to work off the last cache. Called only after a
    successful oBIX read, so the station is known to be reachable.
    """
    if bridge is None or obix_validator is None:
        return
    from .obix_facets import probe_facets, save_facets_cache
    from .obix_validation import load_requirements

    try:
        report = await asyncio.to_thread(
            probe_facets, bridge.client, bridge.point_map, logger=logger
        )
    except Exception as exc:  # noqa: BLE001 - a probe must never break the cycle
        logger.warning("obix facet probe failed: %s", exc)
        return
    if report.aborted or not report.points:
        logger.info("obix facet probe: no facets (%s)", report.summary())
        return
    try:
        save_facets_cache(report.points, source=getattr(bridge.client, "base_url", ""))
    except Exception as exc:  # noqa: BLE001 - the cache is best-effort
        logger.warning("obix facet cache write failed: %s", exc)
    obix_validator.requirements = load_requirements(facets=report.points)
    logger.info(
        "obix facets refreshed: %d point(s) now use station bounds (%s)",
        len(report.points),
        report.summary(),
    )


def _publish_findings_obix(logger, bridge, layout, buffer: MySQLBuffer) -> None:
    """Publish open findings to NeoView over oBIX. Never breaks the cycle."""
    if bridge is None:
        return
    try:
        written = bridge.publish(buffer, layout)
        if written:
            logger.info("findings published to obix: %d value(s)", written)
    except Exception as exc:  # noqa: BLE001
        logger.error("obix findings publication failed: %s", exc)


async def _summary_loop(
    logger,
    stop: asyncio.Event,
    writer,
    settings,
    check_every_s: float = 300.0,
) -> None:
    """Write one summary per local day, shortly after AI_SUMMARY_LOCAL_TIME.

    Sleeps in short steps rather than to the next scheduled instant so that a
    shutdown does not have to wait out a multi-hour sleep.
    """
    if writer is None:
        return
    try:
        hour, _, minute = settings.ai_summary_local_time.partition(":")
        target_hour = int(hour)
        target_minute = int(minute or 0)
    except (TypeError, ValueError):
        logger.warning("AI_SUMMARY_LOCAL_TIME is not HH:MM - summary scheduler disabled")
        return

    last_date = ""
    while not stop.is_set():
        try:
            now_local = datetime.now().astimezone()
            today = now_local.strftime("%Y-%m-%d")
            past_time = (now_local.hour, now_local.minute) >= (target_hour, target_minute)
            if past_time and today != last_date:
                existing = writer.buffer.summary_for(today)
                if existing:
                    last_date = today
                else:
                    try:
                        result = await writer.write(now_local.astimezone(timezone.utc))
                        last_date = today
                        logger.info(
                            "daily summary written for %s (%d finding(s))",
                            result["local_date"],
                            result["findings"],
                        )
                    except Exception as exc:  # noqa: BLE001 - retry next tick
                        logger.warning("daily summary failed: %s: %s", type(exc).__name__, exc)
        except Exception as exc:  # noqa: BLE001 - loop must survive
            logger.error("summary loop error: %s", exc)
        await _wait_or_stop(stop, check_every_s)


async def _extraction_loop(
    logger,
    stop: asyncio.Event,
    buffer: MySQLBuffer,
    extractor: Any,
    normalizer: Any,
    engines: MappingEngine,
    servers: tuple,
    pool_interval_s: float,
    batch_size: int,
    retention_hours: float,
    state: MonitorState,
    recorder: Any = None,
    history_retention_days: int = 90,
    findings_layout: Any = None,
    obix_bridge: Any = None,
    obix_store: Any = None,
    obix_poll_interval_s: float = 60.0,
    obix_validator: Any = None,
    obix_facets_refresh_s: float = 0.0,
) -> None:
    backoff = ExponentialBackoff(
        initial_seconds=1.0,
        max_seconds=float(pool_interval_s * 4) if pool_interval_s > 0 else 300.0,
        multiplier=2.0,
    )
    last_obix_read = 0.0
    last_facets_refresh = 0.0

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
            # NeoView over oBIX, when configured, is the authoritative view for
            # the BMS: fold its values in before history/findings are recorded.
            # It is slower than the poll loop, so it runs on its own interval.
            if obix_bridge is not None and (time.monotonic() - last_obix_read) >= obix_poll_interval_s:
                read_ok = await _read_obix(logger, obix_bridge, buffer, obix_store)
                last_obix_read = time.monotonic()
                # Refresh Neoview facets only after a successful read, so the
                # probe never waits out a timeout on an unreachable station.
                if (
                    read_ok
                    and obix_facets_refresh_s
                    and (time.monotonic() - last_facets_refresh) >= obix_facets_refresh_s
                ):
                    await _refresh_obix_facets(logger, obix_bridge, obix_validator)
                    last_facets_refresh = time.monotonic()
            now = datetime.now(timezone.utc)
            _record_history(buffer, now)
            _run_findings(logger, recorder, now)
            # ``servers`` is (modbus, bacnet); publish AI findings to the
            # BACnet AV/BV objects so the BMS sees this cycle's results.
            _publish_findings(logger, findings_layout, buffer, _bacnet_of(servers))
            # Mirror the same findings into NeoView so its dashboard agrees.
            _publish_findings_obix(logger, obix_bridge, findings_layout, buffer)
            # Pruning hourly buckets hourly is ample; no need every 15s cycle.
            if now.minute == 0:
                _prune_history(buffer, logger, history_retention_days)
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

    # Refuse to start a second gateway: both would bind the same BACnet and
    # Modbus ports, and the loser fails in ways that look like data faults.
    lock_path = (config_dir or Path("config")).joinpath("gateway.lock")
    guard = SingleInstance(lock_path)
    if not guard.acquire():
        logger.error(
            "another gateway is already running (pid %s, lock %s). "
            "Stop it first, or delete the lock file if you are sure it is stale.",
            guard.holder_pid(),
            lock_path,
        )
        return 2

    buffer_cfg = get_buffer_mysql_settings(settings)
    buffer = MySQLBuffer(
        host=buffer_cfg["host"] or "127.0.0.1",
        port=buffer_cfg["port"] or 3306,
        user=buffer_cfg["user"],
        password=buffer_cfg["password"],
        database=buffer_cfg["database"] or "gateway_buffer",
    )
    buffer.initialize()
    logger.info(
        "buffer: mysql %s@%s:%d/%s %s",
        buffer_cfg["user"],
        buffer_cfg["host"],
        buffer_cfg["port"],
        buffer_cfg["database"],
        buffer.counts(),
    )

    engine = MappingEngine(meters, base_points)
    logger.info(
        "map: %d meter(s), %d modbus unit(s)",
        len(meters.meters),
        len(engine.map.modbus_unit_ids()),
    )

    obix_bridge = build_bridge(settings, engine, logger)
    obix_store = build_obix_store(settings, logger) if obix_bridge is not None else None
    if obix_store is not None:
        logger.info(
            "obix store: mysql %s@%s:%d/%s",
            obix_store.user,
            obix_store.host,
            obix_store.port,
            obix_store.database,
        )

    # One validator, built here rather than inside the AI stack, so the oBIX
    # facet refresh in the poll loop and the AI tools / `/bms/alarms` all share
    # the same object: a live facet refresh then applies everywhere at once.
    obix_validator = None
    if obix_store is not None:
        from .obix_validation import ObixValidator, load_requirements

        requirements = load_requirements()
        obix_validator = ObixValidator(requirements)
        if requirements.facets:
            logger.info(
                "obix validation: %d point(s) use cached Neoview facets",
                len(requirements.facets),
            )
        else:
            logger.info(
                "obix validation: using authored requirements (no facet cache yet)"
            )

    findings_layout = load_layout()
    findings_objects = plan_objects(
        findings_layout,
        list(meters.meters),
        {bp.instance for _, _, bp in engine.map.bacnet_entries()},
    )
    bms = BACnetServer(
        settings.bacnet_bind_ip,
        settings.bacnet_bind_port,
        settings.bacnet_device_instance,
        engine,
        findings_objects=findings_objects,
    )
    mbs = ModbusTCPServer(
        settings.modbus_bind_host, settings.modbus_bind_port, engine
    )
    servers = (mbs, bms)

    ai_stack = _build_ai(
        logger, settings, engine, buffer, base_points, meters, obix_store, obix_validator
    )
    if ai_stack:
        (
            ai_client,
            ai_tools,
            ai_assistant,
            ai_summary_writer,
            ai_recorder,
            ai_obix_validator,
        ) = ai_stack
    else:
        ai_client = ai_tools = ai_assistant = ai_summary_writer = ai_recorder = None
        ai_obix_validator = None
    ai_server = None
    resolver = None
    if ai_stack is not None:
        from .ai.registry import parse_principals, principals_by_subject
        from .ai_server import _ChainedResolver, AIServer

        # An explicit registry wins over the legacy shared token: with several
        # humans sharing one token nobody can be told apart in the audit trail,
        # and every caller sees every meter.
        static = parse_principals(settings.ai_principals)
        static_subjects = principals_by_subject(static)
        resolvers = [static]

        if settings.ai_assertion_secret:
            from .ai.assertion import SignedAssertionResolver

            # With EMS database sync configured, the assertion resolver reads
            # live roles and meter ownership straight from the ems-backend
            # database (read-only), so accounts created, deactivated or
            # re-scoped in the EMS app are honoured without a gateway restart.
            # Manual AI_PRINCIPALS entries still win over database rows.
            if settings.ems_mysql_host:
                from .ai.ems_sync import EmsUserSync, MergedSubjectMap

                ems_sync = EmsUserSync(
                    host=settings.ems_mysql_host,
                    port=settings.ems_mysql_port,
                    user=settings.ems_mysql_user,
                    password=settings.ems_mysql_password,
                    database=settings.ems_mysql_database,
                    ttl_seconds=settings.ems_sync_ttl_seconds,
                )
                subjects = MergedSubjectMap(static_subjects, ems_sync)
                logger.info(
                    "ai ems-user sync enabled: %s@%s:%d/%s (ttl %.0fs)",
                    settings.ems_mysql_user,
                    settings.ems_mysql_host,
                    settings.ems_mysql_port,
                    settings.ems_mysql_database,
                    settings.ems_sync_ttl_seconds,
                )
            else:
                subjects = static_subjects
                logger.warning(
                    "ai ems-user sync disabled (EMS_MYSQL_HOST not set) - "
                    "only manually registered AI_PRINCIPALS subjects can use assertions"
                )

            resolvers.append(
                SignedAssertionResolver(
                    secret=settings.ai_assertion_secret, principals=subjects
                )
            )
            logger.info(
                "ai signed-assertion auth enabled for %d registered subject(s)",
                len(subjects),
            )
        else:
            logger.info("ai signed-assertion auth disabled (AI_ASSERTION_SECRET not set)")

        resolver = _ChainedResolver(resolvers)
        ai_server = AIServer(
            settings.ai_bind_host,
            settings.ai_bind_port,
            ai_client,
            ai_tools,
            ai_assistant,
            settings.ai_bearer_token,
            settings.ai_rate_limit_per_min,
            summary_writer=ai_summary_writer,
            resolver=resolver,
            obix_store=obix_store,
            obix_validator=ai_obix_validator,
        )

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
        ai_probe=(ai_server.status if ai_server is not None else (lambda: {"enabled": False})),
    )
    stop = _create_stop_event(logger)

    try:
        await mbs.start()
        await bms.start()
        await health.start()
        if ai_server is not None:
            await ai_server.start()
        await _sync_protocols(logger, engine, servers, buffer, health_state)
    except Exception as exc:  # noqa: BLE001 - server start failures are fatal
        logger.error("protocol server startup failed: %s", exc)
        if ai_server is not None:
            await ai_server.stop()
        await health.stop()
        await mbs.stop()
        await bms.stop()
        buffer.close()
        guard.release()
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
            await asyncio.gather(
                _extraction_loop(
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
                    recorder=ai_recorder,
                    history_retention_days=settings.ai_history_retention_days,
                    findings_layout=findings_layout,
                    obix_bridge=obix_bridge,
                    obix_store=obix_store,
                    obix_poll_interval_s=settings.obix_poll_interval_seconds,
                    obix_validator=obix_validator,
                    obix_facets_refresh_s=settings.obix_facets_refresh_hours * 3600.0,
                ),
                _summary_loop(logger, stop, ai_summary_writer, settings),
            )
    finally:
        if ai_server is not None:
            await ai_server.stop()
        await health.stop()
        await mbs.stop()
        await bms.stop()
        if obix_store is not None:
            obix_store.close()
        buffer.close()
        logger.info("gateway stopped")
        guard.release()
    return 0


def main() -> int:
    args = _parse_args()
    try:
        return asyncio.run(amain(args.config))
    except KeyboardInterrupt:
        return 0


if __name__ == "__main__":
    raise SystemExit(main())