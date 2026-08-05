"""Section-aware chunking: the 200-page-EIR problem (Step 3)."""
import re

from app.models import RawDocument
from app.pipeline.backfill import estimate_cost
from app.sections import select_relevant_text


def make_eir(pages: int = 200) -> str:
    """Synthetic Draft EIR: generator/MW facts buried mid-document, page ~120."""
    filler_section = ("4.4 BIOLOGICAL RESOURCES\n" + ("The project site supports "
                      "disturbed habitat. " * 200) + "\n\n")
    doc = [
        "DRAFT ENVIRONMENTAL IMPACT REPORT\nMeridian Data Center Campus\n"
        "Lead Agency: City of Ontario\nSCH No. 2026070456\n\n"
        "TABLE OF CONTENTS\n"
        "3.0 Project Description ....... 12\n"
        "4.3 Air Quality ....... 118\n"
        "4.9 Utilities and Service Systems ....... 152\n\n",
    ]
    doc.append(filler_section * 12)   # front filler
    doc.append(
        "3.0 PROJECT DESCRIPTION\n"
        "The proposed project comprises two data center buildings totaling "
        "1,100,000 square feet on 88 acres.\n\n")
    doc.append(filler_section * 25)   # ~pages 20-115
    doc.append(
        "4.3 AIR QUALITY\n"
        "Operation would include 52 diesel backup generators rated at 3,000 hp "
        "(2,250 kW) each, permitted through SCAQMD.\n\n")
    doc.append(filler_section * 8)
    doc.append(
        "4.9 UTILITIES AND SERVICE SYSTEMS\n"
        "The project requires 176 megawatts of electrical capacity from SCE via "
        "a new on-site substation.\n\n")
    doc.append(filler_section * 10)
    text = "".join(doc)
    assert len(text) > 400_000  # genuinely long
    return text


def test_short_doc_goes_whole(cfg):
    sel = select_relevant_text("Short NOP text. 40 MW data center.", cfg)
    assert not sel.chunked
    assert sel.sections_found == ["full_document"]


def test_eir_keeps_the_sections_where_mw_lives(cfg):
    sel = select_relevant_text(make_eir(), cfg)
    assert sel.chunked
    assert sel.selected_chars <= cfg.get("llm.extract_max_chars")
    # The facts a head-truncation would have destroyed:
    assert "52 diesel backup generators" in sel.text
    assert "176 megawatts" in sel.text
    assert "1,100,000 square feet" in sel.text
    for name in ("project_description", "air_quality", "utilities_energy"):
        assert name in sel.sections_found, sel.sections_found
    # And the filler did not crowd them out
    assert sel.text.count("disturbed habitat") < 3000


def test_toc_hits_do_not_shadow_body_sections(cfg):
    sel = select_relevant_text(make_eir(), cfg)
    # if the TOC line had been captured instead of the body, the generator
    # sentence (body-only) would be missing
    assert "3,000 hp" in sel.text


def test_no_sections_matched_falls_back_loudly(cfg):
    text = ("no headings here just prose " * 5000)
    sel = select_relevant_text(text, cfg)
    assert sel.chunked
    assert "tail_fallback" in sel.sections_found


def test_estimate_differentiates_eir_from_nop(db_session, cfg):
    """Step 3 acceptance: EIRs must NOT come back at NOP token counts."""
    db_session.add(RawDocument(source="ceqanet", source_uid="nop1", url="u1", title="NOP",
                               content_hash="h1", raw_text="Short NOP. 40 MW. " * 100,
                               meta={"document_type": "NOP"}))
    db_session.add(RawDocument(source="ceqanet", source_uid="eir1", url="u2", title="EIR",
                               content_hash="h2", raw_text=make_eir(),
                               meta={"document_type": "EIR"}))
    db_session.commit()
    est = estimate_cost(db_session, cfg)
    by = est["by_document_type"]
    nop, eir = by["ceqanet:NOP"], by["ceqanet:EIR"]
    assert eir["avg_raw_chars"] > 50 * nop["avg_raw_chars"]
    # EIR sends more to Sonnet than an NOP, but is capped by chunking:
    assert eir["avg_chars_sent_to_sonnet"] > nop["avg_chars_sent_to_sonnet"]
    assert eir["avg_chars_sent_to_sonnet"] <= cfg.get("llm.extract_max_chars")
    # and the raw 400k-char document was NOT sent whole
    assert eir["avg_chars_sent_to_sonnet"] < eir["avg_raw_chars"] / 5


# --- what triage reads ----------------------------------------------------


def test_triage_reads_short_documents_whole(cfg):
    from app.sections import select_triage_text
    text = "a Storey County special use permit for a 200,000 sqft facility " * 50
    sel = select_triage_text(text, cfg)
    assert sel.chunked is False
    assert sel.text == text
    assert sel.sections_found == ["full_document"]


def test_triage_sampling_reaches_the_last_character(cfg):
    """The regression: an 82,724-char Storey County packet was classified on its
    first 7%. A late agenda item must not be structurally unreachable."""
    from app.sections import select_triage_text
    text = ("filler " * 12_000) + "NEEDLE_AT_THE_VERY_END"
    assert len(text) > cfg.get("llm.triage_max_chars", 60000)
    sel = select_triage_text(text, cfg)
    assert sel.chunked is True
    assert "NEEDLE_AT_THE_VERY_END" in sel.text
    assert "DOCUMENT TAIL" in sel.text


def test_triage_coverage_is_reported_not_assumed(cfg):
    """A fixed budget cannot cover an arbitrarily long document — a 400,000-char
    prospectus gets ~15% however it is sliced. That limit is real, so it must be
    reported per document: a negative verdict on 15% is a weaker claim than one on
    the whole filing, and the row has to carry the difference."""
    from app.sections import select_triage_text
    max_chars = cfg.get("llm.triage_max_chars", 60000)

    whole = select_triage_text("x" * (max_chars // 2), cfg)
    assert whole.coverage == 1.0 and whole.chunked is False

    partial = select_triage_text("x" * (max_chars * 8), cfg)
    assert partial.chunked is True
    assert 0.10 < partial.coverage < 0.20
    # Coverage must fall as documents grow, never silently claim completeness.
    bigger = select_triage_text("x" * (max_chars * 16), cfg)
    assert bigger.coverage < partial.coverage


def test_triage_records_coverage_on_the_document(db_session, cfg, monkeypatch):
    """The auditable half: a document dropped on a partial read is queryable
    afterwards instead of having to be rediscovered by hand."""
    import app.pipeline.triage as tri
    from app.models import RawDocument, TriageResult
    from app.pipeline.triage import run_triage

    long_text = "Item 4. Approval of minutes. " * 6000
    doc = RawDocument(source="civicplus", source_uid="cov1", url="https://x/1",
                      title="packet", raw_text=long_text, content_hash="cov1",
                      triage_result=TriageResult.pending)
    db_session.add(doc)
    db_session.commit()
    monkeypatch.setattr(tri, "triage", lambda *a, **k: {
        "category": "other", "names_location": False, "reason": "routine",
        "_coverage": 0.37, "_original_chars": len(long_text)})
    stats = run_triage(db_session, cfg, limit=5)
    db_session.refresh(doc)
    assert doc.meta["triage_coverage"] == 0.37
    assert stats["irrelevant_partial_read"] == 1


def test_triage_sampling_respects_the_budget(cfg):
    from app.sections import select_triage_text
    max_chars = cfg.get("llm.triage_max_chars", 60000)
    sel = select_triage_text("y" * (max_chars * 8), cfg)
    content = sel.selected_chars - sel.text.count("=====") * 60  # minus markers
    assert content <= max_chars * 1.1


def test_triage_selection_is_not_the_extraction_heuristic(cfg):
    """Extraction's section matcher finds no headings in a county agenda packet and
    falls back to head+tail, leaving the middle invisible — that is the failure being
    fixed, and triage must not inherit it.

    Asserted on where the excerpts come FROM, not on finding a planted needle: at a
    fixed budget the sampler leaves sub-window gaps, so a needle test would pass or
    fail on where it happened to land.
    """
    from app.sections import select_relevant_text, select_triage_text
    packet = "Item 4. Approval of minutes. " * 4000        # ~116k chars, no headings

    extract_sel = select_relevant_text(packet, cfg)
    assert "tail_fallback" in extract_sel.sections_found   # the old blind spot

    triage_sel = select_triage_text(packet, cfg)
    pcts = [int(m) for m in re.findall(r"EXCERPT at (\d+)% of document", triage_sel.text)]
    assert pcts, "triage sent no spanning excerpts"
    # Excerpts must come from across the whole document, including past halfway.
    assert min(pcts) < 25 and max(pcts) > 75
    assert any(40 <= p <= 60 for p in pcts), "nothing sampled from the middle"


def test_extraction_spends_its_budget_on_a_headingless_packet(cfg):
    """The regression: heading matching found nothing in a county agenda packet, so
    extraction sent 8,716 chars of a 65,291-char document against a 60,000 budget —
    13%, while triage was reading 95% of the same file and correctly reporting a data
    center campus in it. Unused budget is a dropped project."""
    from app.sections import select_relevant_text
    max_chars = cfg.get("llm.extract_max_chars", 60000)
    packet = "Item 4. Approval of minutes. " * 3000        # ~87k chars, no headings

    sel = select_relevant_text(packet, cfg)
    assert "spanning_topup" in sel.sections_found
    assert sel.selected_chars > max_chars * 0.8, (
        f"only spent {sel.selected_chars} of {max_chars}")
    pcts = [int(m) for m in re.findall(r"EXCERPT at (\d+)% of document", sel.text)]
    assert pcts and max(pcts) > 60, "top-up did not reach the back of the document"


def test_topup_does_not_displace_matched_sections(cfg):
    """A real EIR must keep its targeted sections; the top-up is additive only."""
    from app.sections import select_relevant_text
    filler = "boilerplate text. " * 2000
    doc = ("HEAD MATTER\n" + filler
           + "\n3.2 PROJECT DESCRIPTION\nThe project is a data center. " + filler
           + "\n4.5 UTILITIES AND ENERGY\nDemand is 96 MW. " + filler
           + "\n5.1 AIR QUALITY\nTwelve 3000 hp generators. " + filler)
    sel = select_relevant_text(doc, cfg)
    for name in ("project_description", "utilities_energy", "air_quality"):
        assert name in sel.sections_found
    assert "96 MW" in sel.text and "3000 hp" in sel.text
