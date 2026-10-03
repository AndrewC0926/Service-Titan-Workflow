"""Regression tests for data-layer bugs found in the Prompt 17 review (docs/REVIEW.md)."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pytest

from committee.data.http import FixtureFetcher
from committee.data.lake import Lake, RawZone
from committee.data.pit import PIT
from committee.data.prices import ingest_prices
from committee.data.security_master import TICKERS_URL, seed_security_master

D = Path(__file__).parent / "fixtures" / "data"
AAPL = "CIK0000320193"


def _aggs(closes: dict[dt.date, float]) -> bytes:
    rows = [
        {
            "t": int(dt.datetime.combine(d, dt.time(16), tzinfo=dt.UTC).timestamp() * 1000),
            "o": c,
            "h": c,
            "l": c,
            "c": c,
            "v": 1e6,
        }
        for d, c in sorted(closes.items())
    ]
    return json.dumps({"status": "OK", "results": rows}).encode()


def _actions(splits: list[dict[str, object]]) -> dict[str, bytes]:
    return {
        "v3/reference/splits": json.dumps({"status": "OK", "results": splits}).encode(),
        "v3/reference/dividends": json.dumps({"status": "OK", "results": []}).encode(),
    }


def test_r09_new_split_readjusts_the_whole_stored_history(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A split learned on a short nightly window must not leave older rows on the old
    adjustment basis (the latest-version read would show a fake -50% day)."""
    ids = iter(f"20240901T00000{i}-run" for i in range(1, 10))  # runs hours apart in real life
    monkeypatch.setattr("committee.data.prices.new_ingest_id", lambda: next(ids))
    lake, raw = Lake(tmp_path / "lake"), RawZone(tmp_path / "raw")
    seed_security_master(
        lake,
        raw,
        FixtureFetcher({TICKERS_URL: D / "sec/company_tickers_exchange.json"}),
        dt.datetime(2024, 8, 1, tzinfo=dt.UTC),
    )
    pre = {dt.date(2024, 8, d): 200.0 + d for d in (26, 27, 28, 29, 30)}
    post = {dt.date(2024, 9, d): 100.0 + d for d in (9, 10, 11)}
    first = FixtureFetcher({"v2/aggs/ticker/AAPL/": _aggs(pre), **_actions([])})
    ingest_prices(lake, raw, first, ["AAPL"], dt.date(2024, 8, 26), massive_key="mk",
                  finnhub_key=None, now=dt.datetime(2024, 8, 31, tzinfo=dt.UTC))  # fmt: skip
    split = [{"execution_date": "2024-09-09", "split_from": 1, "split_to": 2}]
    nightly = FixtureFetcher(
        {
            "range/1/day/2024-09-09/": _aggs(post),  # the short nightly window
            "range/1/day/2024-08-26/": _aggs({**pre, **post}),  # the full stored history
            **_actions(split),
        }
    )
    s = ingest_prices(lake, raw, nightly, ["AAPL"], dt.date(2024, 9, 9), massive_key="mk",
                      finnhub_key=None, now=dt.datetime(2024, 9, 12, tzinfo=dt.UTC))  # fmt: skip
    assert s["counts"].get("readjusted_history") == 1
    assert any("range/1/day/2024-08-26/" in c for c in nightly.calls)
    px = PIT(lake).latest("prices_daily", dt.datetime(2024, 9, 12, tzinfo=dt.UTC))
    px = px[px["security_id"] == AAPL].sort_values("date")
    adj = dict(zip((d.date() for d in px["date"]), px["adj_close"], strict=True))
    for d, c in pre.items():
        assert adj[d] == pytest.approx(c / 2)  # every pre-split row on the new basis
    # The daily return across the split is the real one, not -50%.
    r = adj[dt.date(2024, 9, 9)] / adj[dt.date(2024, 8, 30)] - 1
    assert r == pytest.approx(109.0 / (230.0 / 2) - 1)
    # Run again: the action is known now, so no further history refetch.
    again = ingest_prices(lake, raw, nightly, ["AAPL"], dt.date(2024, 9, 9), massive_key="mk",
                          finnhub_key=None, now=dt.datetime(2024, 9, 12, tzinfo=dt.UTC))  # fmt: skip
    assert "readjusted_history" not in again["counts"]


def test_r09_null_keys_from_nullable_columns_dedupe() -> None:
    import pandas as pd

    from committee.data.common import norm_key

    assert norm_key(pd.NA) == norm_key(None) == norm_key(float("nan")) == norm_key(pd.NaT) == ""


def test_r09_ingest_ids_sort_in_run_order() -> None:
    import time

    from committee.data.lake import new_ingest_id

    a = new_ingest_id()
    time.sleep(0.001)
    b = new_ingest_id()
    assert a < b
    # an id in the older (seconds) format sorts before a newer id from the same second
    assert "20261003T120000-ffffffff" < "20261003T120000000000-00000000"
