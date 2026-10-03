"""Typed records of the tax lot ledger."""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass

from pydantic import Field

from committee.domain import TAX_DEFERRED, AccountKind, Model
from committee.engines.tax.holding import Term, term_of

QTY_EPS = 1e-9


@dataclass(frozen=True)
class TaxLot:
    """An open lot as the tax engine tracks it.

    ``cost_per_share`` is the adjusted basis (purchase price plus any wash-sale
    disallowed loss per share). ``holding_start`` is the date the holding
    period is measured from; it equals ``acquired_on`` unless a wash sale
    tacked on the holding period of the sold shares. ``origin_id`` is the
    purchase this lot came from (split lots keep it). ``wash_replacement``
    marks shares that already absorbed a disallowed loss and cannot absorb
    another one.
    """

    lot_id: str
    account: AccountKind
    symbol: str
    qty: float
    cost_per_share: float
    acquired_on: dt.date
    holding_start: dt.date
    origin_id: str
    wash_adjustment_per_share: float = 0.0
    wash_replacement: bool = False

    @property
    def basis(self) -> float:
        return self.qty * self.cost_per_share

    @property
    def tax_deferred(self) -> bool:
        return self.account in TAX_DEFERRED

    def term(self, on: dt.date) -> Term:
        return term_of(self.holding_start, on)

    def unrealized(self, price: float) -> float:
        return self.qty * (price - self.cost_per_share)


@dataclass(frozen=True)
class Purchase:
    """A buy event, kept for wash-sale look-back and look-forward."""

    lot_id: str
    account: AccountKind
    symbol: str
    qty: float
    price: float
    bought_on: dt.date


@dataclass(frozen=True)
class WashMatch:
    """One block of replacement shares matched to a loss sale."""

    replacement_lot_id: str
    replacement_account: AccountKind
    qty: float
    disallowed: float
    permanent: bool  # replacement in IRA/401(k): loss never recovered via basis


@dataclass(frozen=True)
class RealizedLot:
    """One lot (or part of a lot) disposed of in a sale."""

    realized_id: str
    sale_id: str
    lot_id: str
    origin_id: str
    account: AccountKind
    symbol: str
    qty: float
    acquired_on: dt.date
    holding_start: dt.date
    sold_on: dt.date
    proceeds: float
    basis: float
    matches: tuple[WashMatch, ...] = ()

    @property
    def term(self) -> Term:
        return term_of(self.holding_start, self.sold_on)

    @property
    def economic_gain(self) -> float:
        """Proceeds minus basis, before any wash-sale disallowance."""
        return self.proceeds - self.basis

    @property
    def is_loss(self) -> bool:
        return self.economic_gain < 0

    @property
    def disallowed_loss(self) -> float:
        return sum(m.disallowed for m in self.matches)

    @property
    def disallowed_qty(self) -> float:
        return sum(m.qty for m in self.matches)

    @property
    def reportable_gain(self) -> float:
        """Gain or loss as reported: economic gain plus the disallowed loss."""
        return self.economic_gain + self.disallowed_loss

    @property
    def wash_code(self) -> str:
        return "W" if self.matches else ""

    @property
    def loss_per_share(self) -> float:
        return -self.economic_gain / self.qty if self.is_loss else 0.0

    @property
    def unmatched_loss_qty(self) -> float:
        return self.qty - self.disallowed_qty if self.is_loss else 0.0


class LotPick(Model):
    """A specific-identification instruction: sell ``qty`` shares of ``lot_id``."""

    lot_id: str
    qty: float = Field(gt=0)


class SaleRequest(Model):
    """A sale with the lots chosen by specific identification."""

    sale_id: str
    account: AccountKind
    symbol: str
    sold_on: dt.date
    price: float = Field(ge=0)  # net proceeds per share (after commissions)
    picks: tuple[LotPick, ...] = Field(min_length=1)

    @property
    def qty(self) -> float:
        return sum(p.qty for p in self.picks)
