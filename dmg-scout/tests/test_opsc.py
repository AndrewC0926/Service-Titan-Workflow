"""OPSC School Facility Program: bulk-CSV parsing, full-replace-whole-table
semantics, the 18-month/status-changed Signal gate, and the SAB workload-
list text parsing (county-anchor split). See app/pipeline/opsc.py's module
docstring for the compliance check and design this relies on.

The fetch HTTP call is mocked (respx); these tests are about parsing,
gating, and the join/ambiguity logic, not live network."""
from datetime import timedelta

import httpx
import respx
from sqlmodel import select

from app.http import PoliteClient
from app.models import (
    Category, MatchCandidate, OpscProject, OpscStatusClass, OpscWorkload, ProjectSignal,
    Signal, SignalType, SourceRun, Stage, classify_opsc_status, utcnow,
)
from app.pipeline.opsc import (
    CSV_URL, WORKLOAD_URLS, fetch_opsc_projects, fetch_opsc_workload, parse_rows,
    parse_workload_text, signal_stage,
)

CSV_HEADER = (
    "County,District,School_Name,Program,Application_Number,Applicant,"
    "Preliminary_Grant_Application,Full_Grant_Application,Site_and_Design_Application,"
    "Site_Only_Application,Design_Only_Application,Environmental_Hardship_Application,"
    "Reduced_to_Costs_Incurred,Number_of_Elementary_School_Pupil_Grants_Requested,"
    "Number_of_Middle_School_Pupil_Grants_Requested,Number_of_High_School_Pupil_Grants_Requested,"
    "Number_of_Non_Severe_School_Pupil_Grants_Requested,Number_of_Severe_School_Pupil_Grants_Requested,"
    "Grade_Level_of_Project,State_Share_of_Funding,Site_Acquisition,Financial_Hardship,"
    "CSFA_Lease_Amount,CTEFP_Loan_Amount,Type_of_Joint_Use_Facility,Type_of_Joint_Use_Partner,"
    "Industry_Sector,Portables_Replaced,Last_SAB_Date,Status"
)


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _csv_row(county="Los Angeles", district="Test Unified", school="Test Elementary",
            program="New Construction", app_no="50/00000-00-001", full_grant="1000000.00",
            state_share="2000000.00", last_sab_date="7/25/2007", status="Closed") -> str:
    return (f"{county},{district},{school},{program},{app_no},,,{full_grant},,,,,,,,,,,"
           f"MI,{state_share},,,,,,,,,{last_sab_date},{status}")


def _csv(rows: list[str]) -> bytes:
    return ("﻿" + CSV_HEADER + "\n" + "\n".join(rows) + "\n").encode("utf-8")


def _mock_csv(rows: list[str]) -> None:
    respx.get(CSV_URL).mock(return_value=httpx.Response(200, content=_csv(rows)))


# --- parse_rows: field extraction, numeric coercion, verbatim text fields --


def test_parse_rows_extracts_every_field():
    raw = _csv([_csv_row(county="Amador", district="Amador County Office of Education",
                        school="Amador County Community", app_no="50/10033-00-001",
                        full_grant="1885002.00", state_share="2011460.00",
                        last_sab_date="7/25/2007", status="Closed")])
    rows = parse_rows(raw)
    assert len(rows) == 1
    r = rows[0]
    assert r["county"] == "Amador"
    assert r["district"] == "Amador County Office of Education"
    assert r["school_name"] == "Amador County Community"
    assert r["application_number"] == "50/10033-00-001"
    assert r["full_grant_application"] == 1885002.00
    assert r["state_share_of_funding"] == 2011460.00
    assert r["last_sab_date"].strftime("%Y-%m-%d") == "2007-07-25"
    assert r["status"] == "Closed"


def test_parse_rows_drops_unkeyable_rows_with_no_application_number():
    raw = _csv([_csv_row(app_no=""), _csv_row(app_no="50/00000-00-002")])
    rows = parse_rows(raw)
    assert [r["application_number"] for r in rows] == ["50/00000-00-002"]


def test_parse_rows_blank_numeric_field_is_none_not_zero():
    raw = _csv([_csv_row(full_grant="")])
    rows = parse_rows(raw)
    assert rows[0]["full_grant_application"] is None


def test_parse_rows_text_typed_fields_stay_strings_even_when_numeric_looking():
    """Preliminary_Grant_Application is declared type=text by the datastore
    itself (checked live) -- stored verbatim as a string, never coerced to
    a float even when its value looks numeric."""
    raw_row = _csv_row()
    # inject a numeric-looking value into the (text-typed) Preliminary_Grant_Application column
    fields = raw_row.split(",")
    fields[6] = "12345.00"  # Preliminary_Grant_Application is column index 6
    raw = _csv([",".join(fields)])
    rows = parse_rows(raw)
    assert rows[0]["preliminary_grant_application"] == "12345.00"
    assert isinstance(rows[0]["preliminary_grant_application"], str)


# --- classify_opsc_status (WS3.4) -------------------------------------------


def test_classify_opsc_status_closed():
    assert classify_opsc_status("Closed") == OpscStatusClass.closed


def test_classify_opsc_status_funds_released():
    assert classify_opsc_status("Funds Released") == OpscStatusClass.funds_released


def test_classify_opsc_status_none_is_unknown():
    assert classify_opsc_status(None) == OpscStatusClass.unknown


def test_classify_opsc_status_anything_else_is_open():
    assert classify_opsc_status("In Process") == OpscStatusClass.open
    assert classify_opsc_status("100.00% Completed") == OpscStatusClass.open


# --- signal_stage -----------------------------------------------------------


def test_signal_stage_funds_released_and_matching_program_is_procurement():
    assert signal_stage("New Construction", "Funds Released") == Stage.procurement
    assert signal_stage("Modernization", "Funds Released") == Stage.procurement


def test_signal_stage_earlier_status_is_entitlement():
    assert signal_stage("New Construction", None) == Stage.entitlement
    assert signal_stage("New Construction", "In Process") == Stage.entitlement


def test_signal_stage_closed_is_operating_not_entitlement():
    """WS3.4 fix, regression: a Closed application is terminal -- the school
    project it funded is done, not still in entitlement. Old code returned
    Stage.entitlement here (see git history of this test); this must not
    regress back to that."""
    assert signal_stage("New Construction", "Closed") == Stage.operating
    assert signal_stage("Modernization", "Closed") == Stage.operating


def test_signal_stage_funds_released_with_other_program_is_still_entitlement():
    assert signal_stage("Charter", "Funds Released") == Stage.entitlement


# --- fetch_opsc_projects: full replace, in_territory, Signal gating --------


@respx.mock
def test_fetch_stores_rows_and_a_source_run(db_session, cfg):
    _mock_csv([_csv_row(county="Los Angeles")])
    with fast_client() as client:
        stats = fetch_opsc_projects(db_session, cfg, client)
    assert stats["fetched"] == 1 and stats["stored"] == 1 and stats["error"] is None
    rec = db_session.exec(select(OpscProject)).one()
    assert rec.county == "Los Angeles" and rec.in_territory is True
    run = db_session.exec(select(SourceRun)).one()
    assert run.source == "opsc_school_facility" and run.ok is True


@respx.mock
def test_fetch_marks_out_of_territory_county_correctly(db_session, cfg):
    _mock_csv([_csv_row(county="Alpine")])  # not in config.yaml's territory.CA list
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)
    rec = db_session.exec(select(OpscProject)).one()
    assert rec.in_territory is False


@respx.mock
def test_fetch_full_replaces_the_whole_table_on_each_load(db_session, cfg):
    _mock_csv([_csv_row(app_no="50/00000-00-001"), _csv_row(app_no="50/00000-00-002")])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)
    assert db_session.exec(select(OpscProject)).all().__len__() == 2

    _mock_csv([_csv_row(app_no="50/00000-00-003")])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)
    rows = db_session.exec(select(OpscProject)).all()
    assert [r.application_number for r in rows] == ["50/00000-00-003"]


@respx.mock
def test_fetch_cold_start_only_signals_recent_rows_never_flags_all_as_changed(db_session, cfg):
    """First-ever fetch: 'status changed since prior load' has no meaningful
    prior, so ONLY the 18-month recency test applies -- an old row must NOT
    get a signal just because there was no prior snapshot to compare to."""
    old_date = (utcnow() - timedelta(days=365 * 5)).strftime("%-m/%-d/%Y")
    recent_date = (utcnow() - timedelta(days=30)).strftime("%-m/%-d/%Y")
    _mock_csv([
        _csv_row(app_no="50/00000-00-001", last_sab_date=old_date, county="Los Angeles"),
        _csv_row(app_no="50/00000-00-002", last_sab_date=recent_date, county="Los Angeles"),
    ])
    with fast_client() as client:
        stats = fetch_opsc_projects(db_session, cfg, client)
    assert stats["signals_created"] == 1
    sig = db_session.exec(select(Signal).where(Signal.signal_type == SignalType.school_facility_funding)).one()
    assert sig.developer_or_owner == "Test Unified"


@respx.mock
def test_fetch_status_change_signals_even_when_old(db_session, cfg):
    old_date = (utcnow() - timedelta(days=365 * 5)).strftime("%-m/%-d/%Y")
    _mock_csv([_csv_row(app_no="50/00000-00-001", last_sab_date=old_date,
                        county="Los Angeles", status="Closed")])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)  # cold start -- no signal (old, no prior)
    assert db_session.exec(select(Signal)).all() == []

    _mock_csv([_csv_row(app_no="50/00000-00-001", last_sab_date=old_date,
                        county="Los Angeles", status="Funds Released")])
    with fast_client() as client:
        stats = fetch_opsc_projects(db_session, cfg, client)
    assert stats["signals_created"] == 1  # status changed since prior load, even though still old


@respx.mock
def test_fetch_unchanged_old_row_stays_inert_on_second_load(db_session, cfg):
    old_date = (utcnow() - timedelta(days=365 * 5)).strftime("%-m/%-d/%Y")
    _mock_csv([_csv_row(app_no="50/00000-00-001", last_sab_date=old_date,
                        county="Los Angeles", status="Closed")])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)  # cold start
    _mock_csv([_csv_row(app_no="50/00000-00-001", last_sab_date=old_date,
                        county="Los Angeles", status="Closed")])
    with fast_client() as client:
        stats = fetch_opsc_projects(db_session, cfg, client)  # same status, still old
    assert stats["signals_created"] == 0


@respx.mock
def test_fetch_out_of_territory_never_signals_even_when_recent(db_session, cfg):
    recent_date = (utcnow() - timedelta(days=30)).strftime("%-m/%-d/%Y")
    _mock_csv([_csv_row(county="Alpine", last_sab_date=recent_date)])
    with fast_client() as client:
        stats = fetch_opsc_projects(db_session, cfg, client)
    assert stats["signals_created"] == 0


@respx.mock
def test_signal_never_auto_links_or_auto_creates_a_project(db_session, cfg):
    """Confirmed via app.pipeline.resolve's own school_facility_funding
    branch -- run_resolve must never turn an OPSC signal into a new
    Project on its own."""
    from app.pipeline.resolve import run_resolve

    recent_date = (utcnow() - timedelta(days=30)).strftime("%-m/%-d/%Y")
    _mock_csv([_csv_row(county="Los Angeles", last_sab_date=recent_date)])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)

    run_resolve(db_session, cfg, use_llm=False)
    assert db_session.exec(select(ProjectSignal)).all() == []


@respx.mock
def test_signal_with_fuzzy_candidate_queues_match_candidate_never_auto_links(db_session, cfg):
    from app.models import Project
    from app.pipeline.resolve import run_resolve

    project = Project(name="Test Elementary", developer="Test Unified", county="Los Angeles",
                      state="CA", category=Category.industrial)
    db_session.add(project)
    db_session.commit()

    recent_date = (utcnow() - timedelta(days=30)).strftime("%-m/%-d/%Y")
    _mock_csv([_csv_row(county="Los Angeles", district="Test Unified", school="Test Elementary",
                        last_sab_date=recent_date)])
    with fast_client() as client:
        fetch_opsc_projects(db_session, cfg, client)

    run_resolve(db_session, cfg, use_llm=False)
    assert db_session.exec(select(ProjectSignal)).all() == []  # never auto-linked
    candidates = db_session.exec(select(MatchCandidate)).all()
    assert len(candidates) == 1
    assert candidates[0].project_id == project.id


# --- parse_workload_text: county-anchor split, rightmost match ------------


def test_parse_workload_text_splits_district_county_school():
    text = ("District County Site Name Application Number\n"
           "Jurupa Unified Riverside Troth Street Elementary 50/67090-00-020 05/03/23 $ 7,241,504\n")
    result = parse_workload_text(text)
    assert result["parsed"] == 1
    row = result["rows"][0]
    assert row["district"] == "Jurupa Unified"
    assert row["school_name"] == "Troth Street Elementary"
    assert row["application_number"] == "50/67090-00-020"


def test_parse_workload_text_district_repeating_its_own_county_uses_rightmost_match():
    """'Riverside Unified' (district) followed by 'Riverside' (county) --
    the FIRST match would wrongly split after the district's own name."""
    text = "Riverside Unified Riverside John W. North High 50/67215-00-045 01/25/24\n"
    result = parse_workload_text(text)
    assert result["parsed"] == 1
    row = result["rows"][0]
    assert row["district"] == "Riverside Unified"
    assert row["school_name"] == "John W. North High"


def test_parse_workload_text_reports_failure_when_no_county_found():
    text = "Some District With No County Name Some School 50/12345-00-001 01/01/24\n"
    result = parse_workload_text(text)
    assert result["parsed"] == 0
    assert result["failed"] == 1
    assert len(result["fail_examples"]) == 1


def test_parse_workload_text_ignores_lines_with_no_application_number():
    text = "SFP APPLICATIONS\nNew Construction School Facility Program\n"
    result = parse_workload_text(text)
    assert result["parsed"] == 0 and result["failed"] == 0


# --- fetch_opsc_workload: full replace per program, parse-rate floor ------


@respx.mock
def test_fetch_workload_loads_both_programs(db_session, cfg, monkeypatch):
    import app.pipeline.opsc as opsc_mod

    good_text = "Jurupa Unified Riverside Troth Street Elementary 50/67090-00-020 05/03/23\n"
    monkeypatch.setattr(opsc_mod, "pdf_to_text", lambda raw: good_text)
    for url in WORKLOAD_URLS.values():
        respx.get(url).mock(return_value=httpx.Response(200, content=b"fake-pdf-bytes"))

    with fast_client() as client:
        results = fetch_opsc_workload(db_session, cfg, client)

    assert results["New Construction"]["loaded"] is True
    assert results["Modernization"]["loaded"] is True
    rows = db_session.exec(select(OpscWorkload)).all()
    assert len(rows) == 2
    assert all(r.label == "application in house, not funded" for r in rows)


@respx.mock
def test_fetch_workload_skips_program_below_parse_floor(db_session, cfg, monkeypatch):
    import app.pipeline.opsc as opsc_mod

    # 1 parseable line, 9 unparseable -- 10% success, well below the 90% floor.
    bad_line = "Some District With No County Name Some School 50/12345-00-{:03d} 01/01/24\n"
    good_line = "Jurupa Unified Riverside Troth Street Elementary 50/67090-00-020 05/03/23\n"
    text = good_line + "".join(bad_line.format(i) for i in range(9))
    monkeypatch.setattr(opsc_mod, "pdf_to_text", lambda raw: text)
    for url in WORKLOAD_URLS.values():
        respx.get(url).mock(return_value=httpx.Response(200, content=b"fake-pdf-bytes"))

    with fast_client() as client:
        results = fetch_opsc_workload(db_session, cfg, client)

    for program, result in results.items():
        assert result["loaded"] is False
        assert result["error"] is not None
    assert db_session.exec(select(OpscWorkload)).all() == []


@respx.mock
def test_fetch_workload_full_replaces_only_the_matching_program(db_session, cfg, monkeypatch):
    import app.pipeline.opsc as opsc_mod

    good_text = "Jurupa Unified Riverside Troth Street Elementary 50/67090-00-020 05/03/23\n"
    monkeypatch.setattr(opsc_mod, "pdf_to_text", lambda raw: good_text)
    for url in WORKLOAD_URLS.values():
        respx.get(url).mock(return_value=httpx.Response(200, content=b"fake-pdf-bytes"))

    with fast_client() as client:
        fetch_opsc_workload(db_session, cfg, client)
    with fast_client() as client:
        fetch_opsc_workload(db_session, cfg, client)  # re-run: must not duplicate

    rows = db_session.exec(select(OpscWorkload)).all()
    assert len(rows) == 2  # still one per program, not four


# --- schools_board: sort, filters, call target ------------------------------


def _opsc(session, app_no, county="Los Angeles", district="Test Unified",
         school="Test Elementary", program="New Construction", status="In Process",
         last_sab_date=None, grade_level=None, in_territory=True) -> OpscProject:
    row = OpscProject(application_number=app_no, county=county, district=district,
                      school_name=school, program=program, status=status,
                      last_sab_date=last_sab_date, grade_level_of_project=grade_level,
                      in_territory=in_territory, source_url="x")
    session.add(row)
    session.commit()
    return row


def test_schools_board_sorts_by_last_sab_date_descending(db_session, cfg):
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", last_sab_date=utcnow() - timedelta(days=100))
    _opsc(db_session, "2", last_sab_date=utcnow() - timedelta(days=1))
    _opsc(db_session, "3", last_sab_date=None)

    result = schools_board(db_session, cfg)
    ids = [d["row"].application_number for d in result]
    assert ids == ["2", "1", "3"]  # most recent first, no-date rows last


def test_schools_board_excludes_out_of_territory(db_session, cfg):
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", in_territory=True)
    _opsc(db_session, "2", in_territory=False)
    result = schools_board(db_session, cfg)
    assert [d["row"].application_number for d in result] == ["1"]


def test_schools_board_filters_county_district_program_status_grade(db_session, cfg):
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", county="Los Angeles", district="A", program="New Construction",
         status="In Process", grade_level="EL")
    _opsc(db_session, "2", county="Orange", district="B", program="Modernization",
         status="Funds Released", grade_level="HI")
    _opsc(db_session, "3", county="Los Angeles", district="A", program="New Construction",
         status="Closed", grade_level="EL")

    assert [d["row"].application_number for d in schools_board(db_session, cfg, county="Orange")] == ["2"]
    assert [d["row"].application_number for d in schools_board(db_session, cfg, district="A")] == ["1"]
    assert [d["row"].application_number
           for d in schools_board(db_session, cfg, program="Modernization")] == ["2"]
    assert [d["row"].application_number for d in schools_board(db_session, cfg, status="Closed")] == ["3"]
    assert [d["row"].application_number for d in schools_board(db_session, cfg, grade_level="HI")] == ["2"]


def test_schools_board_carries_call_target(db_session, cfg):
    from app.call_target import CallTarget
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", district="Los Angeles Unified", status="In Process")
    _opsc(db_session, "2", district="Some Non-Standards District", status="Funds Released")
    _opsc(db_session, "3", district="Some Non-Standards District", status="In Process")

    by_id = {d["row"].application_number: d for d in schools_board(db_session, cfg)}
    assert by_id["1"]["call_target"].target == CallTarget.owner_standards
    assert by_id["2"]["call_target"].target == CallTarget.bidding_contractors
    assert by_id["3"]["call_target"].target == CallTarget.engineer


def test_schools_board_default_excludes_closed_even_without_status_filter(db_session, cfg):
    """WS3.4 fix, regression: a Closed row must not appear on the default
    (unfiltered) board at all, regardless of what other filters (or none)
    are passed -- old code showed it with call_target R4 'engineer, spec
    not locked', which is wrong for a dead application."""
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", district="Some Non-Standards District", status="In Process")
    _opsc(db_session, "2", district="Some Non-Standards District", status="Closed")

    result = schools_board(db_session, cfg)
    assert [d["row"].application_number for d in result] == ["1"]

    # explicit status filter still surfaces the closed row on request --
    # exclusion is a default-view behavior, not a claim the row is gone
    explicit = schools_board(db_session, cfg, status="Closed")
    assert [d["row"].application_number for d in explicit] == ["2"]


def test_schools_board_closed_standards_district_is_also_excluded_by_default(db_session, cfg):
    """A standards-owner district (R1, owner_standards) still gets excluded
    from the default board when closed -- the board-exclusion check runs
    before call-target rule precedence, not after."""
    from app.pipeline.opsc import schools_board
    _opsc(db_session, "1", district="Los Angeles Unified", status="Closed")

    assert schools_board(db_session, cfg) == []
    explicit = schools_board(db_session, cfg, status="Closed")
    assert len(explicit) == 1
    assert explicit[0]["call_target"].target.value == "owner_standards"
