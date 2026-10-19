"""Tool-calling agent loop for the water-meter assistant.

The loop is deliberately small and bounded:

1. Send the conversation plus the tool catalogue to Bedrock.
2. If the model asked for tools, run them, return the results as a
   ``toolResult`` turn, and go back to step 1.
3. Stop as soon as the model produces text.

``max_hops`` is enforced here (server-side), not requested from the model, so a
runaway model cannot burn quota. If the budget is exhausted the model is given
one final, tool-free turn so the user still gets an answer.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .bedrock import BedrockClient, BedrockError, content_blocks, first_text, tool_uses
from .roles import Principal, Role
from .tools import MeterTools

MAX_HISTORY_TURNS = 12

SYSTEM_PROMPT = """You are the water-metering assistant for the Enye water EMS gateway.

You answer questions about live water meter readings using the tools provided.

Hard rules:
- Never state a meter reading you have not retrieved with a tool this turn.
  If you did not call a tool, you do not know the values.
- Never invent, estimate, or extrapolate a number. If a tool returns no data,
  say the data is unavailable.
- Only the listed tools exist. Do not ask for SQL, credentials, or configuration.
- Quote units exactly as the tool returned them (m3, kg/cm2, L, V, dBm).
- Communication status is one of: on-time (read within 24h), watch (24-48h),
  or lagging (over 48h or no contact recorded). "lagging" means the meter is
  effectively offline.
- Timestamps from the tools are UTC. The office site runs on UTC+8.
- Be brief and factual. No preamble, no filler. Prefer a short table or a
  couple of sentences over a long explanation.

Access scope:
- You can only see meters the tools return. If a question is about a meter the
  tools do not return, say plainly that it is not available to you. Do not
  guess an id, do not speculate about its readings, and do not ask the user to
  supply values.
- If a tool reports "access denied", tell the user that meter is not in their
  account. Do not retry with a different id and do not offer to work around it."""


class Assistant:
    """Bounded tool-calling loop over :class:`BedrockClient`."""

    def __init__(
        self,
        client: BedrockClient,
        tools: MeterTools,
        max_hops: int = 5,
    ) -> None:
        self.client = client
        self.tools = tools
        self.max_hops = max(1, int(max_hops))

    @staticmethod
    def _trim(messages: list[dict]) -> list[dict]:
        """Keep the tail of the conversation so it cannot grow without bound."""
        if len(messages) <= MAX_HISTORY_TURNS:
            return messages
        return messages[-MAX_HISTORY_TURNS:]

    async def ask(
        self,
        question: str,
        history: Optional[list[dict]] = None,
        tools: Optional[MeterTools] = None,
        principal: Optional[Principal] = None,
    ) -> dict[str, Any]:
        """Answer one question. Raises :class:`BedrockError` if AWS is unusable.

        ``tools`` may be a view already bound to the caller's principal; that
        binding - not the system prompt - is what limits which meters can be
        read. ``principal`` only shapes the wording of the scope rule.
        """
        scoped = tools or self.tools
        who = principal or scoped.principal
        system = self._system_prompt(who)
        messages = self._trim(list(history or []))
        messages.append({"role": "user", "content": [{"text": question}]})

        invoked: list[dict[str, Any]] = []

        for hop in range(self.max_hops + 1):
            budget_left = self.max_hops - hop
            response = await self.client.converse(
                messages,
                system=system,
                # With no hops left we withhold tools so the model must answer.
                tools=scoped.specs if budget_left > 0 else None,
            )
            blocks = content_blocks(response)
            messages.append({"role": "assistant", "content": blocks})

            uses = tool_uses(blocks)
            if not uses:
                answer = first_text(blocks)
                if answer is None:
                    raise BedrockError("model returned no text content")
                return {
                    "answer": answer,
                    "tool_calls": invoked,
                    "hops": hop,
                    "usage": response.get("usage") or {},
                    "truncated": False,
                }

            results = []
            for use in uses:
                name = str(use.get("name") or "")
                payload = scoped.dispatch(name, use.get("input") or {})
                invoked.append({"tool": name, "input": use.get("input") or {}})
                results.append(
                    {
                        "toolResult": {
                            "toolUseId": use.get("toolUseId"),
                            "content": [{"text": payload}],
                        }
                    }
                )
            # Bedrock requires tool results to arrive as a user turn.
            messages.append({"role": "user", "content": results})

        # Budget exhausted: one final tool-free turn so the user gets an answer.
        response = await self.client.converse(messages, system=system)
        answer = first_text(content_blocks(response))
        if answer is None:
            raise BedrockError("model returned no text content")
        return {
            "answer": answer,
            "tool_calls": invoked,
            "hops": self.max_hops,
            "usage": response.get("usage") or {},
            "truncated": True,
        }

    @staticmethod
    def _system_prompt(principal: Principal) -> str:
        """Base prompt plus one line naming the caller's scope.

        The tools already refuse out-of-scope meters; this line only stops the
        model from answering "what is meter X?" with a guess when the refusal
        comes back.
        """
        if principal.sees_all_meters:
            return SYSTEM_PROMPT
        meters = ", ".join(principal.meters) or "none"
        return (
            f"{SYSTEM_PROMPT}\n\n"
            f"The current user is {principal.subject} ({principal.role.value}) and may only "
            f"access these meters: {meters}. Any other meter is out of scope. If asked about "
            "one, state that it is not available to them."
        )

    @staticmethod
    def parse_history(raw: Optional[str]) -> list[dict]:
        """Best-effort parse of a client-supplied conversation history."""
        if not raw:
            return []
        try:
            parsed = json.loads(raw)
        except (TypeError, ValueError):
            return []
        if not isinstance(parsed, list):
            return []
        clean: list[dict] = []
        for turn in parsed[-MAX_HISTORY_TURNS:]:
            if not isinstance(turn, dict):
                continue
            role = turn.get("role")
            text = turn.get("text")
            if role in ("user", "assistant") and isinstance(text, str) and text.strip():
                clean.append({"role": role, "content": [{"text": text}]})
        return clean
