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
                fetched = new = 0
                for doc in adapter.fetch(cfg, client):
                    fetched += 1
                    new += _store(session, doc)
                    if fetched % 25 == 0:
                        session.commit()
                run.records_fetched, run.records_new, run.ok = fetched, new, True
            except Exception as exc:  # noqa: BLE001 — recorded, surfaced in digest + dashboard
                run.ok = False
                run.error = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=5)}"
                log.error("source %s failed: %s", name, exc)
        run.finished_at = utcnow()
        session.add(run)
        session.commit()
        runs[name] = run
    return runs


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
