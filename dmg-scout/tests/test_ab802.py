"""AB 802 statewide benchmarking import: positional parsing, full-replace-
per-year semantics, in_territory, and the loose lat/long-then-address join
to RetrofitBuilding/EbeweBenchmark. See app/pipeline/ab802.py's module
docstring for the compliance check and join-method reasoning this relies on.

The fetch HTTP call is mocked (respx); these tests are about parsing,
full-replace, and the join/ambiguity logic, not live network."""
import io

import httpx
import openpyxl
import respx
from sqlmodel import select

from app.http import PoliteClient
from app.models import Ab802Building, EbeweBenchmark, RetrofitBuilding, SourceRun
from app.pipeline.ab802 import (
    FILE_URL_TEMPLATE, fetch_ab802_benchmarks, match_to_ebewe, match_to_retrofit, parse_rows,
)


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


HEADER = [
    "Portfolio Manager Property ID", "Standard ID", "Property Name", "Address 1", "City",
    "State/Province", "Postal Code", "Property GFA - Calculated (Buildings) (ft²)",
    "Primary Property Type - Portfolio Manager-Calculated",
    "List of All Property Use Types (GFA) (ft²)",
    "Weather Normalized Site EUI (kBtu/ft²)", "Natural Gas Use (kBtu)",
    "Electricity Use - Grid Purchase (kBtu)",
    "Electricity Use – Generated from Onsite Renewable Systems and Used Onsite (kBtu)",
    "Fuel Oil #2 Use (kBtu)", "District Steam Use (kBtu)", "Diesel Use (kBtu)", "Propane Use (kBtu)",
    "District Hot Water Use (kBtu)", "District Chilled Water Use (kBtu)", "Year Built", "W_Energy",
    "ENERGY STAR Score", "ENERGY STAR Certified", "ENERGY STAR Certification - Year(s) Certified",
    "Latitude", "Longitude", "County from Geocoding", "Report Generation Date",
    "Used Estimated Energy Values",
    "Alert - Energy Meter has less than 12 full calendar months of data",
    "Total (Location-Based) GHG Emissions (Metric Tons CO2e)",
    "Total (Location-Based) GHG Emissions Intensity (kgCO2e/ft²)", "Year Ending",
]


def _workbook(rows: list[dict]) -> bytes:
    """Minimal file matching the real layout: row 1 title, row 2 blank, row
    3 header, data from row 4 -- see app/pipeline/ab802.py's HEADER_ROW."""
    wb = openpyxl.Workbook()
    ws = wb.active
    ws.append(["Information Reported for 2024, California Building Energy Benchmarking Program"])
    ws.append([])
    ws.append(HEADER)
    for r in rows:
        ws.append([r.get(h) for h in HEADER])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _row(property_id="1001", property_name="Test Tower", address_1="123 Main St",
        city="Los Angeles", state="CA", county="Los Angeles County", year_built=1980,
        eui=90.0, lat=34.05, lon=-118.25, year_ending=2024, **extra) -> dict:
    base = {
        "Portfolio Manager Property ID": property_id, "Property Name": property_name,
        "Address 1": address_1, "City": city, "State/Province": state,
        "Primary Property Type - Portfolio Manager-Calculated": "Office",
        "Weather Normalized Site EUI (kBtu/ft²)": eui, "Year Built": year_built,
        "Latitude": lat, "Longitude": lon, "County from Geocoding": county,
        "Year Ending": year_ending,
    }
    base.update(extra)
    return base


def _rb(session, apn, address=None, latitude=None, longitude=None, county="Los Angeles") -> RetrofitBuilding:
    rb = RetrofitBuilding(apn=apn, address=address, latitude=latitude, longitude=longitude, county=county)
    session.add(rb)
    session.commit()
    return rb


def _ebewe(session, building_id, address, organization, program_year=2024) -> EbeweBenchmark:
    rec = EbeweBenchmark(building_id=building_id, program_year=program_year, building_address=address,
                         organization=organization, source_url="https://data.lacity.org/x")
    session.add(rec)
    session.commit()
    return rec


# --- parse_rows: positional, not text-header matching -----------------------


def test_parse_rows_extracts_every_field_by_position():
    xlsx = _workbook([_row(property_id="42", property_name="Acme Plaza", eui=123.4,
                          year_built=1975, county="Los Angeles County")])
    rows = parse_rows(xlsx, year=2024)
    assert len(rows) == 1
    r = rows[0]
    assert r["portfolio_manager_property_id"] == "42"
    assert r["property_name"] == "Acme Plaza"
    assert r["weather_normalized_site_eui"] == 123.4
    assert r["year_built"] == 1975
    assert r["county_from_geocoding"] == "Los Angeles County"
    assert r["year_ending"] == 2024


def test_parse_rows_drops_unkeyable_rows_with_no_property_id():
    xlsx = _workbook([_row(property_id=None), _row(property_id="7")])
    rows = parse_rows(xlsx, year=2024)
    assert [r["portfolio_manager_property_id"] for r in rows] == ["7"]


def test_parse_rows_treats_not_available_as_null():
    xlsx = _workbook([_row(**{"ENERGY STAR Score": "Not Available"})])
    rows = parse_rows(xlsx, year=2024)
    assert rows[0]["energy_star_score"] is None


# --- fetch_ab802_benchmarks: full-replace-per-year, in_territory, SourceRun --


@respx.mock
def test_fetch_stores_rows_and_a_source_run(db_session, cfg):
    xlsx = _workbook([_row(property_id="1", county="Los Angeles County")])
    respx.get(FILE_URL_TEMPLATE.format(year=2024)).mock(return_value=httpx.Response(200, content=xlsx))

    with fast_client() as client:
        stats = fetch_ab802_benchmarks(db_session, cfg, client, 2024)

    assert stats["fetched"] == 1 and stats["stored"] == 1 and stats["error"] is None
    rec = db_session.exec(select(Ab802Building)).one()
    assert rec.portfolio_manager_property_id == "1" and rec.year_ending == 2024
    assert rec.in_territory is True

    run = db_session.exec(select(SourceRun)).one()
    assert run.source == "ab802_benchmarking" and run.ok is True


@respx.mock
def test_fetch_marks_out_of_territory_county_correctly(db_session, cfg):
    xlsx = _workbook([_row(property_id="1", county="Santa Clara County")])
    respx.get(FILE_URL_TEMPLATE.format(year=2024)).mock(return_value=httpx.Response(200, content=xlsx))
    with fast_client() as client:
        fetch_ab802_benchmarks(db_session, cfg, client, 2024)
    rec = db_session.exec(select(Ab802Building)).one()
    assert rec.in_territory is False


@respx.mock
def test_refetching_one_year_replaces_only_that_year(db_session, cfg):
    xlsx_2023 = _workbook([_row(property_id="1", year_ending=2023, eui=50.0)])
    xlsx_2024 = _workbook([_row(property_id="1", year_ending=2024, eui=60.0)])
    respx.get(FILE_URL_TEMPLATE.format(year=2023)).mock(return_value=httpx.Response(200, content=xlsx_2023))
    respx.get(FILE_URL_TEMPLATE.format(year=2024)).mock(return_value=httpx.Response(200, content=xlsx_2024))

    with fast_client() as client:
        fetch_ab802_benchmarks(db_session, cfg, client, 2023)
        fetch_ab802_benchmarks(db_session, cfg, client, 2024)

    rows = db_session.exec(select(Ab802Building).order_by(Ab802Building.year_ending)).all()
    assert [r.year_ending for r in rows] == [2023, 2024]

    # Re-fetching 2024 with a changed EUI must not touch the 2023 row.
    xlsx_2024_v2 = _workbook([_row(property_id="1", year_ending=2024, eui=99.0)])
    respx.get(FILE_URL_TEMPLATE.format(year=2024)).mock(return_value=httpx.Response(200, content=xlsx_2024_v2))
    with fast_client() as client:
        fetch_ab802_benchmarks(db_session, cfg, client, 2024)
    rows = db_session.exec(select(Ab802Building).order_by(Ab802Building.year_ending)).all()
    assert len(rows) == 2
    by_year = {r.year_ending: r for r in rows}
    assert by_year[2023].weather_normalized_site_eui == 50.0
    assert by_year[2024].weather_normalized_site_eui == 99.0


# --- match_to_retrofit: lat/long first, address fallback, abstain on ambiguity


def test_match_to_retrofit_latlong_within_radius(db_session, cfg):
    _rb(db_session, apn="APN1", latitude=34.0500, longitude=-118.2500, address="123 Main St")
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        latitude=34.05001, longitude=-118.25001, address_1="999 Other St",
                        source_url="x")
    db_session.add(row)
    db_session.commit()

    matched = match_to_retrofit(db_session, [row])
    assert matched == 1
    assert row.assessor_match_method == "latlong"
    assert row.retrofit_apn == "APN1"
    assert row.assessor_match_distance_m is not None and row.assessor_match_distance_m < 30.0


def test_match_to_retrofit_abstains_when_row_is_near_two_apns(db_session, cfg):
    _rb(db_session, apn="APN1", latitude=34.05000, longitude=-118.25000)
    _rb(db_session, apn="APN2", latitude=34.05002, longitude=-118.25002)
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        latitude=34.05001, longitude=-118.25001, source_url="x")
    db_session.add(row)
    db_session.commit()

    matched = match_to_retrofit(db_session, [row])
    assert matched == 0
    assert row.assessor_match_method is None


def test_match_to_retrofit_falls_back_to_normalized_address(db_session, cfg):
    _rb(db_session, apn="APN1", address="700 S MAIN ST", latitude=None, longitude=None)
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        address_1="700 S Main St Suite 4", source_url="x")
    db_session.add(row)
    db_session.commit()

    matched = match_to_retrofit(db_session, [row])
    assert matched == 1
    assert row.assessor_match_method == "normalized_address"
    assert row.assessor_match_distance_m is None
    assert row.retrofit_apn == "APN1"


def test_match_to_retrofit_no_match_when_nothing_close(db_session, cfg):
    _rb(db_session, apn="APN1", latitude=40.0, longitude=-120.0, address="Nowhere near")
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        latitude=34.05, longitude=-118.25, address_1="123 Main St", source_url="x")
    db_session.add(row)
    db_session.commit()

    assert match_to_retrofit(db_session, [row]) == 0
    assert row.assessor_match_method is None


# --- match_to_ebewe: organization -> benchmarking_filer, never "owner" ------


def test_match_to_ebewe_surfaces_organization_as_benchmarking_filer(db_session, cfg):
    _ebewe(db_session, building_id="B1", address="123 Main St", organization="Acme Property Mgmt")
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        address_1="123 Main St", source_url="x")
    db_session.add(row)
    db_session.commit()

    matched = match_to_ebewe(db_session, [row])
    assert matched == 1
    assert row.benchmarking_filer == "Acme Property Mgmt"
    assert row.ebewe_building_id == "B1"


def test_match_to_ebewe_abstains_on_ambiguous_address(db_session, cfg):
    _ebewe(db_session, building_id="B1", address="123 Main St", organization="Org A")
    row1 = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                         address_1="123 Main St", source_url="x")
    row2 = Ab802Building(portfolio_manager_property_id="2", year_ending=2024, in_territory=True,
                         address_1="123 Main St", source_url="x")
    db_session.add(row1)
    db_session.add(row2)
    db_session.commit()

    matched = match_to_ebewe(db_session, [row1, row2])
    assert matched == 0
    assert row1.benchmarking_filer is None and row2.benchmarking_filer is None


def test_match_to_ebewe_no_filer_field_when_organization_blank(db_session, cfg):
    _ebewe(db_session, building_id="B1", address="123 Main St", organization=None)
    row = Ab802Building(portfolio_manager_property_id="1", year_ending=2024, in_territory=True,
                        address_1="123 Main St", source_url="x")
    db_session.add(row)
    db_session.commit()

    assert match_to_ebewe(db_session, [row]) == 0
    assert row.benchmarking_filer is None


# --- latest_in_territory_rows / rank_in_territory ---------------------------


def _ab802(session, property_id, year_ending=2024, in_territory=True, property_type="Office",
          year_built=1980, eui=90.0, county="Los Angeles County", assessor_match=None,
          **extra) -> Ab802Building:
    row = Ab802Building(portfolio_manager_property_id=property_id, year_ending=year_ending,
                        in_territory=in_territory, primary_property_type=property_type,
                        year_built=year_built, weather_normalized_site_eui=eui,
                        county_from_geocoding=county, assessor_match_method=assessor_match,
                        source_url="x", **extra)
    session.add(row)
    session.commit()
    return row


def test_latest_in_territory_rows_picks_most_recent_year_per_property(db_session, cfg):
    from app.pipeline.ab802 import latest_in_territory_rows
    _ab802(db_session, "1", year_ending=2023, eui=50.0)
    _ab802(db_session, "1", year_ending=2024, eui=99.0)
    rows = latest_in_territory_rows(db_session)
    assert len(rows) == 1
    assert rows[0].year_ending == 2024 and rows[0].weather_normalized_site_eui == 99.0


def test_latest_in_territory_rows_excludes_out_of_territory(db_session, cfg):
    from app.pipeline.ab802 import latest_in_territory_rows
    _ab802(db_session, "1", in_territory=True)
    _ab802(db_session, "2", in_territory=False)
    rows = latest_in_territory_rows(db_session)
    assert [r.portfolio_manager_property_id for r in rows] == ["1"]


def test_rank_in_territory_older_and_higher_eui_ranks_first(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "old_bad", year_built=1950, eui=200.0)   # oldest, worst EUI -> top
    _ab802(db_session, "new_good", year_built=2020, eui=20.0)   # newest, best EUI -> bottom
    _ab802(db_session, "middle", year_built=1985, eui=100.0)

    ranked = rank_in_territory(db_session)
    ids = [d["row"].portfolio_manager_property_id for d in ranked]
    assert ids == ["old_bad", "middle", "new_good"]
    assert ranked[0]["age_percentile"] == 1.0
    assert ranked[0]["eui_percentile"] == 1.0


def test_rank_in_territory_ranks_within_property_type_only(db_session, cfg):
    """A 1950 warehouse with low EUI for its type must not be dragged down by
    a 2020 office with a naturally different EUI range -- percentiles are
    computed per primary_property_type, never across types."""
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "wh_old", property_type="Warehouse", year_built=1950, eui=30.0)
    _ab802(db_session, "wh_new", property_type="Warehouse", year_built=2020, eui=10.0)
    _ab802(db_session, "office", property_type="Office", year_built=1990, eui=500.0)

    ranked = rank_in_territory(db_session, property_type="Warehouse")
    ids = [d["row"].portfolio_manager_property_id for d in ranked]
    assert ids == ["wh_old", "wh_new"]


def test_rank_in_territory_missing_year_built_gets_no_age_percentile(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "known", year_built=1950, eui=100.0)
    _ab802(db_session, "unknown_age", year_built=None, eui=100.0)

    ranked = rank_in_territory(db_session)
    by_id = {d["row"].portfolio_manager_property_id: d for d in ranked}
    assert by_id["unknown_age"]["age_percentile"] is None
    assert by_id["known"]["age_percentile"] is not None


def test_rank_in_territory_filters_county_property_type_year_built_and_match(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "1", county="Los Angeles County", property_type="Office",
          year_built=1970, assessor_match="latlong")
    _ab802(db_session, "2", county="Orange County", property_type="Office", year_built=1970)
    _ab802(db_session, "3", county="Los Angeles County", property_type="Warehouse", year_built=1970)
    _ab802(db_session, "4", county="Los Angeles County", property_type="Office", year_built=2020)

    assert {d["row"].portfolio_manager_property_id
            for d in rank_in_territory(db_session, county="Los Angeles County")} == {"1", "3", "4"}
    assert [d["row"].portfolio_manager_property_id
           for d in rank_in_territory(db_session, property_type="Warehouse")] == ["3"]
    assert {d["row"].portfolio_manager_property_id
            for d in rank_in_territory(db_session, year_built_before=2000)} == {"1", "2", "3"}
    assert [d["row"].portfolio_manager_property_id
           for d in rank_in_territory(db_session, has_assessor_match=True)] == ["1"]


def test_rank_in_territory_eui_above_median_for_its_type(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "low", property_type="Office", eui=10.0)
    _ab802(db_session, "mid", property_type="Office", eui=50.0)
    _ab802(db_session, "high", property_type="Office", eui=90.0)

    result = [d["row"].portfolio_manager_property_id
             for d in rank_in_territory(db_session, eui_above_median=True)]
    assert result == ["high"]


@respx.mock
def test_fetch_dedupes_duplicate_property_ids_keeping_the_last_row(db_session, cfg):
    """Confirmed live 2026-09-07: the real file is not unique per Portfolio
    Manager Property ID within a year -- the last row in file order wins,
    dropped count is reported, nothing silently discarded without account."""
    xlsx = _workbook([
        _row(property_id="1", address_1="Original Address", eui=10.0),
        _row(property_id="1", address_1="Corrected Address", eui=20.0),
        _row(property_id="2", address_1="Unrelated"),
    ])
    respx.get(FILE_URL_TEMPLATE.format(year=2024)).mock(return_value=httpx.Response(200, content=xlsx))

    with fast_client() as client:
        stats = fetch_ab802_benchmarks(db_session, cfg, client, 2024)

    assert stats["fetched"] == 3
    assert stats["stored"] == 2
    assert stats["duplicate_property_ids_dropped"] == 1
    assert stats["error"] is None

    rows = db_session.exec(select(Ab802Building)).all()
    assert len(rows) == 2
    kept = next(r for r in rows if r.portfolio_manager_property_id == "1")
    assert kept.address_1 == "Corrected Address" and kept.weather_normalized_site_eui == 20.0


# --- DMG_RELEVANT_PROPERTY_TYPES / looks_like_organization_name -----------


def test_dmg_relevant_types_excludes_catchalls_and_named_exclusions():
    from app.pipeline.ab802 import DMG_RELEVANT_PROPERTY_TYPES
    for excluded in ("Casino", "Worship Facility", "Parking", "Other", "Other - Recreation",
                    "Other - Restaurant/Bar", "Self-Storage Facility", "Senior Living Community"):
        assert excluded not in DMG_RELEVANT_PROPERTY_TYPES
    for included in ("Office", "Medical Office", "K-12 School", "Data Center",
                     "Multifamily Housing", "Distribution Center"):
        assert included in DMG_RELEVANT_PROPERTY_TYPES


def test_looks_like_organization_name_word_boundary_matches():
    from app.pipeline.ab802 import looks_like_organization_name
    assert looks_like_organization_name("Acme Properties LLC") is True
    assert looks_like_organization_name("Kaiser Foundation Hospitals") is True
    assert looks_like_organization_name("University of Southern California") is True
    assert looks_like_organization_name("Los Angeles Unified School District") is True
    assert looks_like_organization_name("City of Los Angeles") is True
    assert looks_like_organization_name("St. Mary's Church") is True


def test_looks_like_organization_name_no_false_positive_on_substrings():
    from app.pipeline.ab802 import looks_like_organization_name
    # "inc" is a substring of "distinct"/"Cincinnati" -- must not match without a word boundary.
    assert looks_like_organization_name("123 Distinct Plaza") is False
    assert looks_like_organization_name("Cincinnati Building") is False
    assert looks_like_organization_name("Dodger Stadium") is False
    assert looks_like_organization_name("") is False
    assert looks_like_organization_name(None) is False


# --- rank_in_territory: restrict_to_relevant_types + name_hint -------------


def test_rank_in_territory_restricts_to_relevant_types_by_default(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "1", property_type="Office")
    _ab802(db_session, "2", property_type="Casino")

    restricted = [d["row"].portfolio_manager_property_id
                 for d in rank_in_territory(db_session, restrict_to_relevant_types=True)]
    assert restricted == ["1"]

    unrestricted = {d["row"].portfolio_manager_property_id
                   for d in rank_in_territory(db_session, restrict_to_relevant_types=False)}
    assert unrestricted == {"1", "2"}


def test_rank_in_territory_explicit_property_type_overrides_the_default_restriction(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "1", property_type="Office")
    _ab802(db_session, "2", property_type="Casino")

    result = [d["row"].portfolio_manager_property_id
             for d in rank_in_territory(db_session, property_type="Casino", restrict_to_relevant_types=True)]
    assert result == ["2"]


def test_rank_in_territory_name_hint_only_when_filer_blank_and_name_reads_organizational(db_session, cfg):
    from app.pipeline.ab802 import rank_in_territory
    _ab802(db_session, "1", property_name="Kaiser Foundation Hospitals - Building A")  # no filer, org name
    _ab802(db_session, "2", property_name="Dodger Stadium")                            # no filer, not org-like
    _ab802(db_session, "3", property_name="Some LLC Tower", benchmarking_filer="Acme Mgmt")  # filer wins

    by_id = {d["row"].portfolio_manager_property_id: d for d in rank_in_territory(db_session)}
    assert by_id["1"]["name_hint"] == "Kaiser Foundation Hospitals - Building A"
    assert by_id["2"]["name_hint"] is None
    assert by_id["3"]["name_hint"] is None  # filer already present -- name_hint is never a second source
