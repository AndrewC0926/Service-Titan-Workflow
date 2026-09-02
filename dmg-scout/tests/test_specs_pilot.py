"""Code-path tests for app.pipeline.specs_pilot ONLY -- control flow,
grounding rejection, outcome classification, line-card matching. These
mock every LLM call with minimal hand-written snippets, exactly the way
tests/test_sam_gov.py mocks its own LLM calls, and say NOTHING about what
real California school district specs actually contain.

docs/specs-pilot/ is empty by design -- no synthetic/fixture documents were
created to test this module against a claimed real-world corpus (an
invented fixture proves nothing about real spec language and has produced
a false-confidence report here before). Do not add a test here that treats
its own snippet as evidence about real specs, and do not derive a
percentage/rate from anything in this file.
"""
from pathlib import Path

import pytest

import app.pipeline.specs_pilot as specs_pilot
from app.pipeline.specs_pilot import (
    OutcomeClass,
    _read_document_text,
    process_document,
    report_text,
    run_specs_pilot,
)


def _metadata(district=None, project_name=None, bid_number=None, bid_date=None):
    return {"district": district, "project_name": project_name,
           "bid_number": bid_number, "bid_date": bid_date}


def _division23(performance_spec_only=False, sections=None, specifying_firm=None):
    return {"specifying_firm": specifying_firm, "performance_spec_only": performance_spec_only,
           "sections": sections or []}


# ---- _read_document_text: dispatch on extension ----------------------------

def test_read_document_text_dispatches_pdf(tmp_path, monkeypatch):
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: "pdf text")
    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake-pdf-bytes")
    assert _read_document_text(p) == "pdf text"


def test_read_document_text_dispatches_docx(tmp_path, monkeypatch):
    monkeypatch.setattr(specs_pilot, "docx_to_text", lambda data: "docx text")
    p = tmp_path / "spec.docx"
    p.write_bytes(b"fake-docx-bytes")
    assert _read_document_text(p) == "docx text"


def test_read_document_text_dispatches_xlsx(tmp_path, monkeypatch):
    monkeypatch.setattr(specs_pilot, "xlsx_to_text", lambda data: "xlsx text")
    p = tmp_path / "spec.xlsx"
    p.write_bytes(b"fake-xlsx-bytes")
    assert _read_document_text(p) == "xlsx text"


def test_read_document_text_raises_on_unsupported_extension(tmp_path):
    p = tmp_path / "spec.txt"
    p.write_bytes(b"whatever")
    with pytest.raises(ValueError, match="unsupported file type"):
        _read_document_text(p)


# ---- process_document: parse failures --------------------------------------

def test_empty_text_layer_is_parse_failed(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: "")
    p = tmp_path / "scanned.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.PARSE_FAILED
    assert "no text layer" in result.parse_error


def test_unreadable_file_is_parse_failed_with_reason(tmp_path, monkeypatch, cfg):
    def _boom(data, max_pages=600):
        raise ValueError("corrupt PDF structure")
    monkeypatch.setattr(specs_pilot, "pdf_to_text", _boom)
    p = tmp_path / "corrupt.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.PARSE_FAILED
    assert "corrupt PDF structure" in result.parse_error


def test_metadata_extraction_failure_is_parse_failed(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(specs_pilot, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00\nsome content")
    monkeypatch.setattr("app.llm.extract_school_bid_metadata",
                        lambda text, **kw: (_ for _ in ()).throw(RuntimeError("LLM 500")))
    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.PARSE_FAILED
    assert "metadata extraction failed" in result.parse_error


def test_division23_extraction_failure_is_parse_failed_but_keeps_metadata(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(specs_pilot, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00\nsome content")
    monkeypatch.setattr("app.llm.extract_school_bid_metadata",
                        lambda text, **kw: _metadata(district="Fontana USD"))
    monkeypatch.setattr("app.llm.extract_division_23_mentions",
                        lambda text, **kw: (_ for _ in ()).throw(RuntimeError("LLM 500")))
    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.PARSE_FAILED
    assert "Division 23 extraction failed" in result.parse_error
    assert result.district == "Fontana USD"  # metadata call succeeded before the failure


# ---- process_document: no Division 23 content ------------------------------

def test_no_division_23_heading_skips_the_llm_call_entirely(tmp_path, monkeypatch, cfg):
    """No LLM budget spent on Division 23 extraction when there is nothing
    to extract from -- same discipline as app.pipeline.sam_gov."""
    monkeypatch.setattr(specs_pilot, "pdf_to_text",
                        lambda data, max_pages=600: "General conditions. No mechanical scope here.")
    monkeypatch.setattr("app.llm.extract_school_bid_metadata",
                        lambda text, **kw: _metadata(district="Fontana USD"))
    def _boom(*a, **kw):
        raise AssertionError("extract_division_23_mentions must not be called")
    monkeypatch.setattr("app.llm.extract_division_23_mentions", _boom)

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.NO_DIVISION_23
    assert result.district == "Fontana USD"
    assert result.mentions == []


# ---- process_document: performance-spec-only --------------------------------

def test_performance_spec_only_with_no_manufacturer(tmp_path, monkeypatch, cfg):
    monkeypatch.setattr(specs_pilot, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00\nPerformance requirements only.")
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda text, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions",
                        lambda text, **kw: _division23(performance_spec_only=True))
    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.outcome == OutcomeClass.PERFORMANCE_SPEC_ONLY
    assert result.mentions == []


# ---- process_document: grounding -- the field the conclusion rests on ------

def test_grounded_manufacturer_is_kept_and_matched_to_line_card(tmp_path, monkeypatch, cfg):
    text = "SECTION 23 74 00 -- PACKAGED ROOFTOP UNITS\nBasis of Design: AAON."
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: text)
    monkeypatch.setattr("app.llm.extract_school_bid_metadata",
                        lambda t, **kw: _metadata(district="Fontana USD", bid_number="24-25-07",
                                                  bid_date="March 3, 2026"))
    monkeypatch.setattr("app.llm.extract_division_23_mentions", lambda t, **kw: _division23(
        sections=[{"spec_section": "23 74 00", "spec_section_title": "Packaged Rooftop Units",
                   "basis_of_design_manufacturer": "AAON", "or_equal_manufacturers": []}]))

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)

    assert result.outcome == OutcomeClass.NAMES_MANUFACTURERS
    assert result.district == "Fontana USD"
    assert result.bid_number == "24-25-07"
    assert result.bid_date == "March 3, 2026"  # verbatim, not reparsed
    assert result.sections_present == ["23 74 00"]
    assert len(result.mentions) == 1
    m = result.mentions[0]
    assert m.manufacturer_name == "AAON"
    assert m.mention_type == "basis_of_design"
    assert m.on_dmg_line_card is True
    assert m.dmg_line_card_name == "AAON"


def test_ungrounded_manufacturer_is_rejected_not_kept(tmp_path, monkeypatch, cfg):
    """The exact bug found and fixed in app.pipeline.sam_gov, guarded
    against here from the start: a manufacturer name the model asserts but
    that never appears in the document text must be dropped."""
    text = "SECTION 23 74 00 -- PACKAGED ROOFTOP UNITS\nPerformance and capacity requirements follow."
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: text)
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda t, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions", lambda t, **kw: _division23(
        sections=[{"spec_section": "23 74 00", "spec_section_title": "Packaged Rooftop Units",
                   "basis_of_design_manufacturer": "Fabricated Manufacturer Inc",
                   "or_equal_manufacturers": []}]))

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)

    assert result.outcome == OutcomeClass.UNGROUNDED_MENTIONS_ONLY
    assert result.mentions == []
    assert len(result.rejected_ungrounded) == 1
    assert "Fabricated Manufacturer Inc" in result.rejected_ungrounded[0]


def test_grounded_and_ungrounded_mentions_are_separated(tmp_path, monkeypatch, cfg):
    text = "SECTION 23 74 00\nBasis of Design: AAON."
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: text)
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda t, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions", lambda t, **kw: _division23(
        sections=[{"spec_section": "23 74 00", "spec_section_title": "Packaged Rooftop Units",
                   "basis_of_design_manufacturer": "AAON",
                   "or_equal_manufacturers": ["Invented Vendor Not In Text"]}]))

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)

    # A real, grounded mention survives -- the outcome is a genuine find,
    # not UNGROUNDED_MENTIONS_ONLY (that is only for a document where
    # NOTHING survives grounding).
    assert result.outcome == OutcomeClass.NAMES_MANUFACTURERS
    assert [m.manufacturer_name for m in result.mentions] == ["AAON"]
    assert len(result.rejected_ungrounded) == 1
    assert "Invented Vendor Not In Text" in result.rejected_ungrounded[0]


def test_manufacturer_not_on_line_card_is_kept_but_flagged(tmp_path, monkeypatch, cfg):
    text = "SECTION 23 74 00\nOr equal: Some Unrelated Vendor LLC."
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: text)
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda t, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions", lambda t, **kw: _division23(
        sections=[{"spec_section": "23 74 00", "spec_section_title": "Packaged Rooftop Units",
                   "basis_of_design_manufacturer": None,
                   "or_equal_manufacturers": ["Some Unrelated Vendor LLC"]}]))

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)

    assert result.outcome == OutcomeClass.NAMES_MANUFACTURERS
    m = result.mentions[0]
    assert m.manufacturer_name == "Some Unrelated Vendor LLC"
    assert m.on_dmg_line_card is False
    assert m.dmg_line_card_name is None


def test_line_card_match_is_exact_not_fuzzy(tmp_path, monkeypatch, cfg):
    """'AAON Coil Products' must NOT match the line card's 'AAON' -- exact
    normalize_name match only, same discipline as every other join this
    session built. A near-miss stays off the card, it does not fuzz onto it."""
    text = "SECTION 23 74 00\nBasis of Design: AAON Coil Products."
    monkeypatch.setattr(specs_pilot, "pdf_to_text", lambda data, max_pages=600: text)
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda t, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions", lambda t, **kw: _division23(
        sections=[{"spec_section": "23 74 00", "spec_section_title": "Packaged Rooftop Units",
                   "basis_of_design_manufacturer": "AAON Coil Products",
                   "or_equal_manufacturers": []}]))

    p = tmp_path / "spec.pdf"
    p.write_bytes(b"fake")
    result = process_document(p, cfg)
    assert result.mentions[0].on_dmg_line_card is False


# ---- run_specs_pilot: directory handling -----------------------------------

def test_nonexistent_directory_is_reported_not_silently_empty():
    result = run_specs_pilot("docs/this-directory-does-not-exist", None)
    assert result["directory_exists"] is False
    assert result["documents"] == []
    assert "does not exist" in report_text(result)


def test_empty_directory_produces_zero_documents_no_llm_spend(tmp_path, cfg):
    result = run_specs_pilot(str(tmp_path), cfg)
    assert result["directory_exists"] is True
    assert result["documents"] == []
    text = report_text(result)
    assert "0 document(s)" in text
    assert "untested against real spec language" in text


def test_unsupported_file_types_are_ignored_not_errored(tmp_path, cfg):
    (tmp_path / "readme.txt").write_text("not a spec")
    (tmp_path / ".DS_Store").write_bytes(b"\x00")
    result = run_specs_pilot(str(tmp_path), cfg)
    assert result["documents"] == []


def test_run_aggregates_multiple_documents_by_outcome(tmp_path, monkeypatch, cfg):
    (tmp_path / "a.pdf").write_bytes(b"fake")
    (tmp_path / "b.pdf").write_bytes(b"fake")
    (tmp_path / "c.pdf").write_bytes(b"fake")

    texts = {
        "a.pdf": "SECTION 23 74 00\nBasis of Design: AAON.",
        "b.pdf": "SECTION 23 74 00\nPerformance only, no brand named.",
        "c.pdf": "General conditions. No mechanical scope.",
    }
    divisions = {
        "a.pdf": _division23(sections=[{"spec_section": "23 74 00", "spec_section_title": "RTUs",
                                       "basis_of_design_manufacturer": "AAON", "or_equal_manufacturers": []}]),
        "b.pdf": _division23(performance_spec_only=True),
    }

    # Dispatch per-file by monkeypatching the read step directly, keyed by
    # filename, rather than trying to key pdf_to_text off its opaque bytes arg.
    monkeypatch.setattr(specs_pilot, "_read_document_text",
                        lambda path: texts[path.name])
    monkeypatch.setattr("app.llm.extract_school_bid_metadata", lambda t, **kw: _metadata())
    monkeypatch.setattr("app.llm.extract_division_23_mentions",
                        lambda t, filename=None, title=None, **kw: divisions[
                            {"SECTION 23 74 00\nBasis of Design: AAON.": "a.pdf",
                             "SECTION 23 74 00\nPerformance only, no brand named.": "b.pdf"}[t]])

    result = run_specs_pilot(str(tmp_path), cfg)
    assert len(result["documents"]) == 3
    assert result["by_outcome"][OutcomeClass.NAMES_MANUFACTURERS] == 1
    assert result["by_outcome"][OutcomeClass.PERFORMANCE_SPEC_ONLY] == 1
    assert result["by_outcome"][OutcomeClass.NO_DIVISION_23] == 1

    text = report_text(result)
    assert "AAON" in text
    assert "below 30" in text  # n=3, corpus-size caveat must fire


# ---- report_text: corpus-size discipline -----------------------------------

def test_report_never_states_a_percentage_below_the_floor():
    docs = [specs_pilot.DocumentResult(filename=f"f{i}.pdf", outcome=OutcomeClass.NAMES_MANUFACTURERS)
           for i in range(8)]
    result = {"directory": "docs/specs-pilot", "directory_exists": True, "documents": docs,
             "by_outcome": {OutcomeClass.NAMES_MANUFACTURERS: 8, OutcomeClass.PERFORMANCE_SPEC_ONLY: 0,
                           OutcomeClass.NO_DIVISION_23: 0, OutcomeClass.PARSE_FAILED: 0,
                           OutcomeClass.UNGROUNDED_MENTIONS_ONLY: 0}}
    text = report_text(result)
    assert "%" not in text.split("Outcome counts")[1].split("Distinct manufacturer")[0]
    assert "no percentage is reported" in text


def test_report_states_a_lean_between_30_and_100():
    docs = [specs_pilot.DocumentResult(filename=f"f{i}.pdf", outcome=OutcomeClass.NAMES_MANUFACTURERS)
           for i in range(40)]
    result = {"directory": "docs/specs-pilot", "directory_exists": True, "documents": docs,
             "by_outcome": {OutcomeClass.NAMES_MANUFACTURERS: 40, OutcomeClass.PERFORMANCE_SPEC_ONLY: 0,
                           OutcomeClass.NO_DIVISION_23: 0, OutcomeClass.PARSE_FAILED: 0,
                           OutcomeClass.UNGROUNDED_MENTIONS_ONLY: 0}}
    text = report_text(result)
    assert "100% named a manufacturer (n=40)" in text
    assert "not yet defensible" in text or "Treat this as a lean" in text


def test_report_states_a_full_percentage_at_or_above_100():
    docs = [specs_pilot.DocumentResult(filename=f"f{i}.pdf", outcome=OutcomeClass.NAMES_MANUFACTURERS)
           for i in range(100)]
    result = {"directory": "docs/specs-pilot", "directory_exists": True, "documents": docs,
             "by_outcome": {OutcomeClass.NAMES_MANUFACTURERS: 100, OutcomeClass.PERFORMANCE_SPEC_ONLY: 0,
                           OutcomeClass.NO_DIVISION_23: 0, OutcomeClass.PARSE_FAILED: 0,
                           OutcomeClass.UNGROUNDED_MENTIONS_ONLY: 0}}
    text = report_text(result)
    assert "100% named a manufacturer (n=100)." in text
    assert "lean" not in text.lower()
