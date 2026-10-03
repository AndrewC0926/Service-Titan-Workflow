"""Point-in-time reads for the signal library.

Every read goes through ``PIT.latest`` (latest version per table key, restricted
to rows with ``as_of(known_time, $asof)``), so nothing filed after ``asof`` can
reach a signal. Windows below only bound how much history is read.
"""

from __future__ import annotations

import datetime as dt
from dataclasses import dataclass
from typing import Any

import pandas as pd

from committee.data.pit import PIT, to_utc
from committee.data.schemas import SCHEMAS
from committee.signals.common import as_date

PRICE_LOOKBACK_DAYS = 400  # 12-1 momentum and 1y vol need ~13 months
FUNDAMENTALS_LOOKBACK_DAYS = 3 * 366
FILINGS_LOOKBACK_DAYS = 3 * 366
NEWS_LOOKBACK_DAYS = 130  # 30-day window + prior 90-day baseline
ESTIMATES_LOOKBACK_DAYS = 200
HOLDINGS_LOOKBACK_DAYS = 400
SHORT_LOOKBACK_DAYS = 120


@dataclass(frozen=True)
class SignalInputs:
    """Everything the screen reads, as of one knowledge time."""

    asof: dt.datetime
    master: pd.DataFrame
    prices: pd.DataFrame
    fundamentals: pd.DataFrame
    insider: pd.DataFrame
    sections: pd.DataFrame
    short_interest: pd.DataFrame
    holdings_13f: pd.DataFrame
    estimates: pd.DataFrame
    news: pd.DataFrame

    @property
    def asof_date(self) -> pd.Timestamp:
        return as_date(self.asof)


def _frame(df: pd.DataFrame, table: str) -> pd.DataFrame:
    """Guarantee the documented columns exist (empty tables included)."""
    cols = [*SCHEMAS[table], "known_time"]
    out = df.copy() if not df.empty else pd.DataFrame(columns=cols)
    for c in cols:
        if c not in out.columns:
            out[c] = None
    return out.reset_index(drop=True)


def _latest(pit: PIT, table: str, asof: dt.datetime, where: str = "TRUE", **p: Any) -> pd.DataFrame:
    if not pit.has(table):
        return _frame(pd.DataFrame(), table)
    return _frame(pit.latest(table, asof, where, **p), table)


def load_inputs(
    pit: PIT, asof: dt.datetime | dt.date | str, *, insider_history_years: int = 3
) -> SignalInputs:
    """Read all screen inputs knowable at ``asof``."""
    t = to_utc(asof)
    d = as_date(t).date()

    def since(days: int) -> dt.date:
        return d - dt.timedelta(days=days)

    # Insider history: trades in the 90-day window may fall in the prior calendar
    # year, whose routine test needs `insider_history_years` years before that.
    insider_start = dt.date(since(366).year - insider_history_years, 1, 1)
    return SignalInputs(
        asof=t,
        master=_latest(pit, "security_master", t),
        prices=_latest(
            pit, "prices_daily", t, "date >= $start AND date <= $end",
            start=since(PRICE_LOOKBACK_DAYS), end=d,
        ),
        fundamentals=_latest(
            pit, "fundamentals", t, "period_end >= $start", start=since(FUNDAMENTALS_LOOKBACK_DAYS)
        ),
        insider=_latest(
            pit, "insider_txns", t, "txn_date >= $start AND txn_date <= $end",
            start=insider_start, end=d,
        ),
        sections=_latest(
            pit, "filing_sections", t, "known_time >= $start",
            start=to_utc(since(FILINGS_LOOKBACK_DAYS)),
        ),
        short_interest=_latest(
            pit, "short_interest", t, "settle_date >= $start AND settle_date <= $end",
            start=since(SHORT_LOOKBACK_DAYS), end=d,
        ),
        holdings_13f=_latest(
            pit, "holdings_13f", t, "period >= $start", start=since(HOLDINGS_LOOKBACK_DAYS)
        ),
        estimates=_latest(
            pit, "estimates", t, "snapshot >= $start AND snapshot <= $end",
            start=since(ESTIMATES_LOOKBACK_DAYS), end=d,
        ),
        news=_latest(
            pit, "news", t, "published_at >= $start",
            start=to_utc(since(NEWS_LOOKBACK_DAYS)),
        ),
    )  # fmt: skip
