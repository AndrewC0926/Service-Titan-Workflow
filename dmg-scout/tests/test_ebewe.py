"""LA EBEWE benchmark import and the address-text join onto retrofit
buildings. See app/pipeline/ebewe.py's module docstring for the measured
comparison against a Census-geocoder spatial join (worse, not built) and
the three exclusion criteria the join below implements.

The fetch HTTP call is mocked (respx); these tests are about parsing,
upsert-by-(building_id, program_year), and the join/exclusion logic, not
live network."""
from datetime import date, datetime

import httpx
import respx

from app.http import PoliteClient
from app.models import EbeweBenchmark, EquipmentPermit, RetrofitBuilding, SourceRun
from app.pipeline.ebewe import ebewe_matches_by_normalized_address, fetch_ebewe_benchmarks
from app.pipeline.regulatory import ARCX_CYCLE_YEARS, ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT, \
    arcx_compliance_status
from app.pipeline.retrofit import build_retrofit_buildings


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _mock_socrata(rows: list[dict]) -> None:
    respx.get(url__startswith="https://data.lacity.org/resource/9yda-i4ya.json").mock(
        return_value=httpx.Response(200, json=rows)
    )


def _row(building_id="111111111111", program_year="2024", apn="004",
        building_address="123 Test St", energy_star_score="42", site_eui="55.5",
        weather_normalized_3="56.1", **extra) -> dict:
    base = {
        "building_id": building_id, "program_year": program_year, "apn": apn,
        "building_address": building_address, "postal_code": "90014",
        "primary_property_1": "Office", "property_gfa_1": "50000", "year_built": "1985",
        "occupancy": "95", "compliance": "COMPLIED", "organization": "Test Org",
        "number_of_buildings": "1", "site_eui": site_eui, "source_eui": "99.9",
        "weather_normalized_3": weather_normalized_3, "weather_normalized_4": "100.5",
        "percent_difference_1": "10.0", "percent_difference_2": "10.0",
        "energy_star_score": energy_star_score, "energy_star_cert_1": "Not Available",
        "total_ghg_emissions": "60.0", "indoor_water_use": "Not Available",
        "indoor_water_use_intensity": "Not Available", "outdoor_water_use": "Not Available",
        "water_use": "500.0",
    }
    base.update(extra)
    return base


# --- fetch / parse ----------------------------------------------------------


@respx.mock
def test_fetch_parses_not_available_as_null_and_writes_a_source_run(db_session, cfg):
    _mock_socrata([_row(energy_star_score="Not Available", site_eui="Not Available")])
    stats = fetch_ebewe_benchmarks(db_session, cfg, fast_client())
    assert stats["fetched"] == 1 and stats["stored"] == 1 and stats["error"] is None

    from sqlmodel import select
    rec = db_session.exec(select(EbeweBenchmark)).one()
    assert rec.energy_star_score is None and rec.site_eui is None
    assert rec.weather_normalized_site_eui == 56.1
    assert rec.ain_last3 == "004"

    run = db_session.exec(select(SourceRun)).one()
    assert run.source == "la_ebewe_benchmarking" and run.ok is True and run.records_fetched == 1


@respx.mock
def test_fetch_keeps_every_program_year_not_just_the_latest(db_session, cfg):
    _mock_socrata([_row(program_year="2020", energy_star_score="30"),
                  _row(program_year="2024", energy_star_score="50")])
    fetch_ebewe_benchmarks(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = db_session.exec(select(EbeweBenchmark)).all()
    assert len(rows) == 2, "an annual filing per building_id must not overwrite prior years"
    assert {r.program_year for r in rows} == {2020, 2024}


@respx.mock
def test_fetch_upserts_same_building_year_on_rerun(db_session, cfg):
    _mock_socrata([_row(energy_star_score="30")])
    fetch_ebewe_benchmarks(db_session, cfg, fast_client())
    _mock_socrata([_row(energy_star_score="80")])
    fetch_ebewe_benchmarks(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = db_session.exec(select(EbeweBenchmark)).all()
    assert len(rows) == 1, "same (building_id, program_year) must update in place, not duplicate"
    assert rows[0].energy_star_score == 80


@respx.mock
def test_fetch_skips_rows_missing_half_the_unique_key(db_session, cfg):
    _mock_socrata([_row(building_id=None), _row(program_year=None), _row()])
    stats = fetch_ebewe_benchmarks(db_session, cfg, fast_client())
    from sqlmodel import select
    assert len(db_session.exec(select(EbeweBenchmark)).all()) == 1
    assert stats["fetched"] == 3 and stats["stored"] == 1


# --- join / exclusion logic --------------------------------------------------


def _benchmark(building_id, program_year, address, ain_last3, **kw) -> EbeweBenchmark:
    return EbeweBenchmark(source="la_ebewe_benchmarking", building_id=building_id,
                          program_year=program_year, building_address=address,
                          ain_last3=ain_last3, source_url="https://x", **kw)


def test_clean_match_requires_address_and_checksum_agreement(db_session, cfg):
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004", energy_star_score=42))
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(db_session, {"1234567004": "123 Main St"})
    key = list(matches)
    assert len(key) == 1
    m = matches[key[0]]
    assert m["ebewe_matched"] is True
    assert m["ebewe_building_id"] == "B1" and m["ebewe_energy_star_score"] == 42


def test_checksum_mismatch_is_excluded_not_guessed(db_session, cfg):
    """Address text agrees, but the documented last-3-of-AIN checksum
    disagrees -- abstain, same discipline as the masked-APN exclusion in
    app.pipeline.retrofit:find_replacement_candidates."""
    db_session.add(_benchmark("B1", 2024, "123 Main St", "999"))  # apn ends 004, not 999
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(db_session, {"1234567004": "123 Main St"})
    assert matches == {}


def test_forward_ambiguous_address_is_excluded(db_session, cfg):
    """One EBEWE building_id's address matches TWO different retrofit apns
    -- which one? Not decidable from text; drop rather than guess."""
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004"))
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(
        db_session, {"1234567004": "123 Main St", "9999999004": "123 Main St"})
    assert matches == {}


def test_reverse_ambiguous_address_is_excluded_not_dict_overwritten(db_session, cfg):
    """TWO distinct EBEWE building_ids share a normalized address (a real
    shape -- e.g. a multi-building complex filing separate EBEWE
    registrations under one street address) -- must be dropped explicitly,
    not silently resolved by whichever wins a dict overwrite."""
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004"))
    db_session.add(_benchmark("B2", 2024, "123 Main St", "004"))
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(db_session, {"1234567004": "123 Main St"})
    assert matches == {}


def test_only_latest_program_year_is_used_for_the_join(db_session, cfg):
    db_session.add(_benchmark("B1", 2018, "123 Main St", "004", energy_star_score=10))
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004", energy_star_score=90))
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(db_session, {"1234567004": "123 Main St"})
    m = list(matches.values())[0]
    assert m["ebewe_program_year"] == 2024 and m["ebewe_energy_star_score"] == 90


def test_reverse_ambiguity_sees_addresses_already_on_disk_from_another_population(db_session, cfg):
    """The reverse-ambiguity check must combine THIS build's candidate
    addresses with whatever's already in retrofit_buildings from the other
    population's last run -- not just the current call's own candidates,
    or a cold build would under-detect it and a later population's build
    would silently steal the match."""
    db_session.add(RetrofitBuilding(apn="9999999004", population="recently_active", address="123 Main St"))
    db_session.commit()
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004"))
    db_session.commit()
    matches = ebewe_matches_by_normalized_address(db_session, {"1234567004": "123 Main St"})
    assert matches == {}, "two apns (one on disk, one in this build) share the address -- still ambiguous"


# --- A/RCx compliance cycle ---------------------------------------------------


def test_arcx_compliance_status_matches_lamc_table_9708_2():
    """LAMC Table 9708.2, verbatim from LADBS's own A/RCx FAQ PDF (see
    app/pipeline/regulatory.py's citation): initial compliance year per
    last digit, then every 5 years thereafter. Cross-checked against that
    same PDF's own prose (FAQ #1): "the next A/RCx compliance due date for
    Building IDs ending in 0 or 1 is still December 1, 2026... ending in
    2 or 3 is still December 1, 2027"."""
    expected_initial = {0: 2021, 1: 2021, 2: 2022, 3: 2022, 4: 2023, 5: 2023,
                        6: 2024, 7: 2024, 8: 2025, 9: 2025}
    assert ARCX_INITIAL_COMPLIANCE_YEAR_BY_LAST_DIGIT == expected_initial
    assert ARCX_CYCLE_YEARS == 5

    as_of = date(2026, 8, 15)
    for digit, initial in expected_initial.items():
        status = arcx_compliance_status(f"12345678{digit}", as_of=as_of)
        assert status["last_digit"] == digit
        assert status["initial_compliance_year"] == initial
        # every cycle year for this digit is initial, initial+5, initial+10, ...
        due = (as_of.year - initial) % ARCX_CYCLE_YEARS == 0 and as_of.year >= initial
        assert status["due_this_year"] == due
        assert status["next_compliance_date"].month == 12 and status["next_compliance_date"].day == 1

    # Direct checks against the FAQ PDF's own stated dates, not just the
    # table-derived loop above.
    d0 = arcx_compliance_status("1234567890", as_of=as_of)
    assert d0["due_this_year"] is True and d0["next_compliance_date"] == date(2026, 12, 1)
    d2 = arcx_compliance_status("1234567892", as_of=as_of)
    assert d2["due_this_year"] is False and d2["next_compliance_date"] == date(2027, 12, 1)


def test_arcx_compliance_status_none_for_missing_or_non_digit_id():
    assert arcx_compliance_status(None) is None
    assert arcx_compliance_status("") is None
    assert arcx_compliance_status("BUILDING-X") is None


# --- integration: attaches onto RetrofitBuilding, never touches rank_score --


@respx.mock
def test_build_retrofit_buildings_attaches_ebewe_match_and_never_moves_rank_score(db_session, cfg):
    """The core design constraint: an EBEWE match must populate the display
    columns and the A/RCx flag, but rank_score for a matched row must be
    IDENTICAL to the same row with no match at all -- energy performance is
    a subset-only tie-breaker applied by the web layer, never a
    rank_buildings() input. See app/assumptions.py's "Benchmark-to-building
    join method and coverage" entry."""
    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(
        return_value=httpx.Response(200, json={"features": []})
    )
    db_session.add(EquipmentPermit(
        source="la_city_mechanical", permit_nbr="P1", apn="1234567004",
        address="123 Main St", county="Los Angeles", state="CA",
        permit_type="HVAC", permit_sub_type="Commercial", status_desc="Issued",
        issue_date=datetime(2015, 1, 1), work_desc="Replace RTU", source_url="https://x",
    ))
    db_session.add(_benchmark("B1", 2024, "123 Main St", "004",
                              energy_star_score=12, site_eui=80.0, weather_normalized_site_eui=81.0))
    db_session.commit()

    build_retrofit_buildings(db_session, cfg, fast_client())

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding).where(RetrofitBuilding.apn == "1234567004")).one()
    assert row.ebewe_matched is True
    assert row.ebewe_energy_star_score == 12
    assert row.ebewe_weather_normalized_site_eui == 81.0
    assert row.ebewe_building_id == "B1"

    from app.pipeline.retrofit import rank_buildings
    with_match = rank_buildings(service_life_status=row.service_life_status, sqft=row.sqft,
                                sb1206_trigger_status=row.sb1206_trigger_status,
                                ebewe_candidate=row.ebewe_candidate, carb_candidate=row.carb_candidate,
                                service_life_years_past=row.service_life_years_past)
    assert with_match == row.rank_score, "rank_buildings() takes no EBEWE-benchmark argument at all"
