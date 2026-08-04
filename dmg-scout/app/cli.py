"""scout CLI — every pipeline stage independently runnable and idempotent."""
from __future__ import annotations

import json
import logging

import typer

from app.config import load_config
from app.db import init_db, session_scope

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

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
    """Run the full pipeline: fetch → triage → extract → resolve → score → notify."""
    for step in (fetch, triage, extract, resolve, score, notify):
        typer.echo(f"--- {step.__name__} ---")
        try:
            if step is fetch:
                step(source=None)
            elif step is resolve:
                step(no_llm=False)
            elif step in (triage, extract):
                step()
            else:
                step()
        except Exception as exc:  # noqa: BLE001 — later stages still run; failure is visible
            typer.echo(f"{step.__name__} FAILED: {exc}", err=True)


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
