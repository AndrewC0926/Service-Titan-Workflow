"""Change-of-ownership recency: the Assessor FeatureServer fetch (batched,
paginated, max-RecordingDate-per-AIN), the durable OwnershipRecency table
that survives RetrofitBuilding's own rebuild, and rank_buildings' recency
term. See app/pipeline/ownership.py's module docstring for the LA County
Recorder compliance finding this whole module exists because of.

The fetch HTTP call is mocked (respx); these tests are about batching,
date-parsing, the durable-table pattern, and the ranking math, not live
network."""
from datetime import datetime, timedelta

import httpx
import respx
from sqlmodel import select

from app.http import PoliteClient
from app.models import OwnershipRecency, RetrofitBuilding, SourceRun
from app.pipeline.ownership import (
    FEATURE_SERVER, _epoch_ms_to_datetime, fetch_ownership_recency, ownership_recency_coverage,
)
from app.pipeline.retrofit import RECENCY_HALFLIFE_MONTHS, rank_buildings


def fast_client() -> PoliteClient:
    return PoliteClient(interval=0, max_retries=0, respect_robots=False)


def _feature(ain: str, recording_date_ms: int | None) -> dict:
    return {"attributes": {"AIN": ain, "RecordingDate": recording_date_ms}}


# --- _epoch_ms_to_datetime ----------------------------------------------------

def test_epoch_ms_converts_to_a_real_datetime():
    dt = _epoch_ms_to_datetime(858931200000)
    assert dt.year == 1997


def test_epoch_ms_none_is_none():
    assert _epoch_ms_to_datetime(None) is None


# --- fetch_ownership_recency: batching, max-per-AIN, durable table -----------

@respx.mock
def test_fetch_upserts_max_recording_date_per_ain(db_session):
    # Same AIN appears twice (two roll years) with different dates -- the
    # max must win, same reasoning as the module docstring.
    respx.post(FEATURE_SERVER).mock(return_value=httpx.Response(200, json={
        "features": [
            _feature("1234567890", 1450684800000),
            _feature("1234567890", 1609228800000),
        ],
        "exceededTransferLimit": False,
    }))
    db_session.add(RetrofitBuilding(apn="1234567890", population="replacement_candidate"))
    db_session.commit()

    with fast_client() as client:
        stats = fetch_ownership_recency(db_session, cfg=None, client=client)

    assert stats["ok"] is True
    assert stats["matched"] == 1
    row = db_session.exec(select(OwnershipRecency).where(OwnershipRecency.apn == "1234567890")).one()
    assert row.last_sale_date == _epoch_ms_to_datetime(1609228800000)


@respx.mock
def test_fetch_records_a_source_run(db_session):
    respx.post(FEATURE_SERVER).mock(return_value=httpx.Response(200, json={
        "features": [_feature("1111111111", 1450684800000)], "exceededTransferLimit": False,
    }))
    db_session.add(RetrofitBuilding(apn="1111111111", population="replacement_candidate"))
    db_session.commit()

    with fast_client() as client:
        fetch_ownership_recency(db_session, cfg=None, client=client)

    runs = db_session.exec(select(SourceRun).where(SourceRun.source == "la_county_ownership")).all()
    assert len(runs) == 1
    assert runs[0].ok is True


@respx.mock
def test_fetch_rerun_updates_not_duplicates(db_session):
    db_session.add(RetrofitBuilding(apn="2222222222", population="replacement_candidate"))
    db_session.commit()

    respx.post(FEATURE_SERVER).mock(return_value=httpx.Response(200, json={
        "features": [_feature("2222222222", 1450684800000)], "exceededTransferLimit": False,
    }))
    with fast_client() as client:
        fetch_ownership_recency(db_session, cfg=None, client=client)

    respx.post(FEATURE_SERVER).mock(return_value=httpx.Response(200, json={
        "features": [_feature("2222222222", 1609228800000)], "exceededTransferLimit": False,
    }))
    with fast_client() as client:
        fetch_ownership_recency(db_session, cfg=None, client=client)

    rows = db_session.exec(select(OwnershipRecency).where(OwnershipRecency.apn == "2222222222")).all()
    assert len(rows) == 1
    assert rows[0].last_sale_date == _epoch_ms_to_datetime(1609228800000)


# --- ownership_recency_coverage -----------------------------------------------

def test_coverage_counts_matched_and_recent(db_session):
    now = datetime(2026, 8, 16)
    db_session.add(RetrofitBuilding(apn="A", population="replacement_candidate",
                                    last_sale_date=now - timedelta(days=30)))
    db_session.add(RetrofitBuilding(apn="B", population="replacement_candidate",
                                    last_sale_date=now - timedelta(days=3000)))
    db_session.add(RetrofitBuilding(apn="C", population="replacement_candidate", last_sale_date=None))
    db_session.commit()

    cov = ownership_recency_coverage(db_session)
    assert cov["retrofit_buildings_total"] == 3
    assert cov["matched"] == 2
    assert cov["sold_last_24mo"] == 1


# --- rank_buildings: the recency term ------------------------------------------

def _rank(**kw):
    base = dict(service_life_status="overdue", sqft=500000, sb1206_trigger_status=None,
               ebewe_candidate=False, carb_candidate=False, service_life_years_past=30)
    return rank_buildings(**{**base, **kw})


def test_recent_sale_outranks_no_sale_within_the_same_tier():
    # The instruction's own worked example: 30yr past due, just sold, vs.
    # 30yr past due, never sold on record.
    just_sold = _rank(months_since_sale=0)
    never_sold = _rank(months_since_sale=None)
    assert just_sold > never_sold


def test_recent_sale_outranks_a_stale_sale_within_the_same_tier():
    just_sold = _rank(months_since_sale=0)
    sold_20_years_ago = _rank(months_since_sale=240)
    assert just_sold > sold_20_years_ago


def test_stale_sale_is_close_to_no_sale_at_all():
    # A sale two decades old has decayed to near-nothing at a 24-month
    # half-life -- should score close to, not meaningfully above, no sale.
    sold_20_years_ago = _rank(months_since_sale=240)
    never_sold = _rank(months_since_sale=None)
    assert abs(sold_20_years_ago - never_sold) < 0.01


def test_recency_never_crosses_a_tier():
    # Same invariant as magnitude/size/regulatory pressure: no combination
    # can make a "due" building outrank an "overdue" one.
    maxed_out_due = rank_buildings(
        service_life_status="due", sqft=2_000_000, sb1206_trigger_status="in_effect",
        ebewe_candidate=True, carb_candidate=True, service_life_years_past=100,
        months_since_sale=0,
    )
    barely_overdue = rank_buildings(
        service_life_status="overdue", sqft=1, sb1206_trigger_status=None,
        ebewe_candidate=False, carb_candidate=False, service_life_years_past=0,
        months_since_sale=None,
    )
    assert barely_overdue > maxed_out_due


def test_null_months_since_sale_contributes_nothing_same_as_before():
    # Backward compatibility: omitting months_since_sale entirely must be
    # byte-identical to passing None explicitly.
    assert _rank() == _rank(months_since_sale=None)


def test_recency_half_life_constant_is_positive():
    assert RECENCY_HALFLIFE_MONTHS > 0
