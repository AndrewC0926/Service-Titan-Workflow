"""Daily prices and corporate actions: Massive (formerly Polygon.io), Finnhub fallback.

- Bars: Massive ``/v2/aggs/ticker/{T}/range/1/day/{from}/{to}`` with
  ``adjusted=false`` (raw closes). If that fails, Finnhub ``/stock/candle``.
- Actions: Massive ``/v3/reference/splits`` and ``/v3/reference/dividends``.
- ``adj_close`` is computed here, back-adjusted (CRSP style): for each day the
  close is multiplied by 1/ratio for every later split and by (1 - D/P_prev)
  for every later cash dividend, P_prev being the raw close the day before the
  ex-date. Only actions with ex_date <= the run date are applied.

known_time for a bar = 16:00 America/New_York + 30 minutes on its date. The
adj_close level reflects the actions known at ingest time; returns computed
from one as-of snapshot are unaffected, which is why each run refetches the
full window from ``since``. Corporate actions are known from 09:30 New York on
the ex-date. Finnhub has no free split feed; when only Finnhub bars are
available the actions from Massive (if any) are still used.
"""

from __future__ import annotations

import datetime as dt
import json
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any

from committee.data.common import (
    NY,
    Batch,
    IngestError,
    RunStats,
    drop_known,
    existing_keys,
    ny_time,
    resolve_universe,
    utcnow,
)
from committee.data.http import Fetcher, FetchError
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT
from committee.journal.store import Journal

MASSIVE_BASE = "https://api.massive.com"
FINNHUB_BASE = "https://finnhub.io/api/v1"
AGGS_PATH = "/v2/aggs/ticker/{ticker}/range/1/day/{start}/{end}"
SPLITS_PATH = "/v3/reference/splits"
DIVIDENDS_PATH = "/v3/reference/dividends"
CANDLE_PATH = "/stock/candle"
MAX_PAGES = 20
SOURCE = "prices"
PRICE_KEY = ("security_id", "date", "close", "adj_close")
ACTION_KEY = ("security_id", "ex_date", "kind", "ratio", "amount")


@dataclass(frozen=True)
class Bar:
    date: dt.date
    open: float
    high: float
    low: float
    close: float
    volume: float


@dataclass(frozen=True)
class Action:
    ex_date: dt.date
    kind: str  # split | dividend
    ratio: float | None = None  # new shares per old share
    amount: float | None = None  # cash per share


class PriceSourceError(Exception):
    pass


def _ny_date_from_ms(ms: float) -> dt.date:
    return dt.datetime.fromtimestamp(ms / 1000, dt.UTC).astimezone(NY).date()


def parse_massive_aggs(body: bytes) -> list[Bar]:
    data = json.loads(body)
    if data.get("status") not in ("OK", "DELAYED"):
        raise PriceSourceError(f"massive aggs status {data.get('status')!r}")
    return [
        Bar(_ny_date_from_ms(r["t"]), r["o"], r["h"], r["l"], r["c"], float(r.get("v", 0.0)))
        for r in data.get("results") or []
    ]


def parse_massive_splits(body: bytes) -> tuple[list[Action], str | None]:
    data = json.loads(body)
    acts = [
        Action(
            dt.date.fromisoformat(r["execution_date"]),
            "split",
            ratio=float(r["split_to"]) / float(r["split_from"]),
        )
        for r in data.get("results") or []
    ]
    return acts, data.get("next_url")


def parse_massive_dividends(body: bytes) -> tuple[list[Action], str | None]:
    data = json.loads(body)
    acts = [
        Action(
            dt.date.fromisoformat(r["ex_dividend_date"]), "dividend", amount=float(r["cash_amount"])
        )
        for r in data.get("results") or []
        if r.get("cash_amount") and r.get("ex_dividend_date")
    ]
    return acts, data.get("next_url")


def parse_finnhub_candles(body: bytes) -> list[Bar]:
    data = json.loads(body)
    if data.get("s") != "ok":
        raise PriceSourceError(f"finnhub candle status {data.get('s')!r}")
    return [
        Bar(_ny_date_from_ms(t * 1000), o, h, lo, c, float(v))
        for t, o, h, lo, c, v in zip(
            data["t"], data["o"], data["h"], data["l"], data["c"], data["v"], strict=True
        )
    ]


def adjust(bars: Sequence[Bar], actions: Sequence[Action]) -> list[float]:
    """Back-adjusted closes for ``bars`` (sorted ascending by date)."""
    closes = {b.date: b.close for b in bars}
    dates = sorted(closes)
    factors: list[tuple[dt.date, float]] = []
    for a in actions:
        if a.kind == "split" and a.ratio:
            factors.append((a.ex_date, 1.0 / a.ratio))
        elif a.kind == "dividend" and a.amount:
            prev = [d for d in dates if d < a.ex_date]
            if prev and (a.ex_date - prev[-1]).days <= 7 and closes[prev[-1]] > a.amount:
                factors.append((a.ex_date, 1.0 - a.amount / closes[prev[-1]]))
    out = []
    for b in bars:
        f = 1.0
        for ex, k in factors:
            if ex > b.date:
                f *= k
        out.append(b.close * f)
    return out


def _paged(
    fetcher: Fetcher, url: str, params: dict[str, str], parse: Any, raw: RawZone, entity: str
) -> list[Action]:
    acts: list[Action] = []
    next_url: str | None = url
    first = True
    for _ in range(MAX_PAGES):
        if not next_url:
            break
        body = fetcher.get(next_url, params if first else {"apiKey": params["apiKey"]})
        raw.put("massive", entity, utcnow(), body)
        page, next_url = parse(body)
        acts += page
        first = False
    return acts


def fetch_actions(
    fetcher: Fetcher, raw: RawZone, key: str, ticker: str, base: str = MASSIVE_BASE
) -> list[Action]:
    q = {"ticker": ticker, "limit": "1000", "apiKey": key}
    return _paged(
        fetcher, base + SPLITS_PATH, q, parse_massive_splits, raw, f"{ticker}-splits"
    ) + _paged(fetcher, base + DIVIDENDS_PATH, q, parse_massive_dividends, raw, f"{ticker}-divs")


def fetch_bars(
    fetcher: Fetcher,
    raw: RawZone,
    ticker: str,
    since: dt.date,
    until: dt.date,
    *,
    massive_key: str | None,
    finnhub_key: str | None,
) -> tuple[list[Bar], str]:
    """Bars from Massive, falling back to Finnhub; returns (bars, provider)."""
    errors = []
    if massive_key:
        url = MASSIVE_BASE + AGGS_PATH.format(ticker=ticker, start=since, end=until)
        try:
            body = fetcher.get(
                url, {"adjusted": "false", "sort": "asc", "limit": "50000", "apiKey": massive_key}
            )
            raw.put("massive", f"{ticker}-aggs", utcnow(), body)
            return parse_massive_aggs(body), "massive"
        except (FetchError, PriceSourceError, ValueError, KeyError) as e:
            errors.append(f"massive: {e}")
    if finnhub_key:
        start = int(dt.datetime.combine(since, dt.time(), tzinfo=dt.UTC).timestamp())
        end = int(dt.datetime.combine(until, dt.time(23, 59), tzinfo=dt.UTC).timestamp())
        try:
            body = fetcher.get(
                FINNHUB_BASE + CANDLE_PATH,
                {
                    "symbol": ticker,
                    "resolution": "D",
                    "from": str(start),
                    "to": str(end),
                    "token": finnhub_key,
                },
            )
            raw.put("finnhub", f"{ticker}-candle", utcnow(), body)
            return parse_finnhub_candles(body), "finnhub"
        except (FetchError, PriceSourceError, ValueError, KeyError) as e:
            errors.append(f"finnhub: {e}")
    raise PriceSourceError("; ".join(errors) or "no price provider key configured")


def price_rows(
    security_id: str, bars: Sequence[Bar], actions: Sequence[Action], today: dt.date
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    bars = sorted((b for b in bars if b.date <= today), key=lambda b: b.date)
    effective = [a for a in actions if a.ex_date <= today]
    adj = adjust(bars, effective)
    prices = [
        {
            "security_id": security_id,
            "date": b.date,
            "open": b.open,
            "high": b.high,
            "low": b.low,
            "close": b.close,
            "adj_close": round(a, 6),
            "volume": b.volume,
            "event_time": ny_time(b.date, 16),
            "known_time": ny_time(b.date, 16, 30),
        }
        for b, a in zip(bars, adj, strict=True)
    ]
    acts = [
        {
            "security_id": security_id,
            "ex_date": a.ex_date,
            "kind": a.kind,
            "ratio": a.ratio,
            "amount": a.amount,
            "event_time": ny_time(a.ex_date, 9, 30),
            "known_time": ny_time(a.ex_date, 9, 30),
        }
        for a in effective
    ]
    return prices, acts


def ingest_prices(
    lake: Lake,
    raw: RawZone,
    fetcher: Fetcher,
    tickers: Sequence[str],
    since: dt.date,
    *,
    massive_key: str | None,
    finnhub_key: str | None,
    until: dt.date | None = None,
    journal: Journal | None = None,
    now: dt.datetime | None = None,
) -> dict[str, Any]:
    if not massive_key and not finnhub_key:
        raise IngestError("set MASSIVE_KEY or FINNHUB_KEY to ingest prices")
    now = now or utcnow()
    today = now.astimezone(NY).date()
    stats = RunStats(SOURCE, new_ingest_id())
    pit = PIT(lake)
    try:
        resolved = resolve_universe(pit, tickers, now, stats)
        known_p = existing_keys(pit, "prices_daily", PRICE_KEY)
        known_a = existing_keys(pit, "corporate_actions", ACTION_KEY)
    finally:
        pit.close()
    batch, ok = Batch(), 0
    for ticker, sid in resolved.items():
        try:
            bars, provider = fetch_bars(
                fetcher, raw, ticker, since, until or today,
                massive_key=massive_key, finnhub_key=finnhub_key,
            )  # fmt: skip
        except PriceSourceError as e:
            stats.error(f"{ticker}: {e}")
            continue
        ok += 1
        stats.counts[f"provider:{provider}"] += 1
        actions: list[Action] = []
        if massive_key:
            try:
                actions = fetch_actions(fetcher, raw, massive_key, ticker)
            except (FetchError, ValueError, KeyError) as e:
                stats.error(f"{ticker} corporate actions: {e}")
        prices, acts = price_rows(sid, bars, actions, today)
        prices = drop_known(prices, known_p, PRICE_KEY)
        acts = drop_known(acts, known_a, ACTION_KEY)
        batch.add("prices_daily", prices)
        batch.add("corporate_actions", acts)
        stats.counts["prices_daily"] += len(prices)
        stats.counts["corporate_actions"] += len(acts)
    stats.written = batch.flush(lake, SOURCE, stats.ingest_id)
    return stats.finish(journal, attempted=len(resolved), succeeded=ok)
