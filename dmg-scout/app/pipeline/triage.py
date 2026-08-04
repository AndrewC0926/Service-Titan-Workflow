"""TRIAGE: cheap Haiku pass over pending documents. Kills ~90% of volume
before paying for extraction. Errors leave documents in 'error' state, retried
on the next run, and never silently dropped."""
from __future__ import annotations

import logging

from sqlmodel import Session, select

from app.config import Config
from app.llm import LLMUnavailable, triage
from app.models import RawDocument, TriageResult

log = logging.getLogger(__name__)


def run_triage(session: Session, cfg: Config, limit: int = 200) -> dict:
    pending = session.exec(
        select(RawDocument)
        .where(RawDocument.triage_result.in_([TriageResult.pending, TriageResult.error]))
        .order_by(RawDocument.fetched_at)
        .limit(limit)
    ).all()

    stats = {"relevant": 0, "irrelevant": 0, "error": 0, "skipped": 0}
    for doc in pending:
        if doc.meta.get("skip_triage"):
            doc.triage_result = TriageResult.relevant
            doc.triage_reason = "adapter marked skip_triage"
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
        relevant = bool(verdict.get("relevant")) and bool(verdict.get("names_location"))
        doc.triage_result = TriageResult.relevant if relevant else TriageResult.irrelevant
        doc.triage_reason = str(verdict.get("reason", ""))[:500]
        stats["relevant" if relevant else "irrelevant"] += 1
        session.add(doc)
        session.commit()
    session.commit()
    return stats
