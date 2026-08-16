"""EXTRACT: Sonnet structured extraction over triaged-relevant documents.
One document can yield one signal (v1: one signal per document; the extraction
JSON is stored in full so nothing is lost). EDGAR hits fetch the filing body
first, since full-text search returns only metadata."""
from __future__ import annotations

import logging
from datetime import datetime

from sqlmodel import Session, select

from app.config import Config
from app.grounding import reject_ungrounded_numbers
from app.http import PoliteClient
from app.llm import LLMUnavailable, extract
from app.normalize import normalize_state
from app.models import (
    Category,
    FacilityType,
    RawDocument,
    Signal,
    SignalType,
    Stage,
    TriageResult,
    utcnow,
)
from app.runguard import stage_run

log = logging.getLogger(__name__)

MAX_BODY_BYTES = 2_000_000


def _ensure_body(doc: RawDocument, client: PoliteClient) -> str:
    """EDGAR (and similar) store metadata at fetch time; pull the real body once."""
    if not doc.meta.get("needs_body_fetch"):
        return doc.raw_text
    try:
        resp = client.get(doc.url)
        body = resp.text[:MAX_BODY_BYTES]
        if body.strip():
            return doc.raw_text + "\n\n--- DOCUMENT BODY ---\n\n" + body
    except Exception as exc:  # noqa: BLE001
        log.warning("body fetch failed for doc %s (%s): %s", doc.id, doc.url, exc)
    return doc.raw_text


def run_extract(session: Session, cfg: Config, limit: int = 100,
                force: bool = False) -> dict:
    """Structured extraction over triaged documents, one signal per document.

    Guarded for the same reason resolve is. This stage selects documents whose
    processed_at is NULL, then spends a Sonnet call on each before writing — so
    two concurrent runs whose selection queries both land before either commits
    will both extract the same documents. That is the #961/#963 failure shape on
    the most expensive stage in the pipeline; it had simply not been caught yet.
    Backstopped by uq_signal_raw_document, which makes a slip loud instead of
    silent.
    """
    with stage_run(session, "extract", force=force):
        return _extract_docs(session, cfg, limit)


def _extract_docs(session: Session, cfg: Config, limit: int) -> dict:
    docs = session.exec(
        select(RawDocument)
        .where(RawDocument.triage_result == TriageResult.relevant,
               RawDocument.processed_at.is_(None))
        .order_by(RawDocument.fetched_at)
        .limit(limit)
    ).all()

    stats = {"extracted": 0, "errors": 0, "rejected_numbers": 0,
             "flagged_signals": 0}
    with PoliteClient() as client:
        for doc in docs:
            text = _ensure_body(doc, client)
            try:
                data = extract(text, title=doc.title, source=doc.source, url=doc.url)
            except LLMUnavailable:
                raise
            except Exception as exc:  # noqa: BLE001
                stats["errors"] += 1
                log.warning("extract failed for doc %s: %s", doc.id, exc)
                continue

            event_date = None
            if data.get("event_date"):
                try:
                    event_date = datetime.fromisoformat(data["event_date"])
                except ValueError:
                    pass

            default_type = doc.meta.get("default_signal_type", SignalType.news_report.value)
            try:
                signal_type = SignalType(default_type)
            except ValueError:
                signal_type = SignalType.news_report

            stage_val = data.get("stage") or "unknown"
            try:
                stage = Stage(stage_val)
            except ValueError:
                stage = Stage.unknown

            # Category is triage's call, carried over rather than re-inferred: one
            # classification per document, and the board filter matches what triage
            # actually decided.
            try:
                category = Category(doc.meta.get("triage_category"))
            except ValueError:
                category = Category.other

            # Unit guard, before anything downstream can see the value. Checked
            # against the FULL document, not the chunk the model was shown: a value
            # grounded anywhere in the filing is grounded, and being stricter than
            # that would reject good numbers on long documents.
            data, rejected = reject_ungrounded_numbers(data, text)
            if rejected:
                stats["rejected_numbers"] += len(rejected)
                for r in rejected:
                    log.warning("doc %s: rejected %s=%g — %s",
                                doc.id, r["field"], r["value"], r["reason"])

            # Idempotency: one signal per raw document; re-extraction replaces it.
            existing = session.exec(
                select(Signal).where(Signal.raw_document_id == doc.id)
            ).first()
            signal = existing or Signal(raw_document_id=doc.id, signal_type=signal_type)
            signal.signal_type = signal_type
            signal.category = category
            try:
                signal.facility_type = FacilityType(data.get("facility_type"))
            except ValueError:
                signal.facility_type = FacilityType.unknown
            signal.event_date = event_date or doc.published_at
            for f in ("project_name", "developer_or_owner", "jurisdiction", "county", "state",
                      "street_address", "apn_parcel", "latitude", "longitude", "mw_it", "mw_total",
                      "generator_count", "generator_hp_each", "generator_kw_each",
                      "generator_critical_count", "generator_critical_mw_each",
                      "generator_house_count", "generator_house_mw_each", "building_sqft",
                      "acres", "building_count", "cooling_type", "water_acre_feet_per_year",
                      "water_source_stated", "water_reclaimed_identified",
                      "water_use_efficiency_stated", "water_opposition_stated",
                      "filing_type", "delivery_method"):
                setattr(signal, f, data.get(f))
            # Normalized on write, not left for a downstream state-keyed lookup
            # to silently miss: the extraction prompt correctly reports a state
            # exactly as the document phrases it ("State of Nevada"), which is
            # right for extraction fidelity but wrong for every state-keyed
            # table in this codebase (territory, county adjacency, day-to-bid
            # overrides), all of which assume a 2-letter USPS code.
            signal.state = normalize_state(signal.state)
            # NOT from the LLM: the adapter read it off the source record, so it is
            # the one identifier on a signal that cannot be hallucinated. See
            # Signal.sch_number and pair_similarity().
            signal.sch_number = (doc.meta or {}).get("sch_number")
            signal.stage = stage
            signal.summary_one_line = data.get("summary_one_line", "")
            signal.confidence = data.get("confidence", 0.0)
            # Kept on the signal so the rejection is auditable later, and so the
            # review queue can show what was thrown away rather than only a null.
            signal.extraction_json = {"raw": data.get("_raw", {}),
                                      "sections": data.get("_sections", {}),
                                      "rejected_numeric": rejected}
            if rejected:
                stats["flagged_signals"] += 1
            signal.named_people = data.get("named_people", [])
            signal.named_firms = data.get("named_firms", [])
            session.add(signal)

            doc.processed_at = utcnow()
            session.add(doc)
            session.commit()
            stats["extracted"] += 1
    return stats
