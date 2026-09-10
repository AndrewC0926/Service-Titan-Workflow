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

from sqlalchemy import update as sa_update
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


_BULK_UPDATE_CHUNK = 5000  # see "bulk update" note below


def _bulk_update_in_chunks(session: Session, source: str, natural_keys: list[str], **values) -> None:
    """One UPDATE per chunk of natural_keys, not one per row -- see
    diff_source's docstring for why this exists at all. Chunked (not one
    single `IN (...)` covering the whole list) to keep each statement's
    parameter count well under PostgreSQL's ~65,535 extended-protocol limit
    as tables grow past today's largest (ab802_buildings, 50,259 rows)."""
    for i in range(0, len(natural_keys), _BULK_UPDATE_CHUNK):
        chunk = natural_keys[i:i + _BULK_UPDATE_CHUNK]
        session.execute(
            sa_update(SourceRowSeen)
            .where(SourceRowSeen.source == source, SourceRowSeen.natural_key.in_(chunk))
            .values(**values)
            .execution_options(synchronize_session=False)
        )


def diff_source(session: Session, source: str, current: dict[str, str]) -> DiffResult:
    """Compare `current` (natural_key -> fingerprint, as of right now) against
    what SourceRowSeen last recorded for `source`. Stages every write via
    `session.add`/mutation/bulk UPDATE -- NOT committed here, same "commit
    belongs to the caller" discipline app.pipeline.notify's `_mark` already
    uses, so a caller that wants to inspect the result before deciding to
    persist it can.

    Baseline (no SourceRowSeen row exists yet for this source): every
    current row is seeded, `new`/`changed`/`removed`/`reappeared` are all
    empty on purpose -- there is no prior snapshot to diff against, so
    reporting "everything is new" would be alerting on the table's entire
    existing history in one run, not on what actually happened since
    yesterday. `seeded` still reports the true count either way.

    THE BULK UPDATE, and why it exists: measured directly against
    production 2026-09-09. A first version of this function mutated every
    row's ORM object individually (`row.last_seen_at = now`, etc.) even
    when nothing about the row had changed -- the common case on every
    normal night. SQLAlchemy 2.0 batches bulk INSERT automatically
    ("insertmanyvalues"), so the very first baseline run -- 118,742 rows,
    all inserts -- completed in 74.51s. There is no equivalent automatic
    batching for UPDATE: the very next run, with the exact same 118,742
    rows now all "unchanged," dirtied all 118,742 already-loaded ORM
    objects, and SQLAlchemy's flush emitted one UPDATE per object -- one
    network round trip per row, against Render's Oregon Postgres. Two
    re-runs measured this directly: 9,581.65s and, before being killed
    mid-run for taking even longer, still going after 26+ minutes with 6
    seconds of accumulated CPU time (`ps` confirmed the process was almost
    entirely blocked on I/O, not computing). See app/assumptions.py's
    "Diff stage runtime" entry for the full measured numbers, before and
    after this fix.

    The fix: a row that is genuinely unchanged (same fingerprint, was not
    previously flagged removed) is never mutated as an individual ORM
    object at all. Its natural_key is collected instead, and every
    unchanged row for a table is advanced with ONE chunked bulk UPDATE
    statement (`_bulk_update_in_chunks`) after the per-row loop -- a
    handful of round trips per table instead of one per row. Rows that
    genuinely need individual attention (new, changed, reappeared) are
    still small in number on any real night and stay on the simple
    per-object ORM path. The `removed` case gets the same bulk treatment,
    for the same reason, even though it is usually a small delta -- a
    source file going empty is exactly the case this must not choke on.
    """
    existing = {r.natural_key: r for r in session.exec(
        select(SourceRowSeen).where(SourceRowSeen.source == source)).all()}
    now = utcnow()
    is_baseline = len(existing) == 0

    result = DiffResult(source=source, baseline=is_baseline, seeded=0)
    untouched_unchanged_keys: list[str] = []

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
        fingerprint_changed = row.fingerprint != fingerprint

        if not fingerprint_changed and not was_removed:
            # The common nightly case, and the ONLY case this function
            # deliberately does not touch the ORM object for -- see the
            # bulk-update note above.
            if not is_baseline:
                result.unchanged.append(natural_key)
            untouched_unchanged_keys.append(natural_key)
            continue

        if fingerprint_changed and not is_baseline:
            result.changed.append((natural_key, row.fingerprint, fingerprint))
        if was_removed and not is_baseline:
            result.reappeared.append(natural_key)

        row.fingerprint = fingerprint
        row.last_seen_at = now
        row.removed_at = None
        if fingerprint_changed:
            # Stamped here, never on the bulk unchanged-path above -- see
            # SourceRowSeen.changed_at's own docstring for why last_seen_at
            # alone can't tell a real change from an ordinary reload. Set
            # even on a baseline run's own fingerprint_changed branch, but
            # that branch is unreachable on a baseline (row is always None
            # there, handled above), so this only ever fires on a genuine
            # post-baseline change.
            row.changed_at = now

    if untouched_unchanged_keys:
        _bulk_update_in_chunks(session, source, untouched_unchanged_keys, last_seen_at=now)

    current_keys = set(current)
    removed_keys = [nk for nk, row in existing.items()
                   if nk not in current_keys and row.removed_at is None]
    if removed_keys:
        if not is_baseline:
            result.removed.extend(removed_keys)
        _bulk_update_in_chunks(session, source, removed_keys, removed_at=now)

    return result


def run_all_diffs(session: Session) -> dict[str, DiffResult]:
    """Runs diff_source for every table in SOURCE_ROW_FETCHERS, in a fixed
    order (dict insertion order, Python 3.7+ guarantee) so a log/report
    reads the same table order every night. Stages writes; the caller
    commits (see app.cli.diff_sources_cmd)."""
    return {source: diff_source(session, source, fetcher(session))
           for source, fetcher in SOURCE_ROW_FETCHERS.items()}
