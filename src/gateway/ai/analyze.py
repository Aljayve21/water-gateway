"""Read-only AI analyzer: findings in, ranked recommendations out.

This is the "recommend" layer. The validator already decides *what* is wrong,
deterministically. What it cannot do is say *why it matters* or *what to do
first*. That is a small, bounded writing job, so it is done here with exactly
one Bedrock call and no tool-calling loop - unlike ``/ai/ask``, which lets the
model browse the data and can cost many calls per question.

Three properties are deliberate:

* **No persistence.** Unlike :class:`SummaryWriter`, nothing is written to the
  database. The endpoint is a GET; it must not leave a trace.
* **Fingerprint cache.** The analysis is reused until the underlying facts
  change, so repeated reads cost nothing. This is what makes the endpoint
  affordable enough to poll.
* **No invented numbers.** The same hard rule as the daily report: every figure
  in the answer must come from the FACT payload, so a recommendation can always
  be traced back to a reading.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from typing import Any, Optional

from .bedrock import content_blocks, first_text

ANALYZE_SYSTEM_PROMPT = """You are the water-metering and BMS assistant for the Enye water EMS gateway.

You are given a FACT JSON of current open findings. Turn it into a short list of
recommendations for the site engineer.

Hard rules:
- Every number you write must appear in the FACT JSON. Never compute, estimate,
  or extrapolate a reading. If a fact is missing, say it is unavailable.
- Order recommendations by how much they matter: safety and supply first, then
  data-integrity faults, then nuisance issues.
- For each recommendation give: what is wrong, why it matters, and the single
  next action. One line each where possible.
- BMS points out of range are usually a *configuration* fault (wrong
  engineering-unit range on the station), not a plant fault. Say so when the
  value is plausible for the point but the configured band is not.
- Never recommend writing to, resetting, or reconfiguring anything. Only say
  what a human should check or fix.
- Write for an engineer in a hurry: no preamble, no greeting, no closing offer
  of further help. Under 250 words.
- If the FACT JSON has no findings, say so plainly in one line."""

# How many individual findings to hand the model. Beyond this the list is
# summarised by rule, which keeps the prompt (and the bill) bounded.
_MAX_FINDINGS_IN_PROMPT = 40


def _rule_counts(findings: list[dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for item in findings:
        rule = str(item.get("rule") or "unknown")
        counts[rule] = counts.get(rule, 0) + 1
    return counts


class Analyzer:
    """One-call analysis of the current findings, cached until they change."""

    def __init__(
        self,
        client,
        *,
        ttl_seconds: int = 900,
        cache_size: int = 64,
    ) -> None:
        self.client = client
        self.ttl_seconds = max(0, int(ttl_seconds))
        self.cache_size = max(1, int(cache_size))
        # scope_key -> (fingerprint, result, generated_monotonic)
        self._cache: "OrderedDict[str, tuple[str, dict, float]]" = OrderedDict()
        self.hits = 0
        self.misses = 0

    # ── facts → prompt ─────────────────────────────────────────────────
    @staticmethod
    def fingerprint(facts: dict[str, Any]) -> str:
        """Stable hash of everything that could change the answer."""
        payload = json.dumps(facts, sort_keys=True, default=str)
        return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]

    def build_prompt(self, facts: dict[str, Any]) -> str:
        return (
            "Analyse the current findings and recommend what to do.\n\n"
            f"FACT JSON:\n{json.dumps(facts, indent=2, default=str)}"
        )

    # ── the call ───────────────────────────────────────────────────────
    async def analyze(
        self,
        facts: dict[str, Any],
        *,
        scope_key: str,
        force: bool = False,
    ) -> dict[str, Any]:
        """Analyse one scope. Raises BedrockError if AWS fails.

        Returns a dict with the readable ``recommendation`` text plus the
        structured counts behind it. ``cached`` says whether the model was
        actually consulted - the caller can surface that so the token spend is
        visible rather than hidden.
        """
        fingerprint = self.fingerprint(facts)
        now = time.monotonic()

        if not force:
            stored = self._cache.get(scope_key)
            if stored is not None:
                stored_fp, result, stored_at = stored
                fresh = self.ttl_seconds == 0 or (now - stored_at) < self.ttl_seconds
                if stored_fp == fingerprint and fresh:
                    self.hits += 1
                    self._cache.move_to_end(scope_key)
                    return {**result, "cached": True}
            self.misses += 1

        response = await self.client.converse(
            [{"role": "user", "content": [{"text": self.build_prompt(facts)}]}],
            system=ANALYZE_SYSTEM_PROMPT,
        )
        body = first_text(content_blocks(response))
        if not body:
            raise RuntimeError("model returned no analysis")

        findings = list(facts.get("findings") or [])
        usage = dict(response.get("usage") or {})
        result: dict[str, Any] = {
            "recommendation": body,
            "findings_analyzed": len(findings),
            "findings_by_rule": _rule_counts(findings),
            "usage": usage,
            "fingerprint": fingerprint,
        }
        self._cache[scope_key] = (fingerprint, result, now)
        self._cache.move_to_end(scope_key)
        while len(self._cache) > self.cache_size:
            self._cache.popitem(last=False)
        return {**result, "cached": False}

    # ── fact shaping ───────────────────────────────────────────────────
    @staticmethod
    def trim(findings: list[dict]) -> dict[str, Any]:
        """Facts for the model: counts for everything, detail for the worst.

        Keeps the prompt bounded no matter how many findings are open, so a
        site-wide analysis does not become a site-wide bill.
        """
        ranked = sorted(
            findings,
            key=lambda f: str(f.get("severity") or ""),
            reverse=True,
        )
        capped = ranked[:_MAX_FINDINGS_IN_PROMPT]
        trimmed = []
        for item in capped:
            trimmed.append(
                {
                    k: item.get(k)
                    for k in (
                        "severity",
                        "rule",
                        "meter_id",
                        "point_id",
                        "source",
                        "message",
                        "value",
                        "text_value",
                        "unit",
                        "min",
                        "max",
                        "expected_unit",
                        "captured_utc",
                        "updated_utc",
                    )
                    if item.get(k) is not None
                }
            )
        return {
            "findings_total": len(findings),
            "findings_by_rule": _rule_counts(findings),
            "findings_returned": len(trimmed),
            "findings_omitted": max(0, len(findings) - len(trimmed)),
            "findings": trimmed,
        }

    def stats(self) -> dict[str, Any]:
        return {
            "cache_entries": len(self._cache),
            "cache_hits": self.hits,
            "cache_misses": self.misses,
            "ttl_seconds": self.ttl_seconds,
        }


def meter_state(readings: Any, meter_ids: Optional[list[str]] = None) -> list[dict]:
    """Compact per-meter state for the FACT payload.

    ``readings`` is an iterable of objects exposing ``meter_id``,
    ``comm_status`` and ``values`` - the shape the anomaly engine already uses.
    """
    out: list[dict] = []
    for reading in readings or []:
        meter_id = getattr(reading, "meter_id", None)
        if meter_ids is not None and meter_id not in meter_ids:
            continue
        values = getattr(reading, "values", None) or {}

        def _value(point: str) -> Any:
            entry = values.get(point) or {}
            return entry.get("value")

        totalizer = _value("totalizer")
        out.append(
            {
                "meter_id": meter_id,
                "comm_status": getattr(reading, "comm_status", None),
                "totalizer_m3": totalizer,
                "battery_v": _value("battery"),
                "alarm": _value("alarm"),
            }
        )
    return out
