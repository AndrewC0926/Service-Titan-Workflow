"""Weekly screen on a hand-built lake: universe, composite, shortlist, persistence, no look-ahead."""

from __future__ import annotations

import datetime as dt
from pathlib import Path

import pandas as pd
import pytest
from typer.testing import CliRunner

from committee.config.loader import load_config
from committee.config.schema import AppConfig
from committee.data.pit import PIT
from committee.journal.store import Journal
from committee.signals.cli import app as signals_app
from committee.signals.persist import journal_screen, persist_signals, write_lazy_prices_diffs
from committee.signals.screen import ScreenResult, run_screen
from fixtures.signals.builder import ASOF, LakeBuilder, kt

IT, EN = "Information Technology", "Energy"
OLD_RISK = "Supply chain risk from overseas vendors.\n\nCompetition may reduce margins."
NEW_RISK = "Cyber attacks may disrupt operations.\n\nLitigation could be costly and lengthy."


def sid(n: int) -> str:
    return f"CIK{n:010d}"


A, B, C, D, E, F, G = (sid(i) for i in range(1, 8))
H, I_, J, K, L, M = (sid(i) for i in range(11, 17))


def build(b: LakeBuilder) -> None:
    # Information Technology
    b.company(A, "AAA", daily=-0.0008, price=40, shares=1e8)  # cluster buy, weak momentum
    b.company(B, "BBB", daily=0.002, price=80, shares=1e8, net_income=3e8, phase=1.0)
    b.company(C, "CCC", daily=0.0005, price=50, shares=1e8, phase=2.0)
    b.company(D, "DDD", price=10, shares=1e8, adv_usd=10e6, phase=3.0)  # asym, clean
    b.company(E, "EEE", price=4, shares=1e9, adv_usd=10e6, phase=4.0)  # asym, penny stock
    # Energy
    b.company(F, "FFF", sector=EN, daily=0.001, price=60, shares=1e8, phase=5.0)
    b.company(G, "GGG", sector=EN, daily=-0.001, price=30, shares=1e8, phase=6.0)
    # Out of the universe
    b.company(H, "HHH", delist_date=dt.date(2026, 6, 1))
    b.company(I_, "III", exchange="OTC")
    b.company(J, "JJJ", price=1, shares=1e8)  # $100M cap
    b.company(K, "KKK", adv_usd=1e6)
    b.company(L, "LLL")  # active M&A
    b.company(M, "MMM", price=150, shares=1e8, adv_usd=5e6)  # $15B cap, $5M ADV: no bucket
    # A: three opportunistic insiders buying in the last 30 days (cluster buy)
    for who, day in (("i1", 5), ("i2", 12), ("i3", 20)):
        b.insider(A, who, dt.date(2026, 9, day), shares=20_000, price=40)
    # Lazy Prices: C rewrote its risk factors, B did not
    for s, risk in ((B, OLD_RISK), (C, NEW_RISK)):
        for acc, year, text in ((f"{s}-24", 2024, OLD_RISK), (f"{s}-25", 2025, risk)):
            period, known = dt.date(year, 12, 31), dt.date(year + 1, 2, 20)
            b.section(s, acc, "10-K", period, "1A", text, known)
            b.section(s, acc, "10-K", period, "7", "Revenue grew modestly.", known)


@pytest.fixture
def cfg(project: Path) -> AppConfig:
    return load_config(project / "config")


@pytest.fixture
def lake_b(tmp_path: Path) -> LakeBuilder:
    b = LakeBuilder(tmp_path / "lake")
    build(b)
    b.flush()
    return b


def screen(b: LakeBuilder, cfg: AppConfig, **kw) -> ScreenResult:  # type: ignore[no-untyped-def]
    pit = b.pit()
    try:
        return run_screen(
            pit, kw.pop("asof", ASOF), kw.pop("signals", cfg.signals),
            kw.pop("universe", cfg.universe), active_mna={"LLL"}, **kw,
        )  # fmt: skip
    finally:
        pit.close()


def by_id(r: ScreenResult) -> dict[str, object]:
    return {c.security_id: c for c in r.candidates}


def test_universe_filter(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    r = screen(lake_b, cfg)
    assert {c.security_id for c in r.candidates} == {A, B, C, D, E, F, G, M}
    assert r.universe_size == 8
    assert r.excluded[H] == "delisted"
    assert r.excluded[I_].startswith("not_us_listed")
    assert r.excluded[J] == "market_cap_below_min"
    assert r.excluded[K] == "adv_below_min"
    assert r.excluded[L] == "active_mna"
    assert r.excluded[M] == "no_eligible_bucket"


def test_buckets_lottery_and_composite(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    r = screen(lake_b, cfg)
    rows = {c.security_id: c for c in r.candidates}
    assert rows[A].bucket == "core_pick" and rows[D].bucket == "asymmetric_bet"
    assert rows[A].lottery is None  # core picks skip the lottery filter
    assert rows[D].lottery is not None and rows[D].lottery.passed
    assert rows[D].asymmetric_profile is not None
    assert r.excluded[E].startswith("lottery_filter: penny_stock")
    for c in r.candidates:
        assert c.composite == pytest.approx(sum(c.contributions.values()))
        assert c.contributions["insider_opportunistic"] >= 0.0
        assert c.contributions["lazy_prices"] <= 0.0
        assert "earnings_revision" not in c.contributions  # feature flag off
        assert all(abs(z) <= cfg.signals.winsorize_z for z in c.zscores.values() if z is not None)
    # within-sector z-scores: momentum of the two energy names is +/-1
    assert rows[F].zscores["momentum"] == pytest.approx(1.0)
    assert rows[G].zscores["momentum"] == pytest.approx(-1.0)
    # insider cluster
    assert rows[A].flags["recent_cluster_buy"] and rows[A].flags["cluster_buy"]
    assert rows[A].contributions["cluster_buy_bonus"] == cfg.signals.cluster_buy_bonus
    assert rows[A].contributions["insider_opportunistic"] > 0
    # Lazy Prices: rewritten risk factors are negative, unchanged ones contribute 0
    assert rows[C].contributions["lazy_prices"] < 0
    assert rows[B].contributions["lazy_prices"] == 0.0
    assert rows[B].signals["lazy_prices"] == pytest.approx(1.0)
    # ranking is composite-descending
    comps = [c.composite for c in r.candidates]
    assert comps == sorted(comps, reverse=True)


def test_shortlist_top_n_plus_cluster_buys(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    u = cfg.universe.model_copy(update={"shortlist_size": 1})
    r = screen(lake_b, cfg, universe=u)
    eligible = [c for c in r.candidates if c.security_id not in r.excluded]
    top = eligible[0]
    ids = [s.security_id for s in r.shortlist]
    assert ids[0] == top.security_id and r.shortlist[0].shortlist_reason == "top_composite"
    assert A in ids
    if top.security_id != A:
        assert r.shortlist[-1].shortlist_reason == "cluster_buy" and len(ids) == 2


def test_full_shortlist_and_removals(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    r = screen(lake_b, cfg)
    assert {s.security_id for s in r.shortlist} == {A, B, C, D, F, G}
    r2 = screen(lake_b, cfg, holdings_under_review={"bbb"}, wash_sale_blocked={C})
    assert {s.security_id for s in r2.shortlist} == {A, D, F, G}
    assert r2.excluded[B] == "holding_under_review"
    assert r2.excluded[C] == "wash_sale_block"


def test_earnings_revision_behind_flag(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    lake_b.estimate(B, "FY2026", dt.date(2026, 6, 1), 2.0)
    lake_b.estimate(B, "FY2026", dt.date(2026, 9, 20), 2.5)
    lake_b.estimate(C, "FY2026", dt.date(2026, 6, 1), 2.0)
    lake_b.estimate(C, "FY2026", dt.date(2026, 9, 20), 1.8)
    on = cfg.signals.model_copy(update={"earnings_revision_enabled": True})
    rows = {c.security_id: c for c in screen(lake_b, cfg, signals=on).candidates}
    assert rows[B].signals["earnings_revision"] == pytest.approx(0.25)
    assert rows[C].signals["earnings_revision"] == pytest.approx(-0.10)
    assert (
        rows[B].contributions["earnings_revision"] > 0 > rows[C].contributions["earnings_revision"]
    )
    off = {c.security_id: c for c in screen(lake_b, cfg).candidates}
    assert "earnings_revision" not in off[B].signals


def test_flags_short_interest_and_13f(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    lake_b.short(B, dt.date(2026, 9, 15), dtc=12.0, pct=0.05)
    lake_b.short(C, dt.date(2026, 9, 15), dtc=2.0, pct=25.0)  # percent units
    lake_b.short(F, dt.date(2026, 9, 15), dtc=2.0, pct=0.05)
    for filer in (1, 2):
        lake_b.holding(filer, dt.date(2026, 3, 31), B)
    for filer in (1, 2, 3, 4):
        lake_b.holding(filer, dt.date(2026, 6, 30), B)
    rows = {c.security_id: c for c in screen(lake_b, cfg).candidates}
    assert rows[B].flags["high_short_interest"] and rows[C].flags["high_short_interest"]
    assert not rows[F].flags["high_short_interest"]
    assert rows[C].flags["short_interest"]["pct_float"] == pytest.approx(0.25)
    assert rows[B].flags["crowding_13f"] == {
        "period": "2026-06-30",
        "holders": 4,
        "holders_prior": 2,
        "change": 2,
    }
    base = {
        c.security_id: c.composite for c in screen(LakeBuilder(lake_b.lake.root), cfg).candidates
    }
    assert rows[B].composite == pytest.approx(base[B])  # flags carry no weight


def test_no_lookahead(lake_b: LakeBuilder, cfg: AppConfig) -> None:
    before = screen(lake_b, cfg)
    late = dt.date(2026, 9, 30)
    # Rows that happened before the as-of but were only known after it.
    for who in ("x1", "x2", "x3"):
        lake_b.insider(G, who, dt.date(2026, 9, 24), shares=1e6, price=30, filed=late)
    lake_b.fundamental(B, "net_income", "FY2025", -5e9, dt.date(2025, 12, 31), late)
    lake_b.fundamental(D, "shares_outstanding", "2026Q2", 5e8, dt.date(2026, 6, 30), late)
    for i in range(50):
        lake_b.news(D, late, i)
    lake_b.section(F, "f-25", "10-K", dt.date(2025, 12, 31), "1A", NEW_RISK, late)
    lake_b.section(F, "f-24", "10-K", dt.date(2024, 12, 31), "1A", OLD_RISK, dt.date(2025, 2, 1))
    lake_b.security(C, "CCC", delist_date=dt.date(2026, 9, 1), known=late)
    lake_b.add(
        "prices_daily",
        {"security_id": D, "date": dt.date(2026, 9, 25), "open": 1.0, "high": 1.0, "low": 1.0,
         "close": 1.0, "adj_close": 1.0, "volume": 1.0,
         "event_time": kt(dt.date(2026, 9, 25)), "known_time": kt(late)},
    )  # fmt: skip
    after = screen(lake_b, cfg)
    assert [(c.security_id, c.composite) for c in after.candidates] == [
        (c.security_id, c.composite) for c in before.candidates
    ]
    assert [s.security_id for s in after.shortlist] == [s.security_id for s in before.shortlist]
    # ...and the same rows do count once the as-of date reaches their known_time
    later = screen(lake_b, cfg, asof=dt.date(2026, 10, 1))
    assert later.excluded[C] == "delisted"


def test_persist_signals_and_journal(lake_b: LakeBuilder, cfg: AppConfig, tmp_path: Path) -> None:
    r = screen(lake_b, cfg)
    n = persist_signals(lake_b.lake, r)
    assert n == len(r.candidates) * (len(r.candidates[0].signals) + 4)
    pit = PIT(lake_b.lake)
    # NOTE: PIT.latest("signals") fails today because `asof` (a DuckDB keyword) is an
    # unquoted partition key in TABLE_KEYS; read with a quoted column instead.
    sql = (
        "SELECT * FROM signals WHERE as_of(known_time, $asof) QUALIFY row_number() OVER "
        '(PARTITION BY security_id, signal_name, "asof" ORDER BY known_time DESC, ingest_id DESC) = 1'
    )
    rows = pit.query(sql, ASOF)
    assert len(rows) == n
    assert set(rows["asof"].dt.date) == {ASOF}
    assert (
        rows["known_time"] == pd.Timestamp(kt(ASOF, 0)).replace(hour=23, minute=59, second=59)
    ).all()
    assert pit.query(sql, ASOF - dt.timedelta(days=1)).empty
    comp = rows[(rows["signal_name"] == "composite") & (rows["security_id"] == A)]
    assert float(comp["value"].iloc[0]) == pytest.approx(by_id(r)[A].composite)  # type: ignore[attr-defined]
    pit.close()

    with Journal(tmp_path / "j.sqlite") as j:
        e = journal_screen(j, r, dict(cfg.signals.weights))
        assert e.entry_type == "screen"
        assert [x["security_id"] for x in e.payload["shortlist"]] == [
            s.security_id for s in r.shortlist
        ]
        assert e.payload["excluded_counts"]["lottery_filter"] == 1
        assert j.verify().ok

    paths = write_lazy_prices_diffs(r, tmp_path / "lp")
    assert {p.stem for p in paths} == {B, C}


def test_cli_screen(project: Path) -> None:
    b = LakeBuilder(project / "var" / "data")
    build(b)
    b.flush()
    res = CliRunner().invoke(
        signals_app, ["screen", "--asof", "2026-09-27", "--root", str(project)]
    )
    assert res.exit_code == 0, res.output
    assert "Screen as of 2026-09-27" in res.output
    for t in ("AAA", "BBB", "DDD"):
        assert t in res.output
    assert "EEE: penny_stock" in res.output
    assert "journal seq" in res.output
    with Journal(project / "var" / "journal.sqlite") as j:
        assert [e.entry_type for e in j.entries("screen")] == ["screen"]


def test_cli_without_lake(project: Path) -> None:
    res = CliRunner().invoke(
        signals_app, ["screen", "--asof", "2026-09-27", "--root", str(project)]
    )
    assert res.exit_code == 1


def test_screen_with_sparse_lake(tmp_path: Path, cfg: AppConfig) -> None:
    """Only master, prices and fundamentals: optional tables absent, nothing crashes."""
    b = LakeBuilder(tmp_path / "sparse")
    b.company(A, "AAA")
    b.company(B, "BBB", phase=1.0)
    r = screen(b, cfg)
    assert {s.security_id for s in r.shortlist} == {A, B}
    for c in r.candidates:
        assert c.signals["insider_opportunistic"] == 0.0
        assert c.signals["lazy_prices"] is None
        assert c.contributions["lazy_prices"] == 0.0
