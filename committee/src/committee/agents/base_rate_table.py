"""Reference-class outcome table for the Base-Rate agent, computed in code (DESIGN 7, P8).

Pipeline (all pure over DataFrames, so it is testable with synthetic data):

1. ``forward_outcomes``: from daily adjusted closes and a benchmark level series,
   compute per (security, date) the excess return over 3/6/12 months and the
   maximum drawdown over the following 12 months.
2. ``reference_class_table``: pick historical samples similar to the target
   (sector, size bucket, signal profile), relaxing the match until the sample
   is large enough, and report outcome frequencies with sample sizes.

``load_samples_from_pit`` builds the sample frame from the PIT lake. Every
query filters with ``as_of(known_time, $asof)``, so forward returns that were
not yet knowable at the review's as-of date are simply missing (NaN) and are
excluded from the frequencies.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Mapping
from typing import Any

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field

HORIZON_DAYS: dict[int, int] = {3: 63, 6: 126, 12: 252}
DRAWDOWN_DAYS = 252
DRAWDOWN_THRESHOLD = -0.30


def size_bucket(market_cap: float | None) -> str:
    if market_cap is None or not np.isfinite(market_cap) or market_cap <= 0:
        return "unknown"
    if market_cap < 2e9:
        return "small"
    if market_cap < 10e9:
        return "mid"
    return "large"


class Profile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    sector: str | None
    size_bucket: str
    signals: frozenset[str] = frozenset()


class BaseRateOutcome(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    event: str
    horizon_months: int
    frequency: float | None
    n: int


class BaseRateTable(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    match_level: str
    description: str
    n_samples: int
    n_securities: int
    outcomes: list[BaseRateOutcome] = Field(default_factory=list)

    def frequency(self, event: str, horizon: int) -> float | None:
        for o in self.outcomes:
            if o.event == event and o.horizon_months == horizon:
                return o.frequency
        return None

    def as_evidence(self) -> list[dict[str, Any]]:
        """One packet row: the table, computed by code from PIT data."""
        return [
            {
                "computed_by": "code (point-in-time prices)",
                "match_level": self.match_level,
                "reference_class": self.description,
                "n_samples": self.n_samples,
                "n_securities": self.n_securities,
                "outcomes": [o.model_dump() for o in self.outcomes],
            }
        ]


# ------------------------------------------------------------------- outcomes
def _forward(series: pd.Series, n: int) -> pd.Series:
    return series.shift(-n) / series - 1.0


def _future_min_ratio(series: pd.Series, n: int) -> pd.Series:
    """min(p[t+1..t+n]) / p[t] - 1, NaN unless the full window is known."""
    fut_min = series[::-1].rolling(n, min_periods=n).min()[::-1].shift(-1)
    return fut_min / series - 1.0


def forward_outcomes(
    prices: pd.DataFrame,
    benchmark: pd.Series,
    horizons: Mapping[int, int] = HORIZON_DAYS,
    drawdown_days: int = DRAWDOWN_DAYS,
) -> pd.DataFrame:
    """Per (security_id, date): excess_{h}m vs benchmark and max_dd_12m.

    ``prices`` has columns security_id, date, adj_close. ``benchmark`` is a
    level series indexed by date (e.g. SPY total-return index).
    """
    bench = benchmark.sort_index().astype(float)
    bench.index = pd.to_datetime(bench.index)
    bench_fwd = {h: _forward(bench, n) for h, n in horizons.items()}
    frames = []
    df = prices[["security_id", "date", "adj_close"]].copy()
    df["date"] = pd.to_datetime(df["date"])
    for sid, g in df.sort_values("date").groupby("security_id", sort=True):
        s = g.set_index("date")["adj_close"].astype(float)
        out = pd.DataFrame(index=s.index)
        out["security_id"] = sid
        for h, n in horizons.items():
            out[f"ret_{h}m"] = _forward(s, n)
            out[f"excess_{h}m"] = out[f"ret_{h}m"] - bench_fwd[h].reindex(s.index)
        out["max_dd_12m"] = _future_min_ratio(s, drawdown_days)
        frames.append(out.reset_index())
    if not frames:
        cols = ["security_id", "date", *[f"excess_{h}m" for h in horizons], "max_dd_12m"]
        return pd.DataFrame(columns=cols)
    return pd.concat(frames, ignore_index=True)


# ------------------------------------------------------------- reference class
def _signal_set(v: Any) -> frozenset[str]:
    if isinstance(v, frozenset | set | list | tuple):
        return frozenset(str(x) for x in v)
    if isinstance(v, str) and v:
        return frozenset(x for x in v.split("|") if x)
    return frozenset()


def _signals_match(sample: frozenset[str], target: frozenset[str]) -> bool:
    if not target:
        return not sample
    return len(sample & target) >= max(1, (len(target) + 1) // 2)


def _levels(profile: Profile) -> list[tuple[str, str, list[str]]]:
    sig = "+".join(sorted(profile.signals)) or "no flagged signals"
    return [
        (
            "sector+size+signals",
            f"{profile.sector} / {profile.size_bucket} cap / signals: {sig}",
            ["sector", "size", "signals"],
        ),
        ("sector+size", f"{profile.sector} / {profile.size_bucket} cap", ["sector", "size"]),
        (
            "size+signals",
            f"all sectors / {profile.size_bucket} cap / signals: {sig}",
            ["size", "signals"],
        ),
        ("size", f"all sectors / {profile.size_bucket} cap", ["size"]),
        ("all", "all stocks in the point-in-time universe", []),
    ]


def reference_class_table(
    samples: pd.DataFrame,
    profile: Profile,
    *,
    min_n: int = 30,
    horizons: Iterable[int] = (3, 6, 12),
    exclude_security: str | None = None,
) -> BaseRateTable:
    """Outcome frequencies for the closest reference class with >= min_n 12m outcomes.

    ``samples`` columns: security_id, date, sector, size_bucket, signals
    (frozenset or 'a|b' string), excess_{h}m, max_dd_12m.
    """
    df = samples
    if exclude_security is not None:
        df = df[df["security_id"] != exclude_security]
    sigs: list[frozenset[str]] = (
        [_signal_set(v) for v in df["signals"]] if "signals" in df else [frozenset()] * len(df)
    )
    masks = {
        "sector": (df["sector"] == profile.sector).to_numpy(),
        "size": (df["size_bucket"] == profile.size_bucket).to_numpy(),
        "signals": np.fromiter(
            (_signals_match(s, profile.signals) for s in sigs), dtype=np.bool_, count=len(sigs)
        ),
    }
    hs = list(horizons)
    levels = _levels(profile)
    chosen = levels[-1]
    sel = df
    for level in levels:
        mask = np.ones(len(df), dtype=bool)
        for key in level[2]:
            mask &= masks[key]
        cand = df[mask]
        if cand[f"excess_{max(hs)}m"].notna().sum() >= min_n or level is levels[-1]:
            chosen, sel = level, cand
            break
    outcomes: list[BaseRateOutcome] = []
    for h in hs:
        col = sel[f"excess_{h}m"].dropna()
        outcomes.append(
            BaseRateOutcome(
                event="beats_benchmark",
                horizon_months=h,
                frequency=round(float((col > 0).mean()), 4) if len(col) else None,
                n=len(col),
            )
        )
    dd = sel["max_dd_12m"].dropna()
    outcomes.append(
        BaseRateOutcome(
            event="drawdown_exceeds_30pct",
            horizon_months=12,
            frequency=round(float((dd <= DRAWDOWN_THRESHOLD).mean()), 4) if len(dd) else None,
            n=len(dd),
        )
    )
    return BaseRateTable(
        match_level=chosen[0],
        description=chosen[1],
        n_samples=len(sel),
        n_securities=int(sel["security_id"].nunique()),
        outcomes=outcomes,
    )


# ----------------------------------------------------------------- PIT loader
def attach_signal_profiles(
    samples: pd.DataFrame,
    signals: pd.DataFrame,
    *,
    threshold: float = 1.0,
    window_days: int = 31,
) -> pd.DataFrame:
    """Add a 'signals' column: names with zscore >= threshold in (date-window, date]."""
    out = samples.copy()
    if signals.empty:
        out["signals"] = pd.Series([frozenset()] * len(out), index=out.index, dtype=object)
        return out
    flagged = signals[signals["zscore"].astype(float) >= threshold].copy()
    flagged["asof"] = pd.to_datetime(flagged["asof"])
    by_sec = {sid: g for sid, g in flagged.groupby("security_id")}
    vals: list[frozenset[str]] = []
    for sid, d in zip(out["security_id"], pd.to_datetime(out["date"]), strict=True):
        g = by_sec.get(sid)
        if g is None:
            vals.append(frozenset())
            continue
        lo = d - pd.Timedelta(days=window_days)
        hit = g[(g["asof"] > lo) & (g["asof"] <= d)]
        vals.append(frozenset(hit["signal_name"].astype(str)))
    out["signals"] = pd.Series(vals, index=out.index, dtype=object)
    return out


def load_samples_from_pit(
    pit: Any,
    asof: dt.date | dt.datetime,
    *,
    benchmark_security_id: str,
    sample_every_days: int = 21,
    signal_threshold: float = 1.0,
) -> pd.DataFrame:
    """Historical samples (security, date, sector, size, signals, outcomes) as of ``asof``."""
    if not pit.has("prices_daily"):
        return pd.DataFrame()
    prices = pit.query(
        "SELECT security_id, date, adj_close FROM prices_daily WHERE as_of(known_time, $asof) "
        "QUALIFY row_number() OVER (PARTITION BY security_id, date "
        "ORDER BY known_time DESC, ingest_id DESC) = 1",
        asof,
    )
    if prices.empty:
        return pd.DataFrame()
    bench_rows = prices[prices["security_id"] == benchmark_security_id]
    bench = pd.Series(
        bench_rows["adj_close"].to_numpy(dtype=float), index=pd.to_datetime(bench_rows["date"])
    )
    stocks = prices[prices["security_id"] != benchmark_security_id]
    outcomes = forward_outcomes(stocks, bench)
    outcomes = outcomes.sort_values(["security_id", "date"]).reset_index(drop=True)
    keep = outcomes.groupby("security_id").cumcount() % sample_every_days == 0
    outcomes = outcomes[keep].reset_index(drop=True)
    master = pit.latest("security_master", asof)
    sectors = (
        dict(zip(master["security_id"], master["sector"], strict=True)) if not master.empty else {}
    )
    outcomes["sector"] = outcomes["security_id"].map(sectors)
    shares = pit.latest("fundamentals", asof, "metric = 'shares_outstanding'")
    caps: list[str] = []
    close = stocks.assign(date=pd.to_datetime(stocks["date"])).set_index(["security_id", "date"])[
        "adj_close"
    ]
    if not shares.empty:
        shares = shares.assign(kt=pd.to_datetime(shares["known_time"], utc=True))
    for sid, d in zip(outcomes["security_id"], pd.to_datetime(outcomes["date"]), strict=True):
        mc: float | None = None
        if not shares.empty:
            s = shares[(shares["security_id"] == sid) & (shares["kt"].dt.tz_convert(None) <= d)]
            if not s.empty:
                mc = float(s.sort_values("kt")["value"].iloc[-1]) * float(close.loc[(sid, d)])
        caps.append(size_bucket(mc))
    outcomes["size_bucket"] = caps
    signals = (
        pit.query(
            'SELECT security_id, "asof", signal_name, zscore '
            "FROM signals WHERE as_of(known_time, $asof) AND zscore IS NOT NULL",
            asof,
        )
        if pit.has("signals")
        else pd.DataFrame()
    )
    return attach_signal_profiles(outcomes, signals, threshold=signal_threshold)
