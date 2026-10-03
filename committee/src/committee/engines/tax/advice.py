"""Holding-period warnings: wait for long-term treatment unless the thesis is broken.

A taxable lot with an unrealized gain that turns long-term within
``warning_days`` (60) gets a warning. The default proposal is WAIT; with
``thesis_broken=True`` it is PROCEED (selling now is allowed, the extra tax
is reported). Lots at a loss, already long-term, or in IRA/401(k) get no
warning.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from committee.engines.tax.holding import days_until_long_term, first_long_term_date
from committee.engines.tax.labels import labeled
from committee.engines.tax.models import TaxLot
from committee.engines.tax.rates import TaxRates

Action = Literal["WAIT", "PROCEED"]


@dataclass(frozen=True)
class HoldingAdvice:
    lot_id: str
    symbol: str
    days_to_long_term: int
    long_term_on: dt.date
    unrealized_gain: float
    tax_now: float
    tax_if_wait: float
    action: Action
    reason: str

    @property
    def tax_saved_by_waiting(self) -> float:
        return self.tax_now - self.tax_if_wait


def holding_advice(
    lot: TaxLot,
    price: float,
    asof: dt.date,
    rates: TaxRates,
    warning_days: int = 60,
    thesis_broken: bool = False,
) -> HoldingAdvice | None:
    """Advice for one lot, or None when no warning applies."""
    gain = lot.unrealized(price)
    days = days_until_long_term(lot.holding_start, asof)
    if lot.tax_deferred or gain <= 0 or days == 0 or days > warning_days:
        return None
    tax_now = rates.tax_on(gain, "short")
    tax_wait = rates.tax_on(gain, "long")
    lt_on = first_long_term_date(lot.holding_start)
    action: Action = "PROCEED" if thesis_broken else "WAIT"
    why = (
        "thesis broken: selling now is allowed"
        if thesis_broken
        else "default proposal is to wait unless the thesis is broken"
    )
    return HoldingAdvice(
        lot.lot_id,
        lot.symbol,
        days,
        lt_on,
        gain,
        tax_now,
        tax_wait,
        action,
        labeled(
            f"{action}: lot {lot.lot_id} ({lot.symbol}) turns long-term on {lt_on} "
            f"({days} days); waiting saves about {tax_now - tax_wait:,.2f} in tax on a "
            f"{gain:,.2f} gain; {why}."
        ),
    )


def long_term_warnings(
    lots: Iterable[TaxLot],
    prices: Mapping[str, float],
    asof: dt.date,
    rates: TaxRates,
    warning_days: int = 60,
    thesis_broken: Iterable[str] = (),
) -> list[HoldingAdvice]:
    """Warnings for every lot within ``warning_days`` of long-term with a gain.

    ``thesis_broken`` lists symbols whose thesis is broken (action PROCEED).
    """
    broken = set(thesis_broken)
    out: list[HoldingAdvice] = []
    for lot in lots:
        if lot.symbol not in prices:
            continue
        adv = holding_advice(
            lot, prices[lot.symbol], asof, rates, warning_days, lot.symbol in broken
        )
        if adv is not None:
            out.append(adv)
    return sorted(out, key=lambda a: (a.days_to_long_term, a.lot_id))
