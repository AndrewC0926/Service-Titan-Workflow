"""Pipeline staleness: is `scout pipeline` actually still running, and does
anyone find out if it stops. Separate from app.ops's healthchecks.io dead
man's switch (an external ping) -- this is in-app, DB-backed, and answers a
slightly different question: not "did the cron process execute" but "when
did data last actually land", which also correctly catches a run that
executed and completed but errored on every stage.

Also covers RetrofitBuilding freshness per population (see
RETROFIT_STALE_THRESHOLD_HOURS), not just `scout pipeline`'s own success --
a pipeline that fetches/triages/extracts/resolves/scores/notifies flawlessly
every night still says nothing about whether build-retrofit-buildings or
find-replacement-candidates are actually landing fresh rows, and this table
being derived (DELETE + reinsert every rebuild) means a broken or removed
rebuild step leaves no error in the data itself to notice later -- only its
absence.

Every write here is best-effort, same discipline as app.access_log and
app.ops.ping_healthcheck: a failure to log a pipeline_run row, or to send
the alert email, must never be the reason a page failed to load or a real
pipeline run raised past its own error handling.
"""
from __future__ import annotations

import logging
import os
import resource
import sys
from contextvars import ContextVar
from datetime import timedelta

import httpx
from sqlmodel import func, or_, select

from app.config import Config, load_config
from app.models import PipelineRun, PipelineStageRun, RawDocument, RetrofitBuilding, StalenessAlert, utcnow

log = logging.getLogger(__name__)

STALE_THRESHOLD_HOURS = 36
ALERT_COOLDOWN_HOURS = 24
STALENESS_NOTIFY_TO = "acrane988@gmail.com"

# How stale RetrofitBuilding is allowed to get before it counts as its own
# staleness-alarm reason, per population -- see app.cli:pipeline for the
# rebuild cadence each of these matches (recently_active daily,
# replacement_candidate weekly via RETROFIT_WEEKLY_WEEKDAY). Each threshold
# is one missed cycle of slack past its cadence, same reasoning
# STALE_THRESHOLD_HOURS applies to the daily pipeline run: 36h tolerates one
# missed day; 216h (9 days) tolerates one missed week plus two days before
# paging. Before this existed, a `scout pipeline` that ran fetch through
# notify successfully every single night still reported "fresh" while
# retrofit-buildings silently stopped updating -- this closes exactly that
# gap; a stuck/broken retrofit step is no longer invisible to the same
# alarm that already watches everything else.
RETROFIT_STALE_THRESHOLD_HOURS = {
    "recently_active": 36,
    "replacement_candidate": 24 * 9,
}

# A row stuck at status="running" with no heartbeat for this long is not
# still running -- reap_stale_runs() reclassifies it as "failed". Sized
# against real observed timing this session, not guessed: every individual
# fetch source and every other stage has completed in well under this
# window in every run observed, including the one that ran for 6.5 minutes
# before Render's OOM killer took it mid-fetch. Generous enough to avoid
# flagging a genuinely slow-but-alive stage, tight enough to catch a dead
# one long before the next scheduled run (24h) would.
HEARTBEAT_STALE_MINUTES = 15

# The cron container's own memory ceiling -- must match render.yaml's cron
# `plan: standard` (2Gi); bump together if the plan ever changes. Render
# exposes no instance metrics API for one-off Job/cron runs (confirmed
# 2026-08-15, checked against /v1/metrics/memory both for the job id and the
# service id, both empty, while the same call against the always-on web
# service returns real series) -- ru_maxrss logged by the pipeline itself,
# compared against this, is the only way to see memory pressure on a
# scheduled run before it OOMs rather than after.
CRON_MEMORY_LIMIT_BYTES = 2048 * 1024 * 1024  # 2Gi

# Same reasoning as budget_status()'s llm.budget_warn_fraction (app/spend.py):
# hear about pressure well before the kill, not exactly at it.
MEMORY_WARN_FRACTION = 0.75

# Same ContextVar pattern app.spend uses for "the currently active run" --
# lets fetch.py's per-source loop (and any other stage) call heartbeat()
# without a run id threaded through every intervening function signature.
# None (the default) when no `scout pipeline` run is active, e.g. `scout
# fetch` invoked standalone -- heartbeat() is then correctly a no-op.
_active_pipeline_run_id: ContextVar[int | None] = ContextVar("_active_pipeline_run_id", default=None)


def start_pipeline_run(session) -> PipelineRun:
    now = utcnow()
    run = PipelineRun(status="running", started_at=now, heartbeat_at=now)
    session.add(run)
    session.commit()
    session.refresh(run)
    _active_pipeline_run_id.set(run.id)
    return run


def heartbeat(session) -> None:
    """Touch the active run's heartbeat_at. No-op if no `scout pipeline` run
    is active -- see _active_pipeline_run_id. Called once per pipeline
    stage (app.cli:pipeline) and, within fetch specifically, once per
    source (app.pipeline.fetch:run_fetch) -- fetch is the one stage
    observed running long enough that a stage-boundary-only heartbeat could
    plausibly go stale while genuinely still alive; every other stage has
    always completed well inside HEARTBEAT_STALE_MINUTES in every run
    observed this session."""
    run_id = _active_pipeline_run_id.get()
    if run_id is None:
        return
    run = session.get(PipelineRun, run_id)
    if run is None:
        return
    run.heartbeat_at = utcnow()
    session.add(run)
    session.commit()


def peak_rss_bytes() -> int:
    """Process's high-water-mark resident set size so far, in bytes.
    ru_maxrss is KiB on Linux (every environment this actually runs in --
    Render's docker images) but bytes on macOS; this is the whole reason
    for a shared helper instead of calling getrusage() at each call site."""
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v if sys.platform == "darwin" else v * 1024


def record_stage_peak_memory(session, stage: str) -> None:
    """Best-effort, same discipline as heartbeat() and the rest of this
    module -- called from the same place a stage's success/failure is
    already recorded (app.cli:pipeline's stage loop), so a future OOM has a
    row for every stage that finished before it, not just the run's own
    (never-written, in that case) peak_rss_bytes. No-op if no `scout
    pipeline` run is active -- see _active_pipeline_run_id."""
    run_id = _active_pipeline_run_id.get()
    if run_id is None:
        return
    try:
        session.add(PipelineStageRun(pipeline_run_id=run_id, stage=stage, peak_rss_bytes=peak_rss_bytes()))
        session.commit()
    except Exception:  # noqa: BLE001 — a memory-logging failure must never break the pipeline itself
        log.exception("record_stage_peak_memory failed for run #%s stage %r", run_id, stage)


def reap_stale_runs(session) -> int:
    """Reclassify any row stuck at status="running" as "failed" rather than
    trust it's still alive. Two ways a row gets here: (1) heartbeat_at was
    set but hasn't moved in HEARTBEAT_STALE_MINUTES -- the process behind it
    is gone (external kill, e.g. Render's OOM killer -- confirmed real,
    2026-08-13). (2) heartbeat_at is NULL and started_at is stale -- rows
    created before this column existed (ids 3, 4, 5, all OOM/composition-bug
    casualties from before the fix), which never got a heartbeat at all.
    Called before a new run starts (app.cli:pipeline) and as part of every
    staleness check (check_and_alert_staleness) -- so a dead run can't keep
    last_successful_run()/hours_stale() answering as if the last real
    attempt is still in flight, and can't block a fresh run from starting."""
    threshold = utcnow() - timedelta(minutes=HEARTBEAT_STALE_MINUTES)
    stuck = session.exec(
        select(PipelineRun).where(
            PipelineRun.status == "running",
            or_(
                PipelineRun.heartbeat_at < threshold,
                PipelineRun.heartbeat_at.is_(None) & (PipelineRun.started_at < threshold),
            ),
        )
    ).all()
    for run in stuck:
        run.status = "failed"
        run.finished_at = utcnow()
        run.error = (
            f"reaped by reap_stale_runs: no heartbeat for over {HEARTBEAT_STALE_MINUTES}m "
            f"(last heartbeat {run.heartbeat_at}) -- presumed killed externally, e.g. OOM"
        )
        session.add(run)
    if stuck:
        session.commit()
    return len(stuck)


def finish_pipeline_run(session, run_id: int, *, status: str, records_processed: int | None,
                        error: str | None, peak_rss_bytes: int | None = None) -> None:
    """status must be "success" or "failed" -- never "running" again. A run
    this never gets called for (process crash) stays "running" forever,
    which last_successful_run() correctly never counts as a success -- and
    peak_rss_bytes stays None, same caveat; see PipelineStageRun for what
    still has something to say about that run's memory."""
    run = session.get(PipelineRun, run_id)
    if run is None:
        log.warning("finish_pipeline_run: no pipeline_run row #%s to update", run_id)
        return
    run.finished_at = utcnow()
    run.status = status
    run.records_processed = records_processed
    run.error = error
    run.peak_rss_bytes = peak_rss_bytes
    session.add(run)
    session.commit()


def records_processed_since(session, since) -> int:
    """How many raw documents this run actually pulled in -- the honest,
    simple definition: a single number spanning fetch/triage/extract's very
    different units of work would either need per-stage instrumentation this
    pass doesn't add, or be arbitrary. Documents fetched is what a pipeline
    run concretely produced."""
    return session.exec(
        select(func.count(RawDocument.id)).where(RawDocument.fetched_at >= since)
    ).one()


def last_successful_run(session) -> PipelineRun | None:
    return session.exec(
        select(PipelineRun).where(PipelineRun.status == "success")
        .order_by(PipelineRun.finished_at.desc()).limit(1)
    ).first()


def latest_pipeline_run(session) -> PipelineRun | None:
    """Most recent run regardless of status -- unlike last_successful_run(),
    deliberately includes "running" (still going) and "failed" (including
    OOM-reaped) rows, because memory_pressure_status() needs to see the run
    that's actually next to fail, not the last one that didn't."""
    return session.exec(select(PipelineRun).order_by(PipelineRun.id.desc()).limit(1)).first()


def stage_peak_memory(session, run_id: int) -> dict[str, int]:
    """{stage: peak_rss_bytes} for one run, in the order stages were
    recorded -- see PipelineStageRun docstring for why the last entry is
    the informative one when a run never finished."""
    rows = session.exec(
        select(PipelineStageRun).where(PipelineStageRun.pipeline_run_id == run_id)
        .order_by(PipelineStageRun.id)
    ).all()
    return {r.stage: r.peak_rss_bytes for r in rows}


def memory_pressure_status(session) -> dict:
    """Peak memory observed on the most recent `scout pipeline` run, against
    CRON_MEMORY_LIMIT_BYTES -- folded into check_and_alert_staleness so the
    same alert path (and its rate limit) that already emails about staleness
    also emails about memory pressure, before an OOM rather than after.
    Takes the max of the run's own peak_rss_bytes (unset if the process was
    killed before finish_pipeline_run) and every PipelineStageRun logged for
    it, so a run that died mid-stage still reports whatever the last
    completed stage saw.

    Returns run_id, not the PipelineRun object itself: SQLAlchemy expires
    loaded attributes on commit by default, and check_and_alert_staleness
    (the only caller) may commit again (reap_stale_runs, the StalenessAlert
    insert) after this returns -- a caller reading this dict once its own
    session_scope() has exited (scout check-freshness does exactly that)
    would hit DetachedInstanceError on any attribute of a live ORM object."""
    run = latest_pipeline_run(session)
    if run is None:
        return {"run_id": None, "peak_bytes": None, "limit_bytes": CRON_MEMORY_LIMIT_BYTES,
                "fraction": None, "warn": False}
    candidates = [v for v in (run.peak_rss_bytes, *stage_peak_memory(session, run.id).values()) if v is not None]
    peak = max(candidates) if candidates else None
    fraction = peak / CRON_MEMORY_LIMIT_BYTES if peak is not None else None
    return {
        "run_id": run.id, "peak_bytes": peak, "limit_bytes": CRON_MEMORY_LIMIT_BYTES,
        "fraction": fraction, "warn": fraction is not None and fraction >= MEMORY_WARN_FRACTION,
    }


def hours_stale(session) -> float | None:
    """Hours since the last successful run finished. None means no
    successful run has EVER been recorded -- maximally stale, but a distinct
    case from "was fresh, went stale", so callers message it separately."""
    run = last_successful_run(session)
    if run is None or run.finished_at is None:
        return None
    return (utcnow() - run.finished_at).total_seconds() / 3600.0


def retrofit_population_hours_stale(session, population: str) -> float | None:
    """Hours since RetrofitBuilding rows for this population were last
    rebuilt (max(built_at)) -- the same question hours_stale() answers for
    `scout pipeline` itself, asked instead of app.pipeline.retrofit's own
    rebuild (build_retrofit_buildings / find_replacement_candidates DELETE
    and reinsert their whole population every run, so the newest built_at
    IS the last rebuild time, not an average of old and new rows). None
    means this population has never been built at all -- maximally stale,
    same convention as hours_stale()."""
    built_at = session.exec(
        select(func.max(RetrofitBuilding.built_at)).where(RetrofitBuilding.population == population)
    ).one()
    if built_at is None:
        return None
    return (utcnow() - built_at).total_seconds() / 3600.0


def _last_alert_within(session, cooldown_hours: float) -> bool:
    since = utcnow() - timedelta(hours=cooldown_hours)
    return session.exec(
        select(StalenessAlert.id).where(StalenessAlert.sent_at >= since).limit(1)
    ).first() is not None


def send_staleness_alert(cfg: Config, reasons: list[str], *, subject: str = "[DMG Scout] Pipeline data is stale") -> bool:
    """Best-effort -- see module docstring. Returns whether it actually sent.
    `reasons` is one sentence per tripped axis (the pipeline run itself,
    any RetrofitBuilding population past its own cadence, and/or memory
    pressure on the last run) -- see check_and_alert_staleness, the only
    caller, for how it's built and how `subject` is chosen. A single email
    covering every tripped axis, not one per axis, so a night where several
    go at once still only sends (and rate-limits) as one alert."""
    api_key = os.environ.get(cfg.get("digest.resend.api_key_env", "RESEND_API_KEY"), "")
    if not api_key:
        log.warning("alert condition tripped but RESEND_API_KEY is empty -- alert not sent")
        return False
    detail = " ".join(reasons)
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": cfg.get("digest.from_addr"),
                "to": [STALENESS_NOTIFY_TO],
                "subject": subject,
                "text": f"{detail} Check `scout doctor` / `scout check-freshness` and the Render "
                        f"cron's own logs.",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("failed to send staleness alert: %s", exc)
        return False


def check_and_alert_staleness(session, cfg: Config, *, threshold_hours: float = STALE_THRESHOLD_HOURS,
                              cooldown_hours: float = ALERT_COOLDOWN_HOURS) -> dict:
    """Shared by the root dashboard view and `scout check-freshness` -- one
    function, so the two can't quietly disagree about what "stale" means.
    Rate-limited to at most one email per cooldown_hours: a StalenessAlert
    row must not exist within the window, checked BEFORE this run's own
    alert (if any) is inserted -- the same before-insert gate app.access_log
    uses for its once-per-username notification. Reaps stale "running" rows
    first so a dead run (external kill, no heartbeat) can't masquerade as
    still in progress.

    Checks three independent axes, any of which alone trips the alarm: (1)
    has `scout pipeline` itself succeeded recently (stale_hours, unchanged
    behavior/keys from before retrofit coverage existed), (2) has EVERY
    RetrofitBuilding population been rebuilt within its own cadence (see
    RETROFIT_STALE_THRESHOLD_HOURS) -- returned under the `retrofit` key,
    keyed by population, and (3) did the most recent `scout pipeline` run's
    peak memory cross MEMORY_WARN_FRACTION of CRON_MEMORY_LIMIT_BYTES --
    returned under the `memory` key, see memory_pressure_status(). Before
    the second axis existed, a pipeline that ran fetch through notify
    successfully every night still reported fresh even if
    build-retrofit-buildings/find-replacement-candidates had been silently
    broken or removed from the schedule for weeks; before the third, this
    alert path had nothing to say about a run that succeeded but crept
    towards the same OOM ceiling that has already killed one run for
    real (2026-08-13) -- it only spoke up after the kill, via axis (1) on
    the next check, never before."""
    reap_stale_runs(session)
    run = last_successful_run(session)
    stale_hours = hours_stale(session)
    pipeline_is_stale = stale_hours is None or stale_hours > threshold_hours

    retrofit: dict[str, dict] = {}
    for population, pop_threshold in RETROFIT_STALE_THRESHOLD_HOURS.items():
        hrs = retrofit_population_hours_stale(session, population)
        retrofit[population] = {
            "hours_stale": hrs,
            "threshold_hours": pop_threshold,
            "stale": hrs is None or hrs > pop_threshold,
        }
    mem = memory_pressure_status(session)
    is_stale = pipeline_is_stale or any(v["stale"] for v in retrofit.values()) or mem["warn"]

    alert_sent = False
    if is_stale:
        reasons = []
        if pipeline_is_stale:
            reasons.append(
                "No successful pipeline run has ever been recorded." if stale_hours is None
                else f"{stale_hours:.0f} hours since the last successful pipeline run "
                    f"(threshold {threshold_hours:.0f}h).")
        for population, info in retrofit.items():
            if not info["stale"]:
                continue
            reasons.append(
                f"retrofit_buildings population {population!r} has never been built." if info["hours_stale"] is None
                else f"retrofit_buildings population {population!r} last rebuilt {info['hours_stale']:.0f} hours "
                    f"ago (threshold {info['threshold_hours']:.0f}h).")
        if mem["warn"]:
            reasons.append(
                f"pipeline run #{mem['run_id']} peak memory {mem['peak_bytes'] / 2**20:.0f} MB is "
                f"{mem['fraction'] * 100:.0f}% of the {mem['limit_bytes'] / 2**20:.0f} MB instance limit "
                f"(warn threshold {MEMORY_WARN_FRACTION * 100:.0f}%) -- next run may OOM.")
        log.warning("staleness alarm tripped: %s", " ".join(reasons))
        subject = ("[DMG Scout] Pipeline memory pressure warning"
                   if mem["warn"] and not pipeline_is_stale and not any(v["stale"] for v in retrofit.values())
                   else "[DMG Scout] Pipeline data is stale")
        try:
            if not _last_alert_within(session, cooldown_hours):
                if send_staleness_alert(cfg, reasons, subject=subject):
                    session.add(StalenessAlert())
                    session.commit()
                    alert_sent = True
        except Exception:  # noqa: BLE001 — the staleness CHECK must never break the page it's on
            log.exception("staleness alert bookkeeping failed")

    return {
        "stale": is_stale, "hours_stale": stale_hours, "threshold_hours": threshold_hours,
        "last_success_at": run.finished_at if run else None, "alert_sent": alert_sent,
        "retrofit": retrofit, "memory": mem,
    }
