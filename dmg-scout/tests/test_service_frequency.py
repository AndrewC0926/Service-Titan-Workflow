"""service_calls_per_year: manual entry only, overrides the service-life
proxy as the lead ranking term when populated, and — the property under
test that matters most — survives RetrofitBuilding's own delete-and-rebuild
semantics because it's read from a separate durable table, not stored only
on the row that gets deleted every run."""
from datetime import datetime, timedelta

import httpx
import respx
from sqlmodel import select

from app.models import RetrofitBuilding, ServiceFrequencyReport
from app.pipeline.retrofit import (
    build_retrofit_buildings, latest_service_frequency_by_apn, rank_buildings,
    service_calls_coverage,
)

from tests.test_retrofit import _mock_assessor, _permit, fast_client


def test_null_service_calls_matches_the_pre_existing_formula_exactly():
    """The null path must be byte-for-byte what rank_buildings returned
    before this parameter existed."""
    kwargs = dict(service_life_status="overdue", sqft=50_000, sb1206_trigger_status="in_effect",
                  ebewe_candidate=True, carb_candidate=False, service_life_years_past=42.0)
    assert rank_buildings(**kwargs) == rank_buildings(**kwargs, service_calls_per_year=None)


def test_reported_calls_outranks_every_proxy_based_score():
    """A reported building beats even the most extreme proxy-based case:
    overdue, huge, every regulatory flag, 300 years past due."""
    max_proxy = rank_buildings(
        service_life_status="overdue", sqft=10_000_000, sb1206_trigger_status="in_effect",
        ebewe_candidate=True, carb_candidate=True, service_life_years_past=300.0,
    )
    reported_but_otherwise_unremarkable = rank_buildings(
        service_life_status="not_due", sqft=100, sb1206_trigger_status=None,
        ebewe_candidate=False, carb_candidate=False, service_life_years_past=-10.0,
        service_calls_per_year=1.0,
    )
    assert reported_but_otherwise_unremarkable > max_proxy


def test_reported_calls_ordering_among_themselves_is_by_call_count():
    low = rank_buildings(service_life_status=None, sqft=None, sb1206_trigger_status=None,
                          ebewe_candidate=False, carb_candidate=False, service_calls_per_year=3.0)
    high = rank_buildings(service_life_status=None, sqft=None, sb1206_trigger_status=None,
                           ebewe_candidate=False, carb_candidate=False, service_calls_per_year=22.0)
    assert high > low


def test_reported_calls_magnitude_is_capped():
    """Same discipline as service_life_years_past's cap -- one absurd
    figure (a typo, a bad month annualized) can't be read as more
    informative than it is."""
    capped = rank_buildings(service_life_status=None, sqft=None, sb1206_trigger_status=None,
                            ebewe_candidate=False, carb_candidate=False, service_calls_per_year=9000.0)
    at_cap = rank_buildings(service_life_status=None, sqft=None, sb1206_trigger_status=None,
                            ebewe_candidate=False, carb_candidate=False, service_calls_per_year=200.0)
    assert capped == at_cap


def test_latest_service_frequency_picks_the_most_recent_report(db_session, cfg):
    db_session.add(ServiceFrequencyReport(apn="A1", service_calls_per_year=5.0, source="Acme HVAC",
                                          reported_at=datetime(2026, 1, 1)))
    db_session.add(ServiceFrequencyReport(apn="A1", service_calls_per_year=22.0, source="Acme HVAC",
                                          reported_at=datetime(2026, 8, 1)))
    db_session.commit()
    latest = latest_service_frequency_by_apn(db_session, ["A1"])
    assert latest["A1"].service_calls_per_year == 22.0


@respx.mock
def test_manual_report_survives_a_full_rebuild(db_session, cfg):
    """The core guarantee: build_retrofit_buildings DELETEs and reinserts
    every 'recently_active' row every run (see its own docstring). A
    ServiceFrequencyReport must still be attached to the NEW row after that
    happens -- proving the join, not a column that would get wiped."""
    db_session.add(_permit("9999999999", datetime.utcnow(), "Replace RTU", "P1"))
    db_session.add(ServiceFrequencyReport(apn="9999999999", service_calls_per_year=24.0,
                                          source="Acme HVAC", reported_at=datetime(2026, 8, 1)))
    db_session.commit()
    _mock_assessor([{"AIN": "9999999999", "PropertyLocation": "1 Test Way",
                     "UseCode": "2100", "UseCodeDescChar1": "Commercial",
                     "YearBuilt": "1990", "SQFTmain": 20000}])

    stats1 = build_retrofit_buildings(db_session, cfg, fast_client())
    assert stats1["service_frequency_reports_applied"] == 1
    row1 = db_session.exec(select(RetrofitBuilding)).one()
    assert row1.service_calls_per_year == 24.0
    assert row1.service_calls_per_year_source == "Acme HVAC"
    assert row1.rank_score > 1000  # the override tier, not the proxy tier

    # rebuild again -- same permits, same report, nothing new. The row was
    # deleted and reinserted; the manual figure must reappear via the join,
    # not by accident of not having been deleted.
    stats2 = build_retrofit_buildings(db_session, cfg, fast_client())
    assert stats2["service_frequency_reports_applied"] == 1
    row2 = db_session.exec(select(RetrofitBuilding)).one()
    assert row2.built_at != row1.built_at  # genuinely rebuilt, not the same row surviving the delete
    assert row2.service_calls_per_year == 24.0
    assert row2.rank_score > 1000


@respx.mock
def test_buildings_without_a_report_rank_exactly_as_before(db_session, cfg):
    """Where null, ranking behaves exactly as it does today -- a building
    with no ServiceFrequencyReport must be completely unaffected by this
    feature existing."""
    db_session.add(_permit("1010101010", datetime.utcnow() - timedelta(days=365 * 30), "Replace boiler", "P1"))
    db_session.commit()
    _mock_assessor([{"AIN": "1010101010", "PropertyLocation": "2 Test Way",
                     "UseCode": "2100", "UseCodeDescChar1": "Commercial",
                     "YearBuilt": "1970", "SQFTmain": 15000}])

    build_retrofit_buildings(db_session, cfg, fast_client())
    row = db_session.exec(select(RetrofitBuilding)).one()
    assert row.service_calls_per_year is None
    assert row.service_calls_per_year_source is None
    assert row.rank_score < 4  # still in the tiered proxy range, nowhere near the override tier


def test_service_calls_coverage_counts_populated_rows(db_session, cfg):
    db_session.add(RetrofitBuilding(apn="A", population="recently_active", rank_score=1.0))
    db_session.add(RetrofitBuilding(apn="B", population="recently_active", rank_score=2.0,
                                    service_calls_per_year=10.0, service_calls_per_year_source="Acme"))
    db_session.commit()
    cov = service_calls_coverage(db_session)
    assert cov["retrofit_buildings_total"] == 2
    assert cov["retrofit_buildings_with_service_calls"] == 1
