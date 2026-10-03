"""Lot selection for sells (specific identification).

Ordering rule (taxable account, normal sale), applied lot by lot until the
requested quantity is filled:

1. Lowest estimated tax per share first, where
   ``tax_per_share = (price - adjusted basis) * rate(term)`` and ``rate`` is
   the ordinary rate for short-term lots and the LTCG rate for long-term lots
   (plus state and optional NIIT). Losses have negative tax, so loss lots go
   first (short-term losses before long-term losses of the same size because
   they offset income at the higher rate); among gain lots this picks the
   smallest taxed gain, which is "highest basis, long-term first" whenever
   the lots' gains are comparable.
2. Ties: long-term before short-term, then highest basis per share, then
   oldest acquisition, then lot id.

Harvest mode: lots ordered by largest loss per share first (most negative
``price - basis``), ties as above; once loss lots are used up, the remaining
quantity follows the normal rule.

Tax-deferred accounts (IRA, 401(k)): selection has no tax effect; lots are
taken first-in, first-out and the estimated tax is zero.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from committee.engines.tax.holding import Term
from committee.engines.tax.labels import labeled
from committee.engines.tax.models import QTY_EPS, LotPick, TaxLot
from committee.engines.tax.rates import TaxRates


@dataclass(frozen=True)
class ChosenLot:
    lot_id: str
    qty: float
    cost_per_share: float
    term: Term
    gain: float
    estimated_tax: float


@dataclass(frozen=True)
class LotSelection:
    chosen: tuple[ChosenLot, ...]
    estimated_tax: float
    realized_gain: float
    shortfall: float  # quantity that could not be filled from the lots given
    explanation: str

    @property
    def picks(self) -> tuple[LotPick, ...]:
        return tuple(LotPick(lot_id=c.lot_id, qty=c.qty) for c in self.chosen)


def _tie_key(lot: TaxLot, on: dt.date) -> tuple[int, float, dt.date, str]:
    return (0 if lot.term(on) == "long" else 1, -lot.cost_per_share, lot.acquired_on, lot.lot_id)


def order_lots(
    lots: list[TaxLot], price: float, on: dt.date, rates: TaxRates, harvest: bool = False
) -> list[TaxLot]:
    """Return lots in the order the engine would sell them (see module docstring)."""
    if lots and all(lot.tax_deferred for lot in lots):
        return sorted(lots, key=lambda lot: (lot.acquired_on, lot.lot_id))

    def tax_per_share(lot: TaxLot) -> float:
        return rates.tax_on(price - lot.cost_per_share, lot.term(on))

    normal = sorted(lots, key=lambda lot: (tax_per_share(lot), *_tie_key(lot, on)))
    if not harvest:
        return normal
    losers = sorted(
        (lot for lot in lots if lot.cost_per_share > price),
        key=lambda lot: (price - lot.cost_per_share, *_tie_key(lot, on)),
    )
    loser_ids = {lot.lot_id for lot in losers}
    return losers + [lot for lot in normal if lot.lot_id not in loser_ids]


def select_lots(
    lots: list[TaxLot],
    qty: float,
    price: float,
    on: dt.date,
    rates: TaxRates,
    harvest: bool = False,
) -> LotSelection:
    """Choose lots to sell ``qty`` shares at ``price`` on ``on`` and estimate the tax.

    ``lots`` should be the open lots of one symbol in one account.
    """
    if qty <= 0:
        raise ValueError("qty must be positive")
    if len({(lot.account, lot.symbol) for lot in lots}) > 1:
        raise ValueError("select_lots needs lots of one symbol in one account")
    chosen: list[ChosenLot] = []
    need = qty
    for lot in order_lots(lots, price, on, rates, harvest):
        if need <= QTY_EPS:
            break
        q = min(need, lot.qty)
        gain = q * (price - lot.cost_per_share)
        tax = 0.0 if lot.tax_deferred else rates.tax_on(gain, lot.term(on))
        chosen.append(ChosenLot(lot.lot_id, q, lot.cost_per_share, lot.term(on), gain, tax))
        need -= q
    est = sum(c.estimated_tax for c in chosen)
    gain_total = sum(c.gain for c in chosen)
    mode = "harvest (largest losses first)" if harvest else "minimum tax per share"
    explanation = labeled(
        f"Sell {qty:g} at {price:.2f} using {len(chosen)} lot(s), {mode}: "
        f"realized gain {gain_total:,.2f}, estimated tax {est:,.2f}."
    )
    return LotSelection(
        chosen=tuple(chosen),
        estimated_tax=est,
        realized_gain=gain_total,
        shortfall=max(0.0, need) if need > QTY_EPS else 0.0,
        explanation=explanation,
    )
