"""Lottery filter for the asymmetric bucket (DESIGN 6). Deterministic, one rule per function.

Each rule returns an exclusion reason or None. Missing-data policy: rules phrased as
"exclude if X" pass when X cannot be evaluated (noted); the cash rule is phrased as a
requirement ("requires positive OCF or 24 months runway") and fails closed.

Rules:
1. max_effect: max single-day return over the past month is in the top decile of the
   eligible universe. Top decile = the top floor(N/10) names by that value (ties at the
   cutoff included); with fewer than 10 names no one is in the top decile.
2. negative_gross_margin: TTM gross profit / revenue < 0.
3. cash_runway: TTM operating cash flow > 0, or cash / (|TTM OCF| / 12) >= 24 months.
4. dilution: shares outstanding up > 10% over the trailing 12 months.
5. chasing: 3-month price return > 100% without a matching revision. A matching
   revision is TTM revenue growth >= 50% or TTM net income growth >= 50% (latest
   fundamentals known at asof), or a 3-month consensus EPS revision >= 25% when
   estimates are enabled.
6. recent_ipo: list_date less than 12 months (365 days) before asof (IPOs and SPACs).
7. penny_stock: last close under ``min_price_usd`` ($5).
8. attention_spike: news count in the last 30 days, as a daily rate, exceeds 5x the
   daily rate of the prior 90 days (with at least 5 recent articles; a zero baseline
   with 5+ recent articles counts as a spike).
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass

import pandas as pd

MAX_DECILE = 10
RUNWAY_MONTHS = 24.0
DILUTION_MAX = 0.10
CHASE_RETURN = 1.0
MATCH_GROWTH = 0.50
MATCH_REVISION = 0.25
IPO_DAYS = 365
ATTENTION_MULTIPLE = 5.0
ATTENTION_MIN_ARTICLES = 5


@dataclass(frozen=True)
class LotteryInputs:
    security_id: str
    asof_date: pd.Timestamp
    max_return_1m: float | None = None
    max_effect_cutoff: float | None = None
    gross_margin: float | None = None
    cfo: float | None = None
    cash: float | None = None
    share_growth: float | None = None
    return_3m: float | None = None
    revenue_growth: float | None = None
    earnings_growth: float | None = None
    eps_revision: float | None = None
    list_date: pd.Timestamp | None = None
    last_close: float | None = None
    min_price: float = 5.0
    news_30d: int = 0
    news_prior_90d: int = 0


@dataclass(frozen=True)
class LotteryResult:
    passed: bool
    reasons: tuple[str, ...]


def max_effect_cutoff(values: Mapping[str, float | None]) -> float | None:
    """The MAX value at the top-decile boundary across the eligible universe."""
    vals = sorted((v for v in values.values() if v is not None), reverse=True)
    k = len(vals) // MAX_DECILE
    return vals[k - 1] if k > 0 else None


def rule_max_effect(x: LotteryInputs) -> str | None:
    if x.max_return_1m is None or x.max_effect_cutoff is None:
        return None
    if x.max_return_1m >= x.max_effect_cutoff:
        return f"max_effect: max daily return {x.max_return_1m:.1%} in top decile"
    return None


def rule_negative_gross_margin(x: LotteryInputs) -> str | None:
    if x.gross_margin is not None and x.gross_margin < 0:
        return f"negative_gross_margin: {x.gross_margin:.1%}"
    return None


def rule_cash_runway(x: LotteryInputs) -> str | None:
    if x.cfo is not None and x.cfo > 0:
        return None
    if x.cfo is None:
        return "cash_runway: operating cash flow unknown"
    if x.cfo == 0:
        return None if (x.cash or 0) > 0 else "cash_runway: no cash and zero operating cash flow"
    months = (x.cash or 0.0) / (abs(x.cfo) / 12.0)
    if months < RUNWAY_MONTHS:
        return f"cash_runway: {months:.1f} months at current burn (< {RUNWAY_MONTHS:.0f})"
    return None


def rule_dilution(x: LotteryInputs) -> str | None:
    if x.share_growth is not None and x.share_growth > DILUTION_MAX:
        return f"dilution: shares +{x.share_growth:.1%} trailing 12 months"
    return None


def rule_chasing(x: LotteryInputs) -> str | None:
    if x.return_3m is None or x.return_3m <= CHASE_RETURN:
        return None
    matched = (
        (x.revenue_growth is not None and x.revenue_growth >= MATCH_GROWTH)
        or (x.earnings_growth is not None and x.earnings_growth >= MATCH_GROWTH)
        or (x.eps_revision is not None and x.eps_revision >= MATCH_REVISION)
    )
    if matched:
        return None
    return f"chasing: +{x.return_3m:.0%} in 3 months without a matching revision"


def rule_recent_ipo(x: LotteryInputs) -> str | None:
    if x.list_date is None:
        return None
    if x.list_date > x.asof_date - pd.Timedelta(days=IPO_DAYS):
        return f"recent_ipo: listed {x.list_date.date()} (< 12 months)"
    return None


def rule_penny_stock(x: LotteryInputs) -> str | None:
    if x.last_close is not None and x.last_close < x.min_price:
        return f"penny_stock: price {x.last_close:.2f} < {x.min_price:.2f}"
    return None


def rule_attention_spike(x: LotteryInputs) -> str | None:
    if x.news_30d < ATTENTION_MIN_ARTICLES:
        return None
    recent_rate, base_rate = x.news_30d / 30.0, x.news_prior_90d / 90.0
    if base_rate == 0 or recent_rate > ATTENTION_MULTIPLE * base_rate:
        return f"attention_spike: {x.news_30d} articles in 30d vs {x.news_prior_90d} in prior 90d"
    return None


RULES: tuple[Callable[[LotteryInputs], str | None], ...] = (
    rule_max_effect,
    rule_negative_gross_margin,
    rule_cash_runway,
    rule_dilution,
    rule_chasing,
    rule_recent_ipo,
    rule_penny_stock,
    rule_attention_spike,
)


def lottery_filter(x: LotteryInputs) -> LotteryResult:
    reasons = tuple(r for r in (rule(x) for rule in RULES) if r is not None)
    return LotteryResult(passed=not reasons, reasons=reasons)
