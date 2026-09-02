"""AB 869 seismic compliance plan roster -- parser, crosstab reader, and
import. The fixture (docs/hcai/ab869/10049.pdf) is a real, hand-supplied
export Andrew produced from the live Tableau view -- not a synthetic PDF
built for this test suite (see app.pipeline.ab869's module docstring for
why: this PDF's text layer has a real, confirmed rendering defect that a
hand-built fixture could never reproduce faithfully).
"""
from pathlib import Path

import pytest
from sqlmodel import select

from app.models import Ab869Building, Ab869Milestone, Ab869Plan, HospitalBuilding, SourceRun
from app.pipeline.ab869 import (
    _is_garbled_word,
    import_ab869,
    parse_pdf,
    read_crosstab,
)

FIXTURE_PDF = "docs/hcai/ab869/10049.pdf"


# ---- garbling heuristic -- tuned against confirmed clean/garbled tokens ---

@pytest.mark.parametrize("word", [
    "BLD-02468", "Construction", "Commencement", "NPC", "4/4D", "mobilization",
    "04/01/2026", "Status:", "Approved", "facility", "Building", "Retrofit",
    "HCAI", "Comment", "Narrative", "NPC5", "SPC-1", "1/4/2028",
])
def test_clean_words_are_not_garbled(word):
    assert _is_garbled_word(word) is False


@pytest.mark.parametrize("word", [
    "NprPoCje", "RMNMDJcoa", "hCaAsC", "CHoomspptilaial)nce",
])
def test_known_garbled_words_are_caught(word):
    assert _is_garbled_word(word) is True


# ---- parse_pdf against the real fixture ------------------------------------

@pytest.fixture(scope="module")
def parsed_fixture():
    return parse_pdf(FIXTURE_PDF)


def test_fixture_parses_without_error(parsed_fixture):
    assert parsed_fixture.parse_error is None


def test_fixture_plan_status_matches_the_screen(parsed_fixture):
    """Andrew's own read of the live view: Status: Not Approved."""
    assert parsed_fixture.plan_status == "Not Approved"
    assert parsed_fixture.plan_status_source == "pdf_header"


def test_fixture_paragraph_keeps_the_clean_sentence_only(parsed_fixture):
    """The sentence right after the status line collides with a citation
    and is unrecoverable; the sentence after THAT is clean on its own and
    must survive -- this is the whole point of skip-not-stop line
    handling, not the earlier stop-at-first-bad-line behavior."""
    assert parsed_fixture.plan_status_paragraph is not None
    assert ("facility shall complete the necessary construction"
           in parsed_fixture.plan_status_paragraph)
    assert parsed_fixture.plan_status_paragraph_reason is not None


def test_fixture_compliance_rows_no_duplicates(parsed_fixture):
    """The exact bug found and fixed: a narrative sentence restating its
    own building number ("Building BLD-02468 requires...") must not create
    a second phantom row for that building."""
    building_nos = [r.building_no for r in parsed_fixture.compliance_rows]
    assert len(building_nos) == len(set(building_nos))
    assert "BLD-02468" in building_nos
    assert building_nos.count("BLD-02468") == 1


def test_fixture_compliance_narrative_matches_the_screen(parsed_fixture):
    row = next(r for r in parsed_fixture.compliance_rows if r.building_no == "BLD-02468")
    assert row.narrative == "Building BLD-02468 requires NPC4 Fire Sprinkler retrofit and NPC 5 upgrade."


def test_fixture_milestone_first_row_matches_the_screen(parsed_fixture):
    row = next(r for r in parsed_fixture.milestone_rows
              if r.building_no == "BLD-02468" and r.milestone_type == "Construction Commencement")
    assert row.description == "NPC 4/4D Construction project mobilization"
    assert row.completion_date == "04/01/2026"


def test_fixture_ungrounded_regions_are_null_with_a_reason_not_guessed(parsed_fixture):
    """Fields this document's own confirmed text-layer defect cannot
    reliably reconstruct must come back null with a reason -- never a
    best-effort guess. The delay section is entirely garbled on this
    fixture (confirmed by direct inspection)."""
    assert parsed_fixture.delay_text is None
    assert parsed_fixture.delay_text_reason is not None


def test_no_garbled_word_survives_into_any_extracted_field(parsed_fixture):
    for row in parsed_fixture.compliance_rows:
        for val in (row.compliance_type, row.narrative, row.hcai_comment):
            if val:
                assert not any(_is_garbled_word(w) for w in val.split()), val
    for row in parsed_fixture.milestone_rows:
        for val in (row.milestone_type, row.description, row.hcai_comment, row.met_by_hcai):
            if val:
                assert not any(_is_garbled_word(w) for w in val.split()), val


def test_parse_pdf_never_raises_on_a_bad_file(tmp_path):
    bad = tmp_path / "not_a_pdf.pdf"
    bad.write_bytes(b"this is not a real pdf")
    result = parse_pdf(bad)
    assert result.parse_error is not None


# ---- crosstab reader --------------------------------------------------------

def _write_crosstab(tmp_path, rows, header=None):
    header = header or [
        "App Status", "Application Status", "Approval Status", "blank", "Building No.",
        "Compliance Type (Reported by Hosptial)", "Owner Name", "Ownership",
        "Who manages the hospital? ", "What type of entity manages this hospital? ",
        "Who is financially responsible for the seismic upgrades?", "Progress", "Status",
    ]
    path = tmp_path / "crosstab.csv"
    with open(path, "w", newline="") as f:
        import csv as _csv
        w = _csv.writer(f)
        w.writerow(header)
        for row in rows:
            w.writerow(row)
    return path


def test_read_crosstab_dedupes_buildings_and_takes_plan_status(tmp_path):
    rows = [
        ["Finalized.", "Not Approved", "Finalized.", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", ""],
        ["Finalized.", "Not Approved", "Finalized.", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", "Completed"],
    ]
    facility = read_crosstab(_write_crosstab(tmp_path, rows))
    assert facility.buildings == {"BLD-001": "NPC Retrofit"}
    assert facility.plan_status == "Not Approved"
    assert facility.owner_name == "Owner Co"
    assert facility.financially_responsible_party == "Responsible Co"
    assert not facility.plan_status_ambiguous_values


def test_read_crosstab_flags_missed_milestone_buildings(tmp_path):
    rows = [
        ["F", "Not Approved", "F", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", ""],
        ["F", "Not Approved", "F", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "Missed Milestone(s)", ""],
        ["F", "Not Approved", "F", " ", "BLD-002", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", ""],
    ]
    facility = read_crosstab(_write_crosstab(tmp_path, rows))
    assert facility.missed_milestone_buildings == {"BLD-001"}


def test_import_sets_has_missed_milestone_on_the_building(db_session, tmp_path):
    raw_dir = _seed_raw_dir(tmp_path)
    # Add a second row for the same building flagged Missed Milestone(s).
    crosstab_path = raw_dir / "10049" / "crosstab.csv"
    import csv as _csv
    with open(crosstab_path, "a", newline="") as f:
        w = _csv.writer(f)
        w.writerow(["F", "Not Approved", "F", " ", "BLD-02468", "NPC Retrofit",
                   "Contra Costa County Medical Center", "County",
                   "Contra Costa County Medical Center", "County",
                   "Contra Costa County Medical Center", "Missed Milestone(s)", ""])
    import_ab869(db_session, raw_dir=str(raw_dir))
    building = db_session.exec(
        select(Ab869Building).where(Ab869Building.perm_id == "10049",
                                    Ab869Building.building_nbr == "BLD-02468")
    ).one()
    assert building.has_missed_milestone is True


def test_read_crosstab_flags_ambiguous_plan_status(tmp_path):
    """Confirmed on the real corpus: perm_id 11966 carries two distinct
    Application Status values across its own rows."""
    rows = [
        ["F", "Out to Applicant", "F", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", ""],
        ["F", "Not Approved", "F", " ", "BLD-001", "NPC Retrofit",
         "Owner Co", "County", "Manager Co", "County", "Responsible Co", "", ""],
    ]
    facility = read_crosstab(_write_crosstab(tmp_path, rows))
    assert facility.plan_status is None
    assert facility.plan_status_ambiguous_values == {"Out to Applicant", "Not Approved"}


# ---- import: idempotency, replace-not-append, no DB writes without both files --

def _seed_raw_dir(tmp_path, perm_id="10049"):
    facility_dir = tmp_path / perm_id
    facility_dir.mkdir()
    (facility_dir / "compliance_plan_expanded.pdf").write_bytes(Path(FIXTURE_PDF).read_bytes())
    rows = [
        ["F", "Not Approved", "F", " ", "BLD-02468", "NPC Retrofit",
         "Contra Costa County Medical Center", "County", "Contra Costa County Medical Center",
         "County", "Contra Costa County Medical Center", "", ""],
    ]
    _write_crosstab(facility_dir, rows)
    return tmp_path


def test_import_writes_plan_buildings_and_milestones(db_session, tmp_path):
    raw_dir = _seed_raw_dir(tmp_path)
    stats = import_ab869(db_session, raw_dir=str(raw_dir))
    assert stats["facilities_imported"] == 1
    assert stats["buildings_written"] == 1

    plan = db_session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == "10049")).one()
    assert plan.plan_status == "Not Approved"
    assert plan.owner_name == "Contra Costa County Medical Center"
    assert plan.other_financial_contact is None
    assert plan.other_financial_contact_reason is not None

    building = db_session.exec(select(Ab869Building).where(Ab869Building.perm_id == "10049")).one()
    assert building.building_nbr == "BLD-02468"
    assert building.compliance_type == "NPC Retrofit"
    assert building.narrative == "Building BLD-02468 requires NPC4 Fire Sprinkler retrofit and NPC 5 upgrade."

    milestones = db_session.exec(select(Ab869Milestone).where(Ab869Milestone.perm_id == "10049")).all()
    assert len(milestones) > 0

    run = db_session.exec(select(SourceRun).where(SourceRun.source == "ab869_compliance_plans")).one()
    assert run.ok is True


def test_import_is_idempotent_on_unchanged_pdf(db_session, tmp_path):
    raw_dir = _seed_raw_dir(tmp_path)
    import_ab869(db_session, raw_dir=str(raw_dir))
    stats2 = import_ab869(db_session, raw_dir=str(raw_dir))
    assert stats2["facilities_imported"] == 0
    assert stats2["facilities_skipped_unchanged"] == 1
    assert db_session.exec(select(Ab869Plan)).all().__len__() == 1


def test_import_replaces_rows_when_pdf_hash_changes(db_session, tmp_path):
    raw_dir = _seed_raw_dir(tmp_path)
    import_ab869(db_session, raw_dir=str(raw_dir))
    # Simulate a re-pulled PDF: different bytes, same facility.
    pdf_path = raw_dir / "10049" / "compliance_plan_expanded.pdf"
    pdf_path.write_bytes(pdf_path.read_bytes() + b"\n%changed")
    stats2 = import_ab869(db_session, raw_dir=str(raw_dir))
    assert stats2["facilities_imported"] == 1
    assert stats2["facilities_skipped_unchanged"] == 0
    plans = db_session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == "10049")).all()
    assert len(plans) == 1  # replaced, not duplicated


def test_import_uses_crosstab_alone_when_pdf_is_missing(db_session, tmp_path):
    """Confirmed real on the corpus: perm_ids 10685 and 17905 have real
    crosstab rows but no PDF (the Playwright pull failed for exactly those
    two). Their compliance_type/building data must still import -- only
    the PDF-only fields (narrative, milestones, delay text) go null with
    a reason, not the whole facility."""
    facility_dir = tmp_path / "10685"
    facility_dir.mkdir()
    rows = [
        ["F", "Approved", "F", " ", "BLD-01466", "SPC and NPC Retrofit",
         "Ventas Realty, LP", "Private (Investor - Limited Liability Company)",
         "THC - Orange County, LLC", "Private (Investor – Corporation)",
         "THC - Orange County, LLC", "", ""],
    ]
    _write_crosstab(facility_dir, rows)
    # deliberately no compliance_plan_expanded.pdf written

    stats = import_ab869(db_session, raw_dir=str(tmp_path))
    assert stats["facilities_imported"] == 1
    assert stats["facilities_no_crosstab_file"] == 0
    assert stats["facilities_no_plan_on_file"] == 0

    plan = db_session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == "10685")).one()
    assert plan.plan_status == "Approved"
    assert plan.plan_status_paragraph is None
    assert "no PDF on disk" in plan.plan_status_paragraph_reason

    building = db_session.exec(select(Ab869Building).where(Ab869Building.perm_id == "10685")).one()
    assert building.compliance_type == "SPC and NPC Retrofit"
    assert building.narrative is None


def test_import_skips_a_facility_with_a_genuinely_empty_crosstab(db_session, tmp_path):
    """The other real case: an empty (2-byte) crosstab means HCAI's own
    filter matched nothing for this facility -- no plan on file, not a
    technical gap. Must not be imported as an empty/blank plan row."""
    facility_dir = tmp_path / "10062"
    facility_dir.mkdir()
    (facility_dir / "crosstab.csv").write_bytes(b"\r\n")

    stats = import_ab869(db_session, raw_dir=str(tmp_path))
    assert stats["facilities_imported"] == 0
    assert stats["facilities_no_plan_on_file"] == 1
    assert db_session.exec(select(Ab869Plan)).all() == []


def test_import_keeps_a_real_status_with_zero_buildings(db_session, tmp_path):
    """The bug this exact test guards against: a facility can carry a real
    Application Status with NO building rows at all -- confirmed real,
    perm_id 10028's own status is "Already 2030 compliant, no need to
    submit a compliance plan" per PIN 80's exemption rule for already-
    compliant facilities. Gating on crosstab.buildings alone silently
    discarded this real, informative status."""
    facility_dir = tmp_path / "10028"
    facility_dir.mkdir()
    rows = [
        ["", "Already 2030 compliant, no need to submit a compliance plan", "", " ",
         "", "", "", "", "", "", "", "", ""],
    ]
    _write_crosstab(facility_dir, rows)

    stats = import_ab869(db_session, raw_dir=str(tmp_path))
    assert stats["facilities_imported"] == 1
    plan = db_session.exec(select(Ab869Plan).where(Ab869Plan.perm_id == "10028")).one()
    assert plan.plan_status == "Already 2030 compliant, no need to submit a compliance plan"
    assert db_session.exec(select(Ab869Building).where(Ab869Building.perm_id == "10028")).all() == []


def test_import_skips_a_facility_missing_the_crosstab(db_session, tmp_path):
    facility_dir = tmp_path / "99999"
    facility_dir.mkdir()
    (facility_dir / "compliance_plan_expanded.pdf").write_bytes(Path(FIXTURE_PDF).read_bytes())
    stats = import_ab869(db_session, raw_dir=str(tmp_path))
    assert stats["facilities_no_crosstab_file"] == 1
    assert stats["facilities_imported"] == 0


def test_import_raises_on_missing_raw_dir(db_session):
    with pytest.raises(RuntimeError, match="does not exist"):
        import_ab869(db_session, raw_dir="docs/hcai/ab869/this-does-not-exist")
