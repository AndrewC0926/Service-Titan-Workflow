"""Lot selection: tax-minimizing ordering, harvest mode, tax-deferred FIFO."""

from __future__ import annotations

import datetime as dt

import pytest

from committee.domain import AccountKind
from committee.engines.tax import DISCLAIMER, LotLedger, TaxLot, TaxRates, order_lots, select_lots
from committee.engines.tax.models import SaleRequest

D = dt.date
ON = D(2026, 10, 1)
RATES = TaxRates(ordinary=0.24, ltcg=0.15)
LT = D(2025, 1, 2)  # long-term on ON
ST = D(2026, 6, 1)  # short-term on ON


def lot(
    lot_id: str, cost: float, acquired: dt.date, qty: float = 10, account: AccountKind = "taxable"
) -> TaxLot:
    return TaxLot(lot_id, account, "XYZ", qty, cost, acquired, acquired, lot_id)


def ids(lots: list[TaxLot]) -> list[str]:
    return [x.lot_id for x in lots]


def test_order_minimizes_tax_per_share() -> None:
    lots = [
        lot("LT80", 80, LT),  # gain 20 * 0.15 = 3.00
        lot("LT95", 95, LT),  # gain 5 * 0.15 = 0.75
        lot("ST98", 98, ST),  # gain 2 * 0.24 = 0.48
        lot("ST110", 110, ST),  # loss -10 * 0.24 = -2.40
        lot("LT110", 110, LT),  # loss -10 * 0.15 = -1.50
    ]
    assert ids(order_lots(lots, 100, ON, RATES)) == ["ST110", "LT110", "ST98", "LT95", "LT80"]


def test_tie_prefers_long_term_then_highest_basis() -> None:
    lots = [
        lot("ST97.5", 97.5, ST),  # 2.5 * 0.24 = 0.60
        lot("LT96", 96, LT),  # 4 * 0.15 = 0.60
    ]
    assert ids(order_lots(lots, 100, ON, RATES)) == ["LT96", "ST97.5"]
    same = [lot("A", 90, LT), lot("B", 90, D(2024, 1, 2))]
    assert ids(order_lots(same, 100, ON, RATES)) == ["B", "A"]  # then oldest


def test_equal_term_gains_pick_highest_basis_first() -> None:
    lots = [lot("LT50", 50, LT), lot("LT90", 90, LT), lot("LT70", 70, LT)]
    assert ids(order_lots(lots, 100, ON, RATES)) == ["LT90", "LT70", "LT50"]


def test_harvest_mode_takes_largest_losses_first() -> None:
    lots = [
        lot("ST105", 105, ST),  # loss 5/sh
        lot("LT130", 130, LT),  # loss 30/sh
        lot("LT95", 95, LT),
        lot("ST98", 98, ST),
    ]
    assert ids(order_lots(lots, 100, ON, RATES, harvest=True)) == [
        "LT130",
        "ST105",
        "ST98",
        "LT95",
    ]


def test_select_lots_splits_last_lot_and_estimates_tax() -> None:
    lots = [lot("LT80", 80, LT), lot("ST98", 98, ST), lot("ST110", 110, ST)]
    sel = select_lots(lots, 25, 100, ON, RATES)
    assert [(c.lot_id, c.qty) for c in sel.chosen] == [("ST110", 10), ("ST98", 10), ("LT80", 5)]
    # -100*0.24 + 20*0.24 + 100*0.15
    assert sel.estimated_tax == pytest.approx(-24 + 4.8 + 15)
    assert sel.realized_gain == pytest.approx(-100 + 20 + 100)
    assert sel.shortfall == 0
    assert DISCLAIMER in sel.explanation
    assert [p.lot_id for p in sel.picks] == ["ST110", "ST98", "LT80"]


def test_select_lots_reports_shortfall() -> None:
    sel = select_lots([lot("A", 90, LT)], 15, 100, ON, RATES)
    assert sel.shortfall == pytest.approx(5)


def test_tax_deferred_is_fifo_and_tax_free() -> None:
    lots = [lot("B", 50, D(2025, 6, 1), account="ira"), lot("A", 150, D(2025, 1, 1), account="ira")]
    sel = select_lots(lots, 15, 100, ON, RATES)
    assert [(c.lot_id, c.qty) for c in sel.chosen] == [("A", 10), ("B", 5)]
    assert sel.estimated_tax == 0


def test_select_lots_validates_input() -> None:
    with pytest.raises(ValueError):
        select_lots([lot("A", 90, LT)], 0, 100, ON, RATES)
    mixed = [lot("A", 90, LT), lot("B", 90, LT, account="ira")]
    with pytest.raises(ValueError):
        select_lots(mixed, 1, 100, ON, RATES)


def test_selection_picks_feed_the_ledger() -> None:
    from committee.domain import Lot

    led = LotLedger()
    led.add_purchase(
        Lot(
            lot_id="A",
            account="taxable",
            symbol="XYZ",
            qty=10,
            cost_per_share=80,
            acquired_on=D(2025, 1, 2),
        )
    )
    led.add_purchase(
        Lot(
            lot_id="B",
            account="taxable",
            symbol="XYZ",
            qty=10,
            cost_per_share=99,
            acquired_on=D(2025, 2, 2),
        )
    )
    sel = select_lots(led.open_lots(account="taxable", symbol="XYZ"), 12, 100, ON, RATES)
    realized = led.apply_sale(
        SaleRequest(
            sale_id="S", account="taxable", symbol="XYZ", sold_on=ON, price=100, picks=sel.picks
        )
    )
    assert sum(r.economic_gain for r in realized) == pytest.approx(sel.realized_gain)
    assert led.lot("A").qty == pytest.approx(8)
