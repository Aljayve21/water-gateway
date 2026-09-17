"""Extraction cycle orchestration: office MySQL -> local SQLite buffer.

One cycle:

  1. If the ``receive_packet.id`` watermark is absent, initialize it to the
     current MAX(id) without replaying history.
  2. Otherwise page new packets after the watermark, stage each as an event
     in the buffer (deduped), and remember the highest id seen.
  3. Refresh per-meter snapshots (heads + latest analysis + last_receive +
     addr) as raw JSON payloads.
  4. Advance the watermark and prune consumed events past retention.

The watermark is only advanced after staging succeeds; a failed cycle leaves
it untouched and is retried with backoff. All writes go to the LOCAL SQLite
buffer — never to the office database.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

from .store import SQLiteBuffer

WATERMARK_KIND = "receive_packet.id"


@dataclass
class CycleResult:
    new_packets: int
    snapshots: int
    watermark_value: int
    elapsed_s: float


def _build_snapshot(
    heads: dict[str, dict],
    analyses: dict[str, dict],
    last_receive: dict[str, dict],
    addrs: dict[str, dict],
) -> dict[str, dict]:
    meters = set(analyses) | set(last_receive)
    snapshots: dict[str, dict] = {}
    for meter_id in sorted(meters):
        snapshots[meter_id] = {
            "meter_id": meter_id,
            "head": heads.get(meter_id),
            "analysis": analyses.get(meter_id),
            "last_receive": last_receive.get(meter_id),
            "addr": (addrs.get(meter_id) or {}).get("addr"),
        }
    return snapshots


def run_cycle(
    buffer: SQLiteBuffer,
    extractor: Any,
    batch_size: int = 500,
    retention_hours: float = 24.0,
) -> CycleResult:
    """Run one extraction cycle using the passed ``extractor`` (an
    ``Extractor`` or any test double exposing the same duck-typed methods).
    Reads happen inside ``extractor.session()``; buffer writes are local.
    """
    start = time.monotonic()

    with extractor.session():
        current = buffer.get_watermark(WATERMARK_KIND)
        new_packets = 0
        highest_id = current or 0

        if current is None:
            highest_id = extractor.max_receive_packet_id()
            buffer.init_watermark_if_absent(WATERMARK_KIND, highest_id)
        else:
            last_seen = current
            while True:
                rows = extractor.fetch_new_packets(after_id=last_seen, limit=batch_size)
                if not rows:
                    break
                for row in rows:
                    if buffer.stage_event("receive_packet", str(row["id"]), row):
                        new_packets += 1
                    row_id = int(row["id"])
                    if row_id > last_seen:
                        last_seen = row_id
                if len(rows) < batch_size:
                    break
            if last_seen > current:
                buffer.save_watermark(WATERMARK_KIND, last_seen)
                highest_id = last_seen

        heads = extractor.fetch_heads()
        analyses = extractor.fetch_latest_analyses()
        last_receive = extractor.fetch_last_receive()
        addrs = extractor.fetch_meter_addrs()

    snapshots = _build_snapshot(heads, analyses, last_receive, addrs)
    for meter_id, payload in snapshots.items():
        buffer.upsert_snapshot(meter_id, payload)

    if retention_hours > 0:
        older_than = datetime.now(timezone.utc) - timedelta(hours=retention_hours)
        buffer.prune_consumed(older_than.isoformat(timespec="seconds"))

    return CycleResult(
        new_packets=new_packets,
        snapshots=len(snapshots),
        watermark_value=highest_id,
        elapsed_s=round(time.monotonic() - start, 3),
    )