"""Block 3 Item 4 (Master Plan v3.2 section 13): the Deadlines page,
grouped by regulation, nearest date first (app.pipeline.deadlines)."""
import base64
from datetime import datetime

import pytest
from fastapi.testclient import TestClient

from app.db import get_session
from app.models import Ab869Plan, HospitalBuilding, RetrofitBuilding
from app.pipeline.deadlines import SB1206_VIRGIN_CUTOFF, deadlines_by_regulation
from app.web.main import app

AUTH = {"Authorization": "Basic " + base64.b64encode(b"andrew:testpw").decode()}


@pytest.fixture()
def client(db_session, monkeypatch):
    monkeypatch.setenv("DASHBOARD_PASSWORD", "testpw")
    app.dependency_overrides[get_session] = lambda: db_session
    yield TestClient(app)
    app.dependency_overrides.clear()


def test_four_regulation_groups_always_present(db_session):
    groups = deadlines_by_regulation(db_session)
    assert set(groups) == {"AB 869", "SB 1206", "EBEWE", "Rule 1146.2"}


def test_ab869_npc_outstanding_facility_appears_with_earliest_deadline_year(db_session):
    db_session.add(HospitalBuilding(
        perm_id="P1", building_nbr="B1", facility_name="Test Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2035,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(HospitalBuilding(
        perm_id="P1", building_nbr="B2", facility_name="Test Hospital",
        county="Los Angeles", npc_rating="3", npc_deadline_year=2030,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P1", plan_status="In Progress",
                             source_pdf_path="/x.pdf", source_pdf_hash="h1"))
    db_session.commit()

    rows = deadlines_by_regulation(db_session)["AB 869"]
    assert len(rows) == 1
    assert rows[0].date == datetime(2030, 1, 1)  # earliest of the two outstanding buildings
    assert "2 of 2" in rows[0].exposure
    assert rows[0].detail_url == "/ab869/P1"


def test_ab869_facility_with_all_buildings_compliant_is_excluded(db_session):
    db_session.add(HospitalBuilding(
        perm_id="P2", building_nbr="B1", facility_name="Compliant Hospital",
        county="Los Angeles", npc_rating="5", npc_deadline_year=None,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P2", plan_status="Complete",
                             source_pdf_path="/x.pdf", source_pdf_hash="h2"))
    db_session.commit()
    assert deadlines_by_regulation(db_session)["AB 869"] == []


def test_sb1206_row_uses_the_fixed_virgin_cutoff_date(db_session):
    db_session.add(RetrofitBuilding(
        apn="1-1-1", address="1 Test Way", sb1206_trigger_status="in_effect",
        sb1206_detail="R-410A, installed 2015",
    ))
    db_session.commit()
    rows = deadlines_by_regulation(db_session)["SB 1206"]
    assert len(rows) == 1
    assert rows[0].date == SB1206_VIRGIN_CUTOFF
    assert rows[0].exposure == "R-410A, installed 2015"
    assert rows[0].eligible_line is None and rows[0].osp_status is None


def test_sb1206_building_with_no_trigger_status_is_excluded(db_session):
    db_session.add(RetrofitBuilding(apn="2-2-2", sb1206_trigger_status=None))
    db_session.commit()
    assert deadlines_by_regulation(db_session)["SB 1206"] == []


def test_ebewe_row_uses_the_arcx_compliance_date(db_session):
    db_session.add(RetrofitBuilding(
        apn="3-3-3", address="3 Test Way", ebewe_matched=True,
        ebewe_arcx_next_compliance_date=datetime(2027, 12, 1), ebewe_arcx_due_this_year=False,
    ))
    db_session.commit()
    rows = deadlines_by_regulation(db_session)["EBEWE"]
    assert len(rows) == 1
    assert rows[0].date == datetime(2027, 12, 1)
    assert "not this year" in rows[0].exposure


def test_ebewe_unmatched_building_is_excluded(db_session):
    db_session.add(RetrofitBuilding(apn="4-4-4", ebewe_candidate=True, ebewe_matched=False))
    db_session.commit()
    assert deadlines_by_regulation(db_session)["EBEWE"] == []


def test_rule_1146_2_boiler_with_known_age(db_session):
    db_session.add(RetrofitBuilding(
        apn="5-5-5", address="5 Test Way", equipment_type="boiler", equipment_age_years=22.0,
    ))
    db_session.commit()
    rows = deadlines_by_regulation(db_session)["Rule 1146.2"]
    assert len(rows) == 1
    assert rows[0].date is None
    assert "22" in rows[0].exposure


def test_rule_1146_2_boiler_with_unknown_age_abstains_rather_than_guessing(db_session):
    db_session.add(RetrofitBuilding(apn="6-6-6", equipment_type="boiler",
                                    equipment_age_years=None, building_age_years=None))
    db_session.commit()
    rows = deadlines_by_regulation(db_session)["Rule 1146.2"]
    assert len(rows) == 1
    assert "ABSTAIN" in rows[0].exposure


def test_rule_1146_2_non_boiler_equipment_is_excluded(db_session):
    db_session.add(RetrofitBuilding(apn="7-7-7", equipment_type="air_handling_unit"))
    db_session.commit()
    assert deadlines_by_regulation(db_session)["Rule 1146.2"] == []


def test_rows_within_a_group_sort_nearest_date_first(db_session):
    db_session.add(HospitalBuilding(
        perm_id="P3", building_nbr="B1", facility_name="Later Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2040,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P3", plan_status="In Progress",
                             source_pdf_path="/x.pdf", source_pdf_hash="h3"))
    db_session.add(HospitalBuilding(
        perm_id="P4", building_nbr="B1", facility_name="Sooner Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2028,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P4", plan_status="In Progress",
                             source_pdf_path="/x.pdf", source_pdf_hash="h4"))
    db_session.commit()

    rows = deadlines_by_regulation(db_session)["AB 869"]
    assert [r.account_or_building for r in rows] == ["Sooner Hospital", "Later Hospital"]


def test_rows_with_no_date_sort_after_dated_rows_within_their_group(db_session):
    """An AB 869 facility outstanding on NPC but with no derivable deadline
    year (npc_deadline_year null on every outstanding building) has a real
    row_date of None -- it must sort AFTER a facility with a real date, not
    crash the sort and not sort first by accident."""
    db_session.add(HospitalBuilding(
        perm_id="P5", building_nbr="B1", facility_name="No Deadline Year Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=None,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P5", plan_status="In Progress",
                             source_pdf_path="/x.pdf", source_pdf_hash="h5"))
    db_session.add(HospitalBuilding(
        perm_id="P6", building_nbr="B1", facility_name="Dated Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2031,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P6", plan_status="In Progress",
                             source_pdf_path="/x.pdf", source_pdf_hash="h6"))
    db_session.commit()

    rows = deadlines_by_regulation(db_session)["AB 869"]
    assert [r.account_or_building for r in rows] == ["Dated Hospital", "No Deadline Year Hospital"]
    assert rows[1].date is None


def test_every_detail_url_resolves_to_a_real_route(client, db_session):
    """Regression: every detail_url in this module previously pointed at a
    URL that did not exist (/ab869-facility/... and /retrofit-building/...
    -- the real routes are /ab869/... and /retrofit/building/...), caught
    only by actually hitting each one through the live app, not by
    checking the string against itself."""
    db_session.add(HospitalBuilding(
        perm_id="P7", building_nbr="B1", facility_name="Route Check Hospital",
        county="Los Angeles", npc_rating="2", npc_deadline_year=2030,
        snapshot_date=datetime(2026, 1, 1), source_url="https://x",
    ))
    db_session.add(Ab869Plan(perm_id="P7", plan_status="Not Approved",
                             source_pdf_path="/x.pdf", source_pdf_hash="h7"))
    db_session.add(RetrofitBuilding(apn="10-10-10", address="10 Test Way",
                                    sb1206_trigger_status="in_effect", ebewe_matched=True,
                                    ebewe_arcx_next_compliance_date=datetime(2027, 12, 1),
                                    equipment_type="boiler"))
    db_session.commit()

    groups = deadlines_by_regulation(db_session)
    for regulation, rows in groups.items():
        for row in rows:
            r = client.get(row.detail_url, headers=AUTH)
            assert r.status_code == 200, f"{regulation} row detail_url {row.detail_url!r} returned {r.status_code}"
