"""scout CLI — every pipeline stage independently runnable and idempotent."""
from __future__ import annotations

import json
import logging

import typer

from app.config import load_config
from app.db import init_db, session_scope
from app.ops import setup_logging

setup_logging()
logging.getLogger(__name__)

app = typer.Typer(help="DMG Scout: early-signal data center project intelligence.")


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
    for name, run in runs.items():
        status = "ok" if run.ok else f"FAILED: {(run.error or '').splitlines()[0]}"
        typer.echo(f"{name}: fetched={run.records_fetched} new={run.records_new} {status}")


@app.command()
def triage(limit: int = 200) -> None:
    """TRIAGE: Haiku relevance pass over pending documents."""
    from app.pipeline.triage import run_triage
    cfg = load_config()
    with session_scope() as session:
        stats = run_triage(session, cfg, limit=limit)
    typer.echo(json.dumps(stats))


@app.command()
def extract(limit: int = 100) -> None:
    """EXTRACT: Sonnet structured extraction over relevant documents."""
    from app.pipeline.extract import run_extract
    cfg = load_config()
    with session_scope() as session:
        stats = run_extract(session, cfg, limit=limit)
    typer.echo(json.dumps(stats))


@app.command()
def resolve(no_llm: bool = typer.Option(False, help="Skip LLM adjudication")) -> None:
    """RESOLVE: match signals to canonical projects."""
    from app.pipeline.resolve import run_resolve
    cfg = load_config()
    with session_scope() as session:
        stats = run_resolve(session, cfg, use_llm=not no_llm)
    typer.echo(json.dumps(stats))


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
    """Run the full pipeline: fetch → triage → extract → resolve → score → notify.
    Pings the dead man's switch (HEALTHCHECK_URL) on completion."""
    from app.ops import ping_healthcheck
    from app.spend import BudgetExceeded

    failures = 0
    for step in (fetch, triage, extract, resolve, score, notify):
        typer.echo(f"--- {step.__name__} ---")
        try:
            if step is fetch:
                step(source=None)
            elif step is resolve:
                step(no_llm=False)
            else:
                step()
        except BudgetExceeded as exc:
            typer.echo(f"{step.__name__} STOPPED BY BUDGET: {exc}", err=True)
            failures += 1
        except Exception as exc:  # noqa: BLE001 — later stages still run; failure is visible
            typer.echo(f"{step.__name__} FAILED: {exc}", err=True)
            failures += 1
    # The switch measures "the cron ran to completion", not "every source was
    # healthy" — per-source failures already alert via digest + dashboard.
    ping_healthcheck(success=True)
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
) -> None:
    """Log a manual signal (tip, engineer move, prequal/bid invite)."""
    from app.manual import add_manual_signal
    with session_scope() as session:
        s = add_manual_signal(session, signal_type, summary, project_name=project,
                              developer=developer, county=county, state=state, mw_it=mw_it,
                              mw_total=mw_total, stage=stage, person_name=person,
                              person_org=org, url=url)
        typer.echo(f"signal #{s.id} recorded ({s.signal_type.value}); run `scout resolve` to link it")


@app.command()
def backfill(
    source: str = typer.Option(..., help="ceqanet | goed | edgar | legistar"),
    since: str = typer.Option(..., help="YYYY-MM-DD start of historical window"),
    estimate: bool = typer.Option(False, help="Estimate LLM cost of the pending corpus, run nothing"),
    reset: bool = typer.Option(False, help="Discard checkpoints and refetch every chunk"),
) -> None:
    """Chunked, checkpointed historical pull. Fetch only — after it completes,
    check the cost with --estimate, then run `scout triage` and `scout extract`
    in batches. A crash resumes at the first incomplete chunk."""
    from datetime import datetime as dt

    from app.pipeline.backfill import estimate_cost, run_backfill
    cfg = load_config()
    if estimate:
        with session_scope() as session:
            typer.echo(json.dumps(estimate_cost(session, cfg), indent=2))
        return
    since_dt = dt.strptime(since, "%Y-%m-%d")
    with session_scope() as session:
        totals = run_backfill(session, cfg, source, since_dt, reset=reset)
    typer.echo(json.dumps(totals))
    typer.echo("Fetch done. Now run: scout backfill --source X --since ... --estimate "
               "to price the LLM pass, then scout triage / scout extract.")


golden_app = typer.Typer(help="Golden-set extraction evaluation (Gate 2).")
app.add_typer(golden_app, name="golden")


@golden_app.command("collect")
def golden_collect(limit: int = 30) -> None:
    """Snapshot extracted docs (weighted to CEQAnet NOP + GOED) for hand-verification."""
    from app.golden import collect
    with session_scope() as session:
        added = collect(session, limit=limit)
    typer.echo(f"{added} documents added to evals/golden.jsonl — now run: scout golden review")


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


@app.command("seed-firms")
def seed_firms_cmd() -> None:
    """Load the firm roster from config.yaml into the firms table."""
    from app.firms import seed_firms
    cfg = load_config()
    with session_scope() as session:
        added = seed_firms(session, cfg)
    typer.echo(f"{added} firms added (existing rows updated in place)")


@app.command("verify-sources")
def verify_sources() -> None:
    """Live smoke-test every enabled adapter; reports URL-structure drift."""
    from app.http import PoliteClient
    from app.sources import enabled_adapters, get_adapter
    cfg = load_config()
    with PoliteClient() as client:
        for name in enabled_adapters(cfg):
            result = get_adapter(name).verify(cfg, client)
            mark = "OK " if result["ok"] else "FAIL"
            typer.echo(f"[{mark}] {name}: {result['detail']}")


@app.command()
def serve(host: str = "0.0.0.0", port: int = 8000) -> None:
    """Run the dashboard."""
    import uvicorn
    uvicorn.run("app.web.main:app", host=host, port=port)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
