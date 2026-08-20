"""Project document upload -> equipment schedule extraction -> grounding.
See app/pipeline/schedule.py, app/grounding.py's schedule-entry functions,
and app/models.py's ProjectDocument/ScheduleEntry docstrings.

The real-document validation for this feature (2026-08-20, against a public
rooftop-unit schedule and a Division 23 spec section) is what caught and
fixed three real bugs pinned here: a truncated tool call silently returning
{} instead of raising, a punctuation mismatch ("MANUFACTURER -- TRANE" vs a
model's "MANUFACTURER TRANE") failing fragment grounding that should have
passed, and a transposed-table quote needing softer, more accurate review
language than "not found at all"."""
from typing import ClassVar

from sqlmodel import select

from app.grounding import ground_schedule_entry, quote_grounded
from app.llm import TruncatedToolCall
from app.models import Category, Project, ProjectDocument, ScheduleEntry
from app.pipeline import schedule as sched
from app.schemas import EquipmentScheduleExtraction, ScheduleEntryExtraction

# Not a real PDF -- app.pdftext.pdf_to_text/pdf_page_count are mocked below,
# same pattern as tests/test_sam_gov.py, so these bytes only need to be
# non-empty and stored/round-tripped correctly.
_MINI_PDF = b"%PDF-1.4 not a real pdf, only used as opaque bytes in these tests"
_SCHEDULE_TEXT = "AHU-1 4 TONS MANUFACTURER TRANE MODEL X100"


def _mock_pdf_reading(monkeypatch, text=_SCHEDULE_TEXT, pages=1):
    monkeypatch.setattr(sched, "pdf_to_text", lambda data, max_pages=80: text)
    monkeypatch.setattr(sched, "pdf_page_count", lambda data: pages)


def _project(db_session) -> Project:
    p = Project(name="Test Project", category=Category.industrial)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


# ---- attach_document ----------------------------------------------------

def test_attach_document_stores_bytes_and_reads_text(db_session, monkeypatch):
    _mock_pdf_reading(monkeypatch)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")
    assert doc.id is not None
    assert doc.project_id == p.id
    assert doc.data == _MINI_PDF
    assert doc.size_bytes == len(_MINI_PDF)
    assert "AHU-1" in doc.raw_text
    assert doc.extraction_error is None


def test_attach_document_rejects_empty_upload(db_session):
    p = _project(db_session)
    try:
        sched.attach_document(db_session, project_id=p.id, data=b"",
                              filename="x.pdf", content_type="application/pdf")
        assert False, "should have raised"
    except sched.UploadRejected as exc:
        assert "empty" in str(exc)


def test_attach_document_rejects_oversized_upload(db_session, monkeypatch):
    monkeypatch.setattr(sched, "MAX_UPLOAD_BYTES", 10)
    p = _project(db_session)
    try:
        sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                              filename="x.pdf", content_type="application/pdf")
        assert False, "should have raised"
    except sched.UploadRejected as exc:
        assert "exceeds" in str(exc)


def test_attach_document_rejects_non_pdf(db_session):
    p = _project(db_session)
    try:
        sched.attach_document(db_session, project_id=p.id, data=b"not a pdf",
                              filename="x.docx",
                              content_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document")
        assert False, "should have raised"
    except sched.UploadRejected as exc:
        assert "PDF" in str(exc)


def test_attach_document_survives_corrupt_pdf(db_session, monkeypatch):
    """A corrupt/unreadable PDF is still worth keeping on file -- the
    document row is stored with extraction_error set, not lost."""
    def boom(data, max_pages=80):
        raise ValueError("not a PDF")
    monkeypatch.setattr(sched, "pdf_page_count", lambda data: (_ for _ in ()).throw(ValueError("not a PDF")))
    monkeypatch.setattr(sched, "pdf_to_text", boom)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=b"%PDF-not-really-a-pdf",
                                filename="corrupt.pdf", content_type="application/pdf")
    assert doc.id is not None
    assert doc.extraction_error


# ---- extract_schedule ----------------------------------------------------

def _entry(**kw) -> ScheduleEntryExtraction:
    base = {"tag": "AHU-1", "equipment_type": "Air Handling Unit", "capacity_value": 4.0,
           "capacity_unit": "TONS", "airflow_cfm": None, "basis_of_design_manufacturer": "TRANE",
           "approved_equals": [], "source_quote": "AHU-1 4 TONS MANUFACTURER TRANE",
           "source_page": 1, "confidence": 0.95}
    base.update(kw)
    return ScheduleEntryExtraction(**base)


def test_extract_schedule_happy_path(db_session, cfg, monkeypatch):
    _mock_pdf_reading(monkeypatch)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")
    monkeypatch.setattr("app.llm.extract_equipment_schedule",
                        lambda text, title="": EquipmentScheduleExtraction(entries=[_entry()]))
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)

    stats = sched.extract_schedule(db_session, cfg, doc)

    assert stats == {"entries": 1, "needs_review": 0}
    rows = db_session.exec(select(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tag == "AHU-1" and row.capacity_value == 4.0 and row.capacity_unit == "TONS"
    assert row.basis_of_design_manufacturer == "TRANE"
    assert row.needs_review is False and row.review_reason is None
    assert row.project_id == p.id
    db_session.refresh(doc)
    assert doc.extracted_at is not None and doc.extraction_error is None


def test_extract_schedule_nulls_ungrounded_manufacturer_and_flags_review(db_session, cfg, monkeypatch):
    """A manufacturer the document doesn't actually name must never reach a
    rep as if it were read off the page -- reject, don't downgrade, same
    discipline as reject_ungrounded_numbers/names for Signal."""
    _mock_pdf_reading(monkeypatch)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")
    monkeypatch.setattr(
        "app.llm.extract_equipment_schedule",
        lambda text, title="": EquipmentScheduleExtraction(entries=[
            _entry(basis_of_design_manufacturer="CARRIER",  # not in the document text
                  approved_equals=["YORK", "TRANE"])         # YORK not in text, TRANE is
        ]))
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)

    stats = sched.extract_schedule(db_session, cfg, doc)

    assert stats["needs_review"] == 1
    row = db_session.exec(select(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id)).one()
    assert row.basis_of_design_manufacturer is None  # nulled, not trusted
    assert row.approved_equals == ["TRANE"]           # YORK dropped, TRANE kept
    assert row.needs_review is True
    assert "CARRIER" in row.review_reason and "YORK" in row.review_reason


def test_extract_schedule_is_idempotent_replace_not_append(db_session, cfg, monkeypatch):
    _mock_pdf_reading(monkeypatch)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")
    monkeypatch.setattr("app.llm.extract_equipment_schedule",
                        lambda text, title="": EquipmentScheduleExtraction(entries=[_entry()]))
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)

    sched.extract_schedule(db_session, cfg, doc)
    sched.extract_schedule(db_session, cfg, doc)

    rows = db_session.exec(select(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id)).all()
    assert len(rows) == 1  # re-extraction replaced, did not duplicate


def test_extract_schedule_raises_on_no_text_layer(db_session, cfg):
    p = _project(db_session)
    doc = ProjectDocument(project_id=p.id, filename="scan.pdf", data=b"x", raw_text="")
    db_session.add(doc)
    db_session.commit()
    db_session.refresh(doc)

    try:
        sched.extract_schedule(db_session, cfg, doc)
        assert False, "should have raised"
    except sched.ExtractionFailed:
        pass
    db_session.refresh(doc)
    assert doc.extraction_error


def test_extract_schedule_survives_llm_failure_without_losing_document(db_session, cfg, monkeypatch):
    _mock_pdf_reading(monkeypatch)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")

    def boom(text, title=""):
        raise RuntimeError("LLM is down")
    monkeypatch.setattr("app.llm.extract_equipment_schedule", boom)
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)

    try:
        sched.extract_schedule(db_session, cfg, doc)
        assert False, "should have raised"
    except sched.ExtractionFailed as exc:
        assert "LLM is down" in str(exc)
    db_session.refresh(doc)
    assert doc.data == _MINI_PDF  # the document itself is never lost
    assert doc.extraction_error and "LLM is down" in doc.extraction_error


# ---- grounding: quote_grounded / ground_schedule_entry --------------------

def test_quote_grounded_contiguous_match():
    text = "Some text. AHU-1 supplies 4 TONS via TRANE model X100. More text."
    check = quote_grounded("AHU-1 supplies 4 TONS via TRANE", text)
    assert check.problem is None
    assert check.assembled is False


def test_quote_grounded_tolerates_punctuation_column_separators():
    """The real bug: a flattened schedule table uses '--' as a column
    separator ('MANUFACTURER -- TRANE'); a model that copies it as
    'MANUFACTURER TRANE' is not fabricating, it's dropping formatting."""
    text = "MARK RTU-01\nMANUFACTURER -- TRANE\nMODEL -- X100"
    check = quote_grounded("MANUFACTURER TRANE", text)
    assert check.problem is None


def test_quote_grounded_multi_span_is_grounded_not_flagged():
    """The fix: a tag proven by its own column header plus a capacity and a
    manufacturer each proven by their own row label IS genuinely grounded,
    just not contiguous -- it must NOT block the row. 'assembled' still
    records that it took several fragments, for display only."""
    # A real transposed schedule: RTU-01's own fields are not adjacent to
    # each other -- RTU-02/RTU-03's values sit between them on each row.
    text = ("MARK RTU-01 RTU-02 RTU-03\n"
           "NOMINAL TONS 4 7.5 3\n"
           "MANUFACTURER -- TRANE CARRIER YORK")
    check = quote_grounded("RTU-01 ... NOMINAL TONS 4 ... MANUFACTURER TRANE", text)
    assert check.problem is None
    assert check.assembled is True


def test_quote_grounded_fabricated_fragment_still_fails():
    """Keep the discipline: a fragment that genuinely isn't in the document
    anywhere is still an unconditional failure, multi-span or not."""
    text = "MARK RTU-01\nNOMINAL TONS 4\nMANUFACTURER -- TRANE"
    check = quote_grounded("RTU-01 ... MANUFACTURER TOTALLY-INVENTED-BRAND-XYZ", text)
    assert check.problem is not None
    assert "not found in the document" in check.problem


def test_ground_schedule_entry_multi_span_clears_review_but_marks_assembled():
    text = ("MARK RTU-01 RTU-02\n"
           "NOMINAL TONS 4 7.5\n"
           "MANUFACTURER -- TRANE CARRIER")
    raw = _entry(tag="RTU-01", capacity_value=4.0, capacity_unit="TONS",
                basis_of_design_manufacturer="TRANE",
                source_quote="RTU-01 ... NOMINAL TONS 4 ... MANUFACTURER TRANE").model_dump(mode="json")
    grounded, reasons = ground_schedule_entry(raw, text)
    assert reasons == []               # must NOT block the row
    assert grounded["quote_assembled"] is True


def test_ground_schedule_entry_flags_ungrounded_capacity():
    text = "AHU-1 MANUFACTURER TRANE"  # no capacity number anywhere
    raw = _entry(capacity_value=99.0).model_dump(mode="json")
    grounded, reasons = ground_schedule_entry(raw, text)
    assert any("capacity_value" in r for r in reasons)
    assert grounded["capacity_value"] == 99.0  # reported, not nulled -- a human decides


def test_ground_schedule_entry_flags_low_confidence():
    text = "AHU-1 4 TONS MANUFACTURER TRANE"
    raw = _entry(confidence=0.2, source_quote="AHU-1 4 TONS MANUFACTURER TRANE").model_dump(mode="json")
    _grounded, reasons = ground_schedule_entry(raw, text)
    assert any("confidence" in r for r in reasons)


def test_ground_schedule_entry_clean_row_has_no_reasons():
    text = "AHU-1 4 TONS MANUFACTURER TRANE MODEL X100"
    raw = _entry(source_quote="AHU-1 4 TONS MANUFACTURER TRANE").model_dump(mode="json")
    grounded, reasons = ground_schedule_entry(raw, text)
    assert reasons == []
    assert grounded["basis_of_design_manufacturer"] == "TRANE"


# ---- TruncatedToolCall -----------------------------------------------------

def test_tool_call_raises_on_max_tokens_instead_of_returning_empty(monkeypatch):
    """Confirmed production case: a truncated tool call comes back as an
    empty dict, which looked identical to 'the model found nothing' until
    stop_reason was checked directly. Must raise, not silently succeed."""
    from app import llm as llm_mod

    class FakeUsage:
        input_tokens = 100
        output_tokens = 16000

    class FakeResp:
        stop_reason = "max_tokens"
        usage = FakeUsage()
        content: ClassVar[list] = []

    class FakeMessages:
        def create(self, **kw):
            return FakeResp()

    class FakeClient:
        messages = FakeMessages()

    monkeypatch.setattr(llm_mod, "_client", lambda: FakeClient())
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)
    monkeypatch.setattr("app.spend.record", lambda *a, **kw: 0.01)

    try:
        llm_mod._tool_call("fake-model", "system", {"name": "t", "input_schema": {}}, "content")
        assert False, "should have raised TruncatedToolCall"
    except TruncatedToolCall as exc:
        assert "max_tokens" in str(exc) or "16000" in str(exc)
