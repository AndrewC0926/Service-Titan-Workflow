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

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlmodel import Session, select

from app.config import Config
from app.http import PoliteClient
from app.models import (
    BACKFILL_RUN_MODE, BackfillCheckpoint, RawDocument, SourceRun, TokenSpend,
    TriageResult, source_run_name, utcnow,
)
from app.pipeline.fetch import store_document
from app.sources import get_adapter

log = logging.getLogger(__name__)

BACKFILLABLE = ("ceqanet", "goed", "edgar", "legistar", "civicplus", "primegov")


class ConcurrentBackfill(RuntimeError):
    """Another backfill of the same source is still running."""


def _checkpoint(session: Session, source: str, chunk_key: str,
                fetched: int, new: int) -> None:
    """Record a completed chunk, tolerating one that is already recorded.

    (source, chunk_key) is unique, and a plain INSERT of a duplicate aborts the
    transaction and takes the whole backfill down with it — observed for real when
    two backfill processes overlapped on the same source. A chunk being
    checkpointed twice is harmless; crashing 20 chunks in is not.
    """
    try:
        with session.begin_nested():
            session.add(BackfillCheckpoint(source=source, chunk_key=chunk_key,
                                           records_fetched=fetched, records_new=new))
        session.commit()
    except IntegrityError:
        session.rollback()
        log.warning("chunk %s for %s was already checkpointed (concurrent run?)",
                    chunk_key, source)


# A run killed by SIGKILL (a `timeout`, an OOM, a container restart) never gets
# to set finished_at, so it stays "unfinished" forever. Without an age limit the
# concurrency guard below would wedge that source permanently and train everyone
# to pass --force reflexively, which defeats the guard. Real overlap is a
# minutes-scale problem; anything older than this is a corpse, not a competitor.
STALE_RUN_HOURS = 12


def _running_backfill(session: Session, source: str,
                      stale_after_hours: float = STALE_RUN_HOURS) -> SourceRun | None:
    """A *live* unfinished backfill run for this source, if one exists.

    Unfinished runs older than `stale_after_hours` are treated as dead and do not
    block a new run.
    """
    run = session.exec(
        select(SourceRun)
        .where(SourceRun.source == f"{source}:backfill",
               SourceRun.finished_at.is_(None))
        .order_by(SourceRun.id.desc())
    ).first()
    if run is None:
        return None
    age_hours = (utcnow() - run.started_at).total_seconds() / 3600
    if age_hours > stale_after_hours:
        log.warning("ignoring source_run #%d for %s: unfinished but %.1fh old, "
                    "so it was almost certainly killed rather than still running",
                    run.id, source, age_hours)
        return None
    return run


def run_backfill(session: Session, cfg: Config, source: str, since: datetime,
                 reset: bool = False, force: bool = False) -> dict:
    if source not in BACKFILLABLE:
        raise ValueError(f"source {source!r} does not support backfill; choose from {BACKFILLABLE}")

    # Two concurrent backfills of one source duplicate every request, race on the
    # checkpoint table, and make the totals meaningless. Refuse by default.
    inflight = _running_backfill(session, source)
    if inflight is not None and not force:
        raise ConcurrentBackfill(
            f"backfill for {source!r} already running (source_run #{inflight.id}, "
            f"started {inflight.started_at:%Y-%m-%d %H:%M:%S}Z). Wait for it, or "
            f"pass force=True / --force if you are sure it is dead.")

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

    run = SourceRun(source=source_run_name(source, BACKFILL_RUN_MODE))
    session.add(run)
    session.commit()
    totals = {"chunks_run": 0, "chunks_skipped": len(chunks) - len(todo),
              "fetched": 0, "new": 0, "chunk_errors": 0, "doc_errors": 0}
    first_error = None
    run_ok: bool | None = None
    run_error: str | None = None
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
                _checkpoint(session, source, chunk["key"], fetched, new)
                totals["chunks_run"] += 1
                totals["fetched"] += fetched
                totals["new"] += new
                totals["doc_errors"] += doc_errors
        problems = []
        if totals["chunk_errors"]:
            problems.append(f"{totals['chunk_errors']} chunks failed; re-run to retry them")
        if totals["doc_errors"]:
            problems.append(f"{totals['doc_errors']} documents failed to store; "
                            f"first: {first_error}")
        run_ok = not problems
        run_error = "; ".join(problems) or None
    except Exception as exc:  # noqa: BLE001
        run_ok = False
        run_error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # The outcome is held in locals, not written to `run` directly, because
        # the rollback below would revert pending attribute changes on a
        # persistent object — and the rollback is required: if the run died on a
        # database error the session is in a failed transaction, so recording the
        # outcome would raise PendingRollbackError and destroy the very report
        # that explains what happened. That is what left two runs stuck at
        # ok=None with no error after a duplicate checkpoint insert.
        try:
            session.rollback()
        except Exception:  # noqa: BLE001 — best effort; recording matters more
            log.debug("rollback before recording backfill outcome failed")
        try:
            run.records_fetched = totals["fetched"]
            run.records_new = totals["new"]
            run.finished_at = utcnow()
            run.ok = run_ok
            run.error = run_error
            session.add(run)
            session.commit()
        except Exception as exc:  # noqa: BLE001
            log.error("could not record backfill outcome for %s: %s", source, exc)
    return totals


MIN_PASS_RATE_SAMPLE = 100  # below this a measured rate is noise, not a measurement

# Phase B gate: an estimate must land within this of a measured actual.
ESTIMATE_TOLERANCE = 0.10


def validate_estimate(session: Session, cfg: Config, since: datetime, until: datetime,
                      sources: list[str] | None = None) -> dict:
    """Predicted vs actually-spent tokens, for a window whose corpus is known.

    Invariant 8 says no estimate is trusted that has not been validated against
    actuals, and until this existed the estimator had never been checked against
    one. It had drifted: on 2026-08-06 it predicted triage input 10.9% under what
    the same 673 documents actually cost.

    Honest use requires a window in which the documents still in the table are
    the documents that window paid for. That is not automatic — a purge and
    re-backfill leaves spend behind for documents that no longer exist — so the
    call count and the document count are both reported per stage, and a
    mismatch between them is surfaced rather than absorbed. A validation run
    across a purge boundary is not a measurement, and it must not read like one.
    """
    from app.sections import select_relevant_text, select_triage_text

    cpt = cfg.get("llm.chars_per_token", 3.7)
    ovh_t = cfg.get("llm.triage_prompt_overhead_tokens", 1087)
    ovh_e = cfg.get("llm.extract_prompt_overhead_tokens", 1462)
    out_t = cfg.get("llm.triage_output_tokens", 95)
    out_e = cfg.get("llm.extract_output_tokens", 624)

    actual: dict[str, dict] = {}
    for stage, n, tin, tout, cost in session.exec(
        select(TokenSpend.stage, func.count(TokenSpend.id),
               func.sum(TokenSpend.input_tokens), func.sum(TokenSpend.output_tokens),
               func.sum(TokenSpend.cost_usd))
        .where(TokenSpend.ts >= since, TokenSpend.ts < until)
        .group_by(TokenSpend.stage)
    ).all():
        actual[stage] = {"calls": n, "in": int(tin or 0), "out": int(tout or 0),
                         "cost_usd": float(cost or 0)}

    q = select(RawDocument).where(RawDocument.triage_result != TriageResult.pending)
    if sources:
        q = q.where(RawDocument.source.in_(sources))
    docs = session.exec(q).all()
    relevant = [d for d in docs if d.triage_result == TriageResult.relevant]

    pred = {
        "triage": {
            "docs": len(docs),
            "in": sum(select_triage_text(d.raw_text, cfg).body_chars / cpt + ovh_t
                      for d in docs),
            "out": len(docs) * out_t,
        },
        "extract": {
            "docs": len(relevant),
            "in": sum(select_relevant_text(d.raw_text, cfg).selected_chars / cpt + ovh_e
                      for d in relevant),
            "out": len(relevant) * out_e,
        },
    }

    stages = {}
    for stage in ("triage", "extract"):
        a, p = actual.get(stage), pred[stage]
        if not a:
            stages[stage] = {"status": "no actual spend in window"}
            continue
        # Per-call, not per-total: the corpus and the call count need not agree
        # (retries, a purge, a partial run), and comparing totals across that gap
        # silently attributes the difference to the model instead of the mismatch.
        entry = {"predicted_docs": p["docs"], "actual_calls": a["calls"]}
        for field in ("in", "out"):
            pc = p[field] / p["docs"] if p["docs"] else 0.0
            ac = a[field] / a["calls"] if a["calls"] else 0.0
            err = (pc - ac) / ac if ac else float("nan")
            entry[f"tokens_{field}_per_call"] = {
                "predicted": round(pc), "actual": round(ac),
                "error_pct": round(err * 100, 1),
                "within_tolerance": abs(err) <= ESTIMATE_TOLERANCE,
            }
        entry["corpus_matches_calls"] = abs(p["docs"] - a["calls"]) <= 0.02 * max(a["calls"], 1)
        stages[stage] = entry

    checked = [v for s in stages.values() if isinstance(s, dict)
               for k, v in s.items() if k.startswith("tokens_")]
    return {
        "window": f"{since:%Y-%m-%d %H:%M} to {until:%Y-%m-%d %H:%M}",
        "sources": sources or "all",
        "tolerance_pct": ESTIMATE_TOLERANCE * 100,
        "stages": stages,
        "pass_rate": dict(zip(("value", "basis"), measured_pass_rate(session, cfg))),
        "gate_passed": bool(checked) and all(c["within_tolerance"] for c in checked),
    }


def measured_pass_rate(session: Session, cfg: Config) -> tuple[float, str]:
    """The share of triaged documents that go on to extraction, MEASURED.

    Invariant 8 is explicit that the estimator must measure the real corpus
    rather than carry an assumption, and this was the last hardcoded assumption
    left in it. The config value said 0.39; the corpus on 2026-08-06 says 0.541
    across 861 triaged documents (0.541 on the 673 CEQAnet documents alone). An
    estimate built on 0.39 understates extraction — the expensive Sonnet stage —
    by a third, and it understates it in the direction that gets a budget
    approved and then overrun.

    Falls back to config only when there is genuinely nothing to measure, and
    says which happened in the returned basis string, because "estimated on 861
    real documents" and "estimated on a number somebody typed in a YAML file"
    must never look alike on the same report.
    """
    counts = dict(session.exec(
        select(RawDocument.triage_result, func.count(RawDocument.id))
        .where(RawDocument.triage_result != TriageResult.pending)
        .group_by(RawDocument.triage_result)).all())
    n_relevant = counts.get(TriageResult.relevant, 0)
    n_judged = sum(counts.values())
    if n_judged < MIN_PASS_RATE_SAMPLE:
        return (cfg.get("llm.assumed_triage_pass_rate", 0.39),
                f"config assumption (only {n_judged} triaged documents to measure, "
                f"need {MIN_PASS_RATE_SAMPLE})")
    return (n_relevant / n_judged,
            f"measured over {n_judged} triaged documents ({n_relevant} relevant)")


def estimate_cost(session: Session, cfg: Config, assumed_docs: dict[str, int] | None = None) -> dict:
    """Token/cost estimate for triaging + extracting the currently-pending corpus.

    If the backfill fetch has already run, this counts the real pending documents
    and their real sizes. Before any fetch, pass assumed_docs per source for a
    paper estimate.
    """
    prices = cfg.get("llm.prices", {})
    triage_model = cfg.get("llm.triage_model")
    extract_model = cfg.get("llm.extract_model")
    extract_chars = cfg.get("llm.extract_max_chars", 60000)
    pass_rate, pass_rate_basis = measured_pass_rate(session, cfg)
    cpt = cfg.get("llm.chars_per_token", 3.7)
    ovh_t = cfg.get("llm.triage_prompt_overhead_tokens", 1087)
    ovh_e = cfg.get("llm.extract_prompt_overhead_tokens", 1462)
    out_t = cfg.get("llm.triage_output_tokens", 95)
    out_e = cfg.get("llm.extract_output_tokens", 624)
    adj_per = cfg.get("llm.adjudicate_cost_per_signal", 0.0034)

    from app.sections import select_relevant_text, select_triage_text

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
            # What each stage actually sends, not what the document contains:
            # triage reads whole up to its cap then samples a spread, extraction
            # section-chunks. Measuring both separately is the only way an EIR and
            # an NOE can show different numbers.
            avg_triage = sum(select_triage_text(d.raw_text, cfg).body_chars
                             for d in sample) / len(sample)
            avg_selected = sum(select_relevant_text(d.raw_text, cfg).selected_chars
                               for d in sample) / len(sample)
            g_triage_in = len(docs) * (avg_triage / cpt + ovh_t)
            g_extract_in = len(docs) * pass_rate * (avg_selected / cpt + ovh_e)
            triage_in += g_triage_in
            extract_in += g_extract_in
            by_type[key] = {
                "docs": len(docs),
                "avg_raw_chars": int(avg_raw),
                "avg_chars_sent_to_triage": int(avg_triage),
                "avg_chars_sent_to_sonnet": int(avg_selected),
                "extract_tokens_per_doc": int(avg_selected / cpt + ovh_e),
                "est_cost_usd": round(
                    cost(triage_model, g_triage_in, len(docs) * out_t)
                    + cost(extract_model, g_extract_in, len(docs) * pass_rate * out_e), 2),
            }
        triage_out = n_docs * out_t
        extract_docs = int(n_docs * pass_rate)
        extract_out = extract_docs * out_e
        basis = (f"measured from {n_docs} pending documents in DB "
                 f"(per-stage chunking, measured token constants)")
    else:
        n_docs = sum((assumed_docs or {}).values()) or 1200
        assumed_chars = cfg.get("llm.assumed_doc_chars", 12000)
        triage_in = n_docs * (min(assumed_chars, cfg.get("llm.triage_max_chars", 60000))
                              / cpt + ovh_t)
        triage_out = n_docs * out_t
        extract_docs = int(n_docs * pass_rate)
        extract_in = extract_docs * (assumed_chars / cpt + ovh_e)
        extract_out = extract_docs * out_e
        by_type = {}
        basis = f"paper estimate, {n_docs} assumed documents"

    triage_cost = cost(triage_model, triage_in, triage_out)
    extract_cost = cost(extract_model, extract_in, extract_out)
    # Resolution was missing from every estimate this system has ever produced,
    # which quietly understated the total by roughly a tenth.
    adjudicate_cost = extract_docs * adj_per
    return {
        "basis": basis,
        "docs_to_triage": n_docs,
        "docs_to_extract_at_measured_pass_rate": extract_docs,
        "triage_pass_rate": round(pass_rate, 3),
        "triage_pass_rate_basis": pass_rate_basis,
        "by_document_type": by_type,
        "triage_tokens_in": int(triage_in), "triage_tokens_out": int(triage_out),
        "extract_tokens_in": int(extract_in), "extract_tokens_out": int(extract_out),
        "triage_cost_usd": round(triage_cost, 2),
        "extract_cost_usd": round(extract_cost, 2),
        "adjudicate_cost_usd": round(adjudicate_cost, 2),
        "total_cost_usd": round(triage_cost + extract_cost + adjudicate_cost, 2),
    }
