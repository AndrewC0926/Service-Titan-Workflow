"""Wash-sale edge cases for the lot ledger and the cross-account guard."""

from __future__ import annotations

import datetime as dt

import pytest

from committee.domain import AccountKind, Lot
from committee.engines.tax import (
    DISCLAIMER,
    Equivalence,
    LedgerError,
    LotLedger,
    LotPick,
    RealizedLot,
    SaleRequest,
    TaxLot,
    WashSaleGuard,
    check_purchase,
    wash_sale_block_list,
)
from committee.engines.tax.holding import is_long_term

D = dt.date


def buy(
    led: LotLedger,
    lot_id: str,
    sym: str,
    qty: float,
    px: float,
    on: dt.date,
    account: AccountKind = "taxable",
) -> list[TaxLot]:
    return led.add_purchase(
        Lot(lot_id=lot_id, account=account, symbol=sym, qty=qty, cost_per_share=px, acquired_on=on)
    )


def sell(
    led: LotLedger,
    sale_id: str,
    sym: str,
    on: dt.date,
    px: float,
    picks: list[tuple[str, float]],
    account: AccountKind = "taxable",
) -> list[RealizedLot]:
    return led.apply_sale(
        SaleRequest(
            sale_id=sale_id,
            account=account,
            symbol=sym,
            sold_on=on,
            price=px,
            picks=tuple(LotPick(lot_id=lid, qty=q) for lid, q in picks),
        )
    )


@pytest.fixture
def led() -> LotLedger:
    return LotLedger()


# ------------------------------------------------------------- basic matching
def test_loss_without_repurchase_is_allowed(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    assert r.economic_gain == pytest.approx(-1000)
    assert r.disallowed_loss == 0 and r.wash_code == ""
    assert r.reportable_gain == pytest.approx(-1000)
    assert led.open_lots() == []


def test_forward_repurchase_disallows_and_adjusts_basis_and_tacks(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    (new,) = buy(led, "A2", "XYZ", 100, 42, D(2026, 3, 20))
    r = led.realized()[0]
    assert r.wash_code == "W"
    assert r.disallowed_loss == pytest.approx(1000)
    assert r.reportable_gain == pytest.approx(0)
    assert new.lot_id == "A2"
    assert new.cost_per_share == pytest.approx(52)
    assert new.wash_adjustment_per_share == pytest.approx(10)
    assert new.acquired_on == D(2026, 3, 20)
    # held 59 days (Jan 2 -> Mar 2) tacked on: Mar 20 - 59 days
    assert new.holding_start == D(2026, 1, 20)
    assert new.wash_replacement


def test_gain_sale_never_washes(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 60, [("A1", 100)])
    (new,) = buy(led, "A2", "XYZ", 100, 61, D(2026, 3, 3))
    assert led.realized()[0].matches == ()
    assert new.cost_per_share == 61 and not new.wash_replacement


def test_different_symbol_does_not_wash(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    buy(led, "B1", "ABC", 100, 40, D(2026, 3, 3))
    assert led.realized()[0].matches == ()


# ---------------------------------------------------------------- partial lots
def test_partial_repurchase_only_replacement_quantity_triggers(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    (new,) = buy(led, "A2", "XYZ", 40, 41, D(2026, 3, 10))
    r = led.realized()[0]
    assert r.disallowed_qty == pytest.approx(40)
    assert r.disallowed_loss == pytest.approx(400)
    assert r.reportable_gain == pytest.approx(-600)
    assert r.unmatched_loss_qty == pytest.approx(60)
    assert new.qty == 40 and new.cost_per_share == pytest.approx(51)


def test_partial_sale_of_lot_splits_and_same_lot_is_not_replacement(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 3, 1))
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 10), 40, [("A1", 30)])
    assert r.qty == 30 and r.basis == pytest.approx(1500) and r.proceeds == pytest.approx(1200)
    assert r.matches == ()  # the unsold 70 shares came from the same purchase
    (rest,) = led.open_lots()
    assert rest.lot_id == "A1" and rest.qty == pytest.approx(70)
    assert rest.cost_per_share == 50 and not rest.wash_replacement


def test_replacement_lot_larger_than_loss_is_split(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 50, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 50)])
    lots = buy(led, "A2", "XYZ", 120, 41, D(2026, 3, 5))
    by_id = {lot.lot_id: lot for lot in lots}
    assert set(by_id) == {"A2/W1", "A2"}
    assert by_id["A2/W1"].qty == 50 and by_id["A2/W1"].cost_per_share == pytest.approx(51)
    assert by_id["A2"].qty == 70 and by_id["A2"].cost_per_share == 41
    assert by_id["A2"].holding_start == D(2026, 3, 5)
    assert not by_id["A2"].wash_replacement


def test_multiple_replacement_lots_matched_in_acquisition_order(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    buy(led, "A2", "XYZ", 30, 41, D(2026, 3, 5))
    buy(led, "A3", "XYZ", 30, 42, D(2026, 3, 6), account="ira")
    lots = buy(led, "A4", "XYZ", 100, 43, D(2026, 3, 7))
    r = led.realized()[0]
    assert [(m.replacement_lot_id, m.qty) for m in r.matches] == [
        ("A2", 30),
        ("A3", 30),
        ("A4/W1", 40),
    ]
    assert r.disallowed_loss == pytest.approx(1000)
    assert [m.permanent for m in r.matches] == [False, True, False]
    by_id = {lot.lot_id: lot for lot in lots}
    assert by_id["A4/W1"].cost_per_share == pytest.approx(53)
    assert by_id["A4"].qty == 60 and by_id["A4"].cost_per_share == 43
    # a further purchase finds nothing left to absorb
    (later,) = buy(led, "A5", "XYZ", 10, 44, D(2026, 3, 8))
    assert not later.wash_replacement


def test_two_loss_sales_matched_earliest_first(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 10, 60, D(2026, 1, 3))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10)])  # $10/sh loss
    sell(led, "S2", "XYZ", D(2026, 3, 3), 40, [("A2", 10)])  # $20/sh loss
    lots = buy(led, "A3", "XYZ", 15, 40, D(2026, 3, 4))
    s1, s2 = led.realized()
    assert s1.disallowed_qty == 10 and s1.disallowed_loss == pytest.approx(100)
    assert s2.disallowed_qty == 5 and s2.disallowed_loss == pytest.approx(100)
    costs = sorted(lot.cost_per_share for lot in lots)
    assert costs == pytest.approx([50, 60])


# ---------------------------------------------------------------- IRA / 401(k)
@pytest.mark.parametrize("account", ["ira", "k401"])
def test_repurchase_in_tax_deferred_account_permanently_disallows(
    led: LotLedger, account: AccountKind
) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
    (ira_lot,) = buy(led, "I1", "XYZ", 100, 41, D(2026, 3, 12), account=account)
    r = led.realized()[0]
    assert r.disallowed_loss == pytest.approx(1000) and r.wash_code == "W"
    (m,) = r.matches
    assert m.permanent and m.replacement_account == account
    # no basis adjustment and no holding-period tacking in the IRA
    assert ira_lot.cost_per_share == 41
    assert ira_lot.holding_start == D(2026, 3, 12)
    assert ira_lot.wash_replacement


def test_loss_inside_ira_is_not_tracked(led: LotLedger) -> None:
    buy(led, "I1", "XYZ", 100, 50, D(2026, 1, 2), account="ira")
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("I1", 100)], account="ira")
    buy(led, "A1", "XYZ", 100, 41, D(2026, 3, 3))
    assert led.realized()[0].matches == ()
    assert led.realized(taxable_only=True) == []
    assert WashSaleGuard(led).block_list(D(2026, 3, 3)) == set()


# ------------------------------------------------------------ window boundaries
def test_purchase_30_days_before_loss_sale_is_matched_retroactively(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 50, 45, D(2026, 2, 1))
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 3), 40, [("A1", 100)])  # Feb 1 + 30 days
    assert r.disallowed_qty == 50 and r.disallowed_loss == pytest.approx(500)
    (a2,) = led.open_lots()
    assert a2.lot_id == "A2" and a2.cost_per_share == pytest.approx(55)
    # A1 held 60 days (Jan 2 -> Mar 3); A2 start moves back 60 days from Feb 1
    assert a2.holding_start == D(2025, 12, 3)


def test_retroactive_partial_split_of_existing_lot(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 20, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 50, 45, D(2026, 2, 20))
    sell(led, "S1", "XYZ", D(2026, 3, 3), 40, [("A1", 20)])
    by_id = {lot.lot_id: lot for lot in led.open_lots()}
    assert by_id["A2/W1"].qty == 20 and by_id["A2/W1"].cost_per_share == pytest.approx(55)
    assert by_id["A2"].qty == 30 and by_id["A2"].cost_per_share == 45


def test_purchase_31_days_before_is_clear(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 50, 45, D(2026, 1, 31))
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 3), 40, [("A1", 100)])
    assert r.matches == ()


def test_day_30_after_washes_day_31_is_clear() -> None:
    for days, washed in ((30, True), (31, False)):
        led = LotLedger()
        buy(led, "A1", "XYZ", 100, 50, D(2026, 1, 2))
        sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 100)])
        buy(led, "A2", "XYZ", 100, 41, D(2026, 3, 2) + dt.timedelta(days=days))
        assert bool(led.realized()[0].matches) is washed


def test_same_day_purchase_before_sale_counts(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 10, 40, D(2026, 3, 2))
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10)])
    assert r.disallowed_qty == 10


def test_sold_replacement_candidates_are_not_matched(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    buy(led, "A2", "XYZ", 10, 30, D(2026, 2, 20))
    sell(led, "S0", "XYZ", D(2026, 2, 25), 45, [("A2", 10)])  # gain; A2 gone
    (r,) = sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10)])
    assert r.matches == ()


def test_lots_sold_together_are_not_each_others_replacement(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 2, 1))
    buy(led, "A2", "XYZ", 10, 52, D(2026, 2, 10))
    rs = sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10), ("A2", 10)])
    assert all(r.matches == () for r in rs)


# --------------------------------------------------------- holding-period tack
def test_tacked_holding_period_can_make_replacement_long_term(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2025, 1, 2))
    sell(led, "S1", "XYZ", D(2025, 12, 1), 40, [("A1", 10)])  # held 333 days
    (a2,) = buy(led, "A2", "XYZ", 10, 41, D(2025, 12, 10))
    assert a2.holding_start == D(2025, 1, 11)
    (r,) = sell(led, "S2", "XYZ", D(2026, 1, 15), 60, [("A2", 10)])
    assert r.term == "long"
    assert not is_long_term(r.acquired_on, r.sold_on)  # actual hold was 36 days
    assert r.basis == pytest.approx(510)


def test_chained_wash_sales_carry_basis_forward(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10)])  # -100, washed
    buy(led, "A2", "XYZ", 10, 40, D(2026, 3, 5))  # basis 50
    sell(led, "S2", "XYZ", D(2026, 3, 20), 35, [("A2", 10)])  # -150 vs adjusted basis
    (a3,) = buy(led, "A3", "XYZ", 10, 35, D(2026, 3, 25))
    s2 = led.realized()[1]
    assert s2.economic_gain == pytest.approx(-150)
    assert s2.disallowed_loss == pytest.approx(150)
    assert a3.cost_per_share == pytest.approx(50)


# ---------------------------------------------------- substantially identical
def test_equivalence_group_share_classes_wash(led: LotLedger) -> None:
    buy(led, "G1", "GOOGL", 10, 200, D(2026, 1, 2))
    sell(led, "S1", "GOOGL", D(2026, 3, 2), 150, [("G1", 10)])
    (g2,) = buy(led, "G2", "GOOG", 10, 151, D(2026, 3, 3))
    assert led.realized()[0].disallowed_loss == pytest.approx(500)
    assert g2.cost_per_share == pytest.approx(201)
    assert WashSaleGuard(led).block_list(D(2026, 3, 3)) == {"GOOG", "GOOGL"}


def test_custom_equivalence_groups() -> None:
    led = LotLedger(equivalence=Equivalence.from_lists([["AAA", "aaa.b"]]))
    buy(led, "A1", "AAA", 10, 20, D(2026, 1, 2))
    sell(led, "S1", "AAA", D(2026, 3, 2), 10, [("A1", 10)])
    buy(led, "B1", "AAA.B", 10, 10, D(2026, 3, 3))
    assert led.realized()[0].disallowed_qty == 10
    assert led.equivalence.key("aaa.b") == "AAA"


def test_equivalence_rejects_overlapping_groups() -> None:
    with pytest.raises(ValueError):
        Equivalence.from_lists([["A", "B"], ["B", "C"]])


def test_replacement_map_is_not_substantially_identical() -> None:
    eq = Equivalence()
    assert eq.check_replacements({"VTI": "ITOT", "IEFA": "VEA"}) == []
    assert eq.check_replacements({"GOOG": "GOOGL"}) == [
        "replacement GOOG->GOOGL is substantially identical"
    ]


# ------------------------------------------------ repeated harvests and guard
def test_repeated_harvest_a_b_a_within_window() -> None:
    led = LotLedger()
    guard = WashSaleGuard(led)
    buy(led, "V1", "VTI", 100, 100, D(2026, 1, 5))
    sell(led, "S1", "VTI", D(2026, 3, 2), 90, [("V1", 100)])
    buy(led, "I1", "ITOT", 100, 90, D(2026, 3, 2))
    # VTI -> ITOT replacement mapping is not a wash sale
    assert led.realized()[0].matches == ()
    assert guard.block_list(D(2026, 3, 2)) == {"VTI"}
    assert guard.check_purchase("ITOT", "taxable", D(2026, 3, 2)).verdict == "ALLOW"
    # Harvest ITOT back into VTI 18 days later: VTI is still blocked
    sell(led, "S2", "ITOT", D(2026, 3, 20), 80, [("I1", 100)])
    v = guard.check_purchase("VTI", "taxable", D(2026, 3, 20))
    assert v.verdict == "BLOCK" and not v.allowed
    assert v.clear_on == D(2026, 4, 2)
    assert v.blocking_sale_ids == ("S1",)
    assert DISCLAIMER in v.reason
    assert guard.block_list(D(2026, 3, 20)) == {"VTI", "ITOT"}
    # If it happens anyway, the VTI loss from S1 is disallowed into the new lot.
    (v2,) = buy(led, "V2", "VTI", 100, 80, D(2026, 3, 20))
    s1, s2 = led.realized()
    assert s1.disallowed_loss == pytest.approx(1000)
    assert s2.matches == ()
    assert v2.cost_per_share == pytest.approx(90)


def test_block_list_window_and_clear_date() -> None:
    led = LotLedger()
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    sell(led, "S1", "XYZ", D(2026, 3, 2), 40, [("A1", 10)])
    assert wash_sale_block_list(led, D(2026, 4, 1)) == {"XYZ"}
    assert wash_sale_block_list(led, D(2026, 4, 2)) == set()
    # a purchase dated within 30 days BEFORE the loss sale is also flagged
    assert wash_sale_block_list(led, D(2026, 2, 1)) == {"XYZ"}
    assert check_purchase(led, "XYZ", "ira", D(2026, 3, 15)).verdict == "BLOCK"
    assert "permanently disallowed" in check_purchase(led, "XYZ", "ira", D(2026, 3, 15)).reason
    assert check_purchase(led, "xyz", "taxable", D(2026, 4, 2)).verdict == "ALLOW"


def test_check_purchase_warns_when_taxable_lots_are_underwater() -> None:
    led = LotLedger()
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    g = WashSaleGuard(led)
    v = g.check_purchase("XYZ", "ira", D(2026, 3, 2), prices={"XYZ": 40})
    assert v.verdict == "WARN" and v.allowed and "A1" in v.reason
    assert g.check_purchase("XYZ", "ira", D(2026, 3, 2), prices={"XYZ": 60}).verdict == "ALLOW"


def test_sale_wash_risk_reports_recent_purchases_in_any_account() -> None:
    led = LotLedger()
    buy(led, "A1", "XYZ", 10, 50, D(2026, 1, 2))
    buy(led, "I1", "XYZ", 1, 45, D(2026, 2, 20), account="ira")
    g = WashSaleGuard(led)
    assert [p.lot_id for p in g.sale_wash_risk("XYZ", D(2026, 3, 2))] == ["I1"]
    assert g.sale_wash_risk("XYZ", D(2026, 3, 23)) == []
    assert g.sale_wash_risk("XYZ", D(2026, 2, 20), exclude_origins={"I1"}) == []


# -------------------------------------------------------------- validation
def test_ledger_rejects_bad_events(led: LotLedger) -> None:
    buy(led, "A1", "XYZ", 10, 50, D(2026, 3, 1))
    with pytest.raises(LedgerError, match="duplicate lot"):
        buy(led, "A1", "XYZ", 10, 50, D(2026, 3, 2))
    with pytest.raises(LedgerError, match="before the last event"):
        buy(led, "A0", "XYZ", 10, 50, D(2026, 2, 1))
    with pytest.raises(LedgerError, match="no open lot"):
        sell(led, "S1", "XYZ", D(2026, 3, 5), 40, [("NOPE", 1)])
    with pytest.raises(LedgerError, match="holds"):
        sell(led, "S1", "XYZ", D(2026, 3, 5), 40, [("A1", 11)])
    with pytest.raises(LedgerError, match="not ABC"):
        sell(led, "S1", "ABC", D(2026, 3, 5), 40, [("A1", 1)])
    with pytest.raises(LedgerError, match="twice"):
        sell(led, "S1", "XYZ", D(2026, 3, 5), 40, [("A1", 1), ("A1", 1)])
    sell(led, "S1", "XYZ", D(2026, 3, 5), 40, [("A1", 1)])
    with pytest.raises(LedgerError, match="duplicate sale"):
        sell(led, "S1", "XYZ", D(2026, 3, 6), 40, [("A1", 1)])
    with pytest.raises(ValueError):
        LotLedger(window_days=10)
    assert led.last_date == D(2026, 3, 5)
    assert len(led.purchases) == 1
    assert led.lot("A1").qty == pytest.approx(9)
    assert led.realized(year=2025) == []
