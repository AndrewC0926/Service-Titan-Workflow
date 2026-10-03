"""Monthly harvest scan: thresholds, replacement mapping, wash-sale skips, journaling."""

from __future__ import annotations

import datetime as dt
import logging
from pathlib import Path

import pytest

from committee.config.loader import load_config
from committee.config.schema import TaxConfig
from committee.domain import AccountKind, Lot
from committee.engines.tax import DISCLAIMER, LotLedger, LotPick, SaleRequest, harvest_scan
from committee.engines.tax.harvest import journal_harvest_scan, qualifies
from committee.journal.store import Journal

D = dt.date
ASOF = D(2026, 10, 1)
ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture(scope="module")
def cfg() -> TaxConfig:
    return load_config(ROOT / "config").tax


def buy(
    led: LotLedger,
    lot_id: str,
    sym: str,
    qty: float,
    px: float,
    on: dt.date,
    account: AccountKind = "taxable",
) -> None:
    led.add_purchase(
        Lot(lot_id=lot_id, account=account, symbol=sym, qty=qty, cost_per_share=px, acquired_on=on)
    )


def sell(
    led: LotLedger, sale_id: str, sym: str, on: dt.date, px: float, lot_id: str, q: float
) -> None:
    led.apply_sale(
        SaleRequest(
            sale_id=sale_id,
            account="taxable",
            symbol=sym,
            sold_on=on,
            price=px,
            picks=(LotPick(lot_id=lot_id, qty=q),),
        )
    )


def test_thresholds_are_strict_and_both_required(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "OK", "VTI", 100, 200, D(2026, 1, 5))  # loss 1500 = 7.5%
    buy(led, "SMALL_USD", "VEA", 90, 60, D(2026, 1, 5))  # loss 900 (15%)
    buy(led, "SMALL_PCT", "IEMG", 1000, 30, D(2026, 1, 5))  # loss 1200 (4%)
    buy(led, "EXACT", "AVUV", 100, 100, D(2026, 1, 5))  # loss exactly 1000 (10%)
    buy(led, "GAIN", "DFSV", 100, 10, D(2026, 1, 5))
    buy(led, "IRA", "IEFA", 100, 100, D(2026, 1, 5), account="ira")  # IRA never harvested
    prices = {"VTI": 185, "VEA": 50, "IEMG": 28.8, "AVUV": 90, "DFSV": 20, "IEFA": 50}
    scan = harvest_scan(led, prices, ASOF, cfg)
    assert [p.symbol for p in scan.proposals] == ["VTI"]
    assert scan.skipped == ()
    (p,) = scan.proposals
    assert p.replacement == "ITOT"
    assert p.total_loss == pytest.approx(1500)
    assert p.lots[0].loss_pct_of_basis == pytest.approx(7.5)
    assert p.lots[0].term == "short"
    assert p.estimated_tax_saving == pytest.approx(1500 * (0.24 + 0.05))
    assert p.replacement_blocked_for_symbol_until == D(2026, 11, 1)
    assert DISCLAIMER in p.reason and "ANY account" in p.reason
    assert scan.label == DISCLAIMER


def test_qualifies_rejects_zero_basis_and_ira() -> None:
    from committee.engines.tax.models import TaxLot

    free = TaxLot("F", "taxable", "X", 10, 0.0, ASOF, ASOF, "F")
    assert not qualifies(free, 0.0, 1000, 5)
    ira = TaxLot("I", "ira", "X", 100, 100.0, ASOF, ASOF, "I")
    assert not qualifies(ira, 50.0, 1000, 5)


def test_lots_grouped_by_symbol(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "V1", "VTI", 100, 200, D(2025, 6, 2))
    buy(led, "V2", "VTI", 100, 210, D(2026, 1, 5))
    buy(led, "V3", "VTI", 100, 150, D(2026, 2, 5))  # a gain lot stays put
    scan = harvest_scan(led, {"VTI": 180}, ASOF, cfg)
    (p,) = scan.proposals
    assert [h.lot_id for h in p.lots] == ["V1", "V2"]
    assert [h.term for h in p.lots] == ["long", "short"]
    assert p.qty == 200 and p.total_loss == pytest.approx(5000)
    assert p.estimated_tax_saving == pytest.approx(2000 * 0.20 + 3000 * 0.29)


def test_missing_replacement_mapping_is_skipped(
    cfg: TaxConfig, caplog: pytest.LogCaptureFixture
) -> None:
    led = LotLedger()
    buy(led, "N1", "NVDA", 100, 200, D(2026, 1, 5))
    with caplog.at_level(logging.INFO, logger="committee.engines.tax.harvest"):
        scan = harvest_scan(led, {"NVDA": 150}, ASOF, cfg)
    assert scan.proposals == ()
    (s,) = scan.skipped
    assert s.replacement is None and "no replacement mapped" in s.reason
    assert DISCLAIMER in s.reason
    assert "NVDA -> None" in caplog.text


def test_recent_purchase_in_any_account_skips(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "E1", "VEA", 400, 60, D(2026, 2, 2))
    buy(led, "E2", "VEA", 5, 51, D(2026, 9, 20), account="ira")  # 11 days ago
    scan = harvest_scan(led, {"VEA": 50}, ASOF, cfg)
    (s,) = scan.skipped
    assert "wash sale" in s.reason and "E2 (ira, 2026-09-20)" in s.reason
    # 31 days later the IRA buy is outside the window
    scan2 = harvest_scan(led, {"VEA": 50}, D(2026, 10, 21), cfg)
    assert [p.symbol for p in scan2.proposals] == ["VEA"]


def test_harvesting_a_recent_lot_itself_is_fine(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "E1", "VEA", 400, 60, D(2026, 9, 20))
    scan = harvest_scan(led, {"VEA": 50}, ASOF, cfg)
    assert [p.symbol for p in scan.proposals] == ["VEA"]


def test_repeated_harvest_back_into_blocked_replacement_is_skipped(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "V1", "VTI", 100, 300, D(2026, 1, 5))
    sell(led, "S1", "VTI", D(2026, 9, 10), 270, "V1", 100)
    buy(led, "I1", "ITOT", 500, 60, D(2026, 9, 10))
    # ITOT drops; harvesting ITOT -> VTI would buy VTI inside the VTI window
    scan = harvest_scan(led, {"ITOT": 55}, D(2026, 9, 25), cfg)
    assert scan.proposals == ()
    (s,) = scan.skipped
    assert s.replacement == "VTI" and "block list" in s.reason
    # day 31 after the VTI loss sale the pair is proposed (I1 is the lot being sold)
    assert harvest_scan(led, {"ITOT": 55}, D(2026, 10, 11), cfg).proposals != ()


def test_identical_replacement_is_a_config_error(cfg: TaxConfig) -> None:
    bad = cfg.model_copy(update={"replacements": {"GOOGL": "GOOG"}})
    led = LotLedger()
    buy(led, "G1", "GOOGL", 100, 200, D(2026, 1, 5))
    (s,) = harvest_scan(led, {"GOOGL": 150}, ASOF, bad).skipped
    assert "substantially identical" in s.reason


def test_unpriced_lots_are_ignored(cfg: TaxConfig) -> None:
    led = LotLedger()
    buy(led, "V1", "VTI", 100, 300, D(2026, 1, 5))
    scan = harvest_scan(led, {}, ASOF, cfg)
    assert scan.proposals == () and scan.skipped == ()


def test_journal_every_pair(cfg: TaxConfig, tmp_path: Path) -> None:
    led = LotLedger()
    buy(led, "V1", "VTI", 100, 300, D(2026, 1, 5))
    buy(led, "N1", "NVDA", 100, 200, D(2026, 1, 5))
    scan = harvest_scan(led, {"VTI": 250, "NVDA": 150}, ASOF, cfg)
    with Journal(tmp_path / "j.sqlite") as j:
        entries = journal_harvest_scan(j, scan)
        assert [e.entry_type for e in entries] == ["harvest_proposal"] * 2
        statuses = {e.payload["symbol"]: e.payload["status"] for e in entries}
        assert statuses == {"VTI": "proposed", "NVDA": "skipped"}
        assert all(e.payload["label"] == DISCLAIMER for e in entries)
        assert entries[0].payload["replacement"] == "ITOT"
        assert entries[0].payload["asof"] == "2026-10-01"
        assert len(journal_harvest_scan(j, scan, include_skipped=False)) == 1
        assert j.verify().ok
