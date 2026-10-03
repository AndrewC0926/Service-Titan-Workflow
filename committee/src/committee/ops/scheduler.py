"""Operating schedule (DESIGN 13, Prompt 16). Each job runs a `committee` CLI
command in a subprocess so a crash in one job never takes down the scheduler.
Failures are journaled as incidents and alerted.

All times are America/New_York.
"""

from __future__ import annotations

import datetime as dt
import shlex
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass

from committee.journal.store import Journal
from committee.ops.alerts import Alerter


@dataclass(frozen=True)
class Job:
    id: str
    cron: str  # minute hour day month day_of_week (APScheduler crontab order)
    command: str
    level_on_failure: str = "P2"
    description: str = ""


JOBS: tuple[Job, ...] = (
    Job(
        "ingest_nightly",
        "0 19 * * mon-fri",
        "ingest nightly",
        "P2",
        "EDGAR, prices, macro, news for the universe and holdings",
    ),
    Job(
        "orders_reconcile", "30 20 * * mon-fri", "orders reconcile", "P1", "reconcile fills to lots"
    ),
    Job("dq_check", "0 21 * * mon-fri", "data check", "P2", "data-quality suite"),
    Job(
        "journal_verify",
        "0 23 * * *",
        "journal verify",
        "P1",
        "verify the hash chain (freezes orders on failure)",
    ),
    Job("journal_anchor", "5 23 * * *", "journal anchor", "P2", "write and email the daily anchor"),
    Job("backup_nightly", "30 23 * * *", "ops backup", "P2", "encrypted backup of var/"),
    Job("digest_daily", "30 7 * * *", "digest send", "P3", "morning digest email"),
    Job(
        "expire_briefings",
        "0 6 * * *",
        "briefings expire",
        "P3",
        "expire briefings older than 7 days",
    ),
    Job("screen_weekly", "0 18 * * sun", "screen --asof today", "P2", "weekly screen"),
    Job("reviews_sunday", "0 21 * * sun", "review batch", "P2", "3-6 committee reviews"),
    Job(
        "reviews_wednesday",
        "0 21 * * wed",
        "review batch --rereview-only",
        "P2",
        "re-review triggers",
    ),
    Job(
        "monitor_triggers",
        "0 20 * * mon-fri",
        "review triggers",
        "P2",
        "falsifiers, kill criteria, 8-K items, -20% drops",
    ),
    Job("harvest_monthly", "0 9 1 * *", "tax harvest-scan", "P3", "monthly tax-loss harvest scan"),
    Job(
        "eval_monthly",
        "0 10 1 * *",
        "eval monthly",
        "P3",
        "factor regression, behavioral and cost reports",
    ),
    Job("recall_probe_weekly", "0 12 * * sat", "agents recall-probe", "P3", "LLM leakage probe"),
    Job(
        "eval_quarterly",
        "0 10 1 1,4,7,10 *",
        "eval quarterly",
        "P3",
        "allocator, agent reweighting, scenario refresh",
    ),
    Job(
        "restore_test_quarterly",
        "0 11 1 1,4,7,10 *",
        "ops restore-test",
        "P2",
        "quarterly restore drill",
    ),
)


@dataclass(frozen=True)
class JobResult:
    job_id: str
    ok: bool
    returncode: int
    seconds: float
    tail: str


Runner = Callable[[list[str]], tuple[int, str]]


def subprocess_runner(args: list[str]) -> tuple[int, str]:
    p = subprocess.run(  # noqa: S603 - fixed argv, no shell
        [sys.executable, "-m", "committee.cli", *args],
        capture_output=True,
        text=True,
        timeout=3 * 3600,
    )
    return p.returncode, (p.stdout + p.stderr)[-2000:]


def run_job(
    job: Job, journal: Journal, alerter: Alerter, runner: Runner = subprocess_runner
) -> JobResult:
    start = dt.datetime.now(dt.UTC)
    try:
        code, out = runner(shlex.split(job.command))
    except Exception as e:
        code, out = 99, f"{type(e).__name__}: {e}"
    secs = (dt.datetime.now(dt.UTC) - start).total_seconds()
    res = JobResult(job.id, code == 0, code, secs, out[-500:])
    if not res.ok:
        journal.append(
            "incident",
            {
                "level": job.level_on_failure,
                "kind": "job_failed",
                "job": job.id,
                "returncode": code,
                "tail": res.tail,
            },
        )
        alerter.alert(job.level_on_failure, f"job {job.id} failed (exit {code})", res.tail)
    return res


def build_scheduler(
    journal_factory: Callable[[], Journal], alerter: Alerter, runner: Runner = subprocess_runner
) -> object:
    """APScheduler BlockingScheduler with every job registered (not started)."""
    from apscheduler.schedulers.blocking import BlockingScheduler
    from apscheduler.triggers.cron import CronTrigger

    sched = BlockingScheduler(timezone="America/New_York")

    def make(job: Job) -> Callable[[], None]:
        def fire() -> None:
            with journal_factory() as j:
                run_job(job, j, alerter, runner)

        return fire

    for job in JOBS:
        sched.add_job(
            make(job),
            CronTrigger.from_crontab(job.cron, timezone="America/New_York"),
            id=job.id,
            coalesce=True,
            max_instances=1,
            misfire_grace_time=3600,
        )
    return sched
