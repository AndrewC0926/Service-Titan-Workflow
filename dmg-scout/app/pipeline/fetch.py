"""FETCH + DEDUPE: run source adapters, store new documents, record source health.

Idempotent: (source, source_uid) is unique; an unchanged content hash is skipped,
a changed hash for a known uid updates the row and re-queues it for triage.
"""
from __future__ import annotations

import logging
import traceback

from sqlmodel import Session, select

from app.config import Config
from app.http import PoliteClient
from app.models import HttpLog, RawDocument, SourceRun, TriageResult, utcnow
from app.pipeline_health import heartbeat
from app.sources import enabled_adapters, get_adapter

log = logging.getLogger(__name__)

MAX_HTTP_LOG_PER_RUN = 1000


def _http_recorder(session: Session, run: SourceRun):
    """Archive every request of a source run for after-the-fact debugging."""
    count = {"n": 0}

    def recorder(**kw) -> None:
        if count["n"] >= MAX_HTTP_LOG_PER_RUN:
            return
        count["n"] += 1
        session.add(HttpLog(source_run_id=run.id, **kw))

    return recorder


def run_fetch(session: Session, cfg: Config, only_source: str | None = None) -> dict[str, SourceRun]:
    names = [only_source] if only_source else enabled_adapters(cfg)
    runs: dict[str, SourceRun] = {}
    for name in names:
        run = SourceRun(source=name)
        session.add(run)
        session.commit()
        with PoliteClient(recorder=_http_recorder(session, run)) as client:
            try:
                adapter = get_adapter(name)
                fetched = new = doc_errors = 0
                first_error = None
                for doc in adapter.fetch(cfg, client):
                    fetched += 1
                    stored, err = store_document(session, doc)
                    new += stored
                    if err:
                        doc_errors += 1
                        first_error = first_error or f"{doc.source_uid}: {err}"
                        log.warning("source %s: document %s failed to store: %s",
                                    name, doc.source_uid, err)
                run.records_fetched, run.records_new = fetched, new
                # Unstorable documents are a real defect (bad bytes, oversized
                # field), and they are almost always deterministic — so surface
                # them loudly rather than letting a green run hide them.
                run.ok = doc_errors == 0
                if doc_errors:
                    run.error = (f"{doc_errors}/{fetched} documents failed to store; "
                                 f"first: {first_error}")
            except Exception as exc:  # noqa: BLE001 — recorded, surfaced in digest + dashboard
                run.ok = False
                run.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
                log.error("source %s failed: %s", name, exc)
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        runs[name] = run
        # fetch is the one stage observed running long enough (over an hour
        # against the full backlog) that a per-stage-only heartbeat could go
        # stale while genuinely still alive -- touch it once per source
        # rather than only once for the whole stage.
        heartbeat(session)
    return runs


def store_document(session: Session, doc) -> tuple[int, str | None]:
    """Store one document in its own savepoint, committing immediately.

    Returns (1 if new/changed else 0, error string or None).

    One malformed document must cost exactly one document. The old loop stored
    into a shared transaction and committed every 25, then called
    session.rollback() when anything raised — so a single PDF carrying NUL bytes
    discarded up to 24 healthy siblings, and because GOED's whole crawl is one
    chunk it discarded the entire pass. The run still looked alive the whole time.

    A SAVEPOINT is what makes this recoverable rather than merely smaller-grained:
    once Postgres rejects a statement the transaction is aborted and every later
    statement fails too, so the bad document has to be rolled back individually
    before the loop can continue.
    """
    try:
        with session.begin_nested():
            n = _store(session, doc)
        session.commit()
        return n, None
    except Exception as exc:  # noqa: BLE001 — one bad document, not one bad run
        session.rollback()
        return 0, f"{type(exc).__name__}: {exc}"


def _store(session: Session, doc) -> int:
    """Insert or update one fetched document. Returns 1 if new/changed else 0."""
    existing = session.exec(
        select(RawDocument).where(
            RawDocument.source == doc.source, RawDocument.source_uid == doc.source_uid
        )
    ).first()
    if existing is None:
        session.add(RawDocument(
            source=doc.source,
            source_uid=doc.source_uid,
            url=doc.url,
            title=doc.title,
            published_at=doc.published_at,
            content_hash=doc.content_hash,
            raw_text=doc.raw_text,
            meta={**doc.meta, "default_signal_type": doc.default_signal_type.value,
                  "skip_triage": doc.skip_triage},
        ))
        return 1
    if existing.content_hash != doc.content_hash:
        existing.raw_text = doc.raw_text
        existing.content_hash = doc.content_hash
        existing.title = doc.title
        existing.fetched_at = utcnow()
        existing.triage_result = TriageResult.pending  # re-triage changed content
        existing.processed_at = None
        session.add(existing)
        return 1
    return 0
