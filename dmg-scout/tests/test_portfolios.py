"""Portfolio-transaction detection: buildings sharing a recording date within
a short window AND close geographic proximity collapse into one group rather
than ranking as unrelated leads. See app/portfolios.py's module docstring for
the real-example calibration (Azusa Ave) and the false-positive discovery
(a 44-building Glendale mass-recording event) that motivated the group-size
cap.

No network, no mocks -- detect_portfolios is pure math over plain objects;
apply_portfolio_grouping/portfolio_coverage exercise the DB layer against
db_session, same fixture test_ownership.py uses."""
from datetime import datetime, timedelta
from types import SimpleNamespace

from sqlmodel import select

from app.models import RetrofitBuilding
from app.portfolios import (
    PORTFOLIO_DATE_WINDOW_DAYS, PORTFOLIO_MAX_GROUP_SIZE, PORTFOLIO_RADIUS_MILES,
    apply_portfolio_grouping, detect_portfolios, portfolio_coverage,
)

# Real coordinates near 1600 S Azusa Ave, City of Industry -- close enough
# together that any two of them are well within PORTFOLIO_RADIUS_MILES.
AZUSA_LAT, AZUSA_LON = 34.015, -117.921


def _b(apn, days_ago, lat=AZUSA_LAT, lon=AZUSA_LON, sqft=10000, address=None, id_=None):
    return SimpleNamespace(
        apn=apn, id=id_ if id_ is not None else apn, address=address or f"{apn} Test St",
        sqft=sqft, latitude=lat, longitude=lon,
        last_sale_date=datetime(2024, 9, 1) - timedelta(days=days_ago),
    )


# --- detect_portfolios: core grouping logic ------------------------------------

def test_two_adjacent_buildings_sold_close_in_time_group_together():
    buildings = [_b("A", 0), _b("B", 11)]
    groups = detect_portfolios(buildings)
    assert groups["A"]["group_id"] == groups["B"]["group_id"]
    assert groups["A"]["member_count"] == 2
    assert groups["B"]["members"][0]["apn"] == "A"


def test_combined_sqft_sums_every_member():
    buildings = [_b("A", 0, sqft=10000), _b("B", 5, sqft=25000), _b("C", 10, sqft=8000)]
    groups = detect_portfolios(buildings)
    assert groups["A"]["combined_sqft"] == 43000
    assert groups["B"]["combined_sqft"] == 43000


def test_members_list_excludes_self():
    buildings = [_b("A", 0), _b("B", 5)]
    groups = detect_portfolios(buildings)
    assert all(m["apn"] != "A" for m in groups["A"]["members"])
    assert all(m["apn"] != "B" for m in groups["B"]["members"])


def test_member_last_sale_date_stored_as_iso_string_not_datetime():
    # SQLAlchemy's JSON column can't serialize a raw datetime -- this list
    # gets written straight into one, so it must already be a string.
    buildings = [_b("A", 0), _b("B", 5)]
    groups = detect_portfolios(buildings)
    stamp = groups["A"]["members"][0]["last_sale_date"]
    assert isinstance(stamp, str)
    assert stamp == datetime(2024, 8, 27).date().isoformat()


def test_gap_beyond_the_date_window_does_not_group():
    buildings = [_b("A", 0), _b("B", PORTFOLIO_DATE_WINDOW_DAYS + 1)]
    groups = detect_portfolios(buildings)
    assert groups == {}


def test_gap_within_the_date_window_still_groups():
    buildings = [_b("A", 0), _b("B", PORTFOLIO_DATE_WINDOW_DAYS)]
    groups = detect_portfolios(buildings)
    assert "A" in groups and "B" in groups


def test_distance_beyond_the_radius_does_not_group():
    # Roughly 25 miles from the Azusa cluster -- well outside PORTFOLIO_RADIUS_MILES.
    far = _b("B", 0, lat=34.05, lon=-118.25)
    buildings = [_b("A", 0), far]
    groups = detect_portfolios(buildings)
    assert groups == {}


def test_missing_sale_date_or_geocode_excludes_a_building_from_grouping():
    no_date = _b("A", 0)
    no_date.last_sale_date = None
    no_geocode = _b("B", 0)
    no_geocode.latitude = None
    real_pair = [_b("C", 0), _b("D", 3)]
    groups = detect_portfolios([no_date, no_geocode, *real_pair])
    assert "A" not in groups
    assert "B" not in groups
    assert "C" in groups and "D" in groups


def test_group_larger_than_the_cap_is_excluded_entirely_not_truncated():
    # The Glendale false positive: a mass-recording event sharing one exact
    # date across many parcels must not present as a single lead, but the
    # excluded buildings also shouldn't silently vanish from the return --
    # they simply don't appear as grouped (equivalent to standalone).
    oversized = [_b(f"G{i}", 0) for i in range(PORTFOLIO_MAX_GROUP_SIZE + 1)]
    groups = detect_portfolios(oversized)
    assert groups == {}


def test_group_at_exactly_the_cap_still_groups():
    at_cap = [_b(f"G{i}", 0) for i in range(PORTFOLIO_MAX_GROUP_SIZE)]
    groups = detect_portfolios(at_cap)
    assert len(groups) == PORTFOLIO_MAX_GROUP_SIZE
    assert groups["G0"]["member_count"] == PORTFOLIO_MAX_GROUP_SIZE


def test_two_unconnected_pairs_form_two_distinct_groups():
    near_a = [_b("A1", 0), _b("A2", 2)]
    near_b = [_b("B1", 0, lat=34.5, lon=-118.5), _b("B2", 2, lat=34.5, lon=-118.5)]
    groups = detect_portfolios(near_a + near_b)
    assert groups["A1"]["group_id"] != groups["B1"]["group_id"]


def test_group_id_is_the_min_apn_in_the_group_deterministic():
    buildings = [_b("Z999", 0), _b("A111", 3), _b("M555", 5)]
    groups = detect_portfolios(buildings)
    assert groups["Z999"]["group_id"] == "A111"
    assert groups["A111"]["group_id"] == "A111"


def test_standalone_building_never_appears_in_the_result():
    buildings = [_b("A", 0), _b("B", 3), _b("SOLO", 500)]
    groups = detect_portfolios(buildings)
    assert "SOLO" not in groups


# --- apply_portfolio_grouping: DB write, full-refresh discipline ---------------

def test_apply_writes_grouping_fields_for_a_real_pair(db_session):
    now = datetime(2024, 9, 1)
    db_session.add(RetrofitBuilding(apn="1600", address="1600 S Azusa Ave", population="replacement_candidate",
                                    sqft=50000, latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now))
    db_session.add(RetrofitBuilding(apn="1620", address="1620 S Azusa Ave", population="replacement_candidate",
                                    sqft=40000, latitude=AZUSA_LAT, longitude=AZUSA_LON,
                                    last_sale_date=now + timedelta(days=11)))
    db_session.commit()

    stats = apply_portfolio_grouping(db_session, "replacement_candidate")

    assert stats["buildings_grouped"] == 2
    assert stats["distinct_groups"] == 1
    rows = {r.apn: r for r in db_session.exec(
        select(RetrofitBuilding).where(RetrofitBuilding.population == "replacement_candidate")).all()}
    assert rows["1600"].portfolio_group_id == rows["1620"].portfolio_group_id
    assert rows["1600"].portfolio_combined_sqft == 90000
    assert rows["1620"].portfolio_members[0]["apn"] == "1600"


def test_apply_only_touches_its_own_population(db_session):
    now = datetime(2024, 9, 1)
    db_session.add(RetrofitBuilding(apn="R1", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now))
    db_session.add(RetrofitBuilding(apn="R2", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now + timedelta(days=2)))
    db_session.add(RetrofitBuilding(apn="X1", population="recently_active",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now))
    db_session.commit()

    apply_portfolio_grouping(db_session, "replacement_candidate")

    x1 = db_session.exec(select(RetrofitBuilding).where(RetrofitBuilding.apn == "X1")).one()
    assert x1.portfolio_group_id is None


def test_apply_is_a_full_refresh_clearing_stale_membership(db_session):
    # A building that WAS grouped on a prior rebuild but no longer qualifies
    # (its former partner is gone) must not keep stale grouping fields --
    # same "full refresh" discipline as app.pipeline.local250's signatory match.
    now = datetime(2024, 9, 1)
    b = RetrofitBuilding(apn="LONE", population="replacement_candidate",
                         latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now,
                         portfolio_group_id="STALE", portfolio_member_count=2,
                         portfolio_combined_sqft=99999.0, portfolio_members=[{"apn": "GONE"}])
    db_session.add(b)
    db_session.commit()

    apply_portfolio_grouping(db_session, "replacement_candidate")

    refreshed = db_session.exec(select(RetrofitBuilding).where(RetrofitBuilding.apn == "LONE")).one()
    assert refreshed.portfolio_group_id is None
    assert refreshed.portfolio_member_count is None
    assert refreshed.portfolio_combined_sqft is None
    assert refreshed.portfolio_members == []


def test_apply_is_idempotent_across_repeated_runs(db_session):
    now = datetime(2024, 9, 1)
    db_session.add(RetrofitBuilding(apn="P1", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now))
    db_session.add(RetrofitBuilding(apn="P2", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now + timedelta(days=1)))
    db_session.commit()

    first = apply_portfolio_grouping(db_session, "replacement_candidate")
    second = apply_portfolio_grouping(db_session, "replacement_candidate")
    assert first == second


# --- portfolio_coverage: live assumptions-register counts -----------------------

def test_coverage_counts_grouped_buildings_and_distinct_groups(db_session):
    now = datetime(2024, 9, 1)
    db_session.add(RetrofitBuilding(apn="C1", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now))
    db_session.add(RetrofitBuilding(apn="C2", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now + timedelta(days=1)))
    db_session.add(RetrofitBuilding(apn="C3", population="replacement_candidate",
                                    latitude=AZUSA_LAT, longitude=AZUSA_LON, last_sale_date=now + timedelta(days=3000)))
    db_session.commit()
    apply_portfolio_grouping(db_session, "replacement_candidate")

    cov = portfolio_coverage(db_session)
    assert cov["buildings_grouped"] == 2
    assert cov["distinct_groups"] == 1


def test_coverage_is_zero_with_no_groups(db_session):
    cov = portfolio_coverage(db_session)
    assert cov["buildings_grouped"] == 0
    assert cov["distinct_groups"] == 0


def test_constants_are_sane():
    assert PORTFOLIO_DATE_WINDOW_DAYS > 0
    assert PORTFOLIO_RADIUS_MILES > 0
    assert PORTFOLIO_MAX_GROUP_SIZE >= 2
