"""SAM.gov Division 23 spec-mention competitive intelligence -- see
app/pipeline/sam_gov.py's module docstring for why this is entirely
separate from RawDocument/Signal/Project. Mocks httpx (search + attachment
download) and the LLM extraction call; never touches a real network or a
real Anthropic call."""
import io

import httpx
import pytest
import respx
from docx import Document
from sqlmodel import select

import app.pipeline.sam_gov as sam_gov
from app.models import SamGovSearchCall, SamSolicitationCheck, SpecMention

SEARCH_URL = sam_gov.SEARCH_URL


def _notice(notice_id="n1", title="HVAC Replacement Bldg 500", state="CA",
           resource_links=None, description="", agency="NAVFAC SOUTHWEST"):
    return {
        "noticeId": notice_id,
        "title": title,
        "description": description,
        "solicitationNumber": f"SOL-{notice_id}",
        "fullParentPathName": f"DEPT OF DEFENSE.DEPT OF THE NAVY.NAVFAC.{agency}",
        "naicsCode": "236220",
        "postedDate": "2026-08-01",
        "placeOfPerformance": {"state": {"code": state}},
        "resourceLinks": resource_links or [],
        "uiLink": f"https://sam.gov/workspace/contract/opp/{notice_id}/view",
    }


def _search_response(notices):
    return httpx.Response(200, json={"opportunitiesData": notices, "totalRecords": len(notices)})


def _mock_all_org_and_state_queries(notices_by_call=None):
    """The real run fires 5 organization queries + 3 state/NAICS queries --
    tests that don't care about specific query wiring just need every one
    of them mocked so respx doesn't error on an unmocked request. Returns
    notices for every call unless notices_by_call overrides specific ones."""
    default = notices_by_call if notices_by_call is not None else []
    respx.get(SEARCH_URL).mock(return_value=_search_response(default))


@pytest.fixture()
def no_llm(monkeypatch):
    """Default: LLM extraction must not be called unless a test explicitly
    wires a response -- catches an accidental attachment/UFGS hit spending
    real budget-shaped calls in tests that shouldn't reach it."""
    def _boom(*a, **k):
        raise AssertionError("extract_division_23_mentions was called unexpectedly")
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", _boom)


def _docx_bytes(paragraphs: list[str]) -> bytes:
    doc = Document()
    for p in paragraphs:
        doc.add_paragraph(p)
    buf = io.BytesIO()
    doc.save(buf)
    return buf.getvalue()


# ---- find_ufgs_23_series_text -----------------------------------------------

def test_find_ufgs_23_series_extracts_from_combined_book():
    text = ("SECTION 22 05 00 - PLUMBING\nblah\n"
            "SECTION 23 64 26 - AIR-COOLED CONDENSING UNITS\nsection text here\n"
            "SECTION 26 05 00 - ELECTRICAL\nmore")
    section = sam_gov.find_ufgs_23_series_text(text)
    assert section.startswith("SECTION 23 64 26")
    assert "section text here" in section
    assert "SECTION 26 05 00" not in section


def test_find_ufgs_23_series_spans_multiple_consecutive_23_sections():
    text = "SECTION 23 05 00 - COMMON WORK\nfoo\nSECTION 23 74 00 - PACKAGED HVAC\nbar\nSECTION 26 05 00\nbaz"
    section = sam_gov.find_ufgs_23_series_text(text)
    assert "SECTION 23 05 00" in section
    assert "SECTION 23 74 00" in section
    assert "SECTION 26 05 00" not in section


def test_find_ufgs_23_series_runs_to_end_when_no_next_section():
    text = "SECTION 23 64 26 - AIR-COOLED CONDENSING UNITS\nonly section, nothing after"
    assert sam_gov.find_ufgs_23_series_text(text) == text


def test_find_ufgs_23_series_returns_none_when_absent():
    assert sam_gov.find_ufgs_23_series_text("SECTION 22 05 00 - PLUMBING only, no HVAC here") is None


def test_find_ufgs_23_series_handles_unspaced_section_numbers():
    text = "SECTION 236426 - AIR-COOLED CONDENSING UNITS\ncontent"
    section = sam_gov.find_ufgs_23_series_text(text)
    assert section is not None
    assert "content" in section


# ---- design-build ------------------------------------------------------------

@pytest.mark.parametrize("title,description,expected", [
    ("Design-Build HVAC Upgrade", "", True),
    ("Design/Build Mechanical Renovation", "", True),
    ("HVAC Replacement, Building 500", "a design-build task order under the MACC", True),
    ("HVAC Replacement, Building 500", "standard design-bid-build project", False),
    ("Chiller Plant Replacement", "", False),
])
def test_is_design_build_matches_keywords(title, description, expected):
    assert sam_gov.is_design_build(_notice(title=title, description=description)) is expected


# ---- attachment format dispatch ----------------------------------------------

def test_text_from_attachment_dispatches_pdf_docx_xlsx(monkeypatch):
    monkeypatch.setattr(sam_gov, "pdf_to_text", lambda data, max_pages=600: "pdf-text")
    monkeypatch.setattr(sam_gov, "docx_to_text", lambda data: "docx-text")
    monkeypatch.setattr(sam_gov, "xlsx_to_text", lambda data: "xlsx-text")
    assert sam_gov._text_from_attachment(b"x", "spec.PDF") == "pdf-text"
    assert sam_gov._text_from_attachment(b"x", "36C26126Q0841+0001.docx") == "docx-text"
    assert sam_gov._text_from_attachment(b"x", "pricing.xlsx") == "xlsx-text"


def test_text_from_attachment_raises_on_unsupported_extension():
    with pytest.raises(ValueError):
        sam_gov._text_from_attachment(b"x", "readme.txt")


def test_docx_to_text_reads_real_docx_bytes():
    data = _docx_bytes(["SECTION 23 64 26 - AIR-COOLED CONDENSING UNITS", "Basis of Design: AAON"])
    text = sam_gov.docx_to_text(data)
    assert "SECTION 23 64 26" in text
    assert "AAON" in text


# ---- full run ------------------------------------------------------------

@respx.mock
def test_design_build_notice_skipped_before_attachment_download(db_session, cfg, no_llm, monkeypatch):
    """The whole point of checking design-build first: never spend an
    attachment download (let alone LLM budget) on a notice that was never
    going to carry a spec book."""
    notice = _notice(title="Design-Build HVAC Upgrade", resource_links=["https://sam.gov/x/download"])
    respx.get(SEARCH_URL).mock(return_value=_search_response([notice]))

    def _boom(*a, **k):
        raise AssertionError("attachment download must not happen for a design-build notice")
    monkeypatch.setattr(sam_gov, "_download_attachment", _boom)
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["design_build_skipped"] == 1
    assert stats["spec_mentions_written"] == 0
    assert stats["searched"] == 8  # 5 organizations + 3 states
    checks = db_session.exec(select(SamSolicitationCheck)).all()
    assert len(checks) == 1
    assert checks[0].outcome == "design_build_skipped"
    assert db_session.exec(select(SpecMention)).all() == []


def test_no_resource_links_records_no_ufgs_23_series_found(db_session, cfg, no_llm, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    with respx.mock:
        respx.get(SEARCH_URL).mock(return_value=_search_response([_notice(resource_links=[])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["no_ufgs_23_series_found"] == 1
    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "no_ufgs_23_series_found"


def test_oversized_attachment_is_skipped_unparsed(db_session, cfg, no_llm, monkeypatch):
    """Regression test: a large drawing/exhibit PDF (architectural floor
    plans, CAD-exported, heavy embedded raster images) hung pdfplumber and
    ran this process's RSS from ~2.4GB to 3.2GB+ (confirmed real,
    2026-08-13) before being killed by hand. MAX_ATTACHMENT_BYTES must
    reject an oversized attachment BEFORE ever calling a parser, not after."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "MAX_ATTACHMENT_BYTES", 100)
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (b"x" * 1000, "huge_drawing_exhibit.pdf"))

    def _boom(data, filename):
        raise AssertionError("must not attempt to parse an oversized attachment")
    monkeypatch.setattr(sam_gov, "_text_from_attachment", _boom)

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/huge"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["fetch_failed"] == 1
    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "fetch_failed"
    assert "exceeds MAX_ATTACHMENT_BYTES" in check.detail


def test_full_run_writes_spec_mentions_and_matches_line_card(db_session, cfg, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (b"fake-bytes", "spec.pdf"))
    # Both manufacturer names must actually appear in this text -- the
    # extraction below is checked against it via name_grounded before
    # either SpecMention row is written (see test_sam_gov.py's grounding
    # tests below for the case where they don't).
    monkeypatch.setattr(sam_gov, "pdf_to_text",
                        lambda data, max_pages=600: (
                            "SECTION 23 74 00 -- PACKAGED OUTDOOR HVAC EQUIPMENT\n"
                            "Basis of Design: AAON. Or equal: Some Unrelated Vendor LLC."))
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", lambda text, **kw: {
        "specifying_firm": "Acme Engineering",
        "performance_spec_only": False,
        "sections": [{
            "spec_section": "23 74 00",
            "spec_section_title": "Packaged Outdoor HVAC Equipment",
            # AAON is a real line_card name in config.yaml -- exercises the match.
            "basis_of_design_manufacturer": "AAON",
            "or_equal_manufacturers": ["Some Unrelated Vendor LLC"],
        }],
    })

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/download"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["spec_mentions_found"] == 1
    assert stats["spec_mentions_written"] == 2
    assert stats["on_line_card"] == 1

    mentions = {m.manufacturer_name: m for m in db_session.exec(select(SpecMention)).all()}
    assert mentions["AAON"].mention_type == "basis_of_design"
    assert mentions["AAON"].on_dmg_line_card is True
    assert mentions["AAON"].dmg_line_card_name == "AAON"
    assert mentions["AAON"].specifying_firm == "Acme Engineering"
    assert mentions["AAON"].spec_section == "23 74 00"
    assert mentions["Some Unrelated Vendor LLC"].mention_type == "or_equal"
    assert mentions["Some Unrelated Vendor LLC"].on_dmg_line_card is False

    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "spec_mentions_found"


def test_performance_spec_only_is_a_distinct_outcome_not_a_miss(db_session, cfg, monkeypatch):
    """FAR 11.104/11.105 discourage brand names -- a section that specifies
    by performance with no manufacturer named must be recorded as its own
    outcome, distinguishable from finding nothing at all."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (b"fake-bytes", "spec.pdf"))
    monkeypatch.setattr(sam_gov, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00\nperformance language only")
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", lambda text, **kw: {
        "specifying_firm": "Acme Engineering",
        "performance_spec_only": True,
        "sections": [],
    })

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/download"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["performance_spec_only"] == 1
    assert stats["spec_mentions_found"] == 0
    assert stats["spec_mentions_written"] == 0
    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "performance_spec_only"
    assert "FAR 11.104" in check.detail
    assert db_session.exec(select(SpecMention)).all() == []


# ---- manufacturer-name grounding: the field the whole conclusion rests on --

def test_ungrounded_manufacturer_name_is_rejected_not_written(db_session, cfg, monkeypatch):
    """The bug: extract_division_23_mentions had no grounding check at all.
    A manufacturer name the model asserts but that never appears in the
    document text it was given must be dropped, not stored."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (b"fake-bytes", "spec.pdf"))
    monkeypatch.setattr(sam_gov, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00 -- PACKAGED OUTDOOR HVAC EQUIPMENT\n"
                                                    "This section specifies by performance only.")
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", lambda text, **kw: {
        "specifying_firm": None,
        "performance_spec_only": False,
        "sections": [{
            "spec_section": "23 74 00", "spec_section_title": "Packaged Outdoor HVAC Equipment",
            "basis_of_design_manufacturer": "Fabricated Manufacturer Inc",
            "or_equal_manufacturers": [],
        }],
    })

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/download"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["manufacturer_mentions_rejected_ungrounded"] == 1
    assert stats["spec_mentions_written"] == 0
    assert stats["spec_mentions_found"] == 0
    assert stats["ungrounded_mentions_only"] == 1
    assert db_session.exec(select(SpecMention)).all() == []

    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "ungrounded_mentions_only"
    assert "Fabricated Manufacturer Inc" in check.detail
    assert "basis_of_design" in check.detail


def test_grounded_and_ungrounded_mentions_in_the_same_notice_are_separated(db_session, cfg, monkeypatch):
    """A partial hallucination must not sink the real mention next to it,
    and must not let the fabricated one ride along either."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (b"fake-bytes", "spec.pdf"))
    monkeypatch.setattr(sam_gov, "pdf_to_text",
                        lambda data, max_pages=600: "SECTION 23 74 00\nBasis of Design: AAON.")
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", lambda text, **kw: {
        "specifying_firm": None,
        "performance_spec_only": False,
        "sections": [{
            "spec_section": "23 74 00", "spec_section_title": "Packaged Outdoor HVAC Equipment",
            "basis_of_design_manufacturer": "AAON",
            "or_equal_manufacturers": ["Invented Vendor That Is Not In The Text"],
        }],
    })

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/download"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["spec_mentions_written"] == 1
    assert stats["manufacturer_mentions_rejected_ungrounded"] == 1
    # A real, grounded mention was written -- the outcome is a genuine find,
    # not "ungrounded_mentions_only" (that outcome is only for a notice
    # where NOTHING survived grounding).
    assert stats["spec_mentions_found"] == 1
    mention = db_session.exec(select(SpecMention)).one()
    assert mention.manufacturer_name == "AAON"
    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.outcome == "spec_mentions_found"


def test_docx_attachment_is_parsed_end_to_end(db_session, cfg, monkeypatch):
    """The actual bug this pass fixes: a real .docx attachment, parsed for
    real (not mocked at the parser level), reaching a UFGS 23-series match."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    docx_bytes = _docx_bytes(["SECTION 23 64 26 - AIR-COOLED CONDENSING UNITS", "Basis of Design: AAON"])
    monkeypatch.setattr(sam_gov, "_download_attachment",
                        lambda url, api_key: (docx_bytes, "spec_23_64_26.docx"))
    monkeypatch.setattr(sam_gov, "extract_division_23_mentions", lambda text, **kw: {
        "specifying_firm": None,
        "performance_spec_only": False,
        "sections": [{
            "spec_section": "23 64 26", "spec_section_title": "Air-Cooled Condensing Units",
            "basis_of_design_manufacturer": "AAON", "or_equal_manufacturers": [],
        }],
    })

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(resource_links=["https://sam.gov/x/download"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["spec_mentions_found"] == 1
    assert stats["on_line_card"] == 1


def test_already_checked_notice_is_not_reprocessed(db_session, cfg, no_llm, monkeypatch):
    """Idempotency: a notice already resolved (skip or real attempt) must
    never spend another attachment download or LLM call on a re-run."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    db_session.add(SamSolicitationCheck(notice_id="n1", title="x", outcome="no_ufgs_23_series_found"))
    db_session.commit()

    def _boom(*a, **k):
        raise AssertionError("must not re-download an already-checked notice")
    monkeypatch.setattr(sam_gov, "_download_attachment", _boom)

    with respx.mock:
        respx.get(SEARCH_URL).mock(
            return_value=_search_response([_notice(notice_id="n1", resource_links=["https://sam.gov/x"])]))
        stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["already_checked"] == 1
    assert len(db_session.exec(select(SamSolicitationCheck)).all()) == 1


def test_org_query_notices_outside_states_are_filtered_out_in_python(db_session, cfg, no_llm, monkeypatch):
    """Organization queries aren't state-filtered at the API level (that
    would cost 3x the calls) -- filtering happens in Python against
    placeOfPerformance. A Guam notice returned by an (unrestricted)
    organization query must never reach notices_seen, even though the
    state/NAICS sweep calls are state-filtered by the API itself and
    return nothing here."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")

    def _fake_search(*, organization_name=None, state=None, **kwargs):
        if organization_name:
            return [_notice(notice_id="guam", state="GU")]
        return []  # state/NAICS sweep: API already filtered by state, nothing matches
    monkeypatch.setattr(sam_gov, "search_opportunities", _fake_search)

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["notices_seen"] == 0
    assert db_session.exec(select(SamSolicitationCheck)).all() == []


def test_no_api_key_disarms_source(db_session, cfg, monkeypatch, no_llm):
    monkeypatch.delenv("SAM_GOV_API_KEY", raising=False)
    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")
    assert stats["searched"] == 0
    assert db_session.exec(select(SamSolicitationCheck)).all() == []


def test_search_opportunities_sends_ptype_solicitation_and_presolicitation():
    with respx.mock:
        route = respx.get(SEARCH_URL).mock(return_value=_search_response([]))
        sam_gov.search_opportunities(api_key="k", posted_from="01/01/2026", posted_to="12/31/2026",
                                     organization_name="NAVFAC SOUTHWEST")
        assert route.called
        req = route.calls[0].request
        assert "organizationName=NAVFAC" in str(req.url)
        # ptype sent as a repeated/array param covering both o (solicitation)
        # and p (pre-solicitation).
        url_str = str(req.url)
        assert "ptype=o" in url_str
        assert "ptype=p" in url_str


def test_run_sam_gov_queries_every_organization_and_state(db_session, cfg, no_llm, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    seen_queries = []

    def _fake_search(**kwargs):
        seen_queries.append(kwargs)
        return []
    monkeypatch.setattr(sam_gov, "search_opportunities", _fake_search)

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["searched"] == 8
    org_queries = [q.get("organization_name") for q in seen_queries if q.get("organization_name")]
    assert set(org_queries) == set(sam_gov.ORGANIZATIONS)
    state_queries = [q.get("state") for q in seen_queries if q.get("ncode")]
    assert set(state_queries) == set(sam_gov.STATES)


# ---- rate-limit guard --------------------------------------------------------
# Real, not hypothetical: a live run on 2026-08-13 hit an actual SAM.gov 429
# after 12 search calls in one day. This module spends 8 calls per full run
# (5 organizations + 3 states), so config.yaml's real sam_gov.daily_call_budget
# (8) and sam_gov.scheduled_call_budget (4) are exercised directly below,
# not overridden -- these tests fail if either value drifts out of sync with
# ORGANIZATIONS/STATES without someone noticing.

def test_manual_run_completes_all_queries_within_the_manual_budget(db_session, cfg, no_llm, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "search_opportunities", lambda **kw: [])

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["searched"] == 8  # 5 orgs + 3 states, all fit inside daily_call_budget=8
    assert stats["search_budget_stopped_early"] is False


def test_scheduled_run_stops_at_the_smaller_scheduled_budget(db_session, cfg, no_llm, monkeypatch):
    """The whole point of --scheduled: it must NOT spend the full manual
    budget, even though nothing else has used any quota today."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "search_opportunities", lambda **kw: [])

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026",
                                is_scheduled=True)

    assert stats["searched"] == 4  # sam_gov.scheduled_call_budget, not the full 8
    assert stats["search_budget_stopped_early"] is True


def test_budget_is_shared_with_calls_already_logged_today(db_session, cfg, no_llm, monkeypatch):
    """The guard must see quota a DIFFERENT invocation already spent today --
    that is the entire reason SamGovSearchCall is a persisted log and not an
    in-process counter. Pre-seed 3 calls (as if a manual run happened
    earlier today), then a scheduled run (budget 4) should get only 1 more."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    for _ in range(3):
        db_session.add(SamGovSearchCall())
    db_session.commit()
    monkeypatch.setattr(sam_gov, "search_opportunities", lambda **kw: [])

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026",
                                is_scheduled=True)

    assert stats["searched"] == 1
    assert stats["search_budget_stopped_early"] is True


def test_budget_already_exhausted_makes_zero_new_calls(db_session, cfg, no_llm, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    for _ in range(8):
        db_session.add(SamGovSearchCall())
    db_session.commit()

    def _boom(**kw):
        raise AssertionError("must not call search_opportunities with the budget already spent")
    monkeypatch.setattr(sam_gov, "search_opportunities", _boom)

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert stats["searched"] == 0
    assert stats["search_budget_stopped_early"] is True


def test_each_successful_search_call_is_logged(db_session, cfg, no_llm, monkeypatch):
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    monkeypatch.setattr(sam_gov, "search_opportunities", lambda **kw: [])

    sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026", is_scheduled=True)

    assert len(db_session.exec(select(SamGovSearchCall)).all()) == 4


def test_a_failed_search_call_is_not_logged_against_the_budget(db_session, cfg, no_llm, monkeypatch):
    """A call that errored never reached SAM.gov successfully (or the error
    IS the rate limit) -- logging it as spent would double-punish a network
    hiccup. Only a call that actually returned results counts."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")

    def _fail(**kw):
        raise httpx.HTTPError("boom")
    monkeypatch.setattr(sam_gov, "search_opportunities", _fail)

    sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026")

    assert db_session.exec(select(SamGovSearchCall)).all() == []


def test_notices_found_before_budget_ran_out_are_still_processed(db_session, cfg, no_llm, monkeypatch):
    """Stopping early must not throw away real results the budget already
    paid for -- only the REMAINING, not-yet-made queries get skipped. A
    scheduled run's budget (4) covers the first 4 org queries and stops
    before any of the 3 state queries; a notice found by the very first
    org query must still be processed."""
    monkeypatch.setenv("SAM_GOV_API_KEY", "test-key")
    found_notice = _notice(notice_id="found-me", resource_links=[])

    def _fake_search(*, organization_name=None, **kw):
        if organization_name == sam_gov.ORGANIZATIONS[0]:
            return [found_notice]
        return []
    monkeypatch.setattr(sam_gov, "search_opportunities", _fake_search)

    stats = sam_gov.run_sam_gov(db_session, cfg, posted_from="01/01/2026", posted_to="12/31/2026",
                                is_scheduled=True)

    assert stats["searched"] == 4
    assert stats["search_budget_stopped_early"] is True
    assert stats["notices_seen"] == 1
    check = db_session.exec(select(SamSolicitationCheck)).one()
    assert check.notice_id == "found-me"
