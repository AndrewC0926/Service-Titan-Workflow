"""After-tax hurdle math."""

from __future__ import annotations

import datetime as dt

import pytest

from committee.engines.tax import DISCLAIMER, TaxRates, after_tax_hurdle

D = dt.date
RATES = TaxRates(ordinary=0.24, ltcg=0.15)


def test_short_term_gain_no_deferral_and_with_deferral() -> None:
    h = after_tax_hurdle(10_000, 6_000, D(2026, 1, 1), D(2026, 4, 11), RATES)
    assert h.term == "short"
    assert h.tax_due == pytest.approx(960)
    assert h.net_proceeds == pytest.approx(9040)
    assert h.hurdle_total_no_deferral == pytest.approx(10_000 / 9040 - 1)
    # held lot becomes long-term by the horizon end; new lot is short-term at 1 year
    hold_end = 10_000 - 0.15 * 4000
    g_new = (hold_end - 0.24 * 9040) / (9040 * 0.76)
    assert h.hold_after_tax_at_horizon == pytest.approx(hold_end)
    assert h.required_annual_return == pytest.approx(g_new - 1)
    assert h.hurdle_annual == pytest.approx(g_new - 1)
    assert DISCLAIMER in h.explanation


def test_long_term_gain_multi_year_with_expected_return() -> None:
    r = 0.07
    n = 5.0
    h = after_tax_hurdle(
        10_000, 4_000, D(2020, 1, 1), D(2026, 1, 1), RATES, horizon_years=n, hold_expected_return=r
    )
    assert h.term == "long"
    tax = 0.15 * 6000
    net = 10_000 - tax
    g = (1 + r) ** n
    hold_end = 10_000 * g - 0.15 * (10_000 * g - 4000)
    g_new = (hold_end - 0.15 * net) / (net * 0.85)
    assert h.required_annual_return == pytest.approx(g_new ** (1 / n) - 1)
    assert h.hurdle_annual == pytest.approx(h.required_annual_return - r)
    assert 0 < h.hurdle_annual < h.hurdle_total_no_deferral


def test_no_tax_means_no_hurdle() -> None:
    h = after_tax_hurdle(
        10_000,
        10_000,
        D(2020, 1, 1),
        D(2026, 1, 1),
        RATES,
        horizon_years=3,
        hold_expected_return=0.05,
    )
    assert h.tax_due == 0
    assert h.hurdle_total_no_deferral == 0
    assert h.hurdle_annual == pytest.approx(0, abs=1e-12)


def test_loss_gives_negative_hurdle() -> None:
    h = after_tax_hurdle(8_000, 10_000, D(2026, 1, 1), D(2026, 4, 1), RATES)
    assert h.tax_due == pytest.approx(-480)
    assert h.hurdle_total_no_deferral < 0
    assert h.hurdle_annual < 0


def test_invalid_inputs() -> None:
    with pytest.raises(ValueError):
        after_tax_hurdle(0, 1, D(2026, 1, 1), D(2026, 2, 1), RATES)
    with pytest.raises(ValueError):
        after_tax_hurdle(100, 1, D(2026, 1, 1), D(2026, 2, 1), RATES, horizon_years=0)
    with pytest.raises(ValueError):
        after_tax_hurdle(100, 0, D(2026, 1, 1), D(2026, 2, 1), TaxRates(1.0, 1.0))
