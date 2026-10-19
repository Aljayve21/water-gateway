"""Optional oBIX bridge: pull NeoView points and publish AI findings.

Enabled only when ``OBIX_BASE_URL`` is set, so a gateway without oBIX behaves
exactly as before. Two directions:

  * read    - read point values from NeoView and write them into the local
              buffer's ``normalized_value`` table, so the Modbus/BACnet servers
              and the AI pipeline see what the BMS sees.
  * publish - write the currently open findings (``findings.meter_state``) back
              into the BMS so its dashboard shows the same AI results.

Two addressings are supported:

  * explicit map (``OBIX_POINT_MAP`` -> ``config/obix.yaml``) - the default for
    a real station, where points are flat (e.g. Niagara
    ``/config/Drivers/BacnetNetwork/ENYECONTROLS/points/<point>``) rather than
    one folder per meter.
  * folder layout (``OBIX_METER_ROOT``) - ``<root>/<meter_id>`` per meter, used
    by the mock and simple servers.

Both directions are best-effort: a read or write failure is logged and the
extraction cycle continues, mirroring how the BACnet findings publication is
allowed to fail without taking the poll loop down.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any, Optional

from .obix import ObixClient, ObixError
from .store import MySQLBuffer
from .config import PROJECT_ROOT

log = logging.getLogger("gateway.obix_sync")


def load_point_map(path: str) -> dict[str, Any]:
    """Load the explicit oBIX point map, or an empty map when unset/missing.

    Relative paths (the default ``config/obix.yaml``) are resolved against the
    project root, so the gateway behaves the same whether it is launched from
    the repo root or from ``src\\`` (as ``scripts/start_gateway.ps1`` does).
    """
    if not path:
        return {}
    file = Path(path)
    if not file.is_absolute() and not file.exists():
        candidate = PROJECT_ROOT / path
        if candidate.exists():
            file = candidate
    if not file.exists():
        log.warning("obix point map %s not found; using folder layout", path)
        return {}
    try:
        import yaml

        data = yaml.safe_load(file.read_text(encoding="utf-8")) or {}
    except Exception as exc:  # noqa: BLE001 - a bad map must not stop the gateway
        log.warning("obix point map %s could not be read: %s", path, exc)
        return {}
    return data if isinstance(data, dict) else {}


class ObixBridge:
    """Read and publish NeoView data through an :class:`ObixClient`."""

    def __init__(
        self,
        client: ObixClient,
        engine: Any,
        meter_root: str = "",
        insights_root: str = "",
        point_map: Optional[dict[str, Any]] = None,
    ) -> None:
        self.client = client
        self.engine = engine
        self.meter_root = (meter_root or "").strip("/")
        self.insights_root = (insights_root or "").strip("/")
        self.point_map = point_map or {}

    # ── read: NeoView -> gateway ───────────────────────────────────────
    def fetch(self) -> dict[tuple[str, str], tuple[float, str]]:
        """Fetch every mapped point (network only; touches no buffer).

        Returns ``{(meter_id, point_id): (value, unit)}``. Kept separate from
        :meth:`store` so this blocking I/O can run in a worker thread while the
        buffer write stays on the event-loop thread (the buffer connection is
        not thread-safe). A missing/unreadable point never blanks the others.
        """
        if self.point_map.get("read"):
            return self._fetch_explicit()
        if not self.meter_root:
            return {}
        return self._fetch_folders()

    def store(
        self, buffer: MySQLBuffer, values: dict[tuple[str, str], tuple[float, str]]
    ) -> int:
        """Write fetched values into ``normalized_value``. Returns the count."""
        for (meter_id, point_id), (value, unit) in values.items():
            buffer.upsert_normalized(meter_id, point_id, value, unit)
        return len(values)

    def read_into(self, buffer: MySQLBuffer) -> int:
        """Fetch and store in one call (used when threading is not needed)."""
        return self.store(buffer, self.fetch())

    def _fetch_explicit(self) -> dict[tuple[str, str], tuple[float, str]]:
        out: dict[tuple[str, str], tuple[float, str]] = {}
        for meter_id, points in self.point_map["read"].items():
            for point_id, href in (points or {}).items():
                try:
                    point = self.client.get_point(str(href), raw=True)
                except ObixError as exc:
                    log.warning("obix read %s/%s failed: %s", meter_id, point_id, exc)
                    continue
                value = point.as_float
                if value is None:
                    continue
                out[(meter_id, point_id)] = (value, point.unit or "")
        return out

    def _fetch_folders(self) -> dict[tuple[str, str], tuple[float, str]]:
        out: dict[tuple[str, str], tuple[float, str]] = {}
        for meter_id, meter in self.engine.map.meters.items():
            try:
                refs = self.client.list_refs(self._meter_path(meter_id))
            except ObixError as exc:
                log.warning("obix read %s failed: %s", self._meter_path(meter_id), exc)
                continue
            by_name = {p.name.rsplit("/", 1)[-1]: p for p in refs}
            for point_id in meter.points:
                point = by_name.get(point_id)
                if point is None:
                    continue
                value = point.as_float
                if value is None:
                    continue
                out[(meter_id, point_id)] = (value, point.unit or "")
        return out

    def _meter_path(self, meter_id: str) -> str:
        return f"{self.meter_root}/{meter_id}" if self.meter_root else meter_id

    # ── publish: gateway -> NeoView ────────────────────────────────────
    def publish(self, buffer: MySQLBuffer, layout: Optional[Any], limit: int = 500) -> int:
        """Publish the open findings to NeoView.

        Returns the number of values written. Needs an explicit write map or an
        insights root, plus an enabled findings layout; otherwise it is a no-op.
        """
        if layout is None or not getattr(layout, "enabled", False):
            return 0
        if not self.point_map.get("write") and not self.insights_root:
            return 0
        from .findings import meter_state

        state = meter_state(layout, buffer.open_findings(limit=limit))
        written = 0
        for meter_id, values in state.items():
            for key, value in values.items():
                href = self._write_path(meter_id, key)
                if not href:
                    continue
                try:
                    self.client.write_point(href, value)
                    written += 1
                except ObixError as exc:
                    log.warning("obix publish %s failed: %s", href, exc)
        return written

    def _write_path(self, meter_id: str, key: str) -> Optional[str]:
        explicit = (self.point_map.get("write") or {}).get(meter_id) or {}
        if key in explicit:
            return str(explicit[key])
        if self.insights_root:
            return f"{self.insights_root}/{meter_id}/{key}"
        return None


def build_bridge(settings: Any, engine: Any, logger: logging.Logger) -> Optional[ObixBridge]:
    """Build the bridge from settings, or ``None`` when oBIX is not configured."""
    if not getattr(settings, "obix_base_url", ""):
        return None
    client = ObixClient(
        settings.obix_base_url,
        username=settings.obix_username or None,
        password=settings.obix_password or None,
        timeout=settings.obix_timeout_seconds,
        verify_tls=settings.obix_verify_tls,
        auth=settings.obix_auth,
    )
    point_map = load_point_map(getattr(settings, "obix_point_map", ""))
    bridge = ObixBridge(
        client,
        engine,
        meter_root=settings.obix_meter_root,
        insights_root=settings.obix_insights_root,
        point_map=point_map,
    )
    logger.info(
        "obix bridge enabled: %s (auth=%s, read %s, publish %s)",
        settings.obix_base_url,
        settings.obix_auth,
        "map" if bridge.point_map.get("read") else (bridge.meter_root or "(unset)"),
        "map" if bridge.point_map.get("write") else (bridge.insights_root or "(unset)"),
    )
    return bridge
