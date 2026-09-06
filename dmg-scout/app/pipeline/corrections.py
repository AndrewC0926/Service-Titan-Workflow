"""RATCHET OVERRIDE: the one path allowed to move Project.stage backward,
shrink Project.mw_it/mw_total, or replace an already-stated
Project.delivery_method -- see the RATCHET BUG diagnosis (2026-09-06) for
why `_absorb()`'s forward-only/max-only/first-wins rules, correct for an
ordinary signal, can otherwise leave a known-wrong fact permanently stuck.

THE PIN. Writing a ManualCorrection for a (project_id, field) pin PINS that
field going forward: a signal whose observed_at (or created_at fallback,
same rule StageObservation.from_event already uses) is at or before
corrected_at is stale evidence the human already had -- it is silently
discarded, exactly like `_absorb()` already silently discards a null value.
A signal observed AFTER corrected_at that would still move the field is NOT
applied automatically either: the forward-only rule alone stops being
sufficient justification once a human has actively overridden the field, so
it is queued as a PinnedFieldConflict instead -- confirm or reject, never
silently either way. `_absorb()` (app.pipeline.resolve) and `best()`
(app.pipeline.size_score.run_size_score, its own closure) both consult
`latest_correction()` before applying their existing rule; an UN-pinned
field's code path is untouched.

Nothing here ever writes to Project except `apply_manual_correction` --
the review queue this module also exposes (`stage_regression_candidates`)
is read-only, surfaced for a human to act on through that one function, an
auto-fix is never fired.
"""
from __future__ import annotations

from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.models import (
    ACTIVE_STATUSES,
    ManualCorrection,
    PinnedFieldConflict,
    Project,
    Signal,
    Stage,
    StageObservation,
    utcnow,
)

STAGE_ORDER = ["unknown", "concept", "entitlement", "design", "permitting",
               "procurement", "construction", "operating"]

# What a caller may correct, and how to turn its posted string into the
# real stored value. Deliberately the same four fields _absorb() treats
# specially (stage forward-only, mw_it/mw_total max-only, delivery_method
# first-wins) -- every other Project column is filled from a signal
# unconditionally and has no ratchet to override in the first place.
FIELD_CONVERTERS = {
    "stage": lambda v: Stage(v),
    "mw_it": lambda v: float(v),
    "mw_total": lambda v: float(v),
    "delivery_method": lambda v: v,
}


def _display(value) -> str:
    return value.value if isinstance(value, Stage) else str(value)


def latest_correction(session: Session, project_id: int, field: str) -> ManualCorrection | None:
    """The active pin for (project_id, field), if any -- the most recent
    ManualCorrection row, since a field can be corrected more than once and
    only the latest one governs."""
    return session.exec(
        select(ManualCorrection)
        .where(ManualCorrection.project_id == project_id, ManualCorrection.field == field)
        .order_by(ManualCorrection.corrected_at.desc())
    ).first()


def predates_pin(observed_at: datetime | None, correction: ManualCorrection) -> bool:
    """True when evidence dated `observed_at` was already available (or
    contemporaneous) at the moment of the correction -- stale, silently
    discarded, same as _absorb() already discards a null value."""
    return observed_at is not None and observed_at <= correction.corrected_at


def queue_pin_conflict(session: Session, project: Project, field: str, signal: Signal,
                       correction: ManualCorrection, candidate_value: str) -> None:
    """Surface a post-pin, forward-moving (or, for delivery_method,
    differing) signal for a human to confirm or reject -- never applied
    automatically. Deduped on (project_id, field, signal_id): re-resolving
    an already-linked signal (concurrent run, backfill) must not double-
    queue the same conflict, same discipline StageObservation's own
    uq_stage_observation_signal enforces for the ordinary case."""
    exists = session.exec(
        select(PinnedFieldConflict).where(
            PinnedFieldConflict.project_id == project.id,
            PinnedFieldConflict.field == field,
            PinnedFieldConflict.signal_id == signal.id,
        )
    ).first()
    if exists:
        return
    session.add(PinnedFieldConflict(
        project_id=project.id, field=field, signal_id=signal.id,
        correction_id=correction.id, pinned_value=correction.new_value,
        candidate_value=candidate_value,
    ))


def apply_manual_correction(session: Session, cfg: Config, project_id: int, field: str,
                            new_value: str, reason: str, corrected_by: str) -> ManualCorrection:
    """The only path allowed to move stage backward, shrink mw_it/mw_total,
    or replace an already-stated delivery_method. Five steps, one
    transaction:

      1. Read old_value off the LIVE Project row, inside this transaction --
         never accepted from the caller/form. A claimed old_value could be
         stale (the page was loaded a while ago) or simply wrong; this
         table exists specifically to be a trustworthy record, so the one
         fact it must never get from outside is the very fact it is meant
         to prove.
      2. Insert the ManualCorrection row (this IS the pin from this instant
         on -- see latest_correction()).
      3. Write new_value onto Project.<field> directly -- bypassing
         _absorb()/_absorb_project() entirely; this function is their only
         sibling allowed to move a field backward.
      4. For field == 'stage': also insert a StageObservation
         (source='manual', signal_id=None) so the ledger stays complete --
         a correction is a real observation, same as a signal's.
      5. Rescore, scoped to just this project, in the SAME transaction --
         same run_size_score(only_project_ids=...) pattern as every other
         fact-writing path fixed in the RATCHET BUG pass.

    Refuses to run when SCOUT_VERIFYING_AGAINST_PROD is set -- same guard
    as every other write path that can trigger a rescore (see
    app.runguard.refuse_if_verifying_against_prod).
    """
    from app.pipeline.size_score import run_size_score
    from app.runguard import refuse_if_verifying_against_prod

    refuse_if_verifying_against_prod("apply_manual_correction")

    if field not in FIELD_CONVERTERS:
        raise ValueError(f"not a correctable field: {field!r}")
    reason = (reason or "").strip()
    corrected_by = (corrected_by or "").strip()
    if not reason:
        raise ValueError("a manual correction requires a reason")
    if not corrected_by:
        raise ValueError("a manual correction requires who made it")

    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no such project: #{project_id}")

    old_value = getattr(project, field)
    old_value_str = _display(old_value) if old_value is not None else None
    converted = FIELD_CONVERTERS[field](new_value)

    corrected_at = utcnow()
    correction = ManualCorrection(
        project_id=project_id, field=field, old_value=old_value_str,
        new_value=_display(converted), reason=reason, corrected_by=corrected_by,
        corrected_at=corrected_at,
    )
    session.add(correction)
    session.flush()  # correction.id populated for PinnedFieldConflict FKs and the return value

    setattr(project, field, converted)
    project.updated_at = corrected_at

    if field == "stage":
        session.add(StageObservation(
            project_id=project.id, stage=converted, observed_at=corrected_at,
            from_event=False, signal_id=None, source="manual",
        ))

    session.add(project)
    run_size_score(session, cfg, only_project_ids=[project_id])
    return correction


def stage_regression_candidates(session: Session) -> list[dict]:
    """Active projects whose most recent StageObservation names an EARLIER
    stage than Project.stage currently shows -- exactly the shape the
    forward-only ratchet can never self-correct, surfaced for a human to
    review. Read-only: never writes, never auto-corrects. See the RATCHET
    BUG diagnosis (2026-09-06) -- 11 production projects matched this shape
    the day it was found.

    A project already carrying an active 'stage' pin is excluded: a human
    has already looked at it, and re-surfacing it here would just be
    re-litigating a decision that's already recorded (see latest_correction).
    """
    projects = session.exec(select(Project).where(Project.status.in_(ACTIVE_STATUSES))).all()
    out = []
    for p in projects:
        if latest_correction(session, p.id, "stage") is not None:
            continue
        obs = session.exec(
            select(StageObservation).where(StageObservation.project_id == p.id)
            .order_by(StageObservation.observed_at.desc())
        ).first()
        if obs is None:
            continue
        if STAGE_ORDER.index(obs.stage.value) < STAGE_ORDER.index(p.stage.value):
            out.append({
                "project": p,
                "last_observed_stage": obs.stage,
                "last_observed_at": obs.observed_at,
            })
    return out
