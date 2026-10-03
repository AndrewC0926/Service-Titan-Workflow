"""Realized-gains CSV, the SQLite ledger store and the `tax` CLI."""

from __future__ import annotations

import csv
import datetime as dt
import io
import json
import sqlite3
from pathlib import Path

import pytest
from typer.testing import CliRunner

from committee.domain import Lot
from committee.engines.tax import (
    DISCLAIMER,
    LedgerError,
    LotLedger,
    LotPick,
    SaleRequest,
    TaxLedgerStore,
    realized_csv,
    summarize,
)
from committee.engines.tax.cli import app
from committee.engines.tax.report import COLUMNS
from committee.engines.tax.store import load_events_file
from committee.journal.store import Journal

D = dt.date
FIX = Path(__file__).parent / "fixtures" / "tax"
runner = CliRunner()


def _lot(
    lot_id: str, sym: str, qty: float, px: float, on: dt.date, account: str = "taxable"
) -> Lot:
    return Lot.model_validate(
        {
            "lot_id": lot_id,
            "account": account,
            "symbol": sym,
            "qty": qty,
            "cost_per_share": px,
            "acquired_on": on,
        }
    )


def _sale(
    sale_id: str, sym: str, on: dt.date, px: float, lot_id: str, q: float, account: str = "taxable"
) -> SaleRequest:
    return SaleRequest.model_validate(
        {
            "sale_id": sale_id,
            "account": account,
            "symbol": sym,
            "sold_on": on,
            "price": px,
            "picks": [LotPick(lot_id=lot_id, qty=q)],
        }
    )


def _parse(text: str) -> tuple[str, list[dict[str, str]]]:
    first, rest = text.split("\n", 1)
    return first, list(csv.DictReader(io.StringIO(rest)))


# ------------------------------------------------------------------- CSV
def test_realized_csv_rows_codes_and_header() -> None:
    led = LotLedger()
    led.add_purchase(_lot("A1", "XYZ", 100, 50, D(2025, 1, 2)))
    led.apply_sale(_sale("S1", "XYZ", D(2025, 12, 1), 40, "A1", 100))  # loss 1000
    led.add_purchase(_lot("A2", "XYZ", 40, 41, D(2025, 12, 10)))  # washes 40 shares
    led.add_purchase(_lot("B1", "ABC", 10, 10, D(2026, 1, 2)))
    led.add_purchase(_lot("I1", "QQQ", 10, 100, D(2026, 1, 2), account="ira"))
    led.apply_sale(_sale("S2", "XYZ", D(2026, 2, 1), 60, "A2", 40))  # tacked -> long
    led.apply_sale(_sale("S3", "ABC", D(2026, 3, 1), 15, "B1", 10))  # short gain
    led.apply_sale(_sale("S4", "QQQ", D(2026, 3, 1), 50, "I1", 10, account="ira"))

    header, rows = _parse(realized_csv(led.realized(), 2025))
    assert header.startswith("# ") and DISCLAIMER in header and "2025" in header
    (r,) = rows
    assert tuple(r) == COLUMNS
    assert r["adjustment_code"] == "W"
    assert r["disallowed_loss"] == "400.00"
    assert r["gain_loss"] == "-600.00"
    assert r["proceeds"] == "4000.00" and r["cost_basis"] == "5000.00"
    assert r["term"] == "short" and r["account"] == "taxable"
    assert r["date_acquired"] == "2025-01-02" == r["date_purchased"]

    _, rows26 = _parse(realized_csv(led.realized(), 2026))
    assert [x["symbol"] for x in rows26] == ["XYZ", "ABC"]  # IRA sale excluded
    xyz = rows26[0]
    assert xyz["term"] == "long"
    assert xyz["date_purchased"] == "2025-12-10"
    assert xyz["date_acquired"] == "2025-01-11"  # tacked holding period
    assert xyz["cost_basis"] == "2040.00" and xyz["adjustment_code"] == ""
    assert xyz["gain_loss"] == "360.00"

    s = summarize(led.realized(), 2026)
    assert s.long_term == pytest.approx(360) and s.short_term == pytest.approx(50)
    assert s.total == pytest.approx(410) and s.rows == 2 and s.disallowed == 0
    assert DISCLAIMER in s.text()
    assert summarize(led.realized(), 2025).disallowed == pytest.approx(400)


def test_empty_report_still_has_header_and_columns() -> None:
    header, rows = _parse(realized_csv([], 2026))
    assert DISCLAIMER in header and rows == []


# ----------------------------------------------------------------- store
def test_store_persists_and_replays(tmp_path: Path) -> None:
    db = tmp_path / "state.sqlite"
    with TaxLedgerStore(db) as st:
        st.record_purchase(_lot("A1", "XYZ", 100, 50, D(2026, 1, 2)))
        st.record_sale(_sale("S1", "XYZ", D(2026, 3, 2), 40, "A1", 100))
        led = st.record_purchase(_lot("A2", "XYZ", 100, 41, D(2026, 3, 10)))
        assert led.realized()[0].disallowed_loss == pytest.approx(1000)
    with TaxLedgerStore(db) as st:
        led = st.ledger()
        assert st.event_count() == 3
        assert led.lot("A2").cost_per_share == pytest.approx(51)
    tables = {r[0] for r in sqlite3.connect(db).execute("SELECT name FROM sqlite_master")}
    assert {t for t in tables if not t.startswith("sqlite_")} <= {"tax_events", "tax_events_order"}


def test_store_backdated_purchase_is_replayed_in_date_order(tmp_path: Path) -> None:
    with TaxLedgerStore(tmp_path / "s.sqlite") as st:
        st.record_purchase(_lot("A1", "XYZ", 100, 50, D(2026, 1, 2)))
        st.record_sale(_sale("S1", "XYZ", D(2026, 3, 2), 40, "A1", 100))
        # entered late, but dated 20 days BEFORE the loss sale
        led = st.record_purchase(_lot("A0", "XYZ", 10, 45, D(2026, 2, 10), account="ira"))
        (m,) = led.realized()[0].matches
        assert m.replacement_lot_id == "A0" and m.permanent


def test_store_rejects_bad_event_and_rolls_back(tmp_path: Path) -> None:
    with TaxLedgerStore(tmp_path / "s.sqlite") as st:
        st.record_purchase(_lot("A1", "XYZ", 10, 50, D(2026, 1, 2)))
        with pytest.raises(LedgerError):
            st.record_sale(_sale("S1", "XYZ", D(2026, 3, 2), 40, "A1", 11))
        with pytest.raises(sqlite3.IntegrityError):
            st.record_purchase(_lot("A1", "XYZ", 10, 50, D(2026, 1, 3)))
        with pytest.raises(ValueError, match="unknown event kind"):
            st.import_events([{"kind": "dividend"}])
        assert st.event_count() == 1


def test_load_events_file_validates(tmp_path: Path) -> None:
    p = tmp_path / "e.json"
    p.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        load_events_file(p)
    assert len(load_events_file(FIX / "events.json")) == 6


# ------------------------------------------------------------------- CLI
@pytest.fixture
def loaded(project: Path) -> Path:
    res = runner.invoke(app, ["import-events", str(FIX / "events.json"), "--root", str(project)])
    assert res.exit_code == 0, res.output
    assert DISCLAIMER in res.output and "6 events" in res.output
    return project


def test_cli_wash_sale_blocks(loaded: Path) -> None:
    res = runner.invoke(app, ["wash-sale-blocks", "--asof", "2026-10-01", "--root", str(loaded)])
    assert res.exit_code == 0 and DISCLAIMER in res.output and "VTI" in res.output
    res = runner.invoke(app, ["wash-sale-blocks", "--asof", "2026-10-16", "--root", str(loaded)])
    assert "(none)" in res.output


def test_cli_check_purchase(loaded: Path) -> None:
    res = runner.invoke(
        app,
        ["check-purchase", "VTI", "--account", "ira", "--on", "2026-10-01", "--root", str(loaded)],
    )
    assert res.exit_code == 1 and "BLOCK" in res.output and DISCLAIMER in res.output
    res = runner.invoke(
        app, ["check-purchase", "ITOT", "--on", "2026-10-01", "--root", str(loaded)]
    )
    assert res.exit_code == 0 and "ALLOW" in res.output
    res = runner.invoke(app, ["check-purchase", "ITOT", "--account", "roth", "--root", str(loaded)])
    assert res.exit_code != 0


def test_cli_harvest_scan_and_journal(loaded: Path) -> None:
    args = ["harvest-scan", "--asof", "2026-10-01", "--prices", str(FIX / "prices.json")]
    res = runner.invoke(app, [*args, "--root", str(loaded), "--journal"])
    assert res.exit_code == 0, res.output
    assert DISCLAIMER in res.output
    assert "HARVEST VEA->IEFA" in res.output
    assert "SKIP VTI->ITOT" in res.output  # IRA bought VTI on 2026-09-25
    assert "journaled 2 harvest_proposal entries" in res.output
    with Journal(loaded / "var" / "journal.sqlite") as j:
        assert [e.payload["status"] for e in j.entries("harvest_proposal")] == [
            "proposed",
            "skipped",
        ]


def test_cli_harvest_scan_nothing_to_do(loaded: Path, tmp_path: Path) -> None:
    prices = tmp_path / "p.json"
    prices.write_text(json.dumps({"VTI": 400, "VEA": 70}))
    res = runner.invoke(
        app,
        ["harvest-scan", "--asof", "2026-10-01", "--prices", str(prices), "--root", str(loaded)],
    )
    assert "no taxable lots meet the harvest thresholds" in res.output


def test_cli_lt_warnings(loaded: Path) -> None:
    base = ["lt-warnings", "--asof", "2026-10-01", "--prices", str(FIX / "prices.json")]
    res = runner.invoke(app, [*base, "--root", str(loaded)])
    assert res.exit_code == 0 and "WAIT: lot V3" in res.output and DISCLAIMER in res.output
    res = runner.invoke(app, [*base, "--thesis-broken", "vti", "--root", str(loaded)])
    assert "PROCEED: lot V3" in res.output
    res = runner.invoke(
        app,
        [
            "lt-warnings",
            "--asof",
            "2027-06-01",
            "--prices",
            str(FIX / "prices.json"),
            "--root",
            str(loaded),
        ],
    )
    assert "no lots within" in res.output


def test_cli_realized_report(loaded: Path, tmp_path: Path) -> None:
    out = tmp_path / "reports" / "realized_2026.csv"
    res = runner.invoke(
        app, ["realized-report", "--year", "2026", "--out", str(out), "--root", str(loaded)]
    )
    assert res.exit_code == 0, res.output
    assert DISCLAIMER in res.output and "short-term -900.00" in res.output
    header, rows = _parse(out.read_text())
    assert DISCLAIMER in header
    (r,) = rows
    assert r["symbol"] == "VTI" and r["adjustment_code"] == "W"
    assert r["disallowed_loss"] == "300.00" and r["gain_loss"] == "-900.00"


def test_cli_equivalence_file_and_bad_import(project: Path, tmp_path: Path) -> None:
    eq = tmp_path / "eq.json"
    eq.write_text(json.dumps([["VTI", "VTSAX"]]))
    events = tmp_path / "e.json"
    events.write_text(
        json.dumps(
            [
                {
                    "kind": "buy",
                    "lot_id": "A",
                    "account": "taxable",
                    "symbol": "VTI",
                    "qty": 10,
                    "cost_per_share": 300,
                    "acquired_on": "2026-01-05",
                },
                {
                    "kind": "sell",
                    "sale_id": "S",
                    "account": "taxable",
                    "symbol": "VTI",
                    "sold_on": "2026-09-15",
                    "price": 250,
                    "picks": [{"lot_id": "A", "qty": 10}],
                },
            ]
        )
    )
    root = ["--root", str(project), "--equivalence", str(eq)]
    assert runner.invoke(app, ["import-events", str(events), *root]).exit_code == 0
    res = runner.invoke(app, ["wash-sale-blocks", "--asof", "2026-09-20", *root])
    assert "VTI, VTSAX" in res.output
    bad = tmp_path / "bad.json"
    bad.write_text(
        json.dumps(
            [
                {
                    "kind": "sell",
                    "sale_id": "X",
                    "account": "taxable",
                    "symbol": "VTI",
                    "sold_on": "2026-09-30",
                    "price": 1,
                    "picks": [{"lot_id": "A", "qty": 1}],
                }
            ]
        )
    )
    res = runner.invoke(app, ["import-events", str(bad), *root])
    assert res.exit_code == 1 and "rejected" in res.output


def test_cli_bad_root(tmp_path: Path) -> None:
    (tmp_path / "config").mkdir()
    (tmp_path / "config" / "models.yaml").write_text("tiers: {}\n")
    res = runner.invoke(app, ["wash-sale-blocks", "--root", str(tmp_path)])
    assert res.exit_code == 2


def test_config_equivalence_groups_are_not_replacements() -> None:
    from pathlib import Path

    from committee.config.loader import load_config
    from committee.engines.tax.equivalence import Equivalence

    cfg = load_config(Path(__file__).resolve().parents[1] / "config").tax
    eq = Equivalence.from_lists(cfg.equivalence_groups)
    assert "GOOG" in eq.members("GOOGL")
    for a, b in cfg.replacements.items():
        assert b not in eq.members(a)
