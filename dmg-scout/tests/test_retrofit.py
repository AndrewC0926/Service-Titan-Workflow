"""Retrofit building pipeline: dedup permits to buildings, join assessor
characteristics, evaluate triggers, rank. See app/pipeline/retrofit.py.

The assessor characteristics HTTP call is mocked (respx) -- these tests are
about the dedup/join/ranking logic, not live network."""
from datetime import datetime, timedelta

import httpx
import respx

from app.http import PoliteClient
from app.models import AssessorCandidate, EquipmentPermit, RetrofitBuilding
from app.pipeline.regulatory import infer_refrigerant
from app.pipeline.retrofit import (
    build_retrofit_buildings, find_replacement_candidates, infer_equipment_type, rank_buildings,
)


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _permit(apn, issue_date, work_desc, permit_nbr=None):
    return EquipmentPermit(
        source="la_city_mechanical", permit_nbr=permit_nbr or f"P-{apn}-{issue_date}",
        apn=apn, address="123 Test St", county="Los Angeles", state="CA",
        permit_type="HVAC", permit_sub_type="Commercial", status_desc="Issued",
        issue_date=issue_date, work_desc=work_desc,
        inferred_refrigerant=infer_refrigerant(issue_date.year)["refrigerant"],
        source_url="https://x",
    )


def _mock_assessor(features: list[dict]) -> None:
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json={"features": [{"attributes": a} for a in features]})
    )


# --- pure functions --------------------------------------------------------


def test_infer_equipment_type_handles_plurals():
    assert infer_equipment_type("install 4 new RTUs") == "packaged_rooftop"
    assert infer_equipment_type("New HVAC system including split units") == "split_dx"
    assert infer_equipment_type("2 RTUs, 1 Walk-in cooler") == "packaged_rooftop"


def test_infer_equipment_type_no_match_is_null_not_guessed():
    assert infer_equipment_type("Wall Heater") is None
    assert infer_equipment_type("") is None
    assert infer_equipment_type(None) is None


def test_rank_buildings_overdue_beats_not_due():
    overdue = rank_buildings(service_life_status="overdue", sqft=50000,
                             sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    not_due = rank_buildings(service_life_status="not_due", sqft=50000,
                             sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    assert overdue > not_due


def test_rank_buildings_unknown_equipment_gets_zero_life_weight():
    unknown = rank_buildings(service_life_status=None, sqft=50000,
                             sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    known_not_due = rank_buildings(service_life_status="not_due", sqft=50000,
                                   sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    assert unknown < known_not_due


def test_rank_buildings_urgency_beats_any_size_never_the_reverse():
    """The bug this regression-tests: a first version weighted service life,
    size and regulatory proximity into one linear blend, and a big enough
    'not_due' skyscraper (recently-serviced) outscored a small 'overdue'
    building. That put the buildings LEAST worth calling at the top. Service
    life status must be a tier no amount of size or regulatory pressure can
    cross -- same fix as ladder.py's reachability-first sort."""
    tiny_overdue = rank_buildings(service_life_status="overdue", sqft=2000,
                                  sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    huge_not_due = rank_buildings(service_life_status="not_due", sqft=2_000_000,
                                  sb1206_trigger_status="in_effect", ebewe_candidate=True, carb_candidate=True)
    assert tiny_overdue > huge_not_due

    small_due = rank_buildings(service_life_status="due", sqft=5000,
                               sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    huge_approaching = rank_buildings(service_life_status="approaching", sqft=2_000_000,
                                      sb1206_trigger_status="in_effect", ebewe_candidate=True, carb_candidate=True)
    assert small_due > huge_approaching


def test_rank_buildings_size_is_capped():
    huge = rank_buildings(service_life_status="not_due", sqft=50_000_000,
                          sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    reasonable = rank_buildings(service_life_status="not_due", sqft=1_000_000,
                                sb1206_trigger_status=None, ebewe_candidate=False, carb_candidate=False)
    assert huge - reasonable < 0.05, "log-scaled and capped, not linear"


# --- full pipeline ----------------------------------------------------------


@respx.mock
def test_dedup_collapses_multiple_permits_to_one_building(db_session, cfg):
    old = datetime.utcnow() - timedelta(days=365 * 20)
    recent = datetime.utcnow() - timedelta(days=365 * 5)
    db_session.add(_permit("1111111111", old, "Replace boiler", "P1"))
    db_session.add(_permit("1111111111", recent, "REPLACE (2) 6-ton RTU", "P2"))
    db_session.commit()
    _mock_assessor([{"AIN": "1111111111", "PropertyLocation": "123 Test St",
                     "UseCode": "2100", "UseCodeDescChar1": "Commercial",
                     "YearBuilt": "1980", "SQFTmain": 30000}])

    stats = build_retrofit_buildings(db_session, cfg, fast_client())
    assert stats["distinct_buildings"] == 1
    assert stats["permits_considered"] == 2

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.permit_count == 2
    # the LATEST permit's equipment/install year wins, not the oldest
    assert row.equipment_type == "packaged_rooftop"
    assert row.latest_permit_nbr == "P2"


@respx.mock
def test_masked_apn_permits_are_excluded_from_dedup(db_session, cfg):
    db_session.add(_permit("2373009***", datetime.utcnow(), "Heat Pump", "P1"))
    db_session.commit()
    _mock_assessor([])

    stats = build_retrofit_buildings(db_session, cfg, fast_client())
    assert stats["permits_masked_apn_skipped"] == 1
    assert stats["distinct_buildings"] == 0


@respx.mock
def test_assessor_match_rate_reported_not_silently_dropped(db_session, cfg):
    db_session.add(_permit("3333333333", datetime.utcnow(), "RTU replacement", "P1"))
    db_session.add(_permit("4444444444", datetime.utcnow(), "RTU replacement", "P2"))
    db_session.commit()
    _mock_assessor([{"AIN": "3333333333", "PropertyLocation": "x", "UseCode": "2100",
                     "UseCodeDescChar1": "Commercial", "YearBuilt": "2000", "SQFTmain": 10000}])
    # 4444444444 not in the mocked response -> unmatched

    stats = build_retrofit_buildings(db_session, cfg, fast_client())
    assert stats["assessor_matched"] == 1
    assert stats["assessor_unmatched"] == 1
    assert stats["distinct_buildings"] == 2, "unmatched building still gets a row, just without sqft/use_code"

    from sqlmodel import select
    rows = {r.apn: r for r in db_session.exec(select(RetrofitBuilding)).all()}
    assert rows["4444444444"].sqft is None
    assert rows["3333333333"].sqft == 10000


@respx.mock
def test_service_life_uses_default_ownership_when_unknown(db_session, cfg):
    """No owner data exists -- must fall back to private_commercial (the
    longest cycle), never guess federal/state_municipal."""
    old = datetime.utcnow() - timedelta(days=365 * 16)
    db_session.add(_permit("5555555555", old, "Replace RTU", "P1"))
    db_session.commit()
    _mock_assessor([{"AIN": "5555555555", "PropertyLocation": "x", "UseCode": "3300",
                     "UseCodeDescChar1": "Warehouse", "YearBuilt": "1990", "SQFTmain": 40000}])

    build_retrofit_buildings(db_session, cfg, fast_client())

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.service_life_status in ("due", "overdue")  # 16yr on a 15-17yr band
    assert "private_commercial" in row.service_life_basis
    assert "no owner data available" in row.service_life_basis


@respx.mock
def test_regulatory_flags_carry_onto_the_building(db_session, cfg):
    old = datetime.utcnow() - timedelta(days=365 * 6)  # 2020ish -> R-410A window
    db_session.add(_permit("6666666666", old, "Replace RTU", "P1"))
    db_session.add(AssessorCandidate(
        source="la_county_assessor", ain="6666666666",
        trigger_key="carb_refrigerant_management_program", use_code="2100",
        source_url="https://x"))
    db_session.add(AssessorCandidate(
        source="la_county_assessor", ain="6666666666",
        trigger_key="la_ebewe_audit_retrocommissioning", source_url="https://x"))
    db_session.commit()
    _mock_assessor([{"AIN": "6666666666", "PropertyLocation": "x", "UseCode": "2100",
                     "UseCodeDescChar1": "Commercial", "YearBuilt": "2000", "SQFTmain": 25000}])

    build_retrofit_buildings(db_session, cfg, fast_client())

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.carb_candidate is True
    assert row.carb_use_code == "2100"
    assert row.ebewe_candidate is True
    assert row.sb1206_trigger_status in ("upcoming", "in_effect")


@respx.mock
def test_rerun_replaces_not_appends(db_session, cfg):
    db_session.add(_permit("7777777777", datetime.utcnow(), "RTU replacement", "P1"))
    db_session.commit()
    _mock_assessor([{"AIN": "7777777777", "PropertyLocation": "x", "UseCode": "2100",
                     "UseCodeDescChar1": "Commercial", "YearBuilt": "2000", "SQFTmain": 10000}])

    build_retrofit_buildings(db_session, cfg, fast_client())
    build_retrofit_buildings(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = db_session.exec(select(RetrofitBuilding)).all()
    assert len(rows) == 1


# --- absence query: the real retrofit opportunity ---------------------


def _mock_commercial_parcels(features: list[dict]) -> None:
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json={"features": [{"attributes": a} for a in features]})
    )


@respx.mock
def test_permitted_parcel_excluded_from_candidates(db_session, cfg):
    """A permit is evidence someone already replaced -- that parcel must
    NOT appear as a replacement candidate, regardless of how old the
    building is."""
    db_session.add(_permit("8888888888", datetime.utcnow(), "RTU replacement", "P1"))
    db_session.commit()
    _mock_commercial_parcels([
        {"AIN": "8888888888", "PropertyLocation": "has a permit", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1980", "SQFTmain": 20000},
        {"AIN": "9999999999", "PropertyLocation": "no permit at all", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1975", "SQFTmain": 15000},
    ])

    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["commercial_parcels_scanned"] == 2
    assert stats["already_permitted_excluded"] == 1
    assert stats["replacement_candidates"] == 1

    from sqlmodel import select
    rows = db_session.exec(select(RetrofitBuilding)).all()
    apns = {r.apn for r in rows}
    assert "8888888888" not in apns
    assert "9999999999" in apns


@respx.mock
def test_candidate_rows_carry_no_equipment_type_or_regulatory_claim(db_session, cfg):
    """No permit text exists for these rows -- must not fabricate an
    equipment type, tonnage, or SB 1206 status."""
    _mock_commercial_parcels([
        {"AIN": "1010101010", "PropertyLocation": "x", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 30000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.population == "replacement_candidate"
    assert row.equipment_type is None
    assert row.mined_tons_each is None
    assert row.sb1206_trigger_status is None
    assert row.service_life_status is None
    assert "No mechanical permit on record" in row.service_life_basis
    assert row.building_age_years is not None


@respx.mock
def test_candidates_do_not_clobber_recently_active_population(db_session, cfg):
    """The two populations must coexist -- rebuilding one must not delete
    rows from the other."""
    db_session.add(_permit("2020202020", datetime.utcnow(), "RTU replacement", "P1"))
    db_session.commit()
    _mock_assessor([{"AIN": "2020202020", "PropertyLocation": "x", "UseCode": "2100",
                     "UseCodeDescChar1": "Commercial", "YearBuilt": "2000", "SQFTmain": 10000}])
    build_retrofit_buildings(db_session, cfg, fast_client())

    _mock_commercial_parcels([
        {"AIN": "3030303030", "PropertyLocation": "x", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 30000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = {r.apn: r for r in db_session.exec(select(RetrofitBuilding)).all()}
    assert rows["2020202020"].population == "recently_active"
    assert rows["3030303030"].population == "replacement_candidate"
    assert len(rows) == 2


@respx.mock
def test_rerun_replaces_only_its_own_population(db_session, cfg):
    _mock_commercial_parcels([
        {"AIN": "4040404040", "PropertyLocation": "x", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 30000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())
    find_replacement_candidates(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = db_session.exec(select(RetrofitBuilding)).all()
    assert len(rows) == 1
