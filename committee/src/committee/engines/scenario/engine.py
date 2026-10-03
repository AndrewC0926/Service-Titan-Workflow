"""Portfolio loss per scenario and the risk-budget modifier rule.

Per-holding scenario return::

    r = b_mkt * market + b_rates * rates_10y_bp / 100 + b_oil * oil
        + b_usd * dollar + sum_t theme_exposure_t * theme_shock_t

(``b_mkt`` is replaced by ``beta_market_down`` when the market shock is
negative and a downside beta is set.) Portfolio return is the market-value
weighted sum; the total-account figure divides by total account value
(cash, if any, is the residual and earns zero), the satellite figure divides
by the satellite's market value.

Risk-budget modifier rule (DESIGN 9: "any loss above 2x the satellite's normal
1-year expected volatility reduces the risk-budget modifier"):

* threshold ``T = loss_vol_multiple x satellite expected vol``;
* every scenario whose satellite loss ``L`` strictly exceeds ``T`` cuts the
  modifier by ``max(min_cut, p x (L - T) / T)`` where ``p`` is the scenario's
  market-implied probability (probability-weighted proportional excess, with
  a floor of ``min_cut`` = 0.05 so any breach visibly reduces the budget);
  each cut is capped at ``1 - floor``;
* ``modifier = max(floor, 1 - sum of cuts)``; it never exceeds 1.0.
"""

from __future__ import annotations

from collections.abc import Mapping

from committee.config.schema import PolicyPortfolio, Scenario, ScenariosConfig
from committee.domain import Holding
from committee.engines.scenario.models import (
    FactorSensitivity,
    ModifierDriver,
    ScenarioLoss,
    ScenarioReport,
)
from committee.engines.scenario.sensitivity import SATELLITE_SLEEVES, sensitivities_for
from committee.engines.scenario.vol import DEFAULT_RHO, satellite_vol

DEFAULT_MIN_CUT = 0.05


def holding_scenario_return(sens: FactorSensitivity, scenario: Scenario) -> float:
    """Return of one holding under one scenario (signed fraction)."""
    s = scenario.shocks
    market = s.get("market", 0.0)
    b_mkt = sens.beta_market
    if market < 0 and sens.beta_market_down is not None:
        b_mkt = sens.beta_market_down
    r = (
        b_mkt * market
        + sens.beta_rates_100bp * s.get("rates_10y_bp", 0.0) / 100.0
        + sens.beta_oil * s.get("oil", 0.0)
        + sens.beta_dollar * s.get("dollar", 0.0)
    )
    for theme, exposure in sens.themes.items():
        r += exposure * scenario.theme_shocks.get(theme, 0.0)
    return r


def is_satellite(h: Holding) -> bool:
    return h.sleeve in SATELLITE_SLEEVES


def scenario_losses(
    holdings: list[Holding],
    sensitivities: Mapping[str, FactorSensitivity],
    scenarios: list[Scenario],
    total_value: float | None = None,
) -> list[ScenarioLoss]:
    """Total-account and satellite-only return for every scenario.

    ``total_value`` defaults to the sum of holding market values; pass the
    account value when it includes cash. Every holding's symbol must have a
    sensitivity.
    """
    gross = sum(h.market_value for h in holdings)
    total = gross if total_value is None else total_value
    if total < gross - 1e-6 * max(1.0, gross):
        raise ValueError("total_value is smaller than the market value of holdings")
    sat_value = sum(h.market_value for h in holdings if is_satellite(h))
    out: list[ScenarioLoss] = []
    for sc in scenarios:
        contrib: dict[str, float] = {}
        total_pnl = 0.0
        sat_pnl = 0.0
        for h in holdings:
            pnl = h.market_value * holding_scenario_return(sensitivities[h.symbol], sc)
            total_pnl += pnl
            if is_satellite(h):
                sat_pnl += pnl
            if total > 0:
                contrib[h.symbol] = contrib.get(h.symbol, 0.0) + pnl / total
        out.append(
            ScenarioLoss(
                scenario_id=sc.id,
                name=sc.name,
                probability=sc.probability,
                total_return=total_pnl / total if total > 0 else 0.0,
                satellite_return=sat_pnl / sat_value if sat_value > 0 else 0.0,
                contributions=contrib,
            )
        )
    return out


def risk_budget_modifier(
    losses: list[ScenarioLoss],
    satellite_expected_vol: float,
    loss_vol_multiple: float,
    floor: float,
    min_cut: float = DEFAULT_MIN_CUT,
) -> tuple[float, list[ModifierDriver]]:
    """Apply the documented modifier rule. Returns (modifier, drivers)."""
    if not 0.0 <= floor <= 1.0:
        raise ValueError("floor must lie in [0, 1]")
    threshold = loss_vol_multiple * satellite_expected_vol
    max_cut = 1.0 - floor
    drivers: list[ModifierDriver] = []
    total_cut = 0.0
    for loss in losses:
        sl = loss.satellite_loss
        if sl <= threshold:
            continue
        excess = (sl - threshold) / threshold if threshold > 0 else float("inf")
        cut = min(max_cut, max(min_cut, loss.probability * excess))
        total_cut += cut
        drivers.append(
            ModifierDriver(
                scenario_id=loss.scenario_id,
                satellite_loss=sl,
                threshold=threshold,
                reduction=cut,
                text=(
                    f"{loss.name}: satellite loss {sl:.1%} exceeds {loss_vol_multiple:g}x expected "
                    f"vol ({threshold:.1%}); p={loss.probability:.0%} cuts the budget by {cut:.3f}"
                ),
            )
        )
    modifier = min(1.0, max(floor, 1.0 - total_cut))
    return modifier, drivers


def run_scenarios(
    holdings: list[Holding],
    config: ScenariosConfig,
    *,
    policy: PolicyPortfolio | None = None,
    total_value: float | None = None,
    estimated: Mapping[str, FactorSensitivity] | None = None,
    rho: float = DEFAULT_RHO,
    min_cut: float = DEFAULT_MIN_CUT,
) -> ScenarioReport:
    """Full scenario run: sensitivities, losses, satellite vol and modifier."""
    sens = sensitivities_for(holdings, policy, estimated)
    gross = sum(h.market_value for h in holdings)
    total = gross if total_value is None else total_value
    losses = scenario_losses(holdings, sens, config.scenarios, total)
    sat = [h for h in holdings if is_satellite(h)]
    vol = satellite_vol(sat, rho)
    modifier, drivers = risk_budget_modifier(
        losses, vol, config.loss_vol_multiple, config.modifier_floor, min_cut
    )
    breaching = {d.scenario_id for d in drivers}
    losses = [
        loss.model_copy(update={"breaches_budget": loss.scenario_id in breaching})
        for loss in losses
    ]
    return ScenarioReport(
        losses=losses,
        total_value=total,
        satellite_value=sum(h.market_value for h in sat),
        satellite_expected_vol=vol,
        loss_threshold=config.loss_vol_multiple * vol,
        modifier=modifier,
        drivers=drivers,
    )
