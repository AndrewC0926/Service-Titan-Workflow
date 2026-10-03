"""Security master with ticker history; delisted names are kept (survivorship).

security_id is ``CIK##########`` — stable across ticker changes. Seeded from
SEC company_tickers_exchange.json; sector from SIC codes in EDGAR submissions.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import Any

import pandas as pd

from committee.data.http import Fetcher
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT

TICKERS_URL = "https://www.sec.gov/files/company_tickers_exchange.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"

# SIC code ranges -> broad sector (GICS-like buckets, good enough for within-sector z-scores).
_SIC_SECTORS: list[tuple[int, int, str]] = [
    (100, 999, "Materials"),
    (1000, 1499, "Materials"),
    (1300, 1399, "Energy"),
    (1500, 1799, "Industrials"),
    (2000, 2199, "Consumer Staples"),
    (2200, 2399, "Consumer Discretionary"),
    (2400, 2799, "Materials"),
    (2800, 2829, "Materials"),
    (2830, 2836, "Health Care"),
    (2837, 2899, "Materials"),
    (2900, 2999, "Energy"),
    (3000, 3499, "Industrials"),
    (3500, 3569, "Industrials"),
    (3570, 3579, "Information Technology"),
    (3580, 3659, "Industrials"),
    (3660, 3699, "Information Technology"),
    (3700, 3799, "Consumer Discretionary"),
    (3800, 3829, "Information Technology"),
    (3830, 3859, "Health Care"),
    (3860, 3999, "Industrials"),
    (4000, 4799, "Industrials"),
    (4800, 4899, "Communication Services"),
    (4900, 4999, "Utilities"),
    (5000, 5199, "Industrials"),
    (5200, 5999, "Consumer Discretionary"),
    (5400, 5499, "Consumer Staples"),
    (6000, 6499, "Financials"),
    (6500, 6799, "Real Estate"),
    (7000, 7369, "Consumer Discretionary"),
    (7370, 7379, "Information Technology"),
    (7380, 7999, "Consumer Discretionary"),
    (8000, 8099, "Health Care"),
    (8100, 8999, "Industrials"),
]


def sic_to_sector(sic: int | str | None) -> str:
    try:
        code = int(sic) if sic not in (None, "") else -1
    except ValueError:
        return "Unknown"
    match = "Unknown"
    best_width = 10**9
    for lo, hi, sector in _SIC_SECTORS:  # narrowest matching range wins
        if lo <= code <= hi and hi - lo < best_width:
            match, best_width = sector, hi - lo
    return match


def security_id_for(cik: int) -> str:
    return f"CIK{int(cik):010d}"


def parse_tickers_exchange(body: bytes) -> pd.DataFrame:
    data = json.loads(body)
    df = pd.DataFrame(data["data"], columns=data["fields"])
    df = df.dropna(subset=["ticker"])
    df["cik"] = df["cik"].astype(int)
    # One security per CIK: keep the first (primary) listing.
    return df.drop_duplicates("cik", keep="first").reset_index(drop=True)


def seed_security_master(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    known_time: dt.datetime,
    sic_by_cik: dict[int, Any] | None = None,
) -> dict[str, int]:
    """Write a new snapshot of the security master. Delists and ticker changes become
    new versions; nothing is ever deleted."""
    body = fetcher.get(TICKERS_URL)
    raw.put("sec_tickers", "company_tickers_exchange", known_time, body)
    cur = parse_tickers_exchange(body)
    ingest = new_ingest_id()
    pit = PIT(lake)
    prev = (
        pit.latest("security_master", known_time) if pit.has("security_master") else pd.DataFrame()
    )
    hist = pit.latest("ticker_history", known_time) if pit.has("ticker_history") else pd.DataFrame()
    pit.close()
    today = known_time.date()
    sic_by_cik = sic_by_cik or {}
    rows: list[dict[str, Any]] = []
    hist_rows: list[dict[str, Any]] = []
    prev_by_id: dict[str, dict[str, Any]] = (
        {str(r["security_id"]): {str(k): v for k, v in r.items()} for r in prev.to_dict("records")}
        if not prev.empty
        else {}
    )
    open_hist = (
        {
            str(r["security_id"]): {str(k): v for k, v in r.items()}
            for r in hist[hist["end_date"].isna()].to_dict("records")
        }
        if not hist.empty
        else {}
    )
    seen: set[str] = set()
    for r in cur.to_dict("records"):
        sid = security_id_for(r["cik"])
        seen.add(sid)
        p = prev_by_id.get(sid)
        sector = (
            sic_to_sector(sic_by_cik.get(r["cik"]))
            if r["cik"] in sic_by_cik
            else (p or {}).get("sector", "Unknown")
        )
        rec = {
            "security_id": sid,
            "cik": int(r["cik"]),
            "ticker": r["ticker"],
            "name": r["name"],
            "exchange": r.get("exchange"),
            "sector": sector,
            "list_date": (p or {}).get("list_date") or today,
            "delist_date": None,
            "event_time": known_time,
            "known_time": known_time,
        }
        changed = (
            p is None
            or any(p.get(k) != rec[k] for k in ("ticker", "name", "exchange", "sector"))
            or not pd.isna(p.get("delist_date"))
        )
        if changed:
            rows.append(rec)
        oh = open_hist.get(sid)
        if oh is None or oh["ticker"] != r["ticker"]:
            if oh is not None:
                hist_rows.append(
                    {**oh, "end_date": today, "event_time": known_time, "known_time": known_time}
                )
            hist_rows.append(
                {
                    "security_id": sid,
                    "ticker": r["ticker"],
                    "start_date": today,
                    "end_date": None,
                    "event_time": known_time,
                    "known_time": known_time,
                }
            )
    delisted = 0
    for sid, p in prev_by_id.items():
        if sid not in seen and pd.isna(p.get("delist_date")):
            rows.append(
                {
                    **{
                        k: p[k]
                        for k in (
                            "security_id",
                            "cik",
                            "ticker",
                            "name",
                            "exchange",
                            "sector",
                            "list_date",
                        )
                    },
                    "delist_date": today,
                    "event_time": known_time,
                    "known_time": known_time,
                }
            )
            oh = open_hist.get(sid)
            if oh is not None:
                hist_rows.append(
                    {**oh, "end_date": today, "event_time": known_time, "known_time": known_time}
                )
            delisted += 1
    for col in ("list_date", "delist_date"):
        for row in rows:
            v = row[col]
            row[col] = (
                None
                if v is None or (not isinstance(v, str) and pd.isna(v))
                else pd.Timestamp(v).date()
            )
    for row in hist_rows:
        for col in ("start_date", "end_date"):
            v = row.get(col)
            row[col] = (
                None
                if v is None or (not isinstance(v, str) and pd.isna(v))
                else pd.Timestamp(v).date()
            )
        row.pop("source", None)
        row.pop("ingest_id", None)
    n = lake.write("security_master", _typed(rows), source="sec_tickers", ingest_id=ingest)
    lake.write("ticker_history", _typed(hist_rows), source="sec_tickers", ingest_id=ingest)
    return {"securities_written": n, "delisted": delisted, "ticker_changes": len(hist_rows)}


def _typed(rows: list[dict[str, Any]]) -> pd.DataFrame:
    df = pd.DataFrame(rows)
    for c in ("list_date", "delist_date", "start_date", "end_date"):
        if c in df.columns:
            df[c] = pd.to_datetime(df[c]).dt.date.astype("object")
            df[c] = df[c].where(df[c].notna(), None)
    return df


def resolve(pit: PIT, ticker: str, asof: dt.datetime | dt.date | str) -> str | None:
    """security_id for a ticker as of a date (ticker history aware)."""
    df = (
        pit.query(
            "SELECT security_id FROM ticker_history WHERE as_of(known_time, $asof) AND ticker = $t "
            "QUALIFY row_number() OVER (PARTITION BY security_id, ticker, start_date ORDER BY known_time DESC) = 1 "
            "ORDER BY start_date DESC",
            asof,
            t=ticker.upper(),
        )
        if pit.has("ticker_history")
        else pd.DataFrame()
    )
    return None if df.empty else str(df.iloc[0]["security_id"])
