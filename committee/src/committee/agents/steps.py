"""One function per committee step (DESIGN 7/8). The orchestrator calls these
individually; ``runner.run_committee`` chains them in DESIGN order."""

from __future__ import annotations

import hashlib
import random
from collections.abc import Mapping
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from typing import Any

from pydantic import BaseModel

from committee.agents.engine_inputs import EngineInputs
from committee.agents.errors import AgentOutputInvalid, IndependenceViolation
from committee.agents.gates import GateResult, apply_gates
from committee.agents.packets import EvidencePacket, ReviewPacket
from committee.agents.runtime import AgentResult, AgentRuntime
from committee.agents.schemas import (
    AnalystOutput,
    BaseRateOutput,
    BearOutput,
    BehavioralOutput,
    ChairOutput,
    MacroNameOutput,
    MacroPortfolioOutput,
    NewsOutput,
    RiskExplainerOutput,
    TaxExplainerOutput,
)
from committee.config.schema import ModelsConfig

PARALLEL_ANALYSTS: tuple[str, ...] = (
    "fundamentals",
    "valuation",
    "filings_insiders",
    "news_narrative",
)
ANALYST_SCHEMAS: dict[str, type[AnalystOutput]] = {
    "fundamentals": AnalystOutput,
    "valuation": AnalystOutput,
    "filings_insiders": AnalystOutput,
    "news_narrative": NewsOutput,
}


def assert_independent(models: ModelsConfig) -> None:
    """Bear (and the Risk explainer) must not share the Chair's tier or model (DESIGN 7)."""
    chair_tier = models.agents["chair"]
    for agent in ("bear", "risk_explainer"):
        tier = models.agents[agent]
        if tier == chair_tier or models.tiers[tier].model == models.tiers[chair_tier].model:
            raise IndependenceViolation(
                f"{agent} uses tier {tier} ({models.tiers[tier].model}); the chair uses "
                f"{chair_tier} ({models.tiers[chair_tier].model}). They must differ."
            )


def review_seed(run_id: str, review_id: str) -> int:
    return int(hashlib.sha256(f"{run_id}:{review_id}".encode()).hexdigest()[:12], 16)


def _dump(m: BaseModel) -> dict[str, Any]:
    return m.model_dump(mode="json")


# ----------------------------------------------------------------- base rate
def run_base_rate(rt: AgentRuntime, review: ReviewPacket) -> AgentResult[BaseRateOutput]:
    return rt.call("base_rate", review.for_agent("base_rate"), BaseRateOutput)


def base_rate_context(base_rate: BaseRateOutput) -> dict[str, Any]:
    return {
        "base_rate": {
            "reference_class": base_rate.reference_class,
            "forecasts": [_dump(f) for f in base_rate.forecasts],
        }
    }


# ------------------------------------------------------------------ analysts
def run_analyst(
    rt: AgentRuntime,
    agent: str,
    review: ReviewPacket,
    base_rate: BaseRateOutput,
    *,
    journal: bool = True,
) -> AgentResult[AnalystOutput]:
    packet = review.for_agent(agent, base_rate_context(base_rate))
    return rt.call(agent, packet, ANALYST_SCHEMAS[agent], journal=journal)


def run_analysts(
    rt: AgentRuntime,
    review: ReviewPacket,
    base_rate: BaseRateOutput,
    *,
    max_workers: int = 4,
) -> dict[str, AgentResult[AnalystOutput]]:
    """Run the four analysts in parallel; journal on this thread in a fixed order.

    Every completed call is journaled (with its cost) even when a sibling fails,
    so the budget guard and the audit trail never miss a paid call.
    """
    rt.check_budget()
    results: dict[str, AgentResult[AnalystOutput]] = {}
    errors: list[Exception] = []
    with ThreadPoolExecutor(max_workers=max_workers) as pool:
        futures = {
            a: pool.submit(run_analyst, rt, a, review, base_rate, journal=False)
            for a in PARALLEL_ANALYSTS
        }
        for agent in PARALLEL_ANALYSTS:
            try:
                res = futures[agent].result()
            except AgentOutputInvalid as e:
                rt.journal_records(e.records)
                errors.append(e)
                continue
            except Exception as e:  # e.g. LLMCallFailed: keep journaling the others
                errors.append(e)
                continue
            rt.journal_records(res.records)
            results[agent] = res
    if errors:
        raise errors[0]
    return results


# --------------------------------------------------------------------- macro
def run_macro_name(rt: AgentRuntime, review: ReviewPacket) -> AgentResult[MacroNameOutput]:
    return rt.call("macro_scenario", review.for_agent("macro_scenario"), MacroNameOutput)


def run_macro_portfolio(
    rt: AgentRuntime, portfolio_packet: EvidencePacket
) -> AgentResult[MacroPortfolioOutput]:
    """Weekly portfolio-level run; its risk_budget_modifier feeds EngineInputs."""
    return rt.call("macro_scenario", portfolio_packet, MacroPortfolioOutput)


# ---------------------------------------------------------------------- bear
def anonymize_outputs(outputs: Mapping[str, BaseModel], seed: int) -> list[dict[str, Any]]:
    """Analyst outputs with agent names removed, shuffled deterministically by seed."""
    items = [_dump(outputs[k]) for k in sorted(outputs)]
    for d in items:
        d.pop("agent", None)
    random.Random(seed).shuffle(items)  # noqa: S311 (deterministic order, not security)
    return [{"analyst": f"Analyst {chr(65 + i)}", **d} for i, d in enumerate(items)]


def run_bear(
    rt: AgentRuntime,
    review: ReviewPacket,
    analyst_outputs: Mapping[str, BaseModel],
    *,
    seed: int,
) -> AgentResult[BearOutput]:
    assert_independent(rt.models)
    packet = review.for_agent("bear", {"analyst_outputs": anonymize_outputs(analyst_outputs, seed)})
    return rt.call("bear", packet, BearOutput)


# ---------------------------------------------------------------- explainers
def run_risk_explainer(
    rt: AgentRuntime, review: ReviewPacket, engines: EngineInputs
) -> AgentResult[RiskExplainerOutput]:
    packet = review.for_agent("risk_explainer")
    return rt.call("risk_explainer", packet, RiskExplainerOutput, context={"risk": engines.risk})


def run_tax_explainer(
    rt: AgentRuntime, review: ReviewPacket, engines: EngineInputs
) -> AgentResult[TaxExplainerOutput]:
    packet = review.for_agent("tax_explainer")
    return rt.call("tax_explainer", packet, TaxExplainerOutput, context={"tax": engines.tax})


# ---------------------------------------------------------------- behavioral
def run_behavioral(rt: AgentRuntime, review: ReviewPacket) -> AgentResult[BehavioralOutput]:
    n_decisions = sum(1 for i in review.items if i.kind == "decision_history")
    packet = review.for_agent("behavioral_auditor", {"decisions_in_last_12_months": n_decisions})
    return rt.call("behavioral_auditor", packet, BehavioralOutput)


# --------------------------------------------------------------------- chair
@dataclass
class ChairDecision:
    result: AgentResult[ChairOutput]
    gates: GateResult


def chair_context(engines: EngineInputs, outputs: Mapping[str, BaseModel]) -> dict[str, Any]:
    return {
        "deterministic": {
            "bucket_tag": engines.bucket_tag,
            "lottery_filter_pass": engines.lottery_filter_pass,
            "risk_verdict": engines.risk.verdict,
            "risk_max_size_pct_total": engines.risk.max_size_pct_total,
            "risk_budget_modifier": engines.risk_budget_modifier,
            "size_cap_pct_total": round(
                engines.risk.max_size_pct_total * engines.risk_budget_modifier, 6
            ),
            "round_trip_cost": engines.round_trip_cost,
        },
        "analyst_brier_weights": dict(engines.brier_weights),
        "committee_outputs": {k: _dump(v) for k, v in sorted(outputs.items())},
    }


def run_chair(
    rt: AgentRuntime,
    review: ReviewPacket,
    engines: EngineInputs,
    outputs: Mapping[str, BaseModel],
) -> ChairDecision:
    assert_independent(rt.models)
    packet = review.for_agent("chair", chair_context(engines, outputs))
    res = rt.call("chair", packet, ChairOutput, context={"bucket_tag": engines.bucket_tag})
    gates = apply_gates(res.output, engines)
    if rt.journal is not None:
        rt.journal.append(
            "note",
            {
                "kind": "chair_gate",
                "run_id": rt.run_id,
                "review_id": review.review_id,
                "chair_prompt_hash": res.prompt_hash,
                **gates.model_dump(mode="json"),
            },
        )
    return ChairDecision(result=res, gates=gates)
