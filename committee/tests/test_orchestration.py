from __future__ import annotations

import copy
import datetime as dt
import json
from pathlib import Path
from typing import Any

import pytest

from committee.agents.llm import LLMRequest, LLMResponse, RecordedClient, Usage
from committee.agents.packets import FixtureSource
from committee.agents.prompts import PromptRegistry
from committee.agents.runtime import AgentRuntime
from committee.broker.approval import approve
from committee.broker.models import ApprovedLeg
from committee.config.loader import load_config
from committee.engines.risk import PortfolioState
from committee.engines.tax.ledger import LotLedger
from committee.journal.store import Journal
from committee.orchestration.cache import CachingClient
from committee.orchestration.review import Orchestrator, ReviewInputs, consensus
from committee.orchestration.states import TransitionError, current_state, transition

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config")
FX = json.loads((ROOT / "tests" / "fixtures" / "agents" / "fx_001.json").read_text())


class EchoingClient:
    """Recorded fixture responses, except the explainers echo the real engine numbers."""

    def __init__(self, responses: dict[str, list[Any]]) -> None:
        self.inner = RecordedClient(copy.deepcopy(responses))
        self.calls = 0

    def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        if request.agent not in ("risk_explainer", "tax_explainer"):
            return self.inner.complete(request)
        pkt = json.loads(request.packet.split("\n", 1)[1])
        item = pkt["items"][0]
        if request.agent == "risk_explainer":
            d = item["data"]
            body = {
                "agent": "risk_explainer",
                "anon_id": pkt["anon_id"],
                "flags": [],
                "explanation": f"The engine says {d['verdict']} [{item['id']}].",
                "verdict": d["verdict"],
                "max_size_pct_total": d["max_size_pct_total"],
                "veto_rule": d.get("veto_rule"),
            }
        else:
            d = item["data"]
            body = {
                "agent": "tax_explainer",
                "anon_id": pkt["anon_id"],
                "flags": [],
                "cpa_flags": d.get("cpa_flags", []),
                "explanation": f"Buy in the {d['recommended_account']} [{item['id']}]. Not tax advice; confirm with a CPA.",
                "recommended_account": d["recommended_account"],
                "wash_sale_blocked": d["wash_sale_blocked"],
                "after_tax_hurdle_pct": d["after_tax_hurdle_pct"],
            }
        return LLMResponse(
            text=json.dumps(body), model_id=request.model, usage=Usage(), stop_reason="end_turn"
        )


def inputs(**kw: Any) -> ReviewInputs:
    base: dict[str, Any] = dict(
        security_id=FX["security"]["security_id"],
        symbol=FX["security"]["ticker"],
        asof=dt.date.fromisoformat(FX["asof"]),
        bucket="core_pick",
        sector="Industrials",
        market_cap_usd=5e9,
        adv_usd=5e7,
        annual_vol=0.30,
        price=61.0,
        source=FixtureSource(FX),
        portfolio=PortfolioState(
            holdings=[], total_value=1_000_000, as_of=dt.date.fromisoformat(FX["asof"])
        ),
        cash_by_account={"ira": 200_000.0, "taxable": 50_000.0},
        tax_ledger=LotLedger(),
    )
    base.update(kw)
    return ReviewInputs(**base)


def make(j: Journal, client: Any) -> Orchestrator:
    rt = AgentRuntime(
        client, CFG.models, PromptRegistry.load(ROOT / "prompts"), run_id="run-1", journal=j
    )
    return Orchestrator(rt, j, CFG, brier_weights=FX["engines"]["brier_weights"])


def test_end_to_end_review_to_approval(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    client = EchoingClient(FX["responses"])
    out = make(j, client).review(inputs(), FX["review_id"])
    assert out.state == "AWAITING_APPROVAL", out.reason
    b = out.briefing
    assert b is not None and b.payload["symbol"] == "NWIN"
    assert b.payload["recommendation"] in ("BUY", "ADD", "WATCH", "PASS")
    if b.payload["legs"]:
        assert (
            b.payload["legs"][0]["max_pct_total"] <= CFG.risk_limits.buckets.core_pick.max_pct_total
        )
    assert "Research, not advice" in b.payload["markdown"]
    states = [e.payload["to"] for e in j.entries("state_transition")]
    assert states == [
        "SCREENED",
        "BASE_RATE",
        "ANALYSTS",
        "BEAR",
        "RISK",
        "TAX",
        "BEHAVIORAL",
        "CHAIR",
        "BRIEFED",
        "AWAITING_APPROVAL",
    ]
    forecasts = list(j.entries("forecast"))
    assert any(f.payload["agent"] == "chair" for f in forecasts) and any(
        f.payload["agent"] == "base_rate" for f in forecasts
    )
    assert all(f.seq < b.seq for f in forecasts)  # pre-registered before the briefing
    agents = {e.payload["agent"] for e in j.entries("agent_output")}
    assert len(agents) == 11
    # Idempotent: re-running returns the journaled briefing, no new LLM calls.
    calls = client.calls
    again = make(j, client).review(inputs(), FX["review_id"])
    assert again.briefing is not None and again.briefing.hash == b.hash and client.calls == calls
    assert j.verify().ok
    # The briefing goes through the human gate.
    if b.payload["legs"]:
        leg = b.payload["legs"][0]
        _, a = approve(
            j,
            b.hash,
            [
                ApprovedLeg(
                    symbol=leg["symbol"],
                    side=leg["side"],
                    account=leg["account"],
                    pct_total=leg["max_pct_total"],
                )
            ],
            "Engine-sized; thesis and falsifiers are clear.",
            1_000_000,
            dt.datetime.now(dt.UTC) + dt.timedelta(days=2),
        )
        assert a.entry_type == "approval"


def test_risk_veto_ends_early_but_scores(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    out = make(j, EchoingClient(FX["responses"])).review(
        inputs(market_cap_usd=1e8), FX["review_id"]
    )
    assert out.state == "VETOED" and "risk veto" in out.reason
    assert any(True for _ in j.entries("forecast"))
    assert j.latest("briefing") is None
    assert {e.payload["agent"] for e in j.entries("agent_output")} >= {"base_rate", "bear"}
    assert "chair" not in {e.payload["agent"] for e in j.entries("agent_output")}


def test_wash_sale_block_vetoes(tmp_path: Path) -> None:
    from committee.domain import Lot
    from committee.engines.tax.models import LotPick, SaleRequest

    ledger = LotLedger()
    ledger.add_purchase(
        Lot(
            lot_id="L1",
            account="taxable",
            symbol="NWIN",
            qty=10,
            cost_per_share=100,
            acquired_on=dt.date(2026, 1, 5),
        )
    )
    ledger.apply_sale(
        SaleRequest(
            sale_id="S1",
            account="taxable",
            symbol="NWIN",
            sold_on=dt.date(2026, 9, 20),
            price=60,
            picks=(LotPick(lot_id="L1", qty=10),),
        )
    )
    j = Journal(tmp_path / "j.sqlite")
    out = make(j, EchoingClient(FX["responses"])).review(inputs(tax_ledger=ledger), FX["review_id"])
    assert out.state == "VETOED" and "wash-sale" in out.reason


def test_invalid_output_parks_in_needs_attention(tmp_path: Path) -> None:
    bad = copy.deepcopy(FX["responses"])
    bad["bear"] = ["not json", "still not json"]
    j = Journal(tmp_path / "j.sqlite")
    out = make(j, EchoingClient(bad)).review(inputs(), FX["review_id"])
    assert out.state == "NEEDS_ATTENTION" and "bear" in out.reason.lower()
    assert current_state(j, FX["review_id"]) == "NEEDS_ATTENTION"


def test_cache_reuses_responses(tmp_path: Path) -> None:
    j1 = Journal(tmp_path / "a.sqlite")
    inner = EchoingClient(FX["responses"])
    cache = CachingClient(inner, tmp_path / "cache.sqlite")
    make(j1, cache).review(inputs(), FX["review_id"])
    n = inner.calls
    j2 = Journal(tmp_path / "b.sqlite")  # a fresh journal: the stage reruns, served from cache
    out = make(j2, cache).review(inputs(), FX["review_id"])
    assert out.state == "AWAITING_APPROVAL" and inner.calls == n and cache.hits >= 11
    assert out.cost_usd == 0.0


def test_transitions_are_enforced(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    with pytest.raises(TransitionError):
        transition(j, "r", "CHAIR")
    transition(j, "r", "SCREENED")
    with pytest.raises(TransitionError):
        transition(j, "r", "CHAIR")


def test_consensus_weights() -> None:
    from committee.agents.schemas import AnalystOutput

    def out(p: float) -> AnalystOutput:
        return AnalystOutput.model_construct(
            forecasts=[
                type(
                    "F", (), {"event": "beats_benchmark", "horizon_months": 12, "probability": p}
                )()
            ],
            scenario_payoffs=[
                type("S", (), {"scenario": "base", "probability": 1.0, "return_36m": 0.331})()
            ],
        )

    p, outcomes = consensus({"a": out(0.6), "b": out(0.3)}, {"a": 2.0, "b": 1.0})
    assert p == pytest.approx(0.5)
    assert outcomes[0].ret == pytest.approx(0.1, abs=1e-3) and outcomes[
        0
    ].probability == pytest.approx(1.0)
