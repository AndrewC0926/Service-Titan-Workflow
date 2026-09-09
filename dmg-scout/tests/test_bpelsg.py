"""BPELSG mechanical engineer roster: tab-delimited parsing scoped to
in-territory Mechanical Engineer rows, idempotent upsert by license_no,
and the normalized-person-name join to a project's mep_engineer-titled
named_people entry -- see app/pipeline/bpelsg.py's module docstring for
the access investigation (DCA's own free monthly Box file, no firm field
at all) this rests on."""
from datetime import datetime

from sqlmodel import select

from app.models import BpelsgEngineer, Category, Project, ProjectSignal, Signal, SignalType, Stage
from app.pipeline.bpelsg import (
    bpelsg_roster_by_normalized_name, load_bpelsg_engineers, match_bpelsg_for_project,
    mep_engineer_person_names_by_project, parse_rows,
)

TERRITORY = {"Los Angeles", "Orange", "San Bernardino", "Riverside", "San Diego", "Imperial", "Kern"}

HEADER = ["Agency Name", "License Type", "Speciality Code", "License Number", "Indiv/Org",
         "Org/Last Name", "First Name", "Middle Name", "Suffix", "Address Line 1",
         "Address Line 2", "City", "County", "State", "Zip", "Country",
         "Original Issue Date", "Expiration Date", "School", "Year Graduated", "Degree",
         "License Status"]


def _row(license_no="1", first="John", middle="", last="Smith", license_type="Mechanical Engineer",
        city="Los Angeles", county="Los Angeles", state="CA", status="Active",
        expiry="09-30-2027") -> dict:
    return {
        "Agency Name": "Board For Prof Engineers, Land Surveyors, And Geologists",
        "License Type": license_type, "Speciality Code": "", "License Number": license_no,
        "Indiv/Org": "I", "Org/Last Name": last, "First Name": first, "Middle Name": middle,
        "Suffix": "", "Address Line 1": "123 Main St", "Address Line 2": "", "City": city,
        "County": county, "State": state, "Zip": "90001", "Country": "",
        "Original Issue Date": "01-01-1990", "Expiration Date": expiry, "School": "",
        "Year Graduated": "", "Degree": "", "License Status": status,
    }


def _tsv(rows: list[dict]) -> bytes:
    lines = ["\t".join(HEADER)]
    for r in rows:
        lines.append("\t".join(r.get(h, "") for h in HEADER))
    return ("\n".join(lines)).encode("utf-8")


# ---- parse_rows: license type + territory + state scope -------------------

def test_parse_rows_keeps_only_mechanical_engineer():
    raw = _tsv([_row(license_no="1", license_type="Mechanical Engineer"),
               _row(license_no="2", license_type="Civil Engineer")])
    rows = parse_rows(raw, TERRITORY)
    assert [r["license_no"] for r in rows] == ["1"]


def test_parse_rows_drops_out_of_territory_county():
    raw = _tsv([_row(license_no="1", county="Los Angeles"),
               _row(license_no="2", county="Sacramento")])
    rows = parse_rows(raw, TERRITORY)
    assert [r["license_no"] for r in rows] == ["1"]


def test_parse_rows_drops_non_ca_state():
    raw = _tsv([_row(license_no="1", state="CA"), _row(license_no="2", state="NV")])
    rows = parse_rows(raw, TERRITORY)
    assert [r["license_no"] for r in rows] == ["1"]


def test_parse_rows_builds_full_name_and_keeps_status_and_expiry():
    raw = _tsv([_row(license_no="1", first="John", middle="Q", last="Smith", status="Delinquent",
                     expiry="03-31-2022")])
    rows = parse_rows(raw, TERRITORY)
    r = rows[0]
    assert r["name"] == "John Q Smith"
    assert r["status"] == "Delinquent"
    assert r["expiry"] == datetime(2022, 3, 31)
    assert r["county"] == "Los Angeles"


# ---- load_bpelsg_engineers: idempotent upsert ------------------------------

def test_load_is_idempotent_on_license_no(db_session, cfg, tmp_path):
    path = tmp_path / "bpelsg.xls"
    path.write_bytes(_tsv([_row(license_no="1", city="Los Angeles")]))

    stats1 = load_bpelsg_engineers(db_session, cfg, file_path=str(path))
    assert stats1["fetched"] == 1 and stats1["new"] == 1 and stats1["updated"] == 0
    assert db_session.exec(select(BpelsgEngineer)).all().__len__() == 1

    # Re-run against the SAME file -- must update in place, not duplicate.
    stats2 = load_bpelsg_engineers(db_session, cfg, file_path=str(path))
    assert stats2["fetched"] == 1 and stats2["new"] == 0 and stats2["updated"] == 1
    rows = db_session.exec(select(BpelsgEngineer)).all()
    assert len(rows) == 1
    assert rows[0].license_no == "1"


def test_load_updates_status_on_a_later_file(db_session, cfg, tmp_path):
    path = tmp_path / "bpelsg.xls"
    path.write_bytes(_tsv([_row(license_no="1", status="Active")]))
    load_bpelsg_engineers(db_session, cfg, file_path=str(path))

    path.write_bytes(_tsv([_row(license_no="1", status="Delinquent")]))
    load_bpelsg_engineers(db_session, cfg, file_path=str(path))

    row = db_session.get(BpelsgEngineer, "1")
    assert row.status == "Delinquent"


def test_load_records_error_on_missing_file(db_session, cfg):
    stats = load_bpelsg_engineers(db_session, cfg, file_path="/nonexistent/path.xls")
    assert stats["error"] is not None


# ---- name-match / abstain -------------------------------------------------

def _project(session, county="Los Angeles") -> Project:
    p = Project(name="Test Project", category=Category.industrial, county=county,
               state="CA", stage=Stage.design)
    session.add(p)
    session.commit()
    session.refresh(p)
    return p


def _signal_with_mep_person(session, project: Project, person_name: str, title="Mechanical Engineer") -> None:
    sig = Signal(signal_type=SignalType.ceqa_nop, category=Category.industrial, stage=Stage.design,
                named_people=[{"name": person_name, "title": title}], confidence=0.9)
    session.add(sig)
    session.commit()
    session.refresh(sig)
    session.add(ProjectSignal(project_id=project.id, signal_id=sig.id))
    session.commit()


def _engineer(session, license_no, name, county) -> BpelsgEngineer:
    e = BpelsgEngineer(license_no=license_no, name=name, license_type="Mechanical Engineer",
                       county=county, status="Active", file_date=datetime(2026, 9, 1))
    session.add(e)
    session.commit()
    return e


def test_mep_engineer_person_names_by_project_finds_titled_person(db_session, cfg):
    p = _project(db_session)
    _signal_with_mep_person(db_session, p, "Jane Doe", title="Project Manager")  # not a match
    _signal_with_mep_person(db_session, p, "John Smith", title="Mechanical Engineer, PE")

    names = mep_engineer_person_names_by_project(db_session, [p.id])
    assert names[p.id] == ["John Smith"]


def test_name_match_resolves_a_single_unambiguous_candidate(db_session, cfg):
    p = _project(db_session, county="Los Angeles")
    _engineer(db_session, "111", "John Smith", "Los Angeles")

    roster = bpelsg_roster_by_normalized_name(db_session)
    match = match_bpelsg_for_project(roster, p, ["John Smith, PE"])
    assert match == {"license_no": "111", "county": "Los Angeles", "status": "Active"}


def test_name_match_abstains_on_two_licensees_same_name_different_county(db_session, cfg):
    """The exact fixture the build was asked for: two licensees share a
    normalized name in different counties, and the project's own county
    matches neither -- must abstain, never guess."""
    p = _project(db_session, county="Orange")
    _engineer(db_session, "111", "John Smith", "Los Angeles")
    _engineer(db_session, "222", "John Smith", "San Bernardino")

    roster = bpelsg_roster_by_normalized_name(db_session)
    match = match_bpelsg_for_project(roster, p, ["John Smith"])
    assert match is None


def test_name_match_narrows_by_project_county_when_it_disambiguates(db_session, cfg):
    """Same two-licensee setup, but the project's OWN county matches
    exactly one of them -- must resolve, not abstain."""
    p = _project(db_session, county="Los Angeles")
    _engineer(db_session, "111", "John Smith", "Los Angeles")
    _engineer(db_session, "222", "John Smith", "San Bernardino")

    roster = bpelsg_roster_by_normalized_name(db_session)
    match = match_bpelsg_for_project(roster, p, ["John Smith"])
    assert match == {"license_no": "111", "county": "Los Angeles", "status": "Active"}


def test_name_match_no_candidate_returns_none(db_session, cfg):
    p = _project(db_session)
    roster = bpelsg_roster_by_normalized_name(db_session)
    match = match_bpelsg_for_project(roster, p, ["Nobody Here"])
    assert match is None
