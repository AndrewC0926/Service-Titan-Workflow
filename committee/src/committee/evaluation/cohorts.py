"""Idea quality: every committee review, acted on or not, scored at 3/6/12 months
(DESIGN 10). Rejected and vetoed ideas get the same scoring so you learn whether
the filters help; human overrides are their own cohort."""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass

import numpy as np

HORIZONS = (3, 6, 12)


@dataclass(frozen=True)
class ReviewOutcome:
    thesis_id: str
    recommendation: str  # Chair recommendation after code gates
    risk_verdict: str  # PASS | RESIZE | VETO
    human: str | None  # approved | rejected | expired | None
    human_initiated: bool
    cohort_model: str  # model_id + prompt hash
    excess: dict[int, float | None]  # horizon months -> return minus benchmark (None = not yet)


def cohort_of(r: ReviewOutcome) -> str:
    if r.human_initiated or (r.recommendation in ("BUY", "ADD") and r.human == "rejected"):
        return "OVERRIDE"
    if r.risk_verdict == "VETO":
        return "VETO"
    if r.recommendation in ("BUY", "ADD"):
        return "BUY"
    return "PASS"


@dataclass(frozen=True)
class CohortStats:
    cohort: str
    horizon: int
    n: int
    mean_excess: float | None
    hit_rate: float | None
    warning: str | None


def small_sample_warning(n: int) -> str | None:
    if n < 10:
        return f"only {n} ideas: this tells you nothing yet"
    if n < 30:
        return f"{n} ideas: far too few to separate skill from luck"
    if n < 100:
        return f"{n} ideas: suggestive at best"
    return None


def cohort_stats(reviews: Iterable[ReviewOutcome]) -> list[CohortStats]:
    by: dict[tuple[str, int], list[float]] = {}
    for r in reviews:
        for h in HORIZONS:
            v = r.excess.get(h)
            if v is not None:
                by.setdefault((cohort_of(r), h), []).append(v)
    out = []
    for c in ("BUY", "PASS", "VETO", "OVERRIDE"):
        for h in HORIZONS:
            xs = by.get((c, h), [])
            out.append(
                CohortStats(
                    c,
                    h,
                    len(xs),
                    float(np.mean(xs)) if xs else None,
                    float(np.mean([x > 0 for x in xs])) if xs else None,
                    small_sample_warning(len(xs)),
                )
            )
    return out


def overrides_underperform(stats: list[CohortStats], horizon: int = 12) -> bool | None:
    """True if the human-override cohort trails the committee's BUY cohort (reported monthly)."""
    d = {(s.cohort, s.horizon): s for s in stats}
    o, b = d.get(("OVERRIDE", horizon)), d.get(("BUY", horizon))
    if not o or not b or o.mean_excess is None or b.mean_excess is None:
        return None
    return o.mean_excess < b.mean_excess
