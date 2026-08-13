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
from datetime import timedelta

import httpx
from sqlmodel import func, select

from app.config import Config, load_config
from app.models import PipelineRun, RawDocument, StalenessAlert, utcnow

log = logging.getLogger(__name__)

STALE_THRESHOLD_HOURS = 36
ALERT_COOLDOWN_HOURS = 24
STALENESS_NOTIFY_TO = "acrane988@gmail.com"


def start_pipeline_run(session) -> PipelineRun:
    run = PipelineRun(status="running")
    session.add(run)
    session.commit()
    session.refresh(run)
    return run


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
    uses for its once-per-username notification."""
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
