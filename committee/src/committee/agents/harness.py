"""Evaluation harness (DESIGN 12, Prompt 7): run all agents on fixture packets and
report schema validity, citation coverage, probability sanity and contamination flags.

Citation coverage and probability sanity are computed here independently of the
schema validators, so the report also covers outputs that failed validation.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from typing import Any

from pydantic import BaseModel

from committee.agents.errors import AgentError, AgentOutputInvalid
from committee.agents.fixtures import Fixture
from committee.agents.gates import GateResult
from committee.agents.llm import LLMClient, RecordedClient
from committee.agents.prompts import PromptRegistry
from committee.agents.runner import AGENT_ORDER, CommitteeResult, run_committee
from committee.agents.runtime import AgentRuntime
from committee.config.schema import ModelsConfig
from committee.journal.store import Journal

CONTAMINATION_FLAG = "CONTAMINATION_RISK"
BASE_RATE_MAX_ADJ_PP = 10.0


# ---------------------------------------------------------- independent checks
def _claims(obj: Any) -> Iterable[dict[str, Any]]:
    """Every dict carrying an evidence_ids list (thesis points, adjustments, flags...)."""
    if isinstance(obj, dict):
        if isinstance(obj.get("evidence_ids"), list):
            yield obj
        for v in obj.values():
            yield from _claims(v)
    elif isinstance(obj, list):
        for v in obj:
            yield from _claims(v)


@dataclass(frozen=True)
class CitationReport:
    claims: int
    cited: int
    unknown_ids: tuple[str, ...]

    @property
    def coverage(self) -> float:
        return 1.0 if self.claims == 0 else self.cited / self.claims


def citation_report(output: Mapping[str, Any], evidence_ids: Iterable[str]) -> CitationReport:
    """A claim is covered when it cites >= 1 id and every id it cites exists."""
    known = set(evidence_ids)
    claims = list(_claims(dict(output)))
    unknown: set[str] = set()
    cited = 0
    for c in claims:
        ids = [str(x) for x in c["evidence_ids"]]
        bad = [i for i in ids if i not in known]
        unknown.update(bad)
        if ids and not bad:
            cited += 1
    return CitationReport(len(claims), cited, tuple(sorted(unknown)))


def _p(v: Any) -> bool:
    return isinstance(v, int | float) and 0.0 <= float(v) <= 1.0


def probability_issues(agent: str, output: Mapping[str, Any]) -> list[str]:
    """Bounds, sums and coherence checks over a raw output dict."""
    issues: list[str] = []
    for f in output.get("forecasts", []) if isinstance(output.get("forecasts"), list) else []:
        if not _p(f.get("probability")):
            issues.append(f"forecast {f.get('event')}@{f.get('horizon_months')} out of [0,1]")
    if isinstance(output.get("forecasts"), dict):
        for k, v in output["forecasts"].items():
            if not _p(v):
                issues.append(f"{k} out of [0,1]")
        fc = output["forecasts"]
        if (
            _p(fc.get("p_doubles_36m"))
            and _p(fc.get("p_loses_50pct_36m"))
            and fc["p_doubles_36m"] + fc["p_loses_50pct_36m"] > 1.01
        ):
            issues.append("P(doubles)+P(loses 50%) > 1")
    payoffs = output.get("scenario_payoffs")
    if isinstance(payoffs, list) and payoffs:
        total = sum(float(p.get("probability", 0)) for p in payoffs)
        if abs(total - 1.0) > 0.01:
            issues.append(f"scenario probabilities sum to {total:.3f}")
        if any(not _p(p.get("probability")) for p in payoffs):
            issues.append("scenario probability out of [0,1]")
    if agent == "base_rate":
        adj = sum(abs(float(a.get("delta_pp", 0))) for a in output.get("adjustments", []))
        if adj > BASE_RATE_MAX_ADJ_PP:
            issues.append(f"base-rate adjustments total {adj:.1f}pp > {BASE_RATE_MAX_ADJ_PP}pp")
    mod = output.get("risk_budget_modifier")
    if mod is not None and not (0.5 <= float(mod) <= 1.0):
        issues.append("risk_budget_modifier outside [0.5, 1.0]")
    return issues


def has_contamination_flag(output: Mapping[str, Any]) -> bool:
    flags = output.get("flags", [])
    return any(isinstance(f, str) and CONTAMINATION_FLAG in f for f in flags)


# ------------------------------------------------------------------- report
@dataclass
class AgentCheck:
    agent: str
    status: str  # ok | repaired | invalid | not_run
    attempts: int = 0
    citations: CitationReport | None = None
    probability_issues: list[str] = field(default_factory=list)
    contamination: bool = False
    error: str | None = None

    @property
    def schema_valid(self) -> bool:
        return self.status in ("ok", "repaired")


@dataclass
class FixtureReport:
    fixture_id: str
    description: str
    checks: list[AgentCheck]
    gates: GateResult | None
    cost_usd: float
    error: str | None = None


@dataclass
class HarnessReport:
    fixtures: list[FixtureReport]

    def _checks(self) -> list[AgentCheck]:
        return [c for f in self.fixtures for c in f.checks if c.status != "not_run"]

    @property
    def schema_validity(self) -> float:
        cs = self._checks()
        return sum(c.schema_valid for c in cs) / len(cs) if cs else 0.0

    @property
    def citation_coverage(self) -> float:
        cs = [c.citations for c in self._checks() if c.citations is not None]
        claims = sum(c.claims for c in cs)
        return 1.0 if claims == 0 else sum(c.cited for c in cs) / claims

    @property
    def probability_failures(self) -> list[str]:
        return [
            f"{f.fixture_id}/{c.agent}: {i}"
            for f in self.fixtures
            for c in f.checks
            for i in c.probability_issues
        ]

    @property
    def contamination_flags(self) -> list[str]:
        return [
            f"{f.fixture_id}/{c.agent}" for f in self.fixtures for c in f.checks if c.contamination
        ]

    def summary(self) -> dict[str, Any]:
        return {
            "fixtures": len(self.fixtures),
            "agent_runs": len(self._checks()),
            "schema_validity": round(self.schema_validity, 4),
            "repairs": sum(c.status == "repaired" for c in self._checks()),
            "citation_coverage": round(self.citation_coverage, 4),
            "probability_failures": self.probability_failures,
            "contamination_flags": self.contamination_flags,
            "gate_downgrades": [
                f"{f.fixture_id}: {f.gates.chair_recommendation} -> {f.gates.recommendation}"
                for f in self.fixtures
                if f.gates is not None and f.gates.downgraded
            ],
            "cost_usd": round(sum(f.cost_usd for f in self.fixtures), 6),
        }


def _check_from_records(
    agent: str, records: list[dict[str, Any]], ids: Iterable[str]
) -> AgentCheck:
    last = records[-1]
    out = last.get("output") if isinstance(last.get("output"), dict) else {}
    status = "invalid" if last["status"] != "ok" else ("repaired" if len(records) > 1 else "ok")
    return AgentCheck(
        agent=agent,
        status=status,
        attempts=len(records),
        citations=citation_report(out, ids) if out else None,
        probability_issues=probability_issues(agent, out) if out else [],
        contamination=has_contamination_flag(out) if out else False,
        error=last.get("validation_error"),
    )


def run_fixture(
    fixture: Fixture,
    models: ModelsConfig,
    registry: PromptRegistry,
    *,
    client: LLMClient | None = None,
    journal: Journal | None = None,
    run_id: str | None = None,
) -> FixtureReport:
    rt = AgentRuntime(
        client or RecordedClient(fixture.responses),
        models,
        registry,
        run_id=run_id or f"harness-{fixture.fixture_id}",
        journal=journal,
    )
    ids = fixture.review.evidence_ids
    checks: dict[str, AgentCheck] = {}
    error: str | None = None
    cost = 0.0
    result = CommitteeResult(review_id=fixture.review.review_id)
    try:
        run_committee(rt, fixture.review, fixture.engines, result=result)
    except AgentOutputInvalid as e:
        error = str(e)
        checks[e.agent] = _check_from_records(e.agent, e.records, ids)
    except AgentError as e:
        error = f"{type(e).__name__}: {e}"
    gates = result.gates
    for agent, res in result.results.items():
        checks[agent] = _check_from_records(agent, res.records, ids)
        cost += res.cost_usd
    ordered = [checks.get(a) or AgentCheck(agent=a, status="not_run") for a in AGENT_ORDER]
    return FixtureReport(
        fixture.fixture_id, fixture.description, ordered, gates, round(cost, 8), error
    )


def run_harness(
    fixtures: Iterable[Fixture], models: ModelsConfig, registry: PromptRegistry
) -> HarnessReport:
    return HarnessReport([run_fixture(f, models, registry) for f in fixtures])


def dump_output(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json")
