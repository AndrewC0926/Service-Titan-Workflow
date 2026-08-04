"""Historical backfill: chunked, checkpointed, resumable.

A chunk (county+doctype for CEQAnet, calendar month for EDGAR, whole crawl for
GOED) is recorded in backfill_checkpoints when it completes; a crash resumes at
the first incomplete chunk instead of restarting. Fetch-side only — triage and
extraction run afterwards via the normal stages, which are already incremental,
so the LLM spend is naturally batched and separately controllable.
"""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.http import PoliteClient
from app.models import BackfillCheckpoint, SourceRun, TriageResult, RawDocument, utcnow
from app.pipeline.fetch import _store
from app.sources import get_adapter

log = logging.getLogger(__name__)

BACKFILLABLE = ("ceqanet", "goed", "edgar", "legistar")


def run_backfill(session: Session, cfg: Config, source: str, since: datetime,
                 reset: bool = False) -> dict:
    if source not in BACKFILLABLE:
        raise ValueError(f"source {source!r} does not support backfill; choose from {BACKFILLABLE}")
    adapter = get_adapter(source)
    chunks = adapter.backfill_chunks(cfg, since)

    if reset:
        for cp in session.exec(select(BackfillCheckpoint)
                               .where(BackfillCheckpoint.source == source)).all():
            session.delete(cp)
        session.commit()

    done_keys = {
        cp.chunk_key for cp in session.exec(
            select(BackfillCheckpoint).where(BackfillCheckpoint.source == source)).all()
    }
    todo = [c for c in chunks if c["key"] not in done_keys]
    log.info("backfill %s since %s: %d chunks total, %d already done, %d to run",
             source, since.date(), len(chunks), len(chunks) - len(todo), len(todo))

    run = SourceRun(source=f"{source}:backfill")
    session.add(run)
    session.commit()
    totals = {"chunks_run": 0, "chunks_skipped": len(chunks) - len(todo),
              "fetched": 0, "new": 0, "chunk_errors": 0}
    try:
        with PoliteClient() as client:
            for chunk in todo:
                fetched = new = 0
                try:
                    for doc in adapter.fetch_chunk(cfg, client, since, chunk):
                        fetched += 1
                        new += _store(session, doc)
                        if fetched % 25 == 0:
                            session.commit()
                except Exception as exc:  # noqa: BLE001 — chunk fails, others continue, no checkpoint
                    totals["chunk_errors"] += 1
                    log.error("backfill chunk %s failed: %s", chunk["key"], exc)
                    session.rollback()
                    continue
                session.add(BackfillCheckpoint(source=source, chunk_key=chunk["key"],
                                               records_fetched=fetched, records_new=new))
                session.commit()
                totals["chunks_run"] += 1
                totals["fetched"] += fetched
                totals["new"] += new
        run.ok = totals["chunk_errors"] == 0
        if totals["chunk_errors"]:
            run.error = f"{totals['chunk_errors']} chunks failed; re-run to retry them"
    except Exception as exc:  # noqa: BLE001
        run.ok = False
        run.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        run.records_fetched = totals["fetched"]
        run.records_new = totals["new"]
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
    return totals


def estimate_cost(session: Session, cfg: Config, assumed_docs: dict[str, int] | None = None) -> dict:
    """Token/cost estimate for triaging + extracting the currently-pending corpus.

    If the backfill fetch has already run, this counts the real pending documents
    and their real sizes. Before any fetch, pass assumed_docs per source for a
    paper estimate.
    """
    prices = cfg.get("llm.prices", {})
    triage_model = cfg.get("llm.triage_model")
    extract_model = cfg.get("llm.extract_model")
    triage_chars = cfg.get("llm.triage_max_chars", 6000)
    extract_chars = cfg.get("llm.extract_max_chars", 60000)
    pass_rate = cfg.get("llm.assumed_triage_pass_rate", 0.3)

    pending = session.exec(
        select(RawDocument).where(RawDocument.triage_result == TriageResult.pending)
    ).all()
    if pending:
        n_docs = len(pending)
        avg_triage_tokens = sum(min(len(d.raw_text), triage_chars) for d in pending) / n_docs / 4
        avg_extract_tokens = sum(min(len(d.raw_text), extract_chars) for d in pending) / n_docs / 4
        basis = f"measured from {n_docs} pending documents in DB"
    else:
        n_docs = sum((assumed_docs or {}).values()) or 1200
        avg_triage_tokens = triage_chars / 4
        avg_extract_tokens = 12000  # typical filing well under the cap
        basis = f"paper estimate, {n_docs} assumed documents"

    triage_in = n_docs * (avg_triage_tokens + 400)   # + system prompt overhead
    triage_out = n_docs * 80
    extract_docs = int(n_docs * pass_rate)
    extract_in = extract_docs * (avg_extract_tokens + 700)
    extract_out = extract_docs * 900

    def cost(model: str, tin: float, tout: float) -> float:
        p = prices.get(model, {"in": 3.0, "out": 15.0})
        return tin / 1e6 * p["in"] + tout / 1e6 * p["out"]

    triage_cost = cost(triage_model, triage_in, triage_out)
    extract_cost = cost(extract_model, extract_in, extract_out)
    return {
        "basis": basis,
        "docs_to_triage": n_docs,
        "docs_to_extract_at_assumed_pass_rate": extract_docs,
        "triage_tokens_in": int(triage_in), "triage_tokens_out": int(triage_out),
        "extract_tokens_in": int(extract_in), "extract_tokens_out": int(extract_out),
        "triage_cost_usd": round(triage_cost, 2),
        "extract_cost_usd": round(extract_cost, 2),
        "total_cost_usd": round(triage_cost + extract_cost, 2),
    }
