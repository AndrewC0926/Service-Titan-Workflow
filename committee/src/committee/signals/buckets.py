"""Deterministic bucket tagging and the asymmetric-bet profile (DESIGN 6).

* core_pick: market cap >= core min ($2B) and ADV >= core min ($20M).
* asymmetric_bet: not core-eligible, asym min ($300M) <= cap <= asym max ($10B) and
  ADV >= asym min ($3M). These names must also pass the lottery filter.
* Anything else (for example a $15B name trading $5M a day) has no bucket and is not
  eligible for the shortlist.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

from committee.config.schema import UniverseConfig

BucketName = Literal["core_pick", "asymmetric_bet"]
REVENUE_GROWTH_MIN = 0.20
UNDERFOLLOWED_MAX_ANALYSTS = 6


def tag_bucket(
    market_cap: float | None, adv: float | None, universe: UniverseConfig
) -> BucketName | None:
    if market_cap is None or adv is None:
        return None
    core, asym = universe.core_pick, universe.asymmetric_bet
    if market_cap >= core.min_market_cap_usd and adv >= core.min_adv_usd:
        return "core_pick"
    if asym.min_market_cap_usd <= market_cap <= asym.max_market_cap_usd and adv >= asym.min_adv_usd:
        return "asymmetric_bet"
    return None


@dataclass(frozen=True)
class AsymmetricProfile:
    """What the screen looks for in an asymmetric bet. ``score`` = criteria met;
    ``available`` = criteria with data (analyst coverage is often unavailable)."""

    revenue_growth: float | None
    gross_margin_improving: bool | None
    high_growth: bool | None  # revenue growth > 20%/yr AND improving gross margin
    insider_buying: bool  # opportunistic insider buying or a cluster buy
    positive_momentum: bool | None
    analyst_count: int | None
    underfollowed: bool | None
    score: int
    available: int


def asymmetric_profile(
    *,
    revenue_growth: float | None,
    gross_margin: float | None,
    gross_margin_prior: float | None,
    opportunistic_buying: bool,
    cluster_buy: bool,
    momentum_12_1: float | None,
    analyst_count: int | None,
) -> AsymmetricProfile:
    improving = (
        None
        if gross_margin is None or gross_margin_prior is None
        else gross_margin > gross_margin_prior
    )
    high_growth = (
        None
        if revenue_growth is None or improving is None
        else revenue_growth > REVENUE_GROWTH_MIN and improving
    )
    pos_mom = None if momentum_12_1 is None else momentum_12_1 > 0
    under = None if analyst_count is None else analyst_count < UNDERFOLLOWED_MAX_ANALYSTS
    insider = opportunistic_buying or cluster_buy
    criteria: list[bool | None] = [high_growth, insider, pos_mom, under]
    return AsymmetricProfile(
        revenue_growth=revenue_growth,
        gross_margin_improving=improving,
        high_growth=high_growth,
        insider_buying=insider,
        positive_momentum=pos_mom,
        analyst_count=analyst_count,
        underfollowed=under,
        score=sum(1 for c in criteria if c),
        available=sum(1 for c in criteria if c is not None),
    )
