"""Column contracts for every PIT lake table (DESIGN 5).

Writers must provide at least these columns (plus event_time and known_time;
the lake adds source and ingest_id). Readers may rely on them. Types are the
pandas/Arrow logical types written.

Times are UTC timestamps; ``date`` columns are calendar dates.
"""

from __future__ import annotations

SCHEMAS: dict[str, dict[str, str]] = {
    "security_master": {
        "security_id": "str  CIK##########",
        "cik": "int",
        "ticker": "str  current ticker",
        "name": "str",
        "exchange": "str|null",
        "sector": "str  broad sector from SIC",
        "list_date": "date",
        "delist_date": "date|null",
    },
    "ticker_history": {
        "security_id": "str",
        "ticker": "str",
        "start_date": "date",
        "end_date": "date|null",
    },
    "prices_daily": {
        # known_time = market close (16:00 ET) + 30 minutes
        "security_id": "str",
        "date": "date",
        "open": "float",
        "high": "float",
        "low": "float",
        "close": "float  unadjusted",
        "adj_close": "float  split- and dividend-adjusted",
        "volume": "float",
    },
    "corporate_actions": {
        "security_id": "str",
        "ex_date": "date",
        "kind": "str  split|dividend",
        "ratio": "float|null  split ratio new/old",
        "amount": "float|null  cash dividend per share",
    },
    "filings": {
        # known_time = EDGAR acceptance datetime
        "accession": "str",
        "cik": "int",
        "security_id": "str",
        "form": "str  10-K, 10-Q, 8-K, 4, 4/A, 3, 5, 13F-HR ...",
        "period": "date|null",
        "accepted_at": "timestamp",
        "url": "str",
        "items": "str|null  comma-separated 8-K item numbers e.g. '2.02,9.01'",
        "sections_hash": "str|null",
    },
    "filing_sections": {
        "accession": "str",
        "security_id": "str",
        "form": "str",
        "period": "date|null",
        "item": "str  '1A' or '7'",
        "text": "str  untrusted",
        "embedding_ref": "str|null",
    },
    "insider_txns": {
        # known_time = Form 4 acceptance datetime; event_time = txn_date
        "accession": "str",
        "line": "int  row index within the filing",
        "cik": "int  issuer CIK",
        "security_id": "str",
        "insider_id": "str  reporting owner CIK",
        "insider_name": "str",
        "role": "str  officer|director|ten_pct_owner|other (comma-joined if several)",
        "is_officer": "bool",
        "is_director": "bool",
        "officer_title": "str|null",
        "txn_code": "str  P, S, A, M, F, G ...",
        "acquired_disposed": "str  A|D",
        "shares": "float",
        "price": "float|null",
        "shares_owned_after": "float|null",
        "is_10b5_1": "bool",
        "is_derivative": "bool",
        "is_amendment": "bool",
        "txn_date": "date",
        "filed_at": "timestamp",
    },
    "fundamentals": {
        # known_time = filing acceptance datetime; restatements are new rows
        "security_id": "str",
        "metric": "str  revenue, gross_profit, operating_income, net_income, cfo, capex, "
        "total_assets, total_liabilities, equity, cash, debt, shares_outstanding, "
        "interest_expense, ebit, ebitda, book_value",
        "fiscal_period": "str  e.g. 2026Q2 or FY2025",
        "period_end": "date",
        "value": "float",
        "form": "str",
        "accession": "str",
    },
    "macro_series": {
        # known_time = vintage (release) date
        "series_id": "str  FRED id",
        "obs_date": "date",
        "value": "float",
        "vintage_date": "date",
    },
    "news": {
        # known_time = published_at
        "news_id": "str",
        "security_id": "str",
        "published_at": "timestamp",
        "headline": "str  untrusted",
        "summary": "str  untrusted",
        "source_url": "str",
        "publisher": "str|null",
    },
    "factor_returns": {
        "dataset": "str  ff5_daily, ff5_monthly, mom_daily, mom_monthly, jkp",
        "factor": "str  mkt_rf, smb, hml, rmw, cma, mom, rf",
        "date": "date",
        "value": "float  decimal return (0.01 = 1%)",
    },
    "risk_indexes": {
        "index_id": "str  gpr, gpr_daily, epu",
        "obs_date": "date",
        "value": "float",
    },
    "short_interest": {
        "security_id": "str",
        "settle_date": "date",
        "short_shares": "float",
        "days_to_cover": "float|null",
        "pct_float": "float|null",
    },
    "holdings_13f": {
        "filer_cik": "int",
        "period": "date",
        "security_id": "str",
        "value_usd": "float",
        "shares": "float",
    },
    "estimates": {
        "security_id": "str",
        "fiscal_period": "str",
        "metric": "str  eps",
        "snapshot": "date",
        "value": "float",
    },
    "signals": {
        "security_id": "str",
        "signal_name": "str",
        "asof": "date",
        "value": "float|null",
        "zscore": "float|null",
    },
    "dead_letter": {
        "source": "str",
        "entity": "str",
        "ref": "str  accession or url",
        "error": "str",
    },
}
