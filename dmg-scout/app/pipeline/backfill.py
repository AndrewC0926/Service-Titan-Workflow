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
from app.pipeline.fetch import store_document
from app.sources import get_adapter

log = logging.getLogger(__name__)

BACKFILLABLE = ("ceqanet", "goed", "edgar", "legistar", "civicplus", "primegov")


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
              "fetched": 0, "new": 0, "chunk_errors": 0, "doc_errors": 0}
    first_error = None
    try:
        with PoliteClient() as client:
            for chunk in todo:
                fetched = new = doc_errors = 0
                # Each document lands in its own committed transaction, so a
                # chunk that dies halfway keeps everything it already fetched.
                # GOED is the worst case: its whole crawl is one chunk, and the
                # old shared transaction meant a single NUL-carrying PDF rolled
                # back the entire pass while the run still reported progress.
                try:
                    for doc in adapter.fetch_chunk(cfg, client, since, chunk):
                        fetched += 1
                        stored, err = store_document(session, doc)
                        new += stored
                        if err:
                            doc_errors += 1
                            first_error = first_error or f"{doc.source_uid}: {err}"
                            log.warning("backfill chunk %s: document %s failed to store: %s",
                                        chunk["key"], doc.source_uid, err)
                except Exception as exc:  # noqa: BLE001 — chunk fails, others continue
                    totals["chunk_errors"] += 1
                    totals["fetched"] += fetched
                    totals["new"] += new
                    totals["doc_errors"] += doc_errors
                    log.error("backfill chunk %s failed after %d documents "
                              "(those are kept): %s", chunk["key"], fetched, exc)
                    session.rollback()   # clear the failed fetch, not the stored docs
                    continue             # no checkpoint: the chunk is incomplete
                session.add(BackfillCheckpoint(source=source, chunk_key=chunk["key"],
                                               records_fetched=fetched, records_new=new))
                session.commit()
                totals["chunks_run"] += 1
                totals["fetched"] += fetched
                totals["new"] += new
                totals["doc_errors"] += doc_errors
        run.ok = totals["chunk_errors"] == 0 and totals["doc_errors"] == 0
        problems = []
        if totals["chunk_errors"]:
            problems.append(f"{totals['chunk_errors']} chunks failed; re-run to retry them")
        if totals["doc_errors"]:
            problems.append(f"{totals['doc_errors']} documents failed to store; "
                            f"first: {first_error}")
        if problems:
            run.error = "; ".join(problems)
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

    from app.sections import select_relevant_text

    def cost(model: str, tin: float, tout: float) -> float:
        p = prices.get(model, {"in": 3.0, "out": 15.0})
        return tin / 1e6 * p["in"] + tout / 1e6 * p["out"]

    pending = session.exec(
        select(RawDocument).where(RawDocument.triage_result == TriageResult.pending)
    ).all()
    if pending:
        # Group by (source, document_type) and measure the ACTUAL chars that
        # extraction would send after section-aware chunking — an EIR must show
        # a different number than an NOP, or something is being discarded.
        groups: dict[str, list[RawDocument]] = {}
        for d in pending:
            key = f"{d.source}:{(d.meta or {}).get('document_type') or '-'}"
            groups.setdefault(key, []).append(d)
        by_type = {}
        n_docs = len(pending)
        triage_in = extract_in = 0.0
        for key, docs in sorted(groups.items()):
            sample = docs[:50]  # regex pass only; sampling keeps this instant
            avg_raw = sum(len(d.raw_text) for d in sample) / len(sample)
            avg_selected = sum(select_relevant_text(d.raw_text, cfg).selected_chars
                               for d in sample) / len(sample)
            g_triage_in = len(docs) * (min(avg_raw, triage_chars) / 4 + 400)
            g_extract_in = len(docs) * pass_rate * (avg_selected / 4 + 700)
            triage_in += g_triage_in
            extract_in += g_extract_in
            by_type[key] = {
                "docs": len(docs),
                "avg_raw_chars": int(avg_raw),
                "avg_chars_sent_to_sonnet": int(avg_selected),
                "extract_tokens_per_doc": int(avg_selected / 4 + 700),
                "est_cost_usd": round(
                    cost(triage_model, g_triage_in, len(docs) * 80)
                    + cost(extract_model, g_extract_in, len(docs) * pass_rate * 900), 2),
            }
        triage_out = n_docs * 80
        extract_docs = int(n_docs * pass_rate)
        extract_out = extract_docs * 900
        basis = f"measured from {n_docs} pending documents in DB (section-aware)"
    else:
        n_docs = sum((assumed_docs or {}).values()) or 1200
        triage_in = n_docs * (triage_chars / 4 + 400)
        triage_out = n_docs * 80
        extract_docs = int(n_docs * pass_rate)
        extract_in = extract_docs * (12000 + 700)
        extract_out = extract_docs * 900
        by_type = {}
        basis = f"paper estimate, {n_docs} assumed documents"

    triage_cost = cost(triage_model, triage_in, triage_out)
    extract_cost = cost(extract_model, extract_in, extract_out)
    return {
        "basis": basis,
        "docs_to_triage": n_docs,
        "docs_to_extract_at_assumed_pass_rate": extract_docs,
        "by_document_type": by_type,
        "triage_tokens_in": int(triage_in), "triage_tokens_out": int(triage_out),
        "extract_tokens_in": int(extract_in), "extract_tokens_out": int(extract_out),
        "triage_cost_usd": round(triage_cost, 2),
        "extract_cost_usd": round(extract_cost, 2),
        "total_cost_usd": round(triage_cost + extract_cost, 2),
    }
