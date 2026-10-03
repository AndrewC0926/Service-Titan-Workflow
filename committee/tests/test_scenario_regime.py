from __future__ import annotations

import datetime as dt

import pandas as pd
import pytest

from committee.engines.scenario import MacroInputs, regime_snapshot
from committee.engines.scenario.regime import real_rate_level

BASE = {
    "fed_funds": 4.0,
    "ust_10y": 4.3,
    "ust_2y": 3.9,
    "breakeven_10y": 2.3,
    "term_premium_10y": 0.6,
    "oil": 85.0,
    "dollar": 120.0,
    "gpr": 110.0,
    "epu": 120.0,
}


def snap(**kw: float) -> object:
    return regime_snapshot({**BASE, **kw})


@pytest.mark.parametrize(
    ("growth_kw", "infl_kw", "quadrant"),
    [
        (
            {"unemployment": 4.0, "unemployment_low_12m": 4.0, "slope_10y_3m": 1.5},
            {"core_pce_yoy": 2.4, "core_pce_yoy_prior": 2.8, "cpi_yoy": 2.5, "cpi_yoy_prior": 3.0},
            "goldilocks",
        ),
        (
            {"unemployment": 3.8, "unemployment_low_12m": 3.8, "slope_10y_3m": 0.5},
            {"core_pce_yoy": 3.2, "core_pce_yoy_prior": 2.8, "breakeven_10y": 2.7},
            "reflation",
        ),
        (
            {"unemployment": 4.6, "unemployment_low_12m": 4.0, "slope_10y_3m": -0.4},
            {"core_pce_yoy": 3.4, "core_pce_yoy_prior": 3.0, "cpi_yoy": 3.6, "cpi_yoy_prior": 3.1},
            "stagflation",
        ),
        (
            {"unemployment": 4.6, "unemployment_low_12m": 4.0, "slope_10y_3m": 0.2},
            {"core_pce_yoy": 2.0, "core_pce_yoy_prior": 2.6, "breakeven_10y": 1.8},
            "deflationary_slowdown",
        ),
    ],
)
def test_quadrants(growth_kw: dict[str, float], infl_kw: dict[str, float], quadrant: str) -> None:
    s = regime_snapshot({**BASE, **growth_kw, **infl_kw}, as_of=dt.date(2026, 10, 2))
    assert s.quadrant == quadrant
    assert s.as_of == dt.date(2026, 10, 2)


def test_growth_votes() -> None:
    s = regime_snapshot({"unemployment": 4.2, "unemployment_low_12m": 4.0, "slope_10y_3m": 0.5})
    assert s.growth_score == 0 and s.growth == "up"  # neutral gap, neutral slope
    s = regime_snapshot({"slope_10y_3m": -0.1})
    assert s.growth == "down" and s.growth_score == -1
    s = regime_snapshot({"unemployment": 4.5, "unemployment_low_12m": 4.0, "slope_10y_3m": 1.2})
    assert s.growth_score == 0 and s.growth == "up"


def test_inflation_tie_uses_level() -> None:
    hot = regime_snapshot({"core_pce_yoy": 3.0, "core_pce_yoy_prior": 3.0, "breakeven_10y": 2.2})
    assert hot.inflation_score == 0 and hot.inflation == "up"
    cool = regime_snapshot({"cpi_yoy": 2.2})
    assert cool.inflation == "down"
    none = regime_snapshot({})
    assert none.inflation == "down" and none.growth == "up"
    assert "core_pce_yoy" in none.missing and none.citations == []


def test_real_rate_curve_and_stress() -> None:
    s = snap(gpr=180.0, epu=90.0)
    assert s.real_rate == pytest.approx(2.0)
    assert s.real_rate_level == "restrictive"
    assert s.curve_2s10s == pytest.approx(0.4)
    assert s.gpr_elevated and not s.epu_elevated
    assert "ust_10y=4.3" in s.citations
    assert regime_snapshot({"ust_10y": 4.0}).real_rate is None
    assert regime_snapshot({"ust_10y": 4.0}).curve_2s10s is None


@pytest.mark.parametrize(
    ("rr", "level"),
    [(None, "unknown"), (-0.5, "negative"), (0.5, "low"), (1.5, "neutral"), (2.5, "restrictive")],
)
def test_real_rate_levels(rr: float | None, level: str) -> None:
    assert real_rate_level(rr) == level


def test_fred_aliases_series_and_nan() -> None:
    series = pd.Series(
        {"DGS10": 4.5, "T10YIE": 2.4, "DGS2": float("nan"), "junk": 1.0, "UNRATE": 4.1}
    )
    s = regime_snapshot(series)
    assert s.inputs.ust_10y == 4.5
    assert s.inputs.ust_2y is None
    assert s.inputs.unemployment == 4.1
    assert s.real_rate == pytest.approx(2.1)
    inputs = MacroInputs(ust_10y=1.0, breakeven_10y=2.0)
    assert regime_snapshot(inputs).real_rate_level == "negative"
    assert regime_snapshot({"ust_10y": None}).inputs.ust_10y is None
