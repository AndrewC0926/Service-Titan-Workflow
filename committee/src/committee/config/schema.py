"""Typed configuration models. Invalid config fails startup with a clear message."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

_LATEST = re.compile(r"latest", re.IGNORECASE)

AgentName = Literal[
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
AGENT_NAMES: tuple[str, ...] = AgentName.__args__  # type: ignore[attr-defined]
AccountKind = Literal["taxable", "ira", "k401"]


class Strict(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


# ---------------------------------------------------------------- models.yaml
class ModelTier(Strict):
    model: str
    temperature: float = Field(ge=0.0, le=1.0)
    max_tokens: int = Field(gt=0)

    @field_validator("model")
    @classmethod
    def _pinned(cls, v: str) -> str:
        if _LATEST.search(v):
            raise ValueError(f"model id {v!r} uses a 'latest' alias; pin an exact id")
        return v


class ModelPrice(Strict):
    input: float
    output: float
    cache_read: float
    cache_write: float


class Budget(Strict):
    monthly_usd: float = Field(gt=0)
    downgrade_at_fraction: float = Field(gt=0, le=1)
    reviews_per_week_normal: int = Field(gt=0)
    reviews_per_week_downgraded: int = Field(gt=0)


class ModelsConfig(Strict):
    tiers: dict[str, ModelTier]
    agents: dict[AgentName, str]
    pricing: dict[str, ModelPrice]
    budget: Budget

    @model_validator(mode="after")
    def _check(self) -> ModelsConfig:
        missing = set(AGENT_NAMES) - set(self.agents)
        if missing:
            raise ValueError(f"agents missing a tier: {sorted(missing)}")
        for agent, tier in self.agents.items():
            if tier not in self.tiers:
                raise ValueError(f"agent {agent} uses unknown tier {tier!r}")
        for t in self.tiers.values():
            if t.model not in self.pricing:
                raise ValueError(f"no pricing for model {t.model!r}")
        chair = self.tiers[self.agents["chair"]]
        for indep in ("bear", "risk_explainer"):
            other = self.tiers[self.agents[indep]]
            if self.agents[indep] == self.agents["chair"] or other.model == chair.model:
                raise ValueError(f"{indep} must use a different model tier/family from chair")
        return self

    def tier_for(self, agent: str) -> ModelTier:
        return self.tiers[self.agents[agent]]  # type: ignore[index]


# ---------------------------------------------------------- risk_limits.yaml
class AccountLimits(Strict):
    satellite_target_pct: float
    satellite_min_pct: float
    satellite_max_pct: float
    speculative_sleeve_max_pct: float

    @model_validator(mode="after")
    def _order(self) -> AccountLimits:
        if not self.satellite_min_pct <= self.satellite_target_pct <= self.satellite_max_pct:
            raise ValueError(
                "satellite_target_pct must lie within [satellite_min_pct, satellite_max_pct]"
            )
        return self


class CorePickLimits(Strict):
    min_market_cap_usd: float
    min_adv_usd: float
    max_pct_total: float = Field(gt=0)
    max_pct_satellite: float = Field(gt=0, le=100)
    default_initial_pct_total: float = Field(gt=0)
    max_name_annual_vol: float = Field(gt=0)


class AsymmetricLimits(Strict):
    max_bucket_pct_satellite: float = Field(gt=0, le=100)
    max_names: int = Field(gt=0)
    min_market_cap_usd: float
    min_adv_usd: float
    max_pct_total: float = Field(gt=0)
    default_initial_pct_total: float = Field(gt=0)
    max_name_annual_vol: float = Field(gt=0)
    lottery_filter: bool


class Buckets(Strict):
    core_pick: CorePickLimits
    asymmetric_bet: AsymmetricLimits


class PositionLimits(Strict):
    kelly_fraction_cap: float = Field(gt=0, le=0.5)
    min_names_satellite: int
    max_names_satellite: int


class ConcentrationLimits(Strict):
    max_sector_pct_satellite: float
    max_theme_pct_satellite: float
    flag_theme_lookthrough_pct_total: float
    themes: list[str]


class LiquidityLimits(Strict):
    max_position_pct_of_adv: float = Field(gt=0)


class VolatilitySettings(Strict):
    vol_scale_new_positions: bool


class PortfolioLimits(Strict):
    max_satellite_drawdown_review_pct: float
    max_total_drawdown_review_pct: float
    turnover_budget_satellite_annual_pct: float
    min_holding_days: int


class CoolingOff(Strict):
    default: int
    behavioral_stop: int
    drawdown_over_25pct: int


REQUIRED_PROHIBITIONS = frozenset({"margin", "leverage", "short_selling", "options"})


class RiskLimits(Strict):
    profile: str
    account: AccountLimits
    buckets: Buckets
    position: PositionLimits
    concentration: ConcentrationLimits
    liquidity: LiquidityLimits
    volatility: VolatilitySettings
    portfolio: PortfolioLimits
    prohibited: list[str]
    cooling_off_hours: CoolingOff

    @field_validator("prohibited")
    @classmethod
    def _prohibited(cls, v: list[str]) -> list[str]:
        missing = REQUIRED_PROHIBITIONS - set(v)
        if missing:
            raise ValueError(f"prohibited list must include {sorted(missing)}")
        return v


# ------------------------------------------------------ policy_portfolio.yaml
SleeveKind = Literal["core_equity", "core_tilt", "diversifier", "liquidity", "satellite", "bonds"]


class Sleeve(Strict):
    id: str
    kind: SleeveKind
    holding: str | None
    target_pct: float = Field(ge=0, le=100)
    band_pct: float | None
    location: list[AccountKind]


class SpeculativeSleeve(Strict):
    max_pct: float
    carved_from: str
    location: list[AccountKind]


class PolicyPortfolio(Strict):
    sleeves: list[Sleeve]
    speculative_sleeve: SpeculativeSleeve
    lookthrough: dict[str, dict[str, float]]

    @model_validator(mode="after")
    def _sum(self) -> PolicyPortfolio:
        total = sum(s.target_pct for s in self.sleeves)
        if abs(total - 100.0) > 1e-6:
            raise ValueError(f"policy sleeve targets sum to {total}, not 100")
        return self

    def sleeve(self, sleeve_id: str) -> Sleeve:
        for s in self.sleeves:
            if s.id == sleeve_id:
                return s
        raise KeyError(sleeve_id)


# --------------------------------------------------------------- universe.yaml
class CoreUniverse(Strict):
    min_market_cap_usd: float
    min_adv_usd: float


class AsymUniverse(Strict):
    min_market_cap_usd: float
    max_market_cap_usd: float
    min_adv_usd: float


class Range(Strict):
    min: int
    max: int


class UniverseConfig(Strict):
    core_pick: CoreUniverse
    asymmetric_bet: AsymUniverse
    adv_window_days: int
    min_price_usd: float
    exclude_active_mna: bool
    shortlist_size: int
    cluster_buy_lookback_days: int
    committee_reviews_per_week: Range
    test_universe: list[str]


# ---------------------------------------------------------------- signals.yaml
class InsiderSettings(Strict):
    routine_lookback_years: int
    score_window_days: int
    cluster_min_insiders: int
    cluster_window_days: int


class SignalsConfig(Strict):
    weights: dict[str, float]
    cluster_buy_bonus: float
    winsorize_z: float
    earnings_revision_enabled: bool
    insider: InsiderSettings


# -------------------------------------------------------------- scenarios.yaml
class Scenario(Strict):
    id: str
    name: str
    definition: str
    probability: float = Field(ge=0, le=1)
    shocks: dict[str, float]
    theme_shocks: dict[str, float]


class ScenariosConfig(Strict):
    scenarios: list[Scenario]
    loss_vol_multiple: float
    modifier_floor: float = Field(ge=0.5, le=1.0)


# ------------------------------------------------------------- tax_config.yaml
class Bracket(Strict):
    up_to: float | None
    rate: float


class HarvestSettings(Strict):
    min_loss_usd: float
    min_loss_pct_of_basis: float


class TaxConfig(Strict):
    effective_year: int
    filing_status: str
    ordinary_brackets: list[Bracket]
    assumed_ordinary_rate: float
    assumed_ltcg_rate: float
    niit_rate: float
    apply_niit: bool
    state_rate: float
    long_term_days: int
    long_term_warning_days: int
    wash_sale_window_days: int = Field(ge=30)
    harvest: HarvestSettings
    replacements: dict[str, str]
    # Substantially identical groups (e.g. share classes). Replacements are never in here.
    equivalence_groups: list[list[str]] = Field(default_factory=list)
    location_preference: dict[str, list[AccountKind]]


# --------------------------------------------------------------------- app.yaml
class Paths(Strict):
    data_dir: str
    raw_dir: str
    journal_db: str
    state_db: str
    anchor_dir: str
    backup_dir: str
    reports_dir: str


class BrokerSettings(Strict):
    provider: Literal["alpaca"]
    per_order_notional_cap_pct: float = Field(gt=0)
    daily_notional_cap_pct: float = Field(gt=0)
    limit_band_pct: float = Field(ge=0)
    time_in_force: Literal["day", "gtc"]
    live_gate_file: str


class ApprovalSettings(Strict):
    expiry_days: int
    min_reason_chars: int


class SecSettings(Strict):
    max_requests_per_second: int = Field(gt=0, le=10)


class DigestSettings(Strict):
    send_at_local: str


class AppSettings(Strict):
    paths: Paths
    broker: BrokerSettings
    approval: ApprovalSettings
    sec: SecSettings
    timezone: str
    digest: DigestSettings


class AppConfig(Strict):
    """All configuration, validated together."""

    models: ModelsConfig
    risk_limits: RiskLimits
    policy_portfolio: PolicyPortfolio
    universe: UniverseConfig
    signals: SignalsConfig
    scenarios: ScenariosConfig
    tax: TaxConfig
    app: AppSettings
