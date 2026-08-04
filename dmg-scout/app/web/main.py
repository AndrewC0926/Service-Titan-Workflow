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
    Contact, MatchCandidate, Outreach, Project, ProjectContact, ProjectSignal,
    RawDocument, Signal, SignalType, SourceRun, Stage, utcnow,
)
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
def board(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    projects = session.exec(
        select(Project).where(Project.status == "active").order_by(Project.score.desc())
    ).all()
    days_since = {}
    for p in projects:
        days_since[p.id] = (utcnow() - p.last_signal_at).days if p.last_signal_at else None
    review_count = session.exec(
        select(func.count(MatchCandidate.id)).where(MatchCandidate.status == "pending")).one()
    return templates.TemplateResponse(request, "board.html", {
        "projects": projects, "days_since": days_since, "review_count": review_count,
        "tb": _title_block(session), "active": "board",
    })


@app.get("/project/{project_id}", response_class=HTMLResponse)
def project_detail(project_id: int, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    links = session.exec(
        select(ProjectSignal).where(ProjectSignal.project_id == project_id)).all()
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
    return templates.TemplateResponse(request, "project.html", {
        "p": project, "timeline": timeline, "people": people, "firms": firms,
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
    return templates.TemplateResponse(request, "contacts.html", {
        "contacts": contacts, "proj_map": proj_map, "role": role, "territory": territory,
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
    return templates.TemplateResponse(request, "health.html", {
        "sources": sources, "recent_runs": runs[:50],
        "tb": _title_block(session), "active": "health",
    })


@app.get("/add-signal", response_class=HTMLResponse)
def add_signal_form(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    return templates.TemplateResponse(request, "add_signal.html", {
        "signal_types": [t.value for t in SignalType],
        "stages": [s.value for s in Stage],
        "tb": _title_block(session), "active": "add",
    })


@app.post("/add-signal")
def add_signal_submit(
    signal_type: str = Form(...), summary: str = Form(...),
    project_name: str = Form(""), developer: str = Form(""), county: str = Form(""),
    state: str = Form(""), mw_it: str = Form(""), stage: str = Form("unknown"),
    person_name: str = Form(""), person_org: str = Form(""), url: str = Form(""),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    add_manual_signal(
        session, signal_type, summary,
        project_name=project_name or None, developer=developer or None,
        county=county or None, state=state or None,
        mw_it=float(mw_it) if mw_it else None, stage=stage or "unknown",
        person_name=person_name or None, person_org=person_org or None, url=url,
    )
    # Immediately resolve + rescore so the entry shows up on the board.
    from app.pipeline.resolve import run_resolve
    from app.pipeline.size_score import run_size_score
    cfg = load_config()
    run_resolve(session, cfg, use_llm=False)
    run_size_score(session, cfg)
    return RedirectResponse("/", status_code=303)
