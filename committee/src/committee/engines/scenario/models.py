"""Typed inputs and outputs of the scenario engine.

Sign conventions used throughout the engine:

* returns are signed fractions (``-0.12`` is a 12% fall);
* ``*_loss`` fields are ``-return`` (a positive number is a loss, a negative
  number is a gain);
* rate betas are expressed per +100bp move in the 10-year yield.
"""

from __future__ import annotations

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

SensitivitySource = Literal["estimated", "default_sleeve", "default_sector", "zero"]


class Frozen(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class FactorSensitivity(Frozen):
    """Linear sensitivities of one holding to the scenario factors.

    ``beta_market_down`` (optional) replaces ``beta_market`` when the market
    shock is negative. It models crisis convexity, e.g. a trend sleeve that is
    flat in rallies but gains in sell-offs.
    """

    beta_market: float = 1.0
    beta_market_down: float | None = None
    beta_rates_100bp: float = 0.0
    beta_oil: float = 0.0
    beta_dollar: float = 0.0
    themes: dict[str, float] = Field(default_factory=dict)
    source: SensitivitySource = "zero"


class ScenarioLoss(Frozen):
    """Portfolio outcome under one scenario."""

    scenario_id: str
    name: str
    probability: float
    total_return: float  # fraction of total account value
    satellite_return: float  # fraction of satellite market value (0 if no satellite)
    breaches_budget: bool = False
    contributions: dict[str, float] = Field(default_factory=dict)  # symbol -> share of total

    @property
    def total_loss(self) -> float:
        return -self.total_return

    @property
    def satellite_loss(self) -> float:
        return -self.satellite_return


class ModifierDriver(Frozen):
    """One scenario that reduced the risk-budget modifier."""

    scenario_id: str
    satellite_loss: float
    threshold: float
    reduction: float
    text: str


class ScenarioReport(Frozen):
    """Everything the Risk engine and Macro agent need from a scenario run."""

    losses: list[ScenarioLoss]
    total_value: float
    satellite_value: float
    satellite_expected_vol: float
    loss_threshold: float  # loss_vol_multiple x satellite_expected_vol
    modifier: float = Field(ge=0.0, le=1.0)
    drivers: list[ModifierDriver] = Field(default_factory=list)

    def loss_for(self, scenario_id: str) -> ScenarioLoss:
        for loss in self.losses:
            if loss.scenario_id == scenario_id:
                return loss
        raise KeyError(scenario_id)
