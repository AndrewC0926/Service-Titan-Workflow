"""Monthly API budget guard (DESIGN 11, cost controls).

Spend this month is the sum of ``cost_usd`` over journaled ``agent_output`` and
``recall_probe`` entries created in the current UTC calendar month. The journal
is the single source of truth, so the guard survives restarts.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Callable

from committee.agents.errors import BudgetExceeded
from committee.config.schema import Budget
from committee.journal.store import Journal, utcnow

COST_ENTRY_TYPES = ("agent_output", "recall_probe")


def month_start(now: dt.datetime) -> dt.datetime:
    now = now.astimezone(dt.UTC)
    return dt.datetime(now.year, now.month, 1, tzinfo=dt.UTC)


class BudgetGuard:
    def __init__(
        self,
        journal: Journal,
        budget: Budget,
        clock: Callable[[], dt.datetime] = utcnow,
    ) -> None:
        self.journal = journal
        self.budget = budget
        self.clock = clock

    def spent_this_month(self) -> float:
        start = month_start(self.clock())
        total = 0.0
        for etype in COST_ENTRY_TYPES:
            for e in self.journal.entries(etype):
                if e.created_at >= start:
                    total += float(e.payload.get("cost_usd", 0.0) or 0.0)
        return total

    def fraction_used(self) -> float:
        return self.spent_this_month() / self.budget.monthly_usd

    def check(self) -> float:
        """Raise BudgetExceeded at 100% of the monthly budget; return fraction used."""
        frac = self.fraction_used()
        if frac >= 1.0:
            raise BudgetExceeded(
                f"monthly API budget exhausted ({frac:.0%} of ${self.budget.monthly_usd:g})"
            )
        return frac

    def downgraded(self) -> bool:
        return self.fraction_used() >= self.budget.downgrade_at_fraction

    def reviews_per_week_allowed(self) -> int:
        if self.fraction_used() >= 1.0:
            return 0
        if self.downgraded():
            return self.budget.reviews_per_week_downgraded
        return self.budget.reviews_per_week_normal
