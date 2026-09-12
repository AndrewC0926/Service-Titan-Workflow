"""Block 4A Item 4 (Master Plan v3.6 sections 30/32): metric snapshots.

write_snapshot() is the one writer: append-only across days, idempotent
within a day (re-running today's computation replaces today's own rows for
a given metric_key+dimensions pair, never touches a prior day's rows, never
duplicates today's).

run_metric_snapshot() computes every metric this item names and is the
pipeline step registered after `notify` (see app/cli.py's `pipeline`
command and `snapshot-metrics` CLI command). Every metric_key it writes
must exist in metrics.yaml (repo root) -- see tests/test_metrics.py's
test_every_written_metric_key_is_documented, the guard that keeps the two
from drifting apart.

Every metric here reads an EXISTING computed value or an existing table --
nothing is re-derived with new judgment calls: opportunities_by_owner
reports "ABSTAIN" for its only bucket because Opportunity has no owner/
rep field anywhere in Scout today (Item 3's own disclosed gap), not
because this module guesses one.
"""
from __future__ import annotations

from collections import Counter
from datetime import timedelta

from sqlmodel import Session, select

from app.models import (
    DecisionNote, DeliveryMethodClass, MetricSnapshot, Opportunity, OpportunityStage, Outcome,
    PenState, Project, ReasonBlock, ReasonStrength, Signal, SourceRowSeen, utcnow,
)


def write_snapshot(session: Session, *, metric_key: str, value: float, dimensions: dict | None = None,
                   computed_by: str = "pipeline", snapshot_date=None) -> MetricSnapshot:
    """Idempotent per day: deletes any existing row for this exact
    (snapshot_date's calendar day, metric_key, dimensions) before
    inserting -- a second run today replaces today's number instead of
    piling up duplicates. A different day's rows are never touched (the
    delete is scoped to the same day), which is the "append-only" half."""
    dimensions = dimensions or {}
    snapshot_date = snapshot_date or utcnow()
    day_start = snapshot_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)

    existing = session.exec(
        select(MetricSnapshot).where(
            MetricSnapshot.metric_key == metric_key,
            MetricSnapshot.snapshot_date >= day_start,
            MetricSnapshot.snapshot_date < day_end,
        )
    ).all()
    for row in existing:
        if row.dimensions == dimensions:
            session.delete(row)
    session.flush()

    row = MetricSnapshot(snapshot_date=snapshot_date, metric_key=metric_key, dimensions=dimensions,
                        value=value, computed_by=computed_by)
    session.add(row)
    session.flush()
    return row


def _signals_by_trigger_type(session: Session) -> list[dict]:
    from app.pipeline.signals_feed import unified_signals

    counts = Counter(s.trigger_type.value for s in unified_signals(session))
    return [{"metric_key": "signals_by_trigger_type", "dimensions": {"trigger_type": k}, "value": v}
            for k, v in counts.items()]


def _qualified_opportunities(session: Session) -> list[dict]:
    count = session.exec(select(Opportunity)).all().__len__()
    return [{"metric_key": "qualified_opportunities", "dimensions": {}, "value": count}]


def _opportunities_by_stage(session: Session) -> list[dict]:
    opps = session.exec(select(Opportunity)).all()
    counts = Counter(o.stage.value for o in opps)
    return [{"metric_key": "opportunities_by_stage", "dimensions": {"stage": k}, "value": v}
            for k, v in counts.items()]


def _opportunities_by_engine(session: Session) -> list[dict]:
    """"Engine" is section 13's own Pipeline filter term for what this
    codebase elsewhere calls origin (Item 4's own "Origin on Opportunity"
    ask, section 32) -- one concept, not two, so this reads
    Opportunity.origin directly rather than inventing a second field."""
    opps = session.exec(select(Opportunity)).all()
    counts = Counter(o.origin.value for o in opps)
    return [{"metric_key": "opportunities_by_engine", "dimensions": {"engine": k}, "value": v}
            for k, v in counts.items()]


def _opportunities_by_owner(session: Session) -> list[dict]:
    """Disclosed ABSTAIN, not a guess: Opportunity has no owner/assigned-
    rep field anywhere in Scout today (the same gap Item 3's
    three_deals_to_explain() already found) -- every Opportunity falls
    into one ABSTAIN bucket until that field exists."""
    count = session.exec(select(Opportunity)).all().__len__()
    return [{"metric_key": "opportunities_by_owner", "dimensions": {"owner": "ABSTAIN"}, "value": count}]


def _whys_strength_distribution(session: Session) -> list[dict]:
    """Section 30: "whys-strength distribution (3, 2, 1 Strong)." For
    every Opportunity with a complete (3-row) Reason Block, how many of
    its three whys are Strong -- 0 through 3."""
    blocks = session.exec(select(ReasonBlock)).all()
    by_opp: dict[int, list[ReasonStrength]] = {}
    for b in blocks:
        by_opp.setdefault(b.opportunity_id, []).append(b.strength)
    counts = Counter(
        strengths.count(ReasonStrength.Strong) for strengths in by_opp.values() if len(strengths) == 3
    )
    return [{"metric_key": "whys_strength_distribution", "dimensions": {"strong_count": str(k)}, "value": v}
            for k, v in counts.items()]


def _earliness_rate(session: Session) -> list[dict]:
    """Section 12b: "The earliness rate (share of opportunities surfaced
    with pen not moved, weekly) is a KPI on Reports.\""""
    opps = session.exec(select(Opportunity)).all()
    if not opps:
        return [{"metric_key": "earliness_rate", "dimensions": {}, "value": 0.0}]
    not_moved = sum(1 for o in opps if o.pen_state == PenState.not_moved)
    return [{"metric_key": "earliness_rate", "dimensions": {}, "value": not_moved / len(opps)}]


def _deadline_exposure_by_regulation(session: Session) -> list[dict]:
    from app.pipeline.deadlines import deadlines_by_regulation

    groups = deadlines_by_regulation(session)
    return [{"metric_key": "deadline_exposure_by_regulation", "dimensions": {"regulation": reg}, "value": len(rows)}
            for reg, rows in groups.items()]


def _outcomes_logged_per_user(session: Session) -> list[dict]:
    outcomes = session.exec(select(Outcome)).all()
    counts = Counter(o.user for o in outcomes)
    return [{"metric_key": "outcomes_logged_per_user", "dimensions": {"user": k}, "value": v}
            for k, v in counts.items()]


def _notes_per_user(session: Session) -> list[dict]:
    notes = session.exec(select(DecisionNote)).all()
    counts = Counter(n.author for n in notes)
    return [{"metric_key": "notes_per_user", "dimensions": {"user": k}, "value": v}
            for k, v in counts.items()]


def _abstain_rate_delivery_method_class(session: Session) -> list[dict]:
    projects = session.exec(select(Project)).all()
    if not projects:
        return [{"metric_key": "abstain_rate_delivery_method_class", "dimensions": {}, "value": 0.0}]
    abstain = sum(1 for p in projects if p.delivery_method_class == DeliveryMethodClass.ABSTAIN)
    return [{"metric_key": "abstain_rate_delivery_method_class", "dimensions": {},
            "value": abstain / len(projects)}]


def _abstain_rate_pen_state(session: Session) -> list[dict]:
    """Measured against the real `signals` table (the population section
    12b's own words name -- "every Signal... carries pen_state"), not
    Opportunity: most real Signal rows have never been through the
    promotion path that sets a real pen_state (Item 1), so this is
    expected to read very high -- an honest finding, not a bug."""
    signals = session.exec(select(Signal)).all()
    if not signals:
        return [{"metric_key": "abstain_rate_pen_state", "dimensions": {}, "value": 0.0}]
    abstain = sum(1 for s in signals if s.pen_state == PenState.ABSTAIN)
    return [{"metric_key": "abstain_rate_pen_state", "dimensions": {}, "value": abstain / len(signals)}]


def _abstain_rate_contact(session: Session) -> list[dict]:
    """"contact" here is the four-part filter's own named_reachable_
    contact part (Item 2) -- the fraction of unified signals with no
    resolvable Contact, reusing that function directly rather than
    re-deriving the same check a second way."""
    from app.pipeline.signals_feed import _named_reachable_contact, unified_signals

    signals = unified_signals(session)
    if not signals:
        return [{"metric_key": "abstain_rate_contact", "dimensions": {}, "value": 0.0}]
    missing = sum(1 for s in signals if _named_reachable_contact(session, s) is None)
    return [{"metric_key": "abstain_rate_contact", "dimensions": {}, "value": missing / len(signals)}]


def _source_freshness_days(session: Session) -> list[dict]:
    rows = session.exec(select(SourceRowSeen.source, SourceRowSeen.last_seen_at)).all()
    latest_by_source: dict[str, object] = {}
    for source, last_seen_at in rows:
        if source not in latest_by_source or last_seen_at > latest_by_source[source]:
            latest_by_source[source] = last_seen_at
    now = utcnow()
    return [
        {"metric_key": "source_freshness_days", "dimensions": {"source": source},
        "value": (now - last_seen_at).total_seconds() / 86400}
        for source, last_seen_at in latest_by_source.items()
    ]


_METRIC_FUNCTIONS = (
    _signals_by_trigger_type,
    _qualified_opportunities,
    _opportunities_by_stage,
    _opportunities_by_engine,
    _opportunities_by_owner,
    _whys_strength_distribution,
    _earliness_rate,
    _deadline_exposure_by_regulation,
    _outcomes_logged_per_user,
    _notes_per_user,
    _abstain_rate_delivery_method_class,
    _abstain_rate_pen_state,
    _abstain_rate_contact,
    _source_freshness_days,
)


def run_metric_snapshot(session: Session, snapshot_date=None) -> dict:
    """The pipeline step run after `notify` (see app/cli.py). Computes
    every metric this item names and writes one MetricSnapshot row per
    (metric_key, dimensions) pair via write_snapshot(). Returns a summary
    dict (metric_key -> list of {dimensions, value}) for the CLI to echo
    and for tests/the backfill report to read back exactly what was
    written, without a second query."""
    snapshot_date = snapshot_date or utcnow()
    written: dict[str, list[dict]] = {}
    for fn in _METRIC_FUNCTIONS:
        for entry in fn(session):
            write_snapshot(session, metric_key=entry["metric_key"], value=entry["value"],
                          dimensions=entry["dimensions"], snapshot_date=snapshot_date)
            written.setdefault(entry["metric_key"], []).append(
                {"dimensions": entry["dimensions"], "value": entry["value"]})
    return written
