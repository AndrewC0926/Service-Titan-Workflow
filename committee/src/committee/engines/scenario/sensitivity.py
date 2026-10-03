"""Per-holding factor sensitivities: an OLS estimator and documented defaults.

Factors (columns of the factor frame / keys of ``Scenario.shocks``):

* ``market``       - broad equity return (fraction)
* ``rates_10y_bp`` - change in the 10-year Treasury yield, in basis points
* ``oil``          - Brent return (fraction)
* ``dollar``       - broad trade-weighted dollar return (fraction)

Default table (used when no return history is available). Rate, oil and dollar
betas are *excess* sensitivities on top of the market beta: scenarios already
carry a market shock, so a utility's rate beta captures only how much more it
moves than the market when yields move.

Policy sleeves (by sleeve id; unknown ids fall back to the sleeve kind):

=========  ======  ===========  ==========  ====  ======
sleeve     market  market_down  rates/100bp  oil   dollar
=========  ======  ===========  ==========  ====  ======
us_total   1.00    -            0.00        0.00  0.00
us_scv     1.15    -            -0.02       0.00  0.00
dev_exus   0.90    -            0.00        0.00  -0.50
intl_scv   0.95    -            -0.01       0.00  -0.50
em         1.05    -            0.00        0.02  -0.80
trend      0.00    -0.20        0.00        0.00  0.00
tbills     0.00    -            0.00        0.00  0.00
bonds      0.00    -            -0.07       0.00  0.00
=========  ======  ===========  ==========  ====  ======

The trend sleeve is flat in rallies and has a market beta of -0.2 when the
market shock is negative (crisis convexity). Core ETFs also carry the
look-through theme weights from ``policy_portfolio.yaml``.

Satellite and speculative names: market beta = bucket base (core pick 1.00,
asymmetric bet 1.30, speculative 1.50) x the sector multiplier below; excess
rate / oil / dollar betas come from the sector row. Themes are the holding's
own tags (fraction of the position exposed).

======================  ===========  ===========  =====  ======
sector                  market mult  rates/100bp  oil    dollar
======================  ===========  ===========  =====  ======
Energy                  0.90         0.00         0.40   0.00
Materials               1.05         0.00         0.10   -0.20
Industrials             1.05         -0.01        -0.05  -0.10
Consumer Discretionary  1.15         -0.02        -0.10  0.00
Consumer Staples        0.65         -0.02        -0.03  -0.10
Health Care             0.80         -0.01        0.00   -0.05
Financials              1.10         0.02         0.00   0.00
Information Technology  1.20         -0.04        0.00   -0.20
Communication Services  1.00         -0.03        0.00   -0.05
Utilities               0.55         -0.06        0.00   0.00
Real Estate             0.85         -0.08        0.00   0.00
(unknown)               1.00         -0.02        0.00   0.00
======================  ===========  ===========  =====  ======
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import NamedTuple

import numpy as np
import pandas as pd

from committee.config.schema import PolicyPortfolio
from committee.domain import Holding
from committee.engines.scenario.models import FactorSensitivity

FACTORS: tuple[str, ...] = ("market", "rates_10y_bp", "oil", "dollar")
SATELLITE_SLEEVES: frozenset[str] = frozenset({"satellite", "speculative"})
MIN_OBSERVATIONS = 30


class _Row(NamedTuple):
    market: float
    rates: float
    oil: float
    dollar: float
    market_down: float | None = None


SLEEVE_DEFAULTS: dict[str, _Row] = {
    "us_total": _Row(1.00, 0.00, 0.00, 0.00),
    "us_scv": _Row(1.15, -0.02, 0.00, 0.00),
    "dev_exus": _Row(0.90, 0.00, 0.00, -0.50),
    "intl_scv": _Row(0.95, -0.01, 0.00, -0.50),
    "em": _Row(1.05, 0.00, 0.02, -0.80),
    "trend": _Row(0.00, 0.00, 0.00, 0.00, market_down=-0.20),
    "tbills": _Row(0.00, 0.00, 0.00, 0.00),
    "bonds": _Row(0.00, -0.07, 0.00, 0.00),
}

KIND_DEFAULTS: dict[str, _Row] = {
    "core_equity": _Row(1.00, 0.00, 0.00, 0.00),
    "core_tilt": _Row(1.10, -0.01, 0.00, 0.00),
    "diversifier": SLEEVE_DEFAULTS["trend"],
    "liquidity": SLEEVE_DEFAULTS["tbills"],
    "bonds": SLEEVE_DEFAULTS["bonds"],
}

BUCKET_MARKET_BASE: dict[str, float] = {
    "core_pick": 1.00,
    "asymmetric_bet": 1.30,
    "speculative": 1.50,
}

SECTOR_DEFAULTS: dict[str, _Row] = {
    "energy": _Row(0.90, 0.00, 0.40, 0.00),
    "materials": _Row(1.05, 0.00, 0.10, -0.20),
    "industrials": _Row(1.05, -0.01, -0.05, -0.10),
    "consumer discretionary": _Row(1.15, -0.02, -0.10, 0.00),
    "consumer staples": _Row(0.65, -0.02, -0.03, -0.10),
    "health care": _Row(0.80, -0.01, 0.00, -0.05),
    "financials": _Row(1.10, 0.02, 0.00, 0.00),
    "information technology": _Row(1.20, -0.04, 0.00, -0.20),
    "communication services": _Row(1.00, -0.03, 0.00, -0.05),
    "utilities": _Row(0.55, -0.06, 0.00, 0.00),
    "real estate": _Row(0.85, -0.08, 0.00, 0.00),
}
UNKNOWN_SECTOR = _Row(1.00, -0.02, 0.00, 0.00)

SECTOR_ALIASES: dict[str, str] = {
    "technology": "information technology",
    "tech": "information technology",
    "it": "information technology",
    "healthcare": "health care",
    "consumer cyclical": "consumer discretionary",
    "consumer defensive": "consumer staples",
    "communication": "communication services",
    "telecom": "communication services",
    "basic materials": "materials",
    "financial services": "financials",
    "realestate": "real estate",
    "reits": "real estate",
}


def normalize_sector(sector: str | None) -> str | None:
    if sector is None:
        return None
    key = " ".join(sector.strip().lower().replace("_", " ").split())
    return SECTOR_ALIASES.get(key, key)


def sector_row(sector: str | None) -> _Row:
    key = normalize_sector(sector)
    if key is None:
        return UNKNOWN_SECTOR
    return SECTOR_DEFAULTS.get(key, UNKNOWN_SECTOR)


class BetaEstimate(NamedTuple):
    sensitivity: FactorSensitivity
    intercept: float
    r_squared: float
    n_obs: int


def estimate_sensitivity(
    asset_returns: pd.Series,
    factors: pd.DataFrame,
    *,
    min_obs: int = MIN_OBSERVATIONS,
    themes: Mapping[str, float] | None = None,
) -> BetaEstimate:
    """OLS of asset returns on factor moves (with intercept).

    ``factors`` may hold any subset of :data:`FACTORS`; missing factors get a
    zero beta. The ``rates_10y_bp`` column is the yield change in basis points;
    its coefficient is rescaled to a per-100bp beta. Rows with any NaN are
    dropped after aligning on the index. Raises ``ValueError`` when fewer than
    ``min_obs`` aligned observations remain or the design matrix is singular.
    """
    unknown = set(factors.columns) - set(FACTORS)
    if unknown:
        raise ValueError(f"unknown factor columns: {sorted(unknown)}")
    cols = [c for c in FACTORS if c in factors.columns]
    if not cols:
        raise ValueError("factors frame has no recognised factor columns")
    frame = pd.concat([asset_returns.rename("__y__"), factors[cols]], axis=1, join="inner")
    frame = frame.dropna()
    n = len(frame)
    if n < max(min_obs, len(cols) + 2):
        raise ValueError(f"need at least {max(min_obs, len(cols) + 2)} observations, got {n}")
    y = frame["__y__"].to_numpy(dtype=float)
    x = np.column_stack([np.ones(n), frame[cols].to_numpy(dtype=float)])
    if np.linalg.matrix_rank(x) < x.shape[1]:
        raise ValueError("factor design matrix is singular (constant or collinear factor)")
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    resid = y - x @ coef
    ss_tot = float(((y - y.mean()) ** 2).sum())
    r2 = 1.0 - float((resid**2).sum()) / ss_tot if ss_tot > 0 else 0.0
    betas = dict(zip(cols, (float(c) for c in coef[1:]), strict=True))
    sens = FactorSensitivity(
        beta_market=betas.get("market", 0.0),
        beta_rates_100bp=betas.get("rates_10y_bp", 0.0) * 100.0,
        beta_oil=betas.get("oil", 0.0),
        beta_dollar=betas.get("dollar", 0.0),
        themes=dict(themes or {}),
        source="estimated",
    )
    return BetaEstimate(sens, float(coef[0]), r2, n)


def default_sensitivity(
    holding: Holding, policy: PolicyPortfolio | None = None
) -> FactorSensitivity:
    """Default sensitivities from the documented sleeve / sector tables."""
    if holding.sleeve in SATELLITE_SLEEVES:
        bucket_key = "speculative" if holding.sleeve == "speculative" else holding.bucket
        base = BUCKET_MARKET_BASE.get(bucket_key or "core_pick", 1.0)
        row = sector_row(holding.sector)
        return FactorSensitivity(
            beta_market=base * row.market,
            beta_rates_100bp=row.rates,
            beta_oil=row.oil,
            beta_dollar=row.dollar,
            themes=dict(holding.themes),
            source="default_sector",
        )
    row_or_none = SLEEVE_DEFAULTS.get(holding.sleeve)
    if row_or_none is None and policy is not None:
        kind = next((s.kind for s in policy.sleeves if s.id == holding.sleeve), None)
        row_or_none = KIND_DEFAULTS.get(kind) if kind is not None else None
    themes: dict[str, float] = dict(holding.themes)
    if policy is not None and holding.symbol in policy.lookthrough:
        themes = {**policy.lookthrough[holding.symbol], **themes}
    if row_or_none is None:
        return FactorSensitivity(beta_market=0.0, themes=themes, source="zero")
    row = row_or_none
    return FactorSensitivity(
        beta_market=row.market,
        beta_market_down=row.market_down,
        beta_rates_100bp=row.rates,
        beta_oil=row.oil,
        beta_dollar=row.dollar,
        themes=themes,
        source="default_sleeve",
    )


def sensitivities_for(
    holdings: list[Holding],
    policy: PolicyPortfolio | None = None,
    estimated: Mapping[str, FactorSensitivity] | None = None,
) -> dict[str, FactorSensitivity]:
    """Sensitivity per symbol: an estimate when supplied, else the default.

    Estimated sensitivities without theme tags inherit the default's themes
    (holding tags / ETF look-through), so tag exposure is never lost.
    """
    out: dict[str, FactorSensitivity] = {}
    est = estimated or {}
    for h in holdings:
        if h.symbol in out:
            continue
        default = default_sensitivity(h, policy)
        given = est.get(h.symbol)
        if given is None:
            out[h.symbol] = default
        elif not given.themes and default.themes:
            out[h.symbol] = given.model_copy(update={"themes": default.themes})
        else:
            out[h.symbol] = given
    return out
