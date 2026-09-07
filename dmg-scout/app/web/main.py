"""Dashboard: FastAPI + Jinja2 + HTMX behind HTTP basic auth. Reads like an
engineering submittal package: title block, dense tables, monospace numbers,
thermal gradient on the score column."""
from __future__ import annotations

import json
import logging
import os
import secrets
from datetime import datetime, timedelta
from pathlib import Path

import httpx
from fastapi import Depends, FastAPI, File, Form, HTTPException, Request, UploadFile, status
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials, HTTPAuthorizationCredentials, HTTPBearer
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlmodel import Session, and_, case, func, or_, select

from app.accounts import (
    ACCOUNT_TYPES,
    COVERAGE_STATUSES,
    MARKET_LABELS,
    MARKETS,
    ROLE_LABELS,
    ROLE_ORDER,
    build_account_page,
)
from app.access_log import (
    access_logging_middleware,
    access_summary,
    admin_username,
    capture_voice_logging_middleware,
)
from app.assumptions import slugify
from app.config import load_config
from app.call_target import CALL_TARGET_LABELS
from app.delivery import DELIVERY_METHOD_ABBR, DELIVERY_METHOD_LABELS, DELIVERY_METHOD_NOTES
from app.reference import ROLE_REFERENCE, TAB_LABELS, TAB_ORDER, equipment_tooltip
from app.schedule_mapping import BRANCH_BY_COUNTY
from app.db import get_session
from app.firmprofile import CONTACT_STATE_LABELS
from app.manual import add_manual_signal
from app.mcp_server import mcp_app, mounted_middleware, mounted_routes
from app.models import (
    ACTIVE_STATUSES,
    OUTCOME_STATUSES,
    Account,
    AccountCoverage,
    CaptureAudio,
    Category,
    Contact,
    Contractor,
    FieldIntel,
    Firm,
    LineCompetitor,
    LinePitch,
    MatchCandidate,
    Outreach,
    ProductLine,
    Project,
    ProjectContact,
    ProjectDocument,
    ProjectFirm,
    ProjectSignal,
    RawDocument,
    ReviewQueue,
    ScheduleEntry,
    HcaiCountyActivity,
    HospitalBuilding,
    RetrofitBuilding,
    SavedSearch,
    Signal,
    SignalType,
    SourceRun,
    Stage,
    StageObservation,
    utcnow,
)

# /board and /retrofit default to the LA-office rep's own territory rather
# than the whole company's (see BRANCH_BY_COUNTY's docstring in
# app/schedule_mapping.py for why this dict, not a new one -- it is the
# SAME literal county association the branch-restriction feature already
# uses, so "my territory" here can never drift from what "my branch" means
# there). Reusing it means this is exactly {"los angeles"} today and would
# need no code change if a second office's rep view were ever added.
MY_BRANCH = "DMG Los Angeles"
MY_TERRITORY_COUNTIES = {c for c, b in BRANCH_BY_COUNTY.items() if b == MY_BRANCH}


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
from app.ops import setup_logging
from app.pipeline.resolve import apply_review_decision

# The cron service calls this via app/cli.py at import time; the web service
# never did, which meant log.info/log.warning calls anywhere in a request
# handler were silently dropped below the root logger's default WARNING
# level with no handler attached at all -- not visible in Render's log
# stream, not even via Python's stderr-writing last-resort handler for
# WARNING+ (that's ex-post-facto, not a substitute for a real handler).
# capture_voice_logging_middleware below depends on this actually working.
setup_logging()
log = logging.getLogger(__name__)

app = FastAPI(title="DMG Scout", lifespan=mcp_app.lifespan)
# Flattened onto the route list directly rather than app.mount("/mcp", mcp_app):
# mounting would prefix the OAuth well-known discovery routes with /mcp too, and
# RFC 8414/9728 require those at the domain root. See app/mcp_server.py. Flattening
# routes bypasses mcp_app's own middleware stack too, so it has to be reapplied
# here explicitly or every Bearer-token request 401s regardless of validity.
for _mw in mounted_middleware():
    app.add_middleware(_mw.cls, *_mw.args, **_mw.kwargs)
app.router.routes.extend(mounted_routes())
app.middleware("http")(access_logging_middleware)
app.middleware("http")(capture_voice_logging_middleware)


@app.exception_handler(RequestValidationError)
async def _validation_exception_handler(request: Request, exc: RequestValidationError) -> Response:
    """FastAPI's own default 422 handler, plus one addition: for
    /capture/voice specifically, stash a readable reason on
    request.state.capture_reason before building the response --
    capture_voice's own `file: UploadFile = File(...)` parameter fails
    validation this way (a RequestValidationError, not an HTTPException
    from application code) for BOTH a missing 'file' field AND a
    wrong-content-type body (confirmed empirically: FastAPI can't actually
    tell these two apart for this endpoint -- a non-multipart body just
    means no 'file' part is ever found either), so this is the only place
    that reason is ever available to capture. See app.access_log's module
    comment for why capture_voice_logging_middleware can't read it off the
    eventual response instead."""
    if request.url.path == "/capture/voice":
        parts = []
        for e in exc.errors():
            loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body")
            msg = e.get("msg", str(e))
            parts.append(f"{loc}: {msg}" if loc else str(msg))
        request.state.capture_reason = "; ".join(parts) or "validation error"
    return await request_validation_exception_handler(request, exc)


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
# Lets a column-header/value tooltip link straight to the constant it rests
# on, e.g. href="/assumptions#{{ assumption_slug('Days to estimated bid, by
# stage') }}" -- one function shared with the id the row itself renders
# (Assumption.slug), so the two can't drift apart into a dead anchor.
templates.env.globals["assumption_slug"] = slugify
templates.env.globals["ROLE_LABELS"] = ROLE_LABELS
templates.env.globals["MARKET_LABELS"] = MARKET_LABELS
templates.env.globals["equipment_tooltip"] = equipment_tooltip
templates.env.globals["DELIVERY_METHOD_LABELS"] = DELIVERY_METHOD_LABELS
templates.env.globals["DELIVERY_METHOD_ABBR"] = DELIVERY_METHOD_ABBR
templates.env.globals["CALL_TARGET_LABELS"] = CALL_TARGET_LABELS
templates.env.globals["DELIVERY_METHOD_NOTES"] = DELIVERY_METHOD_NOTES

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


capture_bearer = HTTPBearer(auto_error=False)


def capture_auth(request: Request,
                 credentials: HTTPAuthorizationCredentials = Depends(capture_bearer)) -> str:
    """Auth for POST /capture/voice (the iOS Shortcut endpoint) -- a bearer
    token via CAPTURE_API_KEY, deliberately separate from the dashboard's
    HTTP Basic DASHBOARD_PASSWORD (see auth() above and
    app.config.capture_api_key's docstring).

    request.state.capture_reason is set here, before each raise, for
    capture_voice_logging_middleware (app/access_log.py) to read back after
    the fact -- see that module's comment for why it can't reliably read
    this off the eventual response instead."""
    from app.config import capture_api_key
    key = capture_api_key()
    if not key:
        request.state.capture_reason = "CAPTURE_API_KEY env var is not set"
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="CAPTURE_API_KEY env var is not set")
    if credentials is None or not secrets.compare_digest(credentials.credentials, key):
        request.state.capture_reason = "bearer token missing or does not match CAPTURE_API_KEY"
        raise HTTPException(status.HTTP_401_UNAUTHORIZED,
                            detail="bearer token missing or does not match CAPTURE_API_KEY",
                            headers={"WWW-Authenticate": "Bearer"})
    return "capture"


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
    from app.pipeline_health import check_and_alert_staleness
    cfg = load_config()
    brief = today_brief(session, cfg)
    # Decorate the (already-selected, already-ordered) "three to call" cards
    # with call_target -- purely a display addition, computed here rather
    # than inside today_brief/three_calls_today so it can never touch which
    # 3 projects are picked or what order they render in. See app/call_target.py.
    if brief["calls"]:
        from app.call_target import (
            engineer_of_record_by_project, gc_by_project, nearby_contractor_by_project,
            project_call_target,
        )
        call_projects = [c["project"] for c in brief["calls"]]
        eor_map = engineer_of_record_by_project(session, [p.id for p in call_projects])
        gc_map = gc_by_project(session, [p.id for p in call_projects])
        nearby_map = nearby_contractor_by_project(session, cfg, call_projects)
        for c in brief["calls"]:
            p = c["project"]
            c["call_target"] = project_call_target(
                cfg, p, engineer_of_record=eor_map.get(p.id), gc=gc_map.get(p.id),
                nearby_contractor=nearby_map.get(p.id))
    # Best-effort: a failure here (DB hiccup, Resend down) must never be the
    # reason the Today page itself fails to load -- see
    # app.pipeline_health's module docstring.
    try:
        staleness = check_and_alert_staleness(session, cfg)
    except Exception:  # noqa: BLE001
        staleness = {"stale": False, "hours_stale": None, "last_success_at": None, "alert_sent": False,
                     "retrofit": {}, "memory": {"warn": False}}
    # Watch list / review queue counts moved here from the nav tab bar
    # (2026-08-18 IA pass: nav collapsed to 5 primary tabs, both of those
    # moved behind the overflow menu) -- Today is the new landing page, so
    # a count that matters shows up here instead of only appearing while
    # already looking at whichever page it was badging.
    watch_count = session.exec(
        select(func.count(Project.id)).where(Project.status.in_(ACTIVE_STATUSES),
                                             Project.in_territory == False)).one()
    review_count = session.exec(
        select(func.count(MatchCandidate.id)).where(MatchCandidate.status == "pending")).one()
    return templates.TemplateResponse(request, "today.html", {
        **brief, "staleness": staleness, "tb": _title_block(session), "active": "today",
        "watch_count": watch_count, "review_count": review_count,
    })


def _my_territory_filter():
    """Project.county exactly matches one of MY_TERRITORY_COUNTIES -- same
    literal, no-guessing discipline as app.schedule_mapping.resolve_branch:
    a project with no county on file, or a county not literally in the set
    (including a multi-county "Los Angeles; San Bernardino" string, which
    resolve_branch also would not resolve), is NOT assumed to be mine and
    stays behind the territory filter rather than being guessed in."""
    return func.lower(func.trim(Project.county)).in_(MY_TERRITORY_COUNTIES)


@app.get("/board", response_class=HTMLResponse)
def board(request: Request, view: str = "board", category: str = "data_center", territory: str = "mine",
          call_target: str = "", county: str = "", district: str = "", program: str = "",
          status: str = "", grade_level: str = "",
          session: Session = Depends(get_session), _: str = Depends(auth)):
    if view == "schools":
        from app.ops import source_is_stale
        from app.pipeline.opsc import schools_board
        cfg = load_config()
        rows = schools_board(session, cfg, county=county or None, district=district or None,
                             program=program or None, status=status or None,
                             grade_level=grade_level or None)
        all_rows = schools_board(session, cfg)
        return templates.TemplateResponse(request, "board.html", {
            "view": view, "is_watchlist": False,
            "school_rows": rows,
            "school_counties": sorted({d["row"].county for d in all_rows if d["row"].county}),
            "school_districts": sorted({d["row"].district for d in all_rows if d["row"].district}),
            "school_programs": sorted({d["row"].program for d in all_rows if d["row"].program}),
            "school_statuses": sorted({d["row"].status for d in all_rows if d["row"].status}),
            "school_grade_levels": sorted({d["row"].grade_level_of_project for d in all_rows
                                          if d["row"].grade_level_of_project}),
            "school_county": county, "school_district": district, "school_program": program,
            "school_status": status, "school_grade_level": grade_level,
            "school_filter_stale": source_is_stale(session, cfg, "opsc_school_facility"),
            "tb": _title_block(session), "active": "board",
        })

    # Two boards, one pipeline. Defaults to data centers: that is the book of
    # business this system was built for, and industrial should never silently
    # dilute it. `?category=all` shows both.
    cat = _parse_category(category)
    base_conditions = [Project.status.in_(ACTIVE_STATUSES), Project.in_territory == True,
                       _category_filter(cat)]
    territory_conditions = base_conditions if territory == "all" else [*base_conditions, _my_territory_filter()]
    q = select(Project).where(*territory_conditions)
    projects = session.exec(q.order_by(Project.score.desc())).all()

    # call_target filter -- computed here (not a DB column), so it narrows
    # `projects` in Python, same as every other board filter narrows the SQL
    # query, before _board_extras builds the summary strip from whatever's
    # left. Chip counts (call_target_counts) are taken BEFORE this filter
    # narrows the list, same as the category chips' own counts are computed
    # independent of which category is currently selected.
    from app.call_target import (
        engineer_of_record_by_project, gc_by_project, nearby_contractor_by_project,
        project_call_target,
    )
    cfg = load_config()
    project_ids = [p.id for p in projects]
    eor_map = engineer_of_record_by_project(session, project_ids)
    gc_map = gc_by_project(session, project_ids)
    nearby_map = nearby_contractor_by_project(session, cfg, projects)
    call_targets = {
        p.id: project_call_target(cfg, p, engineer_of_record=eor_map.get(p.id),
                                  gc=gc_map.get(p.id), nearby_contractor=nearby_map.get(p.id))
        for p in projects
    }
    call_target_counts: dict[str, int] = {}
    for r in call_targets.values():
        call_target_counts[r.target.value] = call_target_counts.get(r.target.value, 0) + 1
    if call_target:
        projects = [p for p in projects if call_targets[p.id].target.value == call_target]
    # Every category gets a count, including esco — a chip whose count is hidden
    # is a category nobody will ever click. Counts respect the territory
    # filter too, so the chip numbers always match what clicking them shows.
    counts = {
        c.value: session.exec(
            select(func.count(Project.id)).where(
                Project.status.in_(ACTIVE_STATUSES),
                Project.in_territory == True,
                Project.category == c,
                *([] if territory == "all" else [_my_territory_filter()]))).one()
        for c in (*Category.boards(), Category.esco)
    }
    watch_count = session.exec(
        select(func.count(Project.id)).where(Project.status.in_(ACTIVE_STATUSES),
                                             Project.in_territory == False)).one()
    # How many this category's "mine" filter is currently hiding -- shown so
    # the toggle to "all" states what it reveals, not just that it exists.
    territory_hidden_count = session.exec(
        select(func.count(Project.id)).where(*base_conditions, ~_my_territory_filter())).one()
    days_since = {}
    for p in projects:
        days_since[p.id] = (utcnow() - p.last_signal_at).days if p.last_signal_at else None
    review_count = session.exec(
        select(func.count(MatchCandidate.id)).where(MatchCandidate.status == "pending")).one()
    # No PRE_BOD rows means the system has stopped doing its actual job — say so.
    has_pre_bod = any(p.window.value == "PRE_BOD" for p in projects)
    from app.coverage import pipeline_completeness
    completeness = pipeline_completeness(session, load_config())
    from app.field_intel import active_field_intel
    return templates.TemplateResponse(request, "board.html", {
        "view": view,
        "projects": projects, "days_since": days_since, "review_count": review_count,
        "has_pre_bod": has_pre_bod, "watch_count": watch_count, "is_watchlist": False,
        "completeness": completeness, "category": category, "cat_counts": counts,
        "territory": territory, "territory_hidden_count": territory_hidden_count,
        "field_intel": active_field_intel(session)[:5],
        "call_targets": call_targets, "call_target_counts": call_target_counts,
        "call_target": call_target,
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

    # Score breakdown text (hover) + spillover magnitude, batched -- one pass
    # over this board's signal links instead of two queries per row. See
    # app/pipeline/size_score.py:score_breakdown for what "why is 1.42 1.42"
    # actually decomposes into, and its docstring for why this is recomputed
    # rather than read back from a stored field.
    from app.pipeline.size_score import score_breakdown, signal_types_by_project
    from app.pipeline.spillover import spillover_factor
    cfg = load_config()
    spillover_enabled = cfg.get("scoring.spillover.enabled", False)
    signal_types = signal_types_by_project(session, [p.id for p in projects])
    score_breakdown_text: dict[int, str] = {}
    spillover_pct: dict[int, int] = {}
    for p in projects:
        tons_mid = ((p.tons_estimate_low + p.tons_estimate_high) / 2
                   if p.tons_estimate_low is not None and p.tons_estimate_high is not None else None)
        bd = score_breakdown(
            cfg, signal_types=signal_types.get(p.id, []), window=p.window,
            tons_midpoint=tons_mid, last_signal_at=p.last_signal_at,
            name=p.name, developer=p.developer, county=p.county,
            spillover_mw=p.spillover_mw, spillover_enabled=spillover_enabled,
        )
        score_breakdown_text[p.id] = bd.as_text()
        if spillover_enabled and p.spillover_mw:
            pct = round((spillover_factor(cfg, p.spillover_mw) - 1.0) * 100)
            if pct > 0:
                spillover_pct[p.id] = pct

    scores = sorted((p.score for p in projects), reverse=True)
    n = len(scores)
    score_median = scores[n // 2] if n % 2 else (scores[n // 2 - 1] + scores[n // 2]) / 2 if n else None

    signal_sparkline = _signal_sparklines(session, [p.id for p in projects])
    window_progress = _window_progress(projects)

    return {
        "contacts": contacts,
        "contact_statuses": statuses,
        "research_names": research_names,
        "stage_ages": ages,
        "stale": stale,
        "stale_months": threshold,
        "signal_sparkline": signal_sparkline,
        "window_progress": window_progress,
        # TO BID column tooltip: names the fixed per-stage constant behind
        # p.days_to_estimated_bid, so it never reads as a measured figure.
        "days_to_bid_by_stage": cfg.get("scoring.days_to_bid_by_stage", {}),
        "score_breakdown_text": score_breakdown_text,
        "spillover_pct": spillover_pct,
        "score_median": score_median,
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
            "delivery_method_known": sum(1 for p in projects if p.delivery_method),
        },
    }


SPARKLINE_WEEKS = 12


def _signal_sparklines(session: Session, project_ids: list[int]) -> dict[int, list[int]]:
    """Weekly signal-activity counts per project, oldest to newest, for the
    board's sparkline widget (app/web/templates/_widgets.html:sparkline).
    ONE query over every id on the current page, bucketed in Python -- not a
    per-row query, same discipline days_since/contacts/ages above already
    follow. Bucketing in Python rather than SQL because the bucket boundary
    math (week-aligned, relative to "now") would otherwise need a dialect-
    specific date-trunc, and this app runs on both sqlite (tests) and
    Postgres (production)."""
    if not project_ids:
        return {}
    now = utcnow()
    window_start = now - timedelta(weeks=SPARKLINE_WEEKS)
    rows = session.exec(
        select(ProjectSignal.project_id, ProjectSignal.linked_at)
        .where(ProjectSignal.project_id.in_(project_ids), ProjectSignal.linked_at >= window_start)
    ).all()
    buckets: dict[int, list[int]] = {pid: [0] * SPARKLINE_WEEKS for pid in project_ids}
    for project_id, linked_at in rows:
        age_weeks = (now - linked_at).days // 7
        idx = SPARKLINE_WEEKS - 1 - age_weeks
        if 0 <= idx < SPARKLINE_WEEKS:
            buckets[project_id][idx] += 1
    return buckets


def _window_progress(projects: list[Project]) -> dict[int, int]:
    """How far a project sits inside its estimated bid window, 0-100, for
    the board's window-remaining bar (app/web/templates/_widgets.html:
    window_bar). Only computed for rows with a REAL measured low/high CI
    (days_to_estimated_bid_range) -- every other stage is a fixed point
    constant with no interval, and drawing a bar against a fabricated range
    would claim precision that number doesn't have.

    The window is anchored at Project.created_at (when this project was
    first tracked) through created_at + days_to_estimated_bid_high -- the
    only start date this system actually has. That is a stated judgment
    call, not a literal stored bid-window date: see the "Window-remaining
    bar" entry in app/assumptions.py for the full disclosure."""
    from app.pipeline.scoring import days_to_estimated_bid_range
    cfg = load_config()
    out: dict[int, int] = {}
    now = utcnow()
    for p in projects:
        low, high = days_to_estimated_bid_range(cfg, p.stage)
        if low is None or high is None or high <= 0:
            continue
        elapsed_days = (now - p.created_at).days
        pct = round(100 * elapsed_days / high)
        out[p.id] = max(0, min(100, pct))
    return out


@app.get("/retrofit", response_class=HTMLResponse)
def retrofit_board(request: Request, county: str = None, min_status: str = None,
                   population: str = "replacement_candidate", limit: int = 200,
                   has_ebewe: bool = False, sold_last_24mo: bool = False, territory: str = "mine",
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
    Both a SEPARATE population from /board (zero APN overlap, confirmed).

    territory defaults to "mine" -- an explicit county always wins (a rep
    picking a specific county from the dropdown means it), otherwise "mine"
    defaults to MY_TERRITORY_COUNTIES (same literal association /board's
    territory filter uses), and "all" applies no default. All of
    retrofit_buildings is Los Angeles county today (2026-08-21 -- confirmed
    by querying every distinct county on file), so this currently changes
    nothing in practice; it exists for when other counties' retrofit data
    is added."""
    effective_county = county
    if effective_county is None and territory == "mine":
        effective_county = next(iter(MY_TERRITORY_COUNTIES)).title()
    base_q = select(RetrofitBuilding).where(RetrofitBuilding.population == population)
    if effective_county:
        base_q = base_q.where(RetrofitBuilding.county == effective_county)
    territory_hidden_count = 0
    if territory == "mine" and county is None:
        territory_hidden_count = session.exec(
            select(func.count(RetrofitBuilding.id)).where(
                RetrofitBuilding.population == population,
                RetrofitBuilding.county != effective_county)).one()
    STATUS_ORDER = ["overdue", "due", "approaching", "not_due"]
    if min_status and min_status in STATUS_ORDER:
        base_q = base_q.where(RetrofitBuilding.service_life_status.in_(
            STATUS_ORDER[:STATUS_ORDER.index(min_status) + 1]))

    # A call list should not contain rows that can't be called (2026-08-16):
    # a no-address row can still rank near the top on urgency/size alone
    # (one sat at #9 in the top 10) with no way to act on it. Excluded from
    # the ranked view/total entirely, not just sorted last -- counted and
    # disclosed below (no_address_total) the same way never-matched
    # contractors are on /contractors, never silently dropped from
    # existence.
    #
    # Coverage is measured against this population BEFORE has_ebewe is
    # applied -- the honest denominator is "this view, before you asked to
    # see only the covered subset". Measuring against the has_ebewe-filtered
    # query instead would always read 100%. total/summary.n (below) DOES
    # reflect has_ebewe/sold_last_24mo, same as it already reflects
    # county/min_status -- "showing top N of TOTAL" should describe the
    # population actually being paged through.
    #
    # These six figures used to be six separate COUNT/MAX queries. Each one
    # is a full round trip to the (remote, Render-hosted) database plus its
    # own table scan -- measured 2026-08-17 at 4.4s combined for a single
    # county-filtered request, the largest piece of a 12s page load. One
    # conditional-aggregation query computes all six in a single scan and a
    # single round trip instead (measured: ~2.2s). Same sparse-coverage
    # discipline as EBEWE for sold_24mo_covered -- see app/assumptions.py's
    # "Retrofit ranking: ownership-change recency" entry for why that's a
    # filter, not a rank_buildings() term. max_recording_date exists because
    # the Assessor's RecordingDate feed itself lags real time -- confirmed
    # 2026-08-16 by querying every recording date on file: the newest one in
    # the ENTIRE 59,525-row source table is from 2025-03-13, not "recently".
    # "Sold in last 24 months" is bounded by whenever this feed was last
    # refreshed, not by today -- shown here so a cluster of hits all landing
    # in one stale quarter reads as a data property, not a ranking artifact.
    valid_address = and_(RetrofitBuilding.address.is_not(None), RetrofitBuilding.address != "")
    sold_24mo_cutoff = utcnow() - timedelta(days=730)
    total_conditions = [valid_address]
    if has_ebewe:
        total_conditions.append(RetrofitBuilding.ebewe_matched.is_(True))
    if sold_last_24mo:
        total_conditions.append(RetrofitBuilding.last_sale_date >= sold_24mo_cutoff)
    total_when = and_(*total_conditions) if len(total_conditions) > 1 else total_conditions[0]

    no_address_total, unfiltered_total, ebewe_covered, sold_24mo_covered, max_recording_date, total = session.execute(
        base_q.with_only_columns(
            func.sum(case((~valid_address, 1), else_=0)),
            func.sum(case((valid_address, 1), else_=0)),
            func.sum(case((and_(valid_address, RetrofitBuilding.ebewe_matched.is_(True)), 1), else_=0)),
            func.sum(case((and_(valid_address, RetrofitBuilding.last_sale_date >= sold_24mo_cutoff), 1), else_=0)),
            func.max(case((valid_address, RetrofitBuilding.last_sale_date))),
            func.sum(case((total_when, 1), else_=0)),
        )
    ).one()
    no_address_total = no_address_total or 0
    unfiltered_total = unfiltered_total or 0
    ebewe_covered = ebewe_covered or 0
    sold_24mo_covered = sold_24mo_covered or 0
    total = total or 0

    base_q = base_q.where(valid_address)
    ranked_q = base_q
    if has_ebewe:
        ranked_q = ranked_q.where(RetrofitBuilding.ebewe_matched.is_(True))
    if sold_last_24mo:
        ranked_q = ranked_q.where(RetrofitBuilding.last_sale_date >= sold_24mo_cutoff)
    order = [RetrofitBuilding.rank_score.desc().nulls_last()]
    if has_ebewe:
        # Energy performance is a TIE-BREAKER within this covered subset only
        # -- never a rank_buildings() term, never applied board-wide (see
        # app/assumptions.py's "Benchmark-to-building join method and
        # coverage" entry for why: coverage is ~8% board-wide, and blending a
        # column that sparse into a global score is exactly the false-
        # precision problem that register exists to flag). A worse (lower)
        # ENERGY STAR score is direct evidence of an underperforming
        # mechanical plant -- YearBuilt only infers that; this is measured.
        order.append(RetrofitBuilding.ebewe_energy_star_score.asc().nulls_last())
    if sold_last_24mo:
        # Same discipline: recency is a TIE-BREAKER within this filtered
        # subset only, never board-wide -- see app/assumptions.py's
        # "Retrofit ranking: ownership-change recency" entry.
        order.append(RetrofitBuilding.last_sale_date.desc().nulls_last())
    # Portfolio siblings (app/portfolios.py) collapse to their single
    # highest-ranked member -- "a single lead with multiple properties, not
    # separate rows" per instruction. Queried with headroom (portfolio
    # groups cap at PORTFOLIO_MAX_GROUP_SIZE members) since collapsing
    # happens AFTER the query, so `limit` rows alone could under-fill the
    # page by however many were absorbed into an earlier anchor.
    from app.portfolios import PORTFOLIO_MAX_GROUP_SIZE
    raw = session.exec(ranked_q.order_by(*order).limit(limit + limit * PORTFOLIO_MAX_GROUP_SIZE // 10)).all()
    buildings = []
    seen_groups: set[str] = set()
    for b in raw:
        if b.portfolio_group_id:
            if b.portfolio_group_id in seen_groups:
                continue  # already shown via an earlier (higher-ranked) anchor
            seen_groups.add(b.portfolio_group_id)
        buildings.append(b)
        if len(buildings) >= limit:
            break

    # Column-only, not select(RetrofitBuilding) -- this used to load all 53,252
    # full ORM rows (every column, including long basis-text fields) just to
    # read .county off each one. Measured (tracemalloc, 2026-08-13): 236.4MB
    # for the full-row version against 0.02MB for this one, identical result.
    # That allocation, repeated across a couple of hits with no OS-level
    # release between them, is what was OOM-killing the 512MB web instance.
    counties = sorted(session.exec(
        select(RetrofitBuilding.county).where(RetrofitBuilding.population == population).distinct()).all())
    # Mobile card view's tel: link (app/web/templates/retrofit_board.html) --
    # one nearest-mechanical-contractor lookup for the whole page, not one
    # per row. See app.contractors.nearest_mechanical_contractor_bulk's own
    # docstring for the measured cost this was built to fit inside.
    from app.contractors import default_radius_miles, nearest_mechanical_contractor_bulk
    nearest_contractor = nearest_mechanical_contractor_bulk(
        session, buildings, radius_miles=default_radius_miles(load_config()))
    summary = {
        "n": total, "shown": len(buildings),
        "overdue": sum(1 for b in buildings if b.service_life_status == "overdue"),
        "due": sum(1 for b in buildings if b.service_life_status == "due"),
        "sb1206": sum(1 for b in buildings if b.sb1206_trigger_status),
        "carb": sum(1 for b in buildings if b.carb_candidate),
        "ebewe": sum(1 for b in buildings if b.ebewe_candidate),
        "ebewe_covered": ebewe_covered,
        "ebewe_covered_of": unfiltered_total,
        "ebewe_covered_pct": round(100 * ebewe_covered / unfiltered_total, 1) if unfiltered_total else 0.0,
        "no_address_total": no_address_total,
        "sold_24mo_covered": sold_24mo_covered,
        "sold_24mo_covered_of": unfiltered_total,
        "sold_24mo_covered_pct": round(100 * sold_24mo_covered / unfiltered_total, 1) if unfiltered_total else 0.0,
        "max_recording_date": max_recording_date,
    }
    return templates.TemplateResponse(request, "retrofit_board.html", {
        "buildings": buildings, "summary": summary, "counties": counties,
        "county": effective_county, "min_status": min_status, "population": population, "limit": limit,
        "has_ebewe": has_ebewe, "sold_last_24mo": sold_last_24mo,
        "territory": territory, "territory_hidden_count": territory_hidden_count,
        "score_max": max([b.rank_score for b in buildings if b.rank_score] or [1.0]),
        "nearest_contractor": nearest_contractor,
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
    fact traces to a public record. No-address rows are excluded, same as
    the board itself: a call list should not contain a row that can't be
    called."""
    q = select(RetrofitBuilding).where(RetrofitBuilding.population == population,
                                       RetrofitBuilding.address.is_not(None), RetrofitBuilding.address != "")
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


@app.get("/retrofit/building/{building_id}", response_class=HTMLResponse)
def retrofit_building_detail(building_id: int, request: Request,
                             session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.contractors import default_radius_miles, nearest_mechanical_contractors
    building = session.get(RetrofitBuilding, building_id)
    if building is None:
        raise HTTPException(404)
    cfg = load_config()
    radius = default_radius_miles(cfg)
    nearest = nearest_mechanical_contractors(session, building, radius_miles=radius)
    return templates.TemplateResponse(request, "retrofit_building_detail.html", {
        "building": building, "nearest_contractors": nearest, "radius_miles": radius,
        "tb": _title_block(session), "active": "retrofit",
    })


# ---- hospitals (HCAI / SB 1953) -------------------------------------------
#
# A SEPARATE population from the project board and from retrofit -- a
# hospital facilities/capital team retrofitting or replacing a building
# under a statutory seismic deadline is a different sale, to a different
# buyer, than either. See app.models.HospitalBuilding's own docstring and
# app/pipeline/hcai.py for the source, the CHHS Terms of Use findings, and
# the SPC/NPC deadline derivation.

@app.get("/hospitals/brief", response_class=HTMLResponse)
def hospitals_brief(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """Printable one-page brief -- territory coverage, what NPC-5 requires
    (quoted from HCAI, not paraphrased), the covered-vs-gap OSP line
    breakdown with numbers and expiration dates, and contractor
    reachability. No dollar estimate -- see app/pipeline/hcai.py's
    HOSPITAL_BRIEF_OSP_FACTS docstring and app/assumptions.py for why."""
    from app.pipeline.hcai import (
        hospital_contractor_reachability, hospital_osp_breakdown, hospital_seismic_coverage,
    )
    cfg = load_config()
    return templates.TemplateResponse(request, "hospitals_brief.html", {
        "coverage": hospital_seismic_coverage(session, cfg),
        "reach": hospital_contractor_reachability(session, cfg),
        "osp": hospital_osp_breakdown(session),
        "generated_at": utcnow(),
        "tb": _title_block(session), "active": "hospitals",
    })


@app.get("/hospitals", response_class=HTMLResponse)
def hospitals_board(request: Request, county: str = None, deadline: str = None, all_ca: bool = False,
                    session: Session = Depends(get_session), _: str = Depends(auth)):
    """deadline: "2020" | "2030" | "extension" | None (all). See
    app/pipeline/hcai.py's SPC1_DEADLINE_YEAR/SPC2_DEADLINE_YEAR.

    Scoped to Scout's territory counties by default (all_ca=0) -- HCAI's own
    dataset is genuinely statewide (58 counties), and this is a sales board
    for a 7-county territory, not a browse-all-California tool. all_ca=1
    lifts the scope for the rare case someone wants to see the rest."""
    from app.pipeline.hcai import (
        NPC5_DEADLINE_YEAR,
        SPC1_DEADLINE_YEAR,
        SPC2_DEADLINE_YEAR,
        hospital_capability_gaps,
        hospital_seismic_coverage,
    )
    cfg = load_config()
    territory_counties = set(cfg.get("territories.california.counties", []))

    q = select(HospitalBuilding)
    if county:
        q = q.where(HospitalBuilding.county == county)
    elif not all_ca:
        q = q.where(HospitalBuilding.county.in_(territory_counties))
    if deadline == "2020":
        q = q.where(HospitalBuilding.spc_deadline_year == SPC1_DEADLINE_YEAR)
    elif deadline == "2030":
        q = q.where(HospitalBuilding.spc_deadline_year == SPC2_DEADLINE_YEAR)
    elif deadline == "extension":
        q = q.where(HospitalBuilding.has_filed_extension == True)  # SQLAlchemy needs literal == True, not `is True`

    buildings = session.exec(
        q.order_by(HospitalBuilding.spc_deadline_year.asc().nulls_last(),
                  HospitalBuilding.county, HospitalBuilding.facility_name)
        .limit(500)
    ).all()
    total = session.exec(select(func.count()).select_from(q.subquery())).one()

    counties = sorted(territory_counties) if not all_ca else sorted(
        session.exec(select(HospitalBuilding.county).distinct()).all())
    # County-level construction-activity context (already-imported aggregate
    # layer, app.pipeline.hcai's OTHER dataset) -- shown as context, not
    # re-imported and not merged row-by-row (different grain: one row per
    # county+status here, one row per building above).
    county_activity: dict[str, list[HcaiCountyActivity]] = {}
    if county:
        rows = session.exec(
            select(HcaiCountyActivity).where(HcaiCountyActivity.county == county)).all()
        if rows:
            county_activity[county] = rows

    return templates.TemplateResponse(request, "hospitals.html", {
        "buildings": buildings, "total": total, "counties": counties,
        "county": county, "deadline": deadline, "all_ca": all_ca, "county_activity": county_activity,
        "coverage": hospital_seismic_coverage(session, cfg),
        "capability_gaps": hospital_capability_gaps(session),
        "spc1_year": SPC1_DEADLINE_YEAR, "spc2_year": SPC2_DEADLINE_YEAR, "npc5_year": NPC5_DEADLINE_YEAR,
        "territory_counties": territory_counties,
        "tb": _title_block(session), "active": "hospitals",
    })


@app.get("/hospitals/building/{building_id}", response_class=HTMLResponse)
def hospital_building_detail(building_id: int, request: Request,
                             session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.pipeline.hcai import hospital_capability_gaps

    building = session.get(HospitalBuilding, building_id)
    if building is None:
        raise HTTPException(404)
    siblings = session.exec(
        select(HospitalBuilding).where(HospitalBuilding.perm_id == building.perm_id,
                                       HospitalBuilding.id != building.id)
        .order_by(HospitalBuilding.building_name)
    ).all()
    county_activity = session.exec(
        select(HcaiCountyActivity).where(HcaiCountyActivity.county == building.county)).all()
    return templates.TemplateResponse(request, "hospital_building_detail.html", {
        "b": building, "siblings": siblings, "county_activity": county_activity,
        "capability_gaps": hospital_capability_gaps(session),
        "tb": _title_block(session), "active": "hospitals",
    })


@app.get("/ab869", response_class=HTMLResponse)
def ab869_board(request: Request, county: str = None, plan_status: str = None,
                has_missed: bool = False, upcoming_12mo: bool = False, owner: str = "",
                session: Session = Depends(get_session), _: str = Depends(auth)):
    """One row per in-territory facility -- the AB 869 seismic compliance
    plan roster. See app.pipeline.ab869's module docstring for the access/
    parsing investigation this rests on. Filtered in Python against
    ab869_board_rows' own small (~200-row) list, not pushed into SQL --
    see that function's own docstring for why."""
    from datetime import timedelta

    from app.pipeline.ab869 import ab869_board_rows
    cfg = load_config()
    rows = ab869_board_rows(session, cfg)

    counties = sorted({r["county"] for r in rows if r["county"]})
    plan_statuses = sorted({r["plan_status"] for r in rows if r["plan_status"]})

    if county:
        rows = [r for r in rows if r["county"] == county]
    if plan_status:
        rows = [r for r in rows if r["plan_status"] == plan_status]
    if has_missed:
        rows = [r for r in rows if r["missed_milestone_count"] > 0]
    if upcoming_12mo:
        cutoff = utcnow() + timedelta(days=365)
        rows = [r for r in rows if r["next_upcoming_date"] and r["next_upcoming_date"] <= cutoff]
    if owner:
        # Case-insensitive SUBSTRING match, deliberately -- "kaiser" must
        # find every real spelling HCAI's own crosstab carries ("Kaiser
        # Permanente Foundation", "Kaiser Permanente Foundation Hospital",
        # "KAISER PERMANENTE FOUNDATION", "Kaiser Foundation Hospital"),
        # since merging those spellings into one canonical entity would be
        # exactly the kind of inference this whole feature avoids.
        needle = owner.lower()
        rows = [r for r in rows if r["financially_responsible_party"]
               and needle in r["financially_responsible_party"].lower()]

    return templates.TemplateResponse(request, "ab869_board.html", {
        "rows": rows, "counties": counties, "plan_statuses": plan_statuses,
        "county": county, "plan_status": plan_status, "has_missed": has_missed,
        "upcoming_12mo": upcoming_12mo, "owner": owner,
        "tb": _title_block(session), "active": "hospitals",
    })


@app.get("/ab869/{perm_id}", response_class=HTMLResponse)
def ab869_facility(perm_id: str, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.contractors import default_radius_miles, nearest_mechanical_contractors
    from app.pipeline.ab869 import ab869_facility_detail, hcai_tableau_url

    detail = ab869_facility_detail(session, perm_id)
    if detail["plan"] is None and not detail["buildings"] and not detail["hospital_buildings"]:
        raise HTTPException(404)

    cfg = load_config()
    geocoded = [hb for hb in detail["hospital_buildings"].values()
               if hb.latitude is not None and hb.longitude is not None]
    nearby_contractors = []
    if geocoded:
        # A synthetic point at the facility's own average lat/lon -- a
        # hospital campus is a tiny fraction of the 15-mile radius, so any
        # one of its buildings (or their average) is a fine stand-in for
        # "the facility's location". nearest_mechanical_contractors only
        # ever reads .latitude/.longitude off what it's given.
        from types import SimpleNamespace
        avg_lat = sum(hb.latitude for hb in geocoded) / len(geocoded)
        avg_lon = sum(hb.longitude for hb in geocoded) / len(geocoded)
        fake_point = SimpleNamespace(latitude=avg_lat, longitude=avg_lon)
        nearby_contractors = nearest_mechanical_contractors(
            session, fake_point, radius_miles=default_radius_miles(cfg))

    from app.call_target import ab869_call_target
    call_target = ab869_call_target(cfg, detail["plan"], detail["facility_name"], nearby_contractors)

    return templates.TemplateResponse(request, "ab869_facility.html", {
        **detail,
        "tableau_url": hcai_tableau_url(perm_id, detail["facility_name"]),
        "nearby_contractors": nearby_contractors,
        "proximity_radius": default_radius_miles(cfg),
        "call_target": call_target,
        "tb": _title_block(session), "active": "hospitals",
    })


@app.get("/contractors", response_class=HTMLResponse)
def contractors_list(request: Request, county: str = None, classification: str = "mechanical",
                     signatory: str = "", limit: int = 200,
                     session: Session = Depends(get_session), _: str = Depends(auth)):
    """Ranked by nearby_urgency_score -- precomputed by `scout
    match-contractors` (see app.contractors.match_contractors), not
    computed live: a per-request N-contractors x M-buildings join does not
    scale at this row count. Raw proximity count alone doesn't discriminate
    (measured 2026-08-15: top 10 by count spanned 1,430-1,456, under 2%, in
    a single dense pocket of southeast LA County) so the sort is aggregate
    service-life urgency, with aggregate estimated tonnage as a tie-breaker
    -- see Contractor's nearby_* field docstrings. Contractors never
    geocoded, or never matched, sort last (nulls_last), shown with an
    explicit note rather than silently mixed in as if they scored zero.

    classification defaults to "mechanical" (C-20/C-38 only) -- unfiltered,
    43 of the top 50 by any ranking were plain "B" (general building)
    licenses, not a call list for a mechanical rep. "all" widens to every
    classification CSLB scope covers; anything else (e.g. "B", "C10")
    substring-filters same as before, for a link naming one specific
    classification."""
    from app.contractors import MATCH_CONTRACTORS_SOURCE, MECHANICAL_CLASSIFICATIONS, ranking_radius_miles
    from app.ops import source_is_stale
    cfg = load_config()
    ranking_stale = source_is_stale(session, cfg, MATCH_CONTRACTORS_SOURCE)
    q = select(Contractor)
    if county:
        q = q.where(Contractor.county == county)
    if classification == "mechanical":
        q = q.where(or_(*(Contractor.classifications.contains(c) for c in MECHANICAL_CLASSIFICATIONS)))
    elif classification and classification != "all":
        q = q.where(Contractor.classifications.contains(classification))
    if signatory == "ua_local_250":
        q = q.where(Contractor.ua_local_250_signatory == True)
    total = session.exec(select(func.count()).select_from(q.subquery())).one()
    contractors = session.exec(
        q.order_by(Contractor.nearby_urgency_score.desc().nulls_last(),
                   Contractor.nearby_estimated_tons.desc().nulls_last())
        .limit(limit)).all()
    counties = sorted({c for c in session.exec(select(Contractor.county).distinct()).all() if c})
    never_matched = session.exec(
        select(func.count()).where(Contractor.nearby_computed_at.is_(None))).one()
    signatory_total = session.exec(
        select(func.count()).where(Contractor.ua_local_250_signatory == True)).one()
    signatory_checked_at = session.exec(
        select(func.max(Contractor.ua_local_250_checked_at))).one()
    return templates.TemplateResponse(request, "contractors.html", {
        "contractors": contractors, "total": total, "counties": counties,
        "county": county, "classification": classification, "signatory": signatory, "limit": limit,
        "never_matched": never_matched, "ranking_radius": ranking_radius_miles(cfg),
        "signatory_total": signatory_total, "signatory_checked_at": signatory_checked_at,
        "ranking_stale": ranking_stale,
        "tb": _title_block(session), "active": "contractors",
    })


@app.get("/contractor/{contractor_id}", response_class=HTMLResponse)
def contractor_detail(contractor_id: int, request: Request,
                      session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.contractors import nearby_replacement_candidates, ranking_radius_miles
    contractor = session.get(Contractor, contractor_id)
    if not contractor:
        raise HTTPException(404)
    cfg = load_config()
    radius = ranking_radius_miles(cfg)
    nearby = (nearby_replacement_candidates(session, contractor, radius)
             if contractor.latitude is not None else [])
    return templates.TemplateResponse(request, "contractor.html", {
        "c": contractor, "nearby": nearby[:10], "nearby_total": len(nearby), "radius_miles": radius,
        "tb": _title_block(session), "active": "contractors",
    })


@app.get("/replacement-leads", response_class=HTMLResponse)
def replacement_leads_view(request: Request, view: str = "contractors",
                           min_overdue: int = 5, limit: int = 100,
                           county: str = "", property_type: str = "", show_all_types: bool = False,
                           year_built_before: int | None = None,
                           eui_above_median: bool = False, has_assessor_match: bool = False,
                           session: Session = Depends(get_session), _: str = Depends(auth)):
    """Two tabs, one page, selected by `view` (chip nav, same pattern
    /board's category/territory chips already use):

    view=contractors (default) -- the owner-direct lane: mechanical
    contractors ranked by nearby_urgency_score at ranking_radius_miles (see
    app.contractors.replacement_leads for why — raw overdue count doesn't
    discriminate any more than raw proximity count did on /contractors),
    each showing an overdue count at the WIDER default_radius_miles (15mi,
    "realistically reachable") — two different radii for two different
    questions, see app.contractors.match_contractors_overdue's own
    docstring. Shows only precomputed, cached stats per row (overdue
    count, urgency, Local 250, CSLB status) — deliberately NOT the
    nearest-few building list live, per row, for up to `limit` contractors
    at once: a dense-area contractor's candidate set is large enough
    (1,000+ buildings within radius, measured against production
    2026-08-24) that even a batched single-query version of that lookup
    ran over a minute for 100 rows. That detail lives one click away, on
    the per-contractor printable handout, the same way /contractors keeps
    its own board to cached aggregates and defers live detail to
    /contractor/{id}.

    view=ab802 -- one row per in-territory AB 802 building (latest year on
    file), ranked by app.pipeline.ab802:rank_in_territory. See that
    function's docstring for the rank (older + higher EUI, within its own
    property type, shown as plain percentile columns, never a black-box
    score). Defaults to DMG_RELEVANT_PROPERTY_TYPES only (office, medical
    office, hospital, K-12 school, college/university, laboratory, data
    center, warehouse/distribution, manufacturing/industrial, hotel,
    retail, multifamily) -- show_all_types=true or an explicit
    property_type both bypass that default, never lose access to the
    excluded types (golf/country club, casino, worship, parking, and
    similar catch-alls), just not first."""
    cfg = load_config()

    leads = distribution = None
    never_scored = ranking_stale = count_stale = None
    ranking_radius = count_radius = None
    ab802_rows = ab802_anomalies = ab802_counties = ab802_property_types = None
    ab802_filter_stale = None
    ab802_retrofit_id_by_apn = {}

    if view == "ab802":
        from app.ops import source_is_stale
        from app.pipeline.ab802 import latest_in_territory_rows, rank_in_territory
        ab802_result = rank_in_territory(
            session, cfg, county=county or None, property_type=property_type or None,
            year_built_before=year_built_before, eui_above_median=eui_above_median,
            has_assessor_match=has_assessor_match,
            restrict_to_relevant_types=not show_all_types)
        ab802_rows = ab802_result["ranked"]
        ab802_anomalies = ab802_result["anomalies"]
        all_rows = latest_in_territory_rows(session)
        ab802_counties = sorted({r.county_from_geocoding for r in all_rows if r.county_from_geocoding})
        ab802_property_types = sorted({r.primary_property_type for r in all_rows if r.primary_property_type})
        ab802_filter_stale = source_is_stale(session, cfg, "ab802_benchmarking")
        matched_apns = [d["row"].retrofit_apn for d in ab802_rows + ab802_anomalies if d["row"].retrofit_apn]
        ab802_retrofit_id_by_apn = dict(session.exec(
            select(RetrofitBuilding.apn, RetrofitBuilding.id)
            .where(RetrofitBuilding.apn.in_(matched_apns))).all()) if matched_apns else {}
    else:
        from app.contractors import (
            MATCH_CONTRACTORS_OVERDUE_SOURCE,
            MATCH_CONTRACTORS_SOURCE,
            default_radius_miles,
            ranking_radius_miles,
            replacement_lead_distribution,
            replacement_leads,
        )
        from app.ops import source_is_stale
        leads = replacement_leads(session, min_overdue=min_overdue, limit=limit)
        distribution = replacement_lead_distribution(session)
        never_scored = session.exec(
            select(func.count()).where(Contractor.latitude.is_not(None),
                                       Contractor.nearby_overdue_count.is_(None))).one()
        ranking_stale = source_is_stale(session, cfg, MATCH_CONTRACTORS_SOURCE)
        count_stale = source_is_stale(session, cfg, MATCH_CONTRACTORS_OVERDUE_SOURCE)
        ranking_radius = ranking_radius_miles(cfg)
        count_radius = default_radius_miles(cfg)

    return templates.TemplateResponse(request, "replacement_leads.html", {
        "view": view,
        "leads": leads, "min_overdue": min_overdue, "limit": limit,
        "ranking_radius_miles": ranking_radius, "count_radius_miles": count_radius,
        "mechanical_total": distribution["total_scored"] if distribution else None,
        "distribution": distribution["at_threshold"] if distribution else None,
        "never_scored": never_scored,
        "ranking_stale": ranking_stale, "count_stale": count_stale,
        "ab802_rows": ab802_rows, "ab802_anomalies": ab802_anomalies, "ab802_counties": ab802_counties,
        "ab802_property_types": ab802_property_types, "ab802_filter_stale": ab802_filter_stale,
        "ab802_retrofit_id_by_apn": ab802_retrofit_id_by_apn,
        "county": county, "property_type": property_type, "show_all_types": show_all_types,
        "year_built_before": year_built_before,
        "eui_above_median": eui_above_median, "has_assessor_match": has_assessor_match,
        "tb": _title_block(session), "active": "replacement-leads",
    })


@app.get("/replacement-leads/{contractor_id}/handout", response_class=HTMLResponse)
def replacement_lead_handout(contractor_id: int, request: Request,
                             session: Session = Depends(get_session), _: str = Depends(auth)):
    """Printable per-contractor handout -- see replacement_lead_handout.html
    for the print stylesheet, same discipline account_brief.html's already
    uses. Uses the radius this contractor's OWN cached nearby_overdue_count
    was computed at, not necessarily the current config default, so the
    list here always matches the count shown on /replacement-leads for the
    same contractor even if the config changes between precompute runs."""
    from app.contractors import default_radius_miles, overdue_buildings_near_contractor_detail
    contractor = session.get(Contractor, contractor_id)
    if not contractor:
        raise HTTPException(404)
    radius = contractor.nearby_overdue_radius_miles or default_radius_miles(load_config())
    nearby = (overdue_buildings_near_contractor_detail(session, contractor, radius)
             if contractor.latitude is not None else [])
    return templates.TemplateResponse(request, "replacement_lead_handout.html", {
        "c": contractor, "nearby": nearby, "radius_miles": radius,
        "tb": _title_block(session), "active": "replacement-leads",
    })


@app.get("/contractor/{contractor_id}/precall", response_class=HTMLResponse)
def contractor_precall(contractor_id: int, request: Request, refresh: bool = False,
                       session: Session = Depends(get_session), _: str = Depends(auth)):
    """Read-only pre-call brief -- see app/precall.py's module docstring for
    the hard constraint this whole feature is built around (never writes to
    Postgres, cache lives on local disk). refresh=1 bypasses the cache."""
    from app.precall import PrecallUnavailable, brief_sections
    from app.precall import pre_call_brief as _pre_call_brief
    from app.spend import BudgetExceeded

    contractor = session.get(Contractor, contractor_id)
    if not contractor:
        raise HTTPException(404)
    try:
        entry = _pre_call_brief(session, "contractor", contractor_id, force_refresh=refresh)
    except (PrecallUnavailable, BudgetExceeded) as exc:
        entry = None
        error = str(exc)
    else:
        error = None
    return templates.TemplateResponse(request, "precall_brief.html", {
        "entry": entry, "sections": brief_sections(entry["text"]) if entry else None,
        "error": error, "entity_type": "contractor", "entity_id": contractor_id,
        "back_url": f"/contractor/{contractor_id}", "title": contractor.business_name,
        "tb": _title_block(session), "active": "contractors",
    })


@app.get("/watchlist", response_class=HTMLResponse)
def watchlist(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """Out-of-territory projects — checked deliberately, never crowding the board."""
    projects = session.exec(
        select(Project).where(Project.status.in_(ACTIVE_STATUSES),
                              Project.in_territory == False)
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

    from app.call_target import (
        CallTarget, ENGINEER_OF_RECORD_ROLE, GC_ROLE, nearby_contractor_by_project,
        project_call_target,
    )
    eor = next((f["name"] for f in resolved_firms if f["role"] == ENGINEER_OF_RECORD_ROLE), None)
    gc_name = next((f["name"] for f in resolved_firms if f["role"] == GC_ROLE), None)
    nearby_map = nearby_contractor_by_project(session, load_config(), [project])
    call_target = project_call_target(load_config(), project, engineer_of_record=eor,
                                      gc=gc_name, nearby_contractor=nearby_map.get(project.id))

    # "Usual team" is a lead, never this project's engineer -- only shown when
    # the call target is R4 engineer and no ProjectFirm has actually named one
    # for THIS project. See app.developer_team's module docstring.
    usual_team = None
    if (call_target.target == CallTarget.engineer
            and call_target.who_label == "engineer of record unknown" and project.developer):
        from app.developer_team import usual_team_for_developer
        usual_team = usual_team_for_developer(session, project.developer)

    timeline = []
    people, firms = [], []
    signals = []
    for link in links:
        s = session.get(Signal, link.signal_id)
        if not s:
            continue
        doc = session.get(RawDocument, s.raw_document_id) if s.raw_document_id else None
        timeline.append({"signal": s, "doc": doc, "link": link})
        people.extend(s.named_people or [])
        firms.extend(s.named_firms or [])
        signals.append(s)
    timeline.sort(key=lambda t: t["signal"].event_date or t["signal"].created_at, reverse=True)

    # Reverse of /line/{id}'s matching_projects_for_line: given THIS
    # project's building type, what does the line card offer by role, and
    # where does it offer nothing at all. Knowing the gap matters as much
    # as knowing the fit -- see app/accounts.py:line_offering_by_role.
    from app.accounts import SOCAL_CARD_DISCLOSURE, line_offering_by_role, project_facility_type
    facility_type = project_facility_type(project, signals)
    role_offerings = line_offering_by_role(session, project.category, facility_type)

    from app.competitors import competing_lines_by_role
    from app.models import RepFirm
    competing_by_role = competing_lines_by_role(session)
    competitor_rep_firms = {f.id: f for f in session.exec(select(RepFirm)).all()}

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
    from app.pipeline.size_score import project_score_breakdown
    score_breakdown = project_score_breakdown(session, load_config(), project)

    documents = session.exec(
        select(ProjectDocument).where(ProjectDocument.project_id == project_id)
        .order_by(ProjectDocument.uploaded_at.desc())).all()
    doc_entries = {
        doc.id: session.exec(
            select(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id)
            .order_by(ScheduleEntry.tag)).all()
        for doc in documents
    }
    from app.schedule_mapping import (
        actionable,
        displaceable,
        map_project_schedule_to_line_card,
        resolve_displacement,
        role_gaps,
    )
    schedule_mapping = map_project_schedule_to_line_card(session, project_id)
    actionable_mapping = actionable(schedule_mapping)
    displacement_rows = resolve_displacement(session, project, schedule_mapping)
    displaceable_rows = displaceable(displacement_rows)
    role_gap_rows = role_gaps(displacement_rows)

    return templates.TemplateResponse(request, "project.html", {
        "p": project, "timeline": timeline, "people": people, "firms": firms,
        "resolved_firms": resolved_firms, "outcome_statuses": OUTCOME_STATUSES,
        "ladder": ladder,
        "outreach": outreach, "contacts": contacts,
        "stage_progression": stage_progression, "stage_age": stage_age,
        "stale_months": stale_months, "score_breakdown": score_breakdown,
        "role_offerings": role_offerings, "facility_type": facility_type,
        "socal_card_disclosure": SOCAL_CARD_DISCLOSURE,
        "competing_by_role": competing_by_role, "competitor_rep_firms": competitor_rep_firms,
        "documents": documents, "doc_entries": doc_entries,
        "schedule_mapping": schedule_mapping, "actionable_mapping": actionable_mapping,
        "displaceable_rows": displaceable_rows, "role_gap_rows": role_gap_rows,
        "call_target": call_target, "usual_team": usual_team,
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
def log_outreach_form(project_id: int, request: Request, channel: str = Form("call"),
                      notes: str = Form(""), next_action: str = Form(""),
                      session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.outreach import log_outreach
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    o = log_outreach(session, project_id=project_id, channel=channel, notes=notes,
                     next_action=next_action or None)
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


@app.get("/project/{project_id}/precall", response_class=HTMLResponse)
def project_precall(project_id: int, request: Request, refresh: bool = False,
                    session: Session = Depends(get_session), _: str = Depends(auth)):
    """Read-only pre-call brief -- see app/precall.py's module docstring for
    the hard constraint this whole feature is built around (never writes to
    Postgres, cache lives on local disk). refresh=1 bypasses the cache."""
    from app.precall import PrecallUnavailable, brief_sections
    from app.precall import pre_call_brief as _pre_call_brief
    from app.spend import BudgetExceeded

    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    try:
        entry = _pre_call_brief(session, "project", project_id, force_refresh=refresh)
    except (PrecallUnavailable, BudgetExceeded) as exc:
        entry = None
        error = str(exc)
    else:
        error = None
    return templates.TemplateResponse(request, "precall_brief.html", {
        "entry": entry, "sections": brief_sections(entry["text"]) if entry else None,
        "error": error, "entity_type": "project", "entity_id": project_id,
        "back_url": f"/project/{project_id}", "title": project.name,
        "tb": _title_block(session), "active": "board",
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


@app.post("/project/{project_id}/documents", response_class=HTMLResponse)
async def upload_project_document(project_id: int, request: Request, file: UploadFile = File(...),
                                  doc_type: str = Form("other"),
                                  session: Session = Depends(get_session), auth_user: str = Depends(auth)):
    """A rep attaches a PDF -- a drawing set, a Division 23 spec section, or
    just the mechanical sheets. Stores it and reads its text layer; does NOT
    run schedule extraction (a separate, explicit, LLM-costing step) -- see
    app/pipeline/schedule.py."""
    from app.pipeline.schedule import UploadRejected, attach_document
    project = session.get(Project, project_id)
    if not project:
        raise HTTPException(404)
    data = await file.read()
    try:
        attach_document(session, project_id=project_id, data=data,
                        filename=file.filename or "document.pdf",
                        content_type=file.content_type, doc_type=doc_type,
                        uploaded_by=auth_user)
    except UploadRejected as exc:
        raise HTTPException(413 if "exceeds" in str(exc) else 400, detail=str(exc)) from exc
    return RedirectResponse(f"/project/{project_id}", status_code=303)


@app.post("/project/{project_id}/documents/{doc_id}/extract", response_class=HTMLResponse)
def extract_project_document_schedule(project_id: int, doc_id: int,
                                      session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.pipeline.schedule import ExtractionFailed, extract_schedule
    doc = session.get(ProjectDocument, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404)
    try:
        stats = extract_schedule(session, load_config(), doc)
    except ExtractionFailed as exc:
        return HTMLResponse(f'<span class="bad">extraction failed — {exc}</span>')
    return HTMLResponse(
        f'<span class="ok">{stats["entries"]} row(s) extracted'
        + (f', <span class="bad">{stats["needs_review"]} need review</span>' if stats["needs_review"] else '')
        + '</span> — <a href="/project/' + str(project_id) + '">refresh the page</a> to see the schedule')


@app.get("/project/{project_id}/documents/{doc_id}/download")
def download_project_document(project_id: int, doc_id: int,
                              session: Session = Depends(get_session), _: str = Depends(auth)):
    doc = session.get(ProjectDocument, doc_id)
    if not doc or doc.project_id != project_id:
        raise HTTPException(404)
    return Response(content=doc.data, media_type=doc.content_type,
                    headers={"Content-Disposition": f'inline; filename="{doc.filename}"'})


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


@app.get("/developer/{developer_name:path}", response_class=HTMLResponse)
def developer_detail(developer_name: str, request: Request,
                     session: Session = Depends(get_session), _: str = Depends(auth)):
    """Every project naming this developer, plus their usual design team --
    see app.developer_team's module docstring. Keyed on normalize_name, not a
    Firm id: most developers only ever exist as Project.developer text, never
    as their own roster row."""
    from app.developer_team import DESIGN_TEAM_ROLES, usual_team_for_developer

    norm = normalize_name(developer_name)
    if not norm:
        raise HTTPException(404)
    projects = [p for p in session.exec(select(Project)).all()
               if p.developer and normalize_name(p.developer) == norm]
    if not projects:
        raise HTTPException(404)
    display_name = next((p.developer for p in projects if p.developer), developer_name)
    projects.sort(key=lambda p: p.score, reverse=True)

    return templates.TemplateResponse(request, "developer.html", {
        "developer": display_name, "projects": projects,
        "team": usual_team_for_developer(session, display_name),
        "design_team_roles": DESIGN_TEAM_ROLES,
        "tb": _title_block(session), "active": "board",
    })


@app.post("/developer/{developer_name:path}/team", response_class=HTMLResponse)
def developer_team_add(developer_name: str, firm_name: str = Form(...), role: str = Form(...),
                       reason: str = Form(...), confirmed_by: str = Form(...),
                       session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.developer_team import add_manual_team_entry

    try:
        add_manual_team_entry(session, developer_name, firm_name, role, reason, confirmed_by)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    return RedirectResponse(f"/developer/{developer_name}", status_code=303)


def _csv_response(filename: str, header: list[str], rows: list[list]) -> Response:
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
             p.last_signal_at.isoformat() if p.last_signal_at else "", p.next_action or "",
             p.days_to_estimated_bid_low if p.days_to_estimated_bid_low is not None else "",
             p.days_to_estimated_bid_high if p.days_to_estimated_bid_high is not None else ""]
            for p in projects]
    name = "watchlist.csv" if watchlist else f"board-{cat.value if cat else 'all'}.csv"
    return _csv_response(name,
                         ["id", "project", "category", "developer", "county", "state",
                          "tons_low", "tons_high",
                          "confidence_flag", "estimate_basis", "stage", "window", "score",
                          "days_to_bid", "status", "last_signal", "next_action",
                          "days_to_bid_ci_low", "days_to_bid_ci_high"], rows)


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
    # Second queue on the same page: numbers AND names the grounding guard threw
    # away (app.grounding.reject_ungrounded_numbers / reject_ungrounded_names).
    # They are already dropped everywhere downstream, so nothing is waiting on a
    # decision — this is here so a rejection is visible rather than a silent
    # hole, and so a pattern of rejections on one source gets noticed.
    flagged = []
    for s in session.exec(select(Signal)).all():
        ej = s.extraction_json or {}
        doc = session.get(RawDocument, s.raw_document_id)
        for r in (ej.get("rejected_numeric") or []) + (ej.get("rejected_names") or []):
            flagged.append({"signal": s, "doc": doc, "rej": r})
    # Numeric rejections first (sorted by the size of what was thrown away —
    # the biggest fabricated number is the most consequential one to notice),
    # then name rejections after: a rejected value isn't comparable in
    # magnitude to a rejected name, so sorting by abs(value) alone would
    # either crash on a string or silently misorder the two kinds together.
    flagged.sort(key=lambda f: (not isinstance(f["rej"].get("value"), (int, float)),
                                -abs(f["rej"]["value"]) if isinstance(f["rej"].get("value"), (int, float)) else 0))
    return templates.TemplateResponse(request, "review.html", {
        "rows": rows, "flagged": flagged,
        "tb": _title_block(session), "active": "review",
    })


@app.post("/review/{candidate_id}/{decision}", response_class=HTMLResponse)
def review_decide(candidate_id: int, decision: str,
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    if decision not in ("merge", "reject"):
        raise HTTPException(400)
    apply_review_decision(session, load_config(), candidate_id, decision)
    return HTMLResponse(f'<td colspan="5" class="resolved">{decision}d ✓</td>')


@app.get("/corrections-review", response_class=HTMLResponse)
def corrections_review(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """The forward-only stage ratchet's own review queue -- see the RATCHET
    BUG diagnosis and RATCHET OVERRIDE design (2026-09-06). Every row here
    is a project whose most recent StageObservation names an earlier stage
    than Project.stage currently shows -- often a legitimate non-issue (a
    later filing simply didn't restate stage), sometimes a real correction.
    Read-only until a human types a reason and submits; nothing here is
    ever auto-applied."""
    from app.pipeline.corrections import stage_regression_candidates
    candidates = stage_regression_candidates(session)
    return templates.TemplateResponse(request, "corrections_review.html", {
        "candidates": candidates, "stages": [s.value for s in Stage],
        "tb": _title_block(session), "active": "corrections-review",
    })


@app.post("/corrections-review/{project_id}", response_class=HTMLResponse)
def corrections_review_submit(
    project_id: int,
    new_value: str = Form(...), reason: str = Form(...), corrected_by: str = Form(...),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    from app.pipeline.corrections import apply_manual_correction

    try:
        apply_manual_correction(session, load_config(), project_id, "stage",
                                new_value, reason, corrected_by)
    except ValueError as exc:
        return HTMLResponse(f'<td colspan="4" class="warn">{exc}</td>', status_code=400)
    return HTMLResponse('<td colspan="4" class="resolved">corrected ✓</td>')


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
                              Project.in_territory == True)).all()
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


# ---- line card -----------------------------------------------------------
#
# A working tool, not a catalog: every line connects to accounts (who
# already buys it) and to the project board (which live projects would
# plausibly call for it), and every eligibility/lead-time/competitor field
# stays null and visibly unfilled until confirmed by name and date — see
# app/models.py's ProductLine docstring and app/accounts.py's module
# comments for the full discipline this follows.

@app.get("/lines", response_class=HTMLResponse)
def lines_index(request: Request, role: str = "", firm: str = "", market: str = "",
                value_tier: str = "", eligible: str = "", branch: str = "",
                session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.accounts import category_is_best_guess
    from app.models import ProductLineBranch

    branch_rows = session.exec(select(ProductLineBranch)).all()
    known_branches = sorted({b.branch for b in branch_rows})
    branch_status_by_line_id: dict[int, ProductLineBranch] = {
        b.product_line_id: b for b in branch_rows if b.branch == branch
    } if branch else {}

    q = select(ProductLine)
    if firm == "both":
        q = q.where(ProductLine.firm == "both")
    elif firm:
        q = q.where(ProductLine.firm.in_([firm, "both"]))
    if value_tier:
        q = q.where(ProductLine.value_tier == int(value_tier))
    if eligible in ("oshpd_osp", "ufc_4_010_06", "ahri_certified"):
        q = q.where(getattr(ProductLine, eligible).is_(True))
    lines = session.exec(q.order_by(ProductLine.value_tier, ProductLine.name)).all()
    if market:
        lines = [line for line in lines if market in (line.markets_served or [])]

    by_role: dict[str, list] = {r: [] for r in ROLE_ORDER}
    for line in lines:
        by_role.setdefault(line.building_role, []).append(line)
    active_role = role if role in ROLE_ORDER else ROLE_ORDER[0]

    # Surfaced regardless of the current filters -- a correction todo-list,
    # not something a facet should be able to hide.
    best_guess_lines = [line for line in session.exec(select(ProductLine)).all()
                        if category_is_best_guess(line)]
    legacy_market_lines = [line for line in session.exec(select(ProductLine)).all()
                           if line.markets_served_source == "legacy_guess"]

    return templates.TemplateResponse(request, "lines.html", {
        "by_role": by_role, "role_order": ROLE_ORDER,
        "role_counts": {r: len(v) for r, v in by_role.items()},
        "active_role": active_role, "markets": MARKETS,
        "f_firm": firm, "f_market": market, "f_value_tier": value_tier, "f_eligible": eligible,
        "f_branch": branch, "known_branches": known_branches,
        "branch_status_by_line_id": branch_status_by_line_id,
        "lines_on_any_card": len({b.product_line_id for b in branch_rows if b.status == "confirmed_covered"}),
        "best_guess_lines": best_guess_lines, "best_guess_total": len(best_guess_lines),
        "best_guess_ids": {line.id for line in best_guess_lines},
        "legacy_market_lines": legacy_market_lines, "legacy_market_total": len(legacy_market_lines),
        "total": len(lines), "total_all": session.exec(
            select(func.count(ProductLine.id))).one(),
        "tb": _title_block(session), "active": "lines",
    })


@app.get("/lines/cheat-sheet/{branch}", response_class=HTMLResponse)
def line_cheat_sheet(branch: str, request: Request,
                     session: Session = Depends(get_session), _: str = Depends(auth)):
    """Printable, confirmed-only cheat sheet for one branch -- for the
    truck. Confirmed pitches ONLY (see app.models.LinePitch's own
    docstring): a draft belongs on /line/{id} for review, never on
    something printed and carried around as if it were settled."""
    from app.models import ProductLineBranch

    branch_lines = session.exec(
        select(ProductLine, LinePitch)
        .where(ProductLine.id == ProductLineBranch.product_line_id,
              ProductLineBranch.branch == branch,
              ProductLineBranch.status == "confirmed_covered",
              LinePitch.product_line_id == ProductLine.id,
              LinePitch.review_status == "confirmed")
        .order_by(ProductLine.building_role, ProductLine.name)
    ).all()
    if not branch_lines:
        known = sorted({b for b in session.exec(
            select(ProductLineBranch.branch).distinct()).all() if b})
        raise HTTPException(404, detail=f"no confirmed pitches for branch {branch!r} -- "
                                       f"known branches: {', '.join(known)}")

    by_role: dict[str, list] = {}
    for line, pitch in branch_lines:
        by_role.setdefault(line.building_role, []).append((line, pitch))

    return templates.TemplateResponse(request, "line_cheat_sheet.html", {
        "branch": branch, "by_role": by_role, "role_order": ROLE_ORDER,
        "tb": _title_block(session), "active": "lines",
    })


@app.get("/reference", response_class=HTMLResponse)
def reference_index(request: Request, tab: str = "",
                     session: Session = Depends(get_session), _: str = Depends(auth)):
    """Static field-reference sheet -- equipment, formulas, abbreviations and
    role definitions. No source, no pipeline, nothing here to go stale --
    see app/reference.py's module docstring for why this carries no
    source-health entry and no assumptions-register entry. The 'pitches'
    tab is the one exception (see that same docstring): confirmed
    line-card pitches only, grouped by role, plus a draft count."""
    active_tab = tab if tab in TAB_ORDER else TAB_ORDER[0]

    confirmed_pitches_by_role: dict[str, list] = {r: [] for r in ROLE_ORDER}
    draft_count_by_role: dict[str, int] = {r: 0 for r in ROLE_ORDER}
    if active_tab == "pitches":
        confirmed = session.exec(
            select(LinePitch, ProductLine)
            .where(LinePitch.product_line_id == ProductLine.id, LinePitch.review_status == "confirmed")
        ).all()
        for pitch, line in confirmed:
            confirmed_pitches_by_role.setdefault(line.building_role, []).append((pitch, line))
        drafts = session.exec(
            select(LinePitch, ProductLine)
            .where(LinePitch.product_line_id == ProductLine.id, LinePitch.review_status == "draft")
        ).all()
        for _pitch, line in drafts:
            draft_count_by_role[line.building_role] = draft_count_by_role.get(line.building_role, 0) + 1

    return templates.TemplateResponse(request, "reference.html", {
        "tab_order": TAB_ORDER, "TAB_LABELS": TAB_LABELS, "active_tab": active_tab,
        "role_order": ROLE_ORDER, "role_reference": ROLE_REFERENCE,
        "confirmed_pitches_by_role": confirmed_pitches_by_role, "draft_count_by_role": draft_count_by_role,
        "total_drafts": sum(draft_count_by_role.values()),
        "tb": _title_block(session), "active": "reference",
    })


@app.get("/line/{line_id}", response_class=HTMLResponse)
def line_detail(line_id: int, request: Request,
                session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.accounts import (
        SELECTION_TOOL_ACCESS_LABELS,
        SELECTION_TOOL_VERIFICATION_LABELS,
        category_is_best_guess,
        line_account_matrix,
        matching_projects_for_line,
        pull_through,
        value_tier_band,
    )
    from app.competitors import competing_lines_by_role
    from app.models import ProductLineBranch, RepFirm, SelectionTool
    line = session.get(ProductLine, line_id)
    if line is None:
        raise HTTPException(404)
    cfg = load_config()
    selection_tool = session.exec(
        select(SelectionTool).where(SelectionTool.product_line_id == line.id)).first()
    competing = competing_lines_by_role(session).get(line.building_role, [])
    rep_firms = {f.id: f for f in session.exec(select(RepFirm)).all()}
    branches = session.exec(
        select(ProductLineBranch).where(ProductLineBranch.product_line_id == line.id)
        .order_by(ProductLineBranch.branch)).all()
    pitch = session.exec(select(LinePitch).where(LinePitch.product_line_id == line.id)).first()
    pitch_competitors = session.exec(
        select(LineCompetitor).where(LineCompetitor.product_line_id == line.id)
        .order_by(LineCompetitor.competitor_name)).all()
    return templates.TemplateResponse(request, "line_detail.html", {
        "line": line, "best_guess": category_is_best_guess(line),
        "pull_through": pull_through(session, cfg, line),
        "accounts_matrix": line_account_matrix(session, line.id),
        "matching_projects": matching_projects_for_line(session, line),
        "value_band": value_tier_band(cfg, line.value_tier),
        "selection_tool": selection_tool,
        "access_labels": SELECTION_TOOL_ACCESS_LABELS, "verif_labels": SELECTION_TOOL_VERIFICATION_LABELS,
        "competing_lines": competing, "rep_firms": rep_firms,
        "branches": branches,
        "pitch": pitch, "pitch_competitors": pitch_competitors,
        "tb": _title_block(session), "active": "lines",
    })


@app.post("/line/{line_id}/pitch/review")
def line_pitch_review(line_id: int, request: Request, action: str = Form(...),
                      reviewed_by: str = Form(...),
                      what_it_is: str = Form(None), where_it_fits: str = Form(None),
                      typical_project_types: str = Form(None), elevator_pitch: str = Form(None),
                      differentiators: str = Form(None), engineer_questions: str = Form(None),
                      session: Session = Depends(get_session), _: str = Depends(auth)):
    """One-tap Confirm/Reject, or an inline Edit-then-confirm -- action is
    'confirm' | 'reject' | 'edit'. Edit applies the submitted field values
    (one line per differentiator/question, blank lines dropped) AND marks
    the row confirmed in the same action, since an edit exists precisely so
    a rep can fix something small and move on, not to leave it drafted
    again for no reason."""
    pitch = session.exec(select(LinePitch).where(LinePitch.product_line_id == line_id)).first()
    if pitch is None:
        raise HTTPException(404)
    if action not in ("confirm", "reject", "edit"):
        raise HTTPException(400, detail=f"unknown action {action!r}")

    if action == "edit":
        pitch.what_it_is = (what_it_is or "").strip() or None
        pitch.where_it_fits = (where_it_fits or "").strip() or None
        pitch.typical_project_types = (typical_project_types or "").strip() or None
        pitch.elevator_pitch = (elevator_pitch or "").strip() or None
        pitch.differentiators = [ln.strip() for ln in (differentiators or "").splitlines() if ln.strip()]
        pitch.engineer_questions = [ln.strip() for ln in (engineer_questions or "").splitlines() if ln.strip()]

    pitch.review_status = "confirmed" if action in ("confirm", "edit") else "rejected"
    pitch.reviewed_by = reviewed_by
    pitch.reviewed_at = utcnow()
    pitch.updated_at = utcnow()
    session.add(pitch)
    session.commit()
    return RedirectResponse(f"/line/{line_id}", status_code=303)


@app.post("/line/{line_id}/competitor/{competitor_id}/review")
def line_competitor_review(line_id: int, competitor_id: int, request: Request,
                           action: str = Form(...), reviewed_by: str = Form(...),
                           why_we_lose: str = Form(None), why_we_win: str = Form(None),
                           session: Session = Depends(get_session), _: str = Depends(auth)):
    row = session.get(LineCompetitor, competitor_id)
    if row is None or row.product_line_id != line_id:
        raise HTTPException(404)
    if action not in ("confirm", "reject", "edit"):
        raise HTTPException(400, detail=f"unknown action {action!r}")

    if action == "edit":
        row.why_we_lose = (why_we_lose or "").strip() or None
        row.why_we_win = (why_we_win or "").strip() or None

    row.review_status = "confirmed" if action in ("confirm", "edit") else "rejected"
    row.reviewed_by = reviewed_by
    row.reviewed_at = utcnow()
    row.updated_at = utcnow()
    session.add(row)
    session.commit()
    return RedirectResponse(f"/line/{line_id}", status_code=303)


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
        account_replacement_windows,
        compute_gaps,
        coverage_summary,
        ensure_coverage_rows,
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


@app.get("/account/{account_id}", response_class=HTMLResponse)
def account_page_view(account_id: int, request: Request,
                      session: Session = Depends(get_session), _: str = Depends(auth)):
    """The pre-meeting page -- see app.accounts.build_account_page/AccountPage
    for what it assembles and why it's a different shape than
    /accounts/{id} (coverage editor) or /accounts/{id}/brief (printable
    one-pager)."""
    try:
        page = build_account_page(session, load_config(), account_id)
    except ValueError:
        raise HTTPException(404)
    return templates.TemplateResponse(request, "account_page.html", {
        "page": page, "account": page.account,
        "role_labels": ROLE_LABELS, "role_order": ROLE_ORDER,
        "tb": _title_block(session), "active": "accounts",
    })


@app.post("/account/{account_id}/outreach")
def account_outreach_form(account_id: int, request: Request, channel: str = Form("call"),
                          notes: str = Form(""), next_action: str = Form(""),
                          session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.outreach import log_outreach
    account = session.get(Account, account_id)
    if not account:
        raise HTTPException(404)
    log_outreach(session, account_id=account_id, channel=channel, notes=notes,
                next_action=next_action or None)
    return RedirectResponse(f"/account/{account_id}", status_code=303)


@app.get("/assumptions", response_class=HTMLResponse)
def assumptions_register(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """Every tunable constant this system scores, sizes, or ranks with, and
    honestly where it came from — see app/assumptions.py's module docstring
    for why this page exists and the discipline it follows."""
    from app.assumptions import assumptions_by_group, load_assumptions, source_tally
    from app.pipeline.retrofit import service_calls_coverage as get_service_calls_coverage
    from app.pipeline.resolve import delivery_method_coverage as get_delivery_method_coverage
    from app.pipeline.ownership import ownership_recency_coverage as get_ownership_recency_coverage
    from app.portfolios import portfolio_coverage as get_portfolio_coverage
    from app.pipeline.hcai import hospital_capability_gaps as get_hospital_capability_gaps
    from app.pipeline.hcai import hospital_seismic_coverage as get_hospital_coverage
    cfg = load_config()
    coverage = get_service_calls_coverage(session)
    delivery_coverage = get_delivery_method_coverage(session)
    ownership_coverage = get_ownership_recency_coverage(session)
    portfolio_cov = get_portfolio_coverage(session)
    hospital_cov = get_hospital_coverage(session, cfg)
    hospital_gaps = get_hospital_capability_gaps(session)
    assumptions = load_assumptions(cfg, service_calls_coverage=coverage,
                                   delivery_method_coverage=delivery_coverage,
                                   ownership_recency_coverage=ownership_coverage,
                                   portfolio_coverage=portfolio_cov,
                                   hospital_coverage=hospital_cov,
                                   hospital_capability_gaps=hospital_gaps)
    return templates.TemplateResponse(request, "assumptions.html", {
        "grouped": assumptions_by_group(cfg, service_calls_coverage=coverage,
                                        delivery_method_coverage=delivery_coverage,
                                        ownership_recency_coverage=ownership_coverage,
                                        portfolio_coverage=portfolio_cov,
                                        hospital_coverage=hospital_cov,
                                        hospital_capability_gaps=hospital_gaps),
        "tally": source_tally(assumptions),
        "total": len(assumptions),
        "tb": _title_block(session), "active": "assumptions",
    })


@app.get("/health", response_class=HTMLResponse)
def source_health(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.models import PipelineRun
    from app.pipeline_health import memory_pressure_status, stage_peak_memory

    # `scout pipeline` run history, peak memory alongside it: Render exposes
    # no instance metrics for one-off cron jobs (confirmed 2026-08-15), so
    # this table -- backed by PipelineRun.peak_rss_bytes and
    # PipelineStageRun -- is the only place that history is visible at all.
    pipeline_runs = session.exec(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(20)).all()
    mem = memory_pressure_status(session)
    latest_stage_peaks = stage_peak_memory(session, mem["run_id"]) if mem["run_id"] else {}

    sources: dict[str, dict] = {}
    runs = session.exec(select(SourceRun).order_by(SourceRun.started_at.desc())).all()
    for run in runs:
        entry = sources.setdefault(run.source, {"last": run, "last_ok": None})
        if entry["last_ok"] is None and run.ok:
            entry["last_ok"] = run
    # ---- the chart series ------------------------------------------------
    # Source health is the one view a chart genuinely beats a table at. The
    # question is "has this source been running, and when did it stop", which is
    # a shape over time: a gap is instantly visible on a timeline and invisible
    # in a list of the last fifty rows sorted by date. The board earned nothing
    # from a chart and did not get one.
    from collections import defaultdict
    from datetime import timedelta

    from app.models import run_name_source
    from app.spend import budget_status

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
    from app.precall import precall_cost_report
    return templates.TemplateResponse(request, "health.html", {
        "sources": sources, "recent_runs": runs[:50], "budget": budget_status(),
        "chart": chart, "tb": _title_block(session), "active": "health",
        "pipeline_runs": pipeline_runs, "memory": mem, "latest_stage_peaks": latest_stage_peaks,
        "precall": precall_cost_report(),
    })


@app.get("/admin/access", response_class=HTMLResponse)
def admin_access(request: Request, session: Session = Depends(get_session), user: str = Depends(auth)):
    """Visible only to the configured admin user. In practice auth() already
    guarantees that -- it's the only username that can ever pass -- but the
    explicit check documents the intent and is what actually gets exercised
    if this dashboard ever grows a second real user."""
    cfg = load_config()
    if user != admin_username(cfg):
        raise HTTPException(status.HTTP_403_FORBIDDEN)
    from app.models import AccessLog

    entries = session.exec(select(AccessLog).order_by(AccessLog.created_at.desc()).limit(200)).all()
    return templates.TemplateResponse(request, "admin_access.html", {
        "entries": entries, "summary": access_summary(session),
        "tb": _title_block(session), "active": "admin_access",
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


# --- Field intel: human-sourced project intelligence, deliberately NOT     -
# --- add-signal's path -- no RawDocument, no resolve/size_score/grounding. -
# --- See app/field_intel.py's own module docstring for why.                -

@app.get("/intel", response_class=HTMLResponse)
def field_intel_list(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.field_intel import active_field_intel
    confirmed = session.exec(
        select(FieldIntel).where(FieldIntel.status == "confirmed")
        .order_by(FieldIntel.confirmed_at.desc())).all()
    return templates.TemplateResponse(request, "intel_list.html", {
        "active_records": active_field_intel(session), "confirmed": confirmed,
        "tb": _title_block(session), "active": "intel",
    })


@app.get("/intel/new", response_class=HTMLResponse)
def field_intel_new(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    return templates.TemplateResponse(request, "intel_form.html", {
        "stages": [s.value for s in Stage],
        "tb": _title_block(session), "active": "intel",
    })


@app.post("/intel")
def field_intel_create(
    request: Request,
    reported_by: str = Form(...), reported_at: str = Form(...), source_notes: str = Form(...),
    owner: str = Form(""), location: str = Form(""), size_scope: str = Form(""),
    stage: str = Form("unknown"), expected_timing: str = Form(""),
    engineer_name: str = Form(""), mech_contractor_name: str = Form(""),
    session: Session = Depends(get_session), _: str = Depends(auth),
):
    from app.field_intel import create_field_intel
    try:
        reported_at_dt = datetime.strptime(reported_at, "%Y-%m-%d")
    except ValueError:
        raise HTTPException(400, detail=f"reported_at must be YYYY-MM-DD, got {reported_at!r}")
    try:
        intel = create_field_intel(
            session, reported_by=reported_by, reported_at=reported_at_dt, source_notes=source_notes,
            owner=owner or None, location=location or None, size_scope=size_scope or None,
            stage=stage or "unknown", expected_timing=expected_timing or None,
            engineer_name=engineer_name or None, mech_contractor_name=mech_contractor_name or None,
        )
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse(f"/intel/{intel.id}", status_code=303)


@app.get("/intel/{intel_id}", response_class=HTMLResponse)
def field_intel_detail(intel_id: int, request: Request,
                       session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.field_intel import confirmation_candidates, firm_active_projects
    intel = session.get(FieldIntel, intel_id)
    if not intel:
        raise HTTPException(404)
    engineer_firm = session.get(Firm, intel.engineer_firm_id) if intel.engineer_firm_id else None
    engineer_account = session.get(Account, intel.engineer_account_id) if intel.engineer_account_id else None
    mech_firm = session.get(Firm, intel.mech_contractor_firm_id) if intel.mech_contractor_firm_id else None
    mech_account = (session.get(Account, intel.mech_contractor_account_id)
                    if intel.mech_contractor_account_id else None)
    confirmed_project = session.get(Project, intel.confirmed_project_id) if intel.confirmed_project_id else None
    return templates.TemplateResponse(request, "intel_detail.html", {
        "intel": intel,
        "engineer_firm": engineer_firm, "engineer_account": engineer_account,
        "mech_firm": mech_firm, "mech_account": mech_account,
        "engineer_firm_projects": firm_active_projects(session, engineer_firm.id) if engineer_firm else [],
        "mech_firm_projects": firm_active_projects(session, mech_firm.id) if mech_firm else [],
        "confirmed_project": confirmed_project,
        "candidates": confirmation_candidates(session, intel),
        "tb": _title_block(session), "active": "intel",
    })


@app.post("/intel/{intel_id}/confirm")
def field_intel_confirm(intel_id: int, request: Request, project_id: int = Form(...),
                        confirmed_by: str = Form(...),
                        session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.field_intel import confirm_field_intel
    try:
        confirm_field_intel(session, intel_id, project_id, confirmed_by=confirmed_by)
    except ValueError as exc:
        raise HTTPException(400, detail=str(exc))
    return RedirectResponse(f"/intel/{intel_id}", status_code=303)


# --- Voice capture: iOS Shortcut -> transcribe -> extract -> resolve ->     -
# --- review_queue proposal -> human confirms/rejects. See                  -
# --- app/pipeline/voice_capture.py for the full chain and app/outreach.py  -
# --- for the one writer a confirm calls through.                           -

@app.post("/capture/voice")
async def capture_voice(request: Request, file: UploadFile = File(...), _: str = Depends(capture_auth),
                        session: Session = Depends(get_session)):
    """Authenticated multipart endpoint the iOS Shortcut posts a recorded
    voice note to. Deliberately returns plain text, not HTML/JSON with a
    schema to maintain -- the Shortcut only needs to know it worked; the
    actual review happens later at /captures on a browser.

    Sets request.state.capture_reason before each raise -- see
    capture_auth's docstring and app.access_log's module comment for why."""
    from app.pipeline.voice_capture import MAX_UPLOAD_BYTES, TranscriptionFailed, capture_voice_note

    audio_bytes = await file.read()
    if not audio_bytes:
        request.state.capture_reason = "empty upload"
        raise HTTPException(400, detail="empty upload")
    if len(audio_bytes) > MAX_UPLOAD_BYTES:
        reason = f"{len(audio_bytes)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte limit"
        request.state.capture_reason = reason
        raise HTTPException(413, detail=reason)
    try:
        row = capture_voice_note(
            session, load_config(), audio_bytes=audio_bytes,
            filename=file.filename or "capture.m4a",
            content_type=file.content_type or "audio/m4a",
        )
    except TranscriptionFailed as exc:
        # The one failure with nothing to review yet -- no transcript exists,
        # so there is no partial capture worth keeping. Every failure AFTER
        # this point (extraction, matching) is instead absorbed into the
        # review_queue row itself -- see capture_voice_note.
        reason = f"transcription failed: {exc}"
        request.state.capture_reason = reason
        raise HTTPException(502, detail=reason) from exc
    return Response(content=f"logged, review at /captures/{row.id}\n", media_type="text/plain")


@app.get("/capture/voice")
def capture_voice_probe(request: Request, token: str | None = None) -> Response:
    """Diagnostic GET for the exact black-box problem this endpoint has had:
    a wrong URL and a broken multipart body both look like nothing
    happened, from the Shortcut or from a phone's browser. Visiting this
    URL confirms the address is even reachable at all -- distinguishing
    "wrong URL, nothing here" from "right URL, wrong/missing token", which
    otherwise look identical.

    Deliberately NOT behind capture_auth: a bare Safari address-bar visit
    can't attach an Authorization header, so requiring one here would make
    it impossible to ever get past "alive" from a phone. The token can
    ALSO be checked here, via ?token=... -- diagnostic only, never accepted
    this way on the real POST path above, and never logged (see
    capture_voice_logging_middleware, which only logs POSTs)."""
    from app.config import capture_api_key

    key = capture_api_key()
    if not key:
        return Response(
            "capture/voice is alive, but CAPTURE_API_KEY is not set on the server -- "
            "every real POST will 503 until it is.\n",
            media_type="text/plain")

    supplied = token
    auth_header = request.headers.get("authorization", "")
    if not supplied and auth_header.lower().startswith("bearer "):
        supplied = auth_header[len("bearer "):]

    if supplied is None:
        return Response(
            "capture/voice is alive.\n"
            "Expects: POST multipart/form-data, field name 'file', header "
            "'Authorization: Bearer <CAPTURE_API_KEY>'.\n"
            "Append ?token=<your CAPTURE_API_KEY> to THIS url to check your token too.\n",
            media_type="text/plain")
    if secrets.compare_digest(supplied, key):
        return Response("capture/voice is alive, and this token is correct.\n", media_type="text/plain")
    return Response("capture/voice is alive, but this token does NOT match CAPTURE_API_KEY.\n",
                    media_type="text/plain", status_code=status.HTTP_401_UNAUTHORIZED)


@app.get("/capture", response_class=HTMLResponse)
def capture_record_page(request: Request, session: Session = Depends(get_session), _: str = Depends(auth)):
    """Browser-recorded capture: records in Safari/Chrome via MediaRecorder
    and uploads through /capture/upload below, which relays it to the REAL
    /capture/voice -- see that route's docstring for why a relay and not a
    direct client-side POST. Exists because the iOS Shortcut has never once
    worked (zero review_queue rows ever, no Shortcuts user agent ever seen)
    and stayed an undebuggable black box on the phone; this removes every
    unknown between a person and a capture -- no Shortcut config, no
    multipart guesswork, no silent iOS failure -- and if IT works, the
    server side is proven fine and the problem was always the Shortcut."""
    from app.config import capture_api_key

    return templates.TemplateResponse(request, "capture_record.html", {
        "tb": _title_block(session), "active": "capture",
        "capture_configured": bool(capture_api_key()),
    })


@app.post("/capture/upload")
async def capture_upload_relay(file: UploadFile = File(...), _: str = Depends(auth)) -> Response:
    """The browser-capture page's upload target. The browser never learns
    CAPTURE_API_KEY -- app.config.capture_api_key's docstring is explicit
    that the phone-automation secret and the dashboard login are different
    secrets with different exposure surfaces, and this page is reachable by
    anyone with the (more widely shared) dashboard password. So this relays
    the recording to the REAL /capture/voice via an in-process ASGI call
    into this same app (httpx's ASGITransport -- not a second server, not a
    real network hop, and not a bypass: it runs the actual route, through
    the actual capture_auth dependency and the actual
    capture_voice_logging_middleware, exactly as a real HTTP request would)
    with the bearer token attached here, server-side. Whatever
    /capture/voice returns -- success text or a failure detail -- is
    relayed back to the browser verbatim, so a browser-capture failure is
    exactly as visible as a real one, and "if it works in the browser, the
    server side is fine" is actually true: this exercises the same auth
    and multipart-parsing code path the iOS Shortcut hits, not a shortcut
    around it."""
    from app.config import capture_api_key

    key = capture_api_key()
    if not key:
        raise HTTPException(status.HTTP_503_SERVICE_UNAVAILABLE,
                            detail="CAPTURE_API_KEY env var is not set")

    audio_bytes = await file.read()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                 base_url="http://capture-relay") as inner:
        resp = await inner.post(
            "/capture/voice",
            headers={"Authorization": f"Bearer {key}"},
            files={"file": (file.filename or "capture.webm", audio_bytes,
                            file.content_type or "application/octet-stream")},
        )
    return Response(content=resp.content, media_type="text/plain", status_code=resp.status_code)


@app.get("/captures", response_class=HTMLResponse)
def captures_list(request: Request, status_filter: str = "pending",
                  session: Session = Depends(get_session), _: str = Depends(auth)):
    q = select(ReviewQueue).order_by(ReviewQueue.created_at.desc())
    if status_filter and status_filter != "all":
        q = q.where(ReviewQueue.status == status_filter)
    rows = session.exec(q.limit(200)).all()
    pending_capture_count = session.exec(
        select(func.count()).select_from(select(ReviewQueue).where(ReviewQueue.status == "pending").subquery())
    ).one()
    return templates.TemplateResponse(request, "captures.html", {
        "rows": rows, "status_filter": status_filter, "pending_capture_count": pending_capture_count,
        "tb": _title_block(session), "active": "captures",
    })


@app.get("/captures/{capture_id}", response_class=HTMLResponse)
def capture_review(capture_id: int, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    from app.voice_match import resolve_all

    row = session.get(ReviewQueue, capture_id)
    if row is None:
        raise HTTPException(404)
    # Candidates were computed once at capture time and are stored in
    # provenance for the record, but re-resolved live here too: Scout's own
    # contacts/firms/projects keep changing after a capture lands in the
    # queue (a contact gets added, a project gets renamed), and a reviewer
    # picking from a stale candidate list is worse than one extra query.
    payload = row.proposed_payload or {}
    candidates = resolve_all(
        session, contact_name=payload.get("contact_name"), firm_name=payload.get("firm_name"),
        project_or_building_name=payload.get("project_or_building_name"),
    )
    return templates.TemplateResponse(request, "capture_review.html", {
        "row": row, "payload": payload, "candidates": candidates,
        "tb": _title_block(session), "active": "captures",
    })


@app.get("/captures/{capture_id}/audio")
def capture_audio_file(capture_id: int, session: Session = Depends(get_session), _: str = Depends(auth)):
    row = session.get(ReviewQueue, capture_id)
    if row is None:
        raise HTTPException(404)
    audio_id = (row.provenance or {}).get("audio_id")
    audio = session.get(CaptureAudio, audio_id) if audio_id else None
    if audio is None:
        raise HTTPException(404, detail="no audio stored for this capture")
    return Response(content=audio.data, media_type=audio.content_type)


@app.post("/captures/{capture_id}/confirm", response_class=HTMLResponse)
def capture_confirm(capture_id: int, request: Request,
                    project_id: int = Form(...), contact_id: str = Form(""),
                    notes: str = Form(""), next_action: str = Form(""),
                    next_action_date: str = Form(""), channel: str = Form("call"),
                    session: Session = Depends(get_session), _: str = Depends(auth)):
    """Confirm calls app.outreach.log_outreach -- the SAME writer the
    dashboard's own outreach form and the log_outreach MCP tool use -- so
    this review card is never a second, independent way an Outreach row
    gets created. project_id is required: Outreach has no unresolved-entity
    concept, so a capture with no project picked cannot be confirmed (see
    app/voice_match.py:resolve_project's docstring) -- edit the field or
    reject instead."""
    from datetime import datetime

    from app.outreach import log_outreach

    row = session.get(ReviewQueue, capture_id)
    if row is None:
        raise HTTPException(404)
    if row.status != "pending":
        raise HTTPException(409, detail=f"capture #{capture_id} is already {row.status}")
    project = session.get(Project, project_id)
    if project is None:
        raise HTTPException(400, detail=f"no project #{project_id}")

    parsed_date = None
    if next_action_date.strip():
        try:
            parsed_date = datetime.fromisoformat(next_action_date.strip())
        except ValueError:
            raise HTTPException(400, detail=f"could not parse next_action_date {next_action_date!r}")

    log_outreach(session, project_id=project_id,
                contact_id=int(contact_id) if contact_id.strip() else None,
                channel=channel, notes=notes, next_action=next_action or None,
                next_action_date=parsed_date)

    row.status = "approved"
    row.decided_at = utcnow()
    session.add(row)
    session.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse(f'<span class="ok">✓ Confirmed — logged on #{project.id} {project.name}</span>')
    return RedirectResponse("/captures", status_code=303)


@app.post("/captures/{capture_id}/reject", response_class=HTMLResponse)
def capture_reject(capture_id: int, request: Request,
                   session: Session = Depends(get_session), _: str = Depends(auth)):
    """Discards the proposal. Nothing about a rejected capture is ever
    written anywhere else -- the row (and its audio/transcript) stays for
    audit, but log_outreach is never called."""
    row = session.get(ReviewQueue, capture_id)
    if row is None:
        raise HTTPException(404)
    if row.status != "pending":
        raise HTTPException(409, detail=f"capture #{capture_id} is already {row.status}")
    row.status = "rejected"
    row.decided_at = utcnow()
    session.add(row)
    session.commit()
    if request.headers.get("HX-Request"):
        return HTMLResponse('<span class="dim">Rejected — discarded.</span>')
    return RedirectResponse("/captures", status_code=303)
