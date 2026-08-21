"""Project document ingestion -> equipment schedule extraction. A FIXED
CHAIN, same shape as app/pipeline/voice_capture.py: one PDF upload, one
text-layer read (cheap, no LLM), one LLM tool call, one grounding pass per
row, one DB write. See app/models.py's ProjectDocument/ScheduleEntry
docstrings for what gets stored and why.

    rep attaches PDF --(multipart POST)--> /project/{id}/documents (web/main.py)
        -> attach_document() (here)
            -> store ProjectDocument (raw bytes, so the schedule can be
               re-extracted or the original re-read without a re-upload)
            -> pdf_to_text() [pdfplumber, no LLM, done at upload time]
    rep clicks "Extract schedule" --(POST)--> /project/{id}/documents/{doc_id}/extract
        -> extract_schedule() (here)
            -> app.llm.extract_equipment_schedule() [Claude, one tool call,
               constrained decoding + Pydantic validation]
            -> app.grounding.ground_schedule_entry() per row [does this tag,
               capacity, and manufacturer actually appear in the document?]
            -> replace every ScheduleEntry for this document (idempotent
               re-extraction, same replace-not-append idiom as Signal's
               one-per-raw_document)

Never loses the document on a partial failure: the PDF and its raw text are
committed BEFORE any LLM call, so an extraction failure (LLM error, no text
layer, budget exhausted) leaves the document on file with extraction_error
set, not silently gone. A row that fails grounding is still stored, flagged
needs_review with review_reason -- never dropped, never silently trusted.
"""
from __future__ import annotations

import logging

from sqlmodel import Session, delete

from app.config import Config
from app.grounding import ground_schedule_entry, merge_duplicate_tag_entries
from app.models import ProjectDocument, ScheduleEntry, utcnow
from app.pdftext import DEFAULT_MAX_PAGES, pdf_page_count, pdf_to_text

log = logging.getLogger(__name__)

# Postgres bytea, not Render's local disk -- see ProjectDocument's docstring
# for why. 20MB comfortably covers a spec section or the mechanical sheets
# excerpt of a drawing set; a full multi-hundred-sheet architectural set
# would need object storage this project doesn't have wired up yet, so it's
# rejected with a clear reason rather than silently truncated.
MAX_UPLOAD_BYTES = 20 * 1024 * 1024


class UploadRejected(Exception):
    pass


class ExtractionFailed(Exception):
    pass


def attach_document(session: Session, *, project_id: int, data: bytes, filename: str,
                    content_type: str | None, doc_type: str = "other",
                    uploaded_by: str | None = None) -> ProjectDocument:
    """Store the PDF and read its text layer immediately -- extraction
    itself (the LLM call) is a separate, explicit step, so attaching a
    document never by itself costs an LLM call or blocks on one."""
    if not data:
        raise UploadRejected("empty upload")
    if len(data) > MAX_UPLOAD_BYTES:
        raise UploadRejected(f"{len(data)} bytes exceeds the {MAX_UPLOAD_BYTES}-byte limit")
    if not filename.lower().endswith(".pdf") and content_type != "application/pdf":
        raise UploadRejected(f"only PDF is supported right now (got {content_type!r})")

    doc = ProjectDocument(
        project_id=project_id, filename=filename,
        content_type=content_type or "application/pdf",
        data=data, size_bytes=len(data), doc_type=doc_type, uploaded_by=uploaded_by,
    )
    try:
        doc.page_count = pdf_page_count(data)
        # DEFAULT_MAX_PAGES (80), same cap GOED board packets use -- see
        # that module's own docstring on why a hard page cap exists at all.
        # A schedule buried past page 80 of a drawing set will not be seen;
        # untested against anything that large so far.
        doc.raw_text = pdf_to_text(data, max_pages=DEFAULT_MAX_PAGES)
        if not doc.raw_text.strip():
            doc.extraction_error = ("no text layer found -- this PDF may be a scanned image "
                                    "with no OCR, or pages beyond the readable limit")
    except Exception as exc:  # noqa: BLE001 -- a corrupt/unreadable PDF is still worth keeping on file
        doc.extraction_error = f"could not read PDF: {type(exc).__name__}: {exc}"
        log.warning("attach_document: project %s, %s: %s", project_id, filename, doc.extraction_error)
    session.add(doc)
    session.commit()
    session.refresh(doc)
    return doc


def extract_schedule(session: Session, cfg: Config, doc: ProjectDocument) -> dict:
    """Run LLM extraction + per-row grounding against doc.raw_text.
    Idempotent: replaces every existing ScheduleEntry for this document
    rather than appending, so re-running after a spec revision (or just to
    pick up a prompt/grounding improvement) never duplicates rows.

    Returns {entries, needs_review} on success. Raises ExtractionFailed if
    there is nothing to extract from (no text layer) or the LLM call itself
    failed -- the caller decides how to surface that; the document itself is
    always left in a consistent state either way (extraction_error set,
    extracted_at stamped)."""
    from app.llm import extract_equipment_schedule
    from app.spend import check_budget

    if not doc.raw_text or not doc.raw_text.strip():
        doc.extraction_error = doc.extraction_error or "no text layer to extract from"
        doc.extracted_at = utcnow()
        session.add(doc)
        session.commit()
        raise ExtractionFailed(doc.extraction_error)

    check_budget("schedule_extraction")
    try:
        extraction = extract_equipment_schedule(doc.raw_text, title=doc.filename)
    except Exception as exc:  # noqa: BLE001 -- the document and its text stay on file either way
        doc.extraction_error = f"{type(exc).__name__}: {exc}"
        doc.extracted_at = utcnow()
        session.add(doc)
        session.commit()
        log.warning("extract_schedule: document %s failed: %s", doc.id, doc.extraction_error)
        raise ExtractionFailed(doc.extraction_error) from exc

    session.exec(delete(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id))

    raw_entries = [e.model_dump(mode="json") for e in extraction.entries]
    merged_entries, merge_log = merge_duplicate_tag_entries(raw_entries)
    if merge_log:
        log.info("extract_schedule: document %s merge_log: %s", doc.id, merge_log)

    n_review = 0
    for raw in merged_entries:
        merge_conflict = raw.pop("_merge_conflict", None)
        grounded, reasons = ground_schedule_entry(raw, doc.raw_text)
        if merge_conflict:
            reasons = [*reasons, merge_conflict]
        if reasons:
            n_review += 1
        session.add(ScheduleEntry(
            project_document_id=doc.id, project_id=doc.project_id,
            tag=grounded["tag"], equipment_type=grounded.get("equipment_type"),
            role=grounded.get("role"),
            capacity_value=grounded.get("capacity_value"), capacity_unit=grounded.get("capacity_unit"),
            capacity_btuh=grounded.get("capacity_btuh"),
            capacity_corroborated=grounded.get("capacity_corroborated"),
            airflow_cfm=grounded.get("airflow_cfm"),
            basis_of_design_manufacturer=grounded.get("basis_of_design_manufacturer"),
            approved_equals=grounded.get("approved_equals") or [],
            source_quote=grounded.get("source_quote", ""), source_page=grounded.get("source_page"),
            confidence=grounded.get("confidence") or 0.0,
            needs_review=bool(reasons),
            review_reason="; ".join(reasons) if reasons else None,
            extraction_json=grounded,
        ))

    doc.extraction_error = None
    doc.extracted_at = utcnow()
    session.add(doc)
    session.commit()
    return {"entries": len(merged_entries), "needs_review": n_review}
