from __future__ import annotations

import math
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from committee.config.loader import load_config
from committee.config.schema import Scenario, ScenariosConfig
from committee.domain import Holding
from committee.engines.scenario import (
    FactorSensitivity,
    ScenarioLoss,
    holding_scenario_return,
    risk_budget_modifier,
    run_scenarios,
    satellite_vol,
    scenario_losses,
)
from committee.engines.scenario.holdings_io import demo_portfolio
from committee.engines.scenario.vol import (
    DEFAULT_NAME_VOL,
    basket,
    component_contributions,
    constant_corr_vol,
)

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config")


def scen(
    sid: str = "s",
    p: float = 0.1,
    shocks: dict[str, float] | None = None,
    themes: dict[str, float] | None = None,
) -> Scenario:
    return Scenario(
        id=sid,
        name=sid.upper(),
        definition="test",
        probability=p,
        shocks=shocks if shocks is not None else {"market": -0.10},
        theme_shocks=themes or {},
    )


def hold(symbol: str, value: float, sleeve: str = "satellite", **kw: object) -> Holding:
    return Holding.model_validate(
        {
            "account": "ira",
            "symbol": symbol,
            "qty": value / 10.0,
            "price": 10.0,
            "sleeve": sleeve,
            **kw,
        }
    )


def loss(sid: str, sat_return: float, p: float = 0.1) -> ScenarioLoss:
    return ScenarioLoss(
        scenario_id=sid, name=sid, probability=p, total_return=0.0, satellite_return=sat_return
    )


# ------------------------------------------------------------------ returns
def test_holding_return_arithmetic() -> None:
    sens = FactorSensitivity(
        beta_market=1.2,
        beta_rates_100bp=-0.05,
        beta_oil=0.3,
        beta_dollar=-0.5,
        themes={"ai_capex_chain": 0.5, "unused": 1.0},
    )
    sc = scen(
        shocks={"market": -0.10, "rates_10y_bp": 150, "oil": 0.2, "dollar": 0.04},
        themes={"ai_capex_chain": -0.30},
    )
    expected = 1.2 * -0.10 + -0.05 * 1.5 + 0.3 * 0.2 + -0.5 * 0.04 + 0.5 * -0.30
    assert holding_scenario_return(sens, sc) == pytest.approx(expected)


def test_downside_beta_only_in_down_markets() -> None:
    trend = FactorSensitivity(beta_market=0.0, beta_market_down=-0.2)
    assert holding_scenario_return(trend, scen(shocks={"market": -0.20})) == pytest.approx(0.04)
    assert holding_scenario_return(trend, scen(shocks={"market": 0.10})) == 0.0
    assert holding_scenario_return(trend, scen(shocks={})) == 0.0


def test_scenario_losses_total_and_satellite() -> None:
    holdings = [hold("CORE", 800.0, sleeve="us_total"), hold("SAT", 200.0)]
    sens = {
        "CORE": FactorSensitivity(beta_market=1.0),
        "SAT": FactorSensitivity(beta_market=2.0),
    }
    [res] = scenario_losses(holdings, sens, [scen()], total_value=2000.0)
    # pnl: core -80, sat -40 -> total -120 / 2000; satellite -40 / 200
    assert res.total_return == pytest.approx(-0.06)
    assert res.satellite_return == pytest.approx(-0.20)
    assert res.total_loss == pytest.approx(0.06)
    assert res.satellite_loss == pytest.approx(0.20)
    assert res.contributions == pytest.approx({"CORE": -0.04, "SAT": -0.02})


def test_scenario_losses_defaults_and_errors() -> None:
    holdings = [hold("CORE", 100.0, sleeve="us_total")]
    sens = {"CORE": FactorSensitivity(beta_market=1.0)}
    [res] = scenario_losses(holdings, sens, [scen()])
    assert res.total_return == pytest.approx(-0.10)
    assert res.satellite_return == 0.0  # no satellite
    with pytest.raises(ValueError, match="smaller"):
        scenario_losses(holdings, sens, [scen()], total_value=50.0)
    [empty] = scenario_losses([], {}, [scen()])
    assert empty.total_return == 0.0


# ---------------------------------------------------------------- modifier
def test_modifier_no_breach_is_one() -> None:
    m, drivers = risk_budget_modifier([loss("a", -0.30)], 0.20, 2.0, 0.5)
    assert m == 1.0 and drivers == []
    # exactly at the threshold is not a breach
    m, _ = risk_budget_modifier([loss("a", -0.40)], 0.20, 2.0, 0.5)
    assert m == 1.0


def test_modifier_probability_weighted_reduction() -> None:
    # threshold 0.40; loss 0.80 -> excess 1.0; p=0.25 -> cut 0.25
    m, drivers = risk_budget_modifier([loss("a", -0.80, p=0.25)], 0.20, 2.0, 0.5)
    assert m == pytest.approx(0.75)
    assert drivers[0].reduction == pytest.approx(0.25)
    assert drivers[0].scenario_id == "a"
    assert "exceeds" in drivers[0].text


def test_modifier_min_cut_and_floor() -> None:
    # tiny probability still cuts by min_cut
    m, _ = risk_budget_modifier([loss("a", -0.41, p=0.01)], 0.20, 2.0, 0.5)
    assert m == pytest.approx(0.95)
    many = [loss(str(i), -0.9, p=0.5) for i in range(5)]
    m, drivers = risk_budget_modifier(many, 0.20, 2.0, 0.5)
    assert m == 0.5
    assert all(d.reduction <= 0.5 for d in drivers)


def test_modifier_zero_vol_threshold() -> None:
    m, drivers = risk_budget_modifier([loss("a", -0.01, p=0.1)], 0.0, 2.0, 0.6)
    assert m == pytest.approx(0.6)
    assert drivers[0].reduction == pytest.approx(0.4)
    with pytest.raises(ValueError):
        risk_budget_modifier([], 0.2, 2.0, 1.5)


@given(
    st.lists(st.tuples(st.floats(-1.0, 1.0), st.floats(0.0, 1.0)), min_size=0, max_size=10),
    st.floats(0.0, 1.0),
    st.floats(0.5, 1.0),
    st.integers(0, 9),
    st.floats(0.0, 0.5),
)
def test_modifier_bounds_and_monotonic(
    rows: list[tuple[float, float]], vol: float, floor: float, idx: int, worse: float
) -> None:
    losses = [loss(str(i), r, p) for i, (r, p) in enumerate(rows)]
    m, _ = risk_budget_modifier(losses, vol, 2.0, floor)
    assert floor <= m <= 1.0
    if losses:
        i = idx % len(losses)
        bumped = list(losses)
        bumped[i] = loss(str(i), losses[i].satellite_return - worse, losses[i].probability)
        m2, _ = risk_budget_modifier(bumped, vol, 2.0, floor)
        assert m2 <= m + 1e-12  # a bigger loss never raises the budget


# ------------------------------------------------------------- full run
def test_run_scenarios_demo_portfolio() -> None:
    holdings = demo_portfolio(CFG.policy_portfolio)
    report = run_scenarios(holdings, CFG.scenarios, policy=CFG.policy_portfolio)
    assert [x.scenario_id for x in report.losses] == [s.id for s in CFG.scenarios.scenarios]
    assert report.total_value == pytest.approx(1_000_000.0)
    assert report.satellite_value == pytest.approx(200_000.0)
    assert 0.5 <= report.modifier <= 1.0
    assert report.loss_threshold == pytest.approx(2.0 * report.satellite_expected_vol)
    rate = report.loss_for("rate_shock")
    assert rate.total_loss > 0.1  # equity-heavy book loses in a rate shock
    rally = report.loss_for("disinflation_rally")
    assert rally.total_return > 0
    with pytest.raises(KeyError):
        report.loss_for("nope")
    breaching = {x.scenario_id for x in report.losses if x.breaches_budget}
    assert breaching == {d.scenario_id for d in report.drivers}


def test_run_scenarios_low_vol_satellite_breaches() -> None:
    sat = [
        hold(
            "SEMI",
            1000.0,
            sector="Information Technology",
            bucket="core_pick",
            themes={"ai_capex_chain": 1.0, "taiwan_supply_chain": 1.0},
            annual_vol=0.10,
        ),
    ]
    cfg = ScenariosConfig(
        scenarios=CFG.scenarios.scenarios, loss_vol_multiple=2.0, modifier_floor=0.5
    )
    report = run_scenarios(sat, cfg, total_value=10_000.0)
    assert report.satellite_expected_vol == pytest.approx(0.10)
    assert report.modifier < 1.0
    assert report.loss_for("taiwan_supply_freeze").breaches_budget
    assert report.loss_for("disinflation_rally").breaches_budget is False


def test_run_scenarios_with_estimates_overrides_default() -> None:
    h = [hold("X", 100.0, sleeve="us_total")]
    est = {"X": FactorSensitivity(beta_market=0.0, source="estimated")}
    report = run_scenarios(h, CFG.scenarios, estimated=est)
    assert all(x.total_return == 0.0 for x in report.losses)


# -------------------------------------------------------------------- vol
def test_constant_corr_vol_limits() -> None:
    w = {"a": 0.5, "b": 0.5}
    v = {"a": 0.2, "b": 0.4}
    assert constant_corr_vol(w, v, 1.0) == pytest.approx(0.3)
    assert constant_corr_vol(w, v, 0.0) == pytest.approx(math.sqrt(0.01 + 0.04))
    rho = 0.3
    expected = math.sqrt(0.01 + 0.04 + 2 * rho * 0.5 * 0.5 * 0.2 * 0.4)
    assert constant_corr_vol(w, v, rho) == pytest.approx(expected)
    with pytest.raises(ValueError):
        constant_corr_vol(w, v, 1.5)


@given(
    st.lists(st.tuples(st.floats(0.0, 1.0), st.floats(0.01, 2.0)), min_size=1, max_size=8),
    st.floats(0.0, 1.0),
)
def test_contributions_sum_to_vol(rows: list[tuple[float, float]], rho: float) -> None:
    w = {str(i): r[0] for i, r in enumerate(rows)}
    v = {str(i): r[1] for i, r in enumerate(rows)}
    vol = constant_corr_vol(w, v, rho)
    contrib = component_contributions(w, v, rho)
    assert sum(contrib.values()) == pytest.approx(vol, abs=1e-9)
    assert all(c >= -1e-12 for c in contrib.values())


def test_basket_defaults_and_aggregation() -> None:
    holdings = [
        hold("A", 100.0, bucket="core_pick"),
        hold("A", 100.0, bucket="core_pick", annual_vol=0.5),
        hold("B", 200.0, bucket="asymmetric_bet"),
        hold("C", 0.0, sleeve="speculative"),
        hold("D", 100.0, sleeve="other"),
    ]
    w, v = basket(holdings)
    assert w["A"] == pytest.approx(0.4)
    assert v["A"] == 0.5
    assert v["B"] == DEFAULT_NAME_VOL["asymmetric_bet"]
    assert v["C"] == DEFAULT_NAME_VOL["speculative"]
    assert v["D"] == DEFAULT_NAME_VOL["other"]
    assert basket([]) == ({}, {})
    assert satellite_vol([]) == 0.0
    assert component_contributions({"a": 0.0}, {"a": 0.3}, 0.3) == {"a": 0.0}
