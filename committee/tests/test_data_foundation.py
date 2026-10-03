from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pandas as pd
import pytest

from committee.data.http import FetchError, FixtureFetcher, HttpFetcher, RateLimiter
from committee.data.lake import Lake, RawZone, new_ingest_id
from committee.data.pit import PIT, to_utc
from committee.data.security_master import TICKERS_URL, resolve, seed_security_master, sic_to_sector

T1 = dt.datetime(2026, 9, 1, 23, 0, tzinfo=dt.UTC)
T2 = dt.datetime(2026, 9, 15, 23, 0, tzinfo=dt.UTC)


def _tickers(rows: list[list[object]]) -> bytes:
    return json.dumps({"fields": ["cik", "name", "ticker", "exchange"], "data": rows}).encode()


def test_lake_write_and_asof(tmp_path: Path) -> None:
    lake = Lake(tmp_path / "lake")
    base = {"security_id": "CIK1", "metric": "revenue", "fiscal_period": "2026Q2", "event_time": T1}
    lake.write(
        "fundamentals",
        [{**base, "value": 100.0, "known_time": T1}],
        source="t",
        ingest_id=new_ingest_id(),
    )
    lake.write(
        "fundamentals",
        [{**base, "value": 90.0, "known_time": T2}],
        source="t",
        ingest_id=new_ingest_id(),
    )
    pit = PIT(lake)
    assert pit.latest("fundamentals", T1 + dt.timedelta(days=1))["value"].tolist() == [100.0]
    assert pit.latest("fundamentals", T2)["value"].tolist() == [
        90.0
    ]  # restatement visible only after T2
    assert pit.latest("fundamentals", T1 - dt.timedelta(seconds=1)).empty
    df = pit.query(
        "SELECT count(*) AS n FROM fundamentals WHERE as_of(known_time, $asof)", "2026-09-10"
    )
    assert int(df["n"][0]) == 1


def test_lake_rejects_missing_lineage(tmp_path: Path) -> None:
    lake = Lake(tmp_path)
    with pytest.raises(ValueError, match="known_time"):
        lake.write("news", [{"news_id": "1", "event_time": T1}], source="x", ingest_id="a")
    with pytest.raises(ValueError, match="aware"):
        RawZone(tmp_path).put("s", "e", dt.datetime(2026, 1, 1), b"x")
    with pytest.raises(ValueError):
        lake.table_dir("not_a_table")


def test_raw_zone_immutable_and_idempotent(tmp_path: Path) -> None:
    rz = RawZone(tmp_path)
    p1 = rz.put("sec", "AAPL", T1, b"hello")
    p2 = rz.put("sec", "AAPL", T1, b"hello")
    assert p1 == p2 and rz.read(p1) == b"hello"
    p3 = rz.put("sec", "AAPL", T1, b"different")
    assert p3 != p1 and len(rz.list("sec", "AAPL")) == 2


def test_to_utc() -> None:
    assert to_utc("2026-09-27") == dt.datetime(2026, 9, 27, 23, 59, 59, tzinfo=dt.UTC)
    assert to_utc(dt.date(2026, 1, 2)).day == 2


def test_security_master_keeps_delisted_and_tracks_ticker_changes(tmp_path: Path) -> None:
    lake, rz = Lake(tmp_path / "lake"), RawZone(tmp_path / "raw")
    f1 = FixtureFetcher(
        {
            TICKERS_URL: _tickers(
                [
                    [320193, "Apple Inc.", "AAPL", "Nasdaq"],
                    [1, "Old Co", "OLD", "NYSE"],
                    [2, "Facebook", "FB", "Nasdaq"],
                ]
            )
        }
    )
    out = seed_security_master(lake, rz, f1, T1, sic_by_cik={320193: 3571, 1: 6022, 2: 7370})
    assert out["securities_written"] == 3
    f2 = FixtureFetcher(
        {
            TICKERS_URL: _tickers(
                [[320193, "Apple Inc.", "AAPL", "Nasdaq"], [2, "Meta Platforms", "META", "Nasdaq"]]
            )
        }
    )
    out2 = seed_security_master(lake, rz, f2, T2)
    assert out2["delisted"] == 1
    pit = PIT(lake)
    sm = pit.latest("security_master", T2).set_index("ticker")
    assert set(sm.index) == {"AAPL", "OLD", "META"}  # delisted name survives
    assert pd.Timestamp(sm.loc["OLD", "delist_date"]).date() == T2.date()
    assert sm.loc["AAPL", "sector"] == "Information Technology"
    assert sm.loc["META", "sector"] == "Information Technology"
    assert resolve(pit, "META", T2) == "CIK0000000002"
    assert resolve(pit, "FB", T1) == "CIK0000000002"
    assert resolve(pit, "META", T1) is None  # not knowable yet
    # third run with no changes writes nothing new
    assert seed_security_master(lake, rz, f2, T2 + dt.timedelta(days=1))["securities_written"] == 0


def test_sic_to_sector() -> None:
    assert sic_to_sector(2834) == "Health Care"
    assert sic_to_sector(1311) == "Energy"
    assert sic_to_sector(6021) == "Financials"
    assert sic_to_sector(None) == "Unknown"
    assert sic_to_sector("abc") == "Unknown"


def test_http_fetcher_retries_then_succeeds() -> None:
    calls = {"n": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        calls["n"] += 1
        return httpx.Response(503) if calls["n"] < 3 else httpx.Response(200, content=b"ok")

    sleeps: list[float] = []
    f = HttpFetcher(transport=httpx.MockTransport(handler), sleep=sleeps.append, per_second=1000)
    assert f.get("https://x/y") == b"ok"
    assert calls["n"] == 3
    assert [s for s in sleeps if s >= 1] == [1.0, 2.0]  # exponential backoff


def test_http_fetcher_gives_up_and_non_retryable() -> None:
    f = HttpFetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(429)),
        sleep=lambda s: None,
        max_retries=2,
    )
    with pytest.raises(FetchError):
        f.get("https://x")
    f2 = HttpFetcher(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), sleep=lambda s: None
    )
    with pytest.raises(FetchError) as e:
        f2.get("https://x")
    assert e.value.status == 404


def test_rate_limiter() -> None:
    t = [0.0]
    sleeps: list[float] = []

    def sleep(s: float) -> None:
        sleeps.append(s)
        t[0] += s

    rl = RateLimiter(8, clock=lambda: t[0], sleep=sleep)
    for _ in range(3):
        rl.wait()
    assert sleeps == pytest.approx([0.125, 0.125])


def test_fixture_fetcher_missing_route() -> None:
    with pytest.raises(FetchError):
        FixtureFetcher({}).get("https://nothing")
