"""Step 10 deliverables: call-list.csv, top-N briefs, frozen baseline snapshot."""
from __future__ import annotations

import csv
import json
from pathlib import Path

from sqlmodel import Session, select

from app.brief import brief_markdown, build_brief
from app.ladder import best_contact
from app.models import ACTIVE_STATUSES, Project, Window, utcnow


def _pre_bod_in_territory(session: Session) -> list[Project]:
    return list(session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.window == Window.PRE_BOD,
                              Project.in_territory == True)  # noqa: E712
        .order_by(Project.score.desc())).all())


def write_call_list(session: Session, out_path: Path) -> int:
    """Every PRE_BOD in-territory project with its best available contact."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    projects = _pre_bod_in_territory(session)
    with open(out_path, "w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow([
            "rank", "project", "developer", "county", "state", "tons_low", "tons_high",
            "confidence_flag", "estimate_basis", "stage", "days_to_est_bid", "score",
            "contact_name", "contact_title", "contact_company", "ladder_rung",
            "rung_meaning", "contact_source_url",
            # Appended, not inserted, to keep every existing column's position stable
            # for anything already parsing this file positionally rather than by
            # header name. Empty unless the stage has a measured interval (currently
            # only entitlement) -- see Project.days_to_estimated_bid_low's docstring.
            "days_to_est_bid_ci_low", "days_to_est_bid_ci_high",
        ])
        for rank, p in enumerate(projects, 1):
            contact = best_contact(session, p)
            writer.writerow([
                rank, p.name, p.developer or "", p.county or "", p.state or "",
                round(p.tons_estimate_low) if p.tons_estimate_low else "",
                round(p.tons_estimate_high) if p.tons_estimate_high else "",
                "LOW_CONFIDENCE" if p.estimate_low_confidence else "",
                p.estimate_basis or "", p.stage.value,
                p.days_to_estimated_bid if p.days_to_estimated_bid is not None else "",
                p.score,
                contact["name"] if contact else "NO CONTACT AT ANY RUNG",
                (contact.get("title") or "") if contact else "",
                (contact.get("company") or "") if contact else "",
                contact["rung"] if contact else "",
                contact["rung_label"] if contact else "",
                (contact.get("source_url") or "") if contact else "",
                p.days_to_estimated_bid_low if p.days_to_estimated_bid_low is not None else "",
                p.days_to_estimated_bid_high if p.days_to_estimated_bid_high is not None else "",
            ])
    return len(projects)


def write_briefs(session: Session, out_dir: Path, top: int = 10) -> list[str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    written = []
    for p in _pre_bod_in_territory(session)[:top]:
        safe = "".join(c if c.isalnum() or c in " -_" else "" for c in p.name)[:60].strip()
        path = out_dir / f"{p.score:05.2f}-{safe or p.id}.md"
        path.write_text(brief_markdown(build_brief(session, p.id)))
        written.append(str(path))
    return written


def write_baseline(session: Session, out_path: Path) -> dict:
    """Frozen board snapshot so in 90 days the tool's output is measurable
    against reality instead of memory."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES))
        .order_by(Project.score.desc())).all()
    snapshot = {
        "frozen_at": utcnow().isoformat(),
        "n_projects": len(projects),
        "n_pre_bod_in_territory": len(_pre_bod_in_territory(session)),
        "projects": [
            {
                "id": p.id, "name": p.name, "developer": p.developer,
                "county": p.county, "state": p.state,
                "tons_low": p.tons_estimate_low, "tons_high": p.tons_estimate_high,
                "estimate_basis": p.estimate_basis,
                "low_confidence": p.estimate_low_confidence,
                "stage": p.stage.value, "window": p.window.value,
                "score": p.score, "days_to_estimated_bid": p.days_to_estimated_bid,
                "days_to_estimated_bid_low": p.days_to_estimated_bid_low,
                "days_to_estimated_bid_high": p.days_to_estimated_bid_high,
                "in_territory": p.in_territory, "status": p.status,
                "last_signal_at": p.last_signal_at.isoformat() if p.last_signal_at else None,
            }
            for p in projects
        ],
    }
    out_path.write_text(json.dumps(snapshot, indent=2))
    return {"projects": len(projects), "path": str(out_path)}
