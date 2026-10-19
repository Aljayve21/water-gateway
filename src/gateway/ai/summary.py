"""Anomaly rules and daily summary generation."""

from __future__ import annotations

import json
from typing import Any, Optional

from .anomaly import AnomalyEngine, readings_from_buffer

DAILY_SYSTEM_PROMPT = """You are the water-metering assistant for the Enye water EMS gateway.

You write one short operational report per day for the site engineer.

Hard rules:
- Every number you write must appear in the FACT JSON you were given. Never
  compute, estimate, or extrapolate a reading.
- If a fact is missing, write that it is unavailable. Do not fill the gap.
- Report what changed: meters offline, alarms, low batteries, consumption that
  spiked. Say plainly when nothing needed attention.
- Do not give repair advice beyond "check the meter" or "check the battery".
- Write for an engineer in a hurry: under 150 words, short sentences, no
  preamble, no greeting, no closing offer of further help."""


class SummaryWriter:
    """Builds the FACT payload and turns a Bedrock response into a summary."""

    def __init__(self, client, buffer, *, local_offset_hours: int = 8) -> None:
        self.client = client
        self.buffer = buffer
        self.local_offset_hours = local_offset_hours

    def local_date(self, moment) -> str:
        return moment.strftime("%Y-%m-%d")

    def gather_facts(self, moment) -> dict[str, Any]:
        """Collect the day's findings plus per-meter state into one dict."""
        from datetime import timedelta, timezone

        local_now = moment.astimezone(timezone.utc) + timedelta(hours=self.local_offset_hours)
        start_local = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
        start_utc = (start_local - timedelta(hours=self.local_offset_hours)).isoformat(
            timespec="seconds"
        )
        end_utc = local_now.isoformat(timespec="seconds")

        findings = self.buffer.findings_between(start_utc, end_utc)
        readings = readings_from_buffer(self.buffer)

        meters = []
        for reading in readings:
            totalizer = reading.values.get("totalizer") or {}
            meters.append(
                {
                    "meter_id": reading.meter_id,
                    "comm_status": reading.comm_status,
                    "totalizer_m3": totalizer.get("value"),
                    "totalizer_updated_utc": totalizer.get("updated_utc"),
                    "battery_v": (reading.values.get("battery") or {}).get("value"),
                    "alarm": (reading.values.get("alarm") or {}).get("value"),
                }
            )

        by_rule: dict[str, int] = {}
        for item in findings:
            by_rule[item["rule"]] = by_rule.get(item["rule"], 0) + 1

        return {
            "local_date": self.local_date(local_now),
            "window_utc": {"start": start_utc, "end": end_utc},
            "meters_total": len(meters),
            "meters_offline": sum(1 for m in meters if m["comm_status"] == "lagging"),
            "findings_total": len(findings),
            "findings_by_rule": by_rule,
            "findings": [
                {"rule": f["rule"], "meter_id": f["meter_id"], "message": f["message"]}
                for f in findings[:40]
            ],
            "meters": meters,
        }

    async def write(self, moment) -> dict[str, Any]:
        """Generate and persist one summary. Raises BedrockError if AWS fails."""
        facts = self.gather_facts(moment)
        date = facts["local_date"]

        prompt = (
            "Write today's water metering report.\n\n"
            f"FACT JSON:\n{json.dumps(facts, indent=2, default=str)}"
        )
        response = await self.client.converse(
            [{"role": "user", "content": [{"text": prompt}]}],
            system=DAILY_SYSTEM_PROMPT,
        )
        from .bedrock import content_blocks, first_text

        body = first_text(content_blocks(response))
        if not body:
            raise RuntimeError("model returned no summary text")

        findings_count = int(facts.get("findings_total") or 0)
        usage = response.get("usage") or {}
        self.buffer.save_summary(date, body, findings=findings_count, usage=usage)
        return {
            "local_date": date,
            "body": body,
            "findings": findings_count,
            "usage": usage,
            "facts": facts,
        }


class FindingRecorder:
    """Runs the rules each poll and writes new findings, deduplicated."""

    def __init__(self, buffer, engine: Optional[AnomalyEngine] = None, *, cooldown_hours: int = 1) -> None:
        self.buffer = buffer
        self.cooldown_hours = max(1, int(cooldown_hours))
        self.engine = engine or AnomalyEngine(
            history_span=lambda meter_id, point_id, since: self.buffer.history_span(
                meter_id, point_id, since
            )
        )

    def run_once(self, moment) -> int:
        """Scan every meter. Returns how many *new* findings were stored."""
        from .anomaly import hour_bucket

        bucket = hour_bucket(moment)
        stored = 0
        for reading in readings_from_buffer(self.buffer):
            for anomaly in self.engine.scan(reading):
                if self.buffer.add_finding(
                    rule=anomaly.rule,
                    meter_id=anomaly.meter_id,
                    severity=anomaly.severity,
                    message=anomaly.message,
                    bucket_utc=bucket,
                    detail=anomaly.detail,
                ):
                    stored += 1
        # Close findings that stopped firing so /ai/findings reflects reality.
        self.buffer.resolve_findings_older_than(
            self._bucket_before(bucket, self.cooldown_hours)
        )
        return stored

    @staticmethod
    def _bucket_before(bucket: str, hours: int) -> str:
        from datetime import datetime, timedelta, timezone

        moment = datetime.fromisoformat(bucket).astimezone(timezone.utc)
        return (moment - timedelta(hours=hours)).isoformat(timespec="seconds")
