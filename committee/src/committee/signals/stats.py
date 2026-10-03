"""Cross-sectional statistics: within-sector z-scores, winsorization, rank averages.

All functions take a value Series and a sector Series sharing the same index
(security_id). Missing values stay missing; they never borrow a peer's value.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

# Relative dispersion below which values are treated as identical (float noise from
# averaging ranks must not be blown up into +/-1 z-scores).
ZERO_DISPERSION = 1e-9


def winsorize(z: pd.Series, limit: float) -> pd.Series:
    """Clip z-scores to [-limit, +limit]. NaN stays NaN."""
    return z.clip(lower=-limit, upper=limit)


def sector_zscore(values: pd.Series, sectors: pd.Series, limit: float) -> pd.Series:
    """z = (x - sector mean) / sector std (population, ddof=0), then winsorized.

    A sector with fewer than two non-missing values, or (numerically) zero dispersion, gets z = 0
    for its non-missing members (no information relative to peers).
    """
    x = pd.to_numeric(values, errors="coerce").astype(float)
    sec = sectors.reindex(x.index).fillna("Unknown").astype(str)
    out = pd.Series(np.nan, index=x.index, dtype=float)
    for _, idx in x.groupby(sec).groups.items():
        g = x.loc[idx]
        present = g.dropna()
        if present.empty:
            continue
        sd, mean = float(present.std(ddof=0)), float(present.mean())
        if len(present) < 2 or not np.isfinite(sd) or sd <= ZERO_DISPERSION * max(1.0, abs(mean)):
            out.loc[present.index] = 0.0
        else:
            out.loc[present.index] = (present - mean) / sd
    return winsorize(out, limit)


def sector_pct_rank(values: pd.Series, sectors: pd.Series) -> pd.Series:
    """Percentile rank (0, 1] within sector, ties averaged. Higher value = higher rank."""
    x = pd.to_numeric(values, errors="coerce").astype(float)
    sec = sectors.reindex(x.index).fillna("Unknown").astype(str)
    return x.groupby(sec).rank(pct=True, method="average")


def rank_average(metrics: pd.DataFrame, sectors: pd.Series) -> pd.Series:
    """Mean of within-sector percentile ranks across columns (each oriented higher=better).

    A name's composite averages only the metrics it has; all-missing gives NaN.
    """
    if metrics.empty:
        return pd.Series(np.nan, index=metrics.index, dtype=float)
    ranks = pd.DataFrame(
        {c: sector_pct_rank(metrics[c], sectors) for c in metrics.columns}, index=metrics.index
    )
    return ranks.mean(axis=1, skipna=True)
