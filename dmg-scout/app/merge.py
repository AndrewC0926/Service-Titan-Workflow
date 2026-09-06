"""Collapse two project rows that are the same project.

The duplicate check (app/duplicates.py) reports fragmentation; this is what fixes
it. They are deliberately separate: reporting is safe and runs after every
resolve, merging destroys a row and does not.

**Retired, not deleted.** The duplicate keeps its id and its history and moves to
status='merged', which is outside ACTIVE_STATUSES, so it drops off every board
and out of every rate without a DELETE. A merge decided on bad evidence is then
reversible, and the row still answers "where did project #963 go".

Both rows record the merge in `notes`, because a project that silently absorbed
another is a project whose signal count nobody can explain later.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    FalsePositiveMark,
    MatchCandidate,
    OutcomeEvent,
    Outreach,
    Project,
    ProjectContact,
    ProjectFirm,
    ProjectSignal,
    StageObservation,
    utcnow,
)

log = logging.getLogger(__name__)

MERGED_STATUS = "merged"

# (model, the columns that must stay unique once project_id is rewritten). A row
# whose rewritten key already exists on the survivor is dropped rather than
# repointed — repointing it would violate the constraint and abort the merge.
_LINK_TABLES = (
    (ProjectSignal, ("signal_id",)),
    (ProjectContact, ("contact_id", "role")),
    (ProjectFirm, ("firm_id", "role")),
    (StageObservation, ("signal_id",)),
)
# No uniqueness to worry about: repoint every row.
_PLAIN_TABLES = (MatchCandidate, Outreach, OutcomeEvent, FalsePositiveMark)

_STAGE_ORDER = ["unknown", "concept", "entitlement", "design", "permitting",
                "procurement", "construction", "operating"]


def choose_survivor(a: Project, b: Project, session: Session) -> tuple[Project, Project]:
    """The row carrying more evidence wins; ties go to the lower id.

    Not "lowest id always": the second row is often the richer one, and keeping
    the sparse row would mean absorbing everything into it for no reason.
    """
    def n_signals(p: Project) -> int:
        return len(session.exec(
            select(ProjectSignal).where(ProjectSignal.project_id == p.id)).all())

    ca, cb = n_signals(a), n_signals(b)
    if ca != cb:
        return (a, b) if ca > cb else (b, a)
    return (a, b) if (a.id or 0) <= (b.id or 0) else (b, a)


def _absorb_project(survivor: Project, dup: Project) -> None:
    """Fill the survivor's gaps from the duplicate. Never overwrite a known value."""
    if dup.name and (not survivor.name or survivor.name.startswith("Unnamed")):
        survivor.name = dup.name
    for field in ("developer", "county", "state", "apn_parcel", "sch_number",
                  "next_action", "next_action_date"):
        if getattr(survivor, field, None) is None or getattr(survivor, field, None) == "":
            setattr(survivor, field, getattr(dup, field, None))
    if survivor.latitude is None and dup.latitude is not None:
        survivor.latitude, survivor.longitude = dup.latitude, dup.longitude
    for field in ("mw_it", "mw_total"):
        d = getattr(dup, field)
        if d and (getattr(survivor, field) or 0) < d:
            setattr(survivor, field, d)
    # Stage only moves forward, same rule resolve uses.
    if _STAGE_ORDER.index(dup.stage.value) > _STAGE_ORDER.index(survivor.stage.value):
        survivor.stage = dup.stage
    if dup.last_signal_at and (survivor.last_signal_at is None
                               or dup.last_signal_at > survivor.last_signal_at):
        survivor.last_signal_at = dup.last_signal_at
    survivor.updated_at = utcnow()


def merge_projects(session: Session, cfg: Config, survivor: Project, dup: Project) -> dict:
    """Fold `dup` into `survivor`. Returns what moved.

    Refuses to merge across categories: a data center and a factory are never the
    same building, and a merge that crosses the boards is a bug report, not a
    cleanup.

    Rescopes run_size_score() to just the survivor at the end, in the same
    transaction as the absorb -- see that function's only_project_ids
    docstring. Before this, the survivor's score/window reflected its
    PRE-merge facts until the next `scout pipeline` run (confirmed real:
    the #963->#961 merge on 2026-08-06 20:38Z sat stale for ~16h20m until
    the following day's 13:00Z run -- see the RATCHET BUG diagnosis).

    Refuses to run at all when SCOUT_VERIFYING_AGAINST_PROD is set -- see
    app.runguard.refuse_if_verifying_against_prod's own docstring.
    """
    from app.runguard import refuse_if_verifying_against_prod
    refuse_if_verifying_against_prod("merge_projects")

    if survivor.id == dup.id:
        raise ValueError("cannot merge a project into itself")
    if survivor.category != dup.category:
        raise ValueError(
            f"refusing to merge across boards: #{survivor.id} is "
            f"{survivor.category.value}, #{dup.id} is {dup.category.value}")
    if dup.status == MERGED_STATUS:
        raise ValueError(f"#{dup.id} is already merged")

    moved: dict[str, int] = {}
    dropped: dict[str, int] = {}

    for model, unique_cols in _LINK_TABLES:
        name = model.__tablename__
        rows = session.exec(select(model).where(model.project_id == dup.id)).all()
        existing = {
            tuple(getattr(r, c) for c in unique_cols)
            for r in session.exec(select(model).where(model.project_id == survivor.id)).all()
        }
        for row in rows:
            key = tuple(getattr(row, c) for c in unique_cols)
            if key in existing:
                # The survivor already has this link; a second copy would violate
                # the unique constraint and take the whole merge down.
                session.delete(row)
                dropped[name] = dropped.get(name, 0) + 1
            else:
                row.project_id = survivor.id
                session.add(row)
                existing.add(key)
                moved[name] = moved.get(name, 0) + 1

    for model in _PLAIN_TABLES:
        name = model.__tablename__
        for row in session.exec(select(model).where(model.project_id == dup.id)).all():
            row.project_id = survivor.id
            session.add(row)
            moved[name] = moved.get(name, 0) + 1

    _absorb_project(survivor, dup)

    stamp = f"{utcnow():%Y-%m-%d %H:%M}Z"
    survivor.notes = (survivor.notes or "") + (
        f"\n[{stamp}] absorbed duplicate project #{dup.id} ({dup.name!r}).")
    dup.notes = (dup.notes or "") + (
        f"\n[{stamp}] merged into project #{survivor.id}; retired, not deleted.")
    dup.status = MERGED_STATUS
    dup.updated_at = utcnow()

    session.add(survivor)
    session.add(dup)

    from app.pipeline.size_score import run_size_score
    run_size_score(session, cfg, only_project_ids=[survivor.id])

    log.info("merged project #%d into #%d: moved %s, dropped %s",
             dup.id, survivor.id, moved or "nothing", dropped or "nothing")
    return {"survivor_id": survivor.id, "merged_id": dup.id,
            "moved": moved, "dropped_as_duplicate_links": dropped}


def merge_duplicate_groups(session: Session, cfg: Config, dry_run: bool = True) -> list[dict]:
    """Collapse every group the duplicate check reports.

    Dry run by default. A group of more than two rows folds into one survivor,
    pairwise.
    """
    from app.duplicates import find_duplicates

    report = find_duplicates(session)
    plans: list[dict] = []
    for label, groups in (("sch", report["by_sch"]),
                          ("name+county", report["by_name_county"])):
        for key, group in groups.items():
            ids = [r["id"] for r in group]
            projects = [session.get(Project, i) for i in ids]
            projects = [p for p in projects if p is not None]
            if len(projects) < 2:
                continue
            survivor = projects[0]
            for other in projects[1:]:
                survivor, loser = choose_survivor(survivor, other, session)
                plan = {"rule": label, "key": key, "survivor_id": survivor.id,
                        "merge_id": loser.id, "name": survivor.name}
                if not dry_run:
                    plan["result"] = merge_projects(session, cfg, survivor, loser)
                plans.append(plan)
    return plans
