"""Pipeline staleness: is `scout pipeline` actually still running, and does
anyone find out if it stops. Separate from app.ops's healthchecks.io dead
man's switch (an external ping) -- this is in-app, DB-backed, and answers a
slightly different question: not "did the cron process execute" but "when
did data last actually land", which also correctly catches a run that
executed and completed but errored on every stage.

Every write here is best-effort, same discipline as app.access_log and
app.ops.ping_healthcheck: a failure to log a pipeline_run row, or to send
the alert email, must never be the reason a page failed to load or a real
pipeline run raised past its own error handling.
"""
from __future__ import annotations

import logging
import os
from contextvars import ContextVar
from datetime import timedelta

import httpx
from sqlmodel import func, or_, select

from app.config import Config, load_config
from app.models import PipelineRun, RawDocument, StalenessAlert, utcnow

log = logging.getLogger(__name__)

STALE_THRESHOLD_HOURS = 36
ALERT_COOLDOWN_HOURS = 24
STALENESS_NOTIFY_TO = "acrane988@gmail.com"

# A row stuck at status="running" with no heartbeat for this long is not
# still running -- reap_stale_runs() reclassifies it as "failed". Sized
# against real observed timing this session, not guessed: every individual
# fetch source and every other stage has completed in well under this
# window in every run observed, including the one that ran for 6.5 minutes
# before Render's OOM killer took it mid-fetch. Generous enough to avoid
# flagging a genuinely slow-but-alive stage, tight enough to catch a dead
# one long before the next scheduled run (24h) would.
HEARTBEAT_STALE_MINUTES = 15

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
                        error: str | None) -> None:
    """status must be "success" or "failed" -- never "running" again. A run
    this never gets called for (process crash) stays "running" forever,
    which last_successful_run() correctly never counts as a success."""
    run = session.get(PipelineRun, run_id)
    if run is None:
        log.warning("finish_pipeline_run: no pipeline_run row #%s to update", run_id)
        return
    run.finished_at = utcnow()
    run.status = status
    run.records_processed = records_processed
    run.error = error
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


def hours_stale(session) -> float | None:
    """Hours since the last successful run finished. None means no
    successful run has EVER been recorded -- maximally stale, but a distinct
    case from "was fresh, went stale", so callers message it separately."""
    run = last_successful_run(session)
    if run is None or run.finished_at is None:
        return None
    return (utcnow() - run.finished_at).total_seconds() / 3600.0


def _last_alert_within(session, cooldown_hours: float) -> bool:
    since = utcnow() - timedelta(hours=cooldown_hours)
    return session.exec(
        select(StalenessAlert.id).where(StalenessAlert.sent_at >= since).limit(1)
    ).first() is not None


def send_staleness_alert(cfg: Config, hours: float | None, last_success_at) -> bool:
    """Best-effort -- see module docstring. Returns whether it actually sent."""
    api_key = os.environ.get(cfg.get("digest.resend.api_key_env", "RESEND_API_KEY"), "")
    if not api_key:
        log.warning("pipeline data is stale but RESEND_API_KEY is empty -- alert not sent")
        return False
    if hours is None:
        detail = "No successful pipeline run has ever been recorded."
    else:
        detail = (f"{hours:.0f} hours since the last successful run "
                 f"(finished {last_success_at:%Y-%m-%d %H:%M} UTC).")
    try:
        resp = httpx.post(
            "https://api.resend.com/emails",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "from": cfg.get("digest.from_addr"),
                "to": [STALENESS_NOTIFY_TO],
                "subject": "[DMG Scout] Pipeline data is stale",
                "text": f"{detail} Threshold is {STALE_THRESHOLD_HOURS} hours. Check `scout doctor` / "
                        f"`scout check-freshness` and the Render cron's own logs.",
            },
            timeout=10,
        )
        resp.raise_for_status()
        return True
    except httpx.HTTPError as exc:
        log.warning("failed to send pipeline staleness alert: %s", exc)
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
    still in progress."""
    reap_stale_runs(session)
    run = last_successful_run(session)
    stale_hours = hours_stale(session)
    is_stale = stale_hours is None or stale_hours > threshold_hours

    alert_sent = False
    if is_stale:
        log.warning("pipeline data is stale: %s",
                    "no successful run on record" if stale_hours is None else f"{stale_hours:.0f}h since last success")
        try:
            if not _last_alert_within(session, cooldown_hours):
                last_success_at = run.finished_at if run else None
                if send_staleness_alert(cfg, stale_hours, last_success_at):
                    session.add(StalenessAlert())
                    session.commit()
                    alert_sent = True
        except Exception:  # noqa: BLE001 — the staleness CHECK must never break the page it's on
            log.exception("staleness alert bookkeeping failed")

    return {
        "stale": is_stale, "hours_stale": stale_hours,
        "last_success_at": run.finished_at if run else None, "alert_sent": alert_sent,
    }
