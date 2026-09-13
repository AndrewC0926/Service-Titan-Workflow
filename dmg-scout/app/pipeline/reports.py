"""Block 4B Item 2 (Master Plan v3.6 sections 30/34/38): the Reports page,
reading only from metric_snapshots (the KPI/aggregate numbers -- the "why
leads exist" sample of three live Opportunities is the one deliberate
exception, a live join for illustration, not an aggregate number; see
report_data()'s own docstring).

Every function here is a pure read over MetricSnapshot -- nothing here
ever recomputes a number from a mutable table itself. That is the whole
point of section 30's "Reports reads only from this table": a KPI here
is exactly what the nightly snapshot wrote, not a live re-derivation that
could silently drift from what a past Weekly Brief already reported.
"""
from __future__ import annotations

from datetime import timedelta

from sqlmodel import Session, select

from app.models import MetricSnapshot, Opportunity, OpportunityStage, ReasonBlock, WhyKind, utcnow

WEEKS_OF_TREND = 12
_WHY_ORDER = {WhyKind.them: 0, WhyKind.now: 1, WhyKind.win: 2}


def _week_start(dt) -> "object":
    """Monday 00:00 of dt's week -- the bucket boundary every weekly
    rollup below uses, so a metric written any day of the week lands in
    the same bucket."""
    d = dt.replace(hour=0, minute=0, second=0, microsecond=0)
    return d - timedelta(days=d.weekday())


def _rows(session: Session, metric_key: str, dimensions: dict | None = None) -> list[MetricSnapshot]:
    rows = session.exec(select(MetricSnapshot).where(MetricSnapshot.metric_key == metric_key)).all()
    if dimensions is not None:
        rows = [r for r in rows if r.dimensions == dimensions]
    return rows


def latest_value(session: Session, metric_key: str, dimensions: dict | None = None) -> float | None:
    """The most recent snapshot's value, summed across dimensions when
    none is given (e.g. signals_by_trigger_type's per-trigger rows) --
    None (not 0) when the metric has never been snapshotted at all, so a
    caller can render "not loaded" instead of a false zero."""
    rows = _rows(session, metric_key, dimensions)
    if not rows:
        return None
    if dimensions is not None:
        latest = max(rows, key=lambda r: r.snapshot_date)
        return latest.value
    latest_date = max(r.snapshot_date for r in rows)
    day_start = latest_date.replace(hour=0, minute=0, second=0, microsecond=0)
    day_end = day_start + timedelta(days=1)
    return sum(r.value for r in rows if day_start <= r.snapshot_date < day_end)


def weekly_series(session: Session, metric_key: str, dimensions: dict | None = None,
                  weeks: int = WEEKS_OF_TREND) -> list[float | None]:
    """One value per week, oldest to newest, `weeks` slots -- the LAST
    snapshot recorded within each week (a point-in-time count, never
    summed across days within the week: signals_by_trigger_type's value
    is a running total on the day it was written, not a daily delta).
    A week with no snapshot at all is None, not 0 or carried forward --
    "state plainly when history is shorter than twelve weeks" means the
    caller can see exactly which weeks are real."""
    rows = _rows(session, metric_key, dimensions)
    if not rows:
        return [None] * weeks

    by_week: dict = {}
    for r in rows:
        wk = _week_start(r.snapshot_date)
        by_week.setdefault(wk, []).append(r)

    this_week = _week_start(utcnow())
    series = []
    for i in range(weeks - 1, -1, -1):
        wk = this_week - timedelta(weeks=i)
        wk_rows = by_week.get(wk)
        if not wk_rows:
            series.append(None)
            continue
        if dimensions is not None:
            latest = max(wk_rows, key=lambda r: r.snapshot_date)
            series.append(latest.value)
        else:
            latest_day = max(r.snapshot_date for r in wk_rows).replace(
                hour=0, minute=0, second=0, microsecond=0)
            series.append(sum(r.value for r in wk_rows
                              if latest_day <= r.snapshot_date < latest_day + timedelta(days=1)))
    return series


def weeks_of_real_history(session: Session, metric_key: str) -> int:
    """How many of the last WEEKS_OF_TREND weeks actually have a real
    snapshot -- what "state plainly when history is shorter than that"
    reads off of, rather than counting None slots in a rendered
    sparkline by hand."""
    series = weekly_series(session, metric_key, weeks=WEEKS_OF_TREND) if _rows(session, metric_key) else []
    return sum(1 for v in series if v is not None)


def week_over_week(session: Session, metric_key: str, dimensions: dict | None = None,
                   good: str = "up") -> dict | None:
    """{value, previous, delta, direction, label} comparing the most
    recent week's snapshot to the one before it. None when fewer than
    two real weeks exist -- never a delta against a guessed/zero prior
    value. `good` ("up" or "down") is the CALLER's judgment about which
    direction is favorable for this specific metric (an ABSTAIN rate
    going up is not the same kind of news as earliness rate going up) --
    this function only computes the arithmetic, never the framing."""
    series = weekly_series(session, metric_key, dimensions, weeks=2)
    prev, curr = series[0], series[1]
    if prev is None or curr is None:
        return None
    delta = curr - prev
    if delta == 0:
        direction = "flat"
    elif (delta > 0) == (good == "up"):
        direction = "up"
    else:
        direction = "down"
    sign = "+" if delta >= 0 else ""
    return {"value": curr, "previous": prev, "delta": delta, "direction": direction,
           "label": f"{sign}{delta:g} vs last week"}


def why_leads_exist_sample(session: Session, limit: int = 3) -> list[dict]:
    """The one deliberate exception to "reads only from metric_snapshot":
    a live, illustrative sample of real Opportunities with their real
    Reason Blocks -- section 38's "one lead explained in full" for the
    Weekly Brief, and this page's own "why leads exist" band. Most
    recently created first; an Opportunity's own id ordering, since
    Opportunity has no created_at field of its own to sort by (it is
    stamped onto its Signal instead) -- higher id is later by
    construction (auto-increment, never reused)."""
    opps = session.exec(select(Opportunity).order_by(Opportunity.id.desc()).limit(limit)).all()
    out = []
    for opp in opps:
        blocks = session.exec(
            select(ReasonBlock).where(ReasonBlock.opportunity_id == opp.id)
        ).all()
        blocks.sort(key=lambda b: _WHY_ORDER[b.why_kind])
        out.append({"opportunity": opp, "reason_blocks": blocks})
    return out


def report_data(session: Session) -> dict:
    """Everything the /reports route needs, one call. Every KPI here
    carries its own metrics.yaml key so the template can link "how is
    that computed" per section 34 -- see app/web/main.py's own
    metrics_yaml_url() helper for the exact anchor."""
    pipeline_stages = (OpportunityStage.identified, OpportunityStage.contacted,
                       OpportunityStage.engaged, OpportunityStage.quoted)
    pipeline_count = sum(
        latest_value(session, "opportunities_by_stage", {"stage": s.value}) or 0
        for s in pipeline_stages
    )

    funnel = [
        {"key": "signals_by_trigger_type", "label": "Signals",
         "value": latest_value(session, "signals_by_trigger_type"),
         "wow": week_over_week(session, "signals_by_trigger_type")},
        {"key": "qualified_opportunities", "label": "Qualified",
         "value": latest_value(session, "qualified_opportunities"),
         "wow": week_over_week(session, "qualified_opportunities")},
        {"key": "opportunities_by_stage", "label": "Pipeline", "value": pipeline_count, "wow": None},
    ]

    whys_rows = _rows(session, "whys_strength_distribution")
    latest_day = max((r.snapshot_date for r in whys_rows), default=None)
    whys_strength = []
    if latest_day is not None:
        day_start = latest_day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        by_strong = {r.dimensions.get("strong_count"): r.value for r in whys_rows
                    if day_start <= r.snapshot_date < day_end}
        whys_strength = [{"strong_count": k, "value": by_strong.get(str(k), 0)} for k in (3, 2, 1, 0)]

    kpi_keys_leading = ["earliness_rate", "spec_position_rate", "outcomes_logged_per_user", "notes_per_user"]
    kpis_leading = [
        {"key": k, "value": latest_value(session, k), "wow": week_over_week(session, k)}
        for k in kpi_keys_leading
    ]
    kpis_lagging_not_loaded = ["bookings", "conversion_rate"]

    deadline_rows = [
        {"regulation": reg, "value": latest_value(session, "deadline_exposure_by_regulation", {"regulation": reg}),
         "wow": week_over_week(session, "deadline_exposure_by_regulation", {"regulation": reg})}
        for reg in ("AB 869", "SB 1206", "EBEWE", "Rule 1146.2")
    ]

    abstain_keys = ["abstain_rate_delivery_method_class", "abstain_rate_pen_state", "abstain_rate_contact"]
    data_health_abstain = [{"key": k, "value": latest_value(session, k)} for k in abstain_keys]
    source_freshness_rows = _rows(session, "source_freshness_days")
    latest_fresh_day = max((r.snapshot_date for r in source_freshness_rows), default=None)
    source_freshness = []
    if latest_fresh_day is not None:
        day_start = latest_fresh_day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        source_freshness = [
            {"source": r.dimensions.get("source"), "days": r.value}
            for r in source_freshness_rows if day_start <= r.snapshot_date < day_end
        ]
    outcomes_rows = _rows(session, "outcomes_logged_per_user")
    latest_outcomes_day = max((r.snapshot_date for r in outcomes_rows), default=None)
    outcome_logging_by_user = []
    if latest_outcomes_day is not None:
        day_start = latest_outcomes_day.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)
        outcome_logging_by_user = [
            {"user": r.dimensions.get("user"), "count": r.value}
            for r in outcomes_rows if day_start <= r.snapshot_date < day_end
        ]

    return {
        "funnel": funnel,
        "whys_strength": whys_strength,
        "kpis_leading": kpis_leading,
        "kpis_lagging_not_loaded": kpis_lagging_not_loaded,
        "deadline_exposure": deadline_rows,
        "data_health_abstain": data_health_abstain,
        "source_freshness": source_freshness,
        "outcome_logging_by_user": outcome_logging_by_user,
        "why_leads_exist": why_leads_exist_sample(session),
        "weeks_of_history": {
            k: weeks_of_real_history(session, k)
            for k in ("signals_by_trigger_type", "qualified_opportunities", "earliness_rate",
                      "spec_position_rate", "deadline_exposure_by_regulation")
        },
    }
