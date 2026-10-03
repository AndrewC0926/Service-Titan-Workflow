from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from committee.agents.budget import BudgetGuard
from committee.agents.errors import AgentOutputInvalid, BudgetExceeded, IndependenceViolation
from committee.agents.fixtures import Fixture, fixture_path, list_fixtures, load_fixture
from committee.agents.harness import citation_report, probability_issues, run_fixture, run_harness
from committee.agents.llm import RecordedClient
from committee.agents.prompts import PromptRegistry
from committee.agents.runner import AGENT_ORDER, CommitteeResult, run_committee
from committee.agents.runtime import AgentRuntime, extract_json
from committee.agents.schemas import AnalystOutput
from committee.agents.steps import anonymize_outputs, assert_independent, run_bear
from committee.config.loader import load_config
from committee.config.schema import ModelsConfig
from committee.journal.store import Journal

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "fixtures" / "agents"
MODELS = load_config(ROOT / "config").models
REGISTRY = PromptRegistry.load(ROOT / "prompts")


def fx(fid: str = "fx_001") -> Fixture:
    return load_fixture(fixture_path(FIXTURES, fid))


def runtime(
    f: Fixture, journal: Journal | None = None, **kw: Any
) -> tuple[AgentRuntime, RecordedClient]:
    client = RecordedClient(copy.deepcopy(f.responses))
    return AgentRuntime(client, MODELS, REGISTRY, run_id="run-1", journal=journal, **kw), client


def test_extract_json_tolerates_fences_and_prose() -> None:
    assert extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert extract_json('Here you go: {"a": 2} thanks') == {"a": 2}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_full_committee_runs_in_design_order_and_journals() -> None:
    f = fx("fx_001")
    j = Journal(":memory:")
    rt, client = runtime(f, j)
    res = run_committee(rt, f.review, f.engines)
    assert list(res.results) == [
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
    ]
    calls = [r.agent for r in client.requests]
    assert calls[0] == "base_rate" and calls[-1] == "chair"
    assert set(calls[1:5]) == {"fundamentals", "valuation", "filings_insiders", "news_narrative"}
    assert calls[5:] == [
        "macro_scenario",
        "bear",
        "risk_explainer",
        "tax_explainer",
        "behavioral_auditor",
        "chair",
    ]
    entries = list(j.entries("agent_output"))
    assert [e.payload["agent"] for e in entries] == list(
        AGENT_ORDER
    )  # fixed order, even if parallel
    p = entries[0].payload
    for key in (
        "agent",
        "model_id",
        "prompt_hash",
        "input_packet_hash",
        "output",
        "tokens",
        "cost_usd",
        "latency_s",
        "run_id",
        "review_id",
        "attempt",
        "status",
    ):
        assert key in p
    assert p["model_id"] == MODELS.tier_for("base_rate").model
    assert p["tokens"]["cache_write"] > 0 and p["cost_usd"] > 0
    gate = j.latest("note")
    assert gate is not None and gate.payload["kind"] == "chair_gate"
    assert (
        res.gates is not None
        and res.gates.recommendation == "BUY"
        and res.gates.size_pct_total == 2.7
    )
    assert res.cost_usd > 0
    assert j.verify().ok


def test_requests_use_pinned_tiers_and_cache_layout() -> None:
    f = fx("fx_001")
    rt, client = runtime(f)
    run_committee(rt, f.review, f.engines)
    by_agent = {r.agent: r for r in client.requests}
    for agent, req in by_agent.items():
        assert req.model == MODELS.tier_for(agent).model
        assert req.max_tokens == MODELS.tier_for(agent).max_tokens
        assert req.system_blocks[0].startswith("You are one member")
        assert f.review.anon_id in req.system_blocks[0]
        assert req.packet.startswith("EVIDENCE PACKET")
        assert "Northwind" not in req.packet and "NWIN" not in req.packet
    assert by_agent["bear"].model != by_agent["chair"].model
    # all agents share one preamble text (cache prefix) within a review
    assert len({r.system_blocks[0] for r in client.requests}) == 1


def test_bear_sees_anonymized_shuffled_outputs() -> None:
    f = fx("fx_001")
    rt, client = runtime(f)
    run_committee(rt, f.review, f.engines)
    bear_req = next(r for r in client.requests if r.agent == "bear")
    packet = json.loads(bear_req.packet.split("\n", 1)[1])
    outs = packet["context"]["analyst_outputs"]
    assert len(outs) == 6
    assert all("agent" not in o for o in outs)
    assert [o["analyst"] for o in outs] == [f"Analyst {c}" for c in "ABCDEF"]
    for name in ("fundamentals", "valuation", "filings_insiders", "news_narrative", "base_rate"):
        assert f'"{name}"' not in json.dumps(outs)


def test_anonymize_shuffle_is_seeded() -> None:
    f = fx("fx_001")
    outs = {
        k: AnalystOutput.model_validate(v[0])
        for k, v in f.responses.items()
        if k in ("fundamentals", "valuation", "filings_insiders")
    }
    a = anonymize_outputs(outs, seed=1)
    assert a == anonymize_outputs(outs, seed=1)
    orders = {
        tuple(o["forecasts"][2]["probability"] for o in anonymize_outputs(outs, seed=s))
        for s in range(10)
    }
    assert len(orders) > 1


def test_bear_and_chair_must_use_different_tiers() -> None:
    raw = MODELS.model_dump()
    raw["agents"]["bear"] = "top"
    bad = ModelsConfig.model_construct(**{**raw, "tiers": MODELS.tiers, "agents": raw["agents"]})
    with pytest.raises(IndependenceViolation):
        assert_independent(bad)
    f = fx("fx_001")
    rt, _ = runtime(f)
    rt.models = bad
    with pytest.raises(IndependenceViolation):
        run_bear(rt, f.review, {}, seed=1)
    assert_independent(MODELS)


def test_repair_retry_then_success_is_journaled() -> None:
    f = fx("fx_005")
    j = Journal(":memory:")
    rt, client = runtime(f, j)
    res = run_committee(rt, f.review, f.engines)
    fund = res.results["fundamentals"]
    assert fund.attempts == 2
    rows = [e.payload for e in j.entries("agent_output") if e.payload["agent"] == "fundamentals"]
    assert [r["status"] for r in rows] == ["invalid", "ok"]
    assert "E99" in rows[0]["validation_error"]
    repair_req = [r for r in client.requests if r.agent == "fundamentals"][1]
    assert repair_req.followups[0][0] == "assistant"
    assert "failed validation" in repair_req.followups[1][1] and "E99" in repair_req.followups[1][1]
    assert res.gates is not None and res.gates.recommendation == "PASS"


def test_fail_closed_after_one_repair() -> None:
    f = fx("fx_001")
    responses = copy.deepcopy(f.responses)
    responses["base_rate"] = ["not json", {"agent": "base_rate"}]
    j = Journal(":memory:")
    rt = AgentRuntime(RecordedClient(responses), MODELS, REGISTRY, run_id="r", journal=j)
    partial = CommitteeResult(review_id=f.review.review_id)
    with pytest.raises(AgentOutputInvalid) as ei:
        run_committee(rt, f.review, f.engines, result=partial)
    assert ei.value.agent == "base_rate" and len(ei.value.records) == 2
    assert [e.payload["status"] for e in j.entries("agent_output")] == ["invalid", "invalid"]
    assert partial.results == {}


def test_parallel_analyst_failure_journals_and_raises() -> None:
    f = fx("fx_001")
    responses = copy.deepcopy(f.responses)
    responses["valuation"] = ["{}", "{}"]
    j = Journal(":memory:")
    rt = AgentRuntime(RecordedClient(responses), MODELS, REGISTRY, run_id="r", journal=j)
    with pytest.raises(AgentOutputInvalid, match="valuation"):
        run_committee(rt, f.review, f.engines)
    statuses = {(e.payload["agent"], e.payload["status"]) for e in j.entries("agent_output")}
    assert ("valuation", "invalid") in statuses and ("fundamentals", "ok") in statuses


def test_budget_guard_blocks_calls() -> None:
    f = fx("fx_001")
    j = Journal(":memory:")
    j.append("agent_output", {"cost_usd": MODELS.budget.monthly_usd})
    rt, client = runtime(f, j, budget=BudgetGuard(j, MODELS.budget))
    with pytest.raises(BudgetExceeded):
        run_committee(rt, f.review, f.engines)
    assert client.requests == []


def test_explainer_cannot_alter_engine_numbers() -> None:
    f = fx("fx_005")
    responses = copy.deepcopy(f.responses)
    tampered = dict(responses["risk_explainer"][0], verdict="PASS")
    responses["risk_explainer"] = [tampered, tampered]
    rt = AgentRuntime(RecordedClient(responses), MODELS, REGISTRY, run_id="r")
    with pytest.raises(AgentOutputInvalid, match="risk_explainer"):
        run_committee(rt, f.review, f.engines)


# -------------------------------------------------------------------- harness
def test_harness_on_all_fixtures() -> None:
    paths = list_fixtures(FIXTURES)
    assert [p.stem for p in paths] == ["fx_001", "fx_002", "fx_003", "fx_004", "fx_005"]
    report = run_harness([load_fixture(p) for p in paths], MODELS, REGISTRY)
    s = report.summary()
    assert s["schema_validity"] == 1.0 and s["repairs"] == 1
    assert s["citation_coverage"] == 1.0
    assert s["probability_failures"] == []
    assert s["contamination_flags"] == ["fx_002/news_narrative"]
    assert s["gate_downgrades"] == ["fx_003: BUY -> PASS", "fx_004: BUY -> WATCH"]
    fx3 = next(f for f in report.fixtures if f.fixture_id == "fx_003")
    assert fx3.gates is not None and any("lottery" in v for v in fx3.gates.violations)


def test_harness_reports_invalid_agent() -> None:
    f = fx("fx_002")
    bad = copy.deepcopy(f.responses)
    bad["chair"] = ["{}", "{}"]
    rep = run_fixture(
        Fixture(f.fixture_id, f.description, f.review, f.engines, bad, f.raw), MODELS, REGISTRY
    )
    statuses = {c.agent: c.status for c in rep.checks}
    assert statuses["chair"] == "invalid" and statuses["bear"] == "ok"
    assert rep.error and "chair" in rep.error and rep.gates is None


def test_citation_and_probability_checks_independent_of_schema() -> None:
    out = {
        "thesis_points": [{"evidence_ids": ["E1"]}, {"evidence_ids": ["E9"]}, {"evidence_ids": []}]
    }
    rep = citation_report(out, {"E1"})
    assert (rep.claims, rep.cited, rep.unknown_ids) == (3, 1, ("E9",))
    assert rep.coverage == pytest.approx(1 / 3)
    issues = probability_issues(
        "base_rate",
        {
            "forecasts": [{"event": "x", "horizon_months": 12, "probability": 1.4}],
            "scenario_payoffs": [{"probability": 0.5}, {"probability": 0.6}],
            "adjustments": [{"delta_pp": 8}, {"delta_pp": -5}],
        },
    )
    assert len(issues) == 3
    chair_issues = probability_issues(
        "chair", {"forecasts": {"p_doubles_36m": 0.7, "p_loses_50pct_36m": 0.5}}
    )
    assert chair_issues == ["P(doubles)+P(loses 50%) > 1"]
    assert probability_issues("macro_scenario", {"risk_budget_modifier": 0.3})
