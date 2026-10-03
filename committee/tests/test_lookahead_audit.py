"""Look-ahead audit (DESIGN 12, Prompt 17 step 2; REVIEW R-10).

Everything an agent can see is built from the PIT lake as of a date: evidence
packets (``PITSource``), the signal inputs (``signals.inputs.load_inputs``),
the base-rate samples and the engine facts. The strongest check is
differential: add rows that only became known AFTER the as-of date (late
revisions, late filings, future bars, a renamed company) and require every one
of those outputs to be byte-for-byte unchanged. This would fail if any read
bypassed ``as_of(known_time, ...)`` / ``PIT.latest``.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from committee.agents.base_rate_table import load_samples_from_pit
from committee.agents.packets import build_review_packet, price_path_summary
from committee.config.loader import load_config
from committee.data.pit import PIT
from committee.orchestration.pit_source import PITSource
from committee.signals.inputs import load_inputs
from fixtures.signals.builder import ASOF, LakeBuilder, bdays, kt

CFG = load_config(Path(__file__).resolve().parents[1] / "config")
S, BENCH = "CIK0000000001", "CIK0000000099"
LATE = ASOF + dt.timedelta(days=3)  # known only after the as-of date


def _base(b: LakeBuilder) -> None:
    b.company(S, "AAA", price=40, shares=1e8, n_days=420)
    b.company(BENCH, "SPY", price=500, shares=1e9, n_days=420)
    b.insider(S, "i1", dt.date(2026, 9, 5), shares=20_000, price=40)
    b.section(S, f"{S}-24", "10-K", dt.date(2024, 12, 31), "1A", "Old risk.", dt.date(2025, 2, 20))
    b.section(S, f"{S}-25", "10-K", dt.date(2025, 12, 31), "1A", "New risk.", dt.date(2026, 2, 20))
    b.news(S, ASOF - dt.timedelta(days=3), 1)
    b.fundamental(S, "revenue", "2026Q2", 2.5e8, dt.date(2026, 6, 30), dt.date(2026, 8, 5))
    b.add(
        "filings",
        {
            "accession": f"{S}-8k-1",
            "cik": 1,
            "security_id": S,
            "form": "8-K",
            "period": dt.date(2026, 8, 1),
            "accepted_at": kt(dt.date(2026, 8, 3)),
            "url": "https://example.invalid/8k",
            "items": "2.02,9.01",
            "sections_hash": None,
            "n_txns": None,
            "event_time": kt(dt.date(2026, 8, 3)),
            "known_time": kt(dt.date(2026, 8, 3)),
        },
    )
    for i, d in enumerate([dt.date(2026, 6, 1), dt.date(2026, 7, 1), dt.date(2026, 8, 1)]):
        b.add(
            "macro_series",
            {
                "series_id": "DGS10",
                "obs_date": d,
                "value": 4.0 + i / 10,
                "vintage_date": d + dt.timedelta(days=1),
                "event_time": kt(d),
                "known_time": kt(d + dt.timedelta(days=1)),
            },
        )
        b.add(
            "risk_indexes",
            {
                "index_id": "epu",
                "obs_date": d,
                "value": 100.0 + i,
                "event_time": kt(d),
                "known_time": kt(d + dt.timedelta(days=5)),
            },
        )
    b.add(
        "signals",
        {
            "security_id": S,
            "signal_name": "momentum_12_1",
            "asof": ASOF - dt.timedelta(days=7),
            "value": 0.2,
            "zscore": 1.1,
            "event_time": kt(ASOF - dt.timedelta(days=7)),
            "known_time": kt(ASOF - dt.timedelta(days=7)),
        },
    )


def _poison(b: LakeBuilder) -> None:
    """Rows about the past or the future that were only knowable after ASOF."""
    k = kt(LATE)
    last_bar = bdays(1)[0]
    b.add(
        "security_master",
        {
            "security_id": S,
            "cik": 1,
            "ticker": "PZN",
            "name": "Poison Renamed Corp",
            "exchange": "NYSE",
            "sector": "Poison",
            "list_date": dt.date(2010, 1, 1),
            "delist_date": None,
            "event_time": k,
            "known_time": k,
        },
    )
    b.add(
        "ticker_history",
        {
            "security_id": S,
            "ticker": "PZN",
            "start_date": LATE,
            "end_date": None,
            "event_time": k,
            "known_time": k,
        },
    )
    for d, px in ((last_bar, 777.0), (ASOF + dt.timedelta(days=1), 999.0)):  # revision + future bar
        b.add(
            "prices_daily",
            {
                "security_id": S,
                "date": d,
                "open": px,
                "high": px,
                "low": px,
                "close": px,
                "adj_close": px,
                "volume": 1.0,
                "event_time": kt(d),
                "known_time": max(k, kt(d)),
            },
        )
    b.fundamental(S, "revenue", "FY2025", 9.99e12, dt.date(2025, 12, 31), LATE)  # restatement
    b.fundamental(S, "shares_outstanding", "FY2025", 1.0, dt.date(2025, 12, 31), LATE)
    b.fundamental(S, "revenue", "2026Q3", 5e12, dt.date(2026, 9, 26), LATE)
    b.insider(S, "i9", dt.date(2026, 9, 20), shares=9e9, price=1.0, filed=LATE)
    for item in ("1A", "7"):
        b.section(S, f"{S}-26", "10-K", dt.date(2026, 6, 30), item, "POISON future risk.", LATE)
    b.add(
        "filings",
        {
            "accession": f"{S}-8k-late",
            "cik": 1,
            "security_id": S,
            "form": "8-K",
            "period": ASOF,
            "accepted_at": k,
            "url": "https://example.invalid/late",
            "items": "4.02",
            "sections_hash": None,
            "n_txns": None,
            "event_time": k,
            "known_time": k,
        },
    )
    b.news(S, LATE, 7)
    b.add(
        "macro_series",
        {
            "series_id": "DGS10",
            "obs_date": dt.date(2026, 8, 1),  # a revised vintage of an old observation
            "value": 424242.0,
            "vintage_date": LATE,
            "event_time": kt(dt.date(2026, 8, 1)),
            "known_time": k,
        },
    )
    b.add(
        "risk_indexes",
        {
            "index_id": "epu",
            "obs_date": dt.date(2026, 8, 1),
            "value": 424242.0,
            "event_time": kt(dt.date(2026, 8, 1)),
            "known_time": k,
        },
    )
    b.add(
        "signals",
        {
            "security_id": S,
            "signal_name": "momentum_12_1",
            "asof": ASOF - dt.timedelta(days=1),  # backfilled later
            "value": 31337.0,
            "zscore": 31337.0,
            "event_time": kt(ASOF - dt.timedelta(days=1)),
            "known_time": k,
        },
    )


def _canon(df: pd.DataFrame) -> pd.DataFrame:
    """Row order of a PIT read is not part of its contract; compare as sets of rows."""
    df = df.drop(columns=["source", "ingest_id"], errors="ignore")
    order = df.astype(str).sort_values(list(df.columns)).index
    return df.loc[order].reset_index(drop=True)


def _views(pit: PIT) -> dict[str, Any]:
    src = PITSource(pit, CFG.scenarios)
    pkt = build_review_packet(src, S, ASOF, "rv-audit", bucket_tag="CORE_PICK")
    inputs = load_inputs(pit, ASOF)
    return {
        "packet": pkt.model_dump(mode="json"),
        "security": src.security(S, ASOF).model_dump(),
        "facts": src.trading_facts(S, ASOF),
        "market_cap": src.market_cap(S, ASOF),
        "inputs": {
            f: _canon(getattr(inputs, f))
            for f in (
                "master",
                "prices",
                "fundamentals",
                "insider",
                "sections",
                "news",
            )
        },
        "samples": _canon(load_samples_from_pit(pit, ASOF, benchmark_security_id=BENCH)),
    }


@pytest.fixture(scope="module")
def views(tmp_path_factory: pytest.TempPathFactory) -> tuple[dict[str, Any], dict[str, Any]]:
    clean = LakeBuilder(tmp_path_factory.mktemp("clean"))
    _base(clean)
    clean.flush()
    dirty = LakeBuilder(tmp_path_factory.mktemp("dirty"))
    _base(dirty)
    dirty.flush()
    _poison(dirty)
    return _views(clean.pit()), _views(dirty.pit())


def test_late_rows_change_nothing_agents_or_signals_see(
    views: tuple[dict[str, Any], dict[str, Any]],
) -> None:
    a, b = views
    assert a["packet"] == b["packet"]
    assert a["security"] == b["security"]
    assert a["facts"] == b["facts"] and a["market_cap"] == b["market_cap"]
    for name in a["inputs"]:
        pd.testing.assert_frame_equal(
            a["inputs"][name], b["inputs"][name], obj=name, check_dtype=False
        )
    pd.testing.assert_frame_equal(a["samples"], b["samples"], check_dtype=False)


def test_packet_is_not_vacuous(views: tuple[dict[str, Any], dict[str, Any]]) -> None:
    """Guard the differential test: every kind the lake has actually reached the packet."""
    kinds = {i["kind"] for i in views[0]["packet"]["items"]}
    assert {
        "signals",
        "fundamentals",
        "valuation",
        "insider_txns",
        "filing_diffs",
        "filings_8k",
        "news",
        "macro",
        "risk_indexes",
        "prices",
    } <= kinds
    assert not views[0]["samples"].empty


def test_late_rows_are_visible_after_they_are_known(tmp_path: Path) -> None:
    """The poison rows are real rows: as of a later date they do show up."""
    b = LakeBuilder(tmp_path)
    _base(b)
    b.flush()
    _poison(b)
    src = PITSource(b.pit(), CFG.scenarios)
    later = LATE + dt.timedelta(days=1)
    assert src.security(S, later).ticker == "PZN"
    text = build_review_packet(src, S, later, "rv-late", bucket_tag="CORE_PICK").model_dump_json()
    assert "424242" in text and '"sector":"Poison"' in text


def test_price_path_ignores_adjustment_level() -> None:
    """REVIEW R-08: a re-adjusted history (later split/dividend) scales every adj_close
    by the same factor; what agents see is invariant to that level."""
    rows = [
        {"date": d.isoformat(), "adj_close": 50 + i * 0.3 + (i % 5)}
        for i, d in enumerate(bdays(300))
    ]
    halved = [{**r, "adj_close": r["adj_close"] / 2} for r in rows]
    assert price_path_summary(rows, ASOF) == price_path_summary(halved, ASOF)
