"""Opportunistic insider purchases and cluster buys (Cohen, Malloy and Pomorski 2012).

Definitions (DESIGN 6):

* Trades counted as history: non-derivative open-market trades (Form 4 code P or S),
  per insider *per issuer*, from filings known as of ``asof``.
* ROUTINE for a trade made in calendar month M of year Y: the insider also traded
  in month M of each of the ``lookback_years`` strictly prior calendar years
  (Y-1, Y-2, Y-3). Trades earlier in year Y (including the as-of month itself)
  never count as history. An insider without that history is OPPORTUNISTIC.
* Qualifying purchase: code P, acquired (A), non-derivative, not under a 10b5-1
  plan, positive shares and price, made by an opportunistic insider, with txn_date
  in the ``score_window_days`` window ending at ``asof`` (inclusive).
* Score = log1p(10_000 * dollars / market_cap), i.e. log1p of opportunistic
  purchase dollars in basis points of market cap. 0 when there are no qualifying
  purchases; None when market cap is unknown.
* Cluster buy: at least ``cluster_min_insiders`` distinct opportunistic insiders with
  qualifying purchases inside some ``cluster_window_days``-day window (inclusive)
  within the score window. ``recent_cluster`` asks the same of the last
  ``recent_days`` days ending at ``asof`` (screen inclusion rule).
* Amendments (4/A): ``PIT.latest`` already keeps the latest version of each
  (accession, line). When an amended filing restates trades, the rows for one
  (issuer, insider, txn_date, code, A/D, derivative) group are taken from the
  latest-filed amendment only, so the original and the amendment never double count.
"""

from __future__ import annotations

import math
from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from committee.config.schema import InsiderSettings
from committee.signals.common import date_col

_GROUP = ["security_id", "insider_id", "txn_date", "txn_code", "acquired_disposed", "is_derivative"]


@dataclass(frozen=True)
class InsiderSignal:
    security_id: str
    score: float | None
    dollars: float
    n_purchases: int
    opportunistic_buyers: tuple[str, ...]
    routine_buyers: tuple[str, ...]
    cluster_buy: bool
    recent_cluster_buy: bool
    cluster_insiders: tuple[str, ...]


def purchase_score(dollars: float, market_cap: float | None) -> float | None:
    """log1p(dollars in basis points of market cap)."""
    if market_cap is None or not market_cap > 0:
        return None
    return math.log1p(1e4 * max(dollars, 0.0) / market_cap)


def _bool(s: pd.Series) -> pd.Series:
    return s.fillna(False).astype(bool)


def normalize(txns: pd.DataFrame) -> pd.DataFrame:
    """Typed copy: dates normalized, flags as bool, codes upper-cased."""
    df = txns.copy()
    if df.empty:
        return df
    df["txn_date"] = date_col(df, "txn_date")
    for c in ("is_10b5_1", "is_derivative", "is_amendment"):
        df[c] = _bool(df[c])
    df["txn_code"] = df["txn_code"].astype(str).str.upper()
    df["acquired_disposed"] = df["acquired_disposed"].astype(str).str.upper()
    df["insider_id"] = df["insider_id"].astype(str)
    df["filed_at"] = pd.to_datetime(df["filed_at"].fillna(df["known_time"]), utc=True)
    return df


def dedupe_amendments(txns: pd.DataFrame) -> pd.DataFrame:
    """Within a trade group that has an amendment, keep only the latest amendment's rows."""
    df = normalize(txns)
    if df.empty:
        return df
    keep = pd.Series(True, index=df.index)
    for _, g in df.groupby(_GROUP, dropna=False):
        amended = g[g["is_amendment"]]
        if amended.empty:
            continue
        last = amended.sort_values(["filed_at", "accession"]).iloc[-1]["accession"]
        keep.loc[g.index] = g["accession"] == last
    return df[keep].reset_index(drop=True)


def is_routine(history: set[tuple[int, int]], year: int, month: int, lookback_years: int) -> bool:
    """Traded in ``month`` of each of the ``lookback_years`` calendar years before ``year``."""
    return all((year - k, month) in history for k in range(1, lookback_years + 1))


def classify(txns: pd.DataFrame, lookback_years: int) -> pd.DataFrame:
    """Open-market (P/S, non-derivative) trades with a boolean ``routine`` column."""
    df = txns[(txns["txn_code"].isin(["P", "S"])) & (~txns["is_derivative"])].copy()
    if df.empty:
        df["routine"] = pd.Series(dtype=bool)
        return df
    years, months = df["txn_date"].dt.year, df["txn_date"].dt.month
    hist: dict[tuple[str, str], set[tuple[int, int]]] = {}
    for key, y, m in zip(
        zip(df["security_id"].astype(str), df["insider_id"], strict=True),
        years,
        months,
        strict=True,
    ):
        hist.setdefault(key, set()).add((int(y), int(m)))
    df["routine"] = [
        is_routine(hist[(str(s), str(i))], int(y), int(m), lookback_years)
        for s, i, y, m in zip(df["security_id"], df["insider_id"], years, months, strict=True)
    ]
    return df


def prepare(txns: pd.DataFrame, lookback_years: int) -> pd.DataFrame:
    """Amendment-deduplicated open-market trades with the ``routine`` flag."""
    return classify(dedupe_amendments(txns), lookback_years)


def qualifying_purchases(
    classified: pd.DataFrame, asof_date: pd.Timestamp, window_days: int
) -> pd.DataFrame:
    """Open-market purchases (routine or not) in the window ending at ``asof_date``,
    with ``dollars``. Callers filter ``~routine`` for the opportunistic subset."""
    df = classified
    start = asof_date - pd.Timedelta(days=window_days - 1)
    shares = pd.to_numeric(df["shares"], errors="coerce")
    price = pd.to_numeric(df["price"], errors="coerce")
    mask = (
        (df["txn_code"] == "P")
        & (df["acquired_disposed"] == "A")
        & (~df["is_10b5_1"])
        & (df["txn_date"] >= start)
        & (df["txn_date"] <= asof_date)
        & (shares > 0)
        & (price > 0)
    )
    out: pd.DataFrame = df[mask].copy()
    out["dollars"] = shares[mask] * price[mask]
    return out


def cluster_insiders(
    dates: pd.Series, insiders: pd.Series, min_insiders: int, window_days: int
) -> tuple[str, ...]:
    """Distinct insiders in the first window (ending on some trade date) that reaches
    ``min_insiders``; empty when no window does."""
    pairs = sorted(zip(dates, insiders.astype(str), strict=True))
    span = pd.Timedelta(days=window_days - 1)
    for end, _ in pairs:
        names = {i for d, i in pairs if end - span <= d <= end}
        if len(names) >= min_insiders:
            return tuple(sorted(names))
    return ()


def insider_signals(
    txns: pd.DataFrame,
    market_caps: Mapping[str, float | None],
    asof_date: pd.Timestamp,
    settings: InsiderSettings,
    recent_days: int,
) -> dict[str, InsiderSignal]:
    """One InsiderSignal per security in ``market_caps``."""
    qual = qualifying_purchases(
        prepare(txns, settings.routine_lookback_years), asof_date, settings.score_window_days
    )
    opp, routine = qual[~qual["routine"]], qual[qual["routine"]]
    recent_start = asof_date - pd.Timedelta(days=recent_days - 1)
    out: dict[str, InsiderSignal] = {}
    for sid, mcap in market_caps.items():
        g = opp[opp["security_id"] == sid]
        dollars = float(g["dollars"].sum()) if not g.empty else 0.0
        cl = cluster_insiders(
            g["txn_date"],
            g["insider_id"],
            settings.cluster_min_insiders,
            settings.cluster_window_days,
        )
        recent = g[g["txn_date"] >= recent_start]
        out[sid] = InsiderSignal(
            security_id=sid,
            score=purchase_score(dollars, mcap),
            dollars=dollars,
            n_purchases=len(g),
            opportunistic_buyers=tuple(sorted(set(g["insider_id"]))),
            routine_buyers=tuple(sorted(set(routine[routine["security_id"] == sid]["insider_id"]))),
            cluster_buy=bool(cl),
            recent_cluster_buy=recent["insider_id"].nunique() >= settings.cluster_min_insiders,
            cluster_insiders=cl,
        )
    return out
