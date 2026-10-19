"""Amazon Bedrock ``Converse`` wrapper for the gateway AI assistant.

Design constraints that shaped this module:

* **Explicit credentials.** ``pydantic-settings`` parses ``.env`` into
  :class:`gateway.config.Settings` but does *not* export those values into
  ``os.environ``. boto3's ambient credential lookup would therefore find
  nothing, so keys are handed to the client constructor explicitly.
* **Never on the caller's thread.** boto3 is synchronous and blocking; every
  call is dispatched with ``asyncio.to_thread`` so the gateway's Modbus/BACnet
  event loop keeps running even while the model is thinking.
* **Multi-block responses.** Bedrock returns a *list* of content blocks and may
  place ``reasoningContent`` before ``text``. Indexing ``content[0]`` is not
  safe, so :func:`first_text` scans for the block it wants.
* **Nova tool-use settings.** Amazon's guidance is greedy decoding
  (``temperature=0``) plus generous ``maxTokens`` for tool payloads, so those
  are applied automatically whenever tools are supplied.
* **Failures are contained.** Everything raises :class:`BedrockError` (or
  :class:`CircuitOpen`) so the caller can degrade gracefully. A circuit breaker
  stops a broken AWS integration from burning quota on every request.
"""

from __future__ import annotations

import asyncio
import re
import time
from typing import Any, Iterable, Optional

from ..config import Settings

# Nova tool-use settings (Amazon's documented guidance).
_TOOL_TEMPERATURE = 0.0
_TOOL_MAX_TOKENS = 2048

# Plain-chat fallbacks when no tools are in play.
_CHAT_TEMPERATURE = 0.2
_CHAT_MAX_TOKENS = 1024

# Circuit breaker tuning.
_BREAKER_THRESHOLD = 5
_BREAKER_RESET_SECONDS = 300.0

# Cheap probe used by /ai/status.
_PING_MAX_TOKENS = 8

_ACCESS_KEY_RE = re.compile(r"\b(AKIA|ASIA)[0-9A-Z]{12,}\b")


class BedrockError(RuntimeError):
    """A Converse call could not be completed."""


class CircuitOpen(BedrockError):
    """Calls are short-circuited after repeated consecutive failures."""


# ─────────────────────────────────────────────────────────────────────────
# Response-shape helpers
# ─────────────────────────────────────────────────────────────────────────
# Nova is inconsistent about the tag it emits: some replies use <think>, others
# <thinking>. Matching only one form left the other visible to operators, so
# both - and mismatched open/close pairs - are handled.
_THINKING_PAIR_RE = re.compile(
    r"<think(?:ing)?>.*?</think(?:ing)?>\s*", re.DOTALL | re.IGNORECASE
)
# An unterminated <think> means the model was cut off mid-thought. Everything
# from that point on is reasoning, not an answer.
_THINKING_TAIL_RE = re.compile(r"<think(?:ing)?>.*", re.DOTALL | re.IGNORECASE)


def strip_thinking(text: str) -> str:
    """Remove Nova reasoning tags from a user-facing answer.

    Nova sometimes inlines its reasoning in the same ``text`` block as the
    answer rather than returning a separate ``reasoningContent`` block. That
    is internal scratch work, so it must not reach the operator.
    """
    cleaned = _THINKING_PAIR_RE.sub("", text)
    cleaned = _THINKING_TAIL_RE.sub("", cleaned)
    return cleaned.strip()


def first_text(blocks: Optional[Iterable[dict]]) -> Optional[str]:
    """Return the first usable ``text`` content block, skipping reasoning.

    Returns ``None`` when the model asked for a tool instead of answering, or
    when every candidate block held only reasoning.
    """
    for block in blocks or ():
        if isinstance(block, dict):
            text = block.get("text")
            if isinstance(text, str):
                cleaned = strip_thinking(text)
                if cleaned:
                    return cleaned
    return None


def tool_uses(blocks: Optional[Iterable[dict]]) -> list[dict]:
    """Return every ``toolUse`` block in the response, in order."""
    found: list[dict] = []
    for block in blocks or ():
        if isinstance(block, dict):
            use = block.get("toolUse")
            if isinstance(use, dict):
                found.append(use)
    return found


def content_blocks(response: dict) -> list[dict]:
    """Extract ``output.message.content`` defensively."""
    message = ((response or {}).get("output") or {}).get("message") or {}
    content = message.get("content")
    return content if isinstance(content, list) else []


# ─────────────────────────────────────────────────────────────────────────
# Circuit breaker
# ─────────────────────────────────────────────────────────────────────────
class CircuitBreaker:
    """Open after N consecutive failures; allow one probe after cooldown."""

    def __init__(
        self,
        threshold: int = _BREAKER_THRESHOLD,
        reset_seconds: float = _BREAKER_RESET_SECONDS,
    ) -> None:
        self.threshold = max(1, int(threshold))
        self.reset_seconds = float(reset_seconds)
        self.failures = 0
        self.opened_at: Optional[float] = None
        self._probing = False

    @property
    def is_open(self) -> bool:
        return self.opened_at is not None

    def allow(self) -> bool:
        """True when a call may proceed right now."""
        if self.opened_at is None:
            return True
        if time.monotonic() - self.opened_at < self.reset_seconds:
            return False
        if self._probing:
            return False
        # Cooldown elapsed: let exactly one request through as a probe.
        self._probing = True
        return True

    def record_success(self) -> None:
        self.failures = 0
        self.opened_at = None
        self._probing = False

    def record_failure(self) -> None:
        self._probing = False
        self.failures += 1
        if self.failures >= self.threshold:
            self.opened_at = time.monotonic()

    def snapshot(self) -> dict[str, Any]:
        return {
            "open": self.is_open,
            "consecutive_failures": self.failures,
            "threshold": self.threshold,
            "probe_in_flight": self._probing,
        }


# ─────────────────────────────────────────────────────────────────────────
# Client
# ─────────────────────────────────────────────────────────────────────────
class BedrockClient:
    """Thin, failure-contained wrapper around ``bedrock-runtime.converse``."""

    def __init__(self, settings: Settings, logger=None) -> None:
        self.settings = settings
        self._log = logger
        self._client = None
        self.breaker = CircuitBreaker()
        self.last_error: Optional[str] = None
        self.last_success_utc: Optional[float] = None

    # ── internals ───────────────────────────────────────────────────────
    def _redact(self, text: str) -> str:
        """Strip anything credential-shaped out of a message bound for logs."""
        for secret in (
            self.settings.aws_secret_access_key,
            self.settings.aws_access_key_id,
            self.settings.ai_bearer_token,
        ):
            if secret and len(secret) >= 8:
                text = text.replace(secret, "***")
        return _ACCESS_KEY_RE.sub("AKIA***", text)

    def _botocore_config(self):
        try:
            from botocore.config import Config
        except ImportError:  # pragma: no cover - botocore ships with boto3
            return None
        return Config(connect_timeout=5, read_timeout=20, retries={"max_attempts": 2})

    def _ensure_client(self):
        """Build the boto3 client lazily, passing credentials explicitly."""
        if self._client is not None:
            return self._client
        try:
            import boto3
        except ImportError as exc:
            raise BedrockError("boto3 is not installed") from exc
        if not self.settings.ai_ready():
            raise BedrockError("AWS credentials are not configured")
        self._client = boto3.client(
            service_name="bedrock-runtime",
            region_name=self.settings.aws_region,
            aws_access_key_id=self.settings.aws_access_key_id,
            aws_secret_access_key=self.settings.aws_secret_access_key,
            config=self._botocore_config(),
        )
        return self._client

    # ── public API ──────────────────────────────────────────────────────
    async def converse(
        self,
        messages: list[dict],
        *,
        system: Optional[str] = None,
        tools: Optional[list[dict]] = None,
        tool_choice: Optional[dict] = None,
        temperature: Optional[float] = None,
        max_tokens: Optional[int] = None,
    ) -> dict:
        """One Converse round-trip. Raises :class:`BedrockError` on failure."""
        if not self.breaker.allow():
            raise CircuitOpen("bedrock circuit breaker open after repeated failures")

        client = self._ensure_client()
        with_tools = bool(tools)
        params: dict[str, Any] = {
            "modelId": self.settings.bedrock_model_id,
            "messages": messages,
            "inferenceConfig": {
                "maxTokens": int(
                    max_tokens
                    if max_tokens is not None
                    else (_TOOL_MAX_TOKENS if with_tools else _CHAT_MAX_TOKENS)
                ),
                "temperature": float(
                    temperature
                    if temperature is not None
                    else (_TOOL_TEMPERATURE if with_tools else _CHAT_TEMPERATURE)
                ),
            },
        }
        if system:
            params["system"] = [{"text": system}]
        if with_tools:
            # Nova rejects a tool schema whose top level carries anything other
            # than type/properties/required, so specs are passed through as-is.
            params["toolConfig"] = {
                "tools": [{"toolSpec": spec} for spec in tools],
                "toolChoice": tool_choice or {"auto": {}},
            }

        try:
            async with asyncio.timeout(self.settings.ai_timeout_seconds):
                response = await asyncio.to_thread(client.converse, **params)
        except asyncio.TimeoutError as exc:
            self._fail(f"bedrock request timed out after {self.settings.ai_timeout_seconds}s")
            raise BedrockError("bedrock request timed out") from exc
        except BedrockError:
            raise
        except Exception as exc:  # noqa: BLE001 - every boto3 error is contained
            self._fail(f"{type(exc).__name__}: {exc}")
            raise BedrockError(self._redact(f"{type(exc).__name__}: {exc}")) from exc

        self.breaker.record_success()
        self.last_error = None
        self.last_success_utc = time.time()
        return response

    async def ping(self) -> dict[str, Any]:
        """Cheap reachability probe for ``/ai/status``."""
        try:
            await self.converse(
                [{"role": "user", "content": [{"text": "ping"}]}],
                max_tokens=_PING_MAX_TOKENS,
            )
        except BedrockError as exc:
            return {"ok": False, "error": str(exc)}
        return {"ok": True}

    def _fail(self, message: str) -> None:
        self.breaker.record_failure()
        self.last_error = self._redact(message)
        if self._log is not None:
            self._log.warning("bedrock call failed: %s", self.last_error)

    def status(self) -> dict[str, Any]:
        """Secret-free description of the AI integration."""
        return {
            "enabled": self.settings.ai_enabled,
            "ready": self.settings.ai_ready(),
            "region": self.settings.aws_region,
            "model_id": self.settings.bedrock_model_id,
            "timeout_seconds": self.settings.ai_timeout_seconds,
            "client_initialized": self._client is not None,
            "breaker": self.breaker.snapshot(),
            "last_error": self.last_error,
        }
