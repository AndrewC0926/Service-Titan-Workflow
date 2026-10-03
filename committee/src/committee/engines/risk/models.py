"""Typed inputs and outputs of the risk engine.

Units: every ``*_pct*`` field is a percentage (``3.0`` means 3%); returns and
probabilities are fractions; money is USD.
"""

from __future__ import annotations

import datetime as dt
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from committee.domain import Bucket, Holding, RiskVerdict, Side
from committee.engines.scenario.models import ScenarioLoss

Instrument = Literal["stock", "etf", "option", "future", "crypto"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class Outcome(Frozen):
    """One branch of the Chair's scenario-implied payoff (e.g. bear/base/bull)."""

    label: str
    probability: float = Field(ge=0.0, le=1.0)
    ret: float = Field(ge=-1.0)  # 12-month total return; -1.0 is a total loss


class Proposal(Frozen):
    """A committee proposal handed to the risk engine.

    ``size_pct_total`` is the requested *order* notional as % of total account
    value (for a buy, the amount to add; for a sell, the amount to sell).
    """

    symbol: str
    side: Side
    bucket: Bucket
    sector: str
    themes: dict[str, float] = Field(default_factory=dict)  # fraction of position exposed
    market_cap_usd: float = Field(ge=0)
    adv_usd: float = Field(ge=0)  # 60-day average daily dollar volume
    annual_vol: float = Field(gt=0)
    price: float = Field(gt=0)
    p_beat_12m: float = Field(ge=0.0, le=1.0)
    outcomes: list[Outcome] = Field(default_factory=list)
    size_pct_total: float = Field(ge=0)
    is_speculative: bool = False
    thesis_break_logged: bool = False
    instrument: Instrument = "stock"
    uses_margin: bool = False
    leverage: float = Field(default=1.0, gt=0)

    @model_validator(mode="after")
    def _check(self) -> Proposal:
        for t, f in self.themes.items():
            if not 0.0 <= f <= 1.0:
                raise ValueError(f"theme exposure {t}={f} must lie in [0, 1]")
        if self.outcomes:
            total = sum(o.probability for o in self.outcomes)
            if abs(total - 1.0) > 1e-6:
                raise ValueError(f"outcome probabilities sum to {total}, not 1")
        return self


class PortfolioState(Frozen):
    """Everything the engine needs to know about the current account."""

    holdings: list[Holding]
    total_value: float = Field(gt=0)
    as_of: dt.date
    satellite_target_pct: float | None = None  # default: risk_limits.account.satellite_target_pct
    trailing_turnover_pct: float = Field(default=0.0, ge=0)  # satellite, trailing 12 months
    satellite_drawdown_pct: float | None = Field(default=None, ge=0)
    total_drawdown_pct: float | None = Field(default=None, ge=0)


class Constraint(Frozen):
    """One limit evaluated for a proposal. ``cap_pct_total`` is the largest
    order (as % of total) this rule allows; ``None`` for non-size rules."""

    rule_id: str
    text: str
    cap_pct_total: float | None = None


class Flag(Frozen):
    rule_id: str
    text: str


class ThemeExposure(Frozen):
    """Post-trade exposure to one theme."""

    satellite_pct: float  # satellite holdings' exposure, % of satellite capital
    total_pct: float  # core ETF look-through + satellite, % of total account


class KellyResult(Frozen):
    f_discrete: float  # full-Kelly fraction maximizing E[log] over the outcomes
    f_binary: float  # closed-form p/a - q/b with the Chair's probability
    f_used: float  # min of the available estimates
    fraction_cap: float  # kelly_fraction_cap (0.5 = half Kelly)
    ceiling_pct_total: float  # 100 * fraction_cap * f_used


class RiskResult(Frozen):
    symbol: str
    side: Side
    verdict: RiskVerdict
    requested_pct_total: float
    max_order_pct_total: float
    max_order_usd: float
    max_order_shares: int  # whole shares, rounded down
    post_trade_position_pct_total: float
    kelly: KellyResult | None = None
    caps: list[Constraint] = Field(default_factory=list)
    binding: list[Constraint] = Field(default_factory=list)
    veto: Constraint | None = None
    theme_exposure: dict[str, ThemeExposure] = Field(default_factory=dict)
    flags: list[Flag] = Field(default_factory=list)
    satellite_vol_before: float = 0.0
    satellite_vol_after: float = 0.0
    marginal_vol_contribution: float = 0.0
    risk_budget_modifier: float = 1.0
    scenario_losses: list[ScenarioLoss] = Field(default_factory=list)
