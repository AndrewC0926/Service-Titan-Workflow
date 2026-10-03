"""Price-derived statistics as of a date: liquidity, momentum, risk, MAX, run-ups.

Formulas (all from ``prices_daily`` rows with date <= asof):

* last_close: unadjusted close on the latest trading day (within 10 calendar days).
* adv: mean of close * volume over the last ``adv_window`` trading days (needs at least
  half of them).
* momentum_12_1: adj_close(asof - 1 month) / adj_close(asof - 12 months) - 1, each the
  last price on or before that date (within 10 days). Skips the latest month.
* return_3m: adj_close(latest) / adj_close(asof - 3 months) - 1.
* max_return_1m: largest daily adj_close return with date in (asof - 1 month, asof].
* volatility_1y: std (ddof=1) of daily returns over the last year * sqrt(252); >= 60 obs.
* beta_1y: cov(r_i, r_m) / var(r_m) over the last year, aligned dates, >= 60 obs. The
  market series is supplied or defaults to the equal-weight mean of all loaded names.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import pandas as pd

from committee.signals.common import date_col, num

TOLERANCE_DAYS = 10
MIN_RISK_OBS = 60


@dataclass(frozen=True)
class PriceStats:
    last_close: float | None
    adv: float | None
    momentum_12_1: float | None
    return_3m: float | None
    max_return_1m: float | None
    volatility_1y: float | None
    beta_1y: float | None


EMPTY_STATS = PriceStats(None, None, None, None, None, None, None)


def on_or_before(s: pd.Series, d: pd.Timestamp, tol_days: int = TOLERANCE_DAYS) -> float | None:
    """Last value in ``s`` (date index, sorted) at or before ``d`` and within tolerance."""
    sub = s[(s.index <= d) & (s.index >= d - pd.Timedelta(days=tol_days))].dropna()
    return num(sub.iloc[-1]) if not sub.empty else None


def _ratio(a: float | None, b: float | None) -> float | None:
    return None if a is None or b is None or b <= 0 else a / b - 1


def wide(prices: pd.DataFrame, col: str) -> pd.DataFrame:
    if prices.empty:
        return pd.DataFrame()
    df = prices.assign(d=date_col(prices, "date"))
    return df.pivot_table(index="d", columns="security_id", values=col, aggfunc="last").sort_index()


def equal_weight_market(returns: pd.DataFrame) -> pd.Series:
    return returns.mean(axis=1, skipna=True)


def price_stats(
    prices: pd.DataFrame,
    asof_date: pd.Timestamp,
    adv_window: int,
    market: pd.Series | None = None,
) -> dict[str, PriceStats]:
    if prices.empty:
        return {}
    p = prices[date_col(prices, "date") <= asof_date]
    adj, close = wide(p, "adj_close"), wide(p, "close")
    dollar_vol = wide(p.assign(dv=p["close"] * p["volume"]), "dv")
    rets = adj.pct_change(fill_method=None)
    mkt = equal_weight_market(rets) if market is None else market
    year_ago = asof_date - pd.DateOffset(years=1)
    month_ago = asof_date - pd.DateOffset(months=1)
    out: dict[str, PriceStats] = {}
    for sid in adj.columns:
        a, r = adj[sid].dropna(), rets[sid].dropna()
        dv = dollar_vol[sid].dropna().tail(adv_window)
        r1y = r[r.index > year_ago]
        vol = float(r1y.std(ddof=1)) * math.sqrt(252) if len(r1y) >= MIN_RISK_OBS else None
        beta = None
        if len(r1y) >= MIN_RISK_OBS:
            joined = pd.concat([r1y, mkt], axis=1, join="inner").dropna()
            var = float(joined.iloc[:, 1].var(ddof=1)) if len(joined) >= MIN_RISK_OBS else 0.0
            if var > 0:
                beta = float(joined.iloc[:, 0].cov(joined.iloc[:, 1])) / var
        r1m = r[r.index > month_ago]
        out[str(sid)] = PriceStats(
            last_close=on_or_before(close[sid], asof_date),
            adv=num(dv.mean()) if len(dv) >= max(1, adv_window // 2) else None,
            momentum_12_1=_ratio(on_or_before(a, month_ago), on_or_before(a, year_ago)),
            return_3m=_ratio(
                on_or_before(a, asof_date), on_or_before(a, asof_date - pd.DateOffset(months=3))
            ),
            max_return_1m=num(r1m.max()) if not r1m.empty else None,
            volatility_1y=num(vol),
            beta_1y=num(beta),
        )
    return out
