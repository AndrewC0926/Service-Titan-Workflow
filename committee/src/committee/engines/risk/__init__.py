"""Risk engine: deterministic eligibility, sizing and verdicts (DESIGN 3, 9).

Public API:

* :func:`evaluate` - ``Proposal`` + ``PortfolioState`` + ``RiskLimits``
  (+ optional ``ScenarioReport`` / modifier) -> :class:`RiskResult`
* :func:`kelly_ceiling`, :func:`kelly_discrete`, :func:`kelly_binary`
* :func:`turnover_from_trades`
"""

from committee.engines.risk.engine import VOL_TARGETS, RiskSettings, evaluate
from committee.engines.risk.exposure import Book, build_book, turnover_from_trades
from committee.engines.risk.kelly import kelly_binary, kelly_ceiling, kelly_discrete
from committee.engines.risk.models import (
    Constraint,
    Flag,
    KellyResult,
    Outcome,
    PortfolioState,
    Proposal,
    RiskResult,
    ThemeExposure,
)

__all__ = [
    "VOL_TARGETS",
    "Book",
    "Constraint",
    "Flag",
    "KellyResult",
    "Outcome",
    "PortfolioState",
    "Proposal",
    "RiskResult",
    "RiskSettings",
    "ThemeExposure",
    "build_book",
    "evaluate",
    "kelly_binary",
    "kelly_ceiling",
    "kelly_discrete",
    "turnover_from_trades",
]
