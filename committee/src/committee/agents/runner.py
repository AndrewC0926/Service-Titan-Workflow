"""Run the whole committee for one review in DESIGN order.

Base-Rate -> analysts (parallel) -> Macro per-name -> Bear -> Risk explainer ->
Tax explainer -> Behavioral Auditor -> Chair (+ deterministic gates).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from committee.agents.engine_inputs import EngineInputs
from committee.agents.gates import GateResult
from committee.agents.packets import ReviewPacket
from committee.agents.runtime import AgentResult, AgentRuntime
from committee.agents.steps import (
    review_seed,
    run_analysts,
    run_base_rate,
    run_bear,
    run_behavioral,
    run_chair,
    run_macro_name,
    run_risk_explainer,
    run_tax_explainer,
)

AGENT_ORDER: tuple[str, ...] = (
    "base_rate",
    "fundamentals",
    "valuation",
    "filings_insiders",
    "news_narrative",
    "macro_scenario",
    "bear",
    "risk_explainer",
    "tax_explainer",
    "behavioral_auditor",
    "chair",
)


@dataclass
class CommitteeResult:
    review_id: str
    results: dict[str, AgentResult[Any]] = field(default_factory=dict)
    gates: GateResult | None = None

    @property
    def outputs(self) -> dict[str, BaseModel]:
        return {k: v.output for k, v in self.results.items()}

    @property
    def cost_usd(self) -> float:
        return sum(r.cost_usd for r in self.results.values())


def run_committee(
    rt: AgentRuntime,
    review: ReviewPacket,
    engines: EngineInputs,
    *,
    max_workers: int = 4,
    result: CommitteeResult | None = None,
) -> CommitteeResult:
    """Run every step; ``result`` (if given) keeps partial outputs when a step fails."""
    out = result if result is not None else CommitteeResult(review_id=review.review_id)
    base = run_base_rate(rt, review)
    out.results["base_rate"] = base
    out.results.update(run_analysts(rt, review, base.output, max_workers=max_workers))
    out.results["macro_scenario"] = run_macro_name(rt, review)
    analysts = {
        k: out.results[k].output
        for k in ("base_rate", "fundamentals", "valuation", "filings_insiders", "news_narrative")
    }
    analysts["macro_scenario"] = out.results["macro_scenario"].output
    out.results["bear"] = run_bear(
        rt, review, analysts, seed=review_seed(rt.run_id, review.review_id)
    )
    out.results["risk_explainer"] = run_risk_explainer(rt, review, engines)
    out.results["tax_explainer"] = run_tax_explainer(rt, review, engines)
    out.results["behavioral_auditor"] = run_behavioral(rt, review)
    decision = run_chair(rt, review, engines, out.outputs)
    out.results["chair"] = decision.result
    out.gates = decision.gates
    return out
