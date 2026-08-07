"""How old is the evidence for a project's stage, and is it still worth believing.

Phase D requires stage age on every board row with anything over twelve months
flagged unverified, and the reason is specific: a stage is a CLAIM ABOUT NOW made
from a document written on a date. "Permitting" read off a 2024 filing does not
mean permitting today — the job may have broken ground, been shelved, or been
built and commissioned by somebody else. The board prints stage next to a bid-date
estimate derived from it, so a stale stage silently produces a confident wrong
date, which is the most expensive kind of wrong this system can be.

The evidence date is the EVENT date where a signal states one, not the date we
happened to fetch it. A CEQAnet notice filed in March 2024 and backfilled last
week is fifteen months old evidence, and dating it from the backfill would reset
every project's clock to whenever the crawler last ran — turning the whole board
green on a day nobody learned anything.

Baseline measured on the live board 2026-08-07, under the ORIGINAL method (dated
from the most recent linked signal of any kind): of 296 active projects, 83 have
evidence under six months old, 73 six to twelve, and 140 over twelve. Nearly half
the board is unverified, which is a fact about public filings rather than a
fault — most of these projects file once at entitlement and are silent for a year.

That original method had a gap: it dated the row from the newest linked signal
regardless of what stage that signal reported. A project sitting at PERMITTING
whose most recent signal is a late-arriving filing that states ENTITLEMENT (an
earlier stage — it does not move project.stage, which only moves forward) still
read as freshly evidenced, because SOME signal touched the row recently. The
freshness was real; it just was not freshness of the claim being displayed. Now
dated from StageObservation, filtered to observations of the CURRENT stage
specifically — see its docstring in app/models.py. Falls back to the original
any-signal method for a project with no matching observation yet (pre-ledger
projects before `scout backfill-stage-observations` has run), so nothing goes
blank in the meantime.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from sqlmodel import Session, select

from app.models import Project, ProjectSignal, Signal, StageObservation, utcnow

# Twelve months, from the charter. Config can lower it; it should not be raised
# without a reason, because the number exists to stop a confident wrong bid date.
DEFAULT_UNVERIFIED_MONTHS = 12


@dataclass
class StageAge:
    observed_at: datetime | None
    days: int | None
    dated_from_event: bool     # False when we fell back to when we saw it

    @property
    def months(self) -> float | None:
        return None if self.days is None else self.days / 30.44

    def unverified(self, threshold_months: int = DEFAULT_UNVERIFIED_MONTHS) -> bool:
        # No evidence date at all is unverified too. "We do not know how old this
        # is" and "this is fresh" must never render the same.
        return self.days is None or self.days > threshold_months * 30.44

    @property
    def label(self) -> str:
        if self.days is None:
            return "—"
        if self.days < 60:
            return f"{self.days}d"
        return f"{self.days / 30.44:.0f}mo"


def stage_ages(session: Session, projects: list[Project],
               now: datetime | None = None) -> dict[int, StageAge]:
    """Evidence age per project: last time the CURRENT stage specifically was
    confirmed, not last time any signal touched the project. See the module
    docstring for why those differ."""
    now = now or utcnow()
    if not projects:
        return {}
    ids = [p.id for p in projects]

    obs_by_project: dict[int, list[StageObservation]] = {}
    for o in session.exec(
            select(StageObservation).where(StageObservation.project_id.in_(ids))).all():
        obs_by_project.setdefault(o.project_id, []).append(o)

    # Fallback source for projects with no observation matching their current
    # stage yet (not backfilled, or a stage set by something other than a
    # signal) — the original any-linked-signal method, kept only as a floor.
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id.in_(ids))).all()
    signal_ids = [l.signal_id for l in links]
    signals = ({s.id: s for s in session.exec(
        select(Signal).where(Signal.id.in_(signal_ids))).all()} if signal_ids else {})
    any_signal_best: dict[int, tuple[datetime, bool]] = {}
    for link in links:
        s = signals.get(link.signal_id)
        if s is None:
            continue
        when, from_event = (s.event_date, True) if s.event_date else (s.created_at, False)
        if when is None:
            continue
        current = any_signal_best.get(link.project_id)
        if current is None or when > current[0]:
            any_signal_best[link.project_id] = (when, from_event)

    out: dict[int, StageAge] = {}
    for p in projects:
        current_stage_obs = [o for o in obs_by_project.get(p.id, []) if o.stage == p.stage]
        if current_stage_obs:
            newest = max(current_stage_obs, key=lambda o: o.observed_at)
            when, from_event = newest.observed_at, newest.from_event
        elif p.id in any_signal_best:
            when, from_event = any_signal_best[p.id]
        elif p.last_signal_at is not None:
            # Falls back to the project's own last_signal_at rather than
            # reporting unknown, but only that far — created_at would date the
            # row from when WE made it, which says nothing about the filing.
            when, from_event = p.last_signal_at, False
        else:
            out[p.id] = StageAge(None, None, False)
            continue
        out[p.id] = StageAge(when, max(0, (now - when).days), from_event)
    return out


def staleness_summary(ages: dict[int, StageAge],
                      threshold_months: int = DEFAULT_UNVERIFIED_MONTHS) -> dict:
    n = len(ages)
    unverified = sum(1 for a in ages.values() if a.unverified(threshold_months))
    dated = [a.days for a in ages.values() if a.days is not None]
    return {
        "n": n,
        "unverified": unverified,
        "pct_unverified": (100 * unverified / n) if n else 0,
        "median_days": sorted(dated)[len(dated) // 2] if dated else None,
        "threshold_months": threshold_months,
    }
