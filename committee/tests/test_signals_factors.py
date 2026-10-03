"""Within-sector z-scores, winsorization, rank averages, fundamentals and price formulas."""

from __future__ import annotations

import datetime as dt
import math

import numpy as np
import pandas as pd
import pytest

from committee.signals.fundamentals import Fundamentals, factor_metrics, fundamentals_by_security
from committee.signals.prices import price_stats
from committee.signals.stats import rank_average, sector_pct_rank, sector_zscore, winsorize
from fixtures.signals.builder import ASOF, bdays

D = pd.Timestamp(ASOF)


# ------------------------------------------------------------------ z-scores
def test_zscore_is_within_sector() -> None:
    v = pd.Series({"a": 1.0, "b": 2.0, "c": 3.0, "x": 100.0, "y": 300.0})
    s = pd.Series({"a": "T", "b": "T", "c": "T", "x": "E", "y": "E"})
    z = sector_zscore(v, s, 3.0)
    sd = np.std([1.0, 2.0, 3.0])
    assert z["a"] == pytest.approx(-1 / sd) and z["b"] == 0.0 and z["c"] == pytest.approx(1 / sd)
    assert z["x"] == pytest.approx(-1.0) and z["y"] == pytest.approx(1.0)


def test_zscore_degenerate_sectors_and_missing() -> None:
    v = pd.Series({"a": 5.0, "b": 7.0, "c": 7.0, "d": np.nan, "e": 1.0})
    s = pd.Series({"a": "Solo", "b": "Flat", "c": "Flat", "d": "Mix", "e": "Mix"})
    z = sector_zscore(v, s, 3.0)
    assert z["a"] == 0.0  # single member
    assert z["b"] == 0.0 and z["c"] == 0.0  # zero dispersion
    assert math.isnan(z["d"]) and z["e"] == 0.0  # missing stays missing
    noisy = pd.Series({"p": 16 / 28, "q": (4 + 7 + 1 + 4) / 28, "r": (4 + 3.5 + 4.5 + 4) / 28})
    assert sector_zscore(noisy, pd.Series("T", index=noisy.index), 3.0).tolist() == [0.0] * 3


def test_winsorize_at_three() -> None:
    v = pd.Series([0.0] * 19 + [100.0], index=[f"n{i}" for i in range(20)])
    s = pd.Series("T", index=v.index)
    z = sector_zscore(v, s, 3.0)
    assert z["n19"] == 3.0  # raw z = sqrt(19) = 4.36
    assert sector_zscore(v, s, 10.0)["n19"] == pytest.approx(math.sqrt(19))
    assert winsorize(pd.Series([-5.0, 1.0]), 3.0).tolist() == [-3.0, 1.0]


def test_rank_average_within_sector() -> None:
    m = pd.DataFrame({"x": [1.0, 2.0, 10.0], "y": [3.0, 1.0, np.nan]}, index=["a", "b", "c"])
    s = pd.Series({"a": "T", "b": "T", "c": "E"})
    assert sector_pct_rank(m["x"], s).to_dict() == {"a": 0.5, "b": 1.0, "c": 1.0}
    r = rank_average(m, s)
    assert r["a"] == pytest.approx((0.5 + 1.0) / 2)
    assert r["b"] == pytest.approx((1.0 + 0.5) / 2)
    assert r["c"] == 1.0  # only x available


# ------------------------------------------------------------------ fundamentals
def _f(rows: list[tuple[str, str, float, str]]) -> Fundamentals:
    df = pd.DataFrame(
        [{"security_id": "S", "metric": m, "fiscal_period": fp, "value": v, "period_end": pe}
         for m, fp, v, pe in rows]
    )  # fmt: skip
    return fundamentals_by_security(df)["S"]


def test_ttm_sums_last_four_quarters_and_prior_year() -> None:
    q = [
        ("revenue", f"{y}Q{i}", float(10 * (y - 2024) + i), "2026-01-01")
        for y in (2024, 2025, 2026)
        for i in (1, 2, 3, 4)
    ]
    f = _f([r for r in q if r[1] not in ("2026Q3", "2026Q4")])
    # latest run ends 2026Q2: 2025Q3 + 2025Q4 + 2026Q1 + 2026Q2
    assert f.ttm("revenue") == 13 + 14 + 21 + 22
    assert f.prior("revenue") == 3 + 4 + 11 + 12
    assert f.flows["revenue"].basis == "ttm"


def test_q4_derived_from_fy_and_fy_fallback() -> None:
    rows = [
        ("revenue", "FY2025", 100.0, "2025-12-31"),
        ("revenue", "2025Q1", 20.0, "2025-03-31"),
        ("revenue", "2025Q2", 25.0, "2025-06-30"),
        ("revenue", "2025Q3", 25.0, "2025-09-30"),
        ("revenue", "2026Q1", 30.0, "2026-03-31"),
        ("revenue", "FY2024", 80.0, "2024-12-31"),
    ]
    f = _f(rows)
    # Q4 2025 = 100 - 70 = 30; TTM to 2026Q1 = 25 + 25 + 30 + 30
    assert f.ttm("revenue") == 110.0
    fy_only = _f(
        [("revenue", "FY2025", 100.0, "2025-12-31"), ("revenue", "FY2024", 80.0, "2024-12-31")]
    )
    assert fy_only.ttm("revenue") == 100.0 and fy_only.prior("revenue") == 80.0
    assert fy_only.revenue_growth == pytest.approx(0.25)
    assert fy_only.flows["revenue"].basis == "fy"


def test_balance_latest_and_year_ago() -> None:
    f = _f([
        ("shares_outstanding", "2025Q2", 100.0, "2025-06-30"),
        ("shares_outstanding", "2026Q1", 105.0, "2026-03-31"),
        ("shares_outstanding", "2026Q2", 115.0, "2026-06-30"),
    ])  # fmt: skip
    assert f.stock("shares_outstanding") == 115.0
    assert f.share_growth == pytest.approx(0.15)


def test_factor_metric_formulas() -> None:
    f = _f([
        ("net_income", "FY2025", 100.0, "2025-12-31"),
        ("cfo", "FY2025", 150.0, "2025-12-31"),
        ("capex", "FY2025", -30.0, "2025-12-31"),
        ("ebit", "FY2025", 200.0, "2025-12-31"),
        ("gross_profit", "FY2025", 400.0, "2025-12-31"),
        ("total_assets", "FY2025", 2000.0, "2025-12-31"),
        ("equity", "FY2025", 800.0, "2025-12-31"),
        ("debt", "FY2025", 300.0, "2025-12-31"),
        ("cash", "FY2025", 100.0, "2025-12-31"),
    ])  # fmt: skip
    m = factor_metrics(f, 1000.0)
    assert m.earnings_yield == pytest.approx(0.10)
    assert m.fcf_yield == pytest.approx(0.12)  # (150 - |-30|) / 1000
    assert m.book_to_market == pytest.approx(0.80)  # equity when book_value missing
    assert m.ebit_to_ev == pytest.approx(200 / 1200)  # EV = 1000 + 300 - 100
    assert m.gross_profitability == pytest.approx(0.20)
    assert m.roic == pytest.approx(200 * 0.79 / 1000)  # IC = 800 + 300 - 100
    assert m.accruals == pytest.approx(-50 / 2000)
    assert m.leverage == pytest.approx(0.15)


# ------------------------------------------------------------------ prices
def _prices(sid: str, path: list[float], vol: float = 1000.0) -> pd.DataFrame:
    days = bdays(len(path))
    return pd.DataFrame(
        {
            "security_id": sid,
            "date": pd.to_datetime(days),
            "close": path,
            "adj_close": path,
            "volume": vol,
        }
    )


def test_momentum_12_1_skips_latest_month_and_adv() -> None:
    days = bdays(300)
    # price 100 until 12 months ago, 150 until 1 month ago, then 300 in the last month
    path = [
        100.0 if d <= dt.date(2025, 9, 26) else 150.0 if d <= dt.date(2026, 8, 27) else 300.0
        for d in days
    ]
    st = price_stats(_prices("S", path), D, 60)["S"]
    assert st.momentum_12_1 == pytest.approx(0.5)
    assert st.return_3m == pytest.approx(1.0)
    assert st.max_return_1m == pytest.approx(1.0)
    assert st.last_close == 300.0
    assert st.adv == pytest.approx(np.mean([p * 1000 for p in path[-60:]]))


def test_volatility_and_beta() -> None:
    n = 260
    mret = [0.01 * math.sin(i) for i in range(n)]
    mkt, stock = [100.0], [100.0]
    for r in mret[1:]:
        mkt.append(mkt[-1] * (1 + r))
        stock.append(stock[-1] * (1 + 2 * r))
    prices = pd.concat([_prices("M", mkt), _prices("S", stock)])
    days = pd.to_datetime(bdays(n))
    market = pd.Series(mret, index=days).iloc[1:]
    st = price_stats(prices, D, 60, market=market)
    assert st["S"].beta_1y == pytest.approx(2.0, rel=1e-6)
    assert st["M"].beta_1y == pytest.approx(1.0, rel=1e-6)
    expected = float(pd.Series(stock).pct_change().iloc[-251:].std(ddof=1)) * math.sqrt(252)
    assert st["S"].volatility_1y == pytest.approx(expected, rel=0.05)


def test_prices_after_asof_are_ignored() -> None:
    p = _prices("S", [10.0] * 100)
    late = pd.DataFrame(
        {
            "security_id": ["S"],
            "date": [pd.Timestamp("2026-09-28")],
            "close": [99.0],
            "adj_close": [99.0],
            "volume": [1.0],
        }
    )
    st = price_stats(pd.concat([p, late]), D, 60)["S"]
    assert st.last_close == 10.0 and st.max_return_1m == 0.0
