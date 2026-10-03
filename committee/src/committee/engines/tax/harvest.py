"""Monthly tax-loss harvest scan (DESIGN 9).

A taxable lot qualifies when its unrealized loss is above ``min_loss_usd``
(1,000) AND above ``min_loss_pct_of_basis`` (5%) of its adjusted basis.
Qualifying lots are grouped by symbol into one proposal: sell them and buy
the pre-mapped replacement ETF (``TaxConfig.replacements``; correlated, not
substantially identical).

A symbol is skipped (and the skip is logged) when:
* no replacement is mapped, or the mapped replacement is substantially
  identical to the symbol (configuration error);
* any account bought the symbol (or an identical one) in the last 30 days,
  other than the lots being harvested: the sale would be a wash sale;
* the replacement itself is on the wash-sale block list (for example a
  repeated A -> B -> A harvest inside the window).

Every evaluated pair is logged; ``journal_harvest_scan`` writes them to the
journal as "harvest_proposal" entries.
"""

from __future__ import annotations

import datetime as dt
import logging
from collections.abc import Mapping
from dataclasses import asdict, dataclass
from typing import Any

from committee.config.schema import TaxConfig
from committee.engines.tax.holding import Term
from committee.engines.tax.labels import DISCLAIMER, labeled
from committee.engines.tax.ledger import LotLedger
from committee.engines.tax.models import TaxLot
from committee.engines.tax.rates import TaxRates
from committee.engines.tax.wash_sale import WashSaleGuard
from committee.journal.store import Journal, JournalEntry

log = logging.getLogger(__name__)


@dataclass(frozen=True)
class HarvestLot:
    lot_id: str
    qty: float
    cost_per_share: float
    loss: float  # positive dollars
    loss_pct_of_basis: float
    term: Term


@dataclass(frozen=True)
class HarvestProposal:
    symbol: str
    replacement: str
    asof: dt.date
    price: float
    lots: tuple[HarvestLot, ...]
    qty: float
    total_loss: float
    estimated_tax_saving: float
    replacement_blocked_for_symbol_until: dt.date
    reason: str


@dataclass(frozen=True)
class HarvestSkip:
    symbol: str
    replacement: str | None
    lot_ids: tuple[str, ...]
    reason: str


@dataclass(frozen=True)
class HarvestScan:
    asof: dt.date
    proposals: tuple[HarvestProposal, ...]
    skipped: tuple[HarvestSkip, ...]
    label: str = DISCLAIMER


def qualifies(lot: TaxLot, price: float, min_loss_usd: float, min_loss_pct: float) -> bool:
    loss = -lot.unrealized(price)
    return (
        lot.account == "taxable"
        and lot.basis > 0
        and loss > min_loss_usd
        and loss / lot.basis * 100.0 > min_loss_pct
    )


def harvest_scan(
    ledger: LotLedger,
    prices: Mapping[str, float],
    asof: dt.date,
    cfg: TaxConfig,
    rates: TaxRates | None = None,
) -> HarvestScan:
    rates = rates or TaxRates.from_config(cfg)
    guard = WashSaleGuard(ledger)
    eq = ledger.equivalence
    blocked = guard.block_list(asof)
    by_symbol: dict[str, list[TaxLot]] = {}
    for lot in ledger.open_lots(account="taxable"):
        price = prices.get(lot.symbol)
        if price is not None and qualifies(
            lot, price, cfg.harvest.min_loss_usd, cfg.harvest.min_loss_pct_of_basis
        ):
            by_symbol.setdefault(lot.symbol, []).append(lot)

    proposals: list[HarvestProposal] = []
    skipped: list[HarvestSkip] = []
    for symbol in sorted(by_symbol):
        lots = by_symbol[symbol]
        ids = tuple(lot.lot_id for lot in lots)
        repl = cfg.replacements.get(symbol)
        skip_reason: str | None = None
        if repl is None:
            skip_reason = f"no replacement mapped for {symbol}"
        elif eq.identical(symbol, repl):
            skip_reason = f"replacement {repl} is substantially identical to {symbol}"
        else:
            risky = guard.sale_wash_risk(symbol, asof, {lot.origin_id for lot in lots})
            if risky:
                buys = ", ".join(f"{p.lot_id} ({p.account}, {p.bought_on})" for p in risky)
                skip_reason = f"sale would be a wash sale: {symbol} bought within 30 days: {buys}"
            elif repl in blocked:
                skip_reason = (
                    f"replacement {repl} is on the wash-sale block list (recent loss sale)"
                )
        if skip_reason is not None:
            skip = HarvestSkip(symbol, repl, ids, labeled(f"SKIP {symbol}->{repl}: {skip_reason}."))
            log.info("harvest pair skipped: %s -> %s: %s", symbol, repl, skip_reason)
            skipped.append(skip)
            continue
        assert repl is not None
        price = prices[symbol]
        hlots = tuple(
            HarvestLot(
                lot.lot_id,
                lot.qty,
                lot.cost_per_share,
                -lot.unrealized(price),
                -lot.unrealized(price) / lot.basis * 100.0,
                lot.term(asof),
            )
            for lot in lots
        )
        total_loss = sum(h.loss for h in hlots)
        saving = -sum(rates.tax_on(-h.loss, h.term) for h in hlots)
        until = asof + dt.timedelta(days=ledger.window_days + 1)
        prop = HarvestProposal(
            symbol=symbol,
            replacement=repl,
            asof=asof,
            price=price,
            lots=hlots,
            qty=sum(h.qty for h in hlots),
            total_loss=total_loss,
            estimated_tax_saving=saving,
            replacement_blocked_for_symbol_until=until,
            reason=labeled(
                f"HARVEST {symbol}->{repl}: sell {len(hlots)} lot(s) for a {total_loss:,.2f} loss "
                f"(est. tax saving {saving:,.2f}). Do not buy {symbol} or an identical security "
                f"in ANY account (including IRA/401(k) and dividend reinvestment) before {until}."
            ),
        )
        log.info("harvest pair proposed: %s -> %s, loss %.2f", symbol, repl, total_loss)
        proposals.append(prop)
    return HarvestScan(asof, tuple(proposals), tuple(skipped))


def _payload(item: HarvestProposal | HarvestSkip, status: str, asof: dt.date) -> dict[str, Any]:
    body = asdict(item)
    body.update({"status": status, "asof": asof.isoformat(), "label": DISCLAIMER})
    return body


def journal_harvest_scan(
    journal: Journal, scan: HarvestScan, include_skipped: bool = True
) -> list[JournalEntry]:
    """Write every proposal (and, by default, every skipped pair) as "harvest_proposal"."""
    out = [
        journal.append("harvest_proposal", _payload(p, "proposed", scan.asof))
        for p in scan.proposals
    ]
    if include_skipped:
        out += [
            journal.append("harvest_proposal", _payload(s, "skipped", scan.asof))
            for s in scan.skipped
        ]
    return out
