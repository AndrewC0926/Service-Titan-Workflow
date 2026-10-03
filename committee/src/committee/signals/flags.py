"""Flag-only signals (no composite weight) and small auxiliary series.

* Short interest: latest settlement on or before asof. ``high_short_interest`` when
  days_to_cover >= 10 or percent of float >= 20%. ``pct_float`` is read as a fraction
  (0.2 = 20%); values above 1 are taken as already in percent.
* 13F crowding: distinct filers holding the name in the latest reported quarter known
  at asof, and the change vs the previous quarter. (DESIGN names "top-50 hedge funds";
  without a curated filer list every 13F filer counts.)
* Earnings revision: 3-month change in consensus EPS for the fiscal period with the
  latest snapshot, (now - then) / max(|then|, 0.01). Feature-flagged by the screen.
* News attention: article counts in the last 30 days and in the 90 days before that.
"""

from __future__ import annotations

from dataclasses import dataclass

import pandas as pd

from committee.signals.common import date_col, num

HIGH_DAYS_TO_COVER = 10.0
HIGH_PCT_FLOAT = 0.20
REVISION_DAYS = 91


@dataclass(frozen=True)
class ShortInterest:
    settle_date: str
    days_to_cover: float | None
    pct_float: float | None
    high: bool


@dataclass(frozen=True)
class Crowding:
    period: str
    holders: int
    holders_prior: int | None
    change: int | None


def short_interest(df: pd.DataFrame, asof_date: pd.Timestamp) -> dict[str, ShortInterest]:
    if df.empty:
        return {}
    d = df.assign(sd=date_col(df, "settle_date"))
    d = d[d["sd"] <= asof_date].sort_values("sd")
    out: dict[str, ShortInterest] = {}
    for sid, g in d.groupby("security_id"):
        last = g.iloc[-1]
        dtc, pf = num(last["days_to_cover"]), num(last["pct_float"])
        if pf is not None and pf > 1:
            pf = pf / 100.0
        high = (dtc is not None and dtc >= HIGH_DAYS_TO_COVER) or (
            pf is not None and pf >= HIGH_PCT_FLOAT
        )
        out[str(sid)] = ShortInterest(str(last["sd"].date()), dtc, pf, high)
    return out


def crowding(df: pd.DataFrame) -> dict[str, Crowding]:
    if df.empty:
        return {}
    d = df.assign(p=date_col(df, "period"))
    counts = d.groupby(["security_id", "p"])["filer_cik"].nunique().reset_index()
    out: dict[str, Crowding] = {}
    for sid, g in counts.groupby("security_id"):
        g = g.sort_values("p")
        last = g.iloc[-1]
        prior = g[g["p"] < last["p"] - pd.Timedelta(days=45)]
        prev = int(prior.iloc[-1]["filer_cik"]) if not prior.empty else None
        n = int(last["filer_cik"])
        out[str(sid)] = Crowding(str(last["p"].date()), n, prev, None if prev is None else n - prev)
    return out


def earnings_revisions(df: pd.DataFrame, asof_date: pd.Timestamp) -> dict[str, float]:
    if df.empty:
        return {}
    d = df[df["metric"].astype(str).str.lower() == "eps"].assign(s=date_col(df, "snapshot"))
    d = d[d["s"] <= asof_date]
    cutoff = asof_date - pd.Timedelta(days=REVISION_DAYS)
    out: dict[str, float] = {}
    for sid, g in d.groupby("security_id"):
        g = g.sort_values(["s", "fiscal_period"], ascending=[True, False])
        fp = g.iloc[-1]["fiscal_period"]
        series = g[g["fiscal_period"] == fp]
        then = series[series["s"] <= cutoff]
        now_v, then_v = (
            num(series.iloc[-1]["value"]),
            (num(then.iloc[-1]["value"]) if not then.empty else None),
        )
        if now_v is None or then_v is None:
            continue
        out[str(sid)] = (now_v - then_v) / max(abs(then_v), 0.01)
    return out


@dataclass(frozen=True)
class NewsCounts:
    last_30d: int
    prior_90d: int


def news_counts(df: pd.DataFrame, asof_date: pd.Timestamp) -> dict[str, NewsCounts]:
    if df.empty:
        return {}
    end = asof_date + pd.Timedelta(days=1)  # asof_date is midnight; include the whole day
    t = date_col(df, "published_at")
    recent_start = end - pd.Timedelta(days=30)
    prior_start = recent_start - pd.Timedelta(days=90)
    d = df.assign(
        recent=(t >= recent_start) & (t < end), prior=(t >= prior_start) & (t < recent_start)
    )
    g = d.groupby("security_id")[["recent", "prior"]].sum()
    return {str(s): NewsCounts(int(r["recent"]), int(r["prior"])) for s, r in g.iterrows()}
