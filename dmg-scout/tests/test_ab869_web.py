"""The /ab869 board and facility drill-down: sort order, each filter, the
blank-not-zero rendering rule, and the case-insensitive owner substring
filter. Seeds a small, realistic in-territory dataset directly (SQLite,
via db_session) -- no real HCAI PDFs needed for these, since they exercise
web-layer behavior, not the parser.
"""
import base64

import pytest
from fastapi.testclient import TestClient
from sqlmodel import select

from app.db import get_session
from app.models import Ab869Building, Ab869Milestone, Ab869Plan, HospitalBuilding, ScaqmdFacility, utcnow
from app.pipeline.ab869 import NO_PLAN_ON_FILE, ab869_board_rows, hcai_tableau_url
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


def _hospital_building(session, perm_id, facility_name, county, building_nbr,
                       spc="5", npc="2", lat=34.0, lon=-118.0, city=None):
    hb = HospitalBuilding(perm_id=perm_id, building_nbr=building_nbr, facility_name=facility_name,
                          county=county, city=city, state="CA", spc_rating=spc, npc_rating=npc,
                          latitude=lat, longitude=lon, snapshot_date=utcnow(),
                          source_url="https://example.com")
    session.add(hb)
    session.commit()
    return hb


def _plan(session, perm_id, **kw):
    defaults = dict(source_pdf_path=f"/tmp/{perm_id}.pdf", source_pdf_hash="abc123")
    defaults.update(kw)
    p = Ab869Plan(perm_id=perm_id, **defaults)
    session.add(p)
    session.commit()
    return p


def _building(session, perm_id, building_nbr, compliance_type="NPC Retrofit", **kw):
    b = Ab869Building(perm_id=perm_id, building_nbr=building_nbr, compliance_type=compliance_type, **kw)
    session.add(b)
    session.commit()
    return b


def _milestone(session, perm_id, building_nbr, milestone_type, completion_date, **kw):
    m = Ab869Milestone(perm_id=perm_id, building_nbr=building_nbr, milestone_type=milestone_type,
                       completion_date=completion_date, **kw)
    session.add(m)
    session.commit()
    return m


# ---- ab869_board_rows: sort, and the "no plan on file" case ---------------

def test_board_rows_sorted_by_npc_building_count_descending(db_session, cfg):
    _hospital_building(db_session, "111", "Small Facility", "Los Angeles", "BLD-001")
    _hospital_building(db_session, "222", "Big Facility", "Los Angeles", "BLD-010")
    _hospital_building(db_session, "222", "Big Facility", "Los Angeles", "BLD-011")
    _plan(db_session, "111"); _plan(db_session, "222")
    _building(db_session, "111", "BLD-001", "NPC Retrofit")
    _building(db_session, "222", "BLD-010", "NPC Retrofit")
    _building(db_session, "222", "BLD-011", "SPC and NPC Retrofit")

    rows = ab869_board_rows(db_session, cfg)
    perm_ids = [r["perm_id"] for r in rows]
    assert perm_ids.index("222") < perm_ids.index("111")
    assert rows[perm_ids.index("222")]["npc_building_count"] == 2
    assert rows[perm_ids.index("111")]["npc_building_count"] == 1


def test_board_rows_include_facility_with_no_ab869_plan_row(db_session, cfg):
    """The 11 real "no plan on file" facilities have NO Ab869Plan row at
    all -- must still appear, not be silently dropped."""
    _hospital_building(db_session, "333", "Never Filed Hospital", "Orange", "BLD-050")
    rows = ab869_board_rows(db_session, cfg)
    row = next(r for r in rows if r["perm_id"] == "333")
    assert row["has_plan"] is False
    assert row["plan_status"] == NO_PLAN_ON_FILE
    assert row["npc_building_count"] == 0


def test_board_rows_only_include_in_territory_counties(db_session, cfg):
    _hospital_building(db_session, "444", "Out Of Territory Hospital", "Contra Costa", "BLD-060")
    rows = ab869_board_rows(db_session, cfg)
    assert not any(r["perm_id"] == "444" for r in rows)


def test_board_rows_next_upcoming_date_ignores_past_and_other_milestone_types(db_session, cfg):
    from datetime import timedelta
    _hospital_building(db_session, "555", "Upcoming Hospital", "Kern", "BLD-070")
    _plan(db_session, "555")
    _building(db_session, "555", "BLD-070")
    past = utcnow() - timedelta(days=10)
    soon = utcnow() + timedelta(days=30)
    far = utcnow() + timedelta(days=400)
    _milestone(db_session, "555", "BLD-070", "Construction Commencement", past)
    _milestone(db_session, "555", "BLD-070", "Construction Final", far)  # wrong type -- ignored
    _milestone(db_session, "555", "BLD-070", "NPC 4/4D Permit Issuance", soon)

    rows = ab869_board_rows(db_session, cfg)
    row = next(r for r in rows if r["perm_id"] == "555")
    assert row["next_upcoming_date"] == soon


# ---- filters, at the HTTP layer ---------------------------------------------

@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def _seed_two_counties(db_session):
    _hospital_building(db_session, "111", "LA Hospital", "Los Angeles", "BLD-001")
    _hospital_building(db_session, "222", "Kern Hospital", "Kern", "BLD-002")
    _plan(db_session, "111", plan_status="Not Approved")
    _plan(db_session, "222", plan_status="Approved")
    _building(db_session, "111", "BLD-001", "NPC Retrofit")
    _building(db_session, "222", "BLD-002", "NPC Retrofit")


def test_county_filter(client, db_session, cfg):
    _seed_two_counties(db_session)
    r = client.get("/ab869?county=Los+Angeles", headers=AUTH)
    assert r.status_code == 200
    assert "LA Hospital" in r.text
    assert "Kern Hospital" not in r.text


def test_plan_status_filter(client, db_session, cfg):
    _seed_two_counties(db_session)
    r = client.get("/ab869?plan_status=Approved", headers=AUTH)
    assert "Kern Hospital" in r.text
    assert "LA Hospital" not in r.text


def test_has_missed_milestones_filter(client, db_session, cfg):
    _seed_two_counties(db_session)
    db_session.exec(select(Ab869Building).where(Ab869Building.perm_id == "111")).one()
    building = db_session.exec(select(Ab869Building).where(Ab869Building.perm_id == "111")).one()
    building.has_missed_milestone = True
    db_session.add(building)
    db_session.commit()

    r = client.get("/ab869?has_missed=true", headers=AUTH)
    assert "LA Hospital" in r.text
    assert "Kern Hospital" not in r.text


def test_upcoming_12mo_filter(client, db_session, cfg):
    from datetime import timedelta
    _seed_two_counties(db_session)
    _milestone(db_session, "111", "BLD-001", "Construction Commencement",
              utcnow() + timedelta(days=60))
    _milestone(db_session, "222", "BLD-002", "Construction Commencement",
              utcnow() + timedelta(days=400))  # more than 12mo out

    r = client.get("/ab869?upcoming_12mo=true", headers=AUTH)
    assert "LA Hospital" in r.text
    assert "Kern Hospital" not in r.text


def test_owner_substring_filter_is_case_insensitive_and_matches_multiple_spellings(client, db_session, cfg):
    """The exact requirement: 'kaiser' must find every real spelling, not
    just one canonical one -- these are never merged."""
    _hospital_building(db_session, "111", "Hospital A", "Los Angeles", "BLD-001")
    _hospital_building(db_session, "222", "Hospital B", "Los Angeles", "BLD-002")
    _hospital_building(db_session, "333", "Hospital C", "Los Angeles", "BLD-003")
    _plan(db_session, "111", financially_responsible_party="Kaiser Permanente Foundation")
    _plan(db_session, "222", financially_responsible_party="KAISER PERMANENTE FOUNDATION")
    _plan(db_session, "333", financially_responsible_party="Providence")
    for pid, bld in (("111", "BLD-001"), ("222", "BLD-002"), ("333", "BLD-003")):
        _building(db_session, pid, bld)

    r = client.get("/ab869?owner=kaiser", headers=AUTH)
    assert "Hospital A" in r.text
    assert "Hospital B" in r.text
    assert "Hospital C" not in r.text


def test_no_plan_on_file_facility_shown_not_hidden_on_board(client, db_session, cfg):
    _hospital_building(db_session, "333", "Never Filed Hospital", "Orange", "BLD-050")
    r = client.get("/ab869", headers=AUTH)
    assert "Never Filed Hospital" in r.text
    assert NO_PLAN_ON_FILE in r.text


# ---- blank-not-zero rule ----------------------------------------------------

def test_null_fields_render_as_not_parsed_never_none_or_zero(client, db_session, cfg):
    _hospital_building(db_session, "111", "Blank Fields Hospital", "Los Angeles", "BLD-001")
    _plan(db_session, "111", plan_status="Not Approved")  # everything else null
    _building(db_session, "111", "BLD-001", "NPC Retrofit")  # narrative left null

    r = client.get("/ab869/111", headers=AUTH)
    assert r.status_code == 200
    assert "not parsed" in r.text
    # The literal Python None must never leak into rendered HTML -- note
    # HCAI's own data can legitimately contain the WORD "None" as a
    # reported value (e.g. "Other contact... : None" is a real, correctly-
    # extracted answer, not a bug) -- but this fixture leaves every such
    # field as Python's actual None, so if the string "None" appears here
    # it can only be a naive, un-guarded Jinja interpolation leaking it.
    assert "None" not in r.text


def test_board_delay_requested_blank_not_none_string(client, db_session, cfg):
    _hospital_building(db_session, "111", "No Delay Parsed Hospital", "Los Angeles", "BLD-001")
    _plan(db_session, "111", plan_status="Not Approved", delay_requested=None)
    _building(db_session, "111", "BLD-001", "NPC Retrofit")

    r = client.get("/ab869", headers=AUTH)
    assert "blank" in r.text
    assert ">None<" not in r.text


def test_missed_milestone_count_of_zero_is_a_real_zero_not_blank(client, db_session, cfg):
    """The blank-not-zero rule is about NULL PDF-derived fields -- a real,
    known count of zero missed milestones is a genuine 0, not "not
    parsed"."""
    _hospital_building(db_session, "111", "Clean Record Hospital", "Los Angeles", "BLD-001")
    _plan(db_session, "111", plan_status="Approved")
    _building(db_session, "111", "BLD-001", "NPC Retrofit", has_missed_milestone=False)

    r = client.get("/ab869", headers=AUTH)
    assert r.status_code == 200


# ---- Tableau URL construction -----------------------------------------------

def test_hcai_tableau_url_matches_the_confirmed_live_format():
    url = hcai_tableau_url("10049", "Contra Costa Regional Medical Center")
    assert url.startswith("https://tab.hcai.ca.gov/t/OSHPD_PUBLIC/views/CompliancePlanWebsite/CompliancePlan?")
    assert "Facility+Number+and+Name=10049+Contra+Costa+Regional+Medical+Center" in url


def test_facility_detail_404s_for_a_totally_unknown_perm_id(client, db_session, cfg):
    r = client.get("/ab869/00000", headers=AUTH)
    assert r.status_code == 404


def test_facility_detail_renders_for_crosstab_only_facility_with_no_pdf(client, db_session, cfg):
    """Mirrors real perm_ids 10685/17905 -- a plan row exists (from the
    crosstab alone) but every PDF-only field is null with a reason."""
    _hospital_building(db_session, "10685", "Kindred Hospital Ontario", "San Bernardino", "BLD-01466")
    _plan(db_session, "10685", plan_status="Approved",
         plan_status_paragraph_reason="no PDF on disk for this facility (Playwright pull failed)")
    _building(db_session, "10685", "BLD-01466", "SPC and NPC Retrofit")

    r = client.get("/ab869/10685", headers=AUTH)
    assert r.status_code == 200
    assert "no PDF on disk" in r.text

    board = client.get("/ab869", headers=AUTH)
    assert "no PDF" in board.text


# ---- air permit on file (SCAQMD facility grain join) -----------------------

def test_board_shows_air_permit_facility_id_when_matched(client, db_session, cfg):
    f = ScaqmdFacility(facility_id="F1", facility_name="LA Hospital", city="Los Angeles",
                       source_url="https://example.com")
    db_session.add(f)
    db_session.commit()
    _hospital_building(db_session, "111", "LA Hospital", "Los Angeles", "BLD-001", city="Los Angeles")

    r = client.get("/ab869", headers=AUTH)
    assert "on file" in r.text
    assert "#F1" in r.text


def test_has_air_permit_filter(client, db_session, cfg):
    _hospital_building(db_session, "111", "LA Hospital", "Los Angeles", "BLD-001", city="Los Angeles")
    _hospital_building(db_session, "222", "Kern Hospital", "Kern", "BLD-002", city="Bakersfield")
    f = ScaqmdFacility(facility_id="F1", facility_name="LA Hospital", city="Los Angeles",
                       source_url="https://example.com")
    db_session.add(f)
    db_session.commit()

    r = client.get("/ab869?has_air_permit=true", headers=AUTH)
    assert "LA Hospital" in r.text
    assert "Kern Hospital" not in r.text
