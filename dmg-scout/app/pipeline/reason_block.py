"""Block 3 (Master Plan v3.2 section 12a): the weakest-why decision rule.

"An Opportunity ranks by its weakest why, never a weighted sum. Three Strong
is a call. Two Strong and one Weak is a call with a stated risk. Any ABSTAIN
is research, not a call." This module is the one place that rule is coded,
so Pipeline sort order and any other "which opportunity first" view stays
consistent with the master plan's own words instead of re-deriving it.
"""
from app.models import ReasonStrength

_STRENGTH_RANK = {
    ReasonStrength.Strong: 0,
    ReasonStrength.Weak: 1,
    ReasonStrength.ABSTAIN: 2,
}


def weakest_why_rank(strengths: list[ReasonStrength]) -> tuple[int, int]:
    """Sort key for a list of exactly the three ReasonBlock strengths (them,
    now, win) belonging to one Opportunity. Lower sorts first/stronger.

    Primary component: the single worst strength among the three (Strong=0,
    Weak=1, ABSTAIN=2) -- this alone enforces "ranks by its weakest why,
    never a weighted sum": any ABSTAIN outranks (sorts after) any all-Strong-
    or-Weak combination, full stop, regardless of the other two whys.

    Secondary component (tiebreak within the same worst tier): the sum of
    all three ranks, so within "no ABSTAIN present" 3-Strong (sum 0) sorts
    ahead of 2-Strong-1-Weak (sum 1) ahead of 1-Strong-2-Weak (sum 2) ahead
    of 3-Weak (sum 3); within "at least one ABSTAIN present" the same sum
    tiebreak orders fewer ABSTAINs first.
    """
    if len(strengths) != 3:
        raise ValueError(f"expected exactly 3 why strengths (them, now, win), got {len(strengths)}")
    ranks = [_STRENGTH_RANK[s] for s in strengths]
    return (max(ranks), sum(ranks))
