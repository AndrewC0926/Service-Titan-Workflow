"""One-page project brief: what gets carried into a meeting with a VP or handed
to a factory rep. Every number traces to a public URL."""
from __future__ import annotations

from sqlmodel import Session, select

from app.models import (
    Firm, Outreach, Project, ProjectFirm, ProjectSignal, RawDocument, Signal, utcnow,
)


def build_brief(session: Session, project_id: int) -> dict:
    project = session.get(Project, project_id)
    if project is None:
        raise ValueError(f"no project {project_id}")

    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project_id)).all()
    timeline = []
    people = []
    for link in links:
        s = session.get(Signal, link.signal_id)
        if not s:
            continue
        doc = session.get(RawDocument, s.raw_document_id) if s.raw_document_id else None
        timeline.append({
            "date": (s.event_date or s.created_at),
            "type": s.signal_type.value,
            "summary": s.summary_one_line or (doc.title if doc else ""),
            "url": doc.url if doc else None,
            "confidence": s.confidence,
            "match_method": link.match_method,
        })
        people.extend(s.named_people or [])
    timeline.sort(key=lambda t: t["date"], reverse=True)

    firm_rows = session.exec(
        select(ProjectFirm, Firm).where(ProjectFirm.project_id == project_id,
                                        Firm.id == ProjectFirm.firm_id)).all()
    firms = [{"name": f.name, "role": pf.role, "type": f.firm_type} for pf, f in firm_rows]
    eor = next((f["name"] for f in firms if f["role"] == "engineer_of_record"), None)
    gc = next((f["name"] for f in firms if f["role"] == "gc"), None)

    outreach = session.exec(
        select(Outreach).where(Outreach.project_id == project_id)
        .order_by(Outreach.date.desc())).all()

    return {
        "generated_at": utcnow(),
        "project": project,
        "timeline": timeline,
        "people": people,
        "firms": firms,
        "engineer_of_record": eor,
        "gc": gc,
        "outreach": outreach,
    }


def _days_to_bid_cell(p) -> str:
    if p.days_to_estimated_bid is None:
        return "—"
    if p.days_to_estimated_bid_low is not None:
        return f"{p.days_to_estimated_bid} (95% CI {p.days_to_estimated_bid_low}–{p.days_to_estimated_bid_high})"
    return str(p.days_to_estimated_bid)


def brief_markdown(b: dict) -> str:
    p = b["project"]
    tons = (f"{p.tons_estimate_low:,.0f}–{p.tons_estimate_high:,.0f} tons"
            if p.tons_estimate_low else "size unknown")
    if p.estimate_low_confidence:
        tons += " (LOW CONFIDENCE — sized from square footage)"
    lines = [
        f"# {p.name}",
        f"_DMG Scout project brief — generated {b['generated_at']:%Y-%m-%d %H:%M}Z_",
        "",
        f"| | |",
        f"|---|---|",
        f"| Developer | {p.developer or 'unknown'} |",
        f"| Location | {p.county or '?'} County, {p.state or '?'} |",
        f"| Estimated cooling | {tons} |",
        f"| Estimate basis | {p.estimate_basis or 'no size input yet'} |",
        f"| Stage / window | {p.stage.value} / {p.window.value} |",
        f"| Days to estimated bid | {_days_to_bid_cell(p)} |",
        f"| Priority score | {p.score:.2f} |",
        f"| Engineer of record | {b['engineer_of_record'] or 'not yet identified'} |",
        f"| General contractor | {b['gc'] or 'not yet identified'} |",
        f"| Status | {p.status} |",
        "",
        "## Signal timeline",
        "",
    ]
    for t in b["timeline"]:
        url = f" — {t['url']}" if t["url"] else ""
        lines.append(f"- **{t['date']:%Y-%m-%d}** [{t['type']}] {t['summary']}{url}")
    if b["people"]:
        lines += ["", "## Named people", ""]
        for person in b["people"]:
            bits = [person.get("name", "?")]
            if person.get("title"):
                bits.append(person["title"])
            if person.get("org"):
                bits.append(person["org"])
            if person.get("phone"):
                bits.append(person["phone"])
            lines.append("- " + ", ".join(bits))
    if p.notes:
        lines += ["", "## Notes", "", p.notes]
    if b["outreach"]:
        lines += ["", "## Outreach log", ""]
        for o in b["outreach"]:
            lines.append(f"- {o.date:%Y-%m-%d} ({o.channel}) {o.notes}"
                         + (f" → next: {o.next_action}" if o.next_action else ""))
    return "\n".join(lines)
