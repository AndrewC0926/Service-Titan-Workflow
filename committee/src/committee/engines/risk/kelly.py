"""Kelly sizing ceiling from the Chair's probability and scenario payoffs.

Two estimates are computed and the *smaller* is used (estimation error makes
Kelly over-bet, so the engine takes the more conservative reading):

1. Discrete Kelly: the fraction ``f`` in ``[0, 1]`` maximizing expected log
   growth ``sum_i p_i * log(1 + f * r_i)`` over the bear/base/bull outcomes.
   ``g(f)`` is concave, so the optimum is the root of
   ``g'(f) = sum_i p_i r_i / (1 + f r_i)``, found by bisection. ``f`` is
   capped at 1.0 because leverage is prohibited.
2. Binary closed form with the Chair's probability ``p = p_beat_12m``:
   ``f* = p / a - q / b`` where ``b`` is the probability-weighted average
   winning return, ``a`` the probability-weighted average losing return
   (magnitude) and ``q = 1 - p``. Clamped to ``[0, 1]``. With no losing
   outcome the bet is unbounded and the estimate is 1.0; with no winning
   outcome it is 0.

The ceiling as % of total account value is ``100 * kelly_fraction_cap * f``
(``kelly_fraction_cap`` = 0.5, so full Kelly is never used).
"""

from __future__ import annotations

import math

from committee.engines.risk.models import KellyResult, Outcome

_MAX_F = 1.0
_TOL = 1e-12
_NEGLIGIBLE = 1e-12


def _growth_slope(outcomes: list[Outcome], f: float) -> float:
    return sum(o.probability * o.ret / (1.0 + f * o.ret) for o in outcomes)


def expected_log_growth(outcomes: list[Outcome], f: float) -> float:
    total = 0.0
    for o in outcomes:
        if o.probability == 0:
            continue
        x = 1.0 + f * o.ret
        if x <= 0:
            return -math.inf
        total += o.probability * math.log(x)
    return total


def kelly_discrete(outcomes: list[Outcome]) -> float:
    """Growth-optimal fraction in [0, 1] for a discrete payoff distribution."""
    live = [o for o in outcomes if o.probability > 0]
    if not live or _growth_slope(live, 0.0) <= 0:
        return 0.0
    hi = _MAX_F
    if all(1.0 + hi * o.ret > 0 for o in live) and _growth_slope(live, hi) >= 0:
        return hi
    lo = 0.0
    # every 1 + f*r > 0 for f < 1 because r >= -1
    hi = _MAX_F - 1e-12
    for _ in range(200):
        mid = 0.5 * (lo + hi)
        if _growth_slope(live, mid) > 0:
            lo = mid
        else:
            hi = mid
        if hi - lo < _TOL:
            break
    return lo


def kelly_binary(p: float, outcomes: list[Outcome]) -> float:
    """Closed-form ``p/a - q/b`` with b = avg win, a = avg loss (clamped)."""
    wins = [o for o in outcomes if o.ret > 0 and o.probability > 0]
    losses = [o for o in outcomes if o.ret < 0 and o.probability > 0]
    if not wins or p <= 0:
        return 0.0
    if not losses:
        return _MAX_F
    b = sum(o.probability * o.ret for o in wins) / sum(o.probability for o in wins)
    a = -sum(o.probability * o.ret for o in losses) / sum(o.probability for o in losses)
    if b <= _NEGLIGIBLE:
        return 0.0  # no meaningful upside
    if a <= _NEGLIGIBLE:
        return _MAX_F  # no meaningful downside: unbounded, capped (no leverage)
    f = p / a - (1.0 - p) / b
    return min(_MAX_F, max(0.0, f))


def kelly_ceiling(p_beat: float, outcomes: list[Outcome], fraction_cap: float) -> KellyResult:
    f_d = kelly_discrete(outcomes)
    f_b = kelly_binary(p_beat, outcomes)
    f = min(f_d, f_b)
    return KellyResult(
        f_discrete=f_d,
        f_binary=f_b,
        f_used=f,
        fraction_cap=fraction_cap,
        ceiling_pct_total=100.0 * fraction_cap * f,
    )
