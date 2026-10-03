"""Ingestion entry points bound to an ``AppContext`` (used by the CLI and the scheduler).

Each ``run_*`` builds the live fetcher from config and secrets unless a fetcher is
passed (tests pass a ``FixtureFetcher``), opens the journal and returns the
journaled ``ingest_summary`` payload.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Sequence
from typing import Any

from committee.context import AppContext
from committee.data.common import IngestError, RunStats, require_secret, utcnow
from committee.data.edgar import ingest_edgar, sec_fetcher
from committee.data.factors import ingest_factors
from committee.data.http import Fetcher, FetchError, HttpFetcher
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.macro import DEFAULT_SERIES, ingest_macro
from committee.data.news import WINDOW_DAYS, ingest_news
from committee.data.prices import ingest_prices
from committee.data.quality import DQReport, Notify, no_email, run_quality
from committee.data.risk_indexes import ingest_risk_indexes
from committee.data.security_master import seed_security_master


def lake_for(ctx: AppContext) -> Lake:
    return Lake(ctx.data_dir)


def raw_for(ctx: AppContext) -> RawZone:
    return RawZone(ctx.raw_dir)


def _sec(ctx: AppContext, fetcher: Fetcher | None) -> Fetcher:
    if fetcher is not None:
        return fetcher
    ua = require_secret(ctx.secrets, "SEC_USER_AGENT")
    return sec_fetcher(ua, ctx.config.app.sec.max_requests_per_second)


def _web(fetcher: Fetcher | None) -> Fetcher:
    return fetcher if fetcher is not None else HttpFetcher(per_second=5.0)


def run_securities(
    ctx: AppContext, *, fetcher: Fetcher | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    f = _sec(ctx, fetcher)
    stats = RunStats("sec_tickers", new_ingest_id())
    ok = 0
    try:
        stats.counts.update(seed_security_master(lake_for(ctx), raw_for(ctx), f, now or utcnow()))
        ok = 1
    except FetchError as e:
        stats.error(f"company tickers: {e}")
    with ctx.journal() as j:
        return stats.finish(j, attempted=1, succeeded=ok)


def run_edgar(
    ctx: AppContext,
    tickers: Sequence[str],
    since: dt.date,
    *,
    fetcher: Fetcher | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    f = _sec(ctx, fetcher)
    with ctx.journal() as j:
        return ingest_edgar(lake_for(ctx), raw_for(ctx), f, tickers, since, journal=j, now=now)


def run_prices(
    ctx: AppContext,
    tickers: Sequence[str],
    since: dt.date,
    *,
    fetcher: Fetcher | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    massive, finnhub = ctx.secrets.get("MASSIVE_KEY"), ctx.secrets.get("FINNHUB_KEY")
    if not massive and not finnhub:
        require_secret(ctx.secrets, "MASSIVE_KEY")  # raises with the standard message
    with ctx.journal() as j:
        return ingest_prices(
            lake_for(ctx), raw_for(ctx), _web(fetcher), tickers, since,
            massive_key=massive, finnhub_key=finnhub, journal=j, now=now,
        )  # fmt: skip


def run_macro(
    ctx: AppContext,
    since: dt.date,
    *,
    series: Sequence[str] = DEFAULT_SERIES,
    fetcher: Fetcher | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    key = require_secret(ctx.secrets, "FRED_KEY")
    with ctx.journal() as j:
        return ingest_macro(
            lake_for(ctx), raw_for(ctx), _web(fetcher), key,
            since=since, series=series, journal=j, now=now,
        )  # fmt: skip


def run_news(
    ctx: AppContext,
    tickers: Sequence[str],
    *,
    days: int = WINDOW_DAYS,
    fetcher: Fetcher | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    key = require_secret(ctx.secrets, "FINNHUB_KEY")
    with ctx.journal() as j:
        return ingest_news(
            lake_for(ctx), raw_for(ctx), _web(fetcher), tickers, key,
            days=days, journal=j, now=now,
        )  # fmt: skip


def run_factors(
    ctx: AppContext, *, fetcher: Fetcher | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    with ctx.journal() as j:
        return ingest_factors(lake_for(ctx), raw_for(ctx), _web(fetcher), journal=j, now=now)


def run_risk_indexes(
    ctx: AppContext, *, fetcher: Fetcher | None = None, now: dt.datetime | None = None
) -> dict[str, Any]:
    with ctx.journal() as j:
        return ingest_risk_indexes(lake_for(ctx), raw_for(ctx), _web(fetcher), journal=j, now=now)


def run_check(
    ctx: AppContext,
    *,
    asof: dt.datetime | dt.date | str | None = None,
    notify: Notify = no_email,
) -> DQReport:
    with ctx.journal() as j:
        return run_quality(lake_for(ctx), asof=asof, journal=j, notify=notify)


__all__ = [
    "IngestError",
    "lake_for",
    "raw_for",
    "run_check",
    "run_edgar",
    "run_factors",
    "run_macro",
    "run_news",
    "run_prices",
    "run_risk_indexes",
    "run_securities",
]
