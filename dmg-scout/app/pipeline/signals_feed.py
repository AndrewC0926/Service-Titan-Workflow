"""Block 3 Item 2 (Master Plan v3.2 section 12): Signal consolidation and
the four-part filter.

"The central transform: Signal to Opportunity through the four-part
filter, config-driven, no LLM. Named reachable human, sellable account or
building, dated reason, eligible fitting line. Missing any one, it stays a
Signal (market context). All four, it becomes an Opportunity (a lead)."

unified_signals() folds six existing sources into ONE read-time shape
(FeedSignal) -- board Projects, RetrofitBuilding replacement candidates, AB
869 facilities with NPC outstanding, HCAI open mechanical projects, OPSC
pre-spec rows, and FieldIntel. It is a view over existing tables, not a new
persisted table: no migration, and every existing table keeps its own
identity and write path exactly as it already had one (see docs/BUILD-
PLAN.md section 9's mapping report for why each source maps to which
trigger_type).

four_part_filter() and promote_to_opportunity() are pure/config-driven, no
LLM -- reusing the SAME eligibility machinery the rest of Scout already
has (app.accounts.line_offering_by_role, app.pipeline.opsc.classify_opsc_
status) rather than inventing a second one.
"""
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from app.models import (
    Ab869Plan,
    Category,
    Contact,
    FieldIntel,
    HcaiProject,
    HospitalBuilding,
    Opportunity,
    OpscProject,
    Project,
    ProjectContact,
    ProjectSignal,
    ReasonBlock,
    ReasonStrength,
    RetrofitBuilding,
    Signal as SignalRow,
    TriggerType,
    WhyKind,
)
from app.pipeline.opsc import OpscStatusClass, classify_opsc_status

# NPC ratings HCAI itself publishes that mean "fully compliant" -- everything
# else real (not None/"N/A"/"NYA", which mean unknown, not outstanding) is
# outstanding. See HospitalBuilding's own docstring: npc_rating is HCAI's raw
# code, kept verbatim.
_NPC_COMPLIANT = {"5", "5s"}
_NPC_UNKNOWN = {None, "N/A", "NYA"}


@dataclass
class FeedSignal:
    """One row of the unified Signal shape. Read-time only -- never
    persisted (see module docstring) -- so `source`/`source_id` are the
    only durable identity: they point back at the one real row this was
    built from, which is what "each row links to its Signal" (Item 4) and
    promote_to_opportunity (below) both key off."""
    source: str          # "project" | "retrofit_building" | "ab869_plan" | "hcai_project" | "opsc_project" | "field_intel"
    source_id: str
    trigger_type: TriggerType
    trigger_date: datetime | None
    evidence: str
    # None = ABSTAIN: this source makes no confidence claim at all, not "low
    # confidence" -- see Signal.confidence (0.0 default, pre-dates any real
    # extraction) and RetrofitBuilding.estimated_tons_basis for the same
    # never-guessed-a-number-into-existence discipline this reuses.
    confidence: float | None
    project_id: int | None = None
    # The only FK target Opportunity.building_id has today (Item 1) --
    # HospitalBuilding-anchored signals (ab869_plan, hcai_project) have no
    # building anchor to offer here on principle, not oversight; see
    # four_part_filter's "sellable account or building" part.
    building_id: int | None = None
    account_id: int | None = None
    category: Category | None = None


def _project_signals(session: Session) -> list[FeedSignal]:
    out = []
    projects = session.exec(select(Project)).all()
    links = session.exec(select(ProjectSignal)).all()
    links_by_project: dict[int, list[ProjectSignal]] = {}
    for link in links:
        links_by_project.setdefault(link.project_id, []).append(link)
    signal_ids = {link.signal_id for link in links}
    signals = {s.id: s for s in session.exec(
        select(SignalRow).where(SignalRow.id.in_(signal_ids)))} if signal_ids else {}

    for p in projects:
        plinks = links_by_project.get(p.id, [])
        linked = [signals[l.signal_id] for l in plinks if l.signal_id in signals]
        latest = max((s.event_date for s in linked if s.event_date), default=None)
        confidence = max((s.confidence for s in linked), default=None)
        out.append(FeedSignal(
            source="project", source_id=str(p.id),
            trigger_type=TriggerType.entitlement_milestone,
            trigger_date=latest,
            evidence=f"{p.name} ({p.category.value}, stage={p.stage.value})",
            confidence=confidence if confidence else None,
            project_id=p.id, category=p.category,
        ))
    return out


def _retrofit_replacement_candidates(session: Session) -> list[FeedSignal]:
    """RetrofitBuilding.population == 'replacement_candidate': "absence is
    the signal here" (see the model's own docstring) -- a commercial/
    industrial parcel old enough to need equipment but with no permit on
    record at all. trigger_date is always None (there is, by definition,
    no dated event for this population -- the whole point is the absence
    of one), which is why this trigger_type is named permit_gap, not
    permit_activity."""
    rows = session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.population == "replacement_candidate")).all()
    out = []
    for b in rows:
        band = (f"{b.estimated_tons_low:.0f}-{b.estimated_tons_high:.0f} tons ({b.estimated_tons_basis})"
                if b.estimated_tons_low and b.estimated_tons_high else "tonnage unestimated")
        out.append(FeedSignal(
            source="retrofit_building", source_id=str(b.id),
            trigger_type=TriggerType.permit_gap,
            trigger_date=None,
            evidence=f"{b.address or b.apn}: no mechanical permit on record since "
                     f"before the permit window, built {b.year_built or '?'}, {band}",
            confidence=None,
            building_id=b.id,
        ))
    return out


def _ab869_npc_outstanding(session: Session) -> list[FeedSignal]:
    """AB 869 facilities (Ab869Plan, facility grain) joined to
    HospitalBuilding by perm_id where at least one building's npc_rating is
    a real, known code other than fully-compliant (5/5s) -- "outstanding"
    means HCAI itself published a non-compliant rating, not "unknown"
    (N/A/NYA buildings are excluded, not counted as outstanding, on the
    same never-guess discipline as everywhere else in this module).
    trigger_date is Jan 1 of npc_deadline_year when Scout's own derived
    deadline year is available, else None (ABSTAIN on the date, never the
    plan's own filing date substituted for a regulatory deadline)."""
    plans = session.exec(select(Ab869Plan)).all()
    perm_ids = [p.perm_id for p in plans]
    if not perm_ids:
        return []
    buildings = session.exec(
        select(HospitalBuilding).where(HospitalBuilding.perm_id.in_(perm_ids))).all()
    buildings_by_perm: dict[str, list[HospitalBuilding]] = {}
    for b in buildings:
        buildings_by_perm.setdefault(b.perm_id, []).append(b)

    out = []
    for plan in plans:
        outstanding = [b for b in buildings_by_perm.get(plan.perm_id, [])
                       if b.npc_rating not in _NPC_UNKNOWN and b.npc_rating not in _NPC_COMPLIANT]
        if not outstanding:
            continue
        deadline_years = [b.npc_deadline_year for b in outstanding if b.npc_deadline_year]
        trigger_date = datetime(min(deadline_years), 1, 1) if deadline_years else None
        names = ", ".join(sorted({b.building_name or b.building_nbr for b in outstanding}))
        out.append(FeedSignal(
            source="ab869_plan", source_id=plan.perm_id,
            trigger_type=TriggerType.deadline,
            trigger_date=trigger_date,
            evidence=f"AB 869 plan status={plan.plan_status or 'unknown'}; "
                     f"NPC outstanding on {len(outstanding)} building(s): {names}",
            confidence=None,
        ))
    return out


def _hcai_open_mechanical(session: Session) -> list[FeedSignal]:
    """HcaiProject rows flagged is_mechanical, stage not yet closed --
    "open" per app.models.HcaiProject.stage's own plan_review/pending_
    start/in_construction/closed/other collapse of HCAI's raw status text.
    trigger_date is date_in when the source states it, else the source
    file's own report_date (never utcnow -- see HcaiProject's docstring on
    why report_date exists at all)."""
    rows = session.exec(
        select(HcaiProject).where(
            HcaiProject.is_mechanical == True,  # noqa: E712
            HcaiProject.stage.in_(("plan_review", "pending_start", "in_construction")),
        )).all()
    out = []
    for r in rows:
        out.append(FeedSignal(
            source="hcai_project", source_id=r.record_no,
            trigger_type=TriggerType.public_work,
            trigger_date=r.date_in or r.report_date,
            evidence=f"{r.facility_name}: {r.scope_text[:200]} (stage={r.stage}, status={r.status_raw})",
            confidence=None,
        ))
    return out


def _opsc_pre_spec(session: Session) -> list[FeedSignal]:
    """OPSC applications not yet at Funds Released (procurement) or Closed
    (operating) -- "engineer, spec not locked" per app.pipeline.opsc's own
    signal_stage docstring, i.e. status_class in {open, unknown}. Reuses
    classify_opsc_status directly rather than re-deriving the same bucket a
    second way. trigger_date is Last_SAB_Date, OPSC's own last-activity
    date on the application -- the only date this source states."""
    rows = session.exec(select(OpscProject).where(OpscProject.in_territory == True)).all()  # noqa: E712
    out = []
    for r in rows:
        status_class = classify_opsc_status(r.status)
        if status_class == OpscStatusClass.closed:
            continue
        if status_class == OpscStatusClass.funds_released and r.program in ("Modernization", "New Construction"):
            continue  # procurement, spec already locked -- not pre-spec
        out.append(FeedSignal(
            source="opsc_project", source_id=r.application_number,
            trigger_type=TriggerType.public_work,
            trigger_date=r.last_sab_date,
            evidence=f"{r.district or '?'} / {r.school_name or '?'}: {r.program or '?'} "
                     f"(status={r.status or 'unknown'})",
            confidence=None,
        ))
    return out


def _field_intel_signals(session: Session) -> list[FeedSignal]:
    """FieldIntel: human-sourced, in-conversation intelligence, before any
    of it is a public document -- see app.models.FieldIntel's own
    docstring. trigger_date is reported_at (when the human reported it,
    the only date this source has); confidence is None throughout (a
    person's word is evidence, not a numeric confidence score -- scoring
    it would manufacture a precision FieldIntel itself does not claim)."""
    rows = session.exec(select(FieldIntel)).all()
    out = []
    for r in rows:
        out.append(FeedSignal(
            source="field_intel", source_id=str(r.id),
            trigger_type=TriggerType.relationship_intro,
            trigger_date=r.reported_at,
            evidence=r.source_notes,
            confidence=None,
            account_id=r.engineer_account_id or r.mech_contractor_account_id,
        ))
    return out


def unified_signals(session: Session) -> list[FeedSignal]:
    """Every source folded into one shape. quiet_account (Account rows with
    no recent activity) is not built here: Account has exactly 1 row in
    Scout today (see docs/BUILD-PLAN.md section 9's mapping report) and no
    "last activity" concept exists yet to compute quiet from -- the enum
    value exists in TriggerType for when that source exists, deliberately
    left unbuilt rather than faked, same discipline as RetrofitBuilding's
    owner field."""
    return (
        _project_signals(session)
        + _retrofit_replacement_candidates(session)
        + _ab869_npc_outstanding(session)
        + _hcai_open_mechanical(session)
        + _opsc_pre_spec(session)
        + _field_intel_signals(session)
    )


# ---------------------------------------------------------------------------
# The four-part filter
# ---------------------------------------------------------------------------

FOUR_PARTS = ("named_reachable_contact", "sellable_account_or_building",
              "dated_reason", "eligible_fitting_line")


@dataclass
class FourPartResult:
    passed: bool
    missing: list[str] = field(default_factory=list)
    contact_id: int | None = None
    line_id: int | None = None


def _named_reachable_contact(session: Session, fs: FeedSignal) -> int | None:
    """A Contact row with reach_status='confirmed' (phone or email
    populated -- see Contact's own docstring), linked to this signal's
    Project via ProjectContact. Only project-sourced signals have a link
    table to check today: retrofit/ab869/hcai/opsc/field_intel have no
    Contact-join table wired to them yet -- this is a real, disclosed gap
    (see docs/BUILD-PLAN.md section 9's Item 2 report), not an oversight
    silently worked around here."""
    if fs.project_id is None:
        return None
    rows = session.exec(
        select(Contact).join(ProjectContact, ProjectContact.contact_id == Contact.id)
        .where(ProjectContact.project_id == fs.project_id, Contact.reach_status == "confirmed")
    ).all()
    return rows[0].id if rows else None


def _sellable_account_or_building(fs: FeedSignal) -> bool:
    """Opportunity.account_id/building_id, Item 1's own two anchors --
    account_id needs a real NetSuite-identified Account (never built by
    this module: no Project-to-Account join exists in Scout today, see
    the Item 1 mapping report's Account row), building_id only ever comes
    from RetrofitBuilding (the only FK target Opportunity.building_id
    has) -- HospitalBuilding-anchored signals (ab869_plan, hcai_project)
    structurally cannot pass this part until a building anchor for them
    is added, which is a real, disclosed schema gap, not a bug here."""
    return fs.account_id is not None or fs.building_id is not None


def _dated_reason(fs: FeedSignal) -> bool:
    return fs.trigger_date is not None


def _eligible_fitting_line(session: Session, fs: FeedSignal) -> int | None:
    """Eligibility from the OSP register (ProductLine.oshpd_osp) and line
    facets (ProductLine.building_role/category), reusing app.accounts.
    line_offering_by_role -- the SAME eligibility engine the rest of Scout
    already uses, not a second one. Only signals that carry a Category
    Scout's line card actually spans (data_center/industrial/esco -- see
    Category's own docstring) can be checked this way; every other source
    (retrofit -- equipment_type is null by construction for replacement
    candidates, ab869/hcai -- HCAI-governed with no category concept at
    all, opsc -- K-12 is not a category Scout's card covers, field_intel --
    no category) returns None (ABSTAIN, this part is missing), honestly,
    rather than guessing a category to force a pass."""
    if fs.category is None:
        return None
    from app.accounts import facility_types_by_project, line_offering_by_role
    from app.models import FacilityType

    facility_type = FacilityType.unknown
    if fs.project_id is not None:
        facility_type = facility_types_by_project(session, [fs.project_id]).get(fs.project_id, FacilityType.unknown)

    offerings = line_offering_by_role(session, fs.category, facility_type)
    for off in offerings:
        if off.relevant and not off.gap and off.lines:
            return off.lines[0].id
    return None


def resolve_signal_id(session: Session, fs: FeedSignal) -> int | None:
    """The real `signals` table row to attach an Opportunity to
    (Opportunity.signal_id is not nullable, Item 1). Only project-sourced
    FeedSignals have one today -- via the existing ProjectSignal link, the
    same real Signal row `_project_signals` above already reads. Every
    other source (retrofit_building, ab869_plan, hcai_project,
    opsc_project, field_intel) has no `signals` row behind it at all: this
    module builds their FeedSignal shape straight from their own table,
    never from `signals`. That is a real, disclosed gap surfaced by Item
    5's Promote button, not silently worked around here -- Block 4 is
    where a real Signal row (or a nullable Opportunity.signal_id) for
    these five sources gets decided and built. Returns None for them on
    principle; callers must treat None as "cannot promote yet", a fifth,
    separate reason beyond the four-part filter's own four."""
    if fs.source != "project" or fs.project_id is None:
        return None
    link = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == fs.project_id)
        .order_by(ProjectSignal.linked_at.desc())
    ).first()
    return link.signal_id if link else None


def four_part_filter(session: Session, fs: FeedSignal) -> FourPartResult:
    """Pure, config-driven, no LLM. Returns pass, or the list of missing
    parts by name -- never a partial score, per the plan's own words:
    "Missing any one, it stays a Signal... All four, it becomes an
    Opportunity." """
    contact_id = _named_reachable_contact(session, fs)
    line_id = _eligible_fitting_line(session, fs)
    missing = []
    if contact_id is None:
        missing.append("named_reachable_contact")
    if not _sellable_account_or_building(fs):
        missing.append("sellable_account_or_building")
    if not _dated_reason(fs):
        missing.append("dated_reason")
    if line_id is None:
        missing.append("eligible_fitting_line")
    return FourPartResult(passed=not missing, missing=missing, contact_id=contact_id, line_id=line_id)


# ---------------------------------------------------------------------------
# Promote to Opportunity
# ---------------------------------------------------------------------------

def promote_to_opportunity(session: Session, fs: FeedSignal, signal_id: int) -> Opportunity:
    """Creates the Opportunity and its three-row ReasonBlock from a
    FeedSignal that has already passed four_part_filter (callers must
    check first -- this function does not re-check, so it can also be used
    to backfill a ReasonBlock for a manually-created Opportunity in tests).
    signal_id is the real `signals` table row to attach (Opportunity.
    signal_id is not nullable, Item 1) -- FeedSignal itself is never
    persisted, so callers resolve or create that row before calling this.

    "Why them"/"why now"/"why win" are filled from what this module can
    actually see today -- Block 3 is public data only, no DMG data, no
    pairing/relationship evidence exists yet, so "win" is ABSTAIN for
    every promotion this block ever makes; that is an honest reflection
    of scope, not a bug to route around here."""
    result = four_part_filter(session, fs)
    opp = Opportunity(
        account_id=fs.account_id,
        building_id=fs.building_id,
        contact_id=result.contact_id,
        signal_id=signal_id,
        line_id=result.line_id,
    )
    session.add(opp)
    session.flush()

    if fs.account_id is not None:
        them_strength, them_evidence = ReasonStrength.Strong, f"Known account (account_id={fs.account_id})"
    elif fs.building_id is not None:
        them_strength, them_evidence = ReasonStrength.Weak, f"Building identified (building_id={fs.building_id}), owner unknown"
    else:
        them_strength, them_evidence = ReasonStrength.ABSTAIN, "No account or building identified"

    now_strength = ReasonStrength.Strong if fs.trigger_date else ReasonStrength.ABSTAIN
    win_strength, win_evidence = ReasonStrength.ABSTAIN, "No DMG relationship/pairing evidence available (Block 3: public data only)"

    rows = [
        ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.them, strength=them_strength,
                   evidence=them_evidence, source=fs.source),
        ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.now, strength=now_strength,
                   evidence=fs.evidence, source=fs.source),
        ReasonBlock(opportunity_id=opp.id, why_kind=WhyKind.win, strength=win_strength,
                   evidence=win_evidence, source=fs.source),
    ]
    for row in rows:
        session.add(row)
    session.flush()
    return opp
