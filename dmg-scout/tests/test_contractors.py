"""CSLB contractor import, geocoding, and the geographic join to retrofit
candidates. Mocks httpx (CSLB postback + Census batch geocoder) and never
touches a real network."""
import httpx
import pytest
import respx
from sqlmodel import select

import app.contractors as contractors
import app.pipeline.cslb as cslb
from app.geocode import BATCH_URL, batch_geocode, chunk_dict
from app.models import Contractor, RetrofitBuilding, RetrofitGeocode

# ---- a tiny, realistic slice of CSLB's real License Master column shape ----
CSV_HEADER = ("LicenseNo,LastUpdate,BusinessName,BUS-NAME-2,FullBusinessName,MailingAddress,City,"
             "State,County,ZIPCode,country,BusinessPhone,BusinessType,IssueDate,ReissueDate,"
             "ExpirationDate,InactivationDate,ReactivationDate,PendingSuspension,PendingClassRemoval,"
             "PendingClassReplace,PrimaryStatus,SecondaryStatus,Classifications(s),AsbestosReg,"
             "WorkersCompCoverageType,WCInsuranceCompany,WCPolicyNumber,WCEffectiveDate,"
             "WCExpirationDate,WCCancellationDate,WCSuspendDate,CBSuretyCompany,CBNumber,"
             "CBEffectiveDate,CBCancellationDate,CBAmount")


def _csv_row(license_no="1000002", business_name="AAON MECHANICAL INC", county="Los Angeles",
            classifications="C20", state="CA", zip_code="90001", city="LOS ANGELES",
            address="123 MAIN ST", status="CLEAR"):
    return (f'{license_no},09/15/2025,{business_name},,,{address},{city},{state},{county},{zip_code},,'
           f'(555) 555-5555,Corporation,01/10/2015,,01/31/2027,,,,,,{status},,{classifications},,'
           f'Exempt,,,,,,,PHILADELPHIA INDEMNITY,PB123,09/14/2025,,25000')


def _csv(*rows):
    return "\n".join([CSV_HEADER, *rows, ""])


# ---- classification / county scope -----------------------------------------

def test_in_scope_matches_hyphenated_and_unhyphenated_classifications():
    assert cslb._in_scope({"County": "Los Angeles", "Classifications(s)": "C20"})
    assert cslb._in_scope({"County": "Los Angeles", "Classifications(s)": "C-20"})
    assert cslb._in_scope({"County": "Orange", "Classifications(s)": "B, C57"})
    assert cslb._in_scope({"County": "Imperial", "Classifications(s)": "C38"})


def test_in_scope_rejects_out_of_scope_county():
    assert not cslb._in_scope({"County": "Sacramento", "Classifications(s)": "C20"})


def test_in_scope_rejects_out_of_scope_classification():
    assert not cslb._in_scope({"County": "Los Angeles", "Classifications(s)": "C57"})


def test_in_scope_handles_missing_fields():
    assert not cslb._in_scope({})
    assert not cslb._in_scope({"County": "Los Angeles"})


# ---- date/amount parsing -----------------------------------------------------

def test_parse_date_handles_real_and_empty_values():
    assert cslb._parse_date("01/31/2027").year == 2027
    assert cslb._parse_date("") is None
    assert cslb._parse_date(None) is None
    assert cslb._parse_date("garbage") is None


def test_parse_amount_strips_currency_formatting():
    assert cslb._parse_amount("$25,000") == 25000.0
    assert cslb._parse_amount("") is None
    assert cslb._parse_amount(None) is None


# ---- import: filtering, upsert, "only store what CSLB states" -------------

def test_import_skips_out_of_scope_rows(db_session):
    csv_text = _csv(_csv_row(license_no="1", county="Sacramento"),
                    _csv_row(license_no="2", classifications="C57"))
    stats = cslb.run_cslb_import(db_session, csv_text=csv_text, geocode_batch_limit=0)
    assert stats["in_scope"] == 0
    assert db_session.exec(select(Contractor)).all() == []


def test_import_stores_exactly_what_cslb_states(db_session):
    csv_text = _csv(_csv_row(license_no="1000002"))
    stats = cslb.run_cslb_import(db_session, csv_text=csv_text, geocode_batch_limit=0)
    assert stats["in_scope"] == 1
    assert stats["upserted"] == 1

    c = db_session.exec(select(Contractor)).one()
    assert c.license_no == "1000002"
    assert c.business_name == "AAON MECHANICAL INC"
    assert c.county == "Los Angeles"
    assert c.classifications == "C20"
    assert c.primary_status == "CLEAR"
    assert c.bond_company == "PHILADELPHIA INDEMNITY"
    assert c.bond_amount == 25000.0
    assert c.workers_comp_coverage_type == "Exempt"
    # Derived fields untouched by import alone (no geocode requested this test).
    assert c.latitude is None
    assert c.nearby_replacement_candidates is None


def test_import_is_idempotent_on_unchanged_rows(db_session):
    csv_text = _csv(_csv_row(license_no="1000002"))
    cslb.run_cslb_import(db_session, csv_text=csv_text, geocode_batch_limit=0)
    stats2 = cslb.run_cslb_import(db_session, csv_text=csv_text, geocode_batch_limit=0)
    assert stats2["unchanged"] == 1
    assert stats2["upserted"] == 0
    assert len(db_session.exec(select(Contractor)).all()) == 1


def test_import_updates_changed_fields_and_clears_stale_geocode_on_address_change(db_session):
    csv_text_v1 = _csv(_csv_row(license_no="1000002", address="123 MAIN ST"))
    cslb.run_cslb_import(db_session, csv_text=csv_text_v1, geocode_batch_limit=0)
    c = db_session.exec(select(Contractor)).one()
    c.latitude, c.longitude = 34.0, -118.0
    db_session.add(c)
    db_session.commit()

    csv_text_v2 = _csv(_csv_row(license_no="1000002", address="456 OTHER AVE"))
    stats = cslb.run_cslb_import(db_session, csv_text=csv_text_v2, geocode_batch_limit=0)
    assert stats["upserted"] == 1

    c2 = db_session.exec(select(Contractor)).one()
    assert c2.business_address == "456 OTHER AVE"
    assert c2.latitude is None  # stale geocode cleared, not left pointing at the old address


@respx.mock
def test_import_geocodes_new_contractors(db_session):
    csv_text = _csv(_csv_row(license_no="1000002"))
    respx.post(BATCH_URL).mock(return_value=httpx.Response(
        200, text='"1000002","123 MAIN ST, LOS ANGELES, CA, 90001","Match","Exact",'
                 '"123 MAIN ST, LOS ANGELES, CA, 90001","-118.25,34.05","1","L"\n'))
    stats = cslb.run_cslb_import(db_session, csv_text=csv_text)
    assert stats["geocoded"] == 1
    assert stats["geocode_batches"] == 1
    c = db_session.exec(select(Contractor)).one()
    assert c.latitude == 34.05
    assert c.longitude == -118.25
    assert c.geocode_source == "us_census_bureau"


# ---- geocode.py --------------------------------------------------------------

@respx.mock
def test_batch_geocode_parses_match_and_no_match_rows():
    respx.post(BATCH_URL).mock(return_value=httpx.Response(
        200, text=('"a","1 X ST","Match","Exact","1 X ST","-118.0,34.0","1","L"\n'
                  '"b","2 Y ST","No_Match","","","","",""\n')))
    result = batch_geocode({"a": ("1 X ST", "LA", "CA", "90001"), "b": ("2 Y ST", "LA", "CA", "90001")})
    assert result["a"] == (34.0, -118.0)
    assert result["b"] is None


def test_batch_geocode_empty_input_makes_no_request():
    assert batch_geocode({}) == {}


def test_batch_geocode_rejects_oversized_batch():
    with pytest.raises(ValueError):
        batch_geocode({str(i): ("x", "y", "CA", "1") for i in range(10_001)})


def test_chunk_dict_splits_preserving_all_items():
    d = {str(i): i for i in range(25)}
    chunks = chunk_dict(d, 10)
    assert [len(c) for c in chunks] == [10, 10, 5]
    assert {k: v for c in chunks for k, v in c.items()} == d


# ---- app.contractors: haversine + geographic join ---------------------------

def test_haversine_known_distance():
    # LA City Hall to Long Beach City Hall, real-world ~20 miles.
    d = contractors.haversine_miles(34.0537, -118.2427, 33.7683, -118.1956)
    assert 18 <= d <= 22


def test_haversine_zero_for_same_point():
    assert contractors.haversine_miles(34.0, -118.0, 34.0, -118.0) == 0.0


def _contractor(license_no="1", lat=34.05, lon=-118.25, classifications="C20", status="CLEAR"):
    return Contractor(license_no=license_no, business_name="Test Co", classifications=classifications,
                      primary_status=status, latitude=lat, longitude=lon)


def _building(apn="a1", lat=34.05, lon=-118.25, population="replacement_candidate", rank_score=5.0):
    return RetrofitBuilding(apn=apn, population=population, latitude=lat, longitude=lon, rank_score=rank_score)


def test_nearby_replacement_candidates_finds_buildings_in_radius(db_session):
    c = _contractor()
    db_session.add(c)
    db_session.add(_building(apn="near", lat=34.06, lon=-118.26, rank_score=9.0))   # ~0.9mi
    db_session.add(_building(apn="far", lat=36.0, lon=-120.0, rank_score=1.0))       # ~200mi
    db_session.commit()
    db_session.refresh(c)

    nearby = contractors.nearby_replacement_candidates(db_session, c, radius_miles=15)
    assert [b.apn for b in nearby] == ["near"]


def test_nearby_replacement_candidates_ranked_by_urgency_descending(db_session):
    c = _contractor()
    db_session.add(c)
    db_session.add(_building(apn="low", lat=34.05, lon=-118.25, rank_score=1.0))
    db_session.add(_building(apn="high", lat=34.05, lon=-118.26, rank_score=9.0))
    db_session.commit()
    db_session.refresh(c)

    nearby = contractors.nearby_replacement_candidates(db_session, c, radius_miles=15)
    assert [b.apn for b in nearby] == ["high", "low"]


def test_nearby_replacement_candidates_excludes_recently_active_population(db_session):
    c = _contractor()
    db_session.add(c)
    db_session.add(_building(apn="active", lat=34.05, lon=-118.25, population="recently_active"))
    db_session.commit()
    db_session.refresh(c)

    assert contractors.nearby_replacement_candidates(db_session, c, radius_miles=15) == []


def test_nearby_replacement_candidates_empty_for_ungeocoded_contractor(db_session):
    c = Contractor(license_no="1", business_name="Test", latitude=None, longitude=None)
    db_session.add(c)
    db_session.commit()
    assert contractors.nearby_replacement_candidates(db_session, c, radius_miles=15) == []


def test_match_contractors_precomputes_and_persists_count(db_session, cfg):
    c1 = _contractor(license_no="1")
    c2 = Contractor(license_no="2", business_name="Ungeocoded", latitude=None, longitude=None)
    db_session.add_all([c1, c2])
    db_session.add(_building(apn="near", lat=34.06, lon=-118.26))
    db_session.commit()

    stats = contractors.match_contractors(db_session, cfg, radius_miles=15)
    assert stats["contractors_matched"] == 1
    assert stats["contractors_skipped_ungeocoded"] == 1

    db_session.refresh(c1)
    assert c1.nearby_replacement_candidates == 1
    assert c1.nearby_radius_miles == 15
    assert c1.nearby_computed_at is not None


def test_nearest_mechanical_contractors_filters_to_c20_c38_only(db_session):
    b = _building(apn="b1", lat=34.05, lon=-118.25)
    db_session.add(b)
    db_session.add(_contractor(license_no="mech", classifications="C20", lat=34.05, lon=-118.26))
    db_session.add(_contractor(license_no="general", classifications="B", lat=34.05, lon=-118.26))
    db_session.commit()
    db_session.refresh(b)

    results = contractors.nearest_mechanical_contractors(db_session, b, radius_miles=15)
    assert [r["contractor"].license_no for r in results] == ["mech"]


def test_nearest_mechanical_contractors_sorted_by_distance(db_session):
    b = _building(apn="b1", lat=34.05, lon=-118.25)
    db_session.add(b)
    db_session.add(_contractor(license_no="far", classifications="C20", lat=34.20, lon=-118.40))
    db_session.add(_contractor(license_no="near", classifications="C38", lat=34.06, lon=-118.26))
    db_session.commit()
    db_session.refresh(b)

    results = contractors.nearest_mechanical_contractors(db_session, b, radius_miles=15)
    assert [r["contractor"].license_no for r in results] == ["near", "far"]
    assert results[0]["distance_miles"] < results[1]["distance_miles"]


def test_nearest_mechanical_contractors_includes_non_clear_status_unfiltered(db_session):
    """This module does not decide who counts as licensed-enough to show --
    a suspended contractor still appears if nearest, with real status."""
    b = _building(apn="b1", lat=34.05, lon=-118.25)
    db_session.add(b)
    db_session.add(_contractor(license_no="susp", classifications="C20", status="Work Comp Susp",
                               lat=34.06, lon=-118.26))
    db_session.commit()
    db_session.refresh(b)

    results = contractors.nearest_mechanical_contractors(db_session, b, radius_miles=15)
    assert len(results) == 1
    assert results[0]["contractor"].primary_status == "Work Comp Susp"


def test_nearest_mechanical_contractors_empty_for_ungeocoded_building(db_session):
    b = RetrofitBuilding(apn="b1", population="replacement_candidate", latitude=None, longitude=None)
    db_session.add(b)
    db_session.commit()
    db_session.refresh(b)
    assert contractors.nearest_mechanical_contractors(db_session, b, radius_miles=15) == []
