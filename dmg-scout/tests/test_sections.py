"""Section-aware chunking: the 200-page-EIR problem (Step 3)."""
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
