"""What the explainers and the Chair see of the deterministic engines.

The real risk/tax/scenario engines live in ``committee.engines`` (built in later
phases). The agent layer depends only on these minimal views, which the
orchestrator fills from engine outputs (or, for now, from fixtures).
"""

from __future__ import annotations

import datetime as dt
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from committee.domain import AccountKind, RiskVerdict

BucketTag = Literal["CORE_PICK", "ASYMMETRIC_BET"]
TAG_TO_BUCKET: dict[str, str] = {"CORE_PICK": "core_pick", "ASYMMETRIC_BET": "asymmetric_bet"}


class View(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RiskEngineView(View):
    verdict: RiskVerdict
    max_size_pct_total: float = Field(ge=0, le=100)
    binding_constraints: list[str] = Field(default_factory=list)
    scenario_losses_pct: dict[str, float] = Field(default_factory=dict)
    veto_rule: str | None = None


class TaxEngineView(View):
    recommended_account: AccountKind
    wash_sale_blocked: bool
    wash_sale_window_ends: dt.date | None = None
    after_tax_hurdle_pct: float
    holding_period_warnings: list[str] = Field(default_factory=list)
    cpa_flags: list[str] = Field(default_factory=list)
    label: str = "Not tax advice. Confirm with a CPA."


class EngineInputs(View):
    """Deterministic inputs the committee cannot change."""

    bucket_tag: BucketTag
    lottery_filter_pass: bool | None = None  # None for core picks (filter not applicable)
    risk: RiskEngineView
    tax: TaxEngineView
    risk_budget_modifier: float = Field(default=1.0, ge=0.5, le=1.0)
    brier_weights: dict[str, float] = Field(default_factory=dict)
    round_trip_cost: float = Field(default=0.01, ge=0)

    def as_evidence(self) -> dict[str, list[dict[str, Any]]]:
        """Engine outputs as packet evidence rows (numbers only; no account data)."""
        return {
            "risk_engine": [self.risk.model_dump(mode="json")],
            "tax_engine": [self.tax.model_dump(mode="json")],
        }
