"""TRIAGE: cheap Haiku pass over pending documents. Classifies rather than
filters — a document comes back data_center, industrial, or other, and only
`other` is dropped. The category rides along in `meta['triage_category']` so
extraction can stamp it on the signal without a second LLM call.

Errors leave documents in 'error' state, retried on the next run, and never
silently dropped."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.llm import LLMUnavailable, triage
from app.models import Category, RawDocument, TriageResult

log = logging.getLogger(__name__)


def run_triage(session: Session, cfg: Config, limit: int = 200) -> dict:
    pending = session.exec(
        select(RawDocument)
        .where(RawDocument.triage_result.in_([TriageResult.pending, TriageResult.error]))
        .order_by(RawDocument.fetched_at)
        .limit(limit)
    ).all()

    stats = {"data_center": 0, "industrial": 0, "irrelevant": 0, "error": 0, "skipped": 0}
    for doc in pending:
        if doc.meta.get("skip_triage"):
            doc.triage_result = TriageResult.relevant
            doc.triage_reason = "adapter marked skip_triage"
            # An adapter that bypasses triage cannot know the category; the
            # extractor's own read decides it later.
            doc.meta = {**(doc.meta or {}), "triage_category": Category.other.value}
            stats["skipped"] += 1
            session.add(doc)
            continue
        try:
            verdict = triage(doc.raw_text, title=doc.title, source=doc.source)
        except LLMUnavailable:
            raise
        except Exception as exc:  # noqa: BLE001
            doc.triage_result = TriageResult.error
            doc.triage_reason = f"{type(exc).__name__}: {exc}"[:500]
            stats["error"] += 1
            session.add(doc)
            log.warning("triage failed for doc %s: %s", doc.id, exc)
            continue

        try:
            category = Category(verdict.get("category"))
        except ValueError:
            # An unrecognised label is not a licence to guess: drop it and say why.
            log.warning("triage returned unknown category %r for doc %s",
                        verdict.get("category"), doc.id)
            category = Category.other

        # A project with no named location cannot be resolved, sized or sold to,
        # so it is not actionable however relevant it sounds.
        if not verdict.get("names_location"):
            category = Category.other

        relevant = category is not Category.other
        doc.triage_result = TriageResult.relevant if relevant else TriageResult.irrelevant
        doc.triage_reason = str(verdict.get("reason", ""))[:500]
        doc.meta = {**(doc.meta or {}), "triage_category": category.value}
        stats[category.value if relevant else "irrelevant"] += 1
        session.add(doc)
        session.commit()
    session.commit()
    return stats
