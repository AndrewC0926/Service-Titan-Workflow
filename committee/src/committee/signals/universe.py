"""Screen universe filter (DESIGN 6).

A security is in the universe as of ``asof`` when it is in the security master, listed
on or before asof and not delisted by asof, trades on a U.S. exchange (OTC and unknown
exchanges are excluded), has market cap > the asymmetric minimum ($300M), 60-day ADV >
the asymmetric minimum ($3M), and is not in an active M&A deal (``active_mna`` ids,
applied when ``exclude_active_mna`` is configured).

Market cap = latest unadjusted close * latest known shares_outstanding.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass

import pandas as pd

from committee.config.schema import UniverseConfig
from committee.signals.common import as_date, num
from committee.signals.fundamentals import EMPTY, Fundamentals
from committee.signals.prices import EMPTY_STATS, PriceStats

US_EXCHANGES = frozenset(
    {"NYSE", "NASDAQ", "NYSE AMERICAN", "NYSE MKT", "NYSE ARCA", "AMEX", "CBOE", "BATS"}
)


@dataclass(frozen=True)
class Member:
    security_id: str
    ticker: str
    sector: str
    list_date: pd.Timestamp | None
    market_cap: float
    adv: float
    price: PriceStats
    fundamentals: Fundamentals


def market_cap(stats: PriceStats, f: Fundamentals) -> float | None:
    shares = f.stock("shares_outstanding")
    if stats.last_close is None or shares is None:
        return None
    return stats.last_close * shares


def _date(v: object) -> pd.Timestamp | None:
    if v is None or (not isinstance(v, str) and pd.isna(v)):  # type: ignore[call-overload]
        return None
    return as_date(str(v))


def build_universe(
    master: pd.DataFrame,
    asof_date: pd.Timestamp,
    stats: Mapping[str, PriceStats],
    funds: Mapping[str, Fundamentals],
    cfg: UniverseConfig,
    active_mna: frozenset[str] = frozenset(),
) -> tuple[list[Member], dict[str, str]]:
    """(members, {security_id: exclusion reason})."""
    members: list[Member] = []
    excluded: dict[str, str] = {}
    asym = cfg.asymmetric_bet
    for r in master.sort_values("security_id").to_dict("records"):
        sid, ticker = str(r["security_id"]), str(r["ticker"])
        listed, delisted = _date(r.get("list_date")), _date(r.get("delist_date"))
        ps, f = stats.get(sid, EMPTY_STATS), funds.get(sid, EMPTY)
        mcap = market_cap(ps, f)
        exch = str(r.get("exchange") or "").strip().upper()
        reason = None
        if delisted is not None and delisted <= asof_date:
            reason = "delisted"
        elif listed is not None and listed > asof_date:
            reason = "not_yet_listed"
        elif exch not in US_EXCHANGES:
            reason = f"not_us_listed ({exch or 'unknown'})"
        elif cfg.exclude_active_mna and (sid in active_mna or ticker in active_mna):
            reason = "active_mna"
        elif mcap is None:
            reason = "no_market_cap"
        elif not mcap > asym.min_market_cap_usd:
            reason = "market_cap_below_min"
        elif ps.adv is None or not ps.adv > asym.min_adv_usd:
            reason = "adv_below_min"
        if reason is not None or mcap is None or ps.adv is None:
            excluded[sid] = reason or "no_data"
            continue
        members.append(
            Member(
                security_id=sid,
                ticker=ticker,
                sector=str(r.get("sector") or "Unknown"),
                list_date=listed,
                market_cap=mcap,
                adv=float(num(ps.adv) or 0.0),
                price=ps,
                fundamentals=f,
            )
        )
    return members, excluded
