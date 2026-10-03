from __future__ import annotations

import copy
from typing import Any

import pytest
from pydantic import ValidationError

from committee.agents.engine_inputs import EngineInputs, RiskEngineView, TaxEngineView
from committee.agents.gates import apply_gates
from committee.agents.schemas import (
    AnalystOutput,
    BaseRateOutput,
    BearOutput,
    BehavioralOutput,
    ChairOutput,
    MacroPortfolioOutput,
    RiskExplainerOutput,
    TaxExplainerOutput,
    iter_citations,
)

CTX = {
    "agent": "fundamentals",
    "anon_id": "SEC-1",
    "evidence_ids": frozenset({"E1", "E2"}),
    "bucket_tag": "CORE_PICK",
}


def analyst(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent": "fundamentals",
        "anon_id": "SEC-1",
        "bucket": "core_pick",
        "thesis_points": [
            {"claim": "c", "evidence_ids": ["E1"], "direction": "positive", "strength": 2}
        ],
        "forecasts": [{"event": "beats_benchmark", "horizon_months": 12, "probability": 0.55}],
        "scenario_payoffs": [
            {"scenario": "bear", "probability": 0.3, "return_36m": -0.2},
            {"scenario": "base", "probability": 0.5, "return_36m": 0.2},
            {"scenario": "bull", "probability": 0.2, "return_36m": 0.6},
        ],
        "key_uncertainties": [],
        "falsifiers": [{"observable": "m", "threshold": "t", "check_by": "2027-01-31"}],
        "insufficient_evidence": [],
        "flags": [],
    }
    d.update(over)
    return d


def test_valid_analyst() -> None:
    out = AnalystOutput.model_validate(analyst(), context=CTX)
    assert out.probability("beats_benchmark", 12) == 0.55
    assert list(iter_citations(out)) == ["E1"]


@pytest.mark.parametrize(
    ("over", "msg"),
    [
        (
            {"forecasts": [{"event": "beats_benchmark", "horizon_months": 12, "probability": 1.2}]},
            "less than or equal",
        ),
        (
            {"forecasts": [{"event": "doubles", "horizon_months": 36, "probability": 0.1}]},
            "beats_benchmark at 12",
        ),
        (
            {
                "forecasts": [
                    {"event": "beats_benchmark", "horizon_months": 12, "probability": 0.5},
                    {"event": "beats_benchmark", "horizon_months": 12, "probability": 0.6},
                ]
            },
            "duplicate",
        ),
        (
            {
                "thesis_points": [
                    {"claim": "c", "evidence_ids": ["E9"], "direction": "positive", "strength": 1}
                ]
            },
            "not in the packet",
        ),
        (
            {
                "thesis_points": [
                    {"claim": "c", "evidence_ids": [], "direction": "positive", "strength": 1}
                ]
            },
            "at least 1",
        ),
        (
            {
                "thesis_points": [
                    {"claim": "c", "evidence_ids": ["X1"], "direction": "positive", "strength": 1}
                ]
            },
            "malformed",
        ),
        ({"agent": "valuation"}, "agent must be"),
        ({"anon_id": "SEC-2"}, "anon_id must be"),
        ({"bucket": "asymmetric_bet"}, "deterministic tag"),
        ({"flags": ["contamination risk"]}, "UPPER_SNAKE"),
        ({"extra_field": 1}, "Extra inputs"),
    ],
)
def test_invalid_analyst(over: dict[str, Any], msg: str) -> None:
    with pytest.raises(ValidationError, match=msg):
        AnalystOutput.model_validate(analyst(**over), context=CTX)


def test_payoffs_must_sum_to_one_and_be_complete() -> None:
    bad = analyst()
    bad["scenario_payoffs"][0]["probability"] = 0.5
    with pytest.raises(ValidationError, match="sum to"):
        AnalystOutput.model_validate(bad, context=CTX)
    ok = copy.deepcopy(analyst())
    ok["scenario_payoffs"][0]["probability"] = 0.305  # within tolerance
    AnalystOutput.model_validate(ok, context=CTX)
    missing = analyst(scenario_payoffs=analyst()["scenario_payoffs"][:2])
    with pytest.raises(ValidationError, match="bear, base and bull"):
        AnalystOutput.model_validate(missing, context=CTX)


def test_base_rate_and_bear_extras() -> None:
    ctx = {**CTX, "agent": "base_rate"}
    d = analyst(agent="base_rate", reference_class="rc", adjustments=[])
    with pytest.raises(ValidationError, match="base-rate forecasts must include"):
        BaseRateOutput.model_validate(d, context=ctx)
    d["forecasts"] = [
        {"event": "beats_benchmark", "horizon_months": h, "probability": 0.5} for h in (3, 6, 12)
    ] + [{"event": "drawdown_exceeds_30pct", "horizon_months": 12, "probability": 0.1}]
    BaseRateOutput.model_validate(d, context=ctx)
    b = analyst(
        agent="bear",
        premortem="p",
        kill_criteria=[{"observable": "o", "threshold": "t"}],
        shared_evidence=["E7"],
    )
    with pytest.raises(ValidationError, match="E7"):
        BearOutput.model_validate(b, context={**CTX, "agent": "bear"})


def test_macro_modifier_bounds() -> None:
    d = {
        "agent": "macro_scenario",
        "anon_id": "PORTFOLIO",
        "regime": {
            "growth": "up",
            "inflation": "down",
            "real_rate_level": "high",
            "evidence_ids": ["E1"],
        },
        "scenario_losses": [],
        "risk_budget_modifier": 0.4,
        "drivers": [{"driver": "rates", "explanation": "x", "evidence_ids": ["E1"]}],
    }
    with pytest.raises(ValidationError, match=r"greater than or equal to 0\.5"):
        MacroPortfolioOutput.model_validate(d)
    d["risk_budget_modifier"] = 0.75
    MacroPortfolioOutput.model_validate(d)


RISK = RiskEngineView(verdict="VETO", max_size_pct_total=0.0, veto_rule="sector > 40%")
TAX = TaxEngineView(recommended_account="ira", wash_sale_blocked=False, after_tax_hurdle_pct=0.0)


def test_risk_explainer_cannot_change_numbers() -> None:
    d = {
        "agent": "risk_explainer",
        "anon_id": "SEC-1",
        "explanation": "Vetoed.",
        "verdict": "VETO",
        "max_size_pct_total": 0.0,
        "veto_rule": "sector > 40%",
    }
    ctx = {"agent": "risk_explainer", "risk": RISK}
    RiskExplainerOutput.model_validate(d, context=ctx)
    for k, v, msg in (
        ("verdict", "PASS", "verdict"),
        ("max_size_pct_total", 2.0, "max_size"),
        ("veto_rule", "x", "veto_rule"),
    ):
        with pytest.raises(ValidationError, match=msg):
            RiskExplainerOutput.model_validate({**d, k: v}, context=ctx)
    with pytest.raises(ValidationError, match="121 words"):
        RiskExplainerOutput.model_validate({**d, "explanation": "word " * 121}, context=ctx)


def test_tax_explainer_echo() -> None:
    d = {
        "agent": "tax_explainer",
        "anon_id": "SEC-1",
        "explanation": "IRA.",
        "recommended_account": "ira",
        "wash_sale_blocked": False,
        "after_tax_hurdle_pct": 0.0,
    }
    TaxExplainerOutput.model_validate(d, context={"tax": TAX})
    with pytest.raises(ValidationError, match="recommended_account"):
        TaxExplainerOutput.model_validate(
            {**d, "recommended_account": "taxable"}, context={"tax": TAX}
        )
    with pytest.raises(ValidationError, match="wash_sale"):
        TaxExplainerOutput.model_validate({**d, "wash_sale_blocked": True}, context={"tax": TAX})


def behavioral(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent": "behavioral_auditor",
        "anon_id": "SEC-1",
        "severity": "none",
        "cooling_off_hours": 0,
        "requires_written_justification": False,
        "flags": [],
    }
    d.update(over)
    return d


def test_behavioral_consistency() -> None:
    BehavioralOutput.model_validate(behavioral())
    flag = [{"kind": "chasing", "detail": "run", "evidence_ids": ["E1"]}]
    BehavioralOutput.model_validate(
        behavioral(
            severity="stop", cooling_off_hours=72, requires_written_justification=True, flags=flag
        )
    )
    with pytest.raises(ValidationError, match="cooling_off_hours 24"):
        BehavioralOutput.model_validate(
            behavioral(severity="caution", cooling_off_hours=0, flags=flag)
        )
    with pytest.raises(ValidationError, match="written_justification"):
        BehavioralOutput.model_validate(
            behavioral(severity="stop", cooling_off_hours=72, flags=flag)
        )
    with pytest.raises(ValidationError, match="at least one flag"):
        BehavioralOutput.model_validate(behavioral(severity="caution", cooling_off_hours=24))
    with pytest.raises(ValidationError, match="not in the packet"):
        BehavioralOutput.model_validate(
            behavioral(
                severity="caution",
                cooling_off_hours=24,
                flags=[{**flag[0], "evidence_ids": ["E5"]}],
            ),
            context={"evidence_ids": frozenset({"E1"})},
        )
    with pytest.raises(ValidationError):
        BehavioralOutput.model_validate(behavioral(cooling_off_hours=12))


def chair(**over: Any) -> dict[str, Any]:
    d: dict[str, Any] = {
        "agent": "chair",
        "anon_id": "SEC-1",
        "bucket_tag": "CORE_PICK",
        "adjustments": [],
        "bear_response": "answered",
        "forecasts": {
            "p_beat_3m": 0.55,
            "p_beat_6m": 0.57,
            "p_beat_12m": 0.60,
            "p_drawdown_30pct_12m": 0.05,
            "p_doubles_36m": 0.10,
            "p_loses_50pct_36m": 0.03,
        },
        "scenario_payoffs": analyst()["scenario_payoffs"],
        "recommendation": "BUY",
        "size_pct_total": 3.0,
        "thesis": "Good business at a fair price. Insiders are buying.",
        "evidence_ids": ["E1"],
        "falsifiers": [{"observable": "m", "threshold": "t", "check_by": "2027-01-31"}],
        "kill_criteria": ["k"],
        "closing_line": "Research, not advice. The human decides.",
        "flags": [],
    }
    d.update(over)
    return d


def test_chair_schema() -> None:
    ChairOutput.model_validate(chair(), context={"bucket_tag": "CORE_PICK"})
    with pytest.raises(ValidationError, match="deterministic tag"):
        ChairOutput.model_validate(chair(), context={"bucket_tag": "ASYMMETRIC_BET"})
    with pytest.raises(ValidationError, match="closing_line"):
        ChairOutput.model_validate(chair(closing_line="Buy now."))
    with pytest.raises(ValidationError, match="2 sentences"):
        ChairOutput.model_validate(chair(thesis="One. Two. Three."))
    with pytest.raises(ValidationError, match="cannot exceed 1"):
        ChairOutput.model_validate(
            chair(
                forecasts={**chair()["forecasts"], "p_doubles_36m": 0.7, "p_loses_50pct_36m": 0.5}
            )
        )
    with pytest.raises(ValidationError):
        ChairOutput.model_validate(chair(recommendation="STRONG_BUY"))


# --------------------------------------------------------------------- gates
def engines(**over: Any) -> EngineInputs:
    d: dict[str, Any] = {
        "bucket_tag": "CORE_PICK",
        "lottery_filter_pass": None,
        "risk": {"verdict": "PASS", "max_size_pct_total": 3.0},
        "tax": TAX.model_dump(),
        "risk_budget_modifier": 1.0,
    }
    d.update(over)
    return EngineInputs.model_validate(d)


def C(**over: Any) -> ChairOutput:
    return ChairOutput.model_validate(chair(**over))


def test_core_pick_passes() -> None:
    g = apply_gates(C(), engines())
    assert g.recommendation == "BUY" and g.size_pct_total == 3.0 and not g.violations


def test_core_pick_below_threshold_to_watch() -> None:
    f = {**chair()["forecasts"], "p_beat_12m": 0.579}
    g = apply_gates(C(forecasts=f), engines())
    assert g.recommendation == "WATCH" and g.size_pct_total == 0 and g.downgraded
    assert "0.58" in g.violations[0]


def test_risk_veto_to_pass() -> None:
    g = apply_gates(
        C(recommendation="ADD"),
        engines(risk={"verdict": "VETO", "max_size_pct_total": 0, "veto_rule": "r"}),
    )
    assert g.recommendation == "PASS" and "VETO (r)" in g.violations[0]


def test_resize_passes_but_clamps_with_modifier() -> None:
    g = apply_gates(
        C(size_pct_total=3.0),
        engines(risk={"verdict": "RESIZE", "max_size_pct_total": 2.0}, risk_budget_modifier=0.75),
    )
    assert g.recommendation == "BUY" and g.size_pct_total == 1.5 and g.size_clamped
    assert g.size_cap_pct_total == 1.5


def asym(**over: Any) -> EngineInputs:
    base: dict[str, Any] = {
        "bucket_tag": "ASYMMETRIC_BET",
        "lottery_filter_pass": True,
        "risk": {"verdict": "PASS", "max_size_pct_total": 1.0},
    }
    return engines(**{**base, **over})


def asym_chair(**over: Any) -> ChairOutput:
    f = {
        **chair()["forecasts"],
        "p_beat_12m": 0.40,
        "p_doubles_36m": 0.20,
        "p_loses_50pct_36m": 0.10,
    }
    payoffs = [
        {"scenario": "bear", "probability": 0.4, "return_36m": -0.5},
        {"scenario": "base", "probability": 0.4, "return_36m": 0.1},
        {"scenario": "bull", "probability": 0.2, "return_36m": 1.5},
    ]
    return C(
        **{
            "bucket_tag": "ASYMMETRIC_BET",
            "forecasts": f,
            "scenario_payoffs": payoffs,
            "size_pct_total": 1.0,
            **over,
        }
    )


def test_asymmetric_low_hit_rate_ok_when_skew_positive() -> None:
    g = apply_gates(asym_chair(), asym())
    assert g.recommendation == "BUY" and g.expected_value_after_cost == pytest.approx(0.13)


def test_asymmetric_negative_ev_after_cost() -> None:
    payoffs = [
        {"scenario": "bear", "probability": 0.5, "return_36m": -0.4},
        {"scenario": "base", "probability": 0.3, "return_36m": 0.1},
        {"scenario": "bull", "probability": 0.2, "return_36m": 0.85},
    ]  # EV = 0.0 before cost -> -0.01 after
    g = apply_gates(asym_chair(scenario_payoffs=payoffs), asym())
    assert g.recommendation == "WATCH" and "EV > 0" in g.violations[0]


def test_asymmetric_skew_and_lottery() -> None:
    f = {**chair()["forecasts"], "p_doubles_36m": 0.14, "p_loses_50pct_36m": 0.10}
    g = apply_gates(asym_chair(forecasts=f), asym())
    assert g.recommendation == "WATCH" and "1.5 x" in g.violations[0]
    g = apply_gates(asym_chair(), asym(lottery_filter_pass=False))
    assert g.recommendation == "PASS" and "lottery" in g.violations[0]


def test_non_buy_recommendations_untouched() -> None:
    g = apply_gates(
        C(recommendation="HOLD", size_pct_total=2.0),
        engines(risk={"verdict": "VETO", "max_size_pct_total": 0}),
    )
    assert g.recommendation == "HOLD" and not g.downgraded and g.size_pct_total == 0
    g = apply_gates(C(recommendation="TRIM", size_pct_total=1.0), engines())
    assert g.recommendation == "TRIM" and g.size_pct_total == 1.0
