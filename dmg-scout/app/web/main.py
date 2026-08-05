"""Dashboard: FastAPI + Jinja2 + HTMX behind HTTP basic auth. Reads like an
engineering submittal package: title block, dense tables, monospace numbers,
thermal gradient on the score column."""
from __future__ import annotations

import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, Form, HTTPException, Request, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.config import load_config
from app.db import get_session
from app.manual import add_manual_signal
from app.models import (
    ACTIVE_STATUSES, OUTCOME_STATUSES, Category, Contact, Firm, MatchCandidate, Outreach,
    Project, ProjectContact, ProjectFirm, ProjectSignal, RawDocument, Signal, SignalType,
    SourceRun, Stage, utcnow,
)


def _parse_category(value: str | None) -> Category | None:
    """None means every category — an unknown value falls back to that rather than
    silently showing an empty board."""
    if not value or value == "all":
        return None
    try:
        return Category(value)
    except ValueError:
        return None
from app.normalize import normalize_name
from app.pipeline.resolve import apply_review_decision

app = FastAPI(title="DMG Scout")
security = HTTPBasic()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def score_color(score: float) -> str:
    """Cold-to-hot thermal gradient: 0 = deep blue, 1+ = red-hot."""
    t = max(0.0, min(1.0, score))
    hue = 215 - t * 215  # 215 (cold blue) -> 0 (hot red)
    return f"hsl({hue:.0f} 85% 55%)"


templates.env.globals["score_color"] = score_color
templates.env.globals["now"] = utcnow


def auth(credentials: HTTPBasicCredentials = Depends(security)) -> str:
    cfg = load_config()
    user = cfg.get("dashboard.basic_auth_username", "andrew")
    password = os.environ.get(cfg.get("dashboard.basic_auth_password_env", "DASHBOARD_PASSWORD"), "")
    if not password:
        # Fail closed: no password configured means no access, and say why.
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="DASHBOARD_PASSWORD env var is not set")
    ok = secrets.compare_digest(credentials.username, user) and secrets.compare_digest(
        credentials.password, password)
    if not ok:
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            headers={"WWW-Authenticate": "Basic"})
    return credentials.username


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True}


def _title_block(session: Session) -> dict:
    return {
        "record_count": session.exec(select(func.count(Project.id))).one(),
        "signal_count": session.exec(select(func.count(Signal.id))).one(),
        "doc_count": session.exec(select(func.count(RawDocument.id))).one(),
        "data_as_of": session.exec(select(func.max(RawDocument.fetched_at))).one(),
    }


@app.get("/", response_class=HTMLResponse)
def board(request: Request, category: str = "data_center",
          session: Session = Depends(get_session), _: str = Depends(auth)):
    # Two boards, one pipeline. Defaults to data centers: that is the book of
    # business this system was built for, and industrial should never silently
    # dilute it. `?category=all` shows both.
    cat = _parse_category(category)
    q = select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == True)  # noqa: E712
    if cat is not None:
        q = q.where(Project.category == cat)
    projects = session.exec(q.order_by(Project.score.desc())).all()
    counts = {
        c.value: session.exec(
            select(func.count(Project.id)).where(
                Project.status.in_(ACTIVE_STATUSES),
                Project.in_territory == True,  # noqa: E712
                Project.category == c)).one()
        for c in (Category.data_center, Category.industrial)
    }
    watch_count = session.exec(
        select(func.count(Project.id)).where(Project.status.in_(ACTIVE_STATUSES),
                                             Project.in_territory == False)).one()  # noqa: E712
    days_since = {}
    for p in projects:
        days_since[p.id] = (utcnow() - p.last_signal_at).days if p.last_signal_at else None
    review_count = session.exec(
        select(func.count(MatchCandidate.id)).where(MatchCandidate.status == "pending")).one()
    # No PRE_BOD rows means the system has stopped doing its actual job — say so.
    has_pre_bod = any(p.window.value == "PRE_BOD" for p in projects)
    from app.coverage import pipeline_completeness
    completeness = pipeline_completeness(session, load_config())
    return templates.TemplateResponse(request, "board.html", {
        "projects": projects, "days_since": days_since, "review_count": review_count,
        "has_pre_bod": has_pre_bod, "watch_count": watch_count, "is_watchlist": False,
        "completeness": completeness, "category": category, "cat_counts": counts,
        "tb": _title_block(session), "active": "board",
    })


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """Out-of-territory projects — checked deliberately, never crowding the board."""
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == False)  # noqa: E712
        .order_by(Project.score.desc())
    ).all()
    days_since = {p.id: (utcnow() - p.last_signal_at).days if p.last_signal_at else None
                  for p in projects}
    return templates.TemplateResponse(request, "board.html", {
        "projects": projects, "days_since": days_since, "review_count": 0,
        "has_pre_bod": True, "watch_count": 0, "is_watchlist": True,
        "tb": _title_block(session), "active": "watchlist",
    })


@app.get("/project/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: int, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project_id)).all()
    roster_links = session.exec(
        select(ProjectFirm, Firm).where(ProjectFirm.project_id == project_id,
                                        Firm.id == ProjectFirm.firm_id)).all()
    resolved_firms = [{"name": f.name, "role": pf.role, "type": f.firm_type,
                       "from_roster": f.added_from == "roster"} for pf, f in roster_links]
    timeline = []
    people, firms = [], []
    for link in links:
        s = session.get(Signal, link.signal_id)
        if not s:
            continue
        doc = session.get(RawDocument, s.raw_document_id) if s.raw_document_id else None
        timeline.append({"signal": s, "doc": doc, "link": link})
        people.extend(s.named_people or [])
        firms.extend(s.named_firms or [])
    timeline.sort(key=lambda t: t["signal"].event_date or t["signal"].created_at, reverse=True)
    outreach = session.exec(
        select(Outreach).where(Outreach.project_id == project_id)
        .order_by(Outreach.date.desc())).all()
    contacts = session.exec(
        select(ProjectContact, Contact)
        .where(ProjectContact.project_id == project_id, Contact.id == ProjectContact.contact_id)
    ).all()
    from app.ladder import build_ladder
    ladder = build_ladder(session, project)
    return templates.TemplateResponse(request, "project.html", {
        "p": project, "timeline": timeline, "people": people, "firms": firms,
        "resolved_firms": resolved_firms, "outcome_statuses": OUTCOME_STATUSES,
        "ladder": ladder,
        "outreach": outreach, "contacts": contacts,
        "tb": _title_block(session), "active": "board",
    })


@app.post("/project/{project_id}/notes")
def save_notes(project_id: int, notes: str = Form(""), next_action: str = Form(""),
               session: Session = Depends(get_session), _: str = Depends(auth)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    project.notes = notes
    project.next_action = next_action or None
    project.updated_at = utcnow()
    session.add(project)
    session.commit()
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@app.post("/project/{project_id}/outreach")
def log_outreach(project_id: int, channel: str = Form("call"), notes: str = Form(""),
                 next_action: str = Form(""),
                 session: Session = Depends(get_session), _: str = Depends(auth)):
    if not session.get(Project, project_id):
        raise HTTPException(404)
    session.add(Outreach(project_id=project_id, channel=channel, notes=notes,
                         next_action=next_action or None))
    session.commit()
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@app.get("/project/{project_id}/brief", response_class=HTMLResponse)
def project_brief(project_id: int, request: Request,
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.brief import build_brief
    try:
        b = build_brief(session, project_id)
    except ValueError:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "brief.html", {
        "b": b, "p": b["project"], "tb": _title_block(session), "active": "board",
    })


@app.post("/project/{project_id}/outcome")
def record_project_outcome(project_id: int, status: str = Form(...), reason: str = Form(""),
                           session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.outcomes import record_outcome
    try:
        record_outcome(session, project_id, status, reason)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@app.post("/project/{project_id}/false-positive", response_class=HTMLResponse)
def mark_false_positive(project_id: int, reason: str = Form(""),
                        session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.models import FalsePositiveMark
    p = session.get(Project, project_id)
    if not p:
        raise HTTPException(404)
    session.add(FalsePositiveMark(project_id=project_id, reason=reason,
                                  score_at_mark=p.score, window_at_mark=p.window.value))
    session.commit()
    return HTMLResponse('<span class="bad">marked FP ✓</span>')


@app.post("/firms")
def add_firm(name: str = Form(...), firm_type: str = Form("unknown"),
             aliases: str = Form(""),
             session: Session = Depends(get_session), _: str = Depends(auth)):
    norm = normalize_name(name)
    existing = session.exec(select(Firm).where(Firm.name_norm == norm)).first()
    alias_list = [a.strip() for a in aliases.split(";") if a.strip()]
    if existing:
        existing.firm_type = firm_type
        existing.aliases = sorted(set(existing.aliases) | set(alias_list))
        session.add(existing)
    else:
        session.add(Firm(name=name, name_norm=norm, firm_type=firm_type,
                         aliases=alias_list, added_from="dashboard"))
    session.commit()
    return RedirectResponse("/contacts", status_code=303)


def _csv_response(filename: str, header: list[str], rows: list[list]) -> "Response":
    import csv
    import io

    from fastapi import Response
    buf = io.StringIO()
    writer = csv.writer(buf)
    writer.writerow(header)
    writer.writerows(rows)
    return Response(buf.getvalue(), media_type="text/csv",
                    headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.get("/export/board.csv")
def export_board(watchlist: bool = False, category: str = "data_center",
                 session: Session = Depends(get_session), _: str = Depends(auth)):
    q = select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == (not watchlist))
    cat = _parse_category(category)
    # The watch list is a geography view, not a category one — never filter it.
    if cat is not None and not watchlist:
        q = q.where(Project.category == cat)
    projects = session.exec(q.order_by(Project.score.desc())).all()
    rows = [[p.id, p.name, p.category.value, p.developer, p.county, p.state,
             p.tons_estimate_low, p.tons_estimate_high,
             "LOW_CONFIDENCE" if p.estimate_low_confidence else "",
             p.estimate_basis, p.stage.value, p.window.value, p.score,
             p.days_to_estimated_bid, p.status,
             p.last_signal_at.isoformat() if p.last_signal_at else "", p.next_action or ""]
            for p in projects]
    name = "watchlist.csv" if watchlist else f"board-{cat.value if cat else 'all'}.csv"
    return _csv_response(name,
                         ["id", "project", "category", "developer", "county", "state",
                          "tons_low", "tons_high",
                          "confidence_flag", "estimate_basis", "stage", "window", "score",
                          "days_to_bid", "status", "last_signal", "next_action"], rows)


@app.get("/export/contacts.csv")
def export_contacts(session: Session = Depends(get_session), _: str = Depends(auth)):
    contacts = session.exec(select(Contact)).all()
    rows = [[c.id, c.name, c.title, c.company, c.company_type, c.territory, c.phone, c.email]
            for c in contacts]
    return _csv_response("contacts.csv",
                         ["id", "name", "title", "company", "type", "territory", "phone", "email"],
                         rows)


@app.get("/export/firms.csv")
def export_firms(session: Session = Depends(get_session), _: str = Depends(auth)):
    firms = session.exec(select(Firm).order_by(Firm.firm_type, Firm.name)).all()
    rows = [[f.id, f.name, f.firm_type, "; ".join(f.aliases or []), f.added_from] for f in firms]
    return _csv_response("firms.csv", ["id", "name", "type", "aliases", "added_from"], rows)


@app.get("/export/signals.csv")
def export_signals(session: Session = Depends(get_session), _: str = Depends(auth)):
    signals = session.exec(select(Signal)).all()
    doc_urls = {d.id: d.url for d in session.exec(select(RawDocument)).all()}
    rows = [[s.id, s.signal_type.value, s.project_name, s.developer_or_owner, s.county, s.state,
             s.mw_it, s.mw_total, s.stage.value, s.summary_one_line, s.confidence,
             doc_urls.get(s.raw_document_id, "")]
            for s in signals]
    return _csv_response("signals.csv",
                         ["id", "type", "project", "developer", "county", "state", "mw_it",
                          "mw_total", "stage", "summary", "confidence", "source_url"], rows)


@app.get("/review", response_class=HTMLResponse)
def review_queue(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    pending = session.exec(
        select(MatchCandidate).where(MatchCandidate.status == "pending")
        .order_by(MatchCandidate.similarity.desc())).all()
    rows = []
    for mc in pending:
        rows.append({"mc": mc, "signal": session.get(Signal, mc.signal_id),
                     "project": session.get(Project, mc.project_id)})
    return templates.TemplateResponse(request, "review.html", {
        "rows": rows, "tb": _title_block(session), "active": "review",
    })


@app.post("/review/{candidate_id}/{decision}", response_class=HTMLResponse)
def review_decide(candidate_id: int, decision: str,
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    if decision not in ("merge", "reject"):
        raise HTTPException(400)
    apply_review_decision(session, candidate_id, decision)
    return HTMLResponse(f'<td colspan="5" class="resolved">{decision}d ✓</td>')


@app.get("/contacts", response_class=HTMLResponse)
def contacts_view(request: Request, role: str = "", territory: str = "",
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    q = select(Contact).order_by(Contact.company, Contact.name)
    if role:
        q = q.where(Contact.company_type == role)
    if territory:
        q = q.where(Contact.territory == territory)
    contacts = session.exec(q).all()
    proj_map: dict[int, list[str]] = {}
    for c in contacts:
        links = session.exec(
            select(ProjectContact, Project)
            .where(ProjectContact.contact_id == c.id, Project.id == ProjectContact.project_id)
        ).all()
        proj_map[c.id] = [f"{proj.name} ({pc.role})" for pc, proj in links]
    firms = session.exec(select(Firm).order_by(Firm.firm_type, Firm.name)).all()
    firm_projects: dict[int, int] = {}
    for f in firms:
        firm_projects[f.id] = len(session.exec(
            select(ProjectFirm).where(ProjectFirm.firm_id == f.id)).all())
    return templates.TemplateResponse(request, "contacts.html", {
        "contacts": contacts, "proj_map": proj_map, "role": role, "territory": territory,
        "firms": firms, "firm_projects": firm_projects,
        "tb": _title_block(session), "active": "contacts",
    })


@app.post("/contacts")
def add_contact(name: str = Form(...), title: str = Form(""), company: str = Form(""),
                company_type: str = Form(""), territory: str = Form(""),
                phone: str = Form(""), email: str = Form(""),
                session: Session = Depends(get_session), _: str = Depends(auth)):
    session.add(Contact(name=name, title=title or None, company=company or None,
                        company_type=company_type or None, territory=territory or None,
                        phone=phone or None, email=email or None))
    session.commit()
    return RedirectResponse("/contacts", status_code=303)


@app.get("/map", response_class=HTMLResponse)
def map_view(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    projects = session.exec(
        select(Project).where(Project.status == "active", Project.latitude.is_not(None))
    ).all()
    markers = [
        {"lat": p.latitude, "lon": p.longitude, "name": p.name, "score": p.score,
         "color": score_color(p.score), "id": p.id,
         "tons": f"{p.tons_estimate_low:,.0f}-{p.tons_estimate_high:,.0f}"
                 if p.tons_estimate_low else "?"}
        for p in projects
    ]
    return templates.TemplateResponse(request, "map.html", {
        "markers": markers, "tb": _title_block(session), "active": "map",
    })


@app.get("/health", response_class=HTMLResponse)
def source_health(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    sources: dict[str, dict] = {}
    runs = session.exec(select(SourceRun).order_by(SourceRun.started_at.desc())).all()
    for run in runs:
        entry = sources.setdefault(run.source, {"last": run, "last_ok": None})
        if entry["last_ok"] is None and run.ok:
            entry["last_ok"] = run
    from app.spend import budget_status
    return templates.TemplateResponse(request, "health.html", {
        "sources": sources, "recent_runs": runs[:50], "budget": budget_status(),
        "tb": _title_block(session), "active": "health",
    })


@app.get("/add-signal", response_class=HTMLResponse)
def add_signal_form(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    return templates.TemplateResponse(request, "add_signal.html", {
        "signal_types": [t.value for t in SignalType],
        "stages": [s.value for s in Stage],
        "categories": [Category.data_center.value, Category.industrial.value],
        "tb": _title_block(session), "active": "add",
    })


@app.post("/add-signal")
def add_signal_submit(
    signal_type: str = Form(...), summary: str = Form(...),
    project_name: str = Form(""), developer: str = Form(""), county: str = Form(""),
    state: str = Form(""), mw_it: str = Form(""), stage: str = Form("unknown"),
    person_name: str = Form(""), person_org: str = Form(""), url: str = Form(""),
    category: str = Form(Category.data_center.value),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    add_manual_signal(
        session, signal_type, summary,
        project_name=project_name or None, developer=developer or None,
        county=county or None, state=state or None,
        mw_it=float(mw_it) if mw_it else None, stage=stage or "unknown",
        person_name=person_name or None, person_org=person_org or None, url=url,
        category=category or Category.data_center.value,
    )
    # Immediately resolve + rescore so the entry shows up on the board.
    from app.pipeline.resolve import run_resolve
    from app.pipeline.size_score import run_size_score
    cfg = load_config()
    run_resolve(session, cfg, use_llm=False)
    run_size_score(session, cfg)
    # Land on the board the entry actually went to, not the default one.
    return RedirectResponse(f"/?category={category or Category.data_center.value}",
                            status_code=303)
