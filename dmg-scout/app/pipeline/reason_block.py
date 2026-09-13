"""Block 3 (Master Plan v3.2 section 12a): the weakest-why decision rule.
Block 4B-prep-3 Item 4 adds the "Do:" sentence synthesis.

"An Opportunity ranks by its weakest why, never a weighted sum. Three Strong
is a call. Two Strong and one Weak is a call with a stated risk. Any ABSTAIN
is research, not a call." This module is the one place that rule is coded,
so Pipeline sort order and any other "which opportunity first" view stays
consistent with the master plan's own words instead of re-deriving it.
"""
from sqlmodel import Session

from app.config import load_config
from app.models import Contact, PenState, ReasonStrength, TriggerType

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


def weakest_of(strengths: list[ReasonStrength]) -> ReasonStrength:
    """The single strength value Pipeline's own "weakest why" column shows
    (Item 5) -- the worst of the three, by the same ranking weakest_why_rank
    sorts on. Strong=0 is best, ABSTAIN=2 is worst, so this is the
    strength at max(rank), not min."""
    return max(strengths, key=lambda s: _STRENGTH_RANK[s])


def compose_do_fields(
    session: Session, trigger_type: TriggerType, pen_state: PenState, contact_id: int | None,
) -> tuple[str | None, str | None, str | None]:
    """(do_person, do_ask, one_sentence) for a ReasonBlock, composed at
    PROMOTE TIME from config.yaml's reason_block_do -- two config-driven
    templates (by_trigger_type's topic/ask, by_pen_state's urgency note),
    combined by plain string formatting, no LLM. Mirrors the exact shape
    of ReasonBlock's own worked example ("Do: call University Mechanical's
    project executive about the next NPC package and ask who is designing
    it").

    ABSTAINs (returns (None, None, None)) rather than guessing whenever
    any input it needs is missing: no contact_id (four_part_filter's own
    named_reachable_contact must have passed for a real promotion, but
    this function doesn't assume that -- it checks), no Contact row found,
    or no config entry for this exact trigger_type/pen_state combination
    (e.g. quiet_account, which no FeedSignal source produces yet and so
    has no config entry -- adding one only when a real source exists to
    exercise it, never speculatively)."""
    if contact_id is None:
        return None, None, None
    contact = session.get(Contact, contact_id)
    if contact is None or not contact.name:
        return None, None, None

    cfg = load_config()
    trigger_cfg = cfg.get(f"reason_block_do.by_trigger_type.{trigger_type.value}")
    urgency = cfg.get(f"reason_block_do.by_pen_state.{pen_state.value}")
    if not trigger_cfg or not urgency:
        return None, None, None

    topic = trigger_cfg.get("topic")
    ask = trigger_cfg.get("ask")
    if not topic or not ask:
        return None, None, None

    do_person = contact.name
    do_ask = ask
    one_sentence = f"Do: call {do_person} about {topic} and ask {do_ask} ({urgency})."
    return do_person, do_ask, one_sentence
