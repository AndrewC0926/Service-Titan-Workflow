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
    from app.duplicates import find_duplicates
    from app.pipeline.resolve import run_resolve
    cfg = load_config()
    with session_scope() as session:
        stats = run_resolve(session, cfg, use_llm=not no_llm)
        # Always, not on request: a resolver that starts fragmenting the board
        # fails silently otherwise — the row count simply grows.
        dup = find_duplicates(session)
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
    # grounding sits between extract and resolve on purpose: it is the last point
    # where a fabricated number can be caught before it becomes a project, a
    # tonnage estimate and a row someone quotes.
    for step in (fetch, triage, extract, grounding, resolve, score, notify):
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
