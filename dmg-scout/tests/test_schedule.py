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

from app.grounding import canonicalize_tag, ground_schedule_entry, merge_duplicate_tag_entries, quote_grounded
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


def test_extract_schedule_merges_duplicate_tag_before_storing(db_session, cfg, monkeypatch):
    """A tag extracted twice (schedule row + spec sheet, same physical
    unit, no field conflict) must store as ONE ScheduleEntry with the
    union of both sources' fields -- not two rows, and the returned
    "entries" count must reflect the merge, not the raw extraction count."""
    text = "CU1 8 TONS MANUFACTURER TRANE MODEL X100 -- CU1 approved equal YORK"
    _mock_pdf_reading(monkeypatch, text=text)
    p = _project(db_session)
    doc = sched.attach_document(db_session, project_id=p.id, data=_MINI_PDF,
                                filename="sheet.pdf", content_type="application/pdf")
    monkeypatch.setattr("app.llm.extract_equipment_schedule", lambda text, title="":
                        EquipmentScheduleExtraction(entries=[
                            _entry(tag="CU1", capacity_value=8.0, source_quote="CU1 ... 8 TONS ... MANUFACTURER TRANE",
                                  confidence=0.9),
                            _entry(tag="CU 1", capacity_value=None, capacity_unit=None,
                                  approved_equals=["YORK"],
                                  source_quote="CU1 approved equal YORK", confidence=0.8),
                        ]))
    monkeypatch.setattr("app.spend.check_budget", lambda stage=None: None)

    stats = sched.extract_schedule(db_session, cfg, doc)

    assert stats["entries"] == 1
    rows = db_session.exec(select(ScheduleEntry).where(ScheduleEntry.project_document_id == doc.id)).all()
    assert len(rows) == 1
    row = rows[0]
    assert row.tag == "CU1"
    assert row.capacity_value == 8.0
    assert row.approved_equals == ["YORK"]
    assert row.needs_review is False  # clean merge, nothing to flag


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


# ---- ground truth (no LLM) -------------------------------------------------

def test_real_document_ground_truth_tag_count():
    """Seven LLM extraction runs against tests/fixtures/rtu_schedule.pdf
    measured 62, 59, 58, 61, 61, 58, 61 tags -- but none of those was ever
    checked against the document itself, only against each other. This
    parses the PDF's ruling-line table structure directly (no LLM) to get
    the real count: 59.

    Breakdown, read from the table structure by hand and confirmed by
    app.pdftext.count_equipment_tags_deterministic:
      - BLDG-2 schedule (page 1): 20 MARK-row tags (RTU-01..16, RTU-A..D)
      - BLDG-3 East Half (page 2): 16 MARK-row tags (RTU-01-A..16-A)
      - BLDG-3 West Half (page 3): 19 MARK-row tags (RTU-01..17, CU1, CU2)
      - Page 4 spec sheets add 4 tags with no MARK-row entry anywhere
        (AH1, AH2, AH3, CU3); CU1 and CU2 also get their own spec-sheet
        block on page 4 but are NOT counted twice, since they already
        appear in the page-3 MARK row as the same physical units.
      55 MARK-row tags + 4 spec-sheet-only tags = 59.

    This is the fixed denominator for all future variance measurements
    against this document -- compare an extraction run's tag count to 59,
    not to a previous run's tag count.
    """
    from pathlib import Path

    from app.pdftext import count_equipment_tags_deterministic

    data = (Path(__file__).parent / "fixtures" / "rtu_schedule.pdf").read_bytes()
    assert count_equipment_tags_deterministic(data) == 59


# ---- canonicalize_tag -------------------------------------------------------

def test_canonicalize_tag_collapses_real_observed_variants():
    """Five temperature=0 extraction runs against rtu_schedule.pdf (2026-08-21)
    produced identical tag counts (62) in every run, and identical tag SETS
    in 4 of 5 -- run 4 alone spelled one row "NEW UNIT-CU-3" where the other
    four spelled it "CU-3" (both read off the same page-4 footnote-derived
    unit). These are the actual variant strings observed, not hypotheticals."""
    assert canonicalize_tag("CU 1") == canonicalize_tag("CU1") == "CU-1"
    assert (canonicalize_tag("CU-3") == canonicalize_tag("CU3")
            == canonicalize_tag("NEW UNIT-CU-3") == "CU-3")
    assert canonicalize_tag("RTU-01") == canonicalize_tag("RTU-1") == "RTU-1"
    assert canonicalize_tag("RTU-01-A") == "RTU-1-A"


def test_canonicalize_tag_leaves_unrecognized_shapes_unchanged():
    """Bounded scope -- a tag that isn't [prefix][number][optional letter]
    passes through upper-cased and stripped rather than being forced into a
    shape it doesn't have."""
    assert canonicalize_tag("HR TRAIN ROOM") == "HR TRAIN ROOM"
    assert canonicalize_tag("  ch-1  ") == "CH-1"


# ---- merge_duplicate_tag_entries -------------------------------------------
# All three fixtures below are the real entries a temperature=0 extraction
# run against tests/fixtures/rtu_schedule.pdf actually produced (2026-08-21),
# trimmed to the fields that matter for each assertion -- not hypotheticals.

def _sched_entry(**kw):
    base = {"equipment_type": None, "role": None, "capacity_value": None,
           "capacity_unit": None, "capacity_btuh": None, "airflow_cfm": None,
           "basis_of_design_manufacturer": None, "approved_equals": [],
           "source_quote": "", "source_page": None, "confidence": 0.8}
    base.update(kw)
    return base


def test_merge_combines_schedule_row_and_spec_sheet_for_the_same_unit():
    """CU1 extracts once from the schedule table (capacity, manufacturer)
    and once from its own spec sheet (approved_equals) -- no field conflict,
    so these merge into one row with the union of both sources' facts."""
    entries = [
        _sched_entry(tag="CU1", equipment_type="Condensing Unit", capacity_value=8.0,
                    capacity_unit="NOMINAL TON", basis_of_design_manufacturer="LIEBERT",
                    confidence=0.88, source_page=3),
        _sched_entry(tag="CU 1", equipment_type="Condensing Unit",
                    basis_of_design_manufacturer="LIEBERT",
                    approved_equals=["Emerson/Liebert"], confidence=0.82, source_page=3),
    ]
    merged, log = merge_duplicate_tag_entries(entries)
    assert len(merged) == 1
    row = merged[0]
    assert row["tag"] == "CU1"                        # higher-confidence source's literal tag
    assert row["capacity_value"] == 8.0                # from the schedule row
    assert row["approved_equals"] == ["Emerson/Liebert"]  # from the spec sheet
    assert "_merge_conflict" not in row
    assert log[0]["merged"] is True


def test_merge_does_not_conflate_same_tag_reused_across_buildings():
    """This document's real RTU-01 names two different units -- 4-ton Trane
    in BLDG-2 (page 1), 6-ton Carrier in BLDG-3 West Half (page 3) -- tag
    numbering resets per building. Same literal tag string, real conflict:
    both rows are kept, unmerged, and NOT flagged for review, since nothing
    about either row is actually wrong."""
    entries = [
        _sched_entry(tag="RTU-01", capacity_value=4.0, basis_of_design_manufacturer="TRANE",
                    confidence=0.92, source_page=1),
        _sched_entry(tag="RTU-01", capacity_value=6.0, basis_of_design_manufacturer="CARRIER",
                    confidence=0.85, source_page=3),
    ]
    merged, log = merge_duplicate_tag_entries(entries)
    assert len(merged) == 2
    assert {e["capacity_value"] for e in merged} == {4.0, 6.0}
    assert all("_merge_conflict" not in e for e in merged)
    assert log[0]["merged"] is False


def test_merge_flags_conflict_when_canonicalization_connects_different_data():
    """"CU-3" (mislabeled "NEW UNIT-CU-3" annotation, actually reads the
    UNRELATED RTU-08 replacement spec block -- 4-ton packaged rooftop unit)
    and "CU3" (the real Liebert condensing-unit spec sheet) canonicalize to
    the same tag but disagree on equipment_type entirely. Different literal
    tag strings connected only by canonicalize_tag's normalization, with a
    real conflict, IS suspicious -- flagged for review rather than merged
    or silently dropped."""
    entries = [
        _sched_entry(tag="CU-3", equipment_type="Packaged Rooftop Unit", capacity_value=4.0,
                    airflow_cfm=1280.0, confidence=0.88, source_page=3),
        _sched_entry(tag="CU3", equipment_type="Condensing Unit",
                    basis_of_design_manufacturer="LIEBERT",
                    approved_equals=["Emerson/Liebert"], confidence=0.82, source_page=3),
    ]
    merged, log = merge_duplicate_tag_entries(entries)
    assert len(merged) == 2
    assert all(e.get("_merge_conflict") for e in merged)
    assert log[0]["merged"] is False
    assert log[0]["conflicts"][0]["flagged"] is True


def test_merge_real_document_extraction_reduces_62_to_59():
    """End-to-end check against the actual 62-entry output of a real
    temperature=0 extraction run against tests/fixtures/rtu_schedule.pdf
    (2026-08-21, checked into fixtures/rtu_schedule_extraction_2026-08-21.json
    verbatim) -- must merge down to 59, matching the deterministic ground
    truth (test_real_document_ground_truth_tag_count). Exactly one
    canonical-tag group (CU-3/CU3) should stay unmerged AND flagged; the 16
    RTU-01..16 cross-building pairs must stay unmerged and NOT flagged."""
    import json
    from pathlib import Path

    entries = json.loads(
        (Path(__file__).parent / "fixtures" / "rtu_schedule_extraction_2026-08-21.json").read_text())
    assert len(entries) == 62

    merged, log = merge_duplicate_tag_entries(entries)
    assert len(merged) == 59

    flagged = [e for e in merged if e.get("_merge_conflict")]
    assert len(flagged) == 2  # both sides of the one genuine CU-3/CU3 conflict
    assert {e["tag"] for e in flagged} == {"CU-3", "CU3"}

    true_merges = [item for item in log if item["merged"]]
    assert {item["canonical_tag"] for item in true_merges} == {"RTU-7", "CU-1", "CU-2"}
