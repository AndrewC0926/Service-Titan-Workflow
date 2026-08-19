"""Operational plumbing: dead man's switch ping, doctor checks, JSON logging."""
from __future__ import annotations

import json
import logging
import os
import shutil
from datetime import datetime, timedelta

import httpx
from sqlmodel import select

from app.config import anthropic_api_key, load_config
from app.db import get_engine, session_scope
from app.models import (
    ACTIVE_STATUSES, STAGE_RUN_NAMES, Project, ProjectSignal, Signal, SourceRun,
    run_name_mode, run_name_source, utcnow,
)

log = logging.getLogger(__name__)

HEALTHCHECK_ENV = "HEALTHCHECK_URL"  # e.g. https://hc-ping.com/<uuid>

DEFAULT_STALE_HOURS = 36


def stale_cutoff(cfg, name: str, now: datetime | None = None) -> datetime:
    """The per-source staleness cutoff: a source's own config entry may set
    `sources.<name>.stale_hours` to override the 36-hour default -- added
    for sources that update on a cadence other than daily (a weekly fetch,
    a manual import), where 36 hours would flag every single run as stale
    almost immediately after a perfectly healthy one. Every source that
    doesn't set this keeps the exact prior 36-hour behavior.

    Shared by `scout doctor` (below) and the digest's own staleness check
    (app.pipeline.notify._stale_sources) specifically so the two cannot
    independently drift out of sync about what "stale" means for the same
    source -- confirmed 2026-08-19 they already had: la_ebewe_benchmarking
    runs weekly (Sundays only) with no override set, so the digest's
    then-hardcoded 36-hour check flagged it as failing on 6 of every 7
    days despite a clean, on-schedule, zero-error run every single week."""
    if now is None:
        now = utcnow()
    hours = cfg.get(f"sources.{name}.stale_hours", DEFAULT_STALE_HOURS)
    return now - timedelta(hours=hours)


def ping_healthcheck(success: bool = True) -> bool:
    """Dead man's switch: healthchecks.io alerts if this ping stops arriving.
    Separate from source-failure alerting — this catches nothing running at all.

    Confirmed in production 2026-08-13: HEALTHCHECK_URL had a trailing '\\n'
    (almost certainly pasted in), which made httpx raise InvalidURL --  a
    plain ValueError subclass, not an httpx.HTTPError, so the old `except
    httpx.HTTPError` let it through uncaught. Because this call sits before
    finish_pipeline_run() in app.cli:pipeline, that crash killed the process
    before the run's own pipeline_run row could ever be marked finished --
    a self-inflicted version of the exact stuck-row problem the heartbeat
    mechanism exists to catch. .strip() defangs the specific cause; the
    broadened except is what actually makes this call live up to "dead
    man's switch", not "a second way for a healthy run to look dead"."""
    url = (os.environ.get(HEALTHCHECK_ENV) or "").strip()
    if not url:
        log.warning("%s not set — dead man's switch is DISARMED", HEALTHCHECK_ENV)
        return False
    target = url if success else url.rstrip("/") + "/fail"
    try:
        httpx.get(target, timeout=10)
        return True
    except Exception as exc:  # noqa: BLE001 — a ping failure must never take the pipeline down with it
        log.error("healthcheck ping failed: %s", exc)
        return False


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        entry = {
            "ts": utcnow().isoformat(timespec="seconds") + "Z",
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        if record.exc_info:
            entry["exc"] = self.formatException(record.exc_info)
        return json.dumps(entry)


def setup_logging() -> None:
    """LOG_FORMAT=json switches to structured logs (default on Render via render.yaml)."""
    handler = logging.StreamHandler()
    if os.environ.get("LOG_FORMAT") == "json":
        handler.setFormatter(JsonFormatter())
    else:
        handler.setFormatter(logging.Formatter(
            "%(asctime)s %(name)s %(levelname)s %(message)s"))
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(logging.INFO)


def doctor() -> list[tuple[str, bool, str]]:
    """Health checks for `scout doctor`: (check, ok, detail)."""
    checks: list[tuple[str, bool, str]] = []

    try:
        with get_engine().connect() as conn:
            conn.exec_driver_sql("SELECT 1")
        checks.append(("database", True, str(get_engine().url).split("@")[-1]))
    except Exception as exc:  # noqa: BLE001
        checks.append(("database", False, f"{type(exc).__name__}: {exc}"))
        return checks  # everything else needs the DB

    key = anthropic_api_key()
    if not key:
        checks.append(("anthropic_api_key", False, "ANTHROPIC_API_KEY not set — triage/extract will fail"))
    else:
        # count_tokens, not models.list. Listing models authenticates the key and
        # nothing else: on 2026-08-06 it returned 200 while every triage and
        # extract call was failing with "credit balance is too low", so doctor
        # reported the LLM healthy against an account that could not run a single
        # inference. Same failure class as a source reporting OK while producing
        # nothing. count_tokens exercises the billing check on the real path and
        # is not itself billed, so it is a free probe that actually fails when
        # the pipeline would fail.
        try:
            import anthropic
            anthropic.Anthropic(api_key=key).messages.count_tokens(
                model=load_config().get("llm.triage_model"),
                messages=[{"role": "user", "content": "ping"}])
            checks.append(("anthropic_api_key", True, "key valid, account can run inference"))
        except Exception as exc:  # noqa: BLE001
            checks.append(("anthropic_api_key", False, f"unusable: {exc}"))

    total, used, free = shutil.disk_usage("/")
    free_gb = free / 1e9
    checks.append(("disk", free_gb > 1.0, f"{free_gb:.1f} GB free"))

    cfg = load_config()
    now = utcnow()

    with session_scope() as session:
        # One pass over the table, grouped by the source a run name belongs to, so
        # a backfill run counts as a run of its source. Matching `source == name`
        # here meant a backfill was invisible: every source whose only runs were
        # backfills read as "no successful run recorded" while its successful runs
        # sat in the same table under "<name>:backfill". See app/models.py.
        all_runs = session.exec(select(SourceRun)).all()
        by_source: dict[str, list[SourceRun]] = {}
        for run in all_runs:
            by_source.setdefault(run_name_source(run.source), []).append(run)

        for name in [n for n in cfg.data.get("sources", {}) if cfg.source_enabled(n)]:
            if name == "manual":
                continue
            runs = by_source.get(name, [])
            oks = sorted((r for r in runs if r.ok), key=lambda r: r.started_at)
            if oks:
                last_ok = oks[-1]
                mode = run_name_mode(last_ok.source) or "fetch"
                checks.append((f"source:{name}", last_ok.started_at >= stale_cutoff(cfg, name, now),
                               f"last success {last_ok.started_at:%Y-%m-%d %H:%M}Z ({mode})"))
            elif runs:
                # Ran and failed is a different diagnosis from never ran, and the
                # old message could not tell them apart. One is a broken adapter,
                # the other is a scheduler that never fired.
                last = max(runs, key=lambda r: r.started_at)
                checks.append((f"source:{name}", False,
                               f"{len(runs)} run(s) recorded, none successful; last "
                               f"{last.started_at:%Y-%m-%d %H:%M}Z: {(last.error or 'no error recorded')[:80]}"))
            else:
                checks.append((f"source:{name}", False, "no run of any kind recorded"))

        # THE CHECK THAT WOULD HAVE CAUGHT THE ABOVE. Every `source:` check reads
        # source_runs through a naming convention, so an unrecognized name is a
        # blind spot: its runs exist, they are healthy or not, and no check can
        # see either. That is the same failure class as a source reporting OK
        # while producing nothing — it ends in a green board over a broken
        # machine — so it fails here rather than degrading some other check into
        # a false alarm nobody can explain.
        known = set(cfg.data.get("sources", {}))
        unattributable = sorted({
            r.source for r in all_runs
            if r.source not in STAGE_RUN_NAMES and run_name_source(r.source) not in known
        })
        checks.append(("source_run_names", not unattributable,
                       "every source_runs name maps to a source or a stage" if not unattributable
                       else f"{len(unattributable)} run name(s) no check can see: "
                            f"{', '.join(unattributable[:5])} — add the source to config.yaml, "
                            f"or the stage to STAGE_RUN_NAMES, or build the name with "
                            f"source_run_name()"))

    # Invariant 2: no project carries a score with zero linked signals. A scored
    # row with nothing behind it renders on the board like any other — a name, a
    # tonnage, a window, a number a rep would act on — and there is no source
    # document under any of it. Invariant 11 says every number on a deliverable
    # traces to a public URL; this is the check that the trace exists at all.
    #
    # Merged rows are excluded and that is not a loophole. Merging moves the
    # signals to the surviving project by design, so the merged row is signal-less
    # for exactly the right reason, and it is off the board anyway (ACTIVE_STATUSES
    # excludes it). Projects 710 and 963 are the two that exist today.
    with session_scope() as session:
        orphans = session.exec(
            select(Project).where(
                Project.status.in_(ACTIVE_STATUSES),
                ~Project.id.in_(select(ProjectSignal.project_id)))).all()
        # Rendered inside the session: these are ORM instances, and reading them
        # after the scope closes raises DetachedInstanceError.
        named = [f"#{p.id} {p.name[:30]} (score {p.score:.2f})" for p in orphans[:5]]
        n_orphans = len(orphans)
    checks.append(("project_evidence", not n_orphans,
                   "every scored project has at least one linked signal" if not n_orphans else
                   f"{n_orphans} scored project(s) with NO linked signal — "
                   + ", ".join(named)
                   + (f" and {n_orphans - 5} more" if n_orphans > 5 else "")))

    # A fragmenting board is a silent failure: nothing errors, the row count just
    # grows and every rate computed over it is wrong. See app/duplicates.py.
    from app.duplicates import find_duplicates
    with session_scope() as session:
        dup = find_duplicates(session)
    checks.append(("project_duplicates", dup["n_groups"] == 0,
                   "no duplicate project rows" if dup["n_groups"] == 0 else
                   f"{dup['n_groups']} suspect groups, {dup['n_excess_rows']} excess rows "
                   f"of {dup['n_projects']} — run `scout duplicates`"))

    # A state value that is not a 2-letter USPS code is invisible to every
    # state-keyed lookup in this codebase — territory, county adjacency, the
    # Nevada/California day-to-bid split — not an error anywhere, just a row
    # that silently never matches. "Nevada" instead of "NV" on a real project
    # is what caught this; normalize_state() fixes it at write time, this
    # catches whatever gets in anyway (a future writer that forgets to call
    # it, a row from before the fix).
    from app.normalize import VALID_STATE_CODES
    with session_scope() as session:
        bad_project_states = session.exec(
            select(Project.state).where(Project.state.is_not(None)).distinct()).all()
        bad_signal_states = session.exec(
            select(Signal.state).where(Signal.state.is_not(None)).distinct()).all()
    bad = sorted({s for s in (*bad_project_states, *bad_signal_states) if s not in VALID_STATE_CODES})
    checks.append(("state_values", not bad,
                   "every state value is a 2-letter USPS code" if not bad else
                   f"{len(bad)} non-canonical state value(s) in use: {', '.join(bad)} — "
                   f"run normalize_state() over existing rows"))

    # Meaningful only on the cron service: HEALTHCHECK_URL is deliberately
    # NOT provisioned on the web service (see render.yaml -- the cron
    # service's envVars block is the only one that lists it), so running
    # doctor() from the web service's own MCP tool (app.mcp_tools.
    # source_health) always saw it unset and reported a FAIL that looked
    # like an incident but was really just asking the wrong service the
    # question. RENDER_SERVICE_TYPE is a Render-injected env var (unset
    # outside Render, e.g. local dev/tests, where the old cron-relevant
    # behavior below is exactly what's wanted since the ambiguity favors a
    # real signal over a silently-skipped one).
    if os.environ.get("RENDER_SERVICE_TYPE") == "web":
        checks.append(("dead_mans_switch", True,
                       "not applicable on the web service — only the cron service pings this"))
    else:
        armed = bool(os.environ.get(HEALTHCHECK_ENV))
        checks.append(("dead_mans_switch", armed,
                       "HEALTHCHECK_URL set" if armed else "HEALTHCHECK_URL not set — cron death would be silent"))

    from app.spend import budget_status
    st = budget_status()
    checks.append(("llm_budget", not st["exhausted"],
                   f"today ${st['today_usd']:.2f} / ${st['daily_budget_usd']:.2f}, month ${st['month_usd']:.2f}"))
    return checks


def fix_state_values(session) -> dict:
    """One-time (and safe to re-run) cleanup for rows written before
    normalize_state() existed at the write sites — see the state_values
    doctor check above. Returns what changed, per table, so a run against
    production is auditable rather than a silent UPDATE."""
    from app.normalize import normalize_state

    changed: dict[str, list[str]] = {"projects": [], "signals": []}
    for model, key in ((Project, "projects"), (Signal, "signals")):
        rows = session.exec(select(model).where(model.state.is_not(None))).all()
        for row in rows:
            fixed = normalize_state(row.state)
            if fixed != row.state:
                changed[key].append(f"#{row.id}: {row.state!r} -> {fixed!r}")
                row.state = fixed
                session.add(row)
    session.commit()
    return changed
