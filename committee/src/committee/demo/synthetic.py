"""Synthetic point-in-time data for the offline demo. Fictional companies only;
every row carries a realistic known_time so the as-of machinery is exercised."""

from __future__ import annotations

import datetime as dt
from typing import Any

import numpy as np
import pandas as pd

from committee.data.lake import Lake, new_ingest_id

SECTORS = ("Information Technology", "Industrials", "Health Care", "Energy")
ETFS = ("SPY", "VTI", "AVUV", "VEA", "AVDV", "VWO", "DBMF", "SGOV")
SOURCE = "synthetic"


def _kt(d: dt.date, hour: int = 21) -> dt.datetime:
    return dt.datetime.combine(d, dt.time(hour, 0), tzinfo=dt.UTC)


def _sid(i: int) -> str:
    return f"CIK{9_000_000 + i:010d}"


def business_days(end: dt.date, n: int) -> list[dt.date]:
    return [d.date() for d in pd.bdate_range(end=end, periods=n)]


def build_lake(lake: Lake, asof: dt.date, n_companies: int = 24, seed: int = 7) -> dict[str, Any]:
    rng = np.random.default_rng(seed)
    days = business_days(asof - dt.timedelta(days=1), 520)
    listed = dt.date(2015, 1, 2)
    sec, hist, prices, funds, ins, sections, news, filings = [], [], [], [], [], [], [], []
    names: dict[str, str] = {}
    market = np.cumsum(rng.normal(0.0004, 0.010, len(days)))

    def add_security(i: int, ticker: str, name: str, sector: str, exchange: str = "NYSE") -> str:
        s = _sid(i)
        names[ticker] = s
        sec.append(
            {
                "security_id": s,
                "cik": 9_000_000 + i,
                "ticker": ticker,
                "name": name,
                "exchange": exchange,
                "sector": sector,
                "list_date": listed,
                "delist_date": None,
                "event_time": _kt(listed),
                "known_time": _kt(listed),
            }
        )
        hist.append(
            {
                "security_id": s,
                "ticker": ticker,
                "start_date": listed,
                "end_date": None,
                "event_time": _kt(listed),
                "known_time": _kt(listed),
            }
        )
        return s

    def add_prices(
        s: str, start_price: float, beta: float, drift: float, vol: float, adv: float
    ) -> None:
        idio = np.cumsum(rng.normal(drift, vol, len(days)))
        path = start_price * np.exp(beta * market + idio)
        for d, px in zip(days, path, strict=True):
            prices.append(
                {
                    "security_id": s,
                    "date": d,
                    "open": px,
                    "high": px * 1.01,
                    "low": px * 0.99,
                    "close": px,
                    "adj_close": px,
                    "volume": adv / px,
                    "event_time": _kt(d, 20),
                    "known_time": _kt(d, 20) + dt.timedelta(minutes=30),
                }
            )

    for k, t in enumerate(ETFS):
        s = add_security(900 + k, t, f"{t} Synthetic Index Fund", "Fund", "NYSE ARCA")
        add_prices(s, 100.0, 1.0 if t not in ("DBMF", "SGOV") else 0.0, 0.0001, 0.002, 5e8)

    quarters = pd.date_range(end=asof - dt.timedelta(days=45), periods=12, freq="QE")
    for i in range(n_companies):
        sector = SECTORS[i % len(SECTORS)]
        ticker = f"Z{chr(65 + i // 26)}{chr(65 + i % 26)}"
        s = add_security(i + 1, ticker, f"Fictional {ticker} Holdings Inc", sector)
        shares = float(rng.uniform(5e7, 4e8))
        price0 = float(rng.uniform(15, 150))
        add_prices(
            s,
            price0,
            float(rng.uniform(0.7, 1.4)),
            float(rng.normal(0.0003, 0.0006)),
            float(rng.uniform(0.008, 0.022)),
            float(rng.uniform(4e6, 2e8)),
        )
        rev = float(rng.uniform(2e8, 3e9)) / 4
        growth = float(rng.uniform(-0.02, 0.08))
        margin = float(rng.uniform(0.2, 0.6))
        for qi, qend in enumerate(quarters):
            pe = qend.date()
            known = pe + dt.timedelta(days=40)
            r = rev * (1 + growth) ** qi
            fp = f"{pe.year}Q{(pe.month - 1) // 3 + 1}"
            vals = {
                "revenue": r,
                "gross_profit": r * margin,
                "operating_income": r * margin * 0.4,
                "ebit": r * margin * 0.4,
                "net_income": r * margin * 0.3,
                "cfo": r * margin * 0.35,
                "capex": r * 0.05,
                "total_assets": r * 8,
                "total_liabilities": r * 4,
                "equity": r * 4,
                "cash": r * 0.8,
                "debt": r * 1.5,
                "shares_outstanding": shares,
                "interest_expense": r * 0.01,
            }
            for m, v in vals.items():
                funds.append(
                    {
                        "security_id": s,
                        "metric": m,
                        "fiscal_period": fp,
                        "period_end": pe,
                        "value": float(v),
                        "form": "10-Q",
                        "accession": f"{s}-{fp}",
                        "event_time": _kt(pe),
                        "known_time": _kt(known),
                    }
                )
        if i % 5 == 0:  # opportunistic cluster buys at a few names
            for j in range(3):
                d = asof - dt.timedelta(days=5 + 6 * j)
                acc = f"{s}-F4-{j}"
                ins.append(
                    {
                        "accession": acc,
                        "line": 0,
                        "cik": 9_000_000 + i + 1,
                        "security_id": s,
                        "insider_id": f"{s}-I{j}",
                        "insider_name": f"Insider {j}",
                        "role": "director",
                        "is_officer": False,
                        "is_director": True,
                        "officer_title": None,
                        "txn_code": "P",
                        "acquired_disposed": "A",
                        "shares": 20_000.0,
                        "price": price0,
                        "shares_owned_after": 50_000.0,
                        "is_10b5_1": False,
                        "is_derivative": False,
                        "is_amendment": False,
                        "txn_date": d,
                        "filed_at": _kt(d + dt.timedelta(days=2)),
                        "event_time": _kt(d),
                        "known_time": _kt(d + dt.timedelta(days=2)),
                    }
                )
                filings.append(
                    {
                        "accession": acc,
                        "cik": 9_000_000 + i + 1,
                        "security_id": s,
                        "form": "4",
                        "period": d,
                        "accepted_at": _kt(d + dt.timedelta(days=2)),
                        "url": "https://example.invalid",
                        "items": None,
                        "sections_hash": None,
                        "n_txns": 1,
                        "event_time": _kt(d),
                        "known_time": _kt(d + dt.timedelta(days=2)),
                    }
                )
        for y, text in (
            (asof.year - 2, "Competition may reduce margins.\n\nWe depend on key suppliers."),
            (
                asof.year - 1,
                "Competition may reduce margins.\n\nWe depend on key suppliers."
                + ("\n\nA material weakness was identified." if i % 7 == 0 else ""),
            ),
        ):
            pe, known = dt.date(y, 12, 31), dt.date(y + 1, 2, 20)
            for item, body in (("1A", text), ("7", "Revenue grew modestly.")):
                sections.append(
                    {
                        "accession": f"{s}-10K-{y}",
                        "security_id": s,
                        "form": "10-K",
                        "period": pe,
                        "item": item,
                        "text": body,
                        "embedding_ref": None,
                        "event_time": _kt(pe),
                        "known_time": _kt(known),
                    }
                )
        for j in range(3):
            d = asof - dt.timedelta(days=3 + 7 * j)
            news.append(
                {
                    "news_id": f"{s}-N{j}",
                    "security_id": s,
                    "published_at": _kt(d, 14),
                    "headline": f"{ticker} reports routine update {j}",
                    "summary": "Fictional Holdings announced an operational update.",
                    "source_url": "https://example.invalid/news",
                    "publisher": "Synthetic Wire",
                    "event_time": _kt(d, 14),
                    "known_time": _kt(d, 14),
                }
            )

    macro = []
    for sid_, base in (
        ("DGS10", 4.5),
        ("DGS2", 4.0),
        ("DFF", 4.25),
        ("T10YIE", 2.3),
        ("UNRATE", 4.3),
        ("DCOILBRENTEU", 82.0),
        ("PCEPILFE", 120.0),
    ):
        for d in business_days(asof - dt.timedelta(days=2), 130)[::5]:
            macro.append(
                {
                    "series_id": sid_,
                    "obs_date": d,
                    "value": base + float(rng.normal(0, 0.05)),
                    "vintage_date": d + dt.timedelta(days=1),
                    "event_time": _kt(d),
                    "known_time": _kt(d + dt.timedelta(days=1)),
                }
            )
    risk = [
        {
            "index_id": idx,
            "obs_date": dt.date(asof.year, m, 1),
            "value": v,
            "event_time": _kt(dt.date(asof.year, m, 1)),
            "known_time": _kt(dt.date(asof.year, m, 1) + dt.timedelta(days=40)),
        }
        for idx, v in (("gpr", 140.0), ("epu", 200.0))
        for m in range(1, 8)
    ]

    ing = new_ingest_id()
    counts = {}
    for table, rows in (
        ("security_master", sec),
        ("ticker_history", hist),
        ("prices_daily", prices),
        ("fundamentals", funds),
        ("insider_txns", ins),
        ("filings", filings),
        ("filing_sections", sections),
        ("news", news),
        ("macro_series", macro),
        ("risk_indexes", risk),
    ):
        counts[table] = lake.write(table, pd.DataFrame(rows), source=SOURCE, ingest_id=ing)
    return {"counts": counts, "tickers": names}
