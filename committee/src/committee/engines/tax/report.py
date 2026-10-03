"""Annual realized-gains report for a CPA (CSV).

Line 1 is a comment header carrying the label "Not tax advice. Confirm with a
CPA."; line 2 holds the column names. One row per realized taxable lot (IRA
and 401(k) sales are not taxable events and are excluded). ``date_acquired``
is the holding-period start, which differs from the purchase date when a wash
sale tacked on the sold shares' holding period; ``date_purchased`` is the
actual trade date. ``gain_loss`` = proceeds - cost_basis + disallowed_loss
(the Form 8949 convention: the disallowed loss is a positive adjustment with
code "W").
"""

from __future__ import annotations

import csv
import io
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path

from committee.engines.tax.labels import DISCLAIMER
from committee.engines.tax.models import RealizedLot

COLUMNS: tuple[str, ...] = (
    "date_acquired",
    "date_purchased",
    "date_sold",
    "symbol",
    "account",
    "lot_id",
    "quantity",
    "proceeds",
    "cost_basis",
    "adjustment_code",
    "disallowed_loss",
    "gain_loss",
    "term",
)


@dataclass(frozen=True)
class RealizedSummary:
    year: int
    short_term: float
    long_term: float
    disallowed: float
    rows: int

    @property
    def total(self) -> float:
        return self.short_term + self.long_term

    def text(self) -> str:
        return (
            f"{DISCLAIMER}\nRealized {self.year}: short-term {self.short_term:,.2f}, "
            f"long-term {self.long_term:,.2f}, total {self.total:,.2f}; "
            f"wash-sale disallowed {self.disallowed:,.2f} ({self.rows} rows)."
        )


def _rows(realized: Iterable[RealizedLot], year: int) -> list[RealizedLot]:
    rows = [r for r in realized if r.account == "taxable" and r.sold_on.year == year]
    return sorted(rows, key=lambda r: (r.sold_on, r.symbol, r.realized_id))


def _money(x: float) -> str:
    return f"{round(x, 2) + 0.0:.2f}"


def summarize(realized: Iterable[RealizedLot], year: int) -> RealizedSummary:
    rows = _rows(realized, year)
    st = sum(r.reportable_gain for r in rows if r.term == "short")
    lt = sum(r.reportable_gain for r in rows if r.term == "long")
    return RealizedSummary(year, st, lt, sum(r.disallowed_loss for r in rows), len(rows))


def realized_csv(realized: Iterable[RealizedLot], year: int) -> str:
    buf = io.StringIO()
    buf.write(f"# {DISCLAIMER} Realized gains and losses, tax year {year}.\n")
    w = csv.writer(buf, lineterminator="\n")
    w.writerow(COLUMNS)
    for r in _rows(realized, year):
        w.writerow(
            (
                r.holding_start.isoformat(),
                r.acquired_on.isoformat(),
                r.sold_on.isoformat(),
                r.symbol,
                r.account,
                r.lot_id,
                f"{r.qty:g}",
                _money(r.proceeds),
                _money(r.basis),
                r.wash_code,
                _money(r.disallowed_loss),
                _money(r.reportable_gain),
                r.term,
            )
        )
    return buf.getvalue()


def write_realized_csv(realized: Iterable[RealizedLot], year: int, out: Path) -> RealizedSummary:
    rows = list(realized)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(realized_csv(rows, year), encoding="utf-8")
    return summarize(rows, year)
