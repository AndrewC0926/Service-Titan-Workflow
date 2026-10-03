"""Portfolio-quality metrics (DESIGN 10): after-cost returns, information ratio,
max drawdown, turnover, deflated Sharpe ratio, probability of backtest
overfitting (CSCV) and minimum track record length.

References: Bailey & Lopez de Prado (2012, 2014); Bailey, Borwein, Lopez de Prado & Zhu (2015).
"""

from __future__ import annotations

import itertools
import math
from collections.abc import Sequence
from typing import Any

import numpy as np
from scipy import stats

Floats = Sequence[float] | np.ndarray[Any, Any]

EULER_GAMMA = 0.5772156649015329


def max_drawdown(returns: Floats) -> float:
    """Largest peak-to-trough loss of the compounded path (a negative number or 0)."""
    wealth = np.cumprod(1 + np.asarray(returns, dtype=float))
    peak = np.maximum.accumulate(np.concatenate([[1.0], wealth]))[1:]
    return float(np.min(wealth / peak - 1)) if wealth.size else 0.0


def information_ratio(active: Floats, periods_per_year: int = 12) -> float:
    a = np.asarray(active, dtype=float)
    sd = a.std(ddof=1)
    return (
        float("nan")
        if sd == 0 or a.size < 2
        else float(a.mean() / sd * math.sqrt(periods_per_year))
    )


def years_to_significance(ir: float, t: float = 2.0) -> float:
    """t ≈ IR·√T  ⇒  T = (t/IR)²  (DESIGN 10: IR 0.5 needs ~16 years)."""
    return float("inf") if ir <= 0 else (t / ir) ** 2


def sharpe(returns: Floats) -> float:
    r = np.asarray(returns, dtype=float)
    sd = r.std(ddof=1)
    return float("nan") if sd == 0 or r.size < 2 else float(r.mean() / sd)


def probabilistic_sharpe(sr: float, sr_benchmark: float, n: int, skew: float, kurt: float) -> float:
    """PSR: P(true SR > benchmark SR) given a per-period SR estimate over n periods
    (kurt is the raw kurtosis, 3 for normal)."""
    denom = math.sqrt(max(1e-12, 1 - skew * sr + (kurt - 1) / 4 * sr**2))
    return float(stats.norm.cdf((sr - sr_benchmark) * math.sqrt(n - 1) / denom))


def expected_max_sharpe(n_trials: int, sr_variance: float) -> float:
    """Expected maximum per-period SR across independent trials under the null."""
    if n_trials <= 1:
        return 0.0
    z1 = stats.norm.ppf(1 - 1.0 / n_trials)
    z2 = stats.norm.ppf(1 - 1.0 / (n_trials * math.e))
    return float(math.sqrt(sr_variance) * ((1 - EULER_GAMMA) * z1 + EULER_GAMMA * z2))


def deflated_sharpe(
    returns: Floats, n_trials: int, trial_sr_variance: float | None = None
) -> float:
    """DSR: PSR against the expected max SR of ``n_trials`` (total trial count from the
    trial registry). Above 0.95 is the allocator's bar."""
    r = np.asarray(returns, dtype=float)
    n = r.size
    if n < 3:
        return float("nan")
    sr = sharpe(r)
    var = trial_sr_variance if trial_sr_variance is not None else 1.0 / (n - 1)
    sr0 = expected_max_sharpe(max(1, n_trials), var)
    return probabilistic_sharpe(
        sr, sr0, n, float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    )


def min_track_record_length(
    returns: Floats, sr_benchmark: float = 0.0, confidence: float = 0.95
) -> float:
    """Periods needed for PSR(sr_benchmark) to reach ``confidence``."""
    r = np.asarray(returns, dtype=float)
    sr = sharpe(r)
    if not sr > sr_benchmark:
        return float("inf")
    skew, kurt = float(stats.skew(r)), float(stats.kurtosis(r, fisher=False))
    z = stats.norm.ppf(confidence)
    return float(1 + (1 - skew * sr + (kurt - 1) / 4 * sr**2) * (z / (sr - sr_benchmark)) ** 2)


def pbo_cscv(trial_returns: np.ndarray, n_splits: int = 8) -> float:
    """Probability of backtest overfitting via combinatorially symmetric cross-validation.

    ``trial_returns``: T x N matrix (periods x strategy variants). Returns the share of
    splits where the in-sample best variant ranks below the median out of sample.
    """
    m = np.asarray(trial_returns, dtype=float)
    t, n = m.shape
    if n < 2 or t < n_splits:
        return float("nan")
    blocks = np.array_split(np.arange(t), n_splits)
    logits = []
    for is_idx in itertools.combinations(range(n_splits), n_splits // 2):
        ins = np.concatenate([blocks[i] for i in is_idx])
        oos = np.concatenate([blocks[i] for i in range(n_splits) if i not in is_idx])
        sr_is = m[ins].mean(0) / (m[ins].std(0, ddof=1) + 1e-12)
        sr_oos = m[oos].mean(0) / (m[oos].std(0, ddof=1) + 1e-12)
        best = int(np.argmax(sr_is))
        rank = stats.rankdata(sr_oos)[best] / (n + 1)
        logits.append(math.log(rank / (1 - rank)))
    return float(np.mean(np.array(logits) <= 0))


def turnover(trades_notional: Floats, avg_value: float) -> float:
    """One-way turnover: sum of buys and sells / 2 / average portfolio value."""
    return float(sum(abs(x) for x in trades_notional) / 2 / avg_value) if avg_value > 0 else 0.0


def after_cost_returns(
    gross: Floats, turnover_per_period: Floats, cost_bps: float
) -> np.ndarray:
    g = np.asarray(gross, dtype=float)
    tv = np.asarray(turnover_per_period, dtype=float)
    return g - tv * 2 * cost_bps / 10_000


def factor_regression(
    returns: Floats, factors: np.ndarray, names: Sequence[str]
) -> dict[str, float]:
    """OLS of (excess) returns on factor returns. Returns alpha and betas."""
    y = np.asarray(returns, dtype=float)
    x = np.column_stack([np.ones(len(y)), np.asarray(factors, dtype=float)])
    coef, *_ = np.linalg.lstsq(x, y, rcond=None)
    out = {"alpha": float(coef[0])}
    out.update({n: float(c) for n, c in zip(names, coef[1:], strict=True)})
    return out


def factor_matched_weights(
    betas: dict[str, float], etf_exposures: dict[str, dict[str, float]]
) -> dict[str, float]:
    """Non-negative ETF weights (summing to 1) whose combined factor exposures best
    match the satellite's measured betas (NNLS)."""
    from scipy.optimize import nnls

    factors = sorted(betas)
    etfs = sorted(etf_exposures)
    a = np.array(
        [[etf_exposures[e].get(f, 0.0) for e in etfs] for f in factors] + [[1.0] * len(etfs)]
    )
    b = np.array([betas[f] for f in factors] + [1.0])
    w, _ = nnls(a, b)
    s = w.sum()
    w = w / s if s > 0 else np.full(len(etfs), 1 / len(etfs))
    return {e: float(x) for e, x in zip(etfs, w, strict=True)}
