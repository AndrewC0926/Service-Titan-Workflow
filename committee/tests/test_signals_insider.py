"""Insider classifier (Cohen-Malloy-Pomorski), purchase score, cluster buys, amendments."""

from __future__ import annotations

import datetime as dt
import math
from pathlib import Path

import pandas as pd
import pytest

from committee.config.schema import InsiderSettings
from committee.signals.inputs import load_inputs
from committee.signals.insider import (
    cluster_insiders,
    insider_signals,
    is_routine,
    prepare,
    purchase_score,
)
from fixtures.signals.builder import ASOF, LakeBuilder

SETTINGS = InsiderSettings(
    routine_lookback_years=3, score_window_days=90, cluster_min_insiders=3, cluster_window_days=30
)
D = pd.Timestamp(ASOF)
SID = "CIK0000000001"
MCAP = 1e9


def run(b: LakeBuilder, asof: dt.date = ASOF, mcap: float = MCAP):
    pit = b.pit()
    inp = load_inputs(pit, asof)
    pit.close()
    return insider_signals(inp.insider, {SID: mcap}, pd.Timestamp(asof), SETTINGS, 30)[SID]


@pytest.fixture
def b(tmp_path: Path) -> LakeBuilder:
    return LakeBuilder(tmp_path / "lake")


# ------------------------------------------------------------------ routine rule
def test_is_routine_needs_same_month_in_each_of_prior_three_years() -> None:
    hist = {(2023, 9), (2024, 9), (2025, 9)}
    assert is_routine(hist, 2026, 9, 3)
    assert not is_routine({(2023, 9), (2025, 9)}, 2026, 9, 3)  # only 2 of 3 years
    assert not is_routine({(2023, 9), (2024, 10), (2025, 9)}, 2026, 9, 3)  # other month
    assert not is_routine(set(), 2026, 9, 3)  # no history -> opportunistic
    # current-year trades (including the as-of month) are never history
    assert not is_routine({(2026, 9), (2025, 9), (2024, 9)}, 2026, 9, 3)


def test_routine_insider_purchases_do_not_score(b: LakeBuilder) -> None:
    for y in (2023, 2024, 2025):
        b.insider(SID, "routine", dt.date(y, 9, 10), code="S", ad="D")
    b.insider(SID, "routine", dt.date(2026, 9, 10), shares=5000, price=20)
    sig = run(b)
    assert sig.routine_buyers == ("routine",)
    assert sig.opportunistic_buyers == ()
    assert sig.dollars == 0.0 and sig.score == 0.0


def test_two_of_three_years_is_opportunistic(b: LakeBuilder) -> None:
    for y in (2023, 2025):
        b.insider(SID, "x", dt.date(y, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2026, 9, 10), shares=1000, price=10)
    sig = run(b)
    assert sig.opportunistic_buyers == ("x",)
    assert sig.dollars == 10_000


def test_same_month_different_years_but_wrong_month_is_opportunistic(b: LakeBuilder) -> None:
    b.insider(SID, "x", dt.date(2023, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2024, 8, 10), code="S", ad="D")  # August, not September
    b.insider(SID, "x", dt.date(2025, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2026, 9, 10))
    assert run(b).opportunistic_buyers == ("x",)


def test_asof_month_trades_do_not_make_an_insider_routine(b: LakeBuilder) -> None:
    b.insider(SID, "x", dt.date(2024, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2025, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2026, 9, 2))  # earlier trade in the as-of month itself
    b.insider(SID, "x", dt.date(2026, 9, 20))
    sig = run(b)
    assert sig.opportunistic_buyers == ("x",)
    assert sig.n_purchases == 2


def test_classification_is_per_trade_year(b: LakeBuilder) -> None:
    # Routine for a trade in Jan 2026 (Jan 2023/24/25), but a July 2026 buy is opportunistic.
    for y in (2023, 2024, 2025):
        b.insider(SID, "x", dt.date(y, 1, 15), code="S", ad="D")
    b.insider(SID, "x", dt.date(2026, 7, 15))
    df = prepare(load_inputs(b.pit(), ASOF).insider, 3)
    row = df[df["txn_date"] == pd.Timestamp("2026-07-15")]
    assert not bool(row["routine"].iloc[0])


def test_history_known_after_asof_is_ignored(b: LakeBuilder) -> None:
    b.insider(SID, "x", dt.date(2023, 9, 10), code="S", ad="D")
    b.insider(SID, "x", dt.date(2024, 9, 10), code="S", ad="D")
    # a late Form 4 for Sep 2025 that is only filed after the as-of date
    b.insider(SID, "x", dt.date(2025, 9, 10), code="S", ad="D", filed=dt.date(2026, 10, 5))
    b.insider(SID, "x", dt.date(2026, 9, 10))
    assert run(b).opportunistic_buyers == ("x",)
    assert run(b, asof=dt.date(2026, 10, 10)).routine_buyers == ("x",)


# ------------------------------------------------------------------ score
def test_purchase_score_formula() -> None:
    assert purchase_score(0.0, 1e9) == 0.0
    assert purchase_score(1e6, 1e9) == pytest.approx(math.log1p(10.0))  # 10 bps
    assert purchase_score(1e6, None) is None
    assert purchase_score(1e6, 0.0) is None


def test_score_filters_10b5_1_derivative_sales_and_window(b: LakeBuilder) -> None:
    b.insider(SID, "a", dt.date(2026, 9, 1), shares=1000, price=50)  # counts: 50k
    b.insider(SID, "b", dt.date(2026, 9, 1), is_10b5_1=True)
    b.insider(SID, "c", dt.date(2026, 9, 1), is_derivative=True)
    b.insider(SID, "d", dt.date(2026, 9, 1), code="S", ad="D")
    b.insider(SID, "e", dt.date(2026, 6, 28))  # 91 days before as-of: outside window
    b.insider(SID, "f", dt.date(2026, 6, 30), shares=100, price=10)  # day 90: inside
    b.insider(SID, "g", dt.date(2026, 9, 1), code="A")  # grant, not open market
    b.insider(SID, "h", dt.date(2026, 9, 25), filed=dt.date(2026, 9, 29))  # filed after as-of
    sig = run(b)
    assert sig.opportunistic_buyers == ("a", "f")
    assert sig.dollars == 51_000
    assert sig.score == pytest.approx(math.log1p(1e4 * 51_000 / MCAP))


def test_amendment_replaces_original(b: LakeBuilder) -> None:
    b.insider(SID, "a", dt.date(2026, 9, 1), shares=1000, price=10, accession="orig")
    b.insider(
        SID, "a", dt.date(2026, 9, 1), shares=3000, price=10,
        accession="amend", is_amendment=True, filed=dt.date(2026, 9, 10),
    )  # fmt: skip
    sig = run(b)
    assert sig.dollars == 30_000 and sig.n_purchases == 1


def test_same_accession_line_correction_uses_latest_version(b: LakeBuilder) -> None:
    b.insider(SID, "a", dt.date(2026, 9, 1), shares=1000, price=10, accession="acc", line=1)
    b.flush()
    b.insider(
        SID, "a", dt.date(2026, 9, 1), shares=2000, price=10,
        accession="acc", line=1, filed=dt.date(2026, 9, 12),
    )  # fmt: skip
    assert run(b).dollars == 20_000
    assert run(b, asof=dt.date(2026, 9, 5)).dollars == 10_000


# ------------------------------------------------------------------ clusters
def _dates(*ds: str) -> pd.Series:
    return pd.Series(pd.to_datetime(list(ds)))


def test_cluster_requires_three_distinct_insiders_within_30_days() -> None:
    ins = pd.Series(["a", "b", "c"])
    assert cluster_insiders(_dates("2026-09-01", "2026-09-15", "2026-09-30"), ins, 3, 30) == (
        "a",
        "b",
        "c",
    )
    assert cluster_insiders(_dates("2026-09-01", "2026-09-15", "2026-10-01"), ins, 3, 30) == ()
    same = pd.Series(["a", "a", "b"])
    assert cluster_insiders(_dates("2026-09-01", "2026-09-02", "2026-09-03"), same, 3, 30) == ()


def test_cluster_flags_recent_vs_older(b: LakeBuilder) -> None:
    for i, d in enumerate([dt.date(2026, 7, 20), dt.date(2026, 7, 25), dt.date(2026, 8, 1)]):
        b.insider(SID, f"old{i}", d)
    sig = run(b)
    assert sig.cluster_buy and not sig.recent_cluster_buy
    for i, d in enumerate([dt.date(2026, 9, 1), dt.date(2026, 9, 10), dt.date(2026, 9, 25)]):
        b.insider(SID, f"new{i}", d)
    sig = run(b)
    assert sig.cluster_buy and sig.recent_cluster_buy


def test_routine_buyers_do_not_count_toward_clusters(b: LakeBuilder) -> None:
    for y in (2023, 2024, 2025):
        b.insider(SID, "r", dt.date(y, 9, 3), code="S", ad="D")
    for who, d in (("r", 3), ("x", 10), ("y", 20)):
        b.insider(SID, who, dt.date(2026, 9, d))
    sig = run(b)
    assert not sig.cluster_buy and not sig.recent_cluster_buy
