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
    infer_equipment_type, rank_buildings,
)
from app.replacement import generic_service_life


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
    """A very old, small candidate (overdue tier) must still outrank a
    borderline-age, huge candidate (not_due/approaching tier) -- the same
    'urgency is a tier size cannot cross' rule rank_buildings already
    enforces, now exercised through the candidate path."""
    _mock_commercial_parcels([
        {"AIN": "9090909090", "PropertyLocation": "tiny ancient", "UseCode": "2100",
         "UseCodeDescChar1": "Commercial", "YearBuilt": "1900", "SQFTmain": 6000},
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
