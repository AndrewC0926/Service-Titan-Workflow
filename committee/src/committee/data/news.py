"""Finnhub company news -> ``news``.

known_time = the article's published time. Headlines and summaries are
untrusted third-party text: stored verbatim here and wrapped in an explicit
untrusted-content delimiter by the packet builder, never interpreted.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from typing import Any

from committee.data.common import (
    Batch,
    RunStats,
    drop_known,
    existing_keys,
    resolve_universe,
    utcnow,
)
from committee.data.http import Fetcher, FetchError
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.journal.store import Journal

COMPANY_NEWS_URL = "https://finnhub.io/api/v1/company-news"
SOURCE = "finnhub_news"
WINDOW_DAYS = 30


def parse_company_news(body: bytes, security_id: str) -> list[dict[str, Any]]:
    items = json.loads(body)
    if not isinstance(items, list):
        raise ValueError(f"unexpected company-news payload: {str(items)[:200]}")
    rows = []
    for a in items:
        if not a.get("datetime") or not a.get("headline"):
            continue
        published = dt.datetime.fromtimestamp(int(a["datetime"]), dt.UTC)
        rows.append(
            {
                "news_id": f"finnhub:{a.get('id') or a.get('url')}",
                "security_id": security_id,
                "published_at": published,
                "headline": str(a.get("headline", "")),
                "summary": str(a.get("summary", "")),
                "source_url": str(a.get("url", "")),
                "publisher": a.get("source") or None,
                "event_time": published,
                "known_time": published,
            }
        )
    return rows


def ingest_news(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    tickers: Sequence[str],
    api_key: str,
    *,
    days: int = WINDOW_DAYS,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    now = now or utcnow()
    stats = RunStats(SOURCE, new_ingest_id())
    pit = PIT(lake)
    try:
        resolved = resolve_universe(pit, tickers, now, stats)
        known = existing_keys(pit, "news", ["news_id"])
    finally:
        pit.close()
    start = (now - dt.timedelta(days=days)).date().isoformat()
    batch, ok = Batch(), 0
    for ticker, sid in resolved.items():
        try:
            body = fetcher.get(
                COMPANY_NEWS_URL,
                {"symbol": ticker, "from": start, "to": now.date().isoformat(), "token": api_key},
            )
            raw.put(SOURCE, ticker, now, body)
            rows = parse_company_news(body, sid)
        except (FetchError, ValueError, KeyError) as e:
            stats.error(f"{ticker}: {e}")
            continue
        ok += 1
        rows = [r for r in rows if r["published_at"] <= now]
        rows = drop_known(rows, known, ["news_id"])
        batch.add("news", rows)
        stats.counts[ticker] += len(rows)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(resolved), succeeded=ok)
