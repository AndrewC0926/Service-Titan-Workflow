"""Agent committee: LLM client, evidence packets, prompts, the eleven agents and gates.

Agents return JSON only and never call tools that act. Risk, tax, sizing and
eligibility stay in deterministic code (see ``gates`` and ``committee.engines``).
"""

from committee.agents.errors import (
    AgentError,
    AgentOutputInvalid,
    BudgetExceeded,
    IndependenceViolation,
    LLMCallFailed,
    PacketLeak,
)

__all__ = [
    "AgentError",
    "AgentOutputInvalid",
    "BudgetExceeded",
    "IndependenceViolation",
    "LLMCallFailed",
    "PacketLeak",
]
