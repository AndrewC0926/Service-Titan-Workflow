"""The grounding audit catches invented numbers, which is the failure that matters."""
import pytest
from app.grounding import (
    _variants,
    audit_signal,
    audit_text,
    audit_corpus,
    count_unit_grounded_occurrences,
    name_grounded,
    reject_ungrounded_names,
)
from app.models import Category, RawDocument, Signal, SignalType, Stage, TriageResult


_seq = iter(range(1, 10_000))


def _pair(db_session, text, **fields):
    n = next(_seq)
    doc = RawDocument(source="ceqanet", source_uid=f"u{n}", url=f"https://x/{n}",
                      title="T", raw_text=text, content_hash=f"h{n}",
                      triage_result=TriageResult.relevant)
    db_session.add(doc); db_session.commit()
    sig = Signal(raw_document_id=doc.id, signal_type=SignalType.ceqa_nop,
                 category=Category.data_center, stage=Stage.permitting, **fields)
    db_session.add(sig); db_session.commit()
    return sig, doc


def test_amperesand_style_fabrication_is_caught(db_session):
    """The regression this exists for: a stated MW with no basis in the document."""
    sig, doc = _pair(db_session, "A 73,000 sq ft solid-state transformer factory.",
                     mw_total=500.0, building_sqft=73000.0)
    out = {f.field: f for f in audit_signal(sig, doc)}
    assert out["mw_total"].grounded is False
    assert out["building_sqft"].grounded is True     # 73,000 is right there
    assert "73,000" in out["mw_total"].nearest


def test_vernon_style_grounded_value_passes(db_session):
    sig, doc = _pair(db_session,
                     "a 99 MW emergency only power system ... forty (40) diesel generators",
                     mw_total=99.0, generator_count=40)
    out = {f.field: f for f in audit_signal(sig, doc)}
    assert out["mw_total"].grounded is True
    assert out["generator_count"].grounded is True


def test_comma_and_million_spellings_count_as_grounded(db_session):
    sig, doc = _pair(db_session, "two buildings totaling 1,100,000 sq ft",
                     building_sqft=1100000.0)
    assert audit_signal(sig, doc)[0].grounded is True

    sig2, doc2 = _pair(db_session, "roughly 1.1 million square feet of space",
                       building_sqft=1100000.0)
    assert audit_signal(sig2, doc2)[0].grounded is True


def test_small_integers_are_flagged_weak_not_treated_as_evidence(db_session):
    """A '2' matches almost any document by chance, so a match proves nothing."""
    sig, doc = _pair(db_session, "Item 2 on the agenda for the 2026 session.",
                     building_count=2)
    f = audit_signal(sig, doc)[0]
    assert f.grounded is True and f.weak is True


def test_nulls_are_not_audited(db_session):
    """A missing value is acceptable; only assertions are audited."""
    sig, doc = _pair(db_session, "no numbers here at all")
    assert audit_signal(sig, doc) == []


def test_report_distinguishes_invention_from_misreading(db_session):
    _pair(db_session, "A 73,000 sq ft factory.", mw_total=500.0)
    text = audit_text(audit_corpus(db_session))
    assert "UNGROUNDED ASSERTIONS (1)" in text
    assert "mw_total = 500" in text


def test_clean_corpus_says_what_it_does_not_prove(db_session):
    _pair(db_session, "a 99 MW system", mw_total=99.0)
    text = audit_text(audit_corpus(db_session))
    assert "rules out invention, NOT misreading" in text


def test_variants_cover_how_filings_write_numbers():
    v = _variants(1100000.0)
    assert "1100000" in v and "1,100,000" in v and "1.1 million" in v


# --- the unit guard: rejection at extraction time -------------------------

from app.grounding import reject_ungrounded_numbers, unit_grounded  # noqa: E402

# Both fixtures are reductions of real documents that produced real bad numbers.
BLUE_OWL = (
    "Blue Owl Digital Infrastructure Trust 8-K. The Operating Partnership acquired "
    "interests valued at $160,775,472, with subsequent adjustments of $161,697,926 "
    "and a final determination of $164,946,388 as of the closing date. No power "
    "capacity is described in this filing."
)
FAAC = (
    "We currently occupy six adjacent facilities with a total of 26K sqft. under "
    "roof with 94K sqft. when you include out of doors inventory and fabrication "
    "areas. Our expansion will provide 109K sqft with modern manufacturing space."
)
VERNON = (
    "GIC Vernon LLC is applying for a Small Power Plant Exemption for the Vernon "
    "Backup Generating Facility, a 99 MW emergency only power system supporting the "
    "Goodman Energy Park data center. NOC Development Type: Power (-)(Megawatts 99). "
    "The project includes forty (40) diesel generators with thirty eight (38) 3 MW "
    "units dedicated to data center critical loads, and two 1 MW house generators. "
    "Location Total Acres: 11.55"
)


def test_blue_owl_dollar_figure_is_rejected_as_megawatts():
    """The live failure: 163.355 MW off dollar amounts in a filing containing no MW
    or megawatt token anywhere. It sized that project at 31,096-44,748 tons."""
    data, rej = reject_ungrounded_numbers({"mw_total": 163.355}, BLUE_OWL)
    assert data["mw_total"] is None, "the fabricated megawatt figure survived"
    assert len(rej) == 1 and rej[0]["field"] == "mw_total"
    assert "163.355" in rej[0]["reason"]
    assert any("160,775,472" in n for n in rej[0]["nearest"])


def test_a_dollar_amount_matching_the_digits_is_still_rejected():
    """Proximity, not mere presence: 161.697926 would 'appear' inside $161,697,926
    but has no unit beside it."""
    assert unit_grounded("mw_total", 161.0, BLUE_OWL) is False


def test_faac_square_footage_is_rejected():
    """The filing says 26K, 94K and 109K sqft. 84,800 is none of them."""
    data, rej = reject_ungrounded_numbers({"building_sqft": 84800.0}, FAAC)
    assert data["building_sqft"] is None
    assert rej[0]["field"] == "building_sqft"


def test_vernon_keeps_the_numbers_that_are_really_there():
    """The guard must not cost us the best row on the board. 99 MW appears twice,
    once as 'Megawatts 99' with the unit BEFORE the value."""
    data, rej = reject_ungrounded_numbers(
        {"mw_total": 99.0, "acres": 11.55}, VERNON)
    assert data["mw_total"] == 99.0
    assert data["acres"] == 11.55
    assert rej == []


def test_vernon_converted_generator_value_is_rejected():
    """The document says '3 MW'; extraction stored 3000 kW. Arithmetically right,
    but the prompt forbids unit conversion and 3000 is nowhere in the source, so it
    becomes a null and a flag rather than a number nobody can trace."""
    data, rej = reject_ungrounded_numbers({"generator_kw_each": 3000.0}, VERNON)
    assert data["generator_kw_each"] is None
    assert rej[0]["field"] == "generator_kw_each"


def test_vernon_critical_and_house_mw_ground_without_any_conversion():
    """The gap the previous test documents, closed: the document says '3 MW' and
    '1 MW' per unit, and generator_critical_mw_each/generator_house_mw_each hold
    those figures AS STATED — no hp/kW conversion, so nothing here is forced into
    a null the way generator_kw_each=3000 was."""
    data, rej = reject_ungrounded_numbers(
        {"generator_critical_mw_each": 3.0, "generator_house_mw_each": 1.0}, VERNON)
    assert data["generator_critical_mw_each"] == 3.0
    assert data["generator_house_mw_each"] == 1.0
    assert rej == []


def test_unit_before_or_after_the_value_both_count():
    assert unit_grounded("mw_total", 99.0, "a 99 MW emergency system") is True
    assert unit_grounded("mw_total", 99.0, "NOC Development Type: Megawatts 99") is True


def test_non_unit_fields_are_untouched():
    """generator_count and building_count carry no unit; the guard must not null
    them, since the plain grounding audit already reports on them. Same for the
    critical/house generator counts — only their per-unit MW figures are
    unit-checked."""
    data, rej = reject_ungrounded_numbers(
        {"generator_count": 40, "building_count": 2, "generator_critical_count": 38,
         "generator_house_count": 2}, VERNON)
    assert data["generator_count"] == 40 and data["building_count"] == 2
    assert data["generator_critical_count"] == 38 and data["generator_house_count"] == 2
    assert rej == []


def test_rejection_nulls_rather_than_downgrades():
    """A rejected field must be indistinguishable from 'not stated' downstream. The
    Amperesand pattern is that a bad number outranks a null everywhere."""
    data, _ = reject_ungrounded_numbers({"mw_total": 163.355}, BLUE_OWL)
    assert "mw_total" in data and data["mw_total"] is None


# --- wired into the pipeline ---------------------------------------------


def test_extract_nulls_a_fabricated_number_end_to_end(db_session, cfg, monkeypatch):
    """The guard has to fire inside run_extract, not just as a helper — that is the
    only point before the value becomes a project, a tonnage and a board row."""
    import app.pipeline.extract as ex
    from app.models import TriageResult
    from app.pipeline.extract import run_extract

    doc = RawDocument(source="edgar", source_uid="bo1", url="https://x/bo1",
                      title="8-K", raw_text=BLUE_OWL, content_hash="bo1",
                      triage_result=TriageResult.relevant,
                      meta={"triage_category": "data_center"})
    db_session.add(doc); db_session.commit()

    monkeypatch.setattr(ex, "extract", lambda *a, **k: {
        "project_name": "Blue Owl DC", "mw_total": 163.355, "county": "Loudoun",
        "state": "VA", "stage": "operating", "confidence": 0.9,
        "named_people": [], "named_firms": [], "_raw": {}, "_sections": {}})

    stats = run_extract(db_session, cfg, limit=5)
    assert stats["extracted"] == 1
    assert stats["rejected_numbers"] == 1 and stats["flagged_signals"] == 1

    from sqlmodel import select as sel
    sig = db_session.exec(sel(Signal).where(Signal.raw_document_id == doc.id)).one()
    assert sig.mw_total is None, "fabricated megawatt figure reached the signal"
    rej = sig.extraction_json["rejected_numeric"]
    assert rej and rej[0]["field"] == "mw_total" and rej[0]["value"] == 163.355


def test_extract_drops_a_fabricated_name_end_to_end(db_session, cfg, monkeypatch):
    """Same guarantee as the numeric guard above, for named_people/
    named_firms -- this stage had NO grounding check of any kind before
    2026-08-19 (raw model output went straight onto the signal)."""
    import app.pipeline.extract as ex
    from app.models import TriageResult
    from app.pipeline.extract import run_extract

    doc = RawDocument(source="goed", source_uid="g1", url="https://x/g1",
                      title="Board Packet", raw_text=REAL_PRODUCTION_TEXT, content_hash="g1",
                      triage_result=TriageResult.relevant,
                      meta={"triage_category": "industrial"})
    db_session.add(doc); db_session.commit()

    monkeypatch.setattr(ex, "extract", lambda *a, **k: {
        "project_name": "Some Facility", "county": "Storey", "state": "NV",
        "stage": "construction", "confidence": 0.9,
        "named_people": [{"name": "Amanda Berry-Jones"}, {"name": "Nobody Real"}],
        "named_firms": [], "_raw": {}, "_sections": {}})

    stats = run_extract(db_session, cfg, limit=5)
    assert stats["extracted"] == 1
    assert stats["rejected_names"] == 1 and stats["flagged_signals"] == 1

    from sqlmodel import select as sel2
    sig2 = db_session.exec(sel2(Signal).where(Signal.raw_document_id == doc.id)).one()
    assert [p["name"] for p in sig2.named_people] == ["Amanda Berry-Jones"]
    assert sig2.extraction_json["rejected_names"][0]["value"] == "Nobody Real"


def test_a_rejected_number_cannot_drive_sizing(db_session, cfg, monkeypatch):
    """The consequence that matters: 163.355 MW sized Blue Owl at 31,096-44,748
    tons. With the field nulled there is nothing left to size from."""
    import app.pipeline.extract as ex
    from app.models import TriageResult
    from app.pipeline.extract import run_extract
    from app.pipeline.resolve import run_resolve
    from app.pipeline.size_score import run_size_score
    from sqlmodel import select as sel

    doc = RawDocument(source="edgar", source_uid="bo2", url="https://x/bo2",
                      title="8-K", raw_text=BLUE_OWL, content_hash="bo2",
                      triage_result=TriageResult.relevant,
                      meta={"triage_category": "data_center"})
    db_session.add(doc); db_session.commit()
    monkeypatch.setattr(ex, "extract", lambda *a, **k: {
        "project_name": "Blue Owl DC", "mw_total": 163.355, "county": "Clark",
        "state": "NV", "stage": "operating", "confidence": 0.9,
        "named_people": [], "named_firms": [], "_raw": {}, "_sections": {}})
    run_extract(db_session, cfg, limit=5)
    run_resolve(db_session, cfg, use_llm=False)
    run_size_score(db_session, cfg)

    from app.models import Project
    proj = db_session.exec(sel(Project)).one()
    assert proj.tons_estimate_low is None and proj.tons_estimate_high is None


@pytest.mark.parametrize("text,expect,why", [
    ("Modular 8-12MW blocks; a 16MW deployment", True, "unit glued to the digits"),
    ("a 16 MW deployment", True, "unit spaced"),
    ("NOC Development Type: Power (-)(Megawatts 16)", True, "unit before the value"),
])
def test_glued_units_are_grounded(text, expect, why):
    """Filings write '16MW' constantly, and a leading \\b cannot match there — there
    is no boundary between '6' and 'M'. That bug rejected Colovore's stated 16 MW,
    the second row on the data center board."""
    assert unit_grounded("mw_total", 16.0, text) is expect, why


def test_unit_must_not_follow_a_letter():
    """'sf' lives inside 'transfer'. A unit that follows a letter is part of a word,
    not a unit."""
    assert unit_grounded("building_sqft", 5000.0, "the transfer of 5,000 units") is False
    assert unit_grounded("building_sqft", 5000.0, "5,000 SF of space") is True


def test_table_column_header_carries_the_unit():
    """GOED packets flatten to 'Building SqFt | ... | 91,000' with the unit in the
    column header, ~90 characters from the value."""
    row = ("Year Land Cost Building SqFt | Cost Purchase Amount | "
           "Year-1 | $6,627,000 $30,183,000 n/a n/a 91,000")
    assert unit_grounded("building_sqft", 91000.0, row) is True


@pytest.mark.parametrize("value,text,expect,why", [
    (3.0,  "Reactors QC Tanks 10 MW Substation $30,183,000", False, "3 inside 30,183,000"),
    (3.0,  "a 3 MW substation", True, "a real 3 MW"),
    (16.0, "a 163.355 MW figure", False, "16 inside 163.355"),
    (16.0, "a 16MW deployment", True, "real 16MW"),
])
def test_values_match_as_numbers_not_digit_runs(value, text, expect, why):
    """Substring search grounded '3' against the 3 in '$30,183,000', so every small
    value grounded trivially and the guard silently stopped guarding."""
    assert unit_grounded("mw_total", value, text) is expect, why


# --- name guard: named_people/named_firms had NO grounding at all before ----
# 2026-08-19: confirmed by reading app/pipeline/extract.py -- signal.named_people
# and signal.named_firms were set directly from the raw model output, with
# nothing between them and persistence. A retroactive scan of all 474
# production signals found zero confirmed pure inventions once real-world
# formatting variance was accounted for (every flagged name traced to a real
# mention: a parenthetical abbreviation, a credential suffix, a PDF line-wrap
# artifact) -- these tests encode exactly the variants that scan validated,
# so a genuinely invented name (no trace of it anywhere) is still caught.

REAL_PRODUCTION_TEXT = (
    "RDA EDAWN, Amanda Berry- infrastructure operators from a single Nevada "
    "operation. The company prioritizes recruiting\nJones from within the state "
    "across manufacturing. Agency Name\n\n\nKimley-Horn and Associates, In\n\n\n"
    "Job Title. Email: jhaughton@synergyconsultingca.com. Certificate holder: "
    "FRANCISCO V. AGUILAR, Secretary of State."
)


def test_name_grounded_true_for_an_exact_match():
    assert name_grounded("Amanda Berry-Jones", "The applicant is Amanda Berry-Jones, CFO.") is True


def test_name_grounded_true_for_a_parenthetical_abbreviation_either_direction():
    assert name_grounded("EDAWN", "Economic Development Authority of Western Nevada (EDAWN)") is True
    assert name_grounded("Economic Development Authority of Western Nevada (EDAWN)",
                         "supported by EDAWN") is True


def test_name_grounded_true_for_a_credential_suffix():
    assert name_grounded("Beth Chow, AICP", "signed by Beth Chow, AICP, Senior Planner") is True


def test_name_grounded_true_for_a_dropped_or_added_middle_initial():
    assert name_grounded("Peter Irby", "Contact: Peter P. Irby, Plant Manager") is True


def test_name_grounded_true_for_a_pdf_line_wrap_split_mid_word():
    """The real Type III production case: 'Amanda Berry-' then a literal
    newline then 'Jones' several words later, from a two-column PDF layout."""
    assert name_grounded("Amanda Berry-Jones", REAL_PRODUCTION_TEXT) is True


def test_name_grounded_true_for_a_field_truncated_at_column_width():
    """The real Heritage Valley production case: 'Kimley-Horn and Associates,
    In' cut off before 'c.' by a form field's own width."""
    assert name_grounded("Kimley-Horn and Associates, Inc.", REAL_PRODUCTION_TEXT) is True


def test_name_grounded_false_for_a_name_with_no_trace_at_all():
    assert name_grounded("Completely Fictional Person", REAL_PRODUCTION_TEXT) is False


def test_reject_ungrounded_names_drops_only_the_ungrounded_entry():
    data = {"named_people": [{"name": "Amanda Berry-Jones"}, {"name": "Nobody Real"}],
           "named_firms": [{"name": "EDAWN"}]}
    out, rejected = reject_ungrounded_names(data, REAL_PRODUCTION_TEXT)
    assert [p["name"] for p in out["named_people"]] == ["Amanda Berry-Jones"]
    assert out["named_firms"] == [{"name": "EDAWN"}]
    assert len(rejected) == 1
    assert rejected[0]["field"] == "named_people" and rejected[0]["value"] == "Nobody Real"


def test_reject_ungrounded_names_is_a_noop_when_everything_grounds():
    data = {"named_people": [{"name": "Amanda Berry-Jones"}], "named_firms": []}
    out, rejected = reject_ungrounded_names(data, REAL_PRODUCTION_TEXT)
    assert out["named_people"] == [{"name": "Amanda Berry-Jones"}]
    assert rejected == []


# --- audit_signal must agree with the write-time guard ----------------------
# 2026-08-19 fix: audit_signal() used to run its OWN, looser check (bare
# digit-boundary match, no unit-proximity requirement at all) instead of
# unit_grounded() -- so `scout grounding` could call a value "grounded" that
# reject_ungrounded_numbers() would have rejected at write time. Confirmed
# in production: Blue Owl's mw_total=163.355 and FAAC's building_sqft=84800
# are both unit-bearing fields the write-time guard rejects, but the OLD
# audit_signal would have reported them grounded (a bare "163" or "84800"
# never actually appears digit-for-digit in either fixture, but the point
# stands generally -- the two checks must not be able to disagree at all).


def test_audit_signal_agrees_with_the_write_time_guard_on_blue_owl(db_session):
    sig, doc = _pair(db_session, BLUE_OWL, mw_total=163.355)
    finding = audit_signal(sig, doc)[0]
    assert finding.grounded is unit_grounded("mw_total", 163.355, BLUE_OWL) is False


def test_audit_signal_agrees_with_the_write_time_guard_on_vernon(db_session):
    sig, doc = _pair(db_session, VERNON, mw_total=99.0)
    finding = audit_signal(sig, doc)[0]
    assert finding.grounded is unit_grounded("mw_total", 99.0, VERNON) is True


def test_audit_signal_flags_single_occurrence_unit_fields_as_weak(db_session):
    """One real mention is real evidence, not corroborated evidence -- see
    count_unit_grounded_occurrences' own docstring for why this is
    informational (weak=True) rather than a rejection: the real production
    case (Colovore Reno 1, mw_total=16.0) that motivated this is a real,
    single, unit-bearing mention that is nonetheless describing a DIFFERENT
    site's history, not this filing's own load -- something occurrence
    COUNTING alone cannot distinguish from a genuinely correct single
    mention (the Nimble 001 production case, mw_total=1.0, 'about 1 MW',
    which is correct). Both are single-occurrence and both are `weak`;
    neither is auto-rejected, because rejecting Nimble's real value to catch
    Colovore's wrong one would be a worse trade than leaving both flagged
    for a human to glance at."""
    sig, doc = _pair(db_session, "a 99 MW emergency system", mw_total=99.0)
    finding = audit_signal(sig, doc)[0]
    assert finding.grounded is True
    assert finding.weak is True
    assert count_unit_grounded_occurrences("mw_total", 99.0, "a 99 MW emergency system") == 1


def test_audit_signal_does_not_flag_multiply_corroborated_values_as_weak(db_session):
    text = "The facility draws 50 MW. Total site capacity: 50 MW as confirmed in Table 2."
    sig, doc = _pair(db_session, text, mw_total=50.0)
    finding = audit_signal(sig, doc)[0]
    assert finding.grounded is True
    assert finding.weak is False


# --- fix_corpus: retroactive correction of already-persisted rows ----------
# 2026-08-19: the real gap this closes. Blue Owl (mw_total=163.355) and FAAC
# (building_sqft=84800) -- both this module's OWN motivating examples --
# were STILL sitting on live signals, because the write-time guard only
# ever runs forward from when it was added, never backward over rows
# extracted before it existed.

from app.grounding import fix_corpus, fix_text  # noqa: E402


def test_fix_corpus_nulls_an_already_persisted_ungrounded_number(db_session):
    sig, doc = _pair(db_session, BLUE_OWL, mw_total=163.355)
    result = fix_corpus(db_session)
    assert result["n_signals_corrected"] == 1
    db_session.refresh(sig)
    assert sig.mw_total is None
    assert sig.extraction_json["rejected_numeric"][0]["field"] == "mw_total"


def test_fix_corpus_removes_an_already_persisted_ungrounded_name(db_session):
    sig, doc = _pair(db_session, REAL_PRODUCTION_TEXT,
                     named_people=[{"name": "Amanda Berry-Jones"}, {"name": "Nobody Real"}])
    result = fix_corpus(db_session)
    assert result["n_signals_corrected"] == 1
    db_session.refresh(sig)
    assert [p["name"] for p in sig.named_people] == ["Amanda Berry-Jones"]


def test_fix_corpus_leaves_a_clean_signal_untouched(db_session):
    sig, doc = _pair(db_session, VERNON, mw_total=99.0, acres=11.55)
    result = fix_corpus(db_session)
    assert result["n_signals_corrected"] == 0
    db_session.refresh(sig)
    assert sig.mw_total == 99.0 and sig.acres == 11.55


def test_fix_corpus_preserves_prior_rejections_rather_than_overwriting(db_session):
    """A signal that was ALREADY flagged at write time (a different field)
    must keep that record when a retroactive pass corrects a second one --
    the auditable history is additive, not last-write-wins."""
    sig, doc = _pair(db_session, BLUE_OWL, mw_total=163.355)
    sig.extraction_json = {"rejected_numeric": [{"field": "acres", "value": 5.0,
                                                 "reason": "prior write-time rejection"}],
                           "rejected_names": []}
    db_session.add(sig); db_session.commit()

    fix_corpus(db_session)
    db_session.refresh(sig)
    fields = {r["field"] for r in sig.extraction_json["rejected_numeric"]}
    assert fields == {"acres", "mw_total"}


def test_fix_text_reports_nothing_to_fix_on_a_clean_corpus(db_session):
    _pair(db_session, VERNON, mw_total=99.0)
    text = fix_text(fix_corpus(db_session))
    assert "Nothing to fix" in text


def test_fix_text_reports_what_was_corrected(db_session):
    _pair(db_session, BLUE_OWL, mw_total=163.355)
    text = fix_text(fix_corpus(db_session))
    assert "nulled mw_total=163.355" in text
