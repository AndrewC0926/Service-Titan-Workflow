from __future__ import annotations

import datetime as dt
from pathlib import Path

import pytest

from committee.broker.approval import approve
from committee.broker.models import ApprovedLeg
from committee.config.loader import load_config
from committee.core.drift import compute_drift
from committee.core.holdings import CASH, HoldingsStore, ImportError_, Position, parse_positions_csv
from committee.core.rebalance import propose_rebalance
from committee.journal.store import Journal

CFG = load_config(Path(__file__).resolve().parents[1] / "config")
POLICY = CFG.policy_portfolio
PRICES = {
    "VTI": 300.0,
    "AVUV": 100.0,
    "VEA": 50.0,
    "AVDV": 70.0,
    "VWO": 45.0,
    "DBMF": 30.0,
    "SGOV": 100.0,
    "NVDA": 150.0,
}
NOW = dt.datetime(2026, 10, 5, tzinfo=dt.UTC)


def price(s: str) -> float:
    return PRICES[s]


def on_target(total: float = 1_000_000) -> list[Position]:
    """A portfolio exactly at the policy targets (cash = liquidity sleeve)."""
    out = []
    for s in POLICY.sleeves:
        v = total * s.target_pct / 100
        if s.id == "satellite":
            out.append(Position("ira", "NVDA", v / PRICES["NVDA"]))
        elif s.id == "tbills":
            out.append(Position("taxable", CASH, v))
        else:
            out.append(Position(s.location[0], s.holding or "", v / PRICES[s.holding or ""]))
    return out


def test_on_target_has_no_breaches() -> None:
    d = compute_drift(POLICY, on_target(), price, satellite_symbols={"NVDA"})
    assert d.total_value_usd == pytest.approx(1_000_000)
    assert d.breaches == [] and d.unmapped == {}
    assert propose_rebalance(POLICY, d, on_target(), price).legs == []


def _shift(positions: list[Position], sym: str, factor: float) -> list[Position]:
    return [
        Position(p.account, p.symbol, p.qty * factor if p.symbol == sym else p.qty)
        for p in positions
    ]


def test_breach_uses_new_cash_first() -> None:
    pos = _shift(on_target(), "VTI", 0.80)  # VTI drops to ~27.6% of total, band 4
    pos.append(Position("taxable", CASH, 70_000))
    d = compute_drift(POLICY, pos, price, satellite_symbols={"NVDA"})
    assert {b.sleeve_id for b in d.breaches} == {"us_total", "tbills"}  # new cash sits in liquidity
    prop = propose_rebalance(POLICY, d, pos, price)
    assert prop.legs and all(leg.side == "buy" for leg in prop.legs)
    assert prop.legs[0].symbol == "VTI" and prop.legs[0].account == "taxable"
    assert prop.legs[0].why == "new cash"


def test_tax_free_swaps_before_taxable_sales() -> None:
    pos = _shift(on_target(), "AVUV", 1.6)  # IRA small value overweight
    d = compute_drift(POLICY, pos, price, satellite_symbols={"NVDA"})
    assert any(b.sleeve_id == "us_scv" for b in d.breaches)
    taxed: list[str] = []
    prop = propose_rebalance(
        POLICY,
        d,
        pos,
        price,
        tax_cost=lambda s, a, x: taxed.append(a) or (0.1 * x if a == "taxable" else 0.0),
    )
    sells = [leg for leg in prop.legs if leg.side == "sell"]
    assert sells and sells[0].symbol == "AVUV" and sells[0].account == "ira"
    assert all(
        leg.account in ("ira", "k401") for leg in prop.legs if leg.why == "tax-free rebalance"
    )
    # every buy respects the sleeve's location table
    loc = {s.holding: s.location for s in POLICY.sleeves}
    assert all(leg.account in loc[leg.symbol] for leg in prop.legs if leg.side == "buy")


def test_taxable_sale_picks_lowest_tax_cost() -> None:
    pos = _shift(_shift(on_target(), "VTI", 1.3), "VEA", 1.3)
    pos = _shift(pos, "VWO", 0.5)  # VWO underweight; taxable-eligible
    d = compute_drift(POLICY, pos, price, satellite_symbols={"NVDA"})
    costs = {"VTI": 0.20, "VEA": 0.01}
    prop = propose_rebalance(POLICY, d, pos, price, tax_cost=lambda s, a, x: costs.get(s, 0.0) * x)
    taxable_sells = [leg for leg in prop.legs if leg.side == "sell" and leg.account == "taxable"]
    assert taxable_sells and taxable_sells[0].symbol == "VEA"
    payload = prop.payload()
    assert payload["recommendation"] == "REBALANCE"
    assert all(0 < leg["max_pct_total"] <= 100 for leg in payload["legs"])  # type: ignore[index, union-attr]


def test_proposal_goes_through_the_gate(tmp_path: Path) -> None:
    pos = [*_shift(on_target(), "VTI", 0.8), Position("taxable", CASH, 70_000)]
    d = compute_drift(POLICY, pos, price, satellite_symbols={"NVDA"})
    prop = propose_rebalance(POLICY, d, pos, price)
    j = Journal(tmp_path / "j.sqlite")
    e = j.append("core_proposal", prop.payload(cooling_off_hours=0))
    leg = prop.payload()["legs"][0]  # type: ignore[index]
    _, a = approve(
        j,
        e.hash,
        [
            ApprovedLeg(
                symbol=leg["symbol"],
                side=leg["side"],
                account=leg["account"],
                pct_total=leg["max_pct_total"],
            )
        ],
        "Rebalance VTI back toward target with new cash.",
        d.total_value_usd,
        dt.datetime.now(dt.UTC),
    )
    assert a.entry_type == "approval"


def test_unmapped_symbols_reported() -> None:
    pos = [*on_target(), Position("taxable", "TSLA", 10)]
    PRICES["TSLA"] = 200.0
    d = compute_drift(POLICY, pos, price, satellite_symbols={"NVDA"})
    assert "TSLA" in d.unmapped


def test_csv_import_and_snapshots(tmp_path: Path) -> None:
    csv = tmp_path / "ira.csv"
    csv.write_text(
        "account,symbol,qty,cost_per_share,acquired_on\nIRA,avuv,100,90,2024-01-02\n401k,VTI,5,,\nira,CASH,1000,,\n"
    )
    store = HoldingsStore(tmp_path / "state.sqlite")
    assert store.import_csv(csv, NOW) == {"ira": 2, "k401": 1}
    cur = store.current()
    assert {(p.account, p.symbol) for p in cur} == {
        ("ira", "AVUV"),
        ("ira", "CASH"),
        ("k401", "VTI"),
    }
    csv.write_text("account,symbol,qty\nira,AVUV,50\n")
    store.import_csv(csv, NOW + dt.timedelta(days=1))
    cur = store.current()
    assert {(p.account, p.symbol, p.qty) for p in cur if p.account == "ira"} == {
        ("ira", "AVUV", 50.0)
    }
    assert any(p.account == "k401" for p in cur)  # other accounts untouched


@pytest.mark.parametrize(
    "body,msg",
    [
        ("symbol,qty\nVTI,1\n", "missing columns"),
        ("account,symbol,qty\nroth,VTI,1\n", "unknown account"),
        ("account,symbol,qty\nira,VTI,-5\n", "short"),
        ("account,symbol,qty\nira,VTI,abc\n", "could not convert"),
    ],
)
def test_csv_errors(tmp_path: Path, body: str, msg: str) -> None:
    p = tmp_path / "x.csv"
    p.write_text(body)
    with pytest.raises(ImportError_, match=msg):
        parse_positions_csv(p)


def test_apply_fill_moves_shares_and_cash(tmp_path: Path) -> None:
    from committee.core.holdings import apply_fill

    store = HoldingsStore(tmp_path / "s.sqlite")
    store.snapshot("ira", [Position("ira", CASH, 10_000.0), Position("ira", "AVUV", 5)], "t", NOW)
    apply_fill(store, "ira", "ABC", "buy", 10, 100.0, NOW.date(), NOW + dt.timedelta(seconds=1))
    cur = {p.symbol: p.qty for p in store.current() if p.account == "ira"}
    assert cur == {"CASH": 9_000.0, "AVUV": 5, "ABC": 10}
    apply_fill(store, "ira", "ABC", "sell", 10, 110.0, NOW.date(), NOW + dt.timedelta(seconds=2))
    cur = {p.symbol: p.qty for p in store.current() if p.account == "ira"}
    assert cur == {"CASH": 10_100.0, "AVUV": 5}
    with pytest.raises(ValueError, match="not held"):
        apply_fill(store, "ira", "XYZ", "sell", 1, 1.0, NOW.date(), NOW + dt.timedelta(seconds=3))
    with pytest.raises(ValueError, match="short"):
        apply_fill(store, "ira", "AVUV", "sell", 6, 1.0, NOW.date(), NOW + dt.timedelta(seconds=4))
