"""Exceptions raised by the agent layer. The orchestrator maps them to NEEDS_ATTENTION."""

from __future__ import annotations

from typing import Any


class AgentError(Exception):
    """Base class for agent-layer failures."""


class BudgetExceeded(AgentError):
    """The monthly API budget is used up; no further LLM calls this month."""


class LLMCallFailed(AgentError):
    """The API call failed after all retries (or with a non-retryable error)."""


class AgentOutputInvalid(AgentError):
    """The agent's output failed schema validation twice (initial + one repair). Fail closed."""

    def __init__(
        self, agent: str, errors: str, records: list[dict[str, Any]] | None = None
    ) -> None:
        super().__init__(f"{agent}: output invalid after repair retry: {errors}")
        self.agent = agent
        self.errors = errors
        self.records: list[dict[str, Any]] = records or []


class IndependenceViolation(AgentError):
    """Bear and Chair resolve to the same model tier or model id."""


class PacketLeak(AgentError):
    """An evidence packet contains data that must never reach a model."""
