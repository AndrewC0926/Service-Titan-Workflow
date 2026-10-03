"""Scenario engine: factor sensitivities, scenario losses, risk-budget modifier, regime.

Public API (see module docstrings for the documented rules):

* :func:`run_scenarios` - holdings + ``ScenariosConfig`` -> :class:`ScenarioReport`
* :func:`scenario_losses`, :func:`risk_budget_modifier`, :func:`holding_scenario_return`
* :func:`estimate_sensitivity`, :func:`default_sensitivity`, :func:`sensitivities_for`
* :func:`regime_snapshot` -> :class:`RegimeSnapshot`
"""

from committee.engines.scenario.engine import (
    holding_scenario_return,
    risk_budget_modifier,
    run_scenarios,
    scenario_losses,
)
from committee.engines.scenario.models import (
    FactorSensitivity,
    ModifierDriver,
    ScenarioLoss,
    ScenarioReport,
)
from committee.engines.scenario.regime import MacroInputs, RegimeSnapshot, regime_snapshot
from committee.engines.scenario.sensitivity import (
    BetaEstimate,
    default_sensitivity,
    estimate_sensitivity,
    sensitivities_for,
)
from committee.engines.scenario.vol import satellite_vol

__all__ = [
    "BetaEstimate",
    "FactorSensitivity",
    "MacroInputs",
    "ModifierDriver",
    "RegimeSnapshot",
    "ScenarioLoss",
    "ScenarioReport",
    "default_sensitivity",
    "estimate_sensitivity",
    "holding_scenario_return",
    "regime_snapshot",
    "risk_budget_modifier",
    "run_scenarios",
    "satellite_vol",
    "scenario_losses",
    "sensitivities_for",
]
