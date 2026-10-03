"""Hand-built synthetic PIT lakes for signal tests (columns follow data.schemas.SCHEMAS)."""

from __future__ import annotations

import datetime as dt
import math
from collections import defaultdict
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import pandas as pd

from committee.data.lake import Lake
from committee.data.pit import PIT
from committee.data.schemas import SCHEMAS

ASOF = dt.date(2026, 9, 27)  # a Sunday
OLD = dt.date(2015, 1, 1)


def kt(d: dt.date, hour: int = 21) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(hour), tzinfo=dt.UTC)


def bdays(n: int, end: dt.date = ASOF) -> list[dt.date]:
    return [t.date() for t in pd.bdate_range(end=end, periods=n)]


def wavy_path(
    n: int, start: float, daily: float, amp: float = 0.01, phase: float = 0.0
) -> list[float]:
    """Deterministic price path: drift ``daily`` plus a sinusoidal wiggle (nonzero vol)."""
    out, p = [], start
    for i in range(n):
        out.append(p)
        p *= 1 + daily + amp * math.sin(i * 0.7 + phase)
    return out


class LakeBuilder:
    def __init__(self, root: Path) -> None:
        self.lake = Lake(root)
        self._rows: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self._n = 0

    # ------------------------------------------------------------ plumbing
    def add(self, table: str, row: dict[str, Any]) -> None:
        missing = set(SCHEMAS[table]) - set(row)
        assert not missing, f"{table} row missing {missing}"
        self._rows[table].append(row)

    def flush(self) -> None:
        for table, rows in self._rows.items():
            self._n += 1
            self.lake.write(table, rows, source="test", ingest_id=f"t{self._n:05d}")
        self._rows.clear()

    def pit(self) -> PIT:
        self.flush()
        return PIT(self.lake)

    # ------------------------------------------------------------ tables
    def security(
        self,
        sid: str,
        ticker: str,
        sector: str = "Information Technology",
        exchange: str | None = "NYSE",
        list_date: dt.date = dt.date(2010, 1, 1),
        delist_date: dt.date | None = None,
        known: dt.date = OLD,
    ) -> None:
        self.add(
            "security_master",
            {
                "security_id": sid,
                "cik": int(sid[3:]) if sid[3:].isdigit() else 0,
                "ticker": ticker,
                "name": ticker + " Inc",
                "exchange": exchange,
                "sector": sector,
                "list_date": list_date,
                "delist_date": delist_date,
                "event_time": kt(known),
                "known_time": kt(known),
            },
        )

    def prices(
        self,
        sid: str,
        path: Sequence[float],
        volume: float,
        end: dt.date = ASOF,
        known_shift_days: int = 0,
    ) -> None:
        for d, p in zip(bdays(len(path), end), path, strict=True):
            self.add(
                "prices_daily",
                {
                    "security_id": sid,
                    "date": d,
                    "open": p,
                    "high": p,
                    "low": p,
                    "close": p,
                    "adj_close": p,
                    "volume": volume,
                    "event_time": kt(d),
                    "known_time": kt(d + dt.timedelta(days=known_shift_days)),
                },
            )

    def fundamental(
        self,
        sid: str,
        metric: str,
        fiscal_period: str,
        value: float,
        period_end: dt.date,
        known: dt.date,
    ) -> None:
        self.add(
            "fundamentals",
            {
                "security_id": sid,
                "metric": metric,
                "fiscal_period": fiscal_period,
                "period_end": period_end,
                "value": value,
                "form": "10-K" if fiscal_period.startswith("FY") else "10-Q",
                "accession": f"acc-{sid}-{fiscal_period}",
                "event_time": kt(period_end),
                "known_time": kt(known),
            },
        )

    def annual(self, sid: str, year: int, known: dt.date | None = None, **metrics: float) -> None:
        pe = dt.date(year, 12, 31)
        for m, v in metrics.items():
            self.fundamental(sid, m, f"FY{year}", v, pe, known or dt.date(year + 1, 2, 15))

    def insider(
        self,
        sid: str,
        insider_id: str,
        txn_date: dt.date,
        code: str = "P",
        shares: float = 1000.0,
        price: float = 10.0,
        *,
        ad: str = "A",
        is_10b5_1: bool = False,
        is_derivative: bool = False,
        is_amendment: bool = False,
        accession: str | None = None,
        line: int = 0,
        filed: dt.date | None = None,
    ) -> None:
        filed_d = filed or txn_date + dt.timedelta(days=2)
        self.add(
            "insider_txns",
            {
                "accession": accession or f"f4-{sid}-{insider_id}-{txn_date}-{code}",
                "line": line,
                "cik": 1,
                "security_id": sid,
                "insider_id": insider_id,
                "insider_name": insider_id,
                "role": "officer",
                "is_officer": True,
                "is_director": False,
                "officer_title": "CEO",
                "txn_code": code,
                "acquired_disposed": ad,
                "shares": shares,
                "price": price,
                "shares_owned_after": None,
                "is_10b5_1": is_10b5_1,
                "is_derivative": is_derivative,
                "is_amendment": is_amendment,
                "txn_date": txn_date,
                "filed_at": kt(filed_d),
                "event_time": kt(txn_date),
                "known_time": kt(filed_d),
            },
        )

    def section(
        self,
        sid: str,
        accession: str,
        form: str,
        period: dt.date,
        item: str,
        text: str,
        known: dt.date,
    ) -> None:
        self.add(
            "filing_sections",
            {
                "accession": accession,
                "security_id": sid,
                "form": form,
                "period": period,
                "item": item,
                "text": text,
                "embedding_ref": None,
                "event_time": kt(period),
                "known_time": kt(known),
            },
        )

    def news(self, sid: str, day: dt.date, i: int) -> None:
        self.add(
            "news",
            {
                "news_id": f"n-{sid}-{day}-{i}",
                "security_id": sid,
                "published_at": kt(day, 14),
                "headline": "h",
                "summary": "s",
                "source_url": "https://example.invalid",
                "publisher": None,
                "event_time": kt(day, 14),
                "known_time": kt(day, 14),
            },
        )

    def short(self, sid: str, settle: dt.date, dtc: float, pct: float) -> None:
        self.add(
            "short_interest",
            {
                "security_id": sid,
                "settle_date": settle,
                "short_shares": 1e6,
                "days_to_cover": dtc,
                "pct_float": pct,
                "event_time": kt(settle),
                "known_time": kt(settle + dt.timedelta(days=8)),
            },
        )

    def holding(self, filer: int, period: dt.date, sid: str) -> None:
        self.add(
            "holdings_13f",
            {
                "filer_cik": filer,
                "period": period,
                "security_id": sid,
                "value_usd": 1e6,
                "shares": 1e4,
                "event_time": kt(period),
                "known_time": kt(period + dt.timedelta(days=45)),
            },
        )

    def estimate(self, sid: str, fp: str, snapshot: dt.date, value: float) -> None:
        self.add(
            "estimates",
            {
                "security_id": sid,
                "fiscal_period": fp,
                "metric": "eps",
                "snapshot": snapshot,
                "value": value,
                "event_time": kt(snapshot),
                "known_time": kt(snapshot),
            },
        )

    # ------------------------------------------------------------ composite helpers
    def company(
        self,
        sid: str,
        ticker: str,
        *,
        sector: str = "Information Technology",
        price: float = 50.0,
        shares: float = 1e8,
        adv_usd: float = 50e6,
        daily: float = 0.0005,
        amp: float = 0.01,
        phase: float = 0.0,
        n_days: int = 300,
        revenue: float = 1e9,
        gross_profit: float = 4e8,
        net_income: float = 1e8,
        cfo: float = 1.5e8,
        cash: float = 2e8,
        debt: float = 1e8,
        total_assets: float = 2e9,
        equity: float = 1e9,
        prior_revenue: float | None = None,
        prior_gross_profit: float | None = None,
        prior_shares: float | None = None,
        **security_kw: Any,
    ) -> None:
        """A complete company whose latest close is exactly ``price``."""
        self.security(sid, ticker, sector=sector, **security_kw)
        path = wavy_path(n_days, 1.0, daily, amp, phase)
        scale = price / path[-1]
        path = [p * scale for p in path]
        self.prices(sid, path, volume=adv_usd / price)
        self.annual(
            sid, 2025,
            revenue=revenue, gross_profit=gross_profit, operating_income=net_income * 1.3,
            net_income=net_income, cfo=cfo, capex=cfo * 0.3, total_assets=total_assets,
            equity=equity, cash=cash, debt=debt, shares_outstanding=shares,
        )  # fmt: skip
        self.annual(
            sid, 2024,
            revenue=prior_revenue if prior_revenue is not None else revenue / 1.05,
            gross_profit=prior_gross_profit if prior_gross_profit is not None else gross_profit / 1.05,
            net_income=net_income * 0.95, cfo=cfo * 0.95,
            shares_outstanding=prior_shares if prior_shares is not None else shares,
        )  # fmt: skip
