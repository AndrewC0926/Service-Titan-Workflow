"""Cross-account wash-sale guard (DESIGN 9).

Blocks any purchase of a substantially identical security in ANY account
(taxable, IRA, 401(k)) within the window (30 days) before or after a loss
sale in the taxable account. ``block_list`` is the set shared with the
screen; ``check_purchase`` gives a verdict and reason for one proposed buy;
``sale_wash_risk`` finds purchases that would make a loss sale today a wash
sale (the "30 days before" side).
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Literal

from committee.domain import TAX_DEFERRED, AccountKind
from committee.engines.tax.labels import labeled
from committee.engines.tax.ledger import LotLedger
from committee.engines.tax.models import Purchase, RealizedLot

Verdict = Literal["ALLOW", "WARN", "BLOCK"]


@dataclass(frozen=True)
class PurchaseVerdict:
    verdict: Verdict
    symbol: str
    account: AccountKind
    on: dt.date
    reason: str
    blocking_sale_ids: tuple[str, ...] = ()
    clear_on: dt.date | None = None  # first date the purchase would be allowed

    @property
    def allowed(self) -> bool:
        return self.verdict != "BLOCK"


class WashSaleGuard:
    def __init__(self, ledger: LotLedger) -> None:
        self.ledger = ledger
        self.window = ledger.window_days
        self.eq = ledger.equivalence

    def loss_sales(self) -> list[RealizedLot]:
        """Taxable sales that realized an economic loss."""
        return [r for r in self.ledger.realized(taxable_only=True) if r.is_loss]

    def _blocking(self, symbol: str, on: dt.date) -> list[RealizedLot]:
        return [
            r
            for r in self.loss_sales()
            if self.eq.identical(r.symbol, symbol) and abs((on - r.sold_on).days) <= self.window
        ]

    def block_list(self, asof: dt.date) -> set[str]:
        """Symbols that may not be bought in any account on ``asof``."""
        out: set[str] = set()
        for r in self.loss_sales():
            if abs((asof - r.sold_on).days) <= self.window:
                out |= self.eq.members(r.symbol)
        return out

    def check_purchase(
        self,
        symbol: str,
        account: AccountKind,
        on: dt.date,
        prices: Mapping[str, float] | None = None,
    ) -> PurchaseVerdict:
        sym = symbol.strip().upper()
        blocking = self._blocking(sym, on)
        if blocking:
            last = max(r.sold_on for r in blocking)
            clear = last + dt.timedelta(days=self.window + 1)
            sales = ", ".join(sorted({f"{r.symbol} on {r.sold_on}" for r in blocking}))
            extra = (
                " In an IRA/401(k) the loss would be permanently disallowed."
                if account in TAX_DEFERRED
                else " The loss would be disallowed and added to the new shares' basis."
            )
            return PurchaseVerdict(
                "BLOCK",
                sym,
                account,
                on,
                labeled(
                    f"BLOCK: buying {sym} in {account} on {on} is within {self.window} days "
                    f"of a taxable loss sale ({sales}).{extra} Clear on {clear}."
                ),
                tuple(sorted({r.sale_id for r in blocking})),
                clear,
            )
        if prices is not None:
            underwater = [
                lot
                for lot in self.ledger.open_lots(account="taxable")
                if self.eq.identical(lot.symbol, sym)
                and lot.symbol in prices
                and lot.unrealized(prices[lot.symbol]) < 0
            ]
            if underwater:
                ids = ", ".join(lot.lot_id for lot in underwater)
                return PurchaseVerdict(
                    "WARN",
                    sym,
                    account,
                    on,
                    labeled(
                        f"WARN: taxable lots of {sym} are at a loss ({ids}); buying now means "
                        f"selling them at a loss within {self.window} days would be a wash sale."
                    ),
                )
        return PurchaseVerdict(
            "ALLOW", sym, account, on, labeled(f"ALLOW: no wash-sale conflict for {sym} on {on}.")
        )

    def sale_wash_risk(
        self, symbol: str, on: dt.date, exclude_origins: Iterable[str] = ()
    ) -> list[Purchase]:
        """Purchases (any account) that would make a loss sale of ``symbol`` on ``on`` a wash sale."""
        excluded = set(exclude_origins)
        return [
            p
            for p in self.ledger.purchases
            if p.lot_id not in excluded
            and self.eq.identical(p.symbol, symbol)
            and 0 <= (on - p.bought_on).days <= self.window
        ]


def wash_sale_block_list(ledger: LotLedger, asof: dt.date) -> set[str]:
    """The block list shared with the screen."""
    return WashSaleGuard(ledger).block_list(asof)


def check_purchase(
    ledger: LotLedger, symbol: str, account: AccountKind, on: dt.date
) -> PurchaseVerdict:
    return WashSaleGuard(ledger).check_purchase(symbol, account, on)
