"""Property-based tests: no risk-engine output ever exceeds any limit."""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from committee.config.loader import load_config
from committee.domain import Holding
from committee.engines.risk import Outcome, PortfolioState, Proposal, build_book, evaluate
from committee.engines.risk.exposure import sector_key

ROOT = Path(__file__).resolve().parents[1]
CFG = load_config(ROOT / "config")
LIMITS = CFG.risk_limits
LOOK = CFG.policy_portfolio.lookthrough
THEMES = list(LIMITS.concentration.themes)
SECTORS = ["Energy", "Information Technology", "Tech", "Health Care", "Utilities"]
SYMBOLS = [f"S{i}" for i in range(40)]
AS_OF = dt.date(2026, 10, 1)
CORE = [("VTI", "us_total"), ("VEA", "dev_exus"), ("VWO", "em"), ("AVUV", "us_scv")]

themes_st = st.dictionaries(st.sampled_from(THEMES), st.floats(0.0, 1.0), max_size=3)


@st.composite
def satellite_holding(draw: st.DrawFn, total: float) -> Holding:
    spec = draw(st.booleans().map(lambda b: b and draw(st.integers(0, 9)) == 0))
    value = draw(st.floats(0.0, 0.06)) * total
    return Holding.model_validate(
        {
            "account": draw(st.sampled_from(["ira", "taxable"])),
            "symbol": draw(st.sampled_from(SYMBOLS)),
            "qty": value / 50.0,
            "price": 50.0,
            "sleeve": "speculative" if spec else "satellite",
            "bucket": draw(st.sampled_from(["core_pick", "asymmetric_bet"])),
            "sector": draw(st.sampled_from(SECTORS)),
            "themes": draw(themes_st),
            "annual_vol": draw(st.one_of(st.none(), st.floats(0.05, 1.2))),
            "opened_on": AS_OF - dt.timedelta(days=draw(st.integers(0, 800))),
        }
    )


@st.composite
def portfolio(draw: st.DrawFn) -> PortfolioState:
    total = draw(st.floats(1e5, 5e7))
    sat = draw(st.lists(satellite_holding(total), max_size=30))
    core = [
        Holding(
            account="taxable",
            symbol=s,
            qty=draw(st.floats(0, 0.4)) * total / 100,
            price=100.0,
            sleeve=sl,
        )
        for s, sl in draw(st.lists(st.sampled_from(CORE), max_size=4, unique=True))
    ]
    return PortfolioState(
        holdings=[*sat, *core],
        total_value=total,
        as_of=AS_OF,
        satellite_target_pct=draw(st.floats(10.0, 35.0)),
        trailing_turnover_pct=draw(st.floats(0.0, 60.0)),
    )


@st.composite
def outcomes(draw: st.DrawFn) -> list[Outcome]:
    rows = draw(
        st.lists(st.tuples(st.floats(0.05, 1.0), st.floats(-1.0, 2.0)), min_size=2, max_size=3)
    )
    w = sum(r[0] for r in rows)
    probs = [r[0] / w for r in rows]
    probs[-1] = 1.0 - sum(probs[:-1])
    return [
        Outcome(label=str(i), probability=max(0.0, p), ret=r[1])
        for i, (p, r) in enumerate(zip(probs, rows, strict=True))
    ]


@st.composite
def proposal(draw: st.DrawFn, side: str = "buy", eligible: bool = False) -> Proposal:
    """Random proposal; ``eligible`` keeps it inside the core-pick filters so
    that the sizing caps (not eligibility vetoes) are exercised."""
    data: dict[str, Any] = {
        "symbol": draw(st.sampled_from([*SYMBOLS[:10], "NEW1", "NEW2"])),
        "side": side,
        "bucket": draw(st.sampled_from(["core_pick", "asymmetric_bet"])),
        "sector": draw(st.sampled_from(SECTORS)),
        "themes": draw(themes_st),
        "market_cap_usd": draw(st.floats(2.5e8, 1e11)),
        "adv_usd": draw(st.floats(1e6, 1e9)),
        "annual_vol": draw(st.floats(0.05, 1.05)),
        "price": draw(st.floats(1.0, 1000.0)),
        "p_beat_12m": draw(st.floats(0.0, 1.0)),
        "outcomes": draw(outcomes()),
        "size_pct_total": draw(st.floats(0.01, 10.0)),
        "is_speculative": draw(st.booleans()),
        "thesis_break_logged": draw(st.booleans()),
    }
    if eligible:
        data["market_cap_usd"] = draw(st.floats(2e9, 1e11))
        data["adv_usd"] = draw(st.floats(2e7, 1e9))
        data["annual_vol"] = draw(st.floats(0.05, 0.7))
        data["p_beat_12m"] = draw(st.floats(0.5, 1.0))
        data["outcomes"] = [
            Outcome(label="bear", probability=0.3, ret=draw(st.floats(-0.6, -0.05))),
            Outcome(label="base", probability=0.4, ret=draw(st.floats(0.0, 0.2))),
            Outcome(label="bull", probability=0.3, ret=draw(st.floats(0.3, 2.0))),
        ]
    return Proposal.model_validate(data)


SETTINGS = settings(max_examples=300, deadline=None, suppress_health_check=[HealthCheck.too_slow])


@SETTINGS
@given(portfolio(), proposal(), st.floats(0.5, 1.0))
def test_buy_never_exceeds_any_limit(s: PortfolioState, p: Proposal, m: float) -> None:
    check_buy(s, p, m)


@SETTINGS
@given(portfolio(), proposal(eligible=True), st.floats(0.5, 1.0))
def test_eligible_buy_never_exceeds_any_limit(s: PortfolioState, p: Proposal, m: float) -> None:
    check_buy(s, p, m)


def check_buy(s: PortfolioState, p: Proposal, m: float) -> None:
    r = evaluate(p, s, LIMITS, lookthrough=LOOK, modifier=m)
    T = s.total_value
    tol = 1e-7 * T
    sat_pct = s.satellite_target_pct
    assert sat_pct is not None
    book = build_book(s.holdings, T, sat_pct, LOOK)
    S = book.satellite_capital
    x = r.max_order_usd
    assert x >= 0 and r.max_order_pct_total >= 0 and r.max_order_shares >= 0
    assert r.max_order_shares * p.price <= x + 1e-6
    assert x <= p.size_pct_total / 100 * T + tol
    if r.verdict == "VETO":
        assert x == 0 and r.veto is not None
        return
    assert x > 0
    if r.verdict == "PASS":
        assert abs(r.max_order_pct_total - p.size_pct_total) < 1e-6
    b = LIMITS.buckets
    existing = book.position_mv.get(p.symbol, 0.0)
    bl = b.core_pick if p.bucket == "core_pick" else b.asymmetric_bet
    # eligibility
    assert p.annual_vol <= bl.max_name_annual_vol
    assert p.market_cap_usd >= bl.min_market_cap_usd
    assert p.adv_usd >= bl.min_adv_usd
    # sizing caps
    assert r.kelly is not None
    assert existing + x <= r.kelly.ceiling_pct_total / 100 * T + tol
    assert r.kelly.ceiling_pct_total <= 100 * LIMITS.position.kelly_fraction_cap
    assert existing + x <= bl.max_pct_total / 100 * T + tol
    if existing <= 0:
        assert x <= bl.default_initial_pct_total / 100 * T + tol
    if p.bucket == "core_pick":
        assert existing + x <= b.core_pick.max_pct_satellite / 100 * S + tol
    else:
        assert book.asym_mv() + x <= b.asymmetric_bet.max_bucket_pct_satellite / 100 * S + tol
        assert len(book.asym_names() | {p.symbol}) <= b.asymmetric_bet.max_names
    conc = LIMITS.concentration
    assert (
        book.sector_mv.get(sector_key(p.sector), 0.0) + x
        <= conc.max_sector_pct_satellite / 100 * S + tol
    )
    for t, f in p.themes.items():
        if f <= 0:
            continue  # a trade with no exposure to the theme cannot breach its cap
        assert book.theme_sat_mv.get(t, 0.0) + x * f <= conc.max_theme_pct_satellite / 100 * S + tol
        assert r.theme_exposure[t].satellite_pct <= conc.max_theme_pct_satellite + 1e-6
    assert x <= LIMITS.liquidity.max_position_pct_of_adv / 100 * p.adv_usd + tol
    if p.is_speculative:
        assert book.speculative_mv + x <= LIMITS.account.speculative_sleeve_max_pct / 100 * T + tol
    assert len(book.names | {p.symbol}) <= LIMITS.position.max_names_satellite
    assert 0 <= r.marginal_vol_contribution <= r.satellite_vol_after + 1e-9


@SETTINGS
@given(portfolio(), proposal(), st.floats(0.0, 1.0), st.floats(0.0, 1.0))
def test_modifier_never_increases_size(
    s: PortfolioState, p: Proposal, m1: float, m2: float
) -> None:
    lo, hi = sorted((m1, m2))
    a = evaluate(p, s, LIMITS, lookthrough=LOOK, modifier=lo)
    b = evaluate(p, s, LIMITS, lookthrough=LOOK, modifier=hi)
    full = evaluate(p, s, LIMITS, lookthrough=LOOK, modifier=1.0)
    assert a.max_order_usd <= b.max_order_usd + 1e-9
    assert b.max_order_usd <= full.max_order_usd + 1e-9
    if full.verdict != "VETO" and b.verdict != "VETO":
        assert (
            b.max_order_usd == full.max_order_usd * hi
            or abs(b.max_order_usd - full.max_order_usd * hi) <= 1e-9 * s.total_value
        )


@SETTINGS
@given(portfolio(), proposal(side="sell"))
def test_sell_never_shorts_or_breaks_rules(s: PortfolioState, p: Proposal) -> None:
    r = evaluate(p, s, LIMITS, lookthrough=LOOK)
    T = s.total_value
    sat_pct = s.satellite_target_pct
    assert sat_pct is not None
    book = build_book(s.holdings, T, sat_pct, LOOK)
    held = book.held_mv.get(p.symbol, 0.0)
    assert r.verdict in ("PASS", "VETO")
    if r.verdict == "VETO":
        assert r.max_order_usd == 0
        return
    assert r.max_order_usd <= held * (1 + 1e-9) + 1e-6
    opened = book.opened_on.get(p.symbol)
    if opened is not None and not p.thesis_break_logged:
        assert (AS_OF - opened).days >= LIMITS.portfolio.min_holding_days
    if p.symbol in book.position_mv:
        after = s.trailing_turnover_pct + 100 * r.max_order_usd / book.satellite_capital
        assert after <= LIMITS.portfolio.turnover_budget_satellite_annual_pct + 1e-6
