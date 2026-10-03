"""Forecast quality: Brier score, skill vs the base rate, reliability (DESIGN 10).

Every probability forecast is pre-registered in the journal before its window
opens; resolution is computed by code from prices.
"""

from __future__ import annotations

import datetime as dt
from collections.abc import Iterable, Sequence
from dataclasses import dataclass

import numpy as np

EVENTS = ("beats_benchmark", "drawdown_exceeds_30pct", "doubles", "loses_50pct")


@dataclass(frozen=True)
class Forecast:
    forecast_id: str
    thesis_id: str
    agent: str
    event: str
    horizon_months: int
    probability: float
    created_at: dt.datetime
    cohort: str  # model_id + prompt hash
    outcome: int | None = None  # 1 happened, 0 did not, None unresolved


def brier(probs: Sequence[float], outcomes: Sequence[int]) -> float:
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    if p.size == 0:
        raise ValueError("no resolved forecasts")
    return float(np.mean((p - o) ** 2))


def brier_skill(
    probs: Sequence[float], ref_probs: Sequence[float], outcomes: Sequence[int]
) -> float:
    """1 - BS/BS_ref. Positive means better than the reference (base rate)."""
    ref = brier(ref_probs, outcomes)
    return float("nan") if ref == 0 else 1.0 - brier(probs, outcomes) / ref


@dataclass(frozen=True)
class ReliabilityBin:
    lo: float
    hi: float
    n: int
    mean_forecast: float
    observed_rate: float


def reliability(
    probs: Sequence[float], outcomes: Sequence[int], bins: int = 10
) -> list[ReliabilityBin]:
    p = np.asarray(probs, dtype=float)
    o = np.asarray(outcomes, dtype=float)
    edges = np.linspace(0, 1, bins + 1)
    out = []
    for i in range(bins):
        m = (p >= edges[i]) & ((p < edges[i + 1]) if i < bins - 1 else (p <= edges[i + 1]))
        if m.any():
            out.append(
                ReliabilityBin(
                    float(edges[i]),
                    float(edges[i + 1]),
                    int(m.sum()),
                    float(p[m].mean()),
                    float(o[m].mean()),
                )
            )
    return out


def murphy(probs: Sequence[float], outcomes: Sequence[int], bins: int = 10) -> dict[str, float]:
    """Murphy decomposition: BS = reliability - resolution + uncertainty (binned)."""
    o = np.asarray(outcomes, dtype=float)
    base = float(o.mean())
    n = len(o)
    rel = (
        sum(
            b.n * (b.mean_forecast - b.observed_rate) ** 2
            for b in reliability(probs, outcomes, bins)
        )
        / n
    )
    res = sum(b.n * (b.observed_rate - base) ** 2 for b in reliability(probs, outcomes, bins)) / n
    return {"reliability": rel, "resolution": res, "uncertainty": base * (1 - base)}


@dataclass(frozen=True)
class AgentScore:
    agent: str
    cohort: str
    event: str
    n: int
    brier: float
    brier_skill_vs_base_rate: float | None
    climatology_skill: float


def score_forecasts(
    forecasts: Iterable[Forecast], base_rate_agent: str = "base_rate"
) -> list[AgentScore]:
    """Per agent x cohort x event Brier, skill vs the Base-Rate agent on the same
    theses, and skill vs climatology (always predicting the observed frequency)."""
    resolved = [f for f in forecasts if f.outcome is not None]
    base = {
        (f.thesis_id, f.event, f.horizon_months): f.probability
        for f in resolved
        if f.agent == base_rate_agent
    }
    groups: dict[tuple[str, str, str], list[Forecast]] = {}
    for f in resolved:
        groups.setdefault((f.agent, f.cohort, f.event), []).append(f)
    out = []
    for (agent, cohort, event), fs in sorted(groups.items()):
        probs = [f.probability for f in fs]
        outs = [int(f.outcome or 0) for f in fs]
        clim = float(np.mean(outs))
        paired = [
            (f.probability, base[(f.thesis_id, f.event, f.horizon_months)], int(f.outcome or 0))
            for f in fs
            if (f.thesis_id, f.event, f.horizon_months) in base
        ]
        bss = None
        if paired and agent != base_rate_agent:
            pp, rp, oo = zip(*paired, strict=True)
            bss = brier_skill(pp, rp, oo)
        out.append(
            AgentScore(
                agent,
                cohort,
                event,
                len(fs),
                brier(probs, outs),
                bss,
                brier_skill(probs, [clim] * len(fs), outs),
            )
        )
    return out


def resolve_beats_benchmark(ret: float, bench_ret: float, cost_bps: float) -> int:
    return int(ret - cost_bps / 10_000 > bench_ret)


def resolve_from_path(
    prices: Sequence[float], event: str, bench_ret: float = 0.0, cost_bps: float = 0.0
) -> int:
    """Resolve an event from the price path over the horizon (first price = entry)."""
    p = np.asarray(prices, dtype=float)
    if p.size < 2:
        raise ValueError("need a price path")
    total = p[-1] / p[0] - 1
    if event == "beats_benchmark":
        return resolve_beats_benchmark(total, bench_ret, cost_bps)
    peak = np.maximum.accumulate(p)
    if event == "drawdown_exceeds_30pct":
        return int(float(np.min(p / peak - 1)) <= -0.30)
    if event == "doubles":
        return int(float(np.max(p / p[0])) >= 2.0)
    if event == "loses_50pct":
        return int(float(np.min(p / p[0])) <= 0.5)
    raise ValueError(f"unknown event {event}")


def agent_weights(
    skills: dict[str, float], shrink: float = 0.5, max_ratio: float = 2.0
) -> dict[str, float]:
    """Quarterly reweight by Brier skill with shrinkage toward equal weights and at
    most ``max_ratio`` between best and worst agent (DESIGN 10)."""
    if not skills:
        return {}
    names = sorted(skills)
    eq = 1.0 / len(names)
    raw = np.array([max(skills[n], 0.0) for n in names])
    raw = raw / raw.sum() if raw.sum() > 0 else np.full(len(names), eq)
    w = shrink * eq + (1 - shrink) * raw
    lo = w.max() / max_ratio
    w = np.maximum(w, lo)
    w = w / w.sum()
    # renormalization can only shrink the ratio, but loop to be safe
    for _ in range(10):
        if w.max() / w.min() <= max_ratio + 1e-9:
            break
        w = np.maximum(w, w.max() / max_ratio)
        w = w / w.sum()
    return {n: float(x) for n, x in zip(names, w, strict=True)}
