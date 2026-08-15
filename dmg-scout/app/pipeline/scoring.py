"""Priority scoring: rank by winnability, not size.

priority = certainty x window_multiplier x size_factor x recency_decay x identity_factor

The window multiplier is the sales-critical piece: a 500 MW project already out
to bid (POST_BOD, x0.15) must rank below a 40 MW project at NOP stage
(PRE_BOD, x1.0). Certainty weights corroboration heavily — two independent
signal types on one project is the threshold separating a real build from a
speculative queue entry.

identity_factor is the fifth term, applied by the caller (run_size_score) rather
than inside priority_score(): it exists because the other four terms only ever
reward a row — none of them can tell "no name, no developer, no county" from a
fully worked lead, so a thin signal with a decent certainty prior could out-rank
a project a rep can actually act on. It is the one term that only ever discounts,
and it is kept separate from priority_score() so callers that do not have a
Project on hand (tests, scoring-only callers) are unaffected.
"""
from __future__ import annotations

import math
from datetime import datetime

from app.config import Config
from app.models import SignalType, Stage, Window, utcnow

# Stage -> window classification. BOD is typically locked mid-design.
STAGE_WINDOW: dict[Stage, Window] = {
    Stage.concept: Window.PRE_BOD,
    Stage.entitlement: Window.PRE_BOD,
    Stage.design: Window.IN_BOD,
    Stage.permitting: Window.IN_BOD,
    Stage.procurement: Window.POST_BOD,
    Stage.construction: Window.POST_BOD,
    Stage.operating: Window.OPERATING,
    Stage.unknown: Window.PRE_BOD,  # optimistic: unknown stage still gets a look
}

# Filing types that pin the window regardless of coarse stage.
FILING_WINDOW_OVERRIDES: dict[str, Window] = {
    "NOP": Window.PRE_BOD,
    "NOD": Window.POST_BOD,
    "bid_invite": Window.POST_BOD,
}


def classify_window(stage: Stage, filing_types: list[str] | None = None) -> Window:
    for ft in filing_types or []:
        if ft in FILING_WINDOW_OVERRIDES:
            # A late filing overrides an optimistic stage, but an early filing
            # never un-rings the bell of a late stage.
            override = FILING_WINDOW_OVERRIDES[ft]
            base = STAGE_WINDOW[stage]
            order = [Window.PRE_BOD, Window.IN_BOD, Window.POST_BOD, Window.OPERATING]
            return max(override, base, key=order.index)
    return STAGE_WINDOW[stage]


def certainty_detail(cfg: Config, signal_types: list[SignalType]) -> tuple[float, str]:
    """Base certainty = strongest single signal's prior; corroboration adds on
    top. Same computation as certainty() below, but also returns a plain-
    English account of which signal type set the base and whether
    corroboration/capping fired — the raw float alone does not say why 0.90
    is 0.90, and that is the whole point of the score-breakdown feature this
    exists for (see app/pipeline/size_score.py:score_breakdown)."""
    if not signal_types:
        return 0.0, "no linked signals -> 0.00"
    priors = cfg.get("scoring.signal_certainty", {})
    scored = [(st, priors.get(st.value, 0.3)) for st in signal_types]
    best_type, base = max(scored, key=lambda t: t[1])
    distinct = len(set(signal_types))
    bonus_per = cfg.get("scoring.corroboration_bonus", 0.25)
    cap = cfg.get("scoring.corroboration_cap", 2)
    bonus_n = min(distinct - 1, cap)
    bonus = bonus_per * bonus_n
    raw = base + bonus
    final = min(1.0, raw)

    detail = f"{base:.2f} base ({best_type.value}, strongest of {distinct} signal type{'s' if distinct != 1 else ''})"
    if bonus_n > 0:
        detail += f" + {bonus_per:.2f}×{bonus_n} corroboration = {raw:.2f}"
    if final < raw:
        detail += " capped at 1.00"
    return final, detail


def certainty(cfg: Config, signal_types: list[SignalType]) -> float:
    """Base certainty = strongest single signal's prior; corroboration adds on top."""
    return certainty_detail(cfg, signal_types)[0]


def size_factor(tons_midpoint: float | None) -> float:
    """Log-scaled so a 10x bigger project doesn't drown out winnability.
    1,000 tons -> 1.0; 10,000 -> 2.0; 100,000 -> 3.0. Unknown size -> 0.5."""
    if not tons_midpoint or tons_midpoint <= 0:
        return 0.5
    return max(0.25, math.log10(tons_midpoint) - 2.0)


def identity_factor(cfg: Config, name: str | None, developer: str | None,
                     county: str | None) -> float:
    """Discount a row for each of {name, developer, county} it cannot supply.

    A project with no stated name, no developer, and no county is not a call-list
    entry — it is a placeholder that happens to have a decent certainty prior.
    "Unnamed project (Storey)" and "Unnamed Phoenix Data Center Acquisitions LLC"
    ranked #7 and #11 on a 17-row board on certainty/window/size alone, ahead of
    fully-identified rows with lower priors, because nothing before this function
    ever looked at whether the row was actionable.

    `name` is checked against the "Unnamed" prefix, not just None: that is the
    literal marker _new_project() in resolve.py writes when no signal ever stated
    a project_name (see app/pipeline/resolve.py), so it is exactly what a rep
    sees as "no name" on the board.
    """
    missing = sum([
        not name or name.startswith("Unnamed"),
        not developer,
        not county,
    ])
    penalties = cfg.get("scoring.identity_penalty", {})
    return penalties.get(missing, 1.0)


def recency_decay(cfg: Config, last_signal_at: datetime | None, now: datetime | None = None) -> float:
    if last_signal_at is None:
        return 1.0
    now = now or utcnow()
    days = max(0.0, (now - last_signal_at).total_seconds() / 86400.0)
    halflife = cfg.get("scoring.recency_halflife_days", 180)
    return 0.5 ** (days / halflife)


def priority_score(
    cfg: Config,
    signal_types: list[SignalType],
    window: Window,
    tons_midpoint: float | None,
    last_signal_at: datetime | None,
    now: datetime | None = None,
) -> float:
    mult = cfg.get("scoring.window_multipliers", {}).get(window.value, 1.0)
    return round(
        certainty(cfg, signal_types)
        * mult
        * size_factor(tons_midpoint)
        * recency_decay(cfg, last_signal_at, now),
        4,
    )


def _days_to_bid_entry(cfg: Config, stage: Stage) -> int | float | dict | None:
    """Raw scoring.days_to_bid_by_stage value for this stage -- a plain
    number for a still-invented placeholder, or a {mid, low, high} dict for
    entitlement, the one stage measured from real CEQAnet NOP->NOD data
    (see config.yaml's own comment for sample size/method,
    app/assumptions.py for the full disclosure)."""
    table = cfg.get("scoring.days_to_bid_by_stage", {})
    val = table.get(stage.value)
    if val is None and stage is Stage.unknown:
        val = table.get("entitlement")  # unknown stage: assume mid-window
    return val


def days_to_estimated_bid(cfg: Config, stage: Stage) -> int | None:
    val = _days_to_bid_entry(cfg, stage)
    if isinstance(val, dict):
        return val.get("mid")
    return val


def days_to_estimated_bid_range(cfg: Config, stage: Stage) -> tuple[int | None, int | None]:
    """(low, high) confidence-interval bounds for days_to_estimated_bid --
    (None, None) for a stage with no measured interval (every stage except
    entitlement, currently). Never invented: a placeholder constant gets no
    fake interval just to fill the columns, because a zero-width or
    guessed range would read as more confidence than the number actually
    has."""
    val = _days_to_bid_entry(cfg, stage)
    if isinstance(val, dict):
        return val.get("low"), val.get("high")
    return None, None
