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
    build_retrofit_buildings, estimate_tonnage, find_replacement_candidates, funnel_counts,
    infer_equipment_type, normalize_address, rank_buildings,
)
from app.replacement import generic_service_life


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _permit(apn, issue_date, work_desc, permit_nbr=None, address="123 Test St"):
    return EquipmentPermit(
        source="la_city_mechanical", permit_nbr=permit_nbr or f"P-{apn}-{issue_date}",
        apn=apn, address=address, county="Los Angeles", state="CA",
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


def test_normalize_address_strips_bare_trailing_unit_number():
    """Real sampled LADBS permit addresses (2026-08-09) format a trailing
    unit as a bare token with no SUITE/UNIT/# keyword at all -- a
    keyword-based strip would miss these entirely."""
    assert normalize_address("700 S MAIN ST 14") == "700 S MAIN ST"
    assert normalize_address("6930 N DE CELIS PL UNIT 4") == "6930 N DE CELIS PL"
    assert normalize_address("215 S SANTA FE AVE NO     8") == "215 S SANTA FE AVE"
    assert normalize_address("700 S MAIN ST 8A & 21A") == "700 S MAIN ST"


def test_normalize_address_matches_across_full_and_abbreviated_forms():
    assert normalize_address("640 South Hill Street") == normalize_address("640 S HILL ST")


def test_normalize_address_drops_trailing_city_state_zip():
    """Assessor/candidate addresses carry city/state/zip; permit addresses
    don't. Truncating at the street suffix handles both without needing to
    know which format a given string is in."""
    assert normalize_address("640 S HILL ST  LOS ANGELES CA  90014") == "640 S HILL ST"


def test_normalize_address_no_recognized_suffix_is_unchanged_not_guessed():
    assert normalize_address("APN 5409015027") == "APN 5409015027"
    assert normalize_address(None) is None
    assert normalize_address("") is None


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


def test_rank_buildings_magnitude_discriminates_within_a_saturated_tier():
    """The regression this guards: find_replacement_candidates' population
    is built pre-2010, so ~95% of it reads 'overdue' -- a status that fires
    on nearly every row is a baseline, not a ranking signal. Within that one
    tier, a building decades further past its service life must outrank a
    same-size building barely past it -- magnitude has to be doing real
    work, not just size."""
    barely_overdue = rank_buildings(service_life_status="overdue", sqft=50000,
                                    sb1206_trigger_status=None, ebewe_candidate=False,
                                    carb_candidate=False, service_life_years_past=1)
    decades_overdue = rank_buildings(service_life_status="overdue", sqft=50000,
                                     sb1206_trigger_status=None, ebewe_candidate=False,
                                     carb_candidate=False, service_life_years_past=80)
    assert decades_overdue > barely_overdue


def test_rank_buildings_magnitude_cap_does_not_saturate_at_the_real_top_of_the_list():
    """Regression: the first cap (60yr) was picked without checking the
    data -- on the actual replacement_candidate population the 99th
    percentile of years-past among overdue rows is ~98, so a 60yr cap
    saturated magnitude for nearly every top-50 row (most sit 60-104yr
    past), handing the sort back to size exactly at the top of the list --
    the same failure the gradient was added to fix, just moved to the tail.
    Same size, different magnitude within the top-of-list range (80 vs 95)
    must still produce different scores."""
    eighty = rank_buildings(service_life_status="overdue", sqft=500000, sb1206_trigger_status=None,
                            ebewe_candidate=True, carb_candidate=False, service_life_years_past=80)
    ninety_five = rank_buildings(service_life_status="overdue", sqft=500000, sb1206_trigger_status=None,
                                 ebewe_candidate=True, carb_candidate=False, service_life_years_past=95)
    assert ninety_five > eighty


def test_rank_buildings_magnitude_leads_size_within_a_tier():
    """Before this fix, size (weight 0.7) was the only thing that varied
    once nearly the whole population saturated one tier -- the board was
    sorting on size while labelling it urgency. Magnitude must now outweigh
    a size difference within the same tier."""
    small_but_ancient = rank_buildings(service_life_status="overdue", sqft=5000,
                                       sb1206_trigger_status=None, ebewe_candidate=False,
                                       carb_candidate=False, service_life_years_past=80)
    huge_but_barely_over = rank_buildings(service_life_status="overdue", sqft=2_000_000,
                                          sb1206_trigger_status="in_effect", ebewe_candidate=True,
                                          carb_candidate=True, service_life_years_past=1)
    assert small_but_ancient > huge_but_barely_over


def test_rank_buildings_magnitude_still_cannot_cross_a_tier():
    """Magnitude is a new lead term within a tier, but the original
    invariant (urgency tier beats any within-tier combination, always)
    must still hold -- a maximally-magnitude, maximally-regulated 'due'
    building must not outrank a barely-overdue one."""
    maxed_out_due = rank_buildings(service_life_status="due", sqft=2_000_000,
                                   sb1206_trigger_status="in_effect", ebewe_candidate=True,
                                   carb_candidate=True, service_life_years_past=1000)
    barely_overdue = rank_buildings(service_life_status="overdue", sqft=1, sb1206_trigger_status=None,
                                    ebewe_candidate=False, carb_candidate=False, service_life_years_past=0)
    assert barely_overdue > maxed_out_due


def test_rank_buildings_negative_magnitude_is_not_a_penalty():
    """A building that hasn't reached its service-life window yet has
    negative years_past -- must floor at zero, not push the score negative
    and invert ordering against an unknown-magnitude building."""
    not_due_with_negative_magnitude = rank_buildings(
        service_life_status="not_due", sqft=50000, sb1206_trigger_status=None,
        ebewe_candidate=False, carb_candidate=False, service_life_years_past=-30)
    not_due_no_magnitude_given = rank_buildings(
        service_life_status="not_due", sqft=50000, sb1206_trigger_status=None,
        ebewe_candidate=False, carb_candidate=False, service_life_years_past=None)
    assert not_due_with_negative_magnitude == not_due_no_magnitude_given


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
    # 16yr against a 15-17yr packaged_rooftop band -> 1yr past the low
    # threshold -- the gradient underneath the tier, persisted, not just
    # implied by the categorical status.
    assert row.service_life_years_past == 1.0


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


def _mock_commercial_parcels(features: list[dict], counts: dict | None = None) -> None:
    """Mocks both the funnel COUNT-only queries (returnCountOnly=true) and
    the full feature fetch off the same endpoint -- find_replacement_candidates
    issues both. `counts` lets a test assert specific funnel numbers; any
    stage not given defaults to len(features), which is what every existing
    test implicitly expects (no funnel filtering narrows the mocked set)."""
    counts = counts or {}

    def responder(request):
        params = dict(request.url.params)
        if params.get("returnCountOnly") == "true":
            where = params.get("where", "")
            if "SQFTmain" in where:
                n = counts.get("sqft_floor_survivors", len(features))
            elif "YearBuilt" in where:
                n = counts.get("built_before", len(features))
            else:
                n = counts.get("use_code_match", len(features))
            return httpx.Response(200, json={"count": n})
        return httpx.Response(200, json={"features": [{"attributes": a} for a in features]})

    respx.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(side_effect=responder)


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
def test_masked_apn_permit_excludes_by_address(db_session, cfg):
    """The hole this closes: a permit with a privacy-masked APN is
    invisible to the APN-exact exclusion above, so a building that was
    genuinely serviced still showed up as a replacement candidate. Same
    address, unusable APN -> must exclude by address instead."""
    db_session.add(_permit("4286009***", datetime.utcnow(), "Install 4 TON condenser",
                           address="700 S MAIN ST 14"))
    db_session.commit()
    _mock_commercial_parcels([
        {"AIN": "7000007000", "PropertyLocation": "700 S MAIN ST", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1901", "SQFTmain": 30000},
        {"AIN": "9999999999", "PropertyLocation": "no permit at all", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1975", "SQFTmain": 15000},
    ])

    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["already_permitted_excluded"] == 0, "the masked-APN permit must not count as an APN match"
    assert stats["masked_or_null_apn_address_excluded"] == 1
    assert stats["replacement_candidates"] == 1

    from sqlmodel import select
    apns = {r.apn for r in db_session.exec(select(RetrofitBuilding)).all()}
    assert "7000007000" not in apns
    assert "9999999999" in apns


@respx.mock
def test_null_apn_permit_also_excludes_by_address(db_session, cfg):
    """A missing APN is the same problem as a masked one -- no APN to join
    on, address is the only usable evidence."""
    db_session.add(_permit(None, datetime.utcnow(), "Replace RTU", permit_nbr="NULL-APN-1",
                           address="925 W 8TH ST"))
    db_session.commit()
    _mock_commercial_parcels([
        {"AIN": "8000008000", "PropertyLocation": "925 W 8TH ST", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1960", "SQFTmain": 12000},
    ])

    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["masked_or_null_apn_address_excluded"] == 1
    assert stats["replacement_candidates"] == 0


@respx.mock
def test_masked_apn_permit_does_not_exclude_a_different_address(db_session, cfg):
    """The exclusion must not over-match -- a masked-APN permit at one
    address must not exclude an unrelated building just because both
    happen to be in the permit table."""
    db_session.add(_permit("4286009***", datetime.utcnow(), "Install condenser",
                           address="700 S MAIN ST 14"))
    db_session.commit()
    _mock_commercial_parcels([
        {"AIN": "9999999999", "PropertyLocation": "500 W OLYMPIC BLVD", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1975", "SQFTmain": 15000},
    ])

    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["masked_or_null_apn_address_excluded"] == 0
    assert stats["replacement_candidates"] == 1


@respx.mock
def test_candidate_rows_carry_no_equipment_type_or_permit_verified_tonnage(db_session, cfg):
    """No permit text exists for these rows -- must not fabricate an
    equipment type, permit-verified tonnage, or SB 1206 status (SB 1206
    structurally never applies pre-2010). Service life status IS computed,
    from YearBuilt as a proxy -- but its basis must be unmistakably marked
    as such, never looking like the permit-verified figure."""
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
    assert row.building_age_years is not None

    # A 1970-built building with no permit is decades past any service life
    # band -- must read overdue, and the basis must say so is YearBuilt-derived.
    assert row.service_life_status == "overdue"
    assert "YEARBUILT-DERIVED" in row.service_life_basis
    assert "not permit-verified" in row.service_life_basis
    assert row.equipment_age_years == row.building_age_years

    # The gradient underneath the tier is persisted too, not just implied --
    # decades past the service-life low threshold, not merely "some overdue".
    assert row.service_life_years_past is not None
    assert row.service_life_years_past > 20

    # Sqft-derived tonnage estimate: a band, not a point figure, and
    # clearly not the permit-mined field.
    assert row.estimated_tons_low is not None
    assert row.estimated_tons_high is not None
    assert row.estimated_tons_low < row.estimated_tons_high
    assert "ESTIMATED" in row.estimated_tons_basis
    assert "NOT mined from a permit" in row.estimated_tons_basis


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


# --- filter chain: use codes, sqft floor, funnel reporting -----------------


@respx.mock
def test_industrial_use_code_included_by_default(db_session, cfg):
    """95,963 raw candidates was Commercial-only. The population is
    genuinely commercial+industrial, and the default config must reflect
    that -- an Industrial-tagged parcel with no permit is a real candidate."""
    _mock_commercial_parcels([
        {"AIN": "5050505050", "PropertyLocation": "warehouse", "UseCode": "3300",
         "UseCodeDescChar1": "Industrial", "YearBuilt": "1965", "SQFTmain": 40000},
    ])
    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["replacement_candidates"] == 1
    assert "Industrial" in stats["use_codes"]

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.use_desc == "Industrial"


@respx.mock
def test_min_sqft_floor_excludes_small_parcels(db_session, cfg):
    _mock_commercial_parcels([
        {"AIN": "6060606060", "PropertyLocation": "tiny kiosk", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 800},
    ], counts={"sqft_floor_survivors": 0})
    stats = find_replacement_candidates(db_session, cfg, fast_client(), min_sqft=5000)
    assert stats["sqft_floor_survivors"] == 0

    # The mocked feature fetch still returns the row (mock doesn't actually
    # filter by WHERE), but the funnel COUNT correctly reports zero
    # survivors -- proving the count path and the fetch path use the same
    # WHERE clause is a live-network property, exercised in the CLI, not
    # something respx can verify here. What this test guards is that a
    # custom min_sqft is threaded through to funnel_counts() at all.
    assert stats["min_sqft"] == 5000


@respx.mock
def test_funnel_counts_reported_at_each_stage(db_session, cfg):
    _mock_commercial_parcels([
        {"AIN": "7070707070", "PropertyLocation": "x", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 30000},
    ], counts={"use_code_match": 100, "built_before": 60, "sqft_floor_survivors": 25})
    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["use_code_match"] == 100
    assert stats["built_before"] == 60
    assert stats["sqft_floor_survivors"] == 25
    # The funnel narrows monotonically -- each stage no bigger than the last.
    assert stats["use_code_match"] >= stats["built_before"] >= stats["sqft_floor_survivors"]


def test_funnel_counts_pure_function(respx_mock):
    """funnel_counts() issues COUNT-only queries (cheap) rather than
    fetching full feature payloads for each stage."""
    calls = []

    def responder(request):
        params = dict(request.url.params)
        assert params.get("returnCountOnly") == "true", "funnel stages must not fetch full features"
        calls.append(params["where"])
        return httpx.Response(200, json={"count": 42})

    respx_mock.get(url__startswith="https://services.arcgis.com/RmCCgQtiZLDCtblq").mock(side_effect=responder)
    result = funnel_counts(fast_client(), use_codes=["Commercial", "Industrial"],
                           year_built_before=2010, min_sqft=5000)
    assert result == {"use_code_match": 42, "built_before": 42, "sqft_floor_survivors": 42}
    assert len(calls) == 3


# --- CARB/EBEWE candidacy applies to replacement candidates too ------------


@respx.mock
def test_carb_ebewe_candidacy_evaluated_for_replacement_candidates(db_session, cfg):
    """Unlike equipment type/tonnage/SB1206, CARB and EBEWE candidacy come
    from assessor use code and size alone -- independent of permit
    evidence, so absence of a permit must not suppress them."""
    db_session.add(AssessorCandidate(
        source="la_county_assessor", ain="8080808080",
        trigger_key="carb_refrigerant_management_program", use_code="2100",
        source_url="https://x"))
    db_session.add(AssessorCandidate(
        source="la_county_assessor", ain="8080808080",
        trigger_key="la_ebewe_audit_retrocommissioning", source_url="https://x"))
    db_session.commit()
    _mock_commercial_parcels([
        {"AIN": "8080808080", "PropertyLocation": "x", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 30000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())

    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.carb_candidate is True
    assert row.ebewe_candidate is True
    assert row.sb1206_trigger_status is None, "SB 1206 needs a post-2010 install year; never applies here"


# --- ranking: candidates use the same urgency-tier discipline --------------


@respx.mock
def test_candidate_ranking_uses_same_urgency_tiers_as_recently_active(db_session, cfg):
    """A genuinely-scored small overdue candidate must still outrank a
    borderline-age, huge candidate (not_due/approaching tier) -- the same
    'urgency is a tier size cannot cross' rule rank_buildings already
    enforces, now exercised through the candidate path.

    YearBuilt=1985, not something truly ancient like 1900: past the
    service-life abstention cutoff (built 1968 or earlier -- see the
    abstention tests above) a building doesn't carry a tier AT ALL, which
    would make this test assert the exact thing the abstention rule exists
    to stop. 1985 stays inside the observation window and is still
    unambiguously overdue by 2026."""
    _mock_commercial_parcels([
        {"AIN": "9090909090", "PropertyLocation": "tiny ancient", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1985", "SQFTmain": 6000},
        {"AIN": "9191919191", "PropertyLocation": "huge newer", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "2009", "SQFTmain": 2_000_000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())

    from sqlmodel import select
    rows = {r.apn: r for r in db_session.exec(select(RetrofitBuilding)).all()}
    tiny_ancient, huge_newer = rows["9090909090"], rows["9191919191"]
    assert tiny_ancient.service_life_status == "overdue"
    assert huge_newer.service_life_status in ("not_due", "approaching")
    assert tiny_ancient.rank_score > huge_newer.rank_score


# --- pure functions: generic_service_life, estimate_tonnage ----------------


def test_generic_service_life_is_composite_across_equipment_types(cfg):
    """No permit text names an equipment type for candidates -- this must
    average across the ownership tier's bands rather than picking one, and
    must default to private_commercial (the longest cycle) exactly like
    service_life() does when ownership is unknown."""
    gsl = generic_service_life(cfg, ownership=None)
    assert gsl.ownership == "private_commercial"
    assert gsl.equipment == "generic_yearbuilt_proxy"
    assert gsl.verified is False

    table = cfg.get("replacement.service_life.ownership.private_commercial.equipment")
    lows = [b["low"] for b in table.values()]
    highs = [b["high"] for b in table.values()]
    assert gsl.low == round(sum(lows) / len(lows))
    assert gsl.high == round(sum(highs) / len(highs))
    # Coarser than any single equipment-specific band, not equal to one.
    assert gsl.low != cfg.get("replacement.service_life.ownership.private_commercial.equipment.packaged_rooftop.low")


def test_generic_service_life_status_behaves_like_equipment_specific():
    from app.config import load_config
    cfg = load_config()
    gsl = generic_service_life(cfg, ownership=None)
    assert gsl.status(0) == "not_due"
    assert gsl.status(gsl.high + 5) == "overdue"


def test_estimate_tonnage_returns_a_band_not_a_point_figure(cfg):
    low, high, basis = estimate_tonnage(cfg, "Commercial", 100_000)
    assert low is not None and high is not None
    assert low < high
    assert "ESTIMATED" in basis
    assert "NOT mined from a permit" in basis


def test_estimate_tonnage_industrial_is_less_dense_than_commercial(cfg):
    """Same square footage, less cooling load per sqft for industrial/
    warehouse space than commercial -- same reasoning sizing.py already
    applies to fulfillment centers vs. offices."""
    c_low, _c_high, _ = estimate_tonnage(cfg, "Commercial", 100_000)
    _i_low, i_high, _ = estimate_tonnage(cfg, "Industrial", 100_000)
    assert i_high < c_low, "industrial band must not even overlap commercial at this sqft"


def test_estimate_tonnage_none_for_missing_sqft_or_unknown_use_desc(cfg):
    assert estimate_tonnage(cfg, "Commercial", None) == (None, None, None)
    assert estimate_tonnage(cfg, None, 50_000) == (None, None, None)
    assert estimate_tonnage(cfg, "Institutional", 50_000) == (None, None, None)


# ---- service-life abstention (2026-08-11) ----------------------------------
#
# private_commercial generic_service_life is low=19/high=22 -- two average
# cycles = 41yr. unobserved_years > 41 abstains (strict), so built 1968 or
# earlier abstains, 1969 (exactly 41yr unobserved) and 1970+ still score.
# These tests pin that boundary explicitly rather than asserting against a
# recomputed cutoff, so a change to the service-life table shows up as a
# broken test, not a silently shifted boundary.

@respx.mock
def test_ancient_building_abstains_from_service_life(db_session, cfg):
    _mock_commercial_parcels([
        {"AIN": "1111111111", "PropertyLocation": "1 Old St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1903", "SQFTmain": 50000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())
    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.service_life_status is None
    assert row.service_life_years_past is None
    assert row.service_life_basis is not None and "ABSTAINED" in row.service_life_basis
    assert row.year_built == 1903  # still known and displayed -- only the score abstains


@respx.mock
def test_building_just_past_the_cutoff_still_abstains(db_session, cfg):
    """41yr unobserved (built 1969) sits exactly AT two cycles, not past
    them, and does not abstain (strict >); 42yr (built 1968) does. Pinning
    both sides of the strict inequality here."""
    _mock_commercial_parcels([
        {"AIN": "2222222222", "PropertyLocation": "2 Old St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1968", "SQFTmain": 50000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())
    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.service_life_status is None


@respx.mock
def test_building_just_inside_the_window_still_scores(db_session, cfg):
    """Not every pre-2010 building abstains -- one built close enough to the
    observation window that a plausible original-equipment-still-there
    story survives the gap must keep scoring on the proxy."""
    _mock_commercial_parcels([
        {"AIN": "3333333333", "PropertyLocation": "3 Newer St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 50000},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())
    from sqlmodel import select
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.service_life_status is not None
    assert row.service_life_years_past is not None
    assert "ABSTAINED" not in (row.service_life_basis or "")


@respx.mock
def test_abstained_rows_rank_below_every_scored_tier(db_session, cfg):
    """The point of the fix: an abstained row must not out-rank (or even
    tie into) a genuinely-scored tier -- it ranks on size/use code alone,
    strictly below the tiered proxy population."""
    _mock_commercial_parcels([
        {"AIN": "4444444444", "PropertyLocation": "4 Old St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1854", "SQFTmain": 1_000_000},
        {"AIN": "5555555555", "PropertyLocation": "5 Newer St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1970", "SQFTmain": 5001},
    ])
    find_replacement_candidates(db_session, cfg, fast_client())
    from sqlmodel import select
    rows = {r.apn: r for r in db_session.exec(select(RetrofitBuilding)).all()}
    ancient_huge = rows["4444444444"]
    newer_tiny = rows["5555555555"]
    assert ancient_huge.service_life_status is None
    assert newer_tiny.service_life_status is not None
    # size alone (1M sqft vs 5,001 sqft) does NOT let the abstained giant
    # out-rank the smaller but genuinely-scored building.
    assert newer_tiny.rank_score > ancient_huge.rank_score


@respx.mock
def test_service_life_abstained_count_reported_in_stats(db_session, cfg):
    _mock_commercial_parcels([
        {"AIN": "6666666666", "PropertyLocation": "6 Old St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1900", "SQFTmain": 10000},
        {"AIN": "7777777777", "PropertyLocation": "7 Newer St", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "2005", "SQFTmain": 10000},
    ])
    stats = find_replacement_candidates(db_session, cfg, fast_client())
    assert stats["service_life_abstained"] == 1
    assert stats["replacement_candidates"] == 2
