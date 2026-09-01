"""Human-sourced project intelligence -- what a GC, engineer, or owner
told a rep in conversation, before any of it is a public document. See
app.models.FieldIntel's own docstring for why this is a genuinely
separate pipeline from Project/Signal, not a variant of either: nothing
here is grounded against a document, nothing here runs through
app.pipeline.resolve/size_score/grounding, and nothing here is ever
silently merged into a Project a later filing creates.

The one join this module DOES share with the rest of the app: engineer_name
and mech_contractor_name are resolved, once, at creation, against the SAME
Firm/Account rosters app.firms.match_firm and the account-roster joins
already use -- exact normalized match only, same "never guess" discipline
as every other join built this session.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, func, select

from app.firms import match_firm
from app.models import (
    ACTIVE_STATUSES,
    Account,
    FieldIntel,
    Project,
    ProjectFirm,
    utcnow,
)
from app.normalize import normalize_name


def _match_account(session: Session, name: str) -> Account | None:
    """Same exact-normalized-name discipline app.firms.match_firm already
    uses for Firm -- no fuzzy/partial matching, and Account carries no
    alias list to check, so a name either reduces to an existing account
    or it doesn't."""
    norm = normalize_name(name)
    if not norm:
        return None
    return session.exec(select(Account).where(Account.name_norm == norm)).first()


def resolve_named_entity(session: Session, name: str | None) -> dict:
    """{'firm': Firm | None, 'account': Account | None} -- checked against
    BOTH rosters for whichever name is passed (engineer or mechanical
    sub): app.accounts.ACCOUNT_TYPES includes 'engineer', so an engineer
    can genuinely be a sales account too, not only ever a Firm. Exact
    normalized match only, same as accounts_matching_firm/the CSLB join/
    search_firms' own roster -- never partial, never fuzzy."""
    if not name or not name.strip():
        return {"firm": None, "account": None}
    return {"firm": match_firm(session, name), "account": _match_account(session, name)}


def create_field_intel(session: Session, *, reported_by: str, reported_at: datetime,
                       source_notes: str, owner: str | None = None, location: str | None = None,
                       size_scope: str | None = None, stage: str = "unknown",
                       expected_timing: str | None = None, engineer_name: str | None = None,
                       mech_contractor_name: str | None = None) -> FieldIntel:
    """The one writer. reported_by/reported_at/source_notes are the only
    required fields -- this record's whole basis is a person's word, so
    those three ARE the source, the same way a URL is for everything else
    in this schema. Every other field stays exactly what was passed
    (None/empty if the person didn't address it) -- never defaulted to a
    guess.

    Resolves engineer_name/mech_contractor_name against the Firm/Account
    rosters ONCE, here, at creation -- a firm added to the roster later
    does not retroactively re-resolve an existing record, same one-time-
    only discipline app.accounts.create_account's own auto-link already
    has."""
    if not reported_by or not reported_by.strip():
        raise ValueError("reported_by is required -- who told you this")
    if not source_notes or not source_notes.strip():
        raise ValueError("source_notes is required -- what they said")

    engineer = resolve_named_entity(session, engineer_name)
    mech = resolve_named_entity(session, mech_contractor_name)

    intel = FieldIntel(
        reported_by=reported_by.strip(),
        reported_at=reported_at,
        source_notes=source_notes.strip(),
        owner=owner or None,
        location=location or None,
        size_scope=size_scope or None,
        stage=stage or "unknown",
        expected_timing=expected_timing or None,
        engineer_name=engineer_name or None,
        engineer_firm_id=engineer["firm"].id if engineer["firm"] else None,
        engineer_account_id=engineer["account"].id if engineer["account"] else None,
        mech_contractor_name=mech_contractor_name or None,
        mech_contractor_firm_id=mech["firm"].id if mech["firm"] else None,
        mech_contractor_account_id=mech["account"].id if mech["account"] else None,
    )
    session.add(intel)
    session.commit()
    session.refresh(intel)
    return intel


def firm_active_projects(session: Session, firm_id: int) -> list[dict]:
    """What else this firm is already on -- the reverse question a rep
    actually has once a name resolves: not "is this firm real" but "what
    is DMG's own history with them." Same shape as
    app.accounts.accounts_matching_firm's own active_projects, reused here
    for an engineer/mechanical-sub firm rather than an account's firm_id."""
    links = session.exec(
        select(ProjectFirm, Project).where(
            ProjectFirm.firm_id == firm_id, Project.id == ProjectFirm.project_id,
            Project.status.in_(ACTIVE_STATUSES))
    ).all()
    return [{"project": p, "role": pf.role, "stage": p.stage} for pf, p in links]


def confirmation_candidates(session: Session, intel: FieldIntel) -> list[Project]:
    """Active public-record projects that MIGHT be the same job this
    record describes -- surfaced for a human to confirm or dismiss, NEVER
    auto-linked (see FieldIntel's own docstring on why a silent merge is
    exactly what this table exists to avoid). Two independent signals,
    either one surfaces a candidate:
      - the owner name normalizes the same as the project's own developer
        field (same exact-match discipline as
        app.accounts.accounts_matching_projects_by_owner_name)
      - the SAME resolved engineer or mechanical-sub firm is linked to
        the project via ProjectFirm -- real corroboration even when the
        owner name doesn't line up (a filing often names a legal entity,
        not the GC's own casual name for the owner).
    Already-confirmed records return no candidates -- a project this
    record is already linked to is the answer, not a candidate."""
    if intel.confirmed_project_id is not None:
        return []
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    candidates: dict[int, Project] = {}

    owner_norm = normalize_name(intel.owner) if intel.owner else None
    if owner_norm:
        for p in projects:
            if p.developer and normalize_name(p.developer) == owner_norm:
                candidates[p.id] = p

    firm_ids = {fid for fid in (intel.engineer_firm_id, intel.mech_contractor_firm_id) if fid}
    if firm_ids:
        links = session.exec(
            select(ProjectFirm, Project).where(
                ProjectFirm.firm_id.in_(firm_ids), Project.id == ProjectFirm.project_id,
                Project.status.in_(ACTIVE_STATUSES))
        ).all()
        for _pf, p in links:
            candidates[p.id] = p

    return list(candidates.values())


def confirm_field_intel(session: Session, intel_id: int, project_id: int, confirmed_by: str) -> FieldIntel:
    """The ONLY way confirmed_project_id is ever set: an explicit human
    call naming which project this turned out to be -- never inferred
    from confirmation_candidates automatically. A project a later filing
    creates is confirmation this record was right, not a duplicate to
    merge into it: both rows survive unchanged except this one gaining a
    pointer and a status flip."""
    intel = session.get(FieldIntel, intel_id)
    if intel is None:
        raise ValueError(f"no field intel #{intel_id}")
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no project #{project_id}")
    intel.confirmed_project_id = project_id
    intel.confirmed_at = utcnow()
    intel.confirmed_by = confirmed_by
    intel.status = "confirmed"
    intel.updated_at = utcnow()
    session.add(intel)
    session.commit()
    session.refresh(intel)
    return intel


def field_intel_activity(session: Session) -> dict:
    """Total records, the most recent one's date, and how many landed in
    the trailing 30 days -- a plain activity counter, not a source-health
    check. This table has no scheduled run to go stale: it only ever
    updates when a rep types something in after a conversation, so a quiet
    week means nobody talked to a GC, not a broken pipeline. Deliberately
    NOT registered under config.yaml's sources: block and NOT wired into
    app.ops.doctor/stale_cutoff -- a staleness banner here would fire on
    every week with no conversation to log, which is normal, and would
    train the same "red banner" signal that means something real broke
    everywhere else on the board to mean nothing here."""
    total = session.exec(select(func.count(FieldIntel.id))).one()
    most_recent = session.exec(select(func.max(FieldIntel.reported_at))).one()
    cutoff = utcnow() - timedelta(days=30)
    last_30 = session.exec(
        select(func.count(FieldIntel.id)).where(FieldIntel.reported_at >= cutoff)
    ).one()
    return {"total": total, "most_recent": most_recent, "last_30_days": last_30}


def active_field_intel(session: Session) -> list[FieldIntel]:
    """Unconfirmed, not stale -- what the board section and the digest
    both list from. Newest reported_at first: a rep cares about the most
    recent conversation, not the oldest open one."""
    return session.exec(
        select(FieldIntel).where(FieldIntel.status == "active")
        .order_by(FieldIntel.reported_at.desc())
    ).all()
