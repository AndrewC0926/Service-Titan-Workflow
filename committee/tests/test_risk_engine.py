from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pytest

from committee.config.loader import load_config
from committee.domain import Holding, Trade
from committee.engines.risk import (
    Outcome,
    PortfolioState,
    Proposal,
    RiskResult,
    RiskSettings,
    evaluate,
    turnover_from_trades,
)
from committee.engines.scenario import run_scenarios
from committee.engines.scenario.holdings_io import demo_portfolio

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config")
LIMITS = CFG.risk_limits
LOOK = CFG.policy_portfolio.lookthrough
AS_OF = dt.date(2026, 10, 1)
TOTAL = 1_000_000.0  # satellite capital S = 200,000 (20%)

GOOD_OUTCOMES = [
    Outcome(label="bear", probability=0.2, ret=-0.30),
    Outcome(label="base", probability=0.5, ret=0.10),
    Outcome(label="bull", probability=0.3, ret=0.60),
]


def prop(**kw: Any) -> Proposal:
    base: dict[str, Any] = {
        "symbol": "NEW",
        "side": "buy",
        "bucket": "core_pick",
        "sector": "Health Care",
        "themes": {},
        "market_cap_usd": 50e9,
        "adv_usd": 500e6,
        "annual_vol": 0.30,
        "price": 50.0,
        "p_beat_12m": 0.6,
        "outcomes": GOOD_OUTCOMES,
        "size_pct_total": 2.0,
    }
    base.update(kw)
    return Proposal.model_validate(base)


def sat(symbol: str, value: float, **kw: Any) -> Holding:
    base: dict[str, Any] = {
        "account": "ira",
        "symbol": symbol,
        "qty": value / 100.0,
        "price": 100.0,
        "sleeve": "satellite",
        "bucket": "core_pick",
        "sector": "Industrials",
        "annual_vol": 0.30,
    }
    base.update(kw)
    return Holding.model_validate(base)


def core(symbol: str, value: float, sleeve: str) -> Holding:
    return Holding(account="taxable", symbol=symbol, qty=value / 100.0, price=100.0, sleeve=sleeve)


def state(holdings: list[Holding] | None = None, **kw: Any) -> PortfolioState:
    base: dict[str, Any] = {"holdings": holdings or [], "total_value": TOTAL, "as_of": AS_OF}
    base.update(kw)
    return PortfolioState.model_validate(base)


def run(p: Proposal, s: PortfolioState | None = None, **kw: Any) -> RiskResult:
    return evaluate(p, s or state(), LIMITS, lookthrough=LOOK, **kw)


def rules(r: RiskResult) -> set[str]:
    return {c.rule_id for c in r.binding}


# ------------------------------------------------------------------ basics
def test_pass_within_all_limits() -> None:
    r = run(prop())
    assert r.verdict == "PASS"
    assert r.max_order_pct_total == pytest.approx(2.0)
    assert r.max_order_usd == pytest.approx(20_000.0)
    assert r.max_order_shares == 400
    assert r.post_trade_position_pct_total == pytest.approx(2.0)
    assert r.binding == [] and r.veto is None
    assert r.kelly is not None and r.kelly.ceiling_pct_total > 2.0
    assert {c.rule_id for c in r.caps} >= {
        "position.kelly_fraction_cap",
        "buckets.core_pick.default_initial_pct_total",
        "buckets.core_pick.max_pct_total",
        "buckets.core_pick.max_pct_satellite",
        "concentration.max_sector_pct_satellite",
        "liquidity.max_position_pct_of_adv",
    }
    # single name satellite: its contribution is the whole satellite vol
    assert r.satellite_vol_before == 0.0
    assert r.satellite_vol_after == pytest.approx(0.30)
    assert r.marginal_vol_contribution == pytest.approx(0.30)
    assert r.risk_budget_modifier == 1.0


def test_shares_round_down() -> None:
    r = run(prop(price=333.0))
    assert r.max_order_shares == 60  # 20,000 / 333 = 60.06


def test_default_initial_size_binds_for_new_position() -> None:
    r = run(prop(size_pct_total=5.0))
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(3.0)
    assert rules(r) == {"buckets.core_pick.default_initial_pct_total"}


def test_asymmetric_default_initial_size() -> None:
    r = run(
        prop(
            bucket="asymmetric_bet",
            market_cap_usd=1e9,
            adv_usd=10e6,
            size_pct_total=2.0,
            annual_vol=0.5,
        )
    )
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert "buckets.asymmetric_bet.default_initial_pct_total" in rules(r)


def test_kelly_ceiling_binds() -> None:
    thin = [
        Outcome(label="bear", probability=0.45, ret=-0.20),
        Outcome(label="bull", probability=0.55, ret=0.20),
    ]
    r = run(prop(outcomes=thin, p_beat_12m=0.55))
    # full Kelly 0.5 (=(0.55*0.2-0.45*0.2)/(0.2*0.2)) -> half Kelly 25%... discrete check:
    assert r.kelly is not None
    assert r.kelly.f_binary == pytest.approx(0.55 / 0.2 - 0.45 / 0.2)
    tiny = [
        Outcome(label="bear", probability=0.49, ret=-0.5),
        Outcome(label="bull", probability=0.51, ret=0.5),
    ]
    r = run(prop(outcomes=tiny, p_beat_12m=0.51, size_pct_total=3.0))
    assert r.kelly is not None
    assert r.kelly.ceiling_pct_total == pytest.approx(2.0, abs=1e-6)  # 0.5 * 0.04
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(2.0, abs=1e-6)
    assert rules(r) == {"position.kelly_fraction_cap"}


def test_kelly_ceiling_counts_existing_position() -> None:
    tiny = [
        Outcome(label="bear", probability=0.49, ret=-0.5),
        Outcome(label="bull", probability=0.51, ret=0.5),
    ]
    s = state([sat("NEW", 15_000.0, sector="Health Care")])
    r = run(prop(outcomes=tiny, p_beat_12m=0.51, size_pct_total=1.0), s)
    assert r.max_order_pct_total == pytest.approx(0.5, abs=1e-6)
    assert r.post_trade_position_pct_total == pytest.approx(2.0, abs=1e-6)


def test_no_edge_vetoes() -> None:
    bad = [
        Outcome(label="bear", probability=0.6, ret=-0.3),
        Outcome(label="bull", probability=0.4, ret=0.2),
    ]
    r = run(prop(outcomes=bad, p_beat_12m=0.4))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "position.kelly_fraction_cap"
    assert r.max_order_pct_total == 0.0 and r.max_order_shares == 0
    r = run(prop(outcomes=[]))
    assert r.verdict == "VETO" and r.veto is not None and "payoff" in r.veto.text


def test_vol_scaling_new_positions() -> None:
    r = run(prop(annual_vol=0.70, size_pct_total=3.0))
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(1.5)  # 3% x 0.35 / 0.70
    assert rules(r) == {"volatility.vol_scale_new_positions"}
    # never scales up
    r = run(prop(annual_vol=0.10, size_pct_total=3.0))
    assert r.max_order_pct_total == pytest.approx(3.0)
    # off when disabled, and not applied when adding to a position
    lim = LIMITS.model_copy(
        update={
            "volatility": LIMITS.volatility.model_copy(update={"vol_scale_new_positions": False})
        }
    )
    r = evaluate(prop(annual_vol=0.70, size_pct_total=3.0), state(), lim)
    assert r.max_order_pct_total == pytest.approx(3.0)
    s = state([sat("NEW", 10_000.0, sector="Health Care")])
    r = run(prop(annual_vol=0.70, size_pct_total=3.0), s)
    assert r.max_order_pct_total == pytest.approx(3.0)


def test_custom_vol_targets() -> None:
    r = run(
        prop(annual_vol=0.60, size_pct_total=3.0),
        settings=RiskSettings(vol_targets={"core_pick": 0.30}),
    )
    assert r.max_order_pct_total == pytest.approx(1.5)


# ------------------------------------------------------------ hard caps
def test_bucket_max_pct_total() -> None:
    s = state([sat("NEW", 50_000.0, sector="Health Care")], satellite_target_pct=35.0)
    r = run(prop(size_pct_total=3.0), s)
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert rules(r) == {"buckets.core_pick.max_pct_total"}
    assert r.post_trade_position_pct_total == pytest.approx(6.0)


def test_core_max_pct_satellite() -> None:
    # S = 10% of 1M = 100k; 30% of S = 30k; existing 20k -> room 10k = 1%
    s = state([sat("NEW", 20_000.0, sector="Health Care")], satellite_target_pct=10.0)
    r = run(prop(size_pct_total=3.0), s)
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert rules(r) == {"buckets.core_pick.max_pct_satellite"}


def test_asymmetric_bucket_pct_satellite() -> None:
    asym = [sat(f"A{i}", 15_000.0, bucket="asymmetric_bet", sector="Health Care") for i in range(5)]
    # 75k of 200k = 37.5%; room to 40% = 5k = 0.5%
    p = prop(
        bucket="asymmetric_bet",
        market_cap_usd=1e9,
        adv_usd=10e6,
        annual_vol=0.5,
        size_pct_total=1.0,
        sector="Energy",
    )
    r = run(p, state(asym))
    assert r.max_order_pct_total == pytest.approx(0.5)
    assert rules(r) == {"buckets.asymmetric_bet.max_bucket_pct_satellite"}


def test_asymmetric_max_names_veto() -> None:
    asym = [sat(f"A{i}", 1_000.0, bucket="asymmetric_bet", sector="Energy") for i in range(10)]
    p = prop(
        bucket="asymmetric_bet",
        market_cap_usd=1e9,
        adv_usd=10e6,
        annual_vol=0.5,
        size_pct_total=0.5,
    )
    r = run(p, state(asym))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "buckets.asymmetric_bet.max_names"
    # adding to an existing asymmetric name is fine
    r = run(p.model_copy(update={"symbol": "A3"}), state(asym))
    assert r.verdict != "VETO"


def test_sector_cap() -> None:
    held = [sat("H1", 50_000.0, sector="Health Care"), sat("H2", 28_000.0, sector="healthcare")]
    r = run(prop(size_pct_total=1.0), state(held))  # 78k of 80k used
    assert r.max_order_pct_total == pytest.approx(0.2)
    assert rules(r) == {"concentration.max_sector_pct_satellite"}


def test_sector_full_vetoes_with_rule() -> None:
    held = [sat("H1", 40_000.0, sector="Health Care"), sat("H2", 40_000.0, sector="Health Care")]
    r = run(prop(size_pct_total=1.0), state(held))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "concentration.max_sector_pct_satellite"


def test_theme_cap_scaled_by_exposure() -> None:
    held = [sat("AI1", 68_000.0, themes={"ai_capex_chain": 1.0})]  # 34% of S
    r = run(prop(themes={"ai_capex_chain": 0.5}, size_pct_total=1.0), state(held))
    # room 2k of theme dollars / 0.5 exposure = 4k = 0.4%
    assert r.max_order_pct_total == pytest.approx(0.4)
    assert rules(r) == {"concentration.max_theme_pct_satellite.ai_capex_chain"}
    assert r.theme_exposure["ai_capex_chain"].satellite_pct == pytest.approx(35.0)


def test_max_names_satellite_veto() -> None:
    held = [sat(f"N{i}", 1_000.0, sector=f"S{i}") for i in range(25)]
    r = run(prop(), state(held))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "position.max_names_satellite"
    r = run(prop(symbol="N0", sector="S0"), state(held))
    assert r.verdict == "PASS"


def test_liquidity_cap() -> None:
    p = prop(adv_usd=25e6, size_pct_total=3.0)  # 1% ADV = 250k
    r = evaluate(p, state(total_value=50_000_000.0), LIMITS)
    assert r.max_order_pct_total == pytest.approx(0.5)
    assert rules(r) == {"liquidity.max_position_pct_of_adv"}


def test_speculative_cap() -> None:
    spec = [sat("BTCETF", 25_000.0, sleeve="speculative", bucket=None, sector="Crypto")]
    p = prop(is_speculative=True, symbol="ETHETF", sector="Crypto", size_pct_total=1.0)
    r = run(p, state(spec))
    assert r.max_order_pct_total == pytest.approx(0.5)
    assert rules(r) == {"account.speculative_sleeve_max_pct"}


# --------------------------------------------------------- eligibility
@pytest.mark.parametrize(
    ("kw", "rule"),
    [
        ({"annual_vol": 0.71}, "buckets.core_pick.max_name_annual_vol"),
        ({"market_cap_usd": 1.9e9}, "buckets.core_pick.min_market_cap_usd"),
        ({"adv_usd": 19e6}, "buckets.core_pick.min_adv_usd"),
        (
            {"bucket": "asymmetric_bet", "annual_vol": 1.01, "market_cap_usd": 1e9, "adv_usd": 5e6},
            "buckets.asymmetric_bet.max_name_annual_vol",
        ),
        (
            {"bucket": "asymmetric_bet", "market_cap_usd": 2.9e8, "adv_usd": 5e6},
            "buckets.asymmetric_bet.min_market_cap_usd",
        ),
        (
            {"bucket": "asymmetric_bet", "market_cap_usd": 1e9, "adv_usd": 2.9e6},
            "buckets.asymmetric_bet.min_adv_usd",
        ),
        ({"instrument": "option"}, "prohibited.options"),
        ({"instrument": "future"}, "prohibited.leverage"),
        ({"leverage": 2.0}, "prohibited.leverage"),
        ({"uses_margin": True}, "prohibited.margin"),
        ({"size_pct_total": 0.0}, "proposal.zero_size"),
    ],
)
def test_veto_rules(kw: dict[str, Any], rule: str) -> None:
    r = run(prop(**kw))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == rule
    assert r.binding == [r.veto]
    assert r.max_order_pct_total == 0.0


def test_proposal_validation() -> None:
    with pytest.raises(ValueError):
        prop(themes={"x": 1.5})
    with pytest.raises(ValueError):
        prop(outcomes=[Outcome(label="a", probability=0.5, ret=0.1)])


# ------------------------------------------------------------- modifier
def test_modifier_scales_and_binds() -> None:
    r = run(prop(size_pct_total=2.0), modifier=0.5)
    assert r.verdict == "RESIZE"
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert rules(r) == {"scenario.risk_budget_modifier"}
    assert r.risk_budget_modifier == 0.5
    with pytest.raises(ValueError):
        run(prop(), modifier=1.2)


def test_scenario_report_wiring() -> None:
    holdings = demo_portfolio(CFG.policy_portfolio)
    report = run_scenarios(holdings, CFG.scenarios, policy=CFG.policy_portfolio)
    forced = report.model_copy(update={"modifier": 0.8})
    r = run(prop(size_pct_total=2.0), state(holdings), scenario_report=forced)
    assert r.risk_budget_modifier == 0.8
    assert r.max_order_pct_total == pytest.approx(1.6)
    assert r.scenario_losses == report.losses
    # explicit modifier overrides the report
    r = run(prop(size_pct_total=2.0), state(holdings), scenario_report=forced, modifier=1.0)
    assert r.max_order_pct_total == pytest.approx(2.0)


def test_scenario_breach_flags_pass_through() -> None:
    from committee.config.schema import ScenariosConfig

    holdings = demo_portfolio(CFG.policy_portfolio)
    tight = ScenariosConfig(
        scenarios=CFG.scenarios.scenarios, loss_vol_multiple=0.5, modifier_floor=0.5
    )
    report = run_scenarios(holdings, tight, policy=CFG.policy_portfolio)
    assert report.drivers
    r = run(prop(), state(holdings), scenario_report=report)
    assert any(f.rule_id.startswith("scenario.budget_breach.") for f in r.flags)
    assert r.risk_budget_modifier == report.modifier < 1.0


# ---------------------------------------------------------------- sells
def test_sell_pass() -> None:
    s = state([sat("OLD", 30_000.0, opened_on=dt.date(2025, 1, 1))])
    r = run(prop(symbol="OLD", side="sell", size_pct_total=1.0), s)
    assert r.verdict == "PASS"
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert r.post_trade_position_pct_total == pytest.approx(2.0)
    assert r.satellite_vol_after == pytest.approx(0.30)


def test_short_sale_veto() -> None:
    s = state([sat("OLD", 10_000.0, opened_on=dt.date(2025, 1, 1))])
    r = run(prop(symbol="OLD", side="sell", size_pct_total=1.5), s)
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "prohibited.short_selling"
    r = run(prop(symbol="NOPE", side="sell", size_pct_total=0.1), s)
    assert r.veto is not None and r.veto.rule_id == "prohibited.short_selling"
    # selling exactly the full position is allowed
    r = run(prop(symbol="OLD", side="sell", size_pct_total=1.0), s)
    assert r.verdict == "PASS" and r.post_trade_position_pct_total == pytest.approx(0.0)


def test_min_holding_period() -> None:
    s = state([sat("OLD", 30_000.0, opened_on=AS_OF - dt.timedelta(days=30))])
    r = run(prop(symbol="OLD", side="sell", size_pct_total=1.0), s)
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "portfolio.min_holding_days"
    r = run(prop(symbol="OLD", side="sell", size_pct_total=1.0, thesis_break_logged=True), s)
    assert r.verdict == "PASS"
    s90 = state([sat("OLD", 30_000.0, opened_on=AS_OF - dt.timedelta(days=90))])
    assert run(prop(symbol="OLD", side="sell", size_pct_total=1.0), s90).verdict == "PASS"


def test_turnover_budget() -> None:
    h = [sat("OLD", 30_000.0, opened_on=dt.date(2025, 1, 1))]
    # S = 200k; selling 1% total = 10k = 5% of S; 46% + 5% > 50%
    r = run(
        prop(symbol="OLD", side="sell", size_pct_total=1.0), state(h, trailing_turnover_pct=46.0)
    )
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "portfolio.turnover_budget_satellite_annual_pct"
    r = run(
        prop(symbol="OLD", side="sell", size_pct_total=1.0), state(h, trailing_turnover_pct=45.0)
    )
    assert r.verdict == "PASS"


def test_core_sell_skips_turnover_and_updates_lookthrough() -> None:
    h = [core("VTI", 400_000.0, "us_total")]
    s = state(h, trailing_turnover_pct=100.0)
    r = run(prop(symbol="VTI", side="sell", size_pct_total=10.0), s)
    assert r.verdict == "PASS"
    # 300k VTI left x 0.22 ai look-through = 6.6% of total
    assert r.theme_exposure["ai_capex_chain"].total_pct == pytest.approx(6.6)
    assert r.satellite_vol_after == 0.0


def test_satellite_sell_reduces_theme_exposure() -> None:
    h = [sat("AI1", 40_000.0, themes={"ai_capex_chain": 0.5}, opened_on=dt.date(2025, 1, 1))]
    r = run(prop(symbol="AI1", side="sell", size_pct_total=2.0), state(h))
    # half the position sold: 20k x 0.5 = 10k of 200k S = 5%
    assert r.theme_exposure["ai_capex_chain"].satellite_pct == pytest.approx(5.0)


def test_sell_prohibited_instrument() -> None:
    r = run(prop(side="sell", instrument="option"))
    assert r.veto is not None and r.veto.rule_id == "prohibited.options"


# ---------------------------------------------------------------- flags
def test_theme_lookthrough_flag() -> None:
    h = [
        core("VTI", 330_000.0, "us_total"),
        core("VWO", 100_000.0, "em"),
        sat("AI1", 60_000.0, themes={"ai_capex_chain": 1.0}),
    ]
    r = run(prop(themes={"ai_capex_chain": 1.0}), state(h))
    te = r.theme_exposure["ai_capex_chain"]
    # theme cap: 60k of 70k room used -> order 10k (1%)
    assert r.max_order_pct_total == pytest.approx(1.0)
    # core: 330k*.22 + 100k*.12 = 84.6k; sat 60k + 10k -> 154.6k / 1M
    assert te.total_pct == pytest.approx(15.46)
    assert te.satellite_pct == pytest.approx(35.0)
    assert not any(f.rule_id.startswith("concentration.flag_theme") for f in r.flags)
    big = [core("VWO", 900_000.0, "em")]
    r = run(prop(), state(big))
    flagged = {f.rule_id for f in r.flags}
    assert "concentration.flag_theme_lookthrough_pct_total.taiwan_supply_chain" not in flagged
    assert r.theme_exposure["china_revenue"].total_pct == pytest.approx(25.2)
    heavy = [sat("CN", 200_000.0, themes={"china_revenue": 1.0}), core("VWO", 700_000.0, "em")]
    r = run(prop(), state(heavy))
    assert "concentration.flag_theme_lookthrough_pct_total.china_revenue" in {
        f.rule_id for f in r.flags
    }


def test_drawdown_review_flags_never_sell() -> None:
    s = state(satellite_drawdown_pct=30.0, total_drawdown_pct=35.0)
    r = run(prop(), s)
    ids = {f.rule_id for f in r.flags}
    assert "portfolio.max_satellite_drawdown_review_pct" in ids
    assert "portfolio.max_total_drawdown_review_pct" in ids
    assert r.verdict == "PASS"
    r = run(prop(), state(satellite_drawdown_pct=29.9, total_drawdown_pct=10.0))
    assert not {f.rule_id for f in r.flags} & ids


def test_min_names_flag() -> None:
    held = [sat(f"N{i}", 40_000.0, sector=f"S{i}") for i in range(5)]  # 200k, fully invested
    r = run(prop(symbol="N0", sector="S0", size_pct_total=0.1), state(held))
    assert "position.min_names_satellite" in {f.rule_id for f in r.flags}


def test_marginal_vol_contribution() -> None:
    held = [sat("A", 50_000.0, annual_vol=0.30), sat("B", 50_000.0, annual_vol=0.30)]
    r = run(prop(annual_vol=0.30, size_pct_total=1.0), state(held))
    # three names: weights 50/50/10 of 110 -> vol from constant-corr formula
    assert r.satellite_vol_after < r.satellite_vol_before + 1e-12
    assert 0 < r.marginal_vol_contribution < r.satellite_vol_after
    r_custom = run(
        prop(annual_vol=0.30, size_pct_total=1.0), state(held), settings=RiskSettings(rho=1.0)
    )
    assert r_custom.satellite_vol_after == pytest.approx(0.30)


# -------------------------------------------------------------- turnover
def test_turnover_from_trades() -> None:
    def t(i: int, side: str, sym: str, day: dt.date, qty: float = 100.0) -> Trade:
        return Trade(
            trade_id=str(i),
            account="ira",
            symbol=sym,
            side=side,
            qty=qty,
            price=100.0,
            traded_on=day,
        )  # type: ignore[arg-type]

    trades = [
        t(1, "sell", "A", AS_OF - dt.timedelta(days=10)),
        t(2, "sell", "A", AS_OF - dt.timedelta(days=365)),  # outside window
        t(3, "buy", "A", AS_OF - dt.timedelta(days=5)),
        t(4, "sell", "VTI", AS_OF - dt.timedelta(days=5)),  # not satellite
        t(5, "sell", "B", AS_OF),
        t(6, "sell", "B", AS_OF + dt.timedelta(days=1)),  # future
    ]
    pct = turnover_from_trades(trades, {"A", "B"}, AS_OF, 100_000.0)
    assert pct == pytest.approx(20.0)
    with pytest.raises(ValueError):
        turnover_from_trades(trades, {"A"}, AS_OF, 0.0)


def test_satellite_target_defaults_from_limits() -> None:
    held = [sat("NEW", 50_000.0, sector="Health Care")]
    # default target 20% -> S=200k; 30% S = 60k -> room 10k, bucket max 6% -> room 10k
    r = run(prop(size_pct_total=3.0), state(held))
    assert r.max_order_pct_total == pytest.approx(1.0)
    assert rules(r) == {"buckets.core_pick.max_pct_total", "buckets.core_pick.max_pct_satellite"}


def test_zero_modifier_vetoes_with_modifier_rule() -> None:
    r = run(prop(size_pct_total=5.0), modifier=0.0)
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "scenario.risk_budget_modifier"


def test_tiny_request_vetoes_as_zero_size() -> None:
    r = run(prop(size_pct_total=1e-12))
    assert r.verdict == "VETO"
    assert r.veto is not None and r.veto.rule_id == "proposal.zero_size"
