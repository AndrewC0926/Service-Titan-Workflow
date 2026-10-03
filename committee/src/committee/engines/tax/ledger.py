"""In-memory lot ledger with specific identification and wash-sale accounting.

Events must arrive in non-decreasing date order (same-day events keep their
arrival order). ``TaxLedgerStore`` replays persisted events in that order.

Wash-sale rules implemented (IRC 1091, Pub. 550, Rev. Rul. 2008-5):

* A loss sale in the taxable account is a wash sale to the extent that
  substantially identical shares are acquired in ANY account (taxable, IRA,
  401(k)) within ``window_days`` (30) before or after the sale date. Day 31 is
  clear.
* Only the replacement quantity triggers: if fewer replacement shares are
  bought than were sold, only the loss on that many shares is disallowed.
* Replacement shares are matched to loss shares in the order they were
  acquired (earliest first), and loss sales are matched in the order they
  occurred. Each replacement share absorbs at most one disallowed loss
  (``TaxLot.wash_replacement``); matched shares are split into their own lot
  (``<lot_id>/W<n>``).
* Taxable replacement: the disallowed loss per share is added to the
  replacement shares' basis, and their holding period includes the holding
  period of the sold shares (``holding.tacked_holding_start``).
* IRA/401(k) replacement: the loss is permanently disallowed; the replacement
  basis and holding period are NOT adjusted (``WashMatch.permanent``).
* Look-back (retroactive) matching considers lots still open at the sale that
  were acquired within the window before it. Shares from the same purchase as
  the shares sold are never their own replacement ("same-lot" exception), and
  shares already disposed of before the loss sale are not considered.
* Losses in IRA/401(k) accounts are not tracked for wash sales (not
  deductible). IRA/401(k) sales are recorded as ``RealizedLot`` rows but are
  not taxable events; reports filter them out.
"""

from __future__ import annotations

import dataclasses
import datetime as dt

from committee.domain import AccountKind, Lot
from committee.engines.tax.equivalence import Equivalence
from committee.engines.tax.holding import tacked_holding_start
from committee.engines.tax.models import (
    QTY_EPS,
    Purchase,
    RealizedLot,
    SaleRequest,
    TaxLot,
    WashMatch,
)


class LedgerError(ValueError):
    """An event that the ledger cannot apply (unknown lot, oversell, out of order)."""


class LotLedger:
    def __init__(self, window_days: int = 30, equivalence: Equivalence | None = None) -> None:
        if window_days < 30:
            raise ValueError("wash-sale window must be at least 30 days")
        self.window_days = window_days
        self.equivalence = equivalence or Equivalence()
        self._lots: dict[str, TaxLot] = {}
        self._purchases: list[Purchase] = []
        self._purchase_seq: dict[str, int] = {}
        self._realized: list[RealizedLot] = []
        self._used_ids: set[str] = set()
        self._sale_ids: set[str] = set()
        self._splits: dict[str, int] = {}
        self._last_date: dt.date | None = None

    # ----------------------------------------------------------------- reads
    def open_lots(
        self, account: AccountKind | None = None, symbol: str | None = None
    ) -> list[TaxLot]:
        out = [
            lot
            for lot in self._lots.values()
            if (account is None or lot.account == account)
            and (symbol is None or lot.symbol == symbol)
        ]
        return sorted(out, key=lambda lot: (lot.acquired_on, self._seq(lot), lot.lot_id))

    def lot(self, lot_id: str) -> TaxLot:
        try:
            return self._lots[lot_id]
        except KeyError:
            raise LedgerError(f"no open lot {lot_id!r}") from None

    @property
    def purchases(self) -> tuple[Purchase, ...]:
        return tuple(self._purchases)

    def realized(self, year: int | None = None, taxable_only: bool = False) -> list[RealizedLot]:
        return [
            r
            for r in self._realized
            if (year is None or r.sold_on.year == year)
            and (not taxable_only or r.account == "taxable")
        ]

    @property
    def last_date(self) -> dt.date | None:
        return self._last_date

    # ---------------------------------------------------------------- writes
    def add_purchase(self, lot: Lot) -> list[TaxLot]:
        """Record a purchase. Returns the resulting open lot(s) after wash-sale matching."""
        if lot.lot_id in self._used_ids:
            raise LedgerError(f"duplicate lot id {lot.lot_id!r}")
        self._advance(lot.acquired_on)
        self._used_ids.add(lot.lot_id)
        self._purchase_seq[lot.lot_id] = len(self._purchases)
        self._purchases.append(
            Purchase(
                lot.lot_id, lot.account, lot.symbol, lot.qty, lot.cost_per_share, lot.acquired_on
            )
        )
        remaining: TaxLot | None = TaxLot(
            lot_id=lot.lot_id,
            account=lot.account,
            symbol=lot.symbol,
            qty=lot.qty,
            cost_per_share=lot.cost_per_share,
            acquired_on=lot.acquired_on,
            holding_start=lot.acquired_on,
            origin_id=lot.lot_id,
        )
        result: list[TaxLot] = []
        # Look-forward: earlier taxable loss sales within the window, earliest first.
        for idx in self._pending_loss_indexes(lot.symbol, lot.acquired_on):
            if remaining is None:
                break
            r = self._realized[idx]
            q = min(r.unmatched_loss_qty, remaining.qty)
            portion, remaining = self._split(remaining, q)
            adjusted, match = self._absorb(portion, r, q)
            self._realized[idx] = dataclasses.replace(r, matches=(*r.matches, match))
            self._lots[adjusted.lot_id] = adjusted
            result.append(adjusted)
        if remaining is not None:
            self._lots[remaining.lot_id] = remaining
            result.append(remaining)
        return result

    def apply_sale(self, sale: SaleRequest) -> list[RealizedLot]:
        """Apply a sale with specifically identified lots; partial lots are split."""
        self._validate_sale(sale)
        self._advance(sale.sold_on)
        self._sale_ids.add(sale.sale_id)
        new: list[int] = []
        sold_origins: set[str] = set()
        for pick in sale.picks:
            lot = self._lots[pick.lot_id]
            qty = min(pick.qty, lot.qty)
            sold_origins.add(lot.origin_id)
            self._realized.append(
                RealizedLot(
                    realized_id=f"{sale.sale_id}:{lot.lot_id}",
                    sale_id=sale.sale_id,
                    lot_id=lot.lot_id,
                    origin_id=lot.origin_id,
                    account=lot.account,
                    symbol=lot.symbol,
                    qty=qty,
                    acquired_on=lot.acquired_on,
                    holding_start=lot.holding_start,
                    sold_on=sale.sold_on,
                    proceeds=qty * sale.price,
                    basis=qty * lot.cost_per_share,
                )
            )
            new.append(len(self._realized) - 1)
            left = lot.qty - qty
            if left > QTY_EPS:
                self._lots[lot.lot_id] = dataclasses.replace(lot, qty=left)
            else:
                del self._lots[lot.lot_id]
        # Look-back: replacement shares bought within the window before the sale.
        for idx in new:
            r = self._realized[idx]
            if r.account != "taxable" or not r.is_loss:
                continue
            for cand in self._lookback_candidates(r, sold_origins):
                need = self._realized[idx].unmatched_loss_qty
                if need <= QTY_EPS:
                    break
                portion, rest = self._split(cand, min(need, cand.qty))
                adjusted, match = self._absorb(portion, r, portion.qty)
                if rest is not None:
                    self._lots[rest.lot_id] = rest
                self._lots[adjusted.lot_id] = adjusted
                cur = self._realized[idx]
                self._realized[idx] = dataclasses.replace(cur, matches=(*cur.matches, match))
        return [self._realized[i] for i in new]

    # -------------------------------------------------------------- helpers
    def _seq(self, lot: TaxLot) -> int:
        return self._purchase_seq.get(lot.origin_id, 0)

    def _advance(self, on: dt.date) -> None:
        if self._last_date is not None and on < self._last_date:
            raise LedgerError(f"event dated {on} is before the last event {self._last_date}")
        self._last_date = on

    def _validate_sale(self, sale: SaleRequest) -> None:
        if sale.sale_id in self._sale_ids:
            raise LedgerError(f"duplicate sale id {sale.sale_id!r}")
        totals: dict[str, float] = {}
        for p in sale.picks:
            totals[p.lot_id] = totals.get(p.lot_id, 0.0) + p.qty
        if len(totals) != len(sale.picks):
            raise LedgerError("a lot appears twice in one sale")
        for lot_id, qty in totals.items():
            lot = self.lot(lot_id)
            if lot.account != sale.account or lot.symbol != sale.symbol:
                raise LedgerError(
                    f"lot {lot_id} is {lot.symbol} in {lot.account}, "
                    f"not {sale.symbol} in {sale.account}"
                )
            if qty > lot.qty + QTY_EPS:
                raise LedgerError(f"selling {qty} of lot {lot_id} which holds {lot.qty}")

    def _in_window(self, a: dt.date, b: dt.date) -> bool:
        return abs((a - b).days) <= self.window_days

    def _pending_loss_indexes(self, symbol: str, bought_on: dt.date) -> list[int]:
        idx = [
            i
            for i, r in enumerate(self._realized)
            if r.account == "taxable"
            and r.unmatched_loss_qty > QTY_EPS
            and self.equivalence.identical(r.symbol, symbol)
            and r.sold_on <= bought_on
            and self._in_window(r.sold_on, bought_on)
        ]
        return sorted(idx, key=lambda i: (self._realized[i].sold_on, i))

    def _lookback_candidates(self, r: RealizedLot, sold_origins: set[str]) -> list[TaxLot]:
        return [
            lot
            for lot in self.open_lots()
            if not lot.wash_replacement
            and lot.origin_id not in sold_origins
            and self.equivalence.identical(lot.symbol, r.symbol)
            and lot.acquired_on <= r.sold_on
            and self._in_window(lot.acquired_on, r.sold_on)
        ]

    def _split(self, lot: TaxLot, qty: float) -> tuple[TaxLot, TaxLot | None]:
        """Split ``qty`` shares off ``lot``. Returns (portion, rest-or-None).

        The rest keeps the original lot id; a partial portion gets ``<id>/W<n>``.
        """
        if qty >= lot.qty - QTY_EPS:
            return lot, None
        n = self._splits.get(lot.lot_id, 0) + 1
        self._splits[lot.lot_id] = n
        new_id = f"{lot.lot_id}/W{n}"
        self._used_ids.add(new_id)
        return (
            dataclasses.replace(lot, lot_id=new_id, qty=qty),
            dataclasses.replace(lot, qty=lot.qty - qty),
        )

    @staticmethod
    def _absorb(portion: TaxLot, r: RealizedLot, qty: float) -> tuple[TaxLot, WashMatch]:
        per_share = r.loss_per_share
        permanent = portion.tax_deferred
        match = WashMatch(
            replacement_lot_id=portion.lot_id,
            replacement_account=portion.account,
            qty=qty,
            disallowed=qty * per_share,
            permanent=permanent,
        )
        if permanent:
            return dataclasses.replace(portion, wash_replacement=True), match
        adjusted = dataclasses.replace(
            portion,
            cost_per_share=portion.cost_per_share + per_share,
            wash_adjustment_per_share=portion.wash_adjustment_per_share + per_share,
            holding_start=tacked_holding_start(portion.holding_start, r.holding_start, r.sold_on),
            wash_replacement=True,
        )
        return adjusted, match
