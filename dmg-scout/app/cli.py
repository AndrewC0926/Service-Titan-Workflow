"""scout CLI — every pipeline stage independently runnable and idempotent."""
from __future__ import annotations

import json
import logging

import typer
from sqlmodel import select

from app.config import load_config
from app.db import init_db, session_scope
from app.ops import setup_logging

setup_logging()
logging.getLogger(__name__)

app = typer.Typer(help="DMG Scout: early-signal data center project intelligence.")

# find-replacement-candidates only runs on this weekday inside `scout
# pipeline` (see pipeline() below) -- it re-derives from LA County's ANNUAL
# assessor parcel roll (roll_year, a once-a-year county publication --
# hardcoded "2025" as of 2026-08-14, see app/pipeline/retrofit.py) via a
# full-county ArcGIS scan (measured 2026-08-14 against production: ~55k
# rows scanned, ~2m40s). The underlying data does
# not meaningfully change day to day, so running it daily would buy no new
# signal at real, repeated cost against a free public service. Weekly is
# still often enough to catch a permit that newly excludes a candidate.
# build-retrofit-buildings is the opposite case: cheap (measured ~4m50s,
# a few-thousand-APN batch lookup, not a county-wide scan) and its output
# changes daily just from equipment aging past service-life thresholds and
# newly-collected geocodes/service-frequency reports rejoining onto it -- so
# it runs every day, unconditionally, below.
RETROFIT_WEEKLY_WEEKDAY = 6  # Sunday -- datetime.weekday(): Monday=0 .. Sunday=6


@app.command()
def initdb() -> None:
    """Create all tables (dev). Production: alembic upgrade head."""
    init_db()
    typer.echo("tables created")


@app.command()
def fetch(source: str = typer.Option(None, help="Run one source only")) -> None:
    """FETCH + DEDUPE: pull new documents from enabled sources."""
    from app.pipeline.fetch import run_fetch
    cfg = load_config()
    with session_scope() as session:
        runs = run_fetch(session, cfg, only_source=source)
        # Rendered INSIDE the session. These are ORM instances, and the commit on
        # scope exit expires them, so reading run.ok out here raised
        # DetachedInstanceError — after every source had already fetched and
        # committed successfully. The damage is not the traceback: `scout
        # pipeline` catches per-step exceptions and counts them, so a completely
        # successful fetch was about to be reported as a failed pipeline step on
        # every nightly cron run.
        lines = []
        for name, run in runs.items():
            first = (run.error or "").splitlines()
            status = "ok" if run.ok else f"FAILED: {first[0] if first else 'no error recorded'}"
            lines.append(f"{name}: fetched={run.records_fetched} "
                         f"new={run.records_new} {status}")
    for line in lines:
        typer.echo(line)


@app.command()
def triage(limit: int = 200) -> None:
    """TRIAGE: Haiku relevance pass over pending documents."""
    from app.pipeline.triage import run_triage
    from app.spend import run_budget
    cfg = load_config()
    with run_budget("triage"), session_scope() as session:
        stats = run_triage(session, cfg, limit=limit)
    typer.echo(json.dumps(stats))
    _fail_if_all_errored("triage", stats, "error")


@app.command()
def extract(limit: int = 100) -> None:
    """EXTRACT: Sonnet structured extraction over relevant documents."""
    from app.pipeline.extract import run_extract
    from app.spend import run_budget
    cfg = load_config()
    with run_budget("extract"), session_scope() as session:
        stats = run_extract(session, cfg, limit=limit)
    typer.echo(json.dumps(stats))
    _fail_if_all_errored("extract", stats, "errors")


def _fail_if_all_errored(stage: str, stats: dict, error_key: str) -> None:
    """A stage that got nothing done must not exit 0. Invariant 1, applied to LLM
    stages rather than to sources.

    Observed on 2026-08-06: `scout triage` errored on all 80 pending documents
    (the Anthropic account was out of credit), printed
    `{"error": 80, ...}` and exited 0. Nothing downstream can tell that apart from
    a clean run over an empty queue — `scout pipeline` would have marched straight
    on to extract, resolve and notify, and the cron would have gone green on a
    night when the pipeline did no work at all.

    Deliberately narrow: SOME errors are normal and stay quiet at this level. Only
    a run that did work and got nothing but errors fails.
    """
    errors = stats.get(error_key, 0)
    done = sum(v for k, v in stats.items()
               if k not in (error_key, "skipped") and isinstance(v, int))
    if errors and not done:
        typer.echo(f"[FAIL] {stage}: {errors} attempted, {errors} errored, 0 succeeded — "
                   f"the stage did no work. Check `scout doctor` for the LLM account "
                   f"and budget before re-running.", err=True)
        raise typer.Exit(1)


@app.command()
def resolve(no_llm: bool = typer.Option(False, help="Skip LLM adjudication"),
            force: bool = typer.Option(
                False, help="Start even if another resolve looks like it is running")) -> None:
    """RESOLVE: match signals to canonical projects."""
    from app.duplicates import find_duplicates
    from app.pipeline.resolve import ConcurrentResolve, run_resolve
    from app.spend import run_budget
    cfg = load_config()
    try:
        with run_budget("resolve"), session_scope() as session:
            stats = run_resolve(session, cfg, use_llm=not no_llm, force=force)
            # Always, not on request: a resolver that starts fragmenting the board
            # fails silently otherwise — the row count simply grows.
            dup = find_duplicates(session)
    except ConcurrentResolve as exc:
        typer.secho(f"REFUSED: {exc}", fg=typer.colors.RED, err=True)
        raise typer.Exit(1) from exc
    stats["duplicate_groups"] = dup["n_groups"]
    stats["duplicate_excess_rows"] = dup["n_excess_rows"]
    typer.echo(json.dumps(stats))
    if dup["n_groups"]:
        typer.secho(
            f"WARNING: {dup['n_groups']} duplicate project groups "
            f"({dup['n_excess_rows']} excess rows) — `scout duplicates` for detail",
            fg=typer.colors.YELLOW)


@app.command()
def duplicates() -> None:
    """Project rows that look like the same project: shared SCH, or name+county."""
    from app.duplicates import duplicates_text, find_duplicates
    with session_scope() as session:
        typer.echo(duplicates_text(find_duplicates(session)))


@app.command("merge-duplicates")
def merge_duplicates(
    apply: bool = typer.Option(False, "--apply",
                               help="Actually merge. Without this it only prints the plan."),
) -> None:
    """Collapse every group `scout duplicates` reports. Dry run unless --apply.

    The duplicate loses its place on the board but not its history: it is retired
    to status='merged', never deleted.
    """
    from app.merge import merge_duplicate_groups
    with session_scope() as session:
        plans = merge_duplicate_groups(session, dry_run=not apply)
    if not plans:
        typer.echo("no duplicate groups to merge")
        return
    for p in plans:
        verb = "merged" if apply else "would merge"
        typer.echo(f"  [{p['rule']}] {verb} #{p['merge_id']} into #{p['survivor_id']}"
                   f"  {p['name'][:56]}")
        if apply:
            r = p["result"]
            typer.echo(f"      moved={r['moved'] or 'nothing'} "
                       f"dropped_dupe_links={r['dropped_as_duplicate_links'] or 'none'}")
    if not apply:
        typer.secho(f"\n{len(plans)} merge(s) planned. Re-run with --apply to do it.",
                    fg=typer.colors.YELLOW)


@app.command("backfill-stage-observations")
def backfill_stage_observations_cmd() -> None:
    """One-time: reconstruct the stage ledger for signals linked before it
    existed. Idempotent — safe to re-run, and normal `scout resolve` runs
    keep the ledger current going forward on its own."""
    from app.pipeline.resolve import backfill_stage_observations
    with session_scope() as session:
        written = backfill_stage_observations(session)
    typer.echo(f"wrote {written} stage observation(s)")


@app.command()
def score() -> None:
    """SIZE + SCORE: recompute tonnage and priority for all active projects."""
    from app.pipeline.size_score import run_size_score
    cfg = load_config()
    with session_scope() as session:
        stats = run_size_score(session, cfg)
    typer.echo(json.dumps(stats))


@app.command()
def notify() -> None:
    """NOTIFY: send the daily digest (new/changed only)."""
    from app.pipeline.notify import run_notify
    cfg = load_config()
    with session_scope() as session:
        result = run_notify(session, cfg)
    typer.echo(json.dumps(result))


@app.command()
def pipeline() -> None:
    """Run the full pipeline: fetch → triage → extract → resolve → score →
    notify → build-retrofit-buildings → find-replacement-candidates (the
    last one Sundays only — see RETROFIT_WEEKLY_WEEKDAY above). Pings the
    dead man's switch (HEALTHCHECK_URL) on completion, and records a
    pipeline_run row for the in-app staleness alarm (`scout check-freshness`
    / the root dashboard banner) — see app.pipeline_health, which also now
    tracks retrofit_buildings freshness per population, not just this run's
    own success/failure."""
    from app.models import utcnow
    from app.ops import ping_healthcheck
    from app.pipeline_health import (
        finish_pipeline_run,
        heartbeat,
        reap_stale_runs,
        records_processed_since,
        start_pipeline_run,
    )
    from app.spend import BudgetExceeded, run_budget

    with session_scope() as session:
        # Before starting a new run, reclassify anything still marked
        # "running" from a prior process that was killed externally (OOM,
        # confirmed real 2026-08-13) -- otherwise a dead row sits there
        # forever and last_successful_run()/hours_stale() never recover.
        reap_stale_runs(session)
        run = start_pipeline_run(session)
        run_id, run_started_at = run.id, run.started_at

    failures = 0
    errors: list[str] = []
    # ONE budget for the whole pipeline, opened here. The per-stage run_budget()
    # calls nest into this one rather than opening their own, so a nightly cron
    # is capped once end-to-end — and because the cap is fixed at open, a run that
    # starts before midnight and finishes after it cannot pick up a second day's
    # allowance partway through.
    with run_budget("pipeline"):
        # grounding sits between extract and resolve on purpose: it is the last point
        # where a fabricated number can be caught before it becomes a project, a
        # tonnage estimate and a row someone quotes.
        #
        # The two retrofit steps sit last, after notify: they are a fully
        # separate subsystem (EquipmentPermit/AssessorCandidate -> derived
        # RetrofitBuilding rows, never joined against Project/Signal), so
        # ordering relative to the project-signal steps above doesn't affect
        # correctness -- but putting them last means a slow or failing
        # retrofit step can never delay or block the digest email, the most
        # time-sensitive thing this pipeline produces. Each is its own
        # try/except in this same loop (see below), same as every other
        # stage, so a retrofit failure is recorded and surfaced exactly like
        # a fetch/triage/extract failure -- it cannot abort a later step,
        # because there IS no later step for it to abort by the time it runs.
        for step in (fetch, triage, extract, grounding, resolve, score, notify,
                    build_retrofit_buildings_cmd, find_replacement_candidates_cmd):
            if step is find_replacement_candidates_cmd and utcnow().weekday() != RETROFIT_WEEKLY_WEEKDAY:
                typer.echo(f"--- {step.__name__} (skipped -- weekly, Sundays only) ---")
                continue
            typer.echo(f"--- {step.__name__} ---")
            try:
                if step is fetch:
                    step(source=None)
                elif step is resolve:
                    step(no_llm=False)
                elif step is grounding:
                    # Report, never halt the run: the unit guard has already nulled
                    # what it could prove wrong, and anything still flagged is for a
                    # human to look at, not a reason to skip scoring.
                    step(strict=False)
                elif step is find_replacement_candidates_cmd:
                    # Real values, not this command's typer.Option(...) defaults --
                    # calling a Typer command directly in Python leaves those
                    # OptionInfo objects unresolved (same reason fetch/resolve/
                    # grounding above pass explicit values instead of calling bare).
                    step(year_built_before=2010, use_codes=None, min_sqft=None, top=50)
                else:
                    step()
            except BudgetExceeded as exc:
                typer.echo(f"{step.__name__} STOPPED BY BUDGET: {exc}", err=True)
                failures += 1
                errors.append(f"{step.__name__}: STOPPED BY BUDGET: {exc}")
            except Exception as exc:  # noqa: BLE001 — later stages still run; failure is visible
                typer.echo(f"{step.__name__} FAILED: {exc}", err=True)
                failures += 1
                errors.append(f"{step.__name__}: {exc}")
            # Touched once per stage regardless of outcome -- a stage that
            # failed still proves the process is alive, which is all a
            # heartbeat claims. fetch also heartbeats per-source internally
            # (app.pipeline.fetch:run_fetch), since it's the one stage
            # observed running long enough that a stage-boundary-only
            # heartbeat could plausibly go stale while genuinely still alive.
            with session_scope() as session:
                heartbeat(session)
    # The switch measures "the cron ran to completion", not "every source was
    # healthy" — per-source failures already alert via digest + dashboard.
    ping_healthcheck(success=True)
    with session_scope() as session:
        processed = records_processed_since(session, run_started_at)
        finish_pipeline_run(session, run_id, status="failed" if failures else "success",
                            records_processed=processed, error="; ".join(errors) or None)
    if failures:
        raise typer.Exit(1)


@app.command()
def doctor() -> None:
    """Health check: DB, API key, disk, per-source freshness, dead man's switch, budget."""
    from app.ops import doctor as run_doctor
    checks = run_doctor()
    bad = 0
    for name, ok, detail in checks:
        mark = "OK  " if ok else "FAIL"
        if not ok:
            bad += 1
        typer.echo(f"[{mark}] {name:24s} {detail}")
    if bad:
        typer.echo(f"{bad} check(s) failing")
        raise typer.Exit(1)
    typer.echo("all checks passing")


@app.command("check-freshness")
def check_freshness_cmd() -> None:
    """Is the pipeline actually still landing data, AND is retrofit_buildings
    actually still being rebuilt (see app.pipeline_health module docstring
    for why that second question needs its own check). No successful
    pipeline_run within 36h, or any RetrofitBuilding population past its own
    rebuild cadence, logs a warning and sends one combined Resend alert
    (rate-limited to once per 24h) -- same check the root dashboard view
    runs on every request, see app.pipeline_health.check_and_alert_staleness."""
    from app.pipeline_health import check_and_alert_staleness
    cfg = load_config()
    with session_scope() as session:
        result = check_and_alert_staleness(session, cfg)

    for population, info in result["retrofit"].items():
        if info["stale"]:
            detail = ("never built" if info["hours_stale"] is None
                      else f"last rebuilt {info['hours_stale']:.1f}h ago (threshold {info['threshold_hours']:.0f}h)")
            typer.echo(f"[STALE] retrofit_buildings population {population!r}: {detail}", err=True)
        else:
            typer.echo(f"[OK]    retrofit_buildings population {population!r}: "
                       f"rebuilt {info['hours_stale']:.1f}h ago")

    if result["last_success_at"] is None:
        typer.echo("[STALE] no successful pipeline run has ever been recorded", err=True)
    elif result["hours_stale"] is not None:
        mark = "STALE" if result["hours_stale"] > result["threshold_hours"] else "OK   "
        typer.echo(f"[{mark}] pipeline: last successful run {result['hours_stale']:.1f}h ago "
                   f"({result['last_success_at']:%Y-%m-%d %H:%M} UTC)")

    if not result["stale"]:
        return
    typer.echo(f"alert email sent this check: {result['alert_sent']}")
    raise typer.Exit(1)


@app.command("sam-gov")
def sam_gov_cmd(
    posted_from: str = typer.Option(..., "--from", help="MM/dd/yyyy, SAM.gov's required range start"),
    posted_to: str = typer.Option(..., "--to", help="MM/dd/yyyy, SAM.gov's required range end"),
    max_notices: int = typer.Option(None, help="Cap notices processed this run (attachment/LLM cost control)"),
    scheduled: bool = typer.Option(
        False, "--scheduled", help="Unattended/automated invocation -- caps search calls to "
                                   "sam_gov.scheduled_call_budget (smaller) instead of "
                                   "sam_gov.daily_call_budget, reserving quota for manual use "
                                   "the same day. A future cron entry should always pass this."),
) -> None:
    """Division 23 spec-mention competitive intelligence from SAM.gov --
    NAVFAC Southwest + CA/AZ/NV mechanical solicitations. Writes
    spec_mentions + sam_solicitation_checks, never projects/signals -- see
    app.pipeline.sam_gov's module docstring. NOT part of `scout pipeline`:
    SAM.gov's search endpoint is rate-limited to roughly 10 requests/day on
    a personal API key, an entirely different operating constraint than the
    other sources, so this is invoked on its own schedule -- and self-limits
    per run via --scheduled/sam_gov's daily/scheduled call budgets (see that
    module's guard) rather than trusting the caller to have counted."""
    from app.pipeline.sam_gov import run_sam_gov
    from app.spend import run_budget
    cfg = load_config()
    with run_budget("sam_gov"), session_scope() as session:
        stats = run_sam_gov(session, cfg, posted_from=posted_from, posted_to=posted_to,
                            max_notices=max_notices, is_scheduled=scheduled)
    typer.echo(json.dumps(stats))


@app.command("cslb")
def cslb_cmd(
    geocode_batch_limit: int = typer.Option(
        None, help="Cap geocoding to this many batches of up to 10,000 addresses each "
                   "(cost/time control for a first run against the full ~49k-contractor scope)"),
) -> None:
    """Import CSLB (California Contractors State License Board) contractor
    licenses -- C-20/C-38/B classifications, LA/Orange/Riverside/San
    Bernardino/Ventura/San Diego/Imperial counties. Downloads CSLB's own
    free public "License Master" bulk file, upserts Contractor rows keyed
    by license number, geocodes new/changed addresses. See
    app.pipeline.cslb's module docstring for the compliance check this was
    built against and app/models.py's Contractor docstring for what gets
    stored."""
    from app.pipeline.cslb import run_cslb_import
    with session_scope() as session:
        stats = run_cslb_import(session, geocode_batch_limit=geocode_batch_limit)
    typer.echo(json.dumps(stats))


@app.command("geocode-retrofit")
def geocode_retrofit_cmd(
    batch_limit: int = typer.Option(
        None, help="Cap geocoding to this many batches of up to 10,000 addresses each"),
    top_n: int = typer.Option(
        None, "--top-n", help="Geocode only the top-N highest rank_score rows, not the whole population "
                              "-- for a bounded/staged run against a large backlog"),
    population: str = typer.Option("replacement_candidate", help="RetrofitBuilding.population to scope to"),
) -> None:
    """Geocode RetrofitBuilding rows that have never been geocoded, into
    retrofit_geocodes -- see app.pipeline.retrofit.geocode_retrofit_buildings
    and RetrofitGeocode's docstring for why this is a separate durable table
    rather than a column on RetrofitBuilding itself (which gets deleted and
    rebuilt from scratch on every `scout retrofit-*` run)."""
    from app.pipeline.retrofit import geocode_retrofit_buildings
    with session_scope() as session:
        stats = geocode_retrofit_buildings(session, batch_limit=batch_limit, top_n=top_n, population=population)
    typer.echo(json.dumps(stats))


@app.command("match-contractors")
def match_contractors_cmd(
    radius_miles: float = typer.Option(None, help="Override contractors.default_radius_miles"),
) -> None:
    """Precompute each geocoded contractor's nearby replacement-candidate
    count -- see app.contractors.match_contractors for why this is cached
    rather than computed per page view, and /contractors, which sorts on
    this cached value."""
    from app.contractors import match_contractors
    cfg = load_config()
    with session_scope() as session:
        stats = match_contractors(session, cfg, radius_miles=radius_miles)
    typer.echo(json.dumps(stats))


@app.command("access-summary")
def access_summary_cmd(days: int = typer.Option(7, help="Look-back window")) -> None:
    """Distinct dashboard usernames seen in the last N days, first/last seen,
    and a page count -- same query app/web/main.py's /admin/access renders,
    see app.access_log.access_summary."""
    from app.access_log import access_summary
    with session_scope() as session:
        rows = access_summary(session, days=days)
    if not rows:
        typer.echo(f"No usernames recorded in the last {days} days.")
        return
    for row in rows:
        typer.echo(f"{row['username']:20s} first={row['first_seen']:%Y-%m-%d %H:%M} "
                   f"last={row['last_seen']:%Y-%m-%d %H:%M}  pages={row['page_count']}")


@app.command("fix-state-values")
def fix_state_values_cmd() -> None:
    """One-time (and safe to re-run) cleanup: normalize any non-canonical
    state value (e.g. "Nevada" instead of "NV") left over from before
    normalize_state() existed at the write sites. See `scout doctor`'s
    state_values check."""
    from app.ops import fix_state_values
    with session_scope() as session:
        changed = fix_state_values(session)
    total = len(changed["projects"]) + len(changed["signals"])
    if not total:
        typer.echo("nothing to fix — every state value already canonical")
        return
    for table, rows in changed.items():
        for line in rows:
            typer.echo(f"  {table}: {line}")
    typer.echo(f"fixed {total} row(s)")


@app.command("import-iepr")
def import_iepr_cmd(
    path: str = typer.Argument(..., help="Path to the downloaded IEPR .xlsx filing"),
    utility: str = typer.Option(..., help='e.g. "SCE"'),
    docket_tn: str = typer.Option(..., help="CEC transaction number, e.g. 266008"),
    source_url: str = typer.Option(..., help="Public efiling.energy.ca.gov URL this file came from"),
) -> None:
    """Manual, twice-yearly import of a utility's CEC IEPR large-load forecast
    filing into the county-level forward-MW layer (see app/pipeline/iepr.py).
    Not a scraper: download the .xlsx from efiling.energy.ca.gov by hand first.
    Re-running with the same --utility/--docket-tn replaces that filing's rows."""
    from app.pipeline.iepr import import_iepr_workbook
    with session_scope() as session:
        stats = import_iepr_workbook(session, path, utility=utility, docket_tn=docket_tn,
                                     source_url=source_url)
    typer.echo(f"{stats['utility']} TN{stats['docket_tn']}: "
               f"{stats['rows_stored']}/{stats['rows_in_sheet']} rows stored")
    if stats["unmapped_cities"]:
        typer.echo("  unmapped cities (county left null — add to _CITY_TO_COUNTY if real):")
        for city, n in sorted(stats["unmapped_cities"].items(), key=lambda kv: -kv[1]):
            typer.echo(f"    {city!r}: {n} row(s)")


@app.command("enrichment-worklist")
def enrichment_worklist_cmd(limit: int = typer.Option(None, help="Show only the top N")) -> None:
    """Free, read-only: firms (MEP/GC/mech-contractor) on active projects with
    no individual Contact, ranked by how many active projects they're on.
    This is the batch to review before spending any Apollo/Lusha credits —
    see app/enrichment.py. Running this never costs anything."""
    from app.enrichment import firms_needing_enrichment
    with session_scope() as session:
        worklist = firms_needing_enrichment(session, limit=limit)
    if not worklist:
        typer.echo("no firms need enrichment right now")
        return
    typer.echo(f"{len(worklist)} firm(s) worth enriching:")
    for w in worklist:
        typer.echo(f"  {w['n_projects']:>2} proj  {w['firm_type']:15s} {w['firm']}")


@app.command("import-enriched-contacts")
def import_enriched_contacts_cmd(path: str = typer.Argument(
        ..., help="JSON file: list of {firm, firm_type, name, title, phone, email, source, project_ids}")) -> None:
    """Import Apollo/Lusha lookup results run OUTSIDE this codebase (Scout has
    no Apollo/Lusha API keys of its own — see app/enrichment.py) into the
    contacts table, flagged by source so the board can show which is which.

    A row with neither phone nor email is stored as reach_status="pending"
    (name known, ladder shows "one phone call away") via
    import_pending_contact — a distinct state, not a relaxed version of the
    confirmed-reachable path. Any row DOES supply phone or email goes through
    import_enriched_contact, which still refuses a contact with neither."""
    import json
    from app.enrichment import import_enriched_contact, import_pending_contact
    rows = json.loads(open(path).read())
    stored, pending, skipped = 0, 0, []
    with session_scope() as session:
        for row in rows:
            try:
                if row.get("phone") or row.get("email"):
                    import_enriched_contact(
                        session, firm_name=row["firm"], firm_type=row.get("firm_type"),
                        name=row["name"], title=row.get("title"), phone=row.get("phone"),
                        email=row.get("email"), source=row["source"],
                        project_ids=row.get("project_ids", []))
                    stored += 1
                else:
                    import_pending_contact(
                        session, firm_name=row["firm"], firm_type=row.get("firm_type"),
                        name=row["name"], title=row.get("title"), source=row["source"],
                        project_ids=row.get("project_ids", []))
                    pending += 1
            except ValueError as exc:
                skipped.append(f"{row.get('name')!r} at {row.get('firm')!r}: {exc}")
    typer.echo(f"stored {stored} confirmed-reachable, {pending} pending (name only) contact(s)")
    for line in skipped:
        typer.echo(f"  SKIPPED: {line}")


@app.command("log-outreach-from-fathom")
def log_outreach_from_fathom_cmd(
    project_id: int = typer.Option(..., help="Scout project ID -- Fathom has no idea what this is"),
    transcript_file: str = typer.Option(..., help="Path to the fetched Fathom transcript text"),
    summary_file: str = typer.Option(None, help="Path to the fetched Fathom AI summary, if any"),
    contact_id: int = typer.Option(None),
    meeting_date: str = typer.Option(None, help="ISO date the call happened, from Fathom's list_meetings"),
    fathom_url: str = typer.Option(None, help="Fathom recording URL, appended to the outreach notes"),
) -> None:
    """Fathom transcript -> one Outreach row, via an LLM summarizing what
    happened on THIS call. See app/pipeline/fathom_outreach.py. Fetch the
    transcript with Fathom's MCP tools first (list_meetings ->
    get_meeting_transcript) and save it to a file -- Scout has no Fathom API
    key of its own, same shape as the Apollo/Lusha enrichment bridge."""
    from datetime import datetime
    from app.pipeline.fathom_outreach import log_outreach_from_fathom

    transcript = open(transcript_file).read()
    summary = open(summary_file).read() if summary_file else None
    parsed_date = datetime.fromisoformat(meeting_date) if meeting_date else None

    with session_scope() as session:
        outreach = log_outreach_from_fathom(
            session, project_id=project_id, transcript=transcript, summary=summary,
            contact_id=contact_id, meeting_date=parsed_date, fathom_url=fathom_url)
        session.flush()
        typer.echo(f"logged outreach #{outreach.id} on project {project_id}")
        typer.echo(f"  notes: {outreach.notes[:200]}")
        if outreach.next_action:
            typer.echo(f"  next action: {outreach.next_action} "
                       f"({outreach.next_action_date.date() if outreach.next_action_date else 'no date stated'})")


@app.command("import-hcai")
def import_hcai_cmd(
    path: str = typer.Argument(..., help="Path to the downloaded CHHS 'Total Construction "
                                          "Cost of Healthcare Projects' CSV"),
    source_url: str = typer.Option(
        "https://data.chhs.ca.gov/dataset/total-construction-cost-of-healthcare-projects",
        help="Public data.chhs.ca.gov dataset page this file came from"),
) -> None:
    """Manual import of HCAI's county-level healthcare construction activity
    (see app/pipeline/hcai.py) -- a county-level AGGREGATE layer, same shape
    as `scout import-iepr`. Not a scraper: the CSV's own download URL embeds
    a changing generation date and the API path that would let code discover
    it automatically is robots.txt-disallowed. Download by hand from
    data.chhs.ca.gov first. Each import replaces every prior row."""
    from app.pipeline.hcai import import_hcai_snapshot
    with session_scope() as session:
        stats = import_hcai_snapshot(session, path, source_url=source_url)
    typer.echo(f"snapshot {stats['snapshot_date']}: "
               f"{stats['rows_stored']}/{stats['rows_in_file']} rows stored")
    if stats["skipped"]:
        typer.echo(f"  skipped (unparseable county): {', '.join(stats['skipped'][:10])}")


@app.command("fetch-permits")
def fetch_permits_cmd(
    since: str = typer.Option(None, help="ISO date; only permits issued on/after this (2020_present window only)"),
    limit: int = typer.Option(5000, help="Max rows per fetch (Socrata page size)"),
    window: str = typer.Option("2020_present", help="2020_present | 2010_2019 | before_2010 | all"),
) -> None:
    """LA City mechanical permits -> install-year evidence for SB 1206's
    R-410A inference, plus work-description-mined equipment count/tonnage.
    See app/pipeline/permits.py. Safe to re-run — upserts by permit_nbr.
    --window all pulls all three date-window datasets in one run."""
    from datetime import datetime
    from app.http import PoliteClient
    from app.pipeline.permits import DATASETS, fetch_la_mechanical_permits
    parsed_since = datetime.fromisoformat(since) if since else None
    windows = list(DATASETS.keys()) if window == "all" else [window]
    with session_scope() as session, PoliteClient() as client:
        for w in windows:
            stats = fetch_la_mechanical_permits(session, load_config(), client,
                                                since=parsed_since, limit=limit, window=w)
            typer.echo(f"[{w}] fetched {stats.get('fetched', 0)}, stored {stats.get('stored', 0)}, "
                       f"SB 1206-flagged {stats.get('sb1206_flagged', 0)}")
            if stats.get("error"):
                typer.echo(f"  ERROR: {stats['error']}", err=True)


@app.command("build-retrofit-buildings")
def build_retrofit_buildings_cmd() -> None:
    """Dedup EquipmentPermit to one row per building (population=
    "recently_active" — buildings WITH permit evidence, market/contractor
    intelligence, not a due/overdue list), join assessor parcel
    characteristics, evaluate regulatory triggers, rank. See
    app/pipeline/retrofit.py. Replaces this population's rows only — it's a
    derived view over permits/assessor data, not its own source."""
    from app.http import PoliteClient
    from app.pipeline.retrofit import build_retrofit_buildings
    with session_scope() as session, PoliteClient() as client:
        stats = build_retrofit_buildings(session, load_config(), client)
    typer.echo(f"{stats['permits_considered']} permits considered "
               f"({stats['permits_masked_apn_skipped']} skipped, privacy-masked APN)")
    typer.echo(f"{stats['distinct_buildings']} distinct buildings")
    typer.echo(f"assessor join: {stats['assessor_matched']} matched, "
               f"{stats['assessor_unmatched']} unmatched")


@app.command("find-replacement-candidates")
def find_replacement_candidates_cmd(
    year_built_before: int = typer.Option(2010, help="Parcels built before this year"),
    use_codes: str = typer.Option(None, help="Comma-separated UseCodeDescChar1 values "
                                              "(default: config.yaml retrofit.candidate_use_codes)"),
    min_sqft: float = typer.Option(None, help="Sqft floor (default: config.yaml retrofit.candidate_min_sqft)"),
    top: int = typer.Option(50, help="Rows to print in the gate report"),
) -> None:
    """The ABSENCE query (population="replacement_candidate"): commercial/
    industrial parcels built before the given year, at or above the sqft
    floor, with NO mechanical permit on record at all across the full
    permit window. This is the real retrofit-opportunity population — a
    permit means someone already replaced; absence means original equipment
    or an unpermitted swap, either way a live candidate, though absence is
    an inference, not proof (see find_replacement_candidates' docstring for
    the expected false-positive direction). Run `scout fetch-permits
    --window all` first for full 2010-present permit coverage. See
    app/pipeline/retrofit.py."""
    from app.http import PoliteClient
    from app.pipeline.retrofit import find_replacement_candidates
    codes = [c.strip() for c in use_codes.split(",")] if use_codes else None
    from sqlmodel import select
    from app.models import RetrofitBuilding
    with session_scope() as session, PoliteClient() as client:
        stats = find_replacement_candidates(session, load_config(), client,
                                            year_built_before=year_built_before,
                                            use_codes=codes, min_sqft=min_sqft)

        typer.echo(f"Filter chain (built before {stats['year_built_before']}, "
                   f"use codes {stats['use_codes']}, sqft >= {stats['min_sqft'] or 0:,.0f}):")
        typer.echo(f"  {stats['use_code_match']:>8,}  use-code match")
        typer.echo(f"  {stats['built_before']:>8,}  + built before {stats['year_built_before']}")
        typer.echo(f"  {stats['sqft_floor_survivors']:>8,}  + sqft floor")
        typer.echo(f"  {stats['commercial_parcels_scanned']:>8,}  scanned (fetch may cap pages — see max_pages)")
        typer.echo(f"  {stats['already_permitted_excluded']:>8,}  - already permitted (APN match, excluded)")
        typer.echo(f"  {stats['masked_or_null_apn_address_excluded']:>8,}  - already permitted (address match on "
                   f"masked/missing-APN permit, excluded)")
        typer.echo(f"  {stats['replacement_candidates']:>8,}  = replacement candidates — the real opportunity size")
        typer.echo(f"  {stats['service_life_abstained']:>8,}  of those ABSTAIN from service-life scoring — "
                   f"built more than two average service cycles before permit records begin; they rank on "
                   f"size/use code alone, see app.pipeline.retrofit:find_replacement_candidates")

        ranked = session.exec(
            select(RetrofitBuilding)
            .where(RetrofitBuilding.population == "replacement_candidate")
            .order_by(RetrofitBuilding.rank_score.desc().nulls_last())
            .limit(top)
        ).all()
        typer.echo(f"\nTop {len(ranked)} ranked (age past service life, size, regulatory proximity — tiered, "
                   f"see rank_buildings). Absence is an inference, not proof — see the docstring/board for the "
                   f"expected false-positive direction:")
        for i, b in enumerate(ranked, 1):
            tons = (f"{b.estimated_tons_low:.0f}-{b.estimated_tons_high:.0f}t est."
                   if b.estimated_tons_low else "tonnage n/a")
            years_past = f" {b.service_life_years_past:+.0f}yr" if b.service_life_years_past is not None else ""
            status = f"{b.service_life_status or 'unknown'}{years_past}" + (f" ({b.equipment_age_years:.0f}yr)" if b.equipment_age_years else "")
            regs = ", ".join(r for r in [
                "SB1206" if b.sb1206_trigger_status else None,
                f"CARB({b.carb_use_code})" if b.carb_candidate else None,
                "EBEWE" if b.ebewe_candidate else None,
            ] if r) or "none"
            addr = b.address or f"APN {b.apn}"
            typer.echo(f"  {i:>3}. {addr[:45]:<45} built {b.year_built or '?'} | {tons:<14} | "
                       f"{status:<20} | {regs}")


@app.command("report-service-frequency")
def report_service_frequency_cmd(
    apn: str = typer.Argument(..., help="Building APN — see the retrofit board or /retrofit/report"),
    calls_per_year: float = typer.Argument(..., help="Actual reported service calls in the past year"),
    source: str = typer.Option(..., help="Who reported it — a named contractor/company, not \"a contractor\""),
    reported_date: str = typer.Option(..., "--reported-date", help="YYYY-MM-DD — when THEY reported it"),
    equipment_note: str = typer.Option(None, help="Which unit, if the report is about one piece of equipment "
                                                    "rather than the whole building"),
) -> None:
    """Manual entry only — there is no scraper and there will not be one.

    Actual reported service frequency is a stronger replacement signal than
    the assessor YearBuilt proxy the rest of the retrofit board runs on (see
    app.pipeline.retrofit:rank_buildings). This writes to
    service_frequency_reports (durable, survives the board's own rebuild)
    and immediately patches any existing retrofit_buildings row for this apn
    so the effect is visible without waiting for a full rebuild — the next
    `build-retrofit-buildings` / `find-replacement-candidates` run re-derives
    the same thing from the same table.

    This is a hypothesis with exactly one data point as of 2026-08-11.
    Entering a figure does not tune scoring — it only lets THIS building's
    row use the override; see app.assumptions and
    app.pipeline.retrofit:service_calls_coverage for how thin the sample is."""
    from datetime import datetime as dt

    from app.models import RetrofitBuilding, ServiceFrequencyReport
    from app.pipeline.retrofit import rank_buildings, service_calls_coverage

    reported_at = dt.strptime(reported_date, "%Y-%m-%d")

    with session_scope() as session:
        report = ServiceFrequencyReport(
            apn=apn, service_calls_per_year=calls_per_year, source=source,
            reported_at=reported_at, equipment_note=equipment_note,
        )
        session.add(report)

        existing = session.exec(select(RetrofitBuilding).where(RetrofitBuilding.apn == apn)).all()
        for b in existing:
            b.service_calls_per_year = calls_per_year
            b.service_calls_per_year_source = source
            b.service_calls_per_year_reported_at = reported_at
            b.rank_score = rank_buildings(
                service_life_status=b.service_life_status, sqft=b.sqft,
                sb1206_trigger_status=b.sb1206_trigger_status, ebewe_candidate=b.ebewe_candidate,
                carb_candidate=b.carb_candidate, service_life_years_past=b.service_life_years_past,
                service_calls_per_year=calls_per_year,
            )
            session.add(b)
        session.commit()

        coverage = service_calls_coverage(session)

    typer.echo(f"Recorded: {apn} — {calls_per_year:.0f} calls/yr, reported by {source!r} on {reported_date}")
    if equipment_note:
        typer.echo(f"  re: {equipment_note}")
    if existing:
        typer.echo(f"Patched {len(existing)} existing retrofit_buildings row(s) for this apn — new rank_score "
                   f"applied immediately, no rebuild needed.")
    else:
        typer.echo("No existing retrofit_buildings row for this apn yet — this report will apply the next time "
                   "build-retrofit-buildings or find-replacement-candidates runs.")
    typer.echo(f"Coverage: {coverage['retrofit_buildings_with_service_calls']} of "
               f"{coverage['retrofit_buildings_total']} retrofit_buildings rows now carry a reported figure "
               f"({coverage['distinct_apns_with_a_report']} distinct buildings reported on, ever).")


@app.command("fetch-assessor-candidates")
def fetch_assessor_candidates_cmd(
    trigger: str = typer.Option("all", help="carb | ebewe | all"),
    roll_year: str = typer.Option("2025"),
    max_pages: int = typer.Option(20),
) -> None:
    """LA County assessor parcel roll -> CANDIDATE parcels for the CARB
    refrigeration-management and/or LA EBEWE audit triggers, by use code /
    building size. Never a confirmed filer list — see app/pipeline/assessor.py."""
    from app.http import PoliteClient
    from app.pipeline.assessor import fetch_carb_candidates, fetch_ebewe_candidates
    cfg = load_config()
    with session_scope() as session, PoliteClient() as client:
        if trigger in ("carb", "all"):
            stats = fetch_carb_candidates(session, cfg, client, roll_year=roll_year, max_pages=max_pages)
            typer.echo(f"CARB candidates: fetched {stats.get('fetched', 0)}, stored {stats.get('stored', 0)}")
        if trigger in ("ebewe", "all"):
            stats = fetch_ebewe_candidates(session, cfg, client, roll_year=roll_year, max_pages=max_pages)
            typer.echo(f"EBEWE candidates: fetched {stats.get('fetched', 0)}, stored {stats.get('stored', 0)}")


@app.command("fetch-dc-news-enrichment")
def fetch_dc_news_enrichment_cmd() -> None:
    """Data Center Frontier / Data Center Dynamics RSS -- matches articles
    against EXISTING active data-center projects by developer name and
    attaches a corroborating signal. Never creates a project — see
    app/pipeline/dc_news_enrichment.py."""
    from app.http import PoliteClient
    from app.pipeline.dc_news_enrichment import run_dc_news_enrichment
    with session_scope() as session, PoliteClient() as client:
        stats = run_dc_news_enrichment(session, load_config(), client)
    typer.echo(f"{stats['entries_seen']} entries seen, {stats['matched']} matched, "
               f"{stats['attached']} attached")
    for name, fs in stats["feeds"].items():
        typer.echo(f"  {name}: {fs['entries']} entries, {fs['matched']} matched, "
                   f"{fs['attached']} attached, {fs['errors']} errors")


@app.command("add-signal")
def add_signal(
    signal_type: str = typer.Argument(..., help="e.g. engineer_move, prequal_invite, bid_invite, manual_tip"),
    summary: str = typer.Argument(...),
    project: str = typer.Option(None), developer: str = typer.Option(None),
    county: str = typer.Option(None), state: str = typer.Option(None),
    mw_it: float = typer.Option(None), mw_total: float = typer.Option(None),
    stage: str = typer.Option("unknown"),
    person: str = typer.Option(None), org: str = typer.Option(None),
    url: str = typer.Option(""),
    category: str = typer.Option("data_center", help="data_center | industrial"),
) -> None:
    """Log a manual signal (tip, engineer move, prequal/bid invite)."""
    from app.manual import add_manual_signal
    with session_scope() as session:
        s = add_manual_signal(session, signal_type, summary, project_name=project,
                              developer=developer, county=county, state=state, mw_it=mw_it,
                              mw_total=mw_total, stage=stage, person_name=person,
                              person_org=org, url=url, category=category)
        typer.echo(f"signal #{s.id} recorded ({s.signal_type.value}, {s.category.value}); "
                   f"run `scout resolve` to link it")


@app.command()
def backfill(
    source: list[str] = typer.Option(
        ..., "--source", "-s",
        help="Repeatable. Also accepts a comma-separated list. "
             "ceqanet | goed | edgar | legistar | civicplus | primegov"),
    since: str = typer.Option(..., help="YYYY-MM-DD start of historical window"),
    estimate: bool = typer.Option(False, help="Estimate LLM cost of the pending corpus, run nothing"),
    reset: bool = typer.Option(False, help="Discard checkpoints and refetch every chunk"),
    force: bool = typer.Option(False, help="Start even if a backfill of this source looks "
                                          "still running (use only if that run is dead)"),
) -> None:
    """Chunked, checkpointed historical pull, one source after another.

    Fetch only — after it completes, check the cost with --estimate, then run
    `scout triage` and `scout extract` in batches. A crash resumes at the first
    incomplete chunk.

    --source is repeatable, and was not always: as a single string, Click kept
    only the LAST value, so `--source ceqanet --source goed --source edgar`
    silently backfilled edgar alone. Unknown names now fail before any fetch
    rather than part-way through.
    """
    from datetime import datetime as dt

    from app.pipeline.backfill import (
        BACKFILLABLE, ConcurrentBackfill, estimate_cost, run_backfill,
    )
    cfg = load_config()
    if estimate:
        with session_scope() as session:
            typer.echo(json.dumps(estimate_cost(session, cfg), indent=2))
        return

    sources = [s.strip() for entry in source for s in entry.split(",") if s.strip()]
    seen: set[str] = set()
    sources = [s for s in sources if not (s in seen or seen.add(s))]
    unknown = [s for s in sources if s not in BACKFILLABLE]
    if unknown:
        raise typer.BadParameter(
            f"{', '.join(unknown)} does not support backfill; "
            f"choose from {', '.join(BACKFILLABLE)}")

    since_dt = dt.strptime(since, "%Y-%m-%d")
    summary: dict[str, dict] = {}
    for name in sources:
        typer.echo(f"--- backfill {name} since {since}")
        try:
            with session_scope() as session:
                summary[name] = run_backfill(session, cfg, name, since_dt,
                                             reset=reset, force=force)
        except ConcurrentBackfill as exc:
            # Skip this source, keep going: the point is to avoid two processes
            # racing on it, not to abandon the other sources on the command line.
            typer.echo(f"[SKIP] {name}: {exc}")
            summary[name] = {"skipped": "already running"}
            continue
        typer.echo(json.dumps({name: summary[name]}))
    if len(sources) > 1:
        typer.echo(json.dumps(summary))
    typer.echo("Fetch done. Now run: scout backfill --source X --since ... --estimate "
               "to price the LLM pass, then scout triage / scout extract.")


golden_app = typer.Typer(help="Golden-set extraction evaluation (Gate 2).")
app.add_typer(golden_app, name="golden")


@golden_app.command("collect")
def golden_collect(
    limit: int = 30,
    include_doc: list[int] = typer.Option(
        None, "--include-doc",
        help="Repeatable raw_document id to force into the set even if triage dropped "
             "it. Costs one extraction each. Use for head+tail fallbacks: "
             "scout doc-stats --fallbacks lists them."),
) -> None:
    """Snapshot extracted docs for hand-verification.

    Weighted toward head+tail fallbacks first, then CEQAnet environmental documents
    and CivicPlus packets — the filings that actually carry MW and generator figures.
    """
    from app.golden import collect, collect_forced
    with session_scope() as session:
        forced = collect_forced(session, list(include_doc or []))
        added = collect(session, limit=limit)
    if forced:
        typer.echo(f"{forced} triage-dropped documents force-included (no signals written)")
    typer.echo(f"{added} documents added to evals/golden.jsonl — now run: scout golden review")


@golden_app.command("worksheet")
def golden_worksheet(out: str = "evals/golden-worksheet.md") -> None:
    """Write the golden set as an offline worksheet, plus per-field fill rates."""
    from pathlib import Path

    from app.golden import fill_rates, write_worksheet
    n = write_worksheet(Path(out))
    typer.echo(f"{n} documents written to {out}\n")
    rates = fill_rates()
    typer.echo(f"{'field':24s}{'filled':>8}{'of':>5}{'rate':>8}")
    for f, r in sorted(rates.items(), key=lambda kv: -kv[1]["rate"]):
        typer.echo(f"{f:24s}{r['filled']:>8}{r['n']:>5}{r['rate']:>7.0%}")
    typer.echo("\nFill rate is not accuracy: a field can be fully populated and "
               "entirely wrong. Gate 0 needs the hand pass.")


@golden_app.command("review")
def golden_review() -> None:
    """Hand-verify each document: model extraction next to source text."""
    from app.golden import review
    done = review()
    typer.echo(f"{done} documents verified")


@golden_app.command("report")
def golden_report() -> None:
    """Per-field precision/recall + fabrication list. Exit 1 on any fabrication."""
    from app.golden import report_text, score
    result = score()
    typer.echo(report_text(result))
    if result["fabrications"]:
        raise typer.Exit(1)


@app.command()
def brief(project_id: int) -> None:
    """Print the one-page project brief as markdown (pipe to a file for sharing)."""
    from app.brief import brief_markdown, build_brief
    with session_scope() as session:
        typer.echo(brief_markdown(build_brief(session, project_id)))


@app.command()
def outcomes() -> None:
    """Which signal types preceded conversions vs deaths. Needs closed outcomes."""
    from app.outcomes import outcomes_report, report_text
    with session_scope() as session:
        typer.echo(report_text(outcomes_report(session)))


@app.command("outcome")
def outcome(project_id: int, status: str, reason: str = typer.Option("", help="Why")) -> None:
    """Record an outcome: contacted | specified | bidding | won | lost | dead."""
    from app.outcomes import record_outcome
    with session_scope() as session:
        ev = record_outcome(session, project_id, status, reason)
        typer.echo(f"project {project_id} -> {ev.status} ({ev.reason or 'no reason given'})")


@app.command()
def deliverables(out: str = "output", top_briefs: int = 10) -> None:
    """Produce output/: call-list.csv, top-N project briefs, baseline.json."""
    from pathlib import Path

    from app.deliverables import write_baseline, write_briefs, write_call_list
    out_dir = Path(out)
    with session_scope() as session:
        n = write_call_list(session, out_dir / "call-list.csv")
        briefs = write_briefs(session, out_dir / "briefs", top=top_briefs)
        base = write_baseline(session, out_dir / "baseline.json")
    typer.echo(f"call-list.csv: {n} PRE_BOD in-territory projects")
    typer.echo(f"briefs: {len(briefs)} written to {out_dir}/briefs/")
    typer.echo(f"baseline.json: {base['projects']} projects frozen")


@app.command()
def audit(top: int = 25) -> None:
    """Board precision audit: why each top row scored there + ranking-failure flags."""
    from app.audit import audit_board, audit_text
    cfg = load_config()
    with session_scope() as session:
        typer.echo(audit_text(audit_board(session, cfg, top=top)))


@app.command()
def ladder() -> None:
    """Contact ladder distribution: how many projects reach a callable human."""
    from app.ladder import contactability_text, distribution_text, ladder_distribution
    with session_scope() as session:
        dist = ladder_distribution(session)
        typer.echo(contactability_text(dist))
        typer.echo("\n" + distribution_text(dist))
        typer.echo("\nPer-project best contact:")
        mark = {"contactable": "CALL", "name_only": "name", "none": "----"}
        for row in sorted(dist["per_project"], key=lambda r: -r["score"]):
            rung = row["best_rung"] if row["best_rung"] else "—"
            reach = ""
            if row["reachable_name"]:
                how = row["reachable_phone"] or row["reachable_email"] or ""
                reach = (f"  -> {row['reachable_name']} "
                         f"({row['reachable_title'] or '?'}, {row['reachable_org'] or '?'}) {how}")
            typer.echo(f"  [{mark[row['contact_status']]}][{rung}] "
                       f"{row['project'][:40]:40s} {row['window']:8s} "
                       f"{row['score']:5.2f}  {row['best_name'] or 'NO CONTACT'}{reach}")


@app.command("validate-estimate")
def validate_estimate_cmd(
    since: str = typer.Option(..., help="YYYY-MM-DD[THH:MM] start of the spend window"),
    until: str = typer.Option(..., help="YYYY-MM-DD[THH:MM] end of the spend window"),
    source: list[str] = typer.Option(None, "--source", "-s",
                                     help="Restrict the corpus to these sources"),
) -> None:
    """Check the cost estimator against what was actually spent (invariant 8).

    Pick a window in which the documents still in the table are the documents
    that window paid for — a purge and re-backfill in between makes the
    comparison meaningless, and the report says so rather than quietly averaging
    across it. Exits 1 when the estimate is outside the Phase B tolerance.
    """
    from datetime import datetime as dt

    from app.pipeline.backfill import validate_estimate

    def parse(s: str) -> dt:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                return dt.strptime(s, fmt)
            except ValueError:
                continue
        raise typer.BadParameter(f"{s!r} is not YYYY-MM-DD or YYYY-MM-DDTHH:MM")

    cfg = load_config()
    with session_scope() as session:
        report = validate_estimate(session, cfg, parse(since), parse(until),
                                   sources=list(source) if source else None)
    typer.echo(json.dumps(report, indent=2))
    for stage, entry in report["stages"].items():
        if isinstance(entry, dict) and entry.get("corpus_matches_calls") is False:
            typer.echo(f"[WARN] {stage}: {entry['predicted_docs']} documents in the corpus "
                       f"but {entry['actual_calls']} calls in the window — the window and "
                       f"the corpus do not describe the same work, so treat the per-call "
                       f"numbers as indicative only.", err=True)
    if not report["gate_passed"]:
        typer.echo(f"[FAIL] estimator outside the {report['tolerance_pct']:.0f}% gate", err=True)
        raise typer.Exit(1)
    typer.echo(f"[OK] estimator within {report['tolerance_pct']:.0f}% on every checked term")


@app.command()
def coverage() -> None:
    """Per-county coverage: projects, stages, sources, latest signal, blind spots."""
    from app.coverage import coverage_report, coverage_text
    cfg = load_config()
    with session_scope() as session:
        typer.echo(coverage_text(coverage_report(session, cfg)))


@app.command("seed-firms")
def seed_firms_cmd() -> None:
    """Load the firm roster from config.yaml into the firms table."""
    from app.firms import seed_firms
    cfg = load_config()
    with session_scope() as session:
        added = seed_firms(session, cfg)
    typer.echo(f"{added} firms added (existing rows updated in place)")


@app.command("seed-lines")
def seed_lines_cmd() -> None:
    """Load the DMG/ToroAire line card from config.yaml into product_lines.

    Safe to re-run after correcting a category, value_tier or equipment_type in
    config.yaml — same idempotent-upsert pattern as seed-firms. Existing
    accounts automatically pick up any newly-added line on their next page
    load (see app.accounts.ensure_coverage_rows), no separate backfill needed.
    """
    from app.accounts import seed_product_lines
    cfg = load_config()
    with session_scope() as session:
        added = seed_product_lines(session, cfg)
    typer.echo(f"{added} product lines added (existing rows updated in place)")


@app.command("seed-selection-tools")
def seed_selection_tools_cmd() -> None:
    """Load accounts.selection_tools from config.yaml into selection_tools —
    one row per ProductLine, 70 total. A line not named in config.yaml gets
    an all-null, verification_status=unchecked row, not a skip — see
    app.accounts.seed_selection_tools. Safe to re-run."""
    from app.accounts import lines_needing_selection_tool_research, seed_selection_tools
    cfg = load_config()
    with session_scope() as session:
        added = seed_selection_tools(session, cfg)
        needs_research = [(line.name, line.firm) for line in lines_needing_selection_tool_research(session)]
    typer.echo(f"{added} selection_tools rows added (existing rows updated in place)")
    typer.echo(f"{len(needs_research)} of 70 lines still unchecked — needs research:")
    for name, firm in needs_research:
        typer.echo(f"  {name} ({firm})")


@app.command("compare-lines")
def compare_lines_cmd(
    tonnage: float = typer.Option(None, help="Facility tonnage, for context only — no line carries a "
                                               "tonnage capacity field, so this never filters candidates"),
    building_type: str = typer.Option(None, help="One of data_center, healthcare, industrial_warehouse, "
                                                   "education, hospitality, labs, office, multifamily"),
    latent_load_priority: bool = typer.Option(False),
    marine_or_corrosive: bool = typer.Option(False),
    water_available: bool = typer.Option(None, help="True/False; omit if unknown"),
    space_rigging_constrained: bool = typer.Option(False),
    redundancy_required: bool = typer.Option(False),
    buyer_type: str = typer.Option(None, help="owner_direct | spec_driven"),
) -> None:
    """Application-driven line comparability: candidates side by side with
    what each trades away. See app.compare's module docstring for the
    abstain rule — a null capability is never a vote against a line."""
    from app.compare import compare_lines

    with session_scope() as session:
        candidates = compare_lines(
            session, tonnage=tonnage, building_type=building_type,
            latent_load_priority=latent_load_priority, marine_or_corrosive=marine_or_corrosive,
            water_available=water_available, space_rigging_constrained=space_rigging_constrained,
            redundancy_required=redundancy_required, buyer_type=buyer_type,
        )
        for c in candidates:
            typer.echo(f"\n=== {c.line} ({c.building_role}) ===")
            if c.why_it_fits:
                typer.echo("  fits: " + " | ".join(c.why_it_fits))
            if c.trades_away:
                typer.echo("  trades away: " + " | ".join(c.trades_away))
            for k, v in c.eligibility_flags.items():
                if v is not None:
                    typer.echo(f"  {k}: {v.value} (checked {v.checked})")
            if c.competitors is not None:
                typer.echo(f"  competitors: {c.competitors.value} (checked {c.competitors.checked})")
            if c.known_limitations:
                typer.echo(f"  known limitations: {c.known_limitations}")
            typer.echo(f"  capability_gaps: {', '.join(c.capability_gaps) or 'none'}")
    typer.echo("\nLead time: not tracked for any line, intentionally omitted.")


@app.command("verify-sources")
def verify_sources(
    only: str = typer.Option("", help="Comma-separated adapter names; default all enabled"),
) -> None:
    """Live smoke-test every enabled adapter; reports URL-structure drift.

    OK only means documents came back. WARN means every request succeeded but
    nothing survived filtering, or some configured sub-target is dead. FAIL
    means the source is unusable. Exits 1 if anything FAILs.
    """
    from app.http import PoliteClient
    from app.sources import enabled_adapters, get_adapter
    cfg = load_config()
    names = [n.strip() for n in only.split(",") if n.strip()] or enabled_adapters(cfg)
    counts: dict[str, int] = {}
    with PoliteClient() as client:
        for name in names:
            result = get_adapter(name).verify(cfg, client)
            status = result.get("status", "fail")
            counts[status] = counts.get(status, 0) + 1
            typer.echo(f"[{status.upper():4}] {name}: {result['detail']}")
    typer.echo("  ".join(f"{k}={v}" for k, v in sorted(counts.items())) or "no adapters run")
    if counts.get("fail"):
        raise typer.Exit(1)


@app.command("doc-stats")
def doc_stats(
    fallbacks: bool = typer.Option(
        False, "--fallbacks",
        help="Instead of per-source stats, list documents where section chunking "
             "matched nothing and fell back to a head+tail sample."),
) -> None:
    """Row count and raw_text length per source, with stub detection.

    A source storing search-result metadata instead of documents looks healthy by
    every other measure — it has rows, its requests return 200, its last run is
    green. Average document length is what exposes it: EDGAR sat at 202 chars
    across 3,016 rows. Exits 1 if any source averages below its stub floor.
    """
    from sqlalchemy import func

    from app.models import RawDocument
    from app.sources import get_adapter, registry
    cfg = load_config()

    if fallbacks:
        from app.sections import select_relevant_text
        with session_scope() as session:
            docs = session.exec(select(RawDocument).order_by(RawDocument.id)).all()
            hits = [(d, select_relevant_text(d.raw_text, cfg)) for d in docs]
            hits = [(d, s) for d, s in hits if "tail_fallback" in s.sections_found]
            if not hits:
                typer.echo("no documents fall back to head+tail sampling")
                return
            typer.echo(f"{'id':>7} {'source':11} {'chars':>9} {'sent':>8} {'triage':11}  title")
            for d, s in sorted(hits, key=lambda x: -len(x[0].raw_text)):
                typer.echo(f"{d.id:>7} {d.source:11} {len(d.raw_text):>9,} "
                           f"{s.selected_chars:>8,} {d.triage_result.value:11}  {d.title[:44]}")
            typer.echo(f"\n{len(hits)} documents. The middle of each was never sent to the "
                       f"model — and triage only read the first "
                       f"{cfg.get('llm.triage_max_chars', 6000):,} chars. Sample them:")
            typer.echo("  scout golden collect " +
                       " ".join(f"--include-doc {d.id}" for d, _ in hits[:3]))
        return
    with session_scope() as session:
        rows = session.exec(
            select(RawDocument.source,
                   func.count(RawDocument.id),
                   func.avg(func.length(RawDocument.raw_text)),
                   func.min(func.length(RawDocument.raw_text)),
                   func.max(func.length(RawDocument.raw_text)))
            .group_by(RawDocument.source)
            .order_by(func.count(RawDocument.id).desc())
        ).all()

    if not rows:
        typer.echo("no documents stored")
        return
    typer.echo(f"{'source':12} {'rows':>7} {'avg chars':>10} {'min':>8} {'max':>9}  verdict")
    stubs = []
    for source, count, avg, lo, hi in rows:
        avg = int(avg or 0)
        floor = (get_adapter(source).min_doc_chars(cfg) if source in registry else 500)
        verdict = "ok"
        if avg < floor:
            verdict = f"STUBS (avg < {floor})"
            stubs.append(source)
        typer.echo(f"{source:12} {count:7} {avg:10} {int(lo or 0):8} {int(hi or 0):9}  {verdict}")
    typer.echo(f"total rows: {sum(r[1] for r in rows)}")
    if stubs:
        typer.echo(f"storing stubs rather than content: {', '.join(stubs)}")
        raise typer.Exit(1)


@app.command("grounding")
def grounding(strict: bool = typer.Option(
        False, "--strict", help="Exit 1 if any asserted number is ungrounded")) -> None:
    """Check every asserted number against its source document.

    Finds invented values, which is the failure that actually reaches a customer —
    a null costs an estimate, a fabricated MW costs credibility. It cannot find a
    misreading: a number can be present in the document and still be the wrong one,
    and only hand verification catches that.
    """
    from app.grounding import audit_corpus, audit_text
    with session_scope() as session:
        result = audit_corpus(session)
    typer.echo(audit_text(result))
    if strict and result["ungrounded"]:
        raise typer.Exit(1)


@app.command("purge-source")
def purge_source(
    source: str = typer.Option(..., help="Source name whose documents to delete"),
    yes: bool = typer.Option(False, "--yes", help="Required; deletion is irreversible"),
) -> None:
    """Delete every raw document for one source, plus everything derived from it.

    For when a source's stored corpus is wrong rather than merely stale — e.g.
    EDGAR's 3,016 keyword-match stubs, which had to go once the query was narrowed.

    Signals derived from those documents go too, and that is deliberate. This used
    to claim it left them alone, which was impossible: signals.raw_document_id is a
    foreign key with NO ACTION, so the bulk delete raised a ForeignKeyViolation the
    moment any purged document had been extracted. It never fired on EDGAR because
    those stubs were never extracted. Beyond the constraint, a signal whose source
    document no longer exists breaks the one guarantee this schema makes — every
    extracted field traces back to a raw_document row with a URL — so keeping it
    would be worse than deleting it.

    Projects are left in place; re-run `scout resolve` after the re-backfill to
    rebuild them from whatever signals survive.
    """
    from sqlalchemy import delete, func

    from app.models import (
        BackfillCheckpoint, MatchCandidate, ProjectSignal, RawDocument, Signal,
    )
    with session_scope() as session:
        n_docs = session.exec(select(func.count(RawDocument.id))
                              .where(RawDocument.source == source)).one()
        n_cps = session.exec(select(func.count(BackfillCheckpoint.id))
                             .where(BackfillCheckpoint.source == source)).one()
        doc_ids = select(RawDocument.id).where(RawDocument.source == source)
        sig_ids = select(Signal.id).where(Signal.raw_document_id.in_(doc_ids))
        n_sigs = session.exec(select(func.count(Signal.id))
                              .where(Signal.raw_document_id.in_(doc_ids))).one()
        n_links = session.exec(select(func.count(ProjectSignal.id))
                               .where(ProjectSignal.signal_id.in_(sig_ids))).one()
        if not yes:
            typer.echo(f"would delete {n_docs} documents, {n_sigs} signals and "
                       f"{n_links} project links plus {n_cps} checkpoints "
                       f"for {source!r}; re-run with --yes")
            return

        # Children first: the FK is NO ACTION, so order is load-bearing.
        session.exec(delete(MatchCandidate).where(MatchCandidate.signal_id.in_(sig_ids)))
        session.exec(delete(ProjectSignal).where(ProjectSignal.signal_id.in_(sig_ids)))
        session.exec(delete(Signal).where(Signal.raw_document_id.in_(doc_ids)))
        # Bulk DELETE, not per-row ORM deletes: thousands of round trips to a
        # remote Postgres takes minutes for what the server does in one statement.
        session.exec(delete(RawDocument).where(RawDocument.source == source))
        session.exec(delete(BackfillCheckpoint).where(BackfillCheckpoint.source == source))
        session.commit()
    typer.echo(f"deleted {n_docs} documents, {n_sigs} signals, {n_links} project links "
               f"and {n_cps} checkpoints for {source!r}")
    if n_sigs:
        typer.echo("run `scout resolve && scout score` to rebuild projects")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the dashboard."""
    import uvicorn
    uvicorn.run("app.web.main:app", host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
