"""Block 3 (Master Plan v3.2 section 12a): the weakest-why decision rule.
Block 4B-prep-3 Item 4 adds the "Do:" sentence synthesis; Block 4B Item 4
rewrites it for the handoff play and the AB 869 deadline ask.

"An Opportunity ranks by its weakest why, never a weighted sum. Three Strong
is a call. Two Strong and one Weak is a call with a stated risk. Any ABSTAIN
is research, not a call." This module is the one place that rule is coded,
so Pipeline sort order and any other "which opportunity first" view stays
consistent with the master plan's own words instead of re-deriving it.
"""
from __future__ import annotations

from typing import TYPE_CHECKING

from sqlmodel import Session, select

from app.config import load_config
from app.contractors import default_radius_miles
from app.models import Ab869Plan, Contact, Contractor, ReasonStrength, RetrofitBuilding, TriggerType

if TYPE_CHECKING:
    from app.pipeline.signals_feed import FeedSignal

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


def _handoff_play_do_fields(session: Session, fs: "FeedSignal", contact: Contact,
                            contractor: Contractor) -> tuple[str | None, str | None, str | None]:
    """Block 4B Item 4: "the ask is the building list, not the single
    permit" -- a permit_gap/permit_activity signal whose matched contact
    is anchored on a Contractor firm. ABSTAINs when the signal's own
    building can't be resolved (no building_id, or the row is gone) --
    never falls back to a vaguer sentence that drops the one concrete
    building this Opportunity is actually about."""
    if fs.building_id is None:
        return None, None, None
    building = session.get(RetrofitBuilding, fs.building_id)
    if building is None:
        return None, None, None

    cfg = load_config()
    template = cfg.get("reason_block_do.handoff_play.template")
    ask = cfg.get("reason_block_do.handoff_play.ask")
    if not template or not ask:
        return None, None, None

    radius_miles = default_radius_miles(cfg)
    address = building.address or building.apn
    equipment = building.equipment_type or "unknown, not guessed"
    install_year = building.latest_install_year if building.latest_install_year else "unknown, not guessed"

    one_sentence = template.format(
        contact=contact.name, contractor=contractor.business_name, radius_miles=radius_miles,
        address=address, equipment=equipment, install_year=install_year, ask=ask,
    )
    return contact.name, ask, one_sentence


def _deadline_ab869_do_fields(session: Session, fs: "FeedSignal",
                              contact: Contact) -> tuple[str | None, str | None, str | None]:
    """Block 4B Item 4: "the ask names the plan status and the OSP-
    eligible package" -- reuses app.pipeline.hcai.hospital_capability_
    gaps directly (computed live from ProductLine.oshpd_osp, never
    hardcoded) rather than re-deriving a second, driftable view of which
    roles DMG can currently field into a hospital. ABSTAINs when no real
    Ab869Plan row resolves for this signal's facility_perm_id -- the
    generic by_trigger_type/deadline entry is the fallback for that
    case, not a guessed plan status."""
    from app.pipeline.hcai import hospital_capability_gaps

    if fs.facility_perm_id is None:
        return None, None, None
    plan = session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == fs.facility_perm_id)).first()
    if plan is None or not plan.plan_status:
        return None, None, None

    cfg = load_config()
    template = cfg.get("reason_block_do.deadline_ab869.template")
    if not template:
        return None, None, None

    gaps = hospital_capability_gaps(session)
    covered_labels = [rg["label"] for rg in gaps["role_gaps"] if rg["covered"]]
    package = ", ".join(covered_labels) if covered_labels else "no OSP-eligible package today"

    one_sentence = template.format(contact=contact.name, plan_status=plan.plan_status, package=package)
    return contact.name, f"whether they'd take the OSP-eligible package DMG can currently field ({package})", one_sentence


def _generic_do_fields(cfg, trigger_type: TriggerType, pen_state, contact: Contact) -> tuple:
    """The original config-driven pair (by_trigger_type's topic/ask,
    by_pen_state's urgency note) -- the fallback for every case the two
    more specific templates above don't cover."""
    trigger_cfg = cfg.get(f"reason_block_do.by_trigger_type.{trigger_type.value}")
    urgency = cfg.get(f"reason_block_do.by_pen_state.{pen_state.value}")
    if not trigger_cfg or not urgency:
        return None, None, None

    topic = trigger_cfg.get("topic")
    ask = trigger_cfg.get("ask")
    if not topic or not ask:
        return None, None, None

    one_sentence = f"Do: call {contact.name} about {topic} and ask {ask} ({urgency})."
    return contact.name, ask, one_sentence


def compose_do_fields(
    session: Session, fs: "FeedSignal", contact_id: int | None,
) -> tuple[str | None, str | None, str | None]:
    """(do_person, do_ask, one_sentence) for a ReasonBlock, composed at
    PROMOTE TIME -- plain string formatting from config.yaml's
    reason_block_do, no LLM. Picks the first template shape that applies:

    1. handoff_play -- fs.trigger_type is permit_gap/permit_activity AND
       the matched contact is anchored on a Contractor firm (Contact.
       contractor_id) -- "the ask is the building list, not the single
       permit."
    2. deadline_ab869 -- fs.trigger_type is deadline AND a real Ab869Plan
       row resolves for fs.facility_perm_id -- "the ask names the plan
       status and the OSP-eligible package."
    3. The original generic by_trigger_type/by_pen_state pair, for
       everything else (a permit_gap/permit_activity contact who is NOT
       contractor-anchored -- e.g. a project contact; a deadline signal
       with no resolvable plan; every other trigger_type, untouched).

    ABSTAINs (returns (None, None, None)) at any point a needed input is
    missing, rather than guessing: no contact_id, no Contact row, no
    Contact.name, or no config entry for the combination that actually
    applies -- see each private helper's own docstring for its specific
    ABSTAIN conditions."""
    if contact_id is None:
        return None, None, None
    contact = session.get(Contact, contact_id)
    if contact is None or not contact.name:
        return None, None, None

    if fs.trigger_type in (TriggerType.permit_gap, TriggerType.permit_activity) and contact.contractor_id is not None:
        contractor = session.get(Contractor, contact.contractor_id)
        if contractor is not None:
            result = _handoff_play_do_fields(session, fs, contact, contractor)
            if result != (None, None, None):
                return result

    if fs.trigger_type == TriggerType.deadline:
        result = _deadline_ab869_do_fields(session, fs, contact)
        if result != (None, None, None):
            return result

    cfg = load_config()
    return _generic_do_fields(cfg, fs.trigger_type, fs.pen_state, contact)
