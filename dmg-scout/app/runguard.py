"""One stage at a time, enforced against the database rather than the process table.

Three stages have now learned the same lesson. Backfill learned it first ("two
concurrent backfills of one source duplicate every request, race on the
checkpoint table, and make the totals meaningless"). Resolve learned it on
2026-08-06, when two runs overlapped for 3.5 minutes and signal 503 became
projects #961 and #963. Extract had the identical gap and had simply not been
caught yet.

The shape they share: a stage selects its work up front, then spends seconds or
minutes per item on an LLM call, then writes. Two runs whose selection queries
both land before either writes will both do the work. The window is not
microseconds — it is however long the model takes.

**Never decide this with a process check.** A stage can run for minutes between
log lines, so a quiet log looks exactly like a dead process, and `pgrep` saying
"finished" when it has not is precisely how #961/#963 happened. The database
knows what is running; the process table only knows what is scheduled.

This guard is necessary but not sufficient. It can be forced, and a snapshot goes
stale the moment it is taken, so the write path still needs its own last-moment
check (see _new_project) and, where a natural key exists, a unique constraint —
which is the only guarantee that survives everything.
"""
from __future__ import annotations

import logging
from collections.abc import Iterator
from contextlib import contextmanager

from sqlmodel import Session, select

from app.models import SourceRun, utcnow

log = logging.getLogger(__name__)

# A run killed by SIGKILL never sets finished_at and would otherwise wedge its
# stage forever, training everyone to pass --force reflexively — which defeats
# the guard. Real overlap is a minutes-scale problem; older than this is a corpse.
STALE_RUN_HOURS = 12


class ConcurrentStage(RuntimeError):
    """Another run of this stage is still in flight."""


def running_stage(session: Session, stage: str,
                  stale_after_hours: float = STALE_RUN_HOURS) -> SourceRun | None:
    """A *live* unfinished run of `stage`, if one exists."""
    run = session.exec(
        select(SourceRun)
        .where(SourceRun.source == stage, SourceRun.finished_at.is_(None))
        .order_by(SourceRun.id.desc())
    ).first()
    if run is None:
        return None
    age_hours = (utcnow() - run.started_at).total_seconds() / 3600
    if age_hours > stale_after_hours:
        log.warning("ignoring source_run #%d for %s: unfinished but %.1fh old, so it "
                    "was almost certainly killed rather than still running",
                    run.id, stage, age_hours)
        return None
    return run


@contextmanager
def stage_run(session: Session, stage: str, force: bool = False) -> Iterator[SourceRun]:
    """Claim `stage`, refusing if another run holds it. Always releases.

    The release is in a finally block on purpose: a stage that raises must not
    wedge the next run for twelve hours.
    """
    inflight = None if force else running_stage(session, stage)
    if inflight is not None:
        raise ConcurrentStage(
            f"{stage} already running (source_run #{inflight.id}, started "
            f"{inflight.started_at:%Y-%m-%d %H:%M:%S}Z). Wait for it, or pass --force "
            f"if you are sure it is dead. Do NOT use `pgrep` to decide that: a stage "
            f"can run for minutes between log lines, and a process check that says "
            f"'finished' when it has not is how projects #961/#963 happened.")

    run = SourceRun(source=stage)
    session.add(run)
    session.commit()
    try:
        yield run
    finally:
        run.finished_at = utcnow()
        if run.ok is None:
            run.ok = True
        session.add(run)
        session.commit()
