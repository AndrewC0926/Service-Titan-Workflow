"""Block 4A Item 5 (Master Plan v3.6): Today re-sourced from Pipeline.

"The three calls come from Pipeline ranked by weakest why; when Pipeline
has fewer than three, fill from Deadlines with a 'no contact yet' flag
rather than from the old board score." Replaces app.pipeline.notify.
three_calls_today (the old Project/board-score selection) as Today's own
"Three to call" source -- that function is untouched and still used
elsewhere (the email digest), this is additive, a new source for one page
section.

"Active-learning spread: one of the three slots each day goes to the
highest-ranked Opportunity with a Weak why, not a Strong one, so labels
land across the range" -- section 13/15's active-learning intent, applied
here specifically to the three call slots rather than the whole board.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.models import Contact, Opportunity, OpportunityStage, ReasonBlock, ReasonStrength
from app.pipeline.reason_block import weakest_of, weakest_why_rank


def _ranked_open_opportunities(session: Session) -> list[tuple[tuple[int, int], Opportunity, ReasonStrength]]:
    """Every open (not won/lost) Opportunity with a COMPLETE (3-row)
    Reason Block, ranked weakest-why first (ascending sort_key = strongest
    first) -- the SAME rule Pipeline's own table sorts by (Block 3 Item
    5), so "ranked by weakest why" means one rule, read from one place,
    not re-derived here. An incomplete Reason Block (a promotion path that
    hasn't written all three whys yet) has nothing to rank and is
    skipped, not guessed into a rank."""
    opportunities = session.exec(
        select(Opportunity).where(Opportunity.stage.not_in((OpportunityStage.won, OpportunityStage.lost)))
    ).all()
    if not opportunities:
        return []
    reason_blocks = session.exec(
        select(ReasonBlock).where(ReasonBlock.opportunity_id.in_([o.id for o in opportunities]))
    ).all()
    blocks_by_opp: dict[int, list[ReasonStrength]] = {}
    for rb in reason_blocks:
        blocks_by_opp.setdefault(rb.opportunity_id, []).append(rb.strength)

    ranked = []
    for o in opportunities:
        strengths = blocks_by_opp.get(o.id, [])
        if len(strengths) != 3:
            continue
        ranked.append((weakest_why_rank(strengths), o, weakest_of(strengths)))
    ranked.sort(key=lambda t: t[0])
    return ranked


def three_calls_from_pipeline(session: Session) -> list[dict]:
    """Up to 3 cards, each either:
    {"kind": "opportunity", "opportunity": Opportunity, "weakest_why":
    ReasonStrength, "contact": Contact | None, "no_contact_yet": bool,
    "active_learning_pick": bool}
    or, filling any remaining slots from Deadlines:
    {"kind": "deadline", "row": DeadlineRow, "no_contact_yet": True}
    """
    ranked = _ranked_open_opportunities(session)
    top3 = list(ranked[:3])

    # Active-learning spread: force one slot to the best-ranked Weak-why
    # opportunity, if one exists and isn't already among the natural
    # top 3 -- displaces the current LAST slot (the weakest of the
    # natural picks) rather than a stronger one, so the spread costs the
    # least-informative slot, not the best call of the day.
    weak_candidates = [t for t in ranked if t[2] == ReasonStrength.Weak]
    active_learning_id = None
    if weak_candidates:
        best_weak = weak_candidates[0]
        if best_weak not in top3:
            if len(top3) == 3:
                top3[-1] = best_weak
            else:
                top3.append(best_weak)
        active_learning_id = best_weak[1].id

    cards = []
    for _, o, weakest in top3:
        contact = session.get(Contact, o.contact_id) if o.contact_id else None
        cards.append({
            "kind": "opportunity", "opportunity": o, "weakest_why": weakest, "contact": contact,
            "no_contact_yet": contact is None, "active_learning_pick": o.id == active_learning_id,
        })

    if len(cards) < 3:
        from app.pipeline.deadlines import deadlines_by_regulation

        groups = deadlines_by_regulation(session)
        all_rows = [row for rows in groups.values() for row in rows]
        all_rows.sort(key=lambda r: (r.date is None, r.date or datetime.max))
        for row in all_rows:
            if len(cards) >= 3:
                break
            cards.append({"kind": "deadline", "row": row, "no_contact_yet": True})

    return cards
