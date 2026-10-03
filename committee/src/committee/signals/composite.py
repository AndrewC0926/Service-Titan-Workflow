"""Raw signal table, within-sector z-scores and the weighted composite (DESIGN 6).

Raw signals (one row per universe member):

* insider_opportunistic: ``insider.purchase_score`` (0 without opportunistic buys).
* lazy_prices: mean TF-IDF cosine similarity vs the prior-year filing (NaN if none).
* value: within-sector rank average of earnings yield, FCF yield, book-to-market and
  EBIT/EV (the inverse of EV/EBIT, so negative EBIT ranks last).
* quality: within-sector rank average of gross profitability, ROIC, -accruals, -leverage.
* momentum: 12-1 return.
* low_risk: within-sector rank average of -volatility_1y and -beta_1y (weight 0).
* earnings_revision: 3-month consensus EPS revision (only when enabled).

Every raw signal is z-scored within sector and winsorized at +/- ``winsorize_z``.
Contribution = weight * z, except insider (positive side only: absence of buying is
not evidence) and lazy_prices (negative side only: unchanged filings contribute 0).
A missing z contributes 0. Weights for signals not computed here (call_tone_change,
news) contribute 0. Composite = sum of contributions + ``cluster_buy_bonus`` when the
name has an insider cluster buy in the score window.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence

import numpy as np
import pandas as pd

from committee.config.schema import SignalsConfig
from committee.signals.fundamentals import factor_metrics
from committee.signals.insider import InsiderSignal
from committee.signals.lazy_prices import LazyPricesResult
from committee.signals.stats import rank_average, sector_zscore
from committee.signals.universe import Member

POSITIVE_ONLY = frozenset({"insider_opportunistic"})
NEGATIVE_ONLY = frozenset({"lazy_prices"})
CORE_SIGNALS = ("insider_opportunistic", "lazy_prices", "value", "quality", "momentum", "low_risk")
CLUSTER_BONUS = "cluster_buy_bonus"


def _nan(x: float | None) -> float:
    return np.nan if x is None else float(x)


def raw_signals(
    members: Sequence[Member],
    insider: Mapping[str, InsiderSignal],
    lazy: Mapping[str, LazyPricesResult],
    revisions: Mapping[str, float] | None,
) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(raw signal table, sub-metric table), both indexed by security_id."""
    ids = [m.security_id for m in members]
    sectors = pd.Series([m.sector for m in members], index=ids)
    sub = pd.DataFrame(
        [
            {
                **dataclasses.asdict(factor_metrics(m.fundamentals, m.market_cap)),
                "volatility_1y": _nan(m.price.volatility_1y),
                "beta_1y": _nan(m.price.beta_1y),
            }
            for m in members
        ],
        index=ids,
        dtype=float,
    )
    if sub.empty:
        sub = pd.DataFrame(index=ids)
    value = rank_average(
        sub.reindex(columns=["earnings_yield", "fcf_yield", "book_to_market", "ebit_to_ev"]),
        sectors,
    )
    quality = rank_average(
        pd.DataFrame(
            {
                "gp": sub.get("gross_profitability"),
                "roic": sub.get("roic"),
                "acc": -sub["accruals"] if "accruals" in sub else np.nan,
                "lev": -sub["leverage"] if "leverage" in sub else np.nan,
            },
            index=ids,
        ),
        sectors,
    )
    low_risk = rank_average(
        pd.DataFrame(
            {
                "vol": -sub["volatility_1y"] if "volatility_1y" in sub else np.nan,
                "beta": -sub["beta_1y"] if "beta_1y" in sub else np.nan,
            },
            index=ids,
        ),
        sectors,
    )
    raw = pd.DataFrame(
        {
            "insider_opportunistic": [
                _nan(insider[i].score) if i in insider else np.nan for i in ids
            ],
            "lazy_prices": [_nan(lazy[i].similarity) if i in lazy else np.nan for i in ids],
            "value": value,
            "quality": quality,
            "momentum": [_nan(m.price.momentum_12_1) for m in members],
            "low_risk": low_risk,
        },
        index=ids,
        dtype=float,
    )
    if revisions is not None:
        raw["earnings_revision"] = [_nan(revisions.get(i)) for i in ids]
    return raw, sub


def zscores(raw: pd.DataFrame, sectors: pd.Series, limit: float) -> pd.DataFrame:
    return pd.DataFrame(
        {c: sector_zscore(raw[c], sectors, limit) for c in raw.columns}, index=raw.index
    )


def contributions(z: pd.DataFrame, cfg: SignalsConfig, cluster: Mapping[str, bool]) -> pd.DataFrame:
    """Per-signal weighted contributions (missing z = 0) plus the cluster bonus column."""
    out = pd.DataFrame(index=z.index)
    for name in z.columns:
        zz = z[name].fillna(0.0)
        if name in POSITIVE_ONLY:
            zz = zz.clip(lower=0.0)
        elif name in NEGATIVE_ONLY:
            zz = zz.clip(upper=0.0)
        out[name] = cfg.weights.get(name, 0.0) * zz
    out[CLUSTER_BONUS] = [cfg.cluster_buy_bonus if cluster.get(i) else 0.0 for i in z.index]
    return out
