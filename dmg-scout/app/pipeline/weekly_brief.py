"""Block 4B Item 3 (Master Plan v3.6 sections 38/40): the Weekly Sales
Intelligence Brief. One page, built from the same snapshot data Reports
reads, plus the week's own Outcome/DecisionNote activity (Reports' own
"why leads exist" exception applies here too -- see app.pipeline.reports'
module docstring).

build_weekly_brief() computes the full content once; archive_weekly_brief()
freezes that exact content into a WeeklyBrief row. Two functions, not one,
so a caller (the web preview route) can render without archiving, and the
CLI command that actually issues a brief always archives what it showed.
"""
from __future__ import annotations

from datetime import datetime, timedelta

from sqlmodel import Session, SQLModel, select

from app.models import (
    Account, Disposition, DecisionNote, Opportunity, Outcome, ProductLine, ReasonBlock, RetrofitBuilding,
    WeeklyBrief, utcnow,
)
from app.pipeline.reason_block import weakest_of, weakest_why_rank
from app.pipeline.reports import report_data, why_leads_exist_sample


def _recent(rows: list, start, end, field: str = "created_at") -> list:
    return [r for r in rows if start <= getattr(r, field) < end]


def _account_or_building_label(session: Session, opp: Opportunity) -> str:
    if opp.account_id is not None:
        account = session.get(Account, opp.account_id)
        return account.name if account else f"account #{opp.account_id}"
    if opp.building_id is not None:
        building = session.get(RetrofitBuilding, opp.building_id)
        return (building.address or building.apn) if building else f"building #{opp.building_id}"
    if opp.facility_perm_id is not None:
        return f"AB 869 facility {opp.facility_perm_id}"
    return "unknown, not guessed"


def top_pipeline_rows_by_weakest_why(session: Session, limit: int = 3) -> list[dict]:
    """The same weakest_why_rank ordering Pipeline's own table sorts by
    (app.web.main:pipeline_index) -- read directly here rather than
    calling the web route, so this has no HTTP/template dependency."""
    opportunities = session.exec(select(Opportunity)).all()
    if not opportunities:
        return []
    blocks = session.exec(
        select(ReasonBlock).where(ReasonBlock.opportunity_id.in_([o.id for o in opportunities]))
    ).all()
    by_opp: dict[int, list] = {}
    for b in blocks:
        by_opp.setdefault(b.opportunity_id, []).append(b)

    rows = []
    for o in opportunities:
        strengths = [b.strength for b in by_opp.get(o.id, [])]
        weakest = weakest_of(strengths) if len(strengths) == 3 else None
        rows.append({
            "opportunity_id": o.id,
            "account_or_building": _account_or_building_label(session, o),
            "weakest_why": weakest.value if weakest else None,
            "sort_key": weakest_why_rank(strengths) if len(strengths) == 3 else (99, 99),
        })
    rows.sort(key=lambda r: r["sort_key"])
    for row in rows:
        del row["sort_key"]
    return rows[:limit]


def new_deadline_exposure(session: Session, window_days: int = 90) -> dict:
    """Buildings/facilities with a real deadline date inside the next
    `window_days` -- section 38's "new deadline exposure entering a
    window." Honest limitation, disclosed rather than hidden: this is
    the CURRENT window's membership, not a diff against last week's
    window (no prior week's own window computation is archived to diff
    against yet -- the first Weekly Brief this runs against has nothing
    to compare to, so "new" and "current" are the same thing today; a
    future brief with a prior WeeklyBrief row to read back could
    tighten this to a real diff)."""
    from app.pipeline.deadlines import deadlines_by_regulation

    now = utcnow()
    horizon = now + timedelta(days=window_days)
    groups = deadlines_by_regulation(session)
    in_window = []
    for regulation, rows in groups.items():
        for row in rows:
            if row.date and now <= row.date < horizon:
                in_window.append({
                    "regulation": regulation, "account_or_building": row.account_or_building,
                    "date": row.date, "exposure": row.exposure, "detail_url": row.detail_url,
                })
    return {"window_days": window_days, "count": len(in_window), "rows": in_window[:10]}


def _recommendation(funnel: list[dict], whys_strength: list[dict], moves_made_count: int) -> list[str]:
    """Three lines, rule-based -- no LLM, same discipline as the do-
    sentence composer (Block 4B-prep-3 Item 4): a plain read of the
    numbers already computed above, never a generated narrative."""
    qualified = next((f["value"] for f in funnel if f["key"] == "qualified_opportunities"), None) or 0
    zero_strong = next((w["value"] for w in whys_strength if w["strong_count"] == 0), 0)
    lines = [f"{qualified:g} Opportunities are live in Pipeline right now."]
    if moves_made_count == 0:
        lines.append("Zero Outcomes or Decision Notes were logged this week -- "
                     "that is the fastest lever before any other number here moves.")
    else:
        lines.append(f"{moves_made_count} Outcome/Note rows were logged this week.")
    if zero_strong:
        lines.append(f"{zero_strong:g} Opportunities have all-ABSTAIN Reason Blocks and need research, not a call.")
    else:
        lines.append("No Opportunity is stuck at all-ABSTAIN this week.")
    return lines[:3]


def build_weekly_brief(session: Session, *, owner_user: str, week_end=None) -> dict:
    """The full, structured content of one Weekly Brief. Pure read --
    never writes anything; see archive_weekly_brief() for the write
    path."""
    week_end = week_end or utcnow()
    week_start = week_end - timedelta(days=7)

    data = report_data(session)

    outcomes_this_week = _recent(session.exec(select(Outcome)).all(), week_start, week_end)
    notes_this_week = _recent(session.exec(select(DecisionNote)).all(), week_start, week_end)
    moves_made = [
        {"kind": "outcome", "at": o.created_at, "user": o.user, "text": f"{o.disposition.value} (opportunity #{o.opportunity_id})"}
        for o in outcomes_this_week
    ] + [
        {"kind": "note", "at": n.created_at, "author": n.author, "text": f"{n.note_type.value}: {n.free_text[:140]}"}
        for n in notes_this_week
    ]
    moves_made.sort(key=lambda m: m["at"], reverse=True)
    moves_made = moves_made[:3]

    moves_next = top_pipeline_rows_by_weakest_why(session, limit=3)

    got_wrong = [
        {"opportunity_id": o.opportunity_id, "user": o.user, "created_at": o.created_at}
        for o in outcomes_this_week if o.disposition == Disposition.bad_number_wrong_contact
    ]

    one_lead_raw = why_leads_exist_sample(session, limit=1)
    one_lead = None
    one_lead_do = None
    if one_lead_raw:
        opp = one_lead_raw[0]["opportunity"]
        blocks = one_lead_raw[0]["reason_blocks"]
        one_lead = {
            "opportunity_id": opp.id,
            "account_or_building": _account_or_building_label(session, opp),
            "reason_blocks": [
                {"why_kind": b.why_kind.value, "strength": b.strength.value, "evidence": b.evidence}
                for b in blocks
            ],
        }
        if blocks:
            one_lead_do = blocks[0].one_sentence

    recommendation = _recommendation(data["funnel"], data["whys_strength"], len(moves_made))

    return {
        "week_start": week_start,
        "week_end": week_end,
        "generated_by": owner_user,
        "generated_at": utcnow(),
        "recommendation": recommendation,
        "funnel": data["funnel"],
        "moves_made": moves_made,
        "moves_made_total_this_week": len(outcomes_this_week) + len(notes_this_week),
        "moves_next": moves_next,
        "new_deadline_exposure": new_deadline_exposure(session),
        "one_lead": one_lead,
        "one_lead_do": one_lead_do,
        "got_wrong": got_wrong,
    }


def _json_safe(obj):
    """Recursively converts a build_weekly_brief() result into plain
    JSON-safe primitives for WeeklyBrief.payload -- a SQLModel row's own
    .model_dump(mode='json') already handles its enums/datetimes
    correctly, so this reuses that rather than hand-rolling a second
    enum/datetime conversion."""
    if isinstance(obj, SQLModel):
        return _json_safe(obj.model_dump(mode="json"))
    if isinstance(obj, datetime):
        return obj.isoformat()
    if isinstance(obj, dict):
        return {k: _json_safe(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_json_safe(v) for v in obj]
    return obj


def archive_weekly_brief(session: Session, *, owner_user: str, week_end=None) -> WeeklyBrief:
    """Builds the brief and freezes it into a new, immutable WeeklyBrief
    row -- never updates an existing row (see that model's own
    docstring: a regeneration is a new archive entry, not an edit)."""
    from app.models import MetricSnapshot

    content = build_weekly_brief(session, owner_user=owner_user, week_end=week_end)
    latest_snapshot = session.exec(
        select(MetricSnapshot).order_by(MetricSnapshot.snapshot_date.desc())
    ).first()
    snapshot_date = latest_snapshot.snapshot_date if latest_snapshot else content["week_end"]

    row = WeeklyBrief(
        snapshot_date=snapshot_date, generated_by=owner_user,
        week_start=content["week_start"], week_end=content["week_end"],
        payload=_json_safe(content),
    )
    session.add(row)
    session.flush()
    return row
