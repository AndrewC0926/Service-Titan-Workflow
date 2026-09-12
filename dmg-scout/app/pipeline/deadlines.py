"""Block 3 Item 4 (Master Plan v3.2 section 13): the Deadlines page --
grouped by regulation, nearest date first. Config-driven, no LLM: every
row comes from a field this codebase already computes (Ab869Plan/
HospitalBuilding's NPC rating and deadline year, RetrofitBuilding's
sb1206_trigger_status/ebewe_arcx_next_compliance_date/equipment_age_years),
never re-derived here.

Deliberately NOT built on top of app.pipeline.signals_feed.unified_signals:
that module's FeedSignal shape is oriented around the four-part filter's
six trigger types (Item 2); Deadlines groups the SAME underlying tables a
different way (BY REGULATION), and needs fields (eligible line, OSP
status) that a generic Signal shape has no place for. Each row links to
the real record it comes from -- the existing Ab869 facility / retrofit
building detail pages (unchanged by this item) -- rather than a
re-invented "Signal" concept of its own.
"""
from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from app.models import Ab869Plan, HospitalBuilding, RetrofitBuilding

# HCAI/OSHPD NPC ratings that mean "fully compliant" -- see
# app.pipeline.signals_feed's identical constant and HospitalBuilding's own
# docstring (npc_rating is HCAI's raw code, kept verbatim). N/A/NYA mean
# unknown, not outstanding -- excluded, never counted as a deadline.
_NPC_COMPLIANT = {"5", "5s"}
_NPC_UNKNOWN = {None, "N/A", "NYA"}

# SB 1206's own virgin-refrigerant cutoff -- a fixed regulatory date, not a
# per-building one (every in-scope building faces the same cutoff; what
# varies is whether THIS building is R-410A and old enough to be flagged at
# all, which RetrofitBuilding.sb1206_trigger_status already answers).
SB1206_VIRGIN_CUTOFF = datetime(2030, 1, 1)


@dataclass
class DeadlineRow:
    regulation: str  # "AB 869" | "SB 1206" | "EBEWE" | "Rule 1146.2"
    account_or_building: str
    date: datetime | None  # None = ABSTAIN, sorts last within its regulation
    exposure: str
    eligible_line: str | None
    osp_status: str | None
    detail_url: str
    source_id: str


def _sort_nearest_first(rows: list[DeadlineRow]) -> list[DeadlineRow]:
    return sorted(rows, key=lambda r: (r.date is None, r.date or datetime.max))


def _ab869_osp_facts(session: Session) -> tuple[str, str]:
    """DMG's own OSP eligibility standing -- a fact about the line card,
    not about any one facility, so every AB 869 row shows the SAME
    eligible-line/OSP-status pair (see app.pipeline.hcai.
    hospital_capability_gaps, the existing live OSP-register computation
    this reuses rather than re-deriving)."""
    from app.pipeline.hcai import hospital_capability_gaps

    gaps = hospital_capability_gaps(session)
    covered_labels = [rg["label"] for rg in gaps["role_gaps"] if rg["covered"]]
    eligible_line = ", ".join(covered_labels) if covered_labels else None
    osp_status = f"{gaps['roles_checked'] - gaps['roles_with_gap']} of {gaps['roles_checked']} roles OSP-current"
    return eligible_line, osp_status


def _ab869_rows(session: Session) -> list[DeadlineRow]:
    plans = session.exec(select(Ab869Plan)).all()
    perm_ids = [p.perm_id for p in plans]
    if not perm_ids:
        return []
    buildings = session.exec(
        select(HospitalBuilding).where(HospitalBuilding.perm_id.in_(perm_ids))).all()
    by_perm: dict[str, list[HospitalBuilding]] = {}
    for b in buildings:
        by_perm.setdefault(b.perm_id, []).append(b)

    eligible_line, osp_status = _ab869_osp_facts(session)

    rows = []
    for plan in plans:
        outstanding = [b for b in by_perm.get(plan.perm_id, [])
                       if b.npc_rating not in _NPC_UNKNOWN and b.npc_rating not in _NPC_COMPLIANT]
        if not outstanding:
            continue
        deadline_years = [b.npc_deadline_year for b in outstanding if b.npc_deadline_year]
        row_date = datetime(min(deadline_years), 1, 1) if deadline_years else None
        facility_name = outstanding[0].facility_name
        rows.append(DeadlineRow(
            regulation="AB 869",
            account_or_building=facility_name,
            date=row_date,
            exposure=f"plan status={plan.plan_status or 'unknown'}; "
                     f"NPC outstanding on {len(outstanding)} of {len(by_perm.get(plan.perm_id, []))} building(s)",
            eligible_line=eligible_line,
            osp_status=osp_status,
            detail_url=f"/ab869/{plan.perm_id}",
            source_id=plan.perm_id,
        ))
    return _sort_nearest_first(rows)


def _sb1206_rows(session: Session) -> list[DeadlineRow]:
    buildings = session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.sb1206_trigger_status.is_not(None))).all()
    rows = [
        DeadlineRow(
            regulation="SB 1206",
            account_or_building=b.address or b.apn,
            date=SB1206_VIRGIN_CUTOFF,
            exposure=b.sb1206_detail or b.sb1206_trigger_status,
            eligible_line=None,  # refrigerant-transition exposure, not a line-card question
            osp_status=None,  # SB 1206 is not HCAI-governed -- OSP does not apply
            detail_url=f"/retrofit/building/{b.id}",
            source_id=str(b.id),
        )
        for b in buildings
    ]
    return _sort_nearest_first(rows)


def _ebewe_rows(session: Session) -> list[DeadlineRow]:
    buildings = session.exec(
        select(RetrofitBuilding).where(
            RetrofitBuilding.ebewe_matched == True,  # noqa: E712
            RetrofitBuilding.ebewe_arcx_next_compliance_date.is_not(None),
        )).all()
    rows = [
        DeadlineRow(
            regulation="EBEWE",
            account_or_building=b.address or b.apn,
            date=b.ebewe_arcx_next_compliance_date,
            exposure=("A/RCx filing due this year" if b.ebewe_arcx_due_this_year
                      else "A/RCx filing due, not this year"),
            eligible_line=None,
            osp_status=None,
            detail_url=f"/retrofit/building/{b.id}",
            source_id=str(b.id),
        )
        for b in buildings
    ]
    return _sort_nearest_first(rows)


def _rule_1146_2_rows(session: Session) -> list[DeadlineRow]:
    """SCAQMD Rule 1146.2 (boiler NOx): boilers by age where known, else
    ABSTAIN -- date is always None (this system has no filing/compliance
    DATE for this rule, only an age fact), so every row here sorts last
    among the four regulations by construction (_sort_nearest_first) and
    the exposure text carries the only real information: age when known,
    an explicit ABSTAIN when not."""
    buildings = session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.equipment_type == "boiler")).all()
    rows = []
    for b in buildings:
        age = b.equipment_age_years if b.equipment_age_years is not None else b.building_age_years
        exposure = f"boiler age {age:.0f} years" if age is not None else "ABSTAIN -- age not known"
        rows.append(DeadlineRow(
            regulation="Rule 1146.2",
            account_or_building=b.address or b.apn,
            date=None,
            exposure=exposure,
            eligible_line=None,
            osp_status=None,
            detail_url=f"/retrofit/building/{b.id}",
            source_id=str(b.id),
        ))
    return _sort_nearest_first(rows)


def deadlines_by_regulation(session: Session) -> dict[str, list[DeadlineRow]]:
    return {
        "AB 869": _ab869_rows(session),
        "SB 1206": _sb1206_rows(session),
        "EBEWE": _ebewe_rows(session),
        "Rule 1146.2": _rule_1146_2_rows(session),
    }
