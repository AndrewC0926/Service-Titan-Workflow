"""LA mechanical permit import (Phase 6c/6d) and assessor candidate import
(Phase 6e) — mocked HTTP, no live network. See app/pipeline/permits.py and
app/pipeline/assessor.py."""
import httpx
import respx

from app.http import PoliteClient
from app.models import AssessorCandidate, EquipmentPermit
from app.pipeline.assessor import fetch_carb_candidates, fetch_ebewe_candidates
from app.pipeline.permits import fetch_la_mechanical_permits


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


@respx.mock
def test_fetch_permits_infers_refrigerant_and_mines_tonnage(db_session, cfg):
    payload = [
        {"permit_nbr": "22044-90000-00017", "primary_address": "123 Main St",
         "permit_type": "HVAC", "permit_sub_type": "Commercial", "status_desc": "Issued",
         "issue_date": "2022-01-03T00:00:00.000",
         "work_desc": "REPLACE (2) 6-ton HEAT PUMP PACKAGE UNIT - ROOF", "apn": "5210013004"},
        {"permit_nbr": "26044-90000-00099", "primary_address": "456 Oak Ave",
         "permit_type": "HVAC", "permit_sub_type": "Commercial", "status_desc": "Issued",
         "issue_date": "2026-01-03T00:00:00.000",
         "work_desc": "install 4 new RTUs", "apn": None},
    ]
    respx.get(url__startswith="https://data.lacity.org/resource/67is-svtd.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    stats = fetch_la_mechanical_permits(db_session, cfg, fast_client())
    assert stats["fetched"] == 2 and stats["stored"] == 2
    assert stats["sb1206_flagged"] == 1, "only the 2022-install permit falls in the R-410A window"

    from sqlmodel import select
    rows = {p.permit_nbr: p for p in db_session.exec(select(EquipmentPermit)).all()}
    old = rows["22044-90000-00017"]
    assert old.inferred_refrigerant == "R-410A"
    assert old.equipment_count == 2 and old.tons_each == 6.0
    assert old.sb1206_trigger_status in ("upcoming", "in_effect")

    recent = rows["26044-90000-00099"]
    assert recent.inferred_refrigerant is None, "2026 install is at/after the A2L transition, not inferred R-410A"
    assert recent.equipment_count == 4


@respx.mock
def test_fetch_permits_is_idempotent_on_permit_nbr(db_session, cfg):
    payload = [{"permit_nbr": "22044-90000-00017", "primary_address": "123 Main St",
               "permit_type": "HVAC", "permit_sub_type": "Commercial", "status_desc": "Issued",
               "issue_date": "2022-01-03T00:00:00.000", "work_desc": "Heat Pump", "apn": None}]
    respx.get(url__startswith="https://data.lacity.org/resource/67is-svtd.json").mock(
        return_value=httpx.Response(200, json=payload)
    )
    fetch_la_mechanical_permits(db_session, cfg, fast_client())
    fetch_la_mechanical_permits(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = db_session.exec(select(EquipmentPermit)).all()
    assert len(rows) == 1, "re-fetching the same permit_nbr must update, not duplicate"


@respx.mock
def test_fetch_permits_reports_error_rather_than_raising(db_session, cfg):
    respx.get(url__startswith="https://data.lacity.org/resource/67is-svtd.json").mock(
        return_value=httpx.Response(503)
    )
    stats = fetch_la_mechanical_permits(db_session, cfg, fast_client())
    assert stats["stored"] == 0
    assert "error" in stats


@respx.mock
def test_fetch_carb_candidates_stores_use_code_matches(db_session, cfg):
    payload = {"features": [
        {"attributes": {"AIN": "2007024038", "UseCode": "1420", "UseCodeDescChar1": "Commercial",
                        "YearBuilt": "1962", "SQFTmain": 1786, "PropertyLocation": "23400 Lake Manor Dr"}},
        {"attributes": {"AIN": "2007024046", "UseCode": "2100", "UseCodeDescChar1": "Commercial",
                        "YearBuilt": "1955", "SQFTmain": 1950, "PropertyLocation": "23360 Lake Manor Dr"}},
    ]}
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json=payload)
    )
    stats = fetch_carb_candidates(db_session, cfg, fast_client(), max_pages=1, page_size=500)
    assert stats["stored"] == 2

    from sqlmodel import select
    rows = db_session.exec(select(AssessorCandidate)).all()
    assert {r.trigger_key for r in rows} == {"carb_refrigerant_management_program"}
    assert {r.use_code for r in rows} == {"1420", "2100"}


@respx.mock
def test_fetch_ebewe_candidates_are_a_distinct_trigger_key(db_session, cfg):
    payload = {"features": [
        {"attributes": {"AIN": "9999999999", "UseCode": "3300", "UseCodeDescChar1": "Warehouse",
                        "YearBuilt": "1990", "SQFTmain": 45000, "PropertyLocation": "1 Big Building Way"}},
    ]}
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json=payload)
    )
    stats = fetch_ebewe_candidates(db_session, cfg, fast_client(), max_pages=1, page_size=500)
    assert stats["stored"] == 1

    from sqlmodel import select
    row = db_session.exec(select(AssessorCandidate)).one()
    assert row.trigger_key == "la_ebewe_audit_retrocommissioning"
    assert row.sqft == 45000


@respx.mock
def test_same_parcel_two_different_triggers_both_stored(db_session, cfg):
    """A parcel can be a CARB candidate AND an EBEWE candidate at once --
    the unique constraint is (source, ain, trigger_key), not (source, ain)."""
    payload = {"features": [
        {"attributes": {"AIN": "1111111111", "UseCode": "1400", "UseCodeDescChar1": "Commercial",
                        "YearBuilt": "2005", "SQFTmain": 55000, "PropertyLocation": "1 Supermarket Way"}},
    ]}
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json=payload)
    )
    fetch_carb_candidates(db_session, cfg, fast_client(), max_pages=1, page_size=500)
    fetch_ebewe_candidates(db_session, cfg, fast_client(), max_pages=1, page_size=500)

    from sqlmodel import select
    rows = db_session.exec(select(AssessorCandidate).where(AssessorCandidate.ain == "1111111111")).all()
    assert len(rows) == 2
    assert {r.trigger_key for r in rows} == {
        "carb_refrigerant_management_program", "la_ebewe_audit_retrocommissioning"}
