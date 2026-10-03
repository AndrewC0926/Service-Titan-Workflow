from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.data.http import FixtureFetcher
from committee.data.lake import Lake, RawZone
from committee.data.pit import PIT
from committee.data.prices import Action, Bar, adjust, ingest_prices
from committee.data.quality import check_prices
from committee.data.security_master import TICKERS_URL, seed_security_master

D = Path(__file__).parent / "fixtures" / "data"
SEEDED = dt.datetime(2024, 8, 1, tzinfo=dt.UTC)
NOW = dt.datetime(2024, 9, 5, 22, 0, tzinfo=dt.UTC)


def seeded(tmp_path: Path) -> tuple[Lake, RawZone]:
    lake, raw = Lake(tmp_path / "lake"), RawZone(tmp_path / "raw")
    seed_security_master(
        lake, raw, FixtureFetcher({TICKERS_URL: D / "sec/company_tickers_exchange.json"}), SEEDED
    )
    return lake, raw


def routes() -> dict[str, Path | bytes]:
    return {
        "v2/aggs/ticker/AAPL/": D / "massive/aggs_aapl.json",
        "v3/reference/splits?apiKey=mk&limit=1000&ticker=AAPL": D / "massive/splits_aapl_p1.json",
        "v3/reference/splits?cursor=": D / "massive/splits_aapl_p2.json",
        "v3/reference/dividends?apiKey=mk&limit=1000&ticker=AAPL": D
        / "massive/dividends_aapl.json",
        "v2/aggs/ticker/MSFT/": D / "massive/error_msft.json",
        "stock/candle": D / "finnhub/candle_msft.json",
    }


def test_adjust_split_then_dividend() -> None:
    d = dt.date
    bars = [
        Bar(d(2024, 1, 2), 0, 0, 0, 200.0, 0),
        Bar(d(2024, 1, 3), 0, 0, 0, 100.0, 0),  # 2:1 split ex
        Bar(d(2024, 1, 4), 0, 0, 0, 50.0, 0),  # $1 dividend ex (prev close 100)
    ]
    acts = [
        Action(d(2024, 1, 3), "split", ratio=2.0),
        Action(d(2024, 1, 4), "dividend", amount=1.0),
    ]
    assert adjust(bars, acts) == pytest.approx([99.0, 99.0, 50.0])


def test_ingest_prices_massive_adjusts_and_finnhub_fallback(tmp_path: Path) -> None:
    lake, raw = seeded(tmp_path)
    ff = FixtureFetcher(routes())
    s = ingest_prices(
        lake, raw, ff, ["AAPL", "MSFT"], dt.date(2024, 8, 26),
        massive_key="mk", finnhub_key="fk", now=NOW,
    )  # fmt: skip
    assert s["counts"]["provider:massive"] == 1 and s["counts"]["provider:finnhub"] == 1
    assert any("cursor=" in c for c in ff.calls)  # splits pagination followed
    assert any(e.startswith("MSFT corporate actions") for e in s["errors"])
    pit = PIT(lake)
    px = pit.latest("prices_daily", NOW).sort_values(["security_id", "date"])
    aapl = px[px["security_id"] == "CIK0000320193"].reset_index(drop=True)
    div_factor = 1 - 0.25 / 104.0
    assert aapl["close"].tolist()[:3] == [400.0, 404.0, 408.0]  # raw, unadjusted
    assert aapl["adj_close"].tolist() == pytest.approx(
        [400 / 4 * div_factor, 404 / 4 * div_factor, 408 / 4 * div_factor,
         103 * div_factor, 104 * div_factor, 100.0, 101.0],
        rel=1e-6,
    )  # fmt: skip
    # known_time = 16:00 New York + 30 min (EDT -> 20:30 UTC)
    assert aapl["known_time"][0].to_pydatetime() == dt.datetime(2024, 8, 26, 20, 30, tzinfo=dt.UTC)
    before_close = dt.datetime(2024, 9, 4, 20, 29, tzinfo=dt.UTC)
    assert pit.latest("prices_daily", before_close)["date"].max().date() == dt.date(2024, 9, 3)
    msft = px[px["security_id"] == "CIK0000789019"]
    assert msft["close"].tolist() == [409.5, 408.9]  # from Finnhub
    acts = pit.latest("corporate_actions", NOW)
    assert sorted(acts["kind"]) == ["dividend", "split", "split"]
    assert dt.date(2030, 2, 1) not in {x.date() for x in acts["ex_date"]}  # not yet effective
    # the -75% raw move on the split day is matched by the split
    assert check_prices(pit, NOW).status == "pass"
    pit.close()

    again = ingest_prices(
        lake, raw, ff, ["AAPL"], dt.date(2024, 8, 26), massive_key="mk", finnhub_key=None, now=NOW
    )
    assert again["rows_written"] == {}


def test_ingest_prices_requires_a_key_and_reports_total_failure(tmp_path: Path) -> None:
    from committee.data.common import IngestError

    lake, raw = seeded(tmp_path)
    with pytest.raises(IngestError, match="MASSIVE_KEY or FINNHUB_KEY"):
        ingest_prices(lake, raw, FixtureFetcher({}), ["AAPL"], dt.date(2024, 1, 1),
                      massive_key=None, finnhub_key=None, now=NOW)  # fmt: skip
    with pytest.raises(IngestError, match="all 1 fetches failed"):
        ingest_prices(lake, raw, FixtureFetcher({}), ["AAPL"], dt.date(2024, 1, 1),
                      massive_key="k", finnhub_key="f", now=NOW)  # fmt: skip
    with pytest.raises(IngestError, match="security master"):
        ingest_prices(lake, raw, FixtureFetcher({}), ["ZZZZ"], dt.date(2024, 1, 1),
                      massive_key="k", finnhub_key=None, now=NOW)  # fmt: skip
