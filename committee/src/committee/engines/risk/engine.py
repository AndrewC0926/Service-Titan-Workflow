"""Risk engine: eligibility, sizing and verdict for one proposal (DESIGN 3, 9).

Buy pipeline (every step can only shrink the size):

1. Prohibited instruments (options, futures, margin, leverage) -> VETO.
2. Eligibility: name vol above the bucket max, market cap or ADV below the
   bucket minimum, satellite name count or asymmetric name count full -> VETO.
3. Half-Kelly ceiling from the Chair's probability and payoff
   (:mod:`committee.engines.risk.kelly`); no payoff or no edge -> VETO.
4. Pre-limit size = min(requested, Kelly ceiling - existing position,
   default initial size for a new position). For a new position with
   ``vol_scale_new_positions`` on, multiply by ``min(1, target vol / name vol)``
   (never scales up). Target vols: :data:`VOL_TARGETS`.
5. Hard caps on the order (each clamped at 0): bucket max % of total, core max
   % of satellite, asymmetric bucket % of satellite, sector % of satellite,
   theme % of satellite (per theme, divided by the proposal's exposure),
   liquidity (order notional <= ``max_position_pct_of_adv`` % of 60-day ADV
   dollar volume; the YAML says "position" but sizing a position to 1% of one
   day's volume would forbid most positions, so the rule is read as the
   order's participation in daily volume), speculative sleeve % of total.
6. Multiply by the risk-budget modifier (0.5-1.0, from the scenario engine).
7. Zero -> VETO naming the binding rule; below request -> RESIZE; else PASS.

Sell pipeline: prohibited instruments; selling more than is held is a short
-> VETO; minimum holding period (unless a thesis break is logged) -> VETO;
satellite turnover budget -> VETO. Sells are never resized.

Drawdown thresholds only raise review flags; the engine never auto-sells.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass, field

from committee.config.schema import RiskLimits
from committee.domain import Holding, RiskVerdict
from committee.engines.risk.exposure import Book, build_book, sector_key
from committee.engines.risk.kelly import kelly_ceiling
from committee.engines.risk.models import (
    Constraint,
    Flag,
    KellyResult,
    PortfolioState,
    Proposal,
    RiskResult,
    ThemeExposure,
)
from committee.engines.scenario.models import ScenarioReport
from committee.engines.scenario.vol import DEFAULT_RHO, basket, component_contributions
from committee.engines.scenario.vol import constant_corr_vol as _vol

EPS = 1e-9
VOL_TARGETS: dict[str, float] = {"core_pick": 0.35, "asymmetric_bet": 0.60}


@dataclass(frozen=True)
class RiskSettings:
    """Engine assumptions that are not (yet) in risk_limits.yaml."""

    rho: float = DEFAULT_RHO  # constant pairwise correlation for satellite vol
    vol_targets: Mapping[str, float] = field(default_factory=lambda: dict(VOL_TARGETS))


def _resolve_modifier(modifier: float | None, report: ScenarioReport | None) -> float:
    m = modifier if modifier is not None else (report.modifier if report is not None else 1.0)
    if not 0.0 <= m <= 1.0:
        raise ValueError(f"risk-budget modifier must lie in [0, 1], got {m}")
    return m


def _pct(usd: float, total: float) -> float:
    return 100.0 * usd / total


def _prohibited(p: Proposal, limits: RiskLimits) -> Constraint | None:
    banned = set(limits.prohibited)
    if p.instrument == "option" and "options" in banned:
        return Constraint(rule_id="prohibited.options", text="Options are prohibited.")
    if p.uses_margin and "margin" in banned:
        return Constraint(rule_id="prohibited.margin", text="Margin is prohibited.")
    if (p.leverage > 1.0 or p.instrument == "future") and "leverage" in banned:
        return Constraint(
            rule_id="prohibited.leverage",
            text="Leveraged instruments (futures, leverage > 1x) are prohibited.",
        )
    return None


def _eligibility(p: Proposal, book: Book, limits: RiskLimits) -> Constraint | None:
    b = limits.buckets.core_pick if p.bucket == "core_pick" else limits.buckets.asymmetric_bet
    pre = f"buckets.{p.bucket}"
    if p.annual_vol > b.max_name_annual_vol:
        return Constraint(
            rule_id=f"{pre}.max_name_annual_vol",
            text=f"Annual vol {p.annual_vol:.0%} exceeds the {p.bucket} max "
            f"{b.max_name_annual_vol:.0%}.",
        )
    if p.market_cap_usd < b.min_market_cap_usd:
        return Constraint(
            rule_id=f"{pre}.min_market_cap_usd",
            text=f"Market cap ${p.market_cap_usd:,.0f} is below the {p.bucket} minimum "
            f"${b.min_market_cap_usd:,.0f}.",
        )
    if p.adv_usd < b.min_adv_usd:
        return Constraint(
            rule_id=f"{pre}.min_adv_usd",
            text=f"60-day ADV ${p.adv_usd:,.0f} is below the {p.bucket} minimum "
            f"${b.min_adv_usd:,.0f}.",
        )
    is_new = p.symbol not in book.names
    if is_new and len(book.names) >= limits.position.max_names_satellite:
        return Constraint(
            rule_id="position.max_names_satellite",
            text=f"Satellite already holds {len(book.names)} names "
            f"(max {limits.position.max_names_satellite}).",
        )
    asym = limits.buckets.asymmetric_bet
    if p.bucket == "asymmetric_bet" and is_new and len(book.asym_names()) >= asym.max_names:
        return Constraint(
            rule_id="buckets.asymmetric_bet.max_names",
            text=f"Asymmetric bucket already holds {len(book.asym_names())} names "
            f"(max {asym.max_names}).",
        )
    return None


def _buy_caps(
    p: Proposal, book: Book, limits: RiskLimits, kelly: KellyResult, settings: RiskSettings
) -> tuple[list[Constraint], float]:
    """All size caps for a buy (as % of total). Returns (caps, pre-modifier size)."""
    T, S = book.total_value, book.satellite_capital
    existing = book.position_mv.get(p.symbol, 0.0)
    is_new = existing <= 0
    caps: list[Constraint] = []

    def add(rule_id: str, text: str, cap_usd: float) -> None:
        caps.append(
            Constraint(rule_id=rule_id, text=text, cap_pct_total=max(0.0, _pct(cap_usd, T)))
        )

    add(
        "position.kelly_fraction_cap",
        f"Half-Kelly ceiling {kelly.ceiling_pct_total:.2f}% of total "
        f"(f={kelly.f_used:.3f} x {kelly.fraction_cap:g}).",
        kelly.ceiling_pct_total / 100.0 * T - existing,
    )
    if p.bucket == "core_pick":
        bl = limits.buckets.core_pick
        default_pct, max_total = bl.default_initial_pct_total, bl.max_pct_total
    else:
        al = limits.buckets.asymmetric_bet
        default_pct, max_total = al.default_initial_pct_total, al.max_pct_total
    if is_new:
        add(
            f"buckets.{p.bucket}.default_initial_pct_total",
            f"New {p.bucket} positions start at {default_pct:g}% of total.",
            default_pct / 100.0 * T,
        )
    pre = min([p.size_pct_total, *(c.cap_pct_total or 0.0 for c in caps)])
    if is_new and limits.volatility.vol_scale_new_positions:
        target = settings.vol_targets.get(p.bucket, p.annual_vol)
        scale = min(1.0, target / p.annual_vol)
        if scale < 1.0:
            add(
                "volatility.vol_scale_new_positions",
                f"New position scaled by target vol {target:.0%} / name vol "
                f"{p.annual_vol:.0%} = {scale:.2f}.",
                pre * scale / 100.0 * T,
            )
    add(
        f"buckets.{p.bucket}.max_pct_total",
        f"Single {p.bucket} name max {max_total:g}% of total.",
        max_total / 100.0 * T - existing,
    )
    if p.bucket == "core_pick":
        cap = limits.buckets.core_pick.max_pct_satellite
        add(
            "buckets.core_pick.max_pct_satellite",
            f"Single core pick max {cap:g}% of satellite.",
            cap / 100.0 * S - existing,
        )
    else:
        cap = limits.buckets.asymmetric_bet.max_bucket_pct_satellite
        add(
            "buckets.asymmetric_bet.max_bucket_pct_satellite",
            f"Asymmetric bucket max {cap:g}% of satellite.",
            cap / 100.0 * S - book.asym_mv(),
        )
    conc = limits.concentration
    sec = sector_key(p.sector)
    add(
        "concentration.max_sector_pct_satellite",
        f"Sector '{sec}' max {conc.max_sector_pct_satellite:g}% of satellite.",
        conc.max_sector_pct_satellite / 100.0 * S - book.sector_mv.get(sec, 0.0),
    )
    for theme, f in sorted(p.themes.items()):
        if f <= 0:
            continue
        room = conc.max_theme_pct_satellite / 100.0 * S - book.theme_sat_mv.get(theme, 0.0)
        add(
            f"concentration.max_theme_pct_satellite.{theme}",
            f"Theme '{theme}' max {conc.max_theme_pct_satellite:g}% of satellite "
            f"(proposal exposure {f:.0%}).",
            room / f,
        )
    liq = limits.liquidity.max_position_pct_of_adv
    add(
        "liquidity.max_position_pct_of_adv",
        f"Order notional max {liq:g}% of 60-day ADV (${p.adv_usd:,.0f}).",
        liq / 100.0 * p.adv_usd,
    )
    if p.is_speculative:
        spec = limits.account.speculative_sleeve_max_pct
        add(
            "account.speculative_sleeve_max_pct",
            f"Speculative sleeve hard cap {spec:g}% of total.",
            spec / 100.0 * T - book.speculative_mv,
        )
    size = min([p.size_pct_total, *(c.cap_pct_total or 0.0 for c in caps)])
    return caps, max(0.0, size)


def _sell_veto(
    p: Proposal, book: Book, state: PortfolioState, limits: RiskLimits
) -> Constraint | None:
    T = book.total_value
    held = book.held_mv.get(p.symbol, 0.0)
    order = p.size_pct_total / 100.0 * T
    if order > held * (1 + 1e-9) + 1e-6:
        return Constraint(
            rule_id="prohibited.short_selling",
            text=f"Selling ${order:,.0f} of {p.symbol} with ${held:,.0f} held would be a short sale.",
        )
    opened = book.opened_on.get(p.symbol)
    min_days = limits.portfolio.min_holding_days
    if opened is not None and not p.thesis_break_logged:
        days = (state.as_of - opened).days
        if days < min_days:
            return Constraint(
                rule_id="portfolio.min_holding_days",
                text=f"{p.symbol} held {days} days; minimum is {min_days} unless a thesis "
                "break is logged.",
            )
    if p.symbol in book.position_mv and book.satellite_capital > 0:
        budget = limits.portfolio.turnover_budget_satellite_annual_pct
        after = state.trailing_turnover_pct + _pct(order, book.satellite_capital)
        if after > budget + EPS:
            return Constraint(
                rule_id="portfolio.turnover_budget_satellite_annual_pct",
                text=f"Sale would take trailing 12-month satellite turnover to {after:.1f}% "
                f"(budget {budget:g}%).",
            )
    return None


def _theme_exposure(
    p: Proposal, book: Book, order_usd: float, limits: RiskLimits
) -> dict[str, ThemeExposure]:
    """Post-trade theme exposure (signed order: + buy, - sell).

    A buy adds ``order x proposal theme fraction`` to the satellite. A sell
    removes exposure pro rata to the fraction of the holding sold, from the
    satellite tags and/or the core ETF look-through.
    """
    T, S = book.total_value, book.satellite_capital
    themes = set(limits.concentration.themes) | set(book.theme_sat_mv) | set(book.theme_core_mv)
    themes |= set(p.themes)
    held = book.held_mv.get(p.symbol, 0.0)
    frac = min(1.0, -order_usd / held) if order_usd < 0 and held > 0 else 0.0
    sat_sym = book.theme_sat_by_symbol.get(p.symbol, {})
    core_sym = book.theme_core_by_symbol.get(p.symbol, {})
    out: dict[str, ThemeExposure] = {}
    for t in sorted(themes):
        sat = book.theme_sat_mv.get(t, 0.0)
        core = book.theme_core_mv.get(t, 0.0)
        if order_usd > 0:
            sat += order_usd * p.themes.get(t, 0.0)
        else:
            sat -= frac * sat_sym.get(t, 0.0)
            core -= frac * core_sym.get(t, 0.0)
        out[t] = ThemeExposure(
            satellite_pct=_pct(sat, S) if S > 0 else 0.0,
            total_pct=_pct(sat + core, T),
        )
    return out


def _vol_stats(p: Proposal, book: Book, order_usd: float, rho: float) -> tuple[float, float, float]:
    """(satellite vol before, after, component contribution of the name after)."""
    before_w, before_v = basket(book.satellite_holdings)
    before = _vol(before_w, before_v, rho) if before_w else 0.0
    sat_existing = book.position_mv.get(p.symbol, 0.0)
    if order_usd < 0:
        # only the satellite part of a sale changes satellite vol
        order_usd = -min(-order_usd, sat_existing)
    if order_usd == 0:
        after_holdings = book.satellite_holdings
    else:
        synthetic = Holding(
            account="ira",
            symbol=p.symbol,
            qty=order_usd / p.price,
            price=p.price,
            sleeve="satellite",
            bucket=p.bucket,
            annual_vol=p.annual_vol,
        )
        after_holdings = [synthetic, *book.satellite_holdings]
    w, v = basket(after_holdings)
    if not w:
        return before, 0.0, 0.0
    w = {k: max(0.0, x) for k, x in w.items()}
    after = _vol(w, v, rho)
    contrib = component_contributions(w, v, rho).get(p.symbol, 0.0)
    return before, after, contrib


def _flags(
    p: Proposal,
    book: Book,
    state: PortfolioState,
    limits: RiskLimits,
    themes: Mapping[str, ThemeExposure],
    report: ScenarioReport | None,
) -> list[Flag]:
    out: list[Flag] = []
    flag_pct = limits.concentration.flag_theme_lookthrough_pct_total
    for t, e in themes.items():
        if e.total_pct > flag_pct + EPS:
            out.append(
                Flag(
                    rule_id=f"concentration.flag_theme_lookthrough_pct_total.{t}",
                    text=f"Theme '{t}' look-through is {e.total_pct:.1f}% of total "
                    f"(flag above {flag_pct:g}%).",
                )
            )
    pl = limits.portfolio
    if state.satellite_drawdown_pct is not None and (
        state.satellite_drawdown_pct >= pl.max_satellite_drawdown_review_pct
    ):
        out.append(
            Flag(
                rule_id="portfolio.max_satellite_drawdown_review_pct",
                text=f"Satellite drawdown {state.satellite_drawdown_pct:.1f}% triggers a full "
                "review (no auto-sell).",
            )
        )
    if state.total_drawdown_pct is not None and (
        state.total_drawdown_pct >= pl.max_total_drawdown_review_pct
    ):
        out.append(
            Flag(
                rule_id="portfolio.max_total_drawdown_review_pct",
                text=f"Total drawdown {state.total_drawdown_pct:.1f}% triggers an IPS review "
                "with the adviser (no auto-sell).",
            )
        )
    invested = book.satellite_mv >= 0.9 * book.satellite_capital > 0
    if invested and len(book.names) < limits.position.min_names_satellite:
        out.append(
            Flag(
                rule_id="position.min_names_satellite",
                text=f"Satellite is invested but holds {len(book.names)} names "
                f"(min {limits.position.min_names_satellite}).",
            )
        )
    if report is not None:
        out.extend(
            Flag(rule_id=f"scenario.budget_breach.{d.scenario_id}", text=d.text)
            for d in report.drivers
        )
    return out


def evaluate(
    proposal: Proposal,
    state: PortfolioState,
    limits: RiskLimits,
    *,
    lookthrough: Mapping[str, Mapping[str, float]] | None = None,
    scenario_report: ScenarioReport | None = None,
    modifier: float | None = None,
    settings: RiskSettings | None = None,
) -> RiskResult:
    """Evaluate one proposal. Pure and deterministic.

    ``modifier`` overrides ``scenario_report.modifier``; with neither the
    modifier is 1.0. ``lookthrough`` is ``PolicyPortfolio.lookthrough``.
    """
    cfg = settings or RiskSettings()
    m = _resolve_modifier(modifier, scenario_report)
    sat_target = (
        state.satellite_target_pct
        if state.satellite_target_pct is not None
        else limits.account.satellite_target_pct
    )
    book = build_book(state.holdings, state.total_value, sat_target, lookthrough)
    T = book.total_value
    p = proposal
    existing = book.position_mv.get(p.symbol, 0.0)
    losses = list(scenario_report.losses) if scenario_report is not None else []

    def finish(
        verdict: RiskVerdict,
        order_pct: float,
        *,
        veto: Constraint | None = None,
        kelly: KellyResult | None = None,
        caps: list[Constraint] | None = None,
        binding: list[Constraint] | None = None,
    ) -> RiskResult:
        order_pct = max(0.0, order_pct)
        usd = order_pct / 100.0 * T
        signed = usd if p.side == "buy" else -usd
        position = (book.held_mv.get(p.symbol, 0.0) if p.side == "sell" else existing) + signed
        themes = _theme_exposure(p, book, signed, limits)
        before, after, contrib = _vol_stats(p, book, signed, cfg.rho)
        return RiskResult(
            symbol=p.symbol,
            side=p.side,
            verdict=verdict,
            requested_pct_total=p.size_pct_total,
            max_order_pct_total=order_pct,
            max_order_usd=usd,
            max_order_shares=math.floor(usd / p.price + 1e-9),
            post_trade_position_pct_total=_pct(max(0.0, position), T),
            kelly=kelly,
            caps=caps or [],
            binding=binding or ([veto] if veto is not None else []),
            veto=veto,
            theme_exposure=themes,
            flags=_flags(p, book, state, limits, themes, scenario_report),
            satellite_vol_before=before,
            satellite_vol_after=after,
            marginal_vol_contribution=contrib,
            risk_budget_modifier=m,
            scenario_losses=losses,
        )

    banned = _prohibited(p, limits)
    if banned is not None:
        return finish("VETO", 0.0, veto=banned)
    if p.size_pct_total <= 0:
        return finish(
            "VETO",
            0.0,
            veto=Constraint(rule_id="proposal.zero_size", text="Requested size is zero."),
        )

    if p.side == "sell":
        sell_veto = _sell_veto(p, book, state, limits)
        if sell_veto is not None:
            return finish("VETO", 0.0, veto=sell_veto)
        return finish("PASS", p.size_pct_total)

    inelig = _eligibility(p, book, limits)
    if inelig is not None:
        return finish("VETO", 0.0, veto=inelig)
    if not p.outcomes:
        return finish(
            "VETO",
            0.0,
            veto=Constraint(
                rule_id="position.kelly_fraction_cap",
                text="No scenario-implied payoff supplied; Kelly ceiling cannot be computed.",
            ),
        )
    kelly = kelly_ceiling(p.p_beat_12m, p.outcomes, limits.position.kelly_fraction_cap)
    if kelly.f_used <= 0:
        return finish(
            "VETO",
            0.0,
            kelly=kelly,
            veto=Constraint(
                rule_id="position.kelly_fraction_cap",
                text="Kelly fraction is zero: no positive edge in the payoff and probability.",
                cap_pct_total=0.0,
            ),
        )
    caps, pre = _buy_caps(p, book, limits, kelly, cfg)
    final = pre * m
    binding = [
        c
        for c in caps
        if c.cap_pct_total is not None
        and c.cap_pct_total <= pre + EPS
        and c.cap_pct_total < p.size_pct_total - EPS
    ]
    if m < 1.0 and final < p.size_pct_total - EPS:
        binding.append(
            Constraint(
                rule_id="scenario.risk_budget_modifier",
                text=f"Risk-budget modifier {m:.2f} from the scenario engine.",
                cap_pct_total=final,
            )
        )
    if final <= EPS:
        if pre > EPS and binding:
            veto = binding[-1]  # the modifier took the size to zero
        elif binding:
            veto = binding[0]
        else:
            veto = Constraint(rule_id="proposal.zero_size", text="Requested size rounds to zero.")
        return finish("VETO", 0.0, veto=veto, kelly=kelly, caps=caps, binding=binding)
    verdict: RiskVerdict = "RESIZE" if final < p.size_pct_total - EPS else "PASS"
    return finish(verdict, final, kelly=kelly, caps=caps, binding=binding)
