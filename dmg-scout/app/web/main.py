"""Dashboard: FastAPI + Jinja2 + HTMX behind HTTP basic auth. Reads like an
engineering submittal package: title block, dense tables, monospace numbers,
thermal gradient on the score column."""
from __future__ import annotations

import json
import os
import secrets
from datetime import datetime
from pathlib import Path

from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, func, select

from app.accounts import ACCOUNT_TYPES, COVERAGE_STATUSES
from app.config import load_config
from app.db import get_session
from app.firmprofile import CONTACT_STATE_LABELS
from app.manual import add_manual_signal
from app.mcp_server import mcp_app, mounted_middleware, mounted_routes
from app.models import (
    ACTIVE_STATUSES, OUTCOME_STATUSES, Account, AccountCoverage, Category, Contact, Firm,
    MatchCandidate, Outreach, Project, ProductLine, ProjectContact, ProjectFirm, ProjectSignal,
    RawDocument, RetrofitBuilding, SavedSearch, Signal, SignalType, SourceRun, Stage,
    StageObservation, utcnow,
)


def _parse_category(value: str | None) -> Category | None:
    """None means "the construction boards" — see _category_filter.

    An unknown value falls back to that rather than silently showing an empty
    board.
    """
    if not value or value == "all":
        return None
    try:
        return Category(value)
    except ValueError:
        return None


def _category_filter(cat: Category | None):
    """The WHERE clause for a board selection.

    `all` means BOTH CONSTRUCTION BOARDS, not literally every category, and the
    distinction matters now that `esco` exists. An ESCO award is a retrofit
    procurement on buildings that already exist: it has no tonnage, no developer
    and no bid date, so dropping it into a list ranked by winnability of new
    construction would add rows that cannot be compared with the ones around
    them. It is kept, counted and reachable under its own chip — just not mixed
    into a ranking it is not competing in.
    """
    if cat is None:
        return Project.category.in_(Category.boards())
    return Project.category == cat
from app.normalize import normalize_name
from app.pipeline.resolve import apply_review_decision

app = FastAPI(title="DMG Scout", lifespan=mcp_app.lifespan)
# Flattened onto the route list directly rather than app.mount("/mcp", mcp_app):
# mounting would prefix the OAuth well-known discovery routes with /mcp too, and
# RFC 8414/9728 require those at the domain root. See app/mcp_server.py. Flattening
# routes bypasses mcp_app's own middleware stack too, so it has to be reapplied
# here explicitly or every Bearer-token request 401s regardless of validity.
for _mw in mounted_middleware():
    app.add_middleware(_mw.cls, *_mw.args, **_mw.kwargs)
app.router.routes.extend(mounted_routes())
security = HTTPBasic()

templates = Jinja2Templates(directory=str(Path(__file__).parent / "templates"))


def score_color(score: float) -> str:
    """Cold-to-hot thermal gradient: 0 = deep blue, 1+ = red-hot."""
    t = max(0.0, min(1.0, score))
    hue = 215 - t * 215  # 215 (cold blue) -> 0 (hot red)
    return f"hsl({hue:.0f} 85% 55%)"


def score_ink(score: float) -> str:
    """Text colour for a chip sitting on score_color().

    The gradient runs through mid-luminance blues and greens where white text
    fails and through yellows where black text is the only readable choice, so the
    chip cannot pick one ink and keep it. Computed from the same t as the
    background so the pair always moves together.
    """
    t = max(0.0, min(1.0, score))
    hue = 215 - t * 215
    # Perceived luminance of hsl(hue 85% 55%) peaks around yellow-green (60-160).
    return "#10131a" if 35 <= hue <= 195 else "#ffffff"


templates.env.globals["score_color"] = score_color
templates.env.globals["score_ink"] = score_ink
templates.env.globals["now"] = utcnow
templates.env.globals["CONTACT_STATE_LABELS"] = CONTACT_STATE_LABELS

app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")),
          name="static")


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
def today(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """The Today view: the same four sections as the digest email — three
    calls, what changed, what's due, one thing worth knowing — clickable, and
    the first thing seen rather than the board. See app/pipeline/notify.py's
    module docstring for why these four; changes_preview() specifically (not
    the digest's own _changes_since_last_digest) so loading this page never
    consumes a change tomorrow's real digest email would otherwise report.
    """
    from app.pipeline.notify import today_brief
    brief = today_brief(session, load_config())
    return templates.TemplateResponse(request, "today.html", {
        **brief, "tb": _title_block(session), "active": "today",
    })


@app.get("/board", response_class=HTMLResponse)
def board(request: Request, category: str = "data_center",
          session: Session = Depends(get_session), _: str = Depends(auth)):
    # Two boards, one pipeline. Defaults to data centers: that is the book of
    # business this system was built for, and industrial should never silently
    # dilute it. `?category=all` shows both.
    cat = _parse_category(category)
    q = select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == True,  # noqa: E712
                              _category_filter(cat))
    projects = session.exec(q.order_by(Project.score.desc())).all()
    # Every category gets a count, including esco — a chip whose count is hidden
    # is a category nobody will ever click.
    counts = {
        c.value: session.exec(
            select(func.count(Project.id)).where(
                Project.status.in_(ACTIVE_STATUSES),
                Project.in_territory == True,  # noqa: E712
                Project.category == c)).one()
        for c in (*Category.boards(), Category.esco)
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
        **_board_extras(session, projects),
    })


def _board_extras(session: Session, projects: list[Project]) -> dict:
    """The contact column and the summary strip.

    The charter's success criterion is "a project, early, plus at least one human
    with a phone or email", and the board did not show the human. It showed the
    project and left the rep to open each row to find out whether there was
    anyone to call — which means the board could not be read as a call list, only
    as a reading list. `build_ladders` makes the column affordable (289 rows in
    under a second, against minutes for the per-project builder).
    """
    from app.ladder import build_ladders, contact_status
    from app.staleness import stage_ages, staleness_summary

    ladders = build_ladders(session, projects)
    contacts: dict[int, dict] = {}
    statuses: dict[int, str] = {}
    # "Research" used to mean the board simply didn't say who — it fires
    # whenever a named person exists with no phone/email (see
    # contact_status), which since Phase 5's Apollo/Lusha search-only pass
    # (app/enrichment.py:import_pending_contact) now regularly means "we know
    # exactly who, call the firm's main line and ask for them by name." The
    # stamp is worthless without the name, so it's carried separately here.
    research_names: dict[int, dict] = {}
    for p in projects:
        cs = contact_status(session, p, ladder=ladders[p.id])
        statuses[p.id] = cs["status"]
        contacts[p.id] = cs["best_reachable"]
        if cs["status"] == "name_only":
            person = next((r for r in ladders[p.id] if r["kind"] == "person"), None)
            if person:
                research_names[p.id] = person

    # How old the evidence for each row's STAGE is. A stage is a claim about now,
    # made from a document with a date on it, and the board prints a bid-date
    # estimate derived from it — so a stale stage silently produces a confident
    # wrong date.
    threshold = load_config().get("board.stage_unverified_months", 12)
    ages = stage_ages(session, projects)
    stale = staleness_summary(ages, threshold)

    windows: dict[str, int] = {}
    for p in projects:
        windows[p.window.value] = windows.get(p.window.value, 0) + 1
    tons_low = sum(p.tons_estimate_low or 0 for p in projects)
    tons_high = sum(p.tons_estimate_high or 0 for p in projects)
    return {
        "contacts": contacts,
        "contact_statuses": statuses,
        "research_names": research_names,
        "stage_ages": ages,
        "stale": stale,
        "stale_months": threshold,
        # Bars are scaled to the board's own maximum, not to 1.0. Scores cluster
        # between 0.3 and 0.7, so a fixed 0-1 scale spends most of its length on
        # range that never occurs and compresses the part that does.
        "score_max": max([p.score for p in projects] or [1.0]) or 1.0,
        "summary": {
            "n": len(projects),
            "callable": sum(1 for v in statuses.values() if v == "contactable"),
            "name_only": sum(1 for v in statuses.values() if v == "name_only"),
            "no_one": sum(1 for v in statuses.values() if v == "none"),
            "sized": sum(1 for p in projects if p.tons_estimate_low),
            "tons_low": tons_low, "tons_high": tons_high,
            "windows": windows,
        },
    }


@app.get("/retrofit", response_class=HTMLResponse)
def retrofit_board(request: Request, county: str = None, min_status: str = None,
                   population: str = "replacement_candidate", limit: int = 200,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    """Two SEPARATE populations, never merged — see RetrofitBuilding's
    docstring:
      replacement_candidate — the real opportunity. Commercial buildings
        old enough to need replacement with NO permit on record at all
        (absence is the signal: a permit means someone already replaced).
        Can run to tens of thousands of rows county-wide (absence is common)
        so this view is capped at `limit` (top-ranked first) — the full
        count is still shown, never silently hidden.
      recently_active — buildings WITH permit evidence in the last several
        years. Not a call list (everything here just got serviced) — market
        and contractor intelligence: where mechanical work is actually
        happening.
    Both a SEPARATE population from /board (zero APN overlap, confirmed)."""
    base_q = select(RetrofitBuilding).where(RetrofitBuilding.population == population)
    if county:
        base_q = base_q.where(RetrofitBuilding.county == county)
    STATUS_ORDER = ["overdue", "due", "approaching", "not_due"]
    if min_status and min_status in STATUS_ORDER:
        base_q = base_q.where(RetrofitBuilding.service_life_status.in_(
            STATUS_ORDER[:STATUS_ORDER.index(min_status) + 1]))

    total = session.exec(select(func.count()).select_from(base_q.subquery())).one()
    buildings = session.exec(
        base_q.order_by(RetrofitBuilding.rank_score.desc().nulls_last()).limit(limit)).all()

    counties = sorted({b.county for b in session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.population == population)).all()})
    summary = {
        "n": total, "shown": len(buildings),
        "overdue": sum(1 for b in buildings if b.service_life_status == "overdue"),
        "due": sum(1 for b in buildings if b.service_life_status == "due"),
        "sb1206": sum(1 for b in buildings if b.sb1206_trigger_status),
        "carb": sum(1 for b in buildings if b.carb_candidate),
        "ebewe": sum(1 for b in buildings if b.ebewe_candidate),
    }
    return templates.TemplateResponse(request, "retrofit_board.html", {
        "buildings": buildings, "summary": summary, "counties": counties,
        "county": county, "min_status": min_status, "population": population, "limit": limit,
        "score_max": max([b.rank_score for b in buildings if b.rank_score] or [1.0]),
        "tb": _title_block(session), "active": "retrofit",
    })


@app.get("/retrofit/report", response_class=HTMLResponse)
def retrofit_report(request: Request, county: str = None, min_status: str = "due", limit: int = 100,
                    population: str = "replacement_candidate",
                    session: Session = Depends(get_session), _: str = Depends(auth)):
    """Printable per-territory retrofit list, with the regulations forcing
    replacement and every figure's basis. replacement_candidate rows carry
    a YearBuilt-derived service life (weaker evidence, disclosed as such in
    each row's basis line) rather than the permit-verified figure
    recently_active rows carry. Hand this to a service contractor — every
    fact traces to a public record."""
    q = select(RetrofitBuilding).where(RetrofitBuilding.population == population)
    if county:
        q = q.where(RetrofitBuilding.county == county)
    STATUS_ORDER = ["overdue", "due", "approaching", "not_due"]
    if min_status in STATUS_ORDER:
        q = q.where(RetrofitBuilding.service_life_status.in_(
            STATUS_ORDER[:STATUS_ORDER.index(min_status) + 1]))
    buildings = session.exec(
        q.order_by(RetrofitBuilding.rank_score.desc().nulls_last()).limit(limit)).all()
    return templates.TemplateResponse(request, "retrofit_report.html", {
        "buildings": buildings, "county": county or "All counties",
        "min_status": min_status, "population": population, "generated_at": utcnow(),
        "tb": _title_block(session), "active": "retrofit",
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
        **_board_extras(session, projects),
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

    from app.staleness import stage_ages
    stage_progression = session.exec(
        select(StageObservation).where(StageObservation.project_id == project_id)
        .order_by(StageObservation.observed_at)).all()
    stale_months = load_config().get("board.stage_unverified_months", 12)
    stage_age = stage_ages(session, [project]).get(project.id)

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
        "stage_progression": stage_progression, "stage_age": stage_age,
        "stale_months": stale_months,
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
def log_outreach(project_id: int, request: Request, channel: str = Form("call"),
                 notes: str = Form(""), next_action: str = Form(""),
                 session: Session = Depends(get_session), _: str = Depends(auth)):
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    o = Outreach(project_id=project_id, channel=channel, notes=notes,
                next_action=next_action or None)
    session.add(o)
    session.commit()
    # The Today page's one-tap "called them" button posts here via HTMX and
    # must NOT navigate away — that is the whole point of one-tap. A plain
    # browser form (the project page's own outreach log) has no HX-Request
    # header and keeps the original redirect-to-project behavior.
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<span class="ok">✓ Logged {o.date:%-I:%M %p}</span>')
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
    # Second queue on the same page: numbers the unit guard threw away. They are
    # already null everywhere downstream, so nothing is waiting on a decision —
    # this is here so a rejection is visible rather than a silent hole, and so a
    # pattern of rejections on one source gets noticed.
    flagged = []
    for s in session.exec(select(Signal)).all():
        for r in (s.extraction_json or {}).get("rejected_numeric") or []:
            doc = session.get(RawDocument, s.raw_document_id)
            flagged.append({"signal": s, "doc": doc, "rej": r})
    flagged.sort(key=lambda f: -abs(f["rej"].get("value") or 0))
    return templates.TemplateResponse(request, "review.html", {
        "rows": rows, "flagged": flagged,
        "tb": _title_block(session), "active": "review",
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
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.latitude.is_not(None))).all()
    ladders = None
    if projects:
        from app.ladder import build_ladders, contact_status
        ladders = build_ladders(session, projects)

    markers = []
    for p in projects:
        reach = (contact_status(session, p, ladder=ladders[p.id])["best_reachable"]
                 if ladders else None)
        markers.append({
            "lat": p.latitude, "lon": p.longitude, "name": p.name, "score": p.score,
            "id": p.id, "county": p.county or "?", "state": p.state or "?",
            "window": p.window.value, "stage": p.stage.value,
            "tons": (f"{p.tons_estimate_low:,.0f}–{p.tons_estimate_high:,.0f}"
                     if p.tons_estimate_low else None),
            # The map answers "what is near me", and near-me is only useful if the
            # row tells you who to ring while you are standing there.
            "who": (reach["name"] if reach else None),
            "reach": (reach["phone"] or reach["email"]) if reach else None,
        })
    return templates.TemplateResponse(request, "map.html", {
        "markers": markers, "tb": _title_block(session), "active": "map",
    })


# ---- saved searches ---------------------------------------------------------

@app.get("/searches", response_class=HTMLResponse)
def searches_view(request: Request, session: Session = Depends(get_session),
                  _: str = Depends(auth)):
    from app.searches import CRITERIA_KEYS, UnknownCriterion, run_search

    rows = []
    for s in session.exec(select(SavedSearch).order_by(SavedSearch.created_at)).all():
        try:
            hits = run_search(session, s.criteria)
            error = None
        except (UnknownCriterion, ValueError) as exc:
            # Shown, never swallowed: a saved search that cannot run is a question
            # the rep believes is being asked and is not.
            hits, error = [], str(exc)
        rows.append({"s": s, "n": len(hits), "top": hits[:5], "error": error})
    return templates.TemplateResponse(request, "searches.html", {
        "rows": rows, "criteria_keys": sorted(CRITERIA_KEYS),
        "tb": _title_block(session), "active": "searches",
    })


@app.post("/searches")
def searches_create(name: str = Form(...), criteria_json: str = Form("{}"),
                    alert: str = Form(None), alert_on_change: str = Form(None),
                    session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.searches import UnknownCriterion, validate
    try:
        criteria = json.loads(criteria_json or "{}")
        if not isinstance(criteria, dict):
            raise ValueError("criteria must be a JSON object")
        validate(criteria)
    except (json.JSONDecodeError, ValueError, UnknownCriterion) as exc:
        raise HTTPException(400, detail=str(exc))
    session.add(SavedSearch(name=name.strip() or "untitled", criteria=criteria,
                            alert=bool(alert), alert_on_change=bool(alert_on_change)))
    session.commit()
    return RedirectResponse("/searches", status_code=status.HTTP_303_SEE_OTHER)


@app.post("/searches/{search_id}/delete")
def searches_delete(search_id: int, session: Session = Depends(get_session),
                    _: str = Depends(auth)):
    s = session.get(SavedSearch, search_id)
    if s:
        session.delete(s)
        session.commit()
    return RedirectResponse("/searches", status_code=status.HTTP_303_SEE_OTHER)


# ---- plain-language search --------------------------------------------------

@app.get("/ask", response_class=HTMLResponse)
def ask_view(request: Request, q: str = "", session: Session = Depends(get_session),
             _: str = Depends(auth)):
    """Ask the board a question in plain language.

    The model produces a validated criteria dict, never SQL, and the dict is shown
    back on the page. A filter you cannot read is a filter you cannot check, and
    an unchecked filter that quietly dropped half the market looks exactly like a
    quiet market.
    """
    from app.llm import LLMUnavailable
    from app.nlsearch import Uninterpretable, interpret
    from app.searches import UnknownCriterion, run_search

    result = {"question": q, "criteria": None, "unsupported": [], "reading": "",
              "projects": [], "error": None}
    if q.strip():
        try:
            parsed = interpret(q)
            result.update(parsed)
            result["projects"] = run_search(session, parsed["criteria"])
        except (LLMUnavailable, Uninterpretable, UnknownCriterion) as exc:
            result["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001 — surfaced, never a blank page
            result["error"] = f"{type(exc).__name__}: {exc}"
    return templates.TemplateResponse(request, "ask.html", {
        "r": result, "tb": _title_block(session), "active": "ask",
    })


# ---- outreach ---------------------------------------------------------------

@app.get("/outreach", response_class=HTMLResponse)
def outreach_view(request: Request, session: Session = Depends(get_session),
                  _: str = Depends(auth)):
    """The call list: what is owed, what is cold, what has never been touched.

    The board answers "which projects are worth calling". This answers "which call
    do I make next", and they are not the same question — a high-scoring project
    you rang yesterday is not today's call, and a mid-scoring one with a promise
    attached to a date is.

    Three sections, in the order a morning actually goes: promises with a date on
    them, then rows that were worked and went quiet, then rows never touched at
    all. Untouched is last on purpose — it is the biggest list and the least
    urgent, and putting it first buries the commitments.
    """
    from app.ladder import build_ladders, contact_status

    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == True)).all()  # noqa: E712
    by_id = {p.id: p for p in projects}
    ladders = build_ladders(session, projects) if projects else {}

    touches = session.exec(select(Outreach).order_by(Outreach.date.desc())).all()
    last_touch: dict[int, Outreach] = {}
    for o in touches:
        last_touch.setdefault(o.project_id, o)

    contacts = {c.id: c for c in session.exec(select(Contact)).all()}
    now = utcnow()

    def row(p: Project) -> dict:
        reach = contact_status(session, p, ladder=ladders.get(p.id))["best_reachable"]
        o = last_touch.get(p.id)
        return {
            "p": p, "o": o,
            "contact": contacts.get(o.contact_id) if o and o.contact_id else None,
            "days": (now - o.date).days if o else None,
            "who": reach,
        }

    due, cold, untouched = [], [], []
    for p in projects:
        r = row(p)
        o = r["o"]
        if o and o.next_action and o.next_action_date:
            r["overdue_by"] = (now - o.next_action_date).days
            due.append(r)
        elif o:
            cold.append(r)
        elif r["who"]:
            # No contact method means no call to make, so an untouched row with
            # nobody reachable is research, not an omission — it belongs on the
            # board's `Research` count, not in a call queue that implies a duty.
            untouched.append(r)

    due.sort(key=lambda r: -r["overdue_by"])
    cold.sort(key=lambda r: -(r["days"] or 0))
    untouched.sort(key=lambda r: -(r["p"].score or 0))
    return templates.TemplateResponse(request, "outreach.html", {
        "due": due, "cold": cold, "untouched": untouched[:50],
        "n_untouched": len(untouched),
        "tb": _title_block(session), "active": "outreach",
    })


# ---- firm profiles ----------------------------------------------------------

@app.get("/firms", response_class=HTMLResponse)
def firms_index(request: Request, session: Session = Depends(get_session),
                _: str = Depends(auth)):
    from app.firmprofile import firm_index
    idx = firm_index(session)
    return templates.TemplateResponse(request, "firms.html", {
        **idx, "tb": _title_block(session), "active": "firms",
    })


@app.get("/firm/{firm_id}", response_class=HTMLResponse)
def firm_detail(firm_id: int, request: Request,
                session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.firmprofile import firm_profile
    prof = firm_profile(session, firm_id)
    if prof is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "firm.html", {
        "p": prof, "tb": _title_block(session), "active": "firms",
    })


@app.get("/firm/{firm_id}/brief", response_class=HTMLResponse)
def firm_brief(firm_id: int, request: Request,
               session: Session = Depends(get_session), _: str = Depends(auth)):
    """Printable one-page brief for a meeting with this firm — every project,
    the collaborators, our history, generated_at so it's clear how fresh the
    printout is. Same pattern as /retrofit/report."""
    from app.firmprofile import firm_profile
    prof = firm_profile(session, firm_id)
    if prof is None:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "firm_brief.html", {
        "p": prof, "generated_at": utcnow(), "tb": _title_block(session), "active": "firms",
    })



# ---- accounts ----------------------------------------------------------
#
# A separate book from the project pipeline above: line-card coverage for the
# ~100 dormant accounts, not construction signals. Nothing here reads or
# writes Project/Signal, and nothing above this section reads Account. The
# only bridge is Account.firm_id, used solely to show live Scout projects on
# an account's brief (see app.accounts.live_scout_projects).

@app.get("/accounts", response_class=HTMLResponse)
def accounts_list(request: Request, rep: str = "", county: str = "", account_type: str = "",
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    q = select(Account).where(Account.status == "active").order_by(Account.name)
    if rep:
        q = q.where(Account.assigned_rep == rep)
    if county:
        q = q.where(Account.county == county)
    if account_type:
        q = q.where(Account.account_type == account_type)
    accounts = session.exec(q).all()

    bought_counts: dict[int, int] = {}
    total_counts: dict[int, int] = {}
    for row in session.exec(
        select(AccountCoverage.account_id, AccountCoverage.status,
              func.count(AccountCoverage.id)).group_by(
                  AccountCoverage.account_id, AccountCoverage.status)).all():
        acc_id, status_, n = row
        total_counts[acc_id] = total_counts.get(acc_id, 0) + n
        if status_ == "bought":
            bought_counts[acc_id] = n

    reps = sorted({a.assigned_rep for a in
                   session.exec(select(Account).where(Account.status == "active")).all()
                   if a.assigned_rep})
    counties = sorted({a.county for a in
                       session.exec(select(Account).where(Account.status == "active")).all()
                       if a.county})
    return templates.TemplateResponse(request, "accounts_list.html", {
        "accounts": accounts, "bought_counts": bought_counts, "total_counts": total_counts,
        "reps": reps, "counties": counties, "account_types": list(ACCOUNT_TYPES),
        "f_rep": rep, "f_county": county, "f_type": account_type,
        "tb": _title_block(session), "active": "accounts",
    })


@app.get("/accounts/new", response_class=HTMLResponse)
def account_new_form(request: Request, session: Session = Depends(get_session),
                     _: str = Depends(auth)):
    parents = session.exec(select(Account).where(Account.status == "active")
                           .order_by(Account.name)).all()
    return templates.TemplateResponse(request, "account_form.html", {
        "account": None, "parents": parents, "account_types": list(ACCOUNT_TYPES),
        "ownership_types": ["private_commercial", "federal", "state_municipal"],
        "tb": _title_block(session), "active": "accounts",
    })


# Registered here, BEFORE /accounts/{account_id} below, on purpose — FastAPI
# matches routes in registration order, and a literal "/accounts/import"
# registered after the dynamic route would be swallowed by it (account_id=
# "import", a 422 on int parsing) rather than ever reaching this handler.
@app.get("/accounts/import", response_class=HTMLResponse)
def accounts_import_form(request: Request, session: Session = Depends(get_session),
                         _: str = Depends(auth)):
    return templates.TemplateResponse(request, "accounts_import.html", {
        "step": "upload", "tb": _title_block(session), "active": "accounts",
    })


@app.post("/accounts/import/preview", response_class=HTMLResponse)
async def accounts_import_preview(request: Request, file: UploadFile = File(...),
                                  session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.importers.accounts_csv import ACCOUNT_FIELDS, guess_mapping, parse_csv, preview_import
    raw_bytes = await file.read()
    raw_text = raw_bytes.decode("utf-8-sig", errors="replace")
    headers, rows = parse_csv(raw_text)
    if not headers:
        return templates.TemplateResponse(request, "accounts_import.html", {
            "step": "upload", "error": "no header row found in that file",
            "tb": _title_block(session), "active": "accounts",
        })
    mapping = guess_mapping(headers)
    preview = preview_import(session, headers, rows, mapping)
    return templates.TemplateResponse(request, "accounts_import.html", {
        "step": "preview", "headers": headers, "mapping": mapping, "preview": preview[:200],
        "n_total": len(rows), "n_shown": min(len(rows), 200),
        "account_fields": ACCOUNT_FIELDS, "raw_csv": raw_text,
        "tb": _title_block(session), "active": "accounts",
    })


@app.post("/accounts/import/commit", response_class=HTMLResponse)
async def accounts_import_commit(request: Request, raw_csv: str = Form(...),
                                 session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.importers.accounts_csv import commit_import, parse_csv
    form = await request.form()
    # Mapping arrives as one form field per CSV header: name="map__<header>".
    mapping = {key[len("map__"):]: value for key, value in form.multi_items()
              if key.startswith("map__") and value}
    headers, rows = parse_csv(raw_csv)
    result = commit_import(session, headers, rows, mapping)
    return templates.TemplateResponse(request, "accounts_import.html", {
        "step": "done", "result": result,
        "tb": _title_block(session), "active": "accounts",
    })


def _parse_form_date(value: str):
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return None


@app.post("/accounts")
def account_create(
    name: str = Form(...), parent_id: str = Form(""), account_type: str = Form("mechanical_contractor"),
    address: str = Form(""), city: str = Form(""), county: str = Form(""), state: str = Form(""),
    assigned_rep: str = Form(""), ownership_type: str = Form("private_commercial"),
    first_order_date: str = Form(""), last_order_date: str = Form(""), notes: str = Form(""),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    from app.accounts import create_account
    account = create_account(
        session, name=name, parent_id=int(parent_id) if parent_id else None,
        account_type=account_type, address=address or None, city=city or None,
        county=county or None, state=state or None, assigned_rep=assigned_rep or None,
        ownership_type=ownership_type,
        first_order_date=_parse_form_date(first_order_date),
        last_order_date=_parse_form_date(last_order_date), notes=notes,
    )
    return RedirectResponse(f"/accounts/{account.id}", status_code=303)


@app.get("/accounts/{account_id}/edit", response_class=HTMLResponse)
def account_edit_form(account_id: int, request: Request,
                      session: Session = Depends(get_session), _: str = Depends(auth)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    parents = session.exec(
        select(Account).where(Account.status == "active", Account.id != account_id)
        .order_by(Account.name)).all()
    return templates.TemplateResponse(request, "account_form.html", {
        "account": account, "parents": parents, "account_types": list(ACCOUNT_TYPES),
        "ownership_types": ["private_commercial", "federal", "state_municipal"],
        "tb": _title_block(session), "active": "accounts",
    })


@app.post("/accounts/{account_id}/edit")
def account_edit_save(
    account_id: int,
    name: str = Form(...), parent_id: str = Form(""), account_type: str = Form("mechanical_contractor"),
    address: str = Form(""), city: str = Form(""), county: str = Form(""), state: str = Form(""),
    assigned_rep: str = Form(""), ownership_type: str = Form("private_commercial"),
    first_order_date: str = Form(""), last_order_date: str = Form(""),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    if parent_id and int(parent_id) == account_id:
        raise HTTPException(400, detail="an account cannot be its own parent")
    account.name = name
    account.name_norm = normalize_name(name)
    account.parent_id = int(parent_id) if parent_id else None
    account.account_type = account_type
    account.address = address or None
    account.city = city or None
    account.county = county or None
    account.state = state or None
    account.assigned_rep = assigned_rep or None
    account.ownership_type = ownership_type
    account.first_order_date = _parse_form_date(first_order_date)
    account.last_order_date = _parse_form_date(last_order_date)
    account.updated_at = utcnow()
    session.add(account)
    session.commit()
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.get("/accounts/{account_id}", response_class=HTMLResponse)
def account_detail(account_id: int, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.accounts import (
        account_replacement_windows, compute_gaps, coverage_summary, ensure_coverage_rows,
        live_scout_projects,
    )
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    # Picks up any line added to the card since this account was created —
    # cheap (two selects over a ~70-row table) and keeps the page always
    # showing every line, never one the seed added after the fact.
    ensure_coverage_rows(session, account)
    parent = session.get(Account, account.parent_id) if account.parent_id else None
    children = session.exec(select(Account).where(Account.parent_id == account_id)).all()
    cfg = load_config()
    cov = coverage_summary(session, account_id)
    cats = cfg.get("accounts.adjacency.categories", []) or []
    cat_order = {c: i for i, c in enumerate(cats)}
    rows = sorted(cov["rows"], key=lambda r: (cat_order.get(r[1].category, 99), r[1].name))
    return templates.TemplateResponse(request, "account_detail.html", {
        "account": account, "parent": parent, "children": children,
        "coverage_rows": rows, "coverage_counts": cov["counts"],
        "gaps": compute_gaps(session, cfg, account_id)[:25],
        "replacement_windows": account_replacement_windows(session, cfg, account_id),
        "live_projects": live_scout_projects(session, account),
        "coverage_statuses": ["bought", "quoted_not_won", "never_quoted", "unknown"],
        "tb": _title_block(session), "active": "accounts",
    })


@app.post("/accounts/{account_id}/coverage/{line_id}", response_class=HTMLResponse)
def account_coverage_update(
    account_id: int, line_id: int, request: Request,
    status: str = Form("unknown"), install_year: str = Form(""), dollar_value: str = Form(""),
    notes: str = Form(""),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    if status not in COVERAGE_STATUSES:
        raise HTTPException(400, detail=f"unknown coverage status {status!r}")
    cov = session.exec(
        select(AccountCoverage).where(AccountCoverage.account_id == account_id,
                                      AccountCoverage.product_line_id == line_id)).first()
    line = session.get(ProductLine, line_id)
    if not cov or not line:
        raise HTTPException(404)
    cov.status = status
    try:
        cov.install_year = int(install_year) if install_year else None
    except ValueError:
        raise HTTPException(400, detail="install_year must be a year")
    try:
        cov.dollar_value = float(dollar_value) if dollar_value else None
    except ValueError:
        raise HTTPException(400, detail="dollar_value must be a number")
    cov.notes = notes
    cov.updated_at = utcnow()
    session.add(cov)
    session.commit()
    return templates.TemplateResponse(request, "_coverage_row.html", {
        "account": session.get(Account, account_id), "coverage": cov, "line": line,
        "coverage_statuses": ["bought", "quoted_not_won", "never_quoted", "unknown"],
    })


@app.post("/accounts/{account_id}/notes")
def account_notes_save(account_id: int, notes: str = Form(""),
                       session: Session = Depends(get_session), _: str = Depends(auth)):
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    account.notes = notes
    account.updated_at = utcnow()
    session.add(account)
    session.commit()
    return RedirectResponse(f"/accounts/{account_id}", status_code=303)


@app.get("/accounts/{account_id}/brief", response_class=HTMLResponse)
def account_brief_view(account_id: int, request: Request,
                       session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.accounts import build_account_brief
    cfg = load_config()
    try:
        b = build_account_brief(session, cfg, account_id)
    except ValueError:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "account_brief.html", {
        "b": b, "account": b.account, "tb": _title_block(session), "active": "accounts",
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

    # ---- the chart series ------------------------------------------------
    # Source health is the one view a chart genuinely beats a table at. The
    # question is "has this source been running, and when did it stop", which is
    # a shape over time: a gap is instantly visible on a timeline and invisible
    # in a list of the last fifty rows sorted by date. The board earned nothing
    # from a chart and did not get one.
    from collections import defaultdict
    from datetime import timedelta

    from app.models import run_name_source

    days = 14
    today = utcnow().date()
    dates = [(today - timedelta(days=i)).isoformat() for i in range(days - 1, -1, -1)]
    index = {d: i for i, d in enumerate(dates)}

    # Per source per day: 1 ok, -1 failed, 0 no run. Backfill runs count as runs
    # of their source, same convention doctor uses.
    grid: dict[str, list[int]] = defaultdict(lambda: [0] * days)
    fetched: dict[str, list[int]] = defaultdict(lambda: [0] * days)
    for run in runs:
        key = run_name_source(run.source)
        slot = index.get(run.started_at.date().isoformat())
        if slot is None:
            continue
        if run.ok is False:
            grid[key][slot] = -1          # a failure on a day outranks a success
        elif run.ok and grid[key][slot] >= 0:
            grid[key][slot] = 1
        fetched[key][slot] += run.records_fetched or 0

    chart = {
        "dates": dates,
        "sources": sorted(grid),
        "status": {k: grid[k] for k in sorted(grid)},
        "fetched": {k: fetched[k] for k in sorted(grid)},
    }
    return templates.TemplateResponse(request, "health.html", {
        "sources": sources, "recent_runs": runs[:50], "budget": budget_status(),
        "chart": chart, "tb": _title_block(session), "active": "health",
    })


@app.get("/add-signal", response_class=HTMLResponse)
def add_signal_form(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    return templates.TemplateResponse(request, "add_signal.html", {
        "signal_types": [t.value for t in SignalType],
        "stages": [s.value for s in Stage],
        # esco included: a rep who hears about an ESPC award at a city they cover
        # has nowhere else to put it, and the manual path is the only way in until
        # triage sees one of its own.
        "categories": [c.value for c in (*Category.boards(), Category.esco)],
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
