"""Satellite volatility under a constant pairwise correlation assumption.

Model: every pair of distinct names has the same correlation ``rho``
(default 0.3, a typical average pairwise correlation of U.S. single stocks).
Portfolio variance is::

    var = sum_i w_i^2 s_i^2 + rho * sum_{i != j} w_i w_j s_i s_j
        = (1 - rho) * sum_i (w_i s_i)^2 + rho * (sum_i w_i s_i)^2

and the component (Euler) contribution of name ``k`` is::

    c_k = w_k s_k * ((1 - rho) * w_k s_k + rho * sum_i w_i s_i) / vol

so that ``sum_k c_k == vol``. Weights are fractions of the satellite's market
value. Missing name vols fall back to :data:`DEFAULT_NAME_VOL` by bucket.
"""

from __future__ import annotations

import math
from collections.abc import Mapping

from committee.domain import Holding

DEFAULT_RHO = 0.3
DEFAULT_NAME_VOL: dict[str, float] = {
    "core_pick": 0.35,
    "asymmetric_bet": 0.60,
    "speculative": 0.80,
    "other": 0.30,
}


def _check_rho(rho: float) -> None:
    if not 0.0 <= rho <= 1.0:
        raise ValueError(f"rho must lie in [0, 1], got {rho}")


def constant_corr_vol(weights: Mapping[str, float], vols: Mapping[str, float], rho: float) -> float:
    """Annualized volatility of a weighted basket with constant correlation."""
    _check_rho(rho)
    ws = [weights[k] * vols[k] for k in weights]
    s = sum(ws)
    var = (1.0 - rho) * sum(x * x for x in ws) + rho * s * s
    return math.sqrt(max(var, 0.0))


def component_contributions(
    weights: Mapping[str, float], vols: Mapping[str, float], rho: float
) -> dict[str, float]:
    """Euler contribution of each name to basket volatility (sums to the vol)."""
    vol = constant_corr_vol(weights, vols, rho)
    if vol == 0.0:
        return {k: 0.0 for k in weights}
    s = sum(weights[k] * vols[k] for k in weights)
    out: dict[str, float] = {}
    for k in weights:
        ws = weights[k] * vols[k]
        out[k] = ws * ((1.0 - rho) * ws + rho * s) / vol
    return out


def holding_vol(h: Holding) -> float:
    if h.annual_vol is not None:
        return h.annual_vol
    key = "speculative" if h.sleeve == "speculative" else (h.bucket or "other")
    return DEFAULT_NAME_VOL.get(key, DEFAULT_NAME_VOL["other"])


def basket(holdings: list[Holding]) -> tuple[dict[str, float], dict[str, float]]:
    """Aggregate holdings by symbol into (weights by market value, vols).

    Weights are fractions of the basket's total market value. Positions held
    in several accounts are summed; the first non-null ``annual_vol`` wins.
    """
    mv: dict[str, float] = {}
    explicit: dict[str, float] = {}
    fallback: dict[str, float] = {}
    for h in holdings:
        mv[h.symbol] = mv.get(h.symbol, 0.0) + h.market_value
        if h.annual_vol is not None:
            explicit.setdefault(h.symbol, h.annual_vol)
        fallback.setdefault(h.symbol, holding_vol(h))
    vols = {k: explicit.get(k, fallback[k]) for k in mv}
    total = sum(mv.values())
    if total <= 0:
        return {}, {}
    return {k: v / total for k, v in mv.items()}, vols


def satellite_vol(holdings: list[Holding], rho: float = DEFAULT_RHO) -> float:
    """Expected 1-year volatility of the given (satellite) holdings."""
    w, v = basket(holdings)
    if not w:
        return 0.0
    return constant_corr_vol(w, v, rho)
