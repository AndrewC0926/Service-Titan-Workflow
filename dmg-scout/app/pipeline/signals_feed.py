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
import weakref
from dataclasses import dataclass, field
from datetime import datetime

from sqlmodel import Session, select

from app.config import load_config
from app.contractors import default_radius_miles, haversine_miles
from app.models import (
    Ab869Plan,
    Category,
    Contact,
    Contractor,
    FieldIntel,
    HcaiProject,
    HospitalBuilding,
    Opportunity,
    Origin,
    OpscProject,
    PenState,
    Project,
    ProjectContact,
    ProjectSignal,
    ReasonBlock,
    ReasonStrength,
    RetrofitBuilding,
    Signal as SignalRow,
    SignalType,
    Stage,
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
    # The two physical anchors Opportunity offers beyond account_id (Block
    # 4A Item 1 added facility_perm_id -- Master Plan v3.6: "an Opportunity
    # may anchor on an Account, a Building, or a Deadline facility").
    building_id: int | None = None
    facility_perm_id: str | None = None
    account_id: int | None = None
    category: Category | None = None
    # Master Plan v3.6 section 12b: "every Signal and Opportunity carries
    # pen_state." Computed per-source below from whatever real evidence
    # that source actually states -- see each builder's own docstring for
    # its basis; ABSTAIN by default, never guessed.
    pen_state: PenState = PenState.ABSTAIN


# Project.stage -> pen_state (Block 4A Item 1): spec-clock earliness per
# Master Plan v3.6 section 12b -- "early means before Division 23 is
# written and a basis of design is named." concept/entitlement/design are
# all pre-Division-23 (not_moved); permitting is the transitional window
# (moving); procurement/construction mean a contractor is already engaged
# (moved). operating/unknown carry no basis to say either way (ABSTAIN).
_PROJECT_STAGE_PEN_STATE = {
    Stage.concept: PenState.not_moved,
    Stage.entitlement: PenState.not_moved,
    Stage.design: PenState.not_moved,
    Stage.permitting: PenState.moving,
    Stage.procurement: PenState.moved,
    Stage.construction: PenState.moved,
}


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
            pen_state=_PROJECT_STAGE_PEN_STATE.get(p.stage, PenState.ABSTAIN),
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
            # not_moved is the population's own definition, not a guess:
            # "no permit on record at all" IS the evidence that no
            # contractor has been engaged yet -- see the model's docstring.
            pen_state=PenState.not_moved,
        ))
    return out


def _retrofit_recently_active(session: Session) -> list[FeedSignal]:
    """RetrofitBuilding.population == 'recently_active': the symmetric
    opposite of _retrofit_replacement_candidates above -- real, dated,
    permit-VERIFIED mechanical work on record (2010-present), not an
    absence. 6,914 such rows in the real local restore; only 2,402 of
    those (34.7%) carry a populated equipment_type -- the rest have a real
    permit on record but infer_equipment_type's own never-guess-past-the-
    work-description discipline (see that function's docstring) left it
    null, exactly like replacement_candidate rows do for a different
    reason. All 6,914 are still folded in here (the same "include the
    whole population, let each downstream part ABSTAIN honestly on what
    it can't determine" discipline replacement_candidate itself uses) --
    _eligible_fitting_line_from_equipment_class already ABSTAINs
    (returns None) on a null/unrecognized equipment_type with zero
    special-casing needed here, since it keys off fs.building_id, not
    population.

    trigger_date is Jan 1 of latest_install_year -- a real, permit-derived
    year, populated on all 6,914 rows in the real local restore (this
    population's own permit-verified definition guarantees it, unlike
    equipment_type above).

    pen_state, from this table's own already-computed service_life_status
    (app.replacement's ownership-branched table, keyed off equipment_age_
    years/latest_install_year -- never re-derived here): a real permit on
    record is direct evidence a contractor HAS already been engaged, so
    `not_due`/`approaching` (still within its expected life -- 2,392 of
    6,914) means that hold is still current, moved -- the window Master
    Plan v3.6 section 12b describes ("the window closes when a contractor
    with an incumbent brand relationship is on site") is closed. Where the
    same field says the equipment has since reached or passed its
    expected replacement window (`due`/`overdue` -- 10 of 6,914), that
    presumed hold is stale and the window has plausibly reopened --
    not_moved. service_life_status is null on 4,512 of 6,914 (population
    definition guarantees a real install year but not a resolvable
    service-life band for every use code) -- ABSTAIN there, never guessed
    into either state."""
    rows = session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.population == "recently_active")).all()
    out = []
    for b in rows:
        trigger_date = datetime(b.latest_install_year, 1, 1) if b.latest_install_year else None
        equip = b.equipment_type or "equipment type unknown"
        evidence = (f"{b.address or b.apn}: permit-verified {equip} work on record"
                   f"{f' ({b.latest_permit_nbr})' if b.latest_permit_nbr else ''}, "
                   f"install year {b.latest_install_year or '?'}, "
                   f"service life {b.service_life_status or 'unknown'}")
        if b.service_life_status is None:
            pen_state = PenState.ABSTAIN
        elif b.service_life_status in ("due", "overdue"):
            pen_state = PenState.not_moved
        else:  # not_due, approaching
            pen_state = PenState.moved
        out.append(FeedSignal(
            source="retrofit_building", source_id=str(b.id),
            trigger_type=TriggerType.permit_activity,
            trigger_date=trigger_date,
            evidence=evidence,
            confidence=None,
            building_id=b.id,
            pen_state=pen_state,
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
    plan's own filing date substituted for a regulatory deadline).

    pen_state (Block 4A Item 1): Master Plan v3.6 section 12b's own literal
    rule -- "On AB 869, earliness is plan status: Not Approved with no
    contractor named is early." Scout has no contractor field on
    HcaiProject/Ab869Plan to confirm the "late" half of that rule (a
    mechanical contractor on record), so this only ever sets not_moved
    (Not Approved / Not Submitted -- both mean the facility has not yet
    engaged HCAI's review process at all) or ABSTAIN, never `moved` or
    `moving`, which this data cannot support -- checked against the real
    plan_status distribution (8 distinct values in the local restore)
    before writing this mapping, not assumed."""
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
        pen_state = (PenState.not_moved if plan.plan_status in ("Not Approved", "Not Submitted")
                    else PenState.ABSTAIN)
        out.append(FeedSignal(
            source="ab869_plan", source_id=plan.perm_id,
            trigger_type=TriggerType.deadline,
            trigger_date=trigger_date,
            evidence=f"AB 869 plan status={plan.plan_status or 'unknown'}; "
                     f"NPC outstanding on {len(outstanding)} building(s): {names}",
            confidence=None,
            facility_perm_id=plan.perm_id,
            pen_state=pen_state,
        ))
    return out


_HCAI_STAGE_PEN_STATE = {
    "plan_review": PenState.not_moved,
    "pending_start": PenState.moving,
    "in_construction": PenState.moved,
}


def _hcai_open_mechanical(session: Session) -> list[FeedSignal]:
    """HcaiProject rows flagged is_mechanical, stage not yet closed --
    "open" per app.models.HcaiProject.stage's own plan_review/pending_
    start/in_construction/closed/other collapse of HCAI's raw status text.
    trigger_date is date_in when the source states it, else the source
    file's own report_date (never utcnow -- see HcaiProject's docstring on
    why report_date exists at all). pen_state (Block 4A Item 1) maps
    directly from that same stage: plan_review is pre-construction
    (not_moved), pending_start is the transitional window (moving),
    in_construction means field work is underway (moved)."""
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
            pen_state=_HCAI_STAGE_PEN_STATE.get(r.stage, PenState.ABSTAIN),
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
            # "engineer, spec not locked" (app.pipeline.opsc's own status_class
            # comment) is a direct pre-spec statement -- not_moved, not a guess.
            pen_state=PenState.not_moved,
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
        + _retrofit_recently_active(session)
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


# Block 4B-prep-2 Item 2: cell size for the in-memory contractor-contact
# proximity grid below, in degrees -- 0.5 degrees is ~34.5mi at this
# state's latitude (app.contractors.MILES_PER_DEGREE_LAT), safely wider
# than contractors.default_radius_miles (15mi), so a correct search never
# needs more than the 3x3 neighborhood of a point's own cell -- any point
# within 15mi of another must fall in an adjacent cell, never two cells
# away, when the cell itself is wider than the search radius.
_CONTRACTOR_GRID_DEGREES = 0.5

# Keyed by Session object identity, not a module-level singleton: distinct
# per request/test (each gets its own Session, so no cross-test leakage
# through pytest's fresh-SQLite-per-test fixture) and freed automatically
# via WeakKeyDictionary once that Session is garbage-collected. Avoids
# re-running the same small query (1,110 rows in the real restore) once
# per FeedSignal -- four_part_filter is called per-signal, tens of
# thousands of times per report/page load, and app.contractors' own
# nearby_* fields exist for exactly this reason: "a live per-request
# N x M join does not scale at this row count" applies just as much here.
_contractor_contact_grid_cache: "weakref.WeakKeyDictionary[Session, dict]" = weakref.WeakKeyDictionary()


def _build_contractor_contact_grid(session: Session) -> dict[tuple[int, int], list[tuple[int, float, float]]]:
    """{grid_cell: [(contact_id, contractor_lat, contractor_lon), ...]} for
    every reachable Contact anchored on a geocoded Contractor firm
    (Contact.contractor_id, Block 4B-prep-2 Item 1) -- one query, not one
    per signal."""
    rows = session.exec(
        select(Contact.id, Contractor.latitude, Contractor.longitude)
        .join(Contractor, Contractor.id == Contact.contractor_id)
        .where(Contact.reach_status == "confirmed", Contractor.latitude.is_not(None))
    ).all()
    grid: dict[tuple[int, int], list[tuple[int, float, float]]] = {}
    for contact_id, lat, lon in rows:
        cell = (int(lat // _CONTRACTOR_GRID_DEGREES), int(lon // _CONTRACTOR_GRID_DEGREES))
        grid.setdefault(cell, []).append((contact_id, lat, lon))
    return grid


def _contractor_backed_contact_near(session: Session, lat: float | None, lon: float | None,
                                    radius_miles: float) -> int | None:
    """The nearest reachable Contact anchored on a Contractor firm whose
    geocoded yard sits within radius_miles of (lat, lon) -- real haversine
    distance, same function app.contractors' own building<->contractor
    join uses, not a second drifting implementation. None when the anchor
    point itself has no coordinates, or nothing real is within radius --
    never a guess at which contractor "probably" covers the area."""
    if lat is None or lon is None:
        return None
    grid = _contractor_contact_grid_cache.get(session)
    if grid is None:
        grid = _build_contractor_contact_grid(session)
        _contractor_contact_grid_cache[session] = grid
    cell = (int(lat // _CONTRACTOR_GRID_DEGREES), int(lon // _CONTRACTOR_GRID_DEGREES))
    best_id, best_dist = None, None
    for dlat in (-1, 0, 1):
        for dlon in (-1, 0, 1):
            for contact_id, clat, clon in grid.get((cell[0] + dlat, cell[1] + dlon), ()):
                d = haversine_miles(lat, lon, clat, clon)
                if d <= radius_miles and (best_dist is None or d < best_dist):
                    best_id, best_dist = contact_id, d
    return best_id


def _named_reachable_contact(session: Session, fs: FeedSignal) -> int | None:
    """A Contact row with reach_status='confirmed' (phone or email
    populated -- see Contact's own docstring), reachable one of two ways:

    1. Linked to this signal's Project via ProjectContact -- project-
       sourced signals only (unchanged from Block 3 Item 2).
    2. Block 4B-prep-2 Item 2: anchored on a Contractor firm
       (Contact.contractor_id) whose own geocoded yard sits within
       contractors.default_radius_miles (15mi -- the SAME "realistically
       reachable" radius app.contractors already uses for a building's
       own nearest-mechanical-contractors list, not a second number
       invented here) of the Building or Deadline facility this signal
       anchors on. A mechanical contractor with a yard 15 minutes from a
       building that needs work is a real, callable lead for that
       building even with no per-project link -- exactly the gap Block
       4A Item 5 flagged: the only sources with a building/facility
       anchor (retrofit_building, ab869_plan) never had a contact path at
       all before this.

    hcai_project/opsc_project/field_intel still have neither path (no
    Project link, and no building/facility anchor this function can
    resolve coordinates from) -- a real, disclosed gap, not silently
    worked around."""
    if fs.project_id is not None:
        rows = session.exec(
            select(Contact).join(ProjectContact, ProjectContact.contact_id == Contact.id)
            .where(ProjectContact.project_id == fs.project_id, Contact.reach_status == "confirmed")
        ).all()
        if rows:
            return rows[0].id

    lat = lon = None
    if fs.building_id is not None:
        building = session.get(RetrofitBuilding, fs.building_id)
        if building is not None:
            lat, lon = building.latitude, building.longitude
    elif fs.facility_perm_id is not None:
        hb = session.exec(
            select(HospitalBuilding)
            .where(HospitalBuilding.perm_id == fs.facility_perm_id, HospitalBuilding.latitude.is_not(None))
        ).first()
        if hb is not None:
            lat, lon = hb.latitude, hb.longitude

    radius_miles = default_radius_miles(load_config())
    return _contractor_backed_contact_near(session, lat, lon, radius_miles)


def _sellable_account_or_building(fs: FeedSignal) -> bool:
    """Opportunity's three anchors -- account_id, building_id
    (RetrofitBuilding), facility_perm_id (Ab869Plan, Block 4A Item 1: "an
    Opportunity may anchor on an Account, a Building, or a Deadline
    facility"). account_id needs a real NetSuite-identified Account (never
    built by this module: no Project-to-Account join exists in Scout
    today, see the Item 1 mapping report's Account row) and always passes
    unconditionally -- a known buyer is a known buyer, full stop.

    A building/facility anchor is REPLACEMENT-CLOCK work (an existing
    building, not new construction), and Item 1 gates it on pen_state:
    "sellable account or building passes on a building or facility anchor
    for replacement-clock work, with pen_state required not_moved or
    moving" -- a building whose owner already committed to like-for-like
    (moved) isn't sellable just because Scout knows where it is; that's
    the replacement clock's own window-closed case (Master Plan v3.6
    section 12b: "the window closes when a contractor with an incumbent
    brand relationship is on site")."""
    if fs.account_id is not None:
        return True
    if fs.building_id is not None or fs.facility_perm_id is not None:
        return fs.pen_state in (PenState.not_moved, PenState.moving)
    return False


def _dated_reason(fs: FeedSignal) -> bool:
    return fs.trigger_date is not None


def _eligible_fitting_line_from_equipment_class(session: Session, fs: FeedSignal) -> int | None:
    """Block 4B-prep Item 3: the equipment-class path, for the sources
    _eligible_fitting_line's category-based check above always ABSTAINs
    on (fs.category is None for retrofit_building/ab869_plan/hcai_project/
    opsc_project/field_intel -- none of these carry a Category Scout's
    line card spans). Only retrofit_building has ANY chance of a known
    equipment class today (RetrofitBuilding.equipment_type, permit-
    verified) -- ab869_plan/hcai_project are HCAI-governed facilities with
    NO equipment-class-bearing field at all (see
    app.pipeline.equipment_eligibility's module docstring), so this always
    returns None for those, honestly, not a gap in this function.

    osp_required=True for ab869_plan/hcai_project on principle (hospital
    facilities require OSP) even though no row can exercise it today,
    since neither source has an equipment class to look up in the first
    place."""
    from app.pipeline.equipment_eligibility import (
        EquipmentClass, eligible_lines_for_equipment_class, equipment_class_from_retrofit_type,
    )

    equipment_class = EquipmentClass.unknown
    osp_required = False
    if fs.source == "retrofit_building" and fs.building_id is not None:
        building = session.get(RetrofitBuilding, fs.building_id)
        if building is not None:
            equipment_class = equipment_class_from_retrofit_type(building.equipment_type)
    elif fs.source in ("ab869_plan", "hcai_project"):
        osp_required = True  # HCAI-governed -- see docstring above

    if equipment_class == EquipmentClass.unknown:
        return None
    lines = eligible_lines_for_equipment_class(session, equipment_class, osp_required=osp_required)
    return lines[0].id if lines else None


def _eligible_fitting_line(session: Session, fs: FeedSignal) -> int | None:
    """Eligibility from the OSP register (ProductLine.oshpd_osp) and line
    facets (ProductLine.building_role/category), reusing app.accounts.
    line_offering_by_role -- the SAME eligibility engine the rest of Scout
    already uses, not a second one. Only signals that carry a Category
    Scout's line card actually spans (data_center/industrial/esco -- see
    Category's own docstring) can be checked this way; every other source
    falls through to the equipment-class path below (Block 4B-prep Item 3)
    before finally ABSTAINing."""
    if fs.category is not None:
        from app.accounts import facility_types_by_project, line_offering_by_role
        from app.models import FacilityType

        facility_type = FacilityType.unknown
        if fs.project_id is not None:
            facility_type = facility_types_by_project(session, [fs.project_id]).get(
                fs.project_id, FacilityType.unknown)

        offerings = line_offering_by_role(session, fs.category, facility_type)
        for off in offerings:
            if off.relevant and not off.gap and off.lines:
                return off.lines[0].id
        return None

    return _eligible_fitting_line_from_equipment_class(session, fs)


def resolve_signal_id(session: Session, fs: FeedSignal) -> int | None:
    """A real, ALREADY-EXISTING `signals` table row for this FeedSignal
    (Opportunity.signal_id is not nullable, Item 1) -- read-only, never
    creates one. Only project-sourced FeedSignals have one via the
    existing ProjectSignal link, the same real Signal row
    `_project_signals` above already reads.

    Block 4A Item 1 closes this gap for retrofit_building/ab869_plan (see
    ensure_signal_for_promotion below, which creates a Signal row for
    those two AT PROMOTION TIME rather than expecting one to already
    exist) -- hcai_project/opsc_project/field_intel still have no path to
    a real Signal row at all (out of Item 1's scope, which named only
    "buildings and deadlines"); this function returns None for all four
    non-project sources, on principle, not oversight."""
    if fs.source != "project" or fs.project_id is None:
        return None
    link = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == fs.project_id)
        .order_by(ProjectSignal.linked_at.desc())
    ).first()
    return link.signal_id if link else None


# Sources ensure_signal_for_promotion can CREATE a Signal row for, and the
# SignalType that marks a row as synthesized-at-promotion rather than
# extraction-pipeline output (see app.models.SignalType's own comment).
_PROMOTION_SIGNAL_TYPE = {
    "retrofit_building": SignalType.retrofit_permit_gap,
    "ab869_plan": SignalType.ab869_npc_deadline,
}


def can_promote_signal(fs: FeedSignal) -> bool:
    """Whether promote_to_opportunity has ANY path to a real Signal row
    for this source at all -- project (an existing link may or may not
    actually be there; resolve_signal_id still has to check), or
    retrofit_building/ab869_plan (ensure_signal_for_promotion creates one,
    Block 4A Item 1). False for hcai_project/opsc_project/field_intel,
    which still have no path -- a fifth, separate reason a card's Promote
    button can be disabled, beyond the four-part filter's own four."""
    return fs.source in ("project", "retrofit_building", "ab869_plan")


def ensure_signal_for_promotion(session: Session, fs: FeedSignal) -> int | None:
    """The real `signals` row to attach an Opportunity to -- resolves an
    existing one (project-sourced) or CREATES one (retrofit_building/
    ab869_plan-sourced), per Block 4A Item 1's own words: "a Signal row is
    created for any building or facility the moment it is promoted."
    hcai_project/opsc_project/field_intel still return None (see
    can_promote_signal) -- not this item's scope.

    The created row's pen_state carries over from the FeedSignal's own
    computed value (never re-derived), and its signal_type
    (retrofit_permit_gap/ab869_npc_deadline) marks it as synthesized here,
    never mistaken for extraction-pipeline output -- see
    app.models.SignalType's own comment on these two values."""
    existing = resolve_signal_id(session, fs)
    if existing is not None:
        return existing
    signal_type = _PROMOTION_SIGNAL_TYPE.get(fs.source)
    if signal_type is None:
        return None
    new_signal = SignalRow(
        signal_type=signal_type,
        event_date=fs.trigger_date,
        summary_one_line=fs.evidence[:2000],
        pen_state=fs.pen_state,
    )
    session.add(new_signal)
    session.flush()
    return new_signal.id


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

def promote_to_opportunity(session: Session, fs: FeedSignal, signal_id: int, owner_user: str) -> Opportunity:
    """Creates the Opportunity and its three-row ReasonBlock from a
    FeedSignal that has already passed four_part_filter (callers must
    check first -- this function does not re-check, so it can also be used
    to backfill a ReasonBlock for a manually-created Opportunity in tests).
    signal_id is the real `signals` table row to attach (Opportunity.
    signal_id is not nullable, Item 1) -- FeedSignal itself is never
    persisted, so callers resolve or create that row before calling this.
    owner_user is required, no default (Block 4B-prep Item 2) -- "default
    to the creating user" is the CALLER's job (the authenticated username
    triggering this promotion), never a value this function invents.

    "Why them"/"why now"/"why win" are filled from what this module can
    actually see today -- Block 3 is public data only, no DMG data, no
    pairing/relationship evidence exists yet, so "win" is ABSTAIN for
    every promotion this block ever makes; that is an honest reflection
    of scope, not a bug to route around here."""
    result = four_part_filter(session, fs)
    # Block 4A Item 4: relationship_intro matches TriggerType.relationship_
    # intro exactly (a FieldIntel-sourced signal) -- every other trigger
    # type this module promotes today comes from a real regulatory/board
    # feed, i.e. scout_signal. rep_originated/inbound/inside_sales have no
    # promotion path yet (no manual Opportunity-creation UI exists), so
    # they never get set here -- an honest reflection of what exists, not
    # a gap silently patched.
    origin = Origin.relationship_intro if fs.trigger_type == TriggerType.relationship_intro else Origin.scout_signal
    opp = Opportunity(
        account_id=fs.account_id,
        building_id=fs.building_id,
        facility_perm_id=fs.facility_perm_id,
        contact_id=result.contact_id,
        signal_id=signal_id,
        line_id=result.line_id,
        pen_state=fs.pen_state,
        origin=origin,
        owner_user=owner_user,
    )
    session.add(opp)
    session.flush()

    if fs.account_id is not None:
        them_strength, them_evidence = ReasonStrength.Strong, f"Known account (account_id={fs.account_id})"
    elif fs.building_id is not None:
        them_strength, them_evidence = ReasonStrength.Weak, f"Building identified (building_id={fs.building_id}), owner unknown"
    elif fs.facility_perm_id is not None:
        them_strength, them_evidence = ReasonStrength.Weak, f"Facility identified (perm_id={fs.facility_perm_id}), owner unknown"
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
