"""Nightly diff -- item 1 of docs/DAILY-BRIEF-DESIGN.md, built alone: no
brief, no Opportunity table, no UI change lands with this module.

Five Pipeline B tables have no integer id to hang the existing
`DigestLog`/`notify.py::_detect_changes` new/stage_change/contactable
machinery off of -- every one of their natural keys is a string, several
composite. `SourceRowSeen` (app/models.py) is a small, generic
(source, natural_key) -> fingerprint memory instead, one row per real-world
key ever observed, compared on every run to decide new / changed /
unchanged / removed / reappeared.

`projects` and `signals` are deliberately untouched here -- they already
have the equivalent mechanism (DigestLog, int-keyed, Project-specific
semantics) and reusing it correctly is what section 1b of the design
document argues against: overloading `DigestLog.kind`'s existing
Project-only vocabulary with five more meanings would blur what it already
means, not extend it cleanly.

Fingerprint field choices per table -- registered in app/assumptions.py's
"Nightly diff" group, source_type=MEASURED (each is a verified, disclosed
judgment call about what's alert-worthy, not a guess):

  hcai_projects:      stage, is_mechanical
  ab869_plans:        plan_status, delay_requested
  ab802_buildings:    air_permit_facility_id
  opsc_projects:       status
  scaqmd_facilities:  (none -- see _scaqmd_facilities_rows)

ab869_plans deliberately does NOT fingerprint missed_milestone_count or
next_upcoming_date, despite the original design doc proposing both: those
are aggregate facts computed per facility across Ab869Building/
Ab869Milestone rows in app.pipeline.ab869.ab869_board_rows, not columns
Ab869Plan itself carries. Fingerprinting them here would mean re-running
that whole aggregation once per diffed row, every night, for a value this
module has no cheap way to read directly -- real cost for a benefit this
build didn't measure as needed. Worth a follow-up diff over the board
aggregation specifically, if the daily brief build ever wants it; not
built here.

ab802_buildings' natural key is per (property, year) -- AB 802 is an
annual filing, a new row every year, not a change to last year's. The one
field that legitimately changes on an EXISTING (property, year) row after
its initial load is the AB 869/802 air-permit join
(app.pipeline.scaqmd._link_ab802), which can newly populate
air_permit_facility_id on a re-run without the underlying AB 802 filing
itself changing at all -- that is the one thing worth alerting on for this
table, so it's the only field fingerprinted.

scaqmd_facilities is fingerprinted on nothing: a facility's own
registration doesn't meaningfully change between reloads (see
ScaqmdFacility's own docstring -- this table is facility-notification
grain, not a permit record with fields that move), so only the natural
key's existence is tracked. The fingerprint constant "static" exists so
diff_source's fingerprint-comparison branch is exercised identically to
every other table (never differs, so a row here can be new or removed, but
never "changed") rather than special-cased.
"""
from __future__ import annotations

from dataclasses import dataclass, field

from sqlmodel import Session, select

from app.models import (
    Ab802Building,
    Ab869Plan,
    HcaiProject,
    OpscProject,
    ScaqmdFacility,
    SourceRowSeen,
    utcnow,
)

# ---- per-table fingerprint field lists (documentation + the assumptions ---
# register read this constant directly, so it can never drift from what the
# row-fetchers below actually do)
SOURCE_FINGERPRINT_FIELDS: dict[str, tuple[str, ...]] = {
    "hcai_projects": ("stage", "is_mechanical"),
    "ab869_plans": ("plan_status", "delay_requested"),
    "ab802_buildings": ("air_permit_facility_id",),
    "opsc_projects": ("status",),
    "scaqmd_facilities": (),
}


# ---- per-table natural-key / fingerprint extraction -----------------------

def _hcai_projects_rows(session: Session) -> dict[str, str]:
    rows = session.exec(select(HcaiProject.record_no, HcaiProject.stage, HcaiProject.is_mechanical)).all()
    return {record_no: f"{stage}|{is_mechanical}" for record_no, stage, is_mechanical in rows}


def _ab869_plans_rows(session: Session) -> dict[str, str]:
    rows = session.exec(select(Ab869Plan.perm_id, Ab869Plan.plan_status, Ab869Plan.delay_requested)).all()
    return {perm_id: f"{plan_status}|{delay_requested}" for perm_id, plan_status, delay_requested in rows}


def _ab802_buildings_rows(session: Session) -> dict[str, str]:
    rows = session.exec(select(Ab802Building.portfolio_manager_property_id, Ab802Building.year_ending,
                               Ab802Building.air_permit_facility_id)).all()
    return {f"{pmid}:{year}": f"{air_permit_facility_id}" for pmid, year, air_permit_facility_id in rows}


def _opsc_projects_rows(session: Session) -> dict[str, str]:
    rows = session.exec(select(OpscProject.application_number, OpscProject.status)).all()
    return {application_number: f"{status}" for application_number, status in rows}


def _scaqmd_facilities_rows(session: Session) -> dict[str, str]:
    rows = session.exec(select(ScaqmdFacility.facility_id, ScaqmdFacility.source)).all()
    return {f"{facility_id}:{source}": "static" for facility_id, source in rows}


SOURCE_ROW_FETCHERS = {
    "hcai_projects": _hcai_projects_rows,
    "ab869_plans": _ab869_plans_rows,
    "ab802_buildings": _ab802_buildings_rows,
    "opsc_projects": _opsc_projects_rows,
    "scaqmd_facilities": _scaqmd_facilities_rows,
}


@dataclass
class DiffResult:
    source: str
    baseline: bool
    seeded: int                              # rows newly written to SourceRowSeen this run (baseline or not)
    new: list[str] = field(default_factory=list)
    changed: list[tuple[str, str, str]] = field(default_factory=list)  # (natural_key, old_fp, new_fp)
    unchanged: list[str] = field(default_factory=list)
    removed: list[str] = field(default_factory=list)
    reappeared: list[str] = field(default_factory=list)


def diff_source(session: Session, source: str, current: dict[str, str]) -> DiffResult:
    """Compare `current` (natural_key -> fingerprint, as of right now) against
    what SourceRowSeen last recorded for `source`. Stages every write via
    `session.add`/mutation -- NOT committed here, same "commit belongs to the
    caller" discipline app.pipeline.notify's `_mark` already uses, so a
    caller that wants to inspect the result before deciding to persist it can.

    Baseline (no SourceRowSeen row exists yet for this source): every
    current row is seeded, `new`/`changed`/`removed`/`reappeared` are all
    empty on purpose -- there is no prior snapshot to diff against, so
    reporting "everything is new" would be alerting on the table's entire
    existing history in one run, not on what actually happened since
    yesterday. `seeded` still reports the true count either way.
    """
    existing = {r.natural_key: r for r in session.exec(
        select(SourceRowSeen).where(SourceRowSeen.source == source)).all()}
    now = utcnow()
    is_baseline = len(existing) == 0

    result = DiffResult(source=source, baseline=is_baseline, seeded=0)

    for natural_key, fingerprint in current.items():
        row = existing.get(natural_key)
        if row is None:
            result.seeded += 1
            if not is_baseline:
                result.new.append(natural_key)
            session.add(SourceRowSeen(source=source, natural_key=natural_key, fingerprint=fingerprint,
                                      first_seen_at=now, last_seen_at=now))
            continue

        was_removed = row.removed_at is not None
        if row.fingerprint != fingerprint:
            if not is_baseline:
                result.changed.append((natural_key, row.fingerprint, fingerprint))
        elif not is_baseline:
            result.unchanged.append(natural_key)
        if was_removed and not is_baseline:
            result.reappeared.append(natural_key)

        row.fingerprint = fingerprint
        row.last_seen_at = now
        row.removed_at = None

    current_keys = set(current)
    for natural_key, row in existing.items():
        if natural_key not in current_keys and row.removed_at is None:
            if not is_baseline:
                result.removed.append(natural_key)
            row.removed_at = now

    return result


def run_all_diffs(session: Session) -> dict[str, DiffResult]:
    """Runs diff_source for every table in SOURCE_ROW_FETCHERS, in a fixed
    order (dict insertion order, Python 3.7+ guarantee) so a log/report
    reads the same table order every night. Stages writes; the caller
    commits (see app.cli.diff_sources_cmd)."""
    return {source: diff_source(session, source, fetcher(session))
           for source, fetcher in SOURCE_ROW_FETCHERS.items()}
