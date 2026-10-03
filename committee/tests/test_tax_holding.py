"""Holding-period clock, the 60-day long-term warning and the flat-rate model."""

from __future__ import annotations

import datetime as dt

import pytest

from committee.domain import AccountKind
from committee.engines.tax import (
    DISCLAIMER,
    TaxLot,
    TaxRates,
    anniversary,
    days_until_long_term,
    first_long_term_date,
    holding_advice,
    is_long_term,
    long_term_warnings,
    term_of,
)
from committee.engines.tax.holding import tacked_holding_start
from committee.engines.tax.labels import labeled

D = dt.date
RATES = TaxRates(ordinary=0.24, ltcg=0.15)


def lot(
    acquired: dt.date,
    cost: float = 100.0,
    qty: float = 10.0,
    account: AccountKind = "taxable",
    lot_id: str = "L1",
    symbol: str = "XYZ",
) -> TaxLot:
    return TaxLot(lot_id, account, symbol, qty, cost, acquired, acquired, lot_id)


def test_more_than_one_year_rule() -> None:
    a = D(2025, 3, 15)
    assert anniversary(a) == D(2026, 3, 15)
    assert not is_long_term(a, D(2026, 3, 15))
    assert is_long_term(a, D(2026, 3, 16))
    assert first_long_term_date(a) == D(2026, 3, 16)
    assert term_of(a, D(2026, 3, 15)) == "short"
    assert term_of(a, D(2026, 3, 16)) == "long"


def test_feb_29_acquisition() -> None:
    a = D(2024, 2, 29)
    assert anniversary(a) == D(2025, 2, 28)
    assert not is_long_term(a, D(2025, 2, 28))
    assert is_long_term(a, D(2025, 3, 1))


def test_leap_year_in_holding_period_uses_calendar_not_365_days() -> None:
    a = D(2023, 3, 1)  # the year that follows contains Feb 29, 2024
    assert (D(2024, 3, 1) - a).days == 366
    assert not is_long_term(a, D(2024, 3, 1))
    assert is_long_term(a, D(2024, 3, 2))


def test_days_until_long_term() -> None:
    a = D(2025, 12, 1)
    assert days_until_long_term(a, D(2026, 10, 15)) == 48
    assert days_until_long_term(a, D(2026, 12, 2)) == 0
    assert days_until_long_term(a, D(2027, 1, 1)) == 0


def test_tacked_holding_start() -> None:
    assert tacked_holding_start(D(2026, 3, 20), D(2026, 1, 2), D(2026, 3, 2)) == D(2026, 1, 20)


def test_rates() -> None:
    r = TaxRates(ordinary=0.24, ltcg=0.15, state=0.05, niit=0.038, apply_niit=True)
    assert r.rate("short") == pytest.approx(0.328)
    assert r.rate("long") == pytest.approx(0.238)
    assert r.tax_on(-1000, "short") == pytest.approx(-328)
    assert TaxRates(0.24, 0.15, niit=0.038).rate("long") == pytest.approx(0.15)


def test_rates_from_config(ctx) -> None:  # type: ignore[no-untyped-def]
    r = TaxRates.from_config(ctx.config.tax)
    assert r.rate("short") == pytest.approx(0.29)
    assert r.rate("long") == pytest.approx(0.20)


def test_labeled_is_idempotent() -> None:
    assert labeled("x") == f"x {DISCLAIMER}"
    assert labeled(labeled("x")) == labeled("x")


# ------------------------------------------------------------ 60-day warning
def test_gain_within_60_days_defaults_to_wait() -> None:
    adv = holding_advice(lot(D(2025, 12, 1)), 150.0, D(2026, 10, 15), RATES)
    assert adv is not None
    assert adv.action == "WAIT"
    assert adv.days_to_long_term == 48
    assert adv.long_term_on == D(2026, 12, 2)
    assert adv.unrealized_gain == pytest.approx(500)
    assert adv.tax_now == pytest.approx(120)
    assert adv.tax_if_wait == pytest.approx(75)
    assert adv.tax_saved_by_waiting == pytest.approx(45)
    assert DISCLAIMER in adv.reason


def test_thesis_broken_proceeds() -> None:
    adv = holding_advice(lot(D(2025, 12, 1)), 150.0, D(2026, 10, 15), RATES, thesis_broken=True)
    assert adv is not None and adv.action == "PROCEED"
    assert "thesis broken" in adv.reason


@pytest.mark.parametrize(
    ("acquired", "price", "account", "expect"),
    [
        (D(2025, 12, 1), 90.0, "taxable", False),  # at a loss
        (D(2025, 12, 1), 100.0, "taxable", False),  # flat
        (D(2025, 12, 1), 150.0, "ira", False),  # tax-deferred
        (D(2025, 6, 1), 150.0, "taxable", False),  # already long-term
        (D(2026, 1, 1), 150.0, "taxable", False),  # more than 60 days away
    ],
)
def test_no_warning_cases(
    acquired: dt.date, price: float, account: AccountKind, expect: bool
) -> None:
    adv = holding_advice(lot(acquired, account=account), price, D(2026, 10, 15), RATES)
    assert (adv is not None) is expect


def test_warning_boundary_60_vs_61_days() -> None:
    asof = D(2026, 10, 1)
    at_60 = asof + dt.timedelta(days=60)  # first long-term date
    acquired_60 = at_60 - dt.timedelta(days=1)
    acquired_60 = acquired_60.replace(year=acquired_60.year - 1)
    assert days_until_long_term(acquired_60, asof) == 60
    assert holding_advice(lot(acquired_60), 150.0, asof, RATES) is not None
    acquired_61 = acquired_60 + dt.timedelta(days=1)
    assert days_until_long_term(acquired_61, asof) == 61
    assert holding_advice(lot(acquired_61), 150.0, asof, RATES) is None


def test_long_term_warnings_batch() -> None:
    lots = [
        lot(D(2025, 12, 1), lot_id="A", symbol="AAA"),
        lot(D(2025, 11, 20), lot_id="B", symbol="BBB"),
        lot(D(2025, 11, 20), lot_id="C", symbol="NOPRICE"),
    ]
    out = long_term_warnings(
        lots, {"AAA": 150, "BBB": 150}, D(2026, 10, 15), RATES, thesis_broken=["AAA"]
    )
    assert [a.lot_id for a in out] == ["B", "A"]
    assert [a.action for a in out] == ["WAIT", "PROCEED"]
