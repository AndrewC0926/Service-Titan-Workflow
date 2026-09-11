"""CALL TARGET: who the rep should be talking to on a project, derived by
rule from stored facts alone -- no LLM, no new source, and this never
touches Project.score or Project.window's own ordering.

Six rule-derived types (CallTarget below), evaluated in strict priority
order by `determine_call_target()` -- the FIRST rule whose condition holds
wins, and every result names which rule fired (R1-R6) so a card or a test
can show its own reasoning:

  R1 owner_standards -- developer/owner substring-matches a configured
     standards-owner list (call_target.standards_owners in config.yaml,
     editable without a deploy). Outranks everything else: a standards
     program applies at ANY stage, so it must win even over a design-build
     delivery method or a POST_BOD window that would otherwise route
     elsewhere.
  R2 db_contractor -- delivery_method is literally "design_build" (not
     progressive_design_build, design_assist, or cm_at_risk -- those are
     real, different delivery methods with a different buyer).
  R3 owner -- an existing-building record (Project.category == esco,
     Project.window == OPERATING, or an AB 869 facility, which has no
     "new construction" concept at all) with no engineer_of_record firm
     attached -- "no engineer in the loop" is the literal test, not
     gc/mech_contractor absence.
  R4 engineer -- window is PRE_BOD or IN_BOD: the mechanical basis of
     design hasn't been decided yet, or is being decided right now.
  R5 bidding_contractors -- window is POST_BOD: the spec is locked,
     contractors are choosing among approved equals.
  R6 unknown -- none of the above could decide, with the missing input
     named (never silently blank).

`determine_call_target()` itself is a pure function -- no DB access, no
session -- so the priority order between rules is directly unit-testable
without touching a database. Everything else in this module gathers real
inputs (developer name, delivery_method, window, whether a design-team
firm is attached) from the DB and resolves WHO best answers that target
type, reusing existing joins verbatim: the engineer of record via
ProjectFirm (same role string build_brief already reads), nearby CSLB
C-20/C-38 contractors via the existing 15-mile
app.contractors.nearest_mechanical_contractors join (it only ever reads
.latitude/.longitude off whatever it's given, so a Project or a synthetic
facility point both work unchanged), and financially_responsible_party
for an AB 869 facility.
"""
from __future__ import annotations

import enum
import random
from dataclasses import dataclass

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES, Ab869Plan, Category, Firm, OpscStatusClass, Project, ProjectFirm,
    Window, classify_opsc_status,
)

ENGINEER_OF_RECORD_ROLE = "engineer_of_record"
GC_ROLE = "gc"


class CallTarget(str, enum.Enum):
    engineer = "engineer"
    db_contractor = "db_contractor"
    bidding_contractors = "bidding_contractors"
    owner = "owner"
    owner_standards = "owner_standards"
    unknown = "unknown"
    closed = "closed"   # OPSC-only (WS3.4): the row is terminal, deliberately
                        # no call target -- distinct from `unknown`, which
                        # means Scout couldn't determine one


CALL_TARGET_LABELS = {
    CallTarget.engineer: "Engineer",
    CallTarget.db_contractor: "Design-build contractor",
    CallTarget.bidding_contractors: "Bidding contractors",
    CallTarget.owner: "Owner",
    CallTarget.owner_standards: "Owner (standards program)",
    CallTarget.unknown: "Unknown",
    CallTarget.closed: "Closed (no call target)",
}


@dataclass
class CallTargetResult:
    target: CallTarget
    rule: str        # "R1".."R6"
    reason: str       # one line: why this rule fired
    who_label: str | None = None    # a name, or an explicit "... unknown" string -- never blank
    who_detail: str | None = None   # phone/distance/extra context, when there is any


def _matches_standards_owner(standards_owners: list[str], name: str | None) -> str | None:
    """The configured owner string that matched, or None. Substring,
    case-insensitive, in list order -- config.yaml's own comment documents
    the seed list; editing it needs no deploy."""
    if not name:
        return None
    lname = name.lower()
    for owner in standards_owners:
        if owner and owner.lower() in lname:
            return owner
    return None


def determine_call_target(*, developer_or_owner: str | None, delivery_method: str | None,
                          is_existing_building_record: bool, has_engineer_of_record: bool,
                          window: Window | None, standards_owners: list[str]
                          ) -> tuple[CallTarget, str, str]:
    """R1-R6, in priority order. Pure: no DB access, so this and the
    priority order between rules are directly unit-testable without a
    session. Every caller (Project, an AB 869 facility) normalizes its own
    inputs and calls this same function -- one priority chain, not one
    copy per entity type that could quietly drift apart."""
    match = _matches_standards_owner(standards_owners, developer_or_owner)
    if match:
        return (CallTarget.owner_standards, "R1",
                f"developer/owner matches the standards-owner list ({match!r})")

    if delivery_method == "design_build":
        return CallTarget.db_contractor, "R2", "delivery_method is design_build"

    if is_existing_building_record and not has_engineer_of_record:
        return (CallTarget.owner, "R3",
                "existing-building record with no engineer of record attached")

    if window in (Window.PRE_BOD, Window.IN_BOD):
        return CallTarget.engineer, "R4", f"window is {window.value}"

    if window == Window.POST_BOD:
        return CallTarget.bidding_contractors, "R5", "window is POST_BOD"

    missing = []
    if not developer_or_owner:
        missing.append("developer/owner name (for R1)")
    if delivery_method is None:
        missing.append("delivery_method (for R2)")
    if window is None:
        missing.append("window (for R4/R5)")
    if not missing:
        missing.append("nothing obviously missing, but no rule matched -- see the inputs recorded")
    return CallTarget.unknown, "R6", "no rule fired -- missing: " + ", ".join(missing)


# --- batched lookups (Today/board render many rows; never N+1 per row) -----


def engineer_of_record_by_project(session: Session, project_ids: list[int]) -> dict[int, str | None]:
    """{project_id: firm name} for every project with an engineer_of_record
    ProjectFirm row -- one query for the whole board, same batching
    discipline as app.pipeline.size_score.signal_types_by_project."""
    if not project_ids:
        return {}
    rows = session.exec(
        select(ProjectFirm.project_id, Firm.name)
        .where(ProjectFirm.project_id.in_(project_ids), ProjectFirm.role == ENGINEER_OF_RECORD_ROLE,
              Firm.id == ProjectFirm.firm_id)
    ).all()
    # A project could in principle carry more than one engineer_of_record row
    # (never expected, never enforced not to happen) -- first one wins,
    # deterministically, rather than an arbitrary last-write.
    out: dict[int, str | None] = {}
    for project_id, name in rows:
        out.setdefault(project_id, name)
    return out


def gc_by_project(session: Session, project_ids: list[int]) -> dict[int, str | None]:
    """Same shape as engineer_of_record_by_project, for role == 'gc' --
    the design-build contractor, when one is already on the firm roster."""
    if not project_ids:
        return {}
    rows = session.exec(
        select(ProjectFirm.project_id, Firm.name)
        .where(ProjectFirm.project_id.in_(project_ids), ProjectFirm.role == GC_ROLE,
              Firm.id == ProjectFirm.firm_id)
    ).all()
    out: dict[int, str | None] = {}
    for project_id, name in rows:
        out.setdefault(project_id, name)
    return out


def nearby_contractor_by_project(session: Session, cfg: Config, projects: list[Project]
                                 ) -> dict[int, dict]:
    """{project_id: {contractor_name, contractor_phone, distance_miles}} --
    the SAME bulk join the retrofit board's mobile card view uses
    (app.contractors.nearest_mechanical_contractor_bulk), reused unchanged:
    it only ever reads .latitude/.longitude/.id off whatever list it's
    given, and Project carries all three. One query for the whole board,
    not one 15-mile bounding-box query per project."""
    from app.contractors import default_radius_miles, nearest_mechanical_contractor_bulk

    return nearest_mechanical_contractor_bulk(session, projects, radius_miles=default_radius_miles(cfg))


# --- Project -----------------------------------------------------------


def project_call_target(cfg: Config, project: Project, *, engineer_of_record: str | None,
                        gc: str | None, nearby_contractor: dict | None) -> CallTargetResult:
    """call_target for one Project, given already-looked-up (batched)
    engineer/gc/nearby-contractor inputs -- see the *_by_project batch
    helpers above for how a caller (Today, the board, project_detail)
    should gather those for one project or for a whole page at once."""
    standards_owners = cfg.get("call_target.standards_owners", []) or []
    target, rule, reason = determine_call_target(
        developer_or_owner=project.developer,
        delivery_method=project.delivery_method,
        # esco is always an existing building (see Category.esco's own
        # docstring); OPERATING window is the same fact from the other
        # direction -- the building is already built and no basis-of-
        # design decision is left to influence, so R3's "existing
        # building, owner is the target" applies just as much to an
        # operating data center/industrial project as it does to an esco
        # retrofit. Added 2026-09-06 after the pre-deploy distribution
        # showed 37 OPERATING-window projects falling to R6 unknown for
        # want of this.
        is_existing_building_record=(project.category == Category.esco
                                     or project.window == Window.OPERATING),
        has_engineer_of_record=engineer_of_record is not None,
        window=project.window,
        standards_owners=standards_owners,
    )

    who_label: str | None
    who_detail: str | None = None
    radius = cfg.get("contractors.default_radius_miles", 15)
    if target == CallTarget.owner_standards:
        who_label = project.developer
    elif target == CallTarget.db_contractor:
        who_label = gc or "design-build contractor unknown"
    elif target == CallTarget.owner:
        if nearby_contractor:
            who_label = nearby_contractor["contractor_name"]
            who_detail = f"{nearby_contractor['distance_miles']}mi (CSLB C-20/C-38)"
        else:
            who_label = f"no CSLB mechanical contractor found within {radius:.0f}mi"
    elif target == CallTarget.engineer:
        who_label = engineer_of_record or "engineer of record unknown"
    elif target == CallTarget.bidding_contractors:
        if nearby_contractor:
            who_label = nearby_contractor["contractor_name"]
            who_detail = f"{nearby_contractor['distance_miles']}mi (CSLB C-20/C-38)"
        else:
            who_label = f"no CSLB mechanical contractor found within {radius:.0f}mi"
    else:
        who_label = None

    return CallTargetResult(target, rule, reason, who_label, who_detail)


# --- AB 869 facility -----------------------------------------------------


def ab869_call_target(cfg: Config, plan: Ab869Plan | None, facility_name: str | None,
                      nearby_contractors: list[dict]) -> CallTargetResult:
    """call_target for one AB 869 facility. Always an existing building --
    there is no 'new construction' concept in this population at all, and
    no engineer-of-record equivalent in the schema, so R3 fires for every
    facility R1 doesn't already claim. nearby_contractors is the SAME list
    app.web.main:ab869_facility already computes via
    nearest_mechanical_contractors against the facility's own average
    lat/lon (see that route) -- passed in, not recomputed here."""
    standards_owners = cfg.get("call_target.standards_owners", []) or []
    name_for_match = (plan.owner_name if plan else None) or facility_name
    target, rule, reason = determine_call_target(
        developer_or_owner=name_for_match,
        delivery_method=None,
        is_existing_building_record=True,
        has_engineer_of_record=False,
        window=None,
        standards_owners=standards_owners,
    )

    who_label: str | None
    who_detail: str | None = None
    if target == CallTarget.owner_standards:
        who_label = name_for_match
    elif target == CallTarget.owner:
        frp = plan.financially_responsible_party if plan else None
        if frp:
            who_label = frp
        elif nearby_contractors:
            who_label = nearby_contractors[0]["contractor"].business_name
            who_detail = f"{nearby_contractors[0]['distance_miles']}mi (CSLB C-20/C-38)"
        else:
            radius = cfg.get("contractors.default_radius_miles", 15)
            who_label = f"financially responsible party unknown; no CSLB contractor within {radius:.0f}mi either"
    else:
        who_label = None

    return CallTargetResult(target, rule, reason, who_label)


# --- board-wide distribution / sampling, for the pre-deploy report --------


def call_target_distribution(session: Session, cfg: Config) -> dict:
    """Everything the pre-deploy report needs in one pass: counts by type,
    by (type, window), the unknown breakdown by reason, the
    engineer-of-record-unknown count (the case size for a future HCAI eSP
    engineer-of-record source), and a full per-project result list a
    caller can sample from. One board-wide pass, batched -- see the
    *_by_project helpers above."""
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    project_ids = [p.id for p in projects]
    eor_map = engineer_of_record_by_project(session, project_ids)
    gc_map = gc_by_project(session, project_ids)
    nearby_map = nearby_contractor_by_project(session, cfg, projects)

    results = []
    for p in projects:
        r = project_call_target(cfg, p, engineer_of_record=eor_map.get(p.id),
                                gc=gc_map.get(p.id), nearby_contractor=nearby_map.get(p.id))
        results.append((p, r))

    by_type: dict[str, int] = {}
    by_type_window: dict[tuple[str, str], int] = {}
    unknown_reasons: dict[str, int] = {}
    engineer_unknown = 0
    for p, r in results:
        by_type[r.target.value] = by_type.get(r.target.value, 0) + 1
        by_type_window[(r.target.value, p.window.value)] = by_type_window.get((r.target.value, p.window.value), 0) + 1
        if r.target == CallTarget.unknown:
            unknown_reasons[r.reason] = unknown_reasons.get(r.reason, 0) + 1
        if r.target == CallTarget.engineer and r.who_label == "engineer of record unknown":
            engineer_unknown += 1

    return {
        "n": len(results), "by_type": by_type, "by_type_window": by_type_window,
        "unknown_reasons": unknown_reasons, "engineer_unknown": engineer_unknown,
        "results": results,
    }


def sample_for_review(distribution: dict, n: int = 10, seed: int | None = None) -> list[tuple[Project, CallTargetResult]]:
    """n projects chosen at random from a call_target_distribution() result,
    for a human to sanity-check the rule/who-shown pairing before deploy.
    seed is for tests only -- a real report call omits it."""
    results = distribution["results"]
    rng = random.Random(seed)
    return rng.sample(results, min(n, len(results)))


def opsc_call_target(cfg: Config, district: str | None, status: str | None) -> CallTargetResult:
    """Call target for an OPSC Schools-tab row -- not a Project, so R1-R6's
    Project-shaped inputs (delivery_method, existing-building-ness,
    Window) don't apply; this is its own small rule table, reusing
    CallTarget/CallTargetResult and the SAME standards-owner precedence as
    determine_call_target's own R1.

    R1-equivalent: District matches call_target.standards_owners (the same
    config list determine_call_target already checks -- LAUSD/"Los Angeles
    Unified" is already on it) -> owner_standards, outranking status --
    including a closed application, since a standards program still applies
    at any stage.
    R6-equivalent (WS3.4 fix): Status is "Closed" -> `closed`, terminal, no
    call target. Checked before R5/R4 -- a closed application was
    previously falling into R4 "engineer, spec not yet locked", which is
    wrong: the application is done, there is no spec left to lock.
    R5-equivalent: Status is literally "Funds Released" -> bidding_contractors
    (the spec is locked; contractors are bidding). Otherwise (any earlier
    status, including unknown) -> R4-equivalent engineer (the mechanical
    basis of design is still being decided). There is no OPSC-side
    equivalent of R2/R3 -- a funding record never states a delivery method
    or an existing-building fact, and "status unknown" still falls through
    to engineer rather than a further unknown bucket, since PRE_BOD is the
    honest default for a filed-but-not-yet-funds-released application."""
    standards_owners = cfg.get("call_target.standards_owners") or []
    matched = _matches_standards_owner(standards_owners, district)
    if matched:
        return CallTargetResult(CallTarget.owner_standards, "R1",
                                f"district matches configured standards owner: {matched}",
                                who_label=matched)
    status_class = classify_opsc_status(status)
    if status_class == OpscStatusClass.closed:
        return CallTargetResult(CallTarget.closed, "R6",
                                "status is Closed -- application is terminal, no active call target",
                                who_label="closed, no call target")
    if status_class == OpscStatusClass.funds_released:
        return CallTargetResult(CallTarget.bidding_contractors, "R5", "status is Funds Released",
                                who_label="bidding contractors")
    return CallTargetResult(CallTarget.engineer, "R4",
                            f"status is {status or 'unknown'}, spec not yet locked",
                            who_label="engineer of record unknown")
