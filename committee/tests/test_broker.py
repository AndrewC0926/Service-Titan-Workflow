from __future__ import annotations

import datetime as dt
from pathlib import Path
from types import SimpleNamespace

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from committee.broker.alpaca import AlpacaBroker
from committee.broker.approval import ApprovalError, approve, decided, expire_stale, reject
from committee.broker.fake import FakeBroker
from committee.broker.gateway import (
    Caps,
    OrderBlocked,
    OrderGateway,
    kill_switch,
    release_kill_switch,
)
from committee.broker.live_gate import file_sha256, live_allowed
from committee.broker.models import ApprovedLeg
from committee.journal.store import Journal
from committee.ops.flags import Flags

T0 = dt.datetime(2026, 10, 5, 14, 0, tzinfo=dt.UTC)


class Clock:
    def __init__(self) -> None:
        self.t = T0

    def __call__(self) -> dt.datetime:
        return self.t

    def advance(self, **kw: float) -> None:
        self.t += dt.timedelta(**kw)


@pytest.fixture
def env(tmp_path: Path):  # type: ignore[no-untyped-def]
    clock = Clock()
    j = Journal(tmp_path / "j.sqlite", clock=clock)
    flags = Flags(tmp_path / "flags")
    broker = FakeBroker()
    gw = OrderGateway(j, broker, flags, Caps(), last_close=lambda s: 100.0, clock=clock)
    return SimpleNamespace(j=j, clock=clock, flags=flags, broker=broker, gw=gw)


def brief(
    j: Journal,
    *,
    severity: str = "none",
    cooling: int = 24,
    rec: str = "BUY",
    max_pct: float = 3.0,
    account: str = "ira",
) -> str:
    e = j.append(
        "briefing",
        {
            "symbol": "ABC",
            "recommendation": rec,
            "legs": [
                {"symbol": "ABC", "side": "buy", "account": account, "max_pct_total": max_pct}
            ],
            "cooling_off_hours": cooling,
            "behavioral_severity": severity,
        },
    )
    return e.hash


LEG = [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=3.0)]
REASON = "Insider cluster plus cheap valuation; sized per engine."


def test_cooling_off_enforced(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j)
    env.clock.advance(hours=23)
    with pytest.raises(ApprovalError, match="cooling-off"):
        approve(env.j, h, LEG, REASON, 100_000, env.clock())
    env.clock.advance(hours=1)
    rec, e = approve(env.j, h, LEG, REASON, 100_000, env.clock())
    assert e.entry_type == "approval" and rec.briefing_hash == h
    assert decided(env.j, h) == "approved"
    with pytest.raises(ApprovalError, match="already decided"):
        approve(env.j, h, LEG, REASON, 100_000, env.clock())


def test_stop_requires_justification_and_72h(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j, severity="stop", cooling=72)
    env.clock.advance(hours=48)
    with pytest.raises(ApprovalError, match="cooling-off"):
        approve(env.j, h, LEG, REASON, 100_000, env.clock())
    env.clock.advance(hours=24)
    with pytest.raises(ApprovalError, match="justification"):
        approve(env.j, h, LEG, REASON, 100_000, env.clock())
    approve(
        env.j,
        h,
        LEG,
        REASON,
        100_000,
        env.clock(),
        justification="Thesis predates the run-up; documented in the March note.",
    )


def test_never_larger_never_unrecommended_needs_reason(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j, cooling=0)
    with pytest.raises(ApprovalError, match="smaller only"):
        approve(
            env.j,
            h,
            [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=3.5)],
            REASON,
            1e5,
            env.clock(),
        )
    with pytest.raises(ApprovalError, match="not recommended"):
        approve(
            env.j,
            h,
            [ApprovedLeg(symbol="XYZ", side="buy", account="ira", pct_total=1)],
            REASON,
            1e5,
            env.clock(),
        )
    with pytest.raises(ApprovalError, match="not recommended"):
        approve(
            env.j,
            h,
            [ApprovedLeg(symbol="ABC", side="buy", account="taxable", pct_total=1)],
            REASON,
            1e5,
            env.clock(),
        )
    with pytest.raises(ApprovalError, match="reason"):
        approve(env.j, h, LEG, "ok", 1e5, env.clock())
    with pytest.raises(ApprovalError, match="at least one"):
        approve(env.j, h, [], REASON, 1e5, env.clock())
    approve(
        env.j,
        h,
        [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=1.0)],
        REASON,
        1e5,
        env.clock(),
    )


def test_expiry_and_pass_and_non_briefing(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j)
    env.clock.advance(days=7, minutes=1)
    with pytest.raises(ApprovalError, match="expired"):
        approve(env.j, h, LEG, REASON, 1e5, env.clock())
    assert expire_stale(env.j, env.clock()) and decided(env.j, h) == "expired"
    hp = brief(env.j, rec="PASS", cooling=0)
    with pytest.raises(ApprovalError, match="nothing to approve"):
        approve(env.j, hp, LEG, REASON, 1e5, env.clock())
    note = env.j.append("note", {"x": 1})
    with pytest.raises(ApprovalError, match="not an approvable"):
        approve(env.j, note.hash, LEG, REASON, 1e5, env.clock())
    with pytest.raises(ApprovalError, match="no journal entry"):
        approve(env.j, "0" * 64, LEG, REASON, 1e5, env.clock())


def test_reject(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j)
    reject(env.j, h, "Valuation already prices in success.", env.clock())
    assert decided(env.j, h) == "rejected"
    with pytest.raises(ApprovalError):
        reject(env.j, h, "again again again", env.clock())


def _approved(env, pct: float = 3.0, value: float = 100_000) -> str:  # type: ignore[no-untyped-def]
    h = brief(env.j, cooling=0, max_pct=pct)
    _, e = approve(
        env.j,
        h,
        [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=pct)],
        REASON,
        value,
        env.clock(),
    )
    return e.hash


def test_no_order_without_approval(env) -> None:  # type: ignore[no-untyped-def]
    h = brief(env.j, cooling=0)
    with pytest.raises(OrderBlocked, match="no journaled approval"):
        env.gw.place(h, 100_000)  # a briefing hash is not an approval
    with pytest.raises(OrderBlocked):
        env.gw.place("f" * 64, 100_000)
    assert env.broker.submitted == []


def test_caps_and_tranches(env) -> None:  # type: ignore[no-untyped-def]
    a = _approved(env, pct=3.0)  # $3,000 target; per-order cap 2% = $2,000
    placed = env.gw.place(a, 100_000)
    assert len(placed) == 1
    o = placed[0]
    assert o.limit_price == 100.5 and o.notional_usd <= 2000 and o.qty == 19
    placed2 = env.gw.place(a, 100_000)  # next tranche, same day, under the 5% daily cap
    assert sum(p.notional_usd for p in placed + placed2) <= 3000
    assert env.gw.place(a, 100_000) == []  # fully executed: idempotent
    assert all(r.client_order_id.startswith("cmt-") for r in env.broker.submitted)
    assert len({r.client_order_id for r in env.broker.submitted}) == len(env.broker.submitted)


def test_daily_cap(env) -> None:  # type: ignore[no-untyped-def]
    a1, a2, a3 = (_approved(env, pct=2.0) for _ in range(3))
    total = 0.0
    for a in (a1, a2, a3):
        total += sum(p.notional_usd for p in env.gw.place(a, 100_000))
    assert total <= 5000
    env.clock.advance(days=1)
    total2 = sum(p.notional_usd for p in env.gw.place(a3, 100_000))
    assert total2 > 0


def test_kill_switch_blocks_and_cancels(env) -> None:  # type: ignore[no-untyped-def]
    env.broker.auto_fill = False
    a = _approved(env)
    env.gw.place(a, 100_000)
    n = kill_switch(env.j, env.broker, env.flags, "drill")
    assert n == 1
    with pytest.raises(OrderBlocked, match="kill_switch"):
        env.gw.place(a, 100_000)
    release_kill_switch(env.j, env.flags, "drill over")
    env.flags.set("frozen", "journal verify failed")
    with pytest.raises(OrderBlocked, match="frozen"):
        env.gw.place(a, 100_000)


def test_broker_error_is_journaled(env) -> None:  # type: ignore[no-untyped-def]
    a = _approved(env)
    env.broker.fail_submit = True
    with pytest.raises(OrderBlocked, match="broker error"):
        env.gw.place(a, 100_000)
    rejected = [e for e in env.j.entries("order") if e.payload["status"] == "rejected"]
    assert len(rejected) == 1
    env.broker.fail_submit = False
    assert env.gw.place(a, 100_000)  # rejected orders don't count toward done


def test_reconcile_fills_once(env) -> None:  # type: ignore[no-untyped-def]
    a = _approved(env)
    env.gw.place(a, 100_000)
    got: list[object] = []
    fills = env.gw.reconcile(on_fill=got.append)
    assert len(fills) == 1 and got == fills
    assert env.gw.reconcile() == []
    assert env.gw.unreconciled_orders() == []


def test_live_manual_routing_and_mode_guard(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    with pytest.raises(ValueError):
        OrderGateway(
            j, FakeBroker(is_paper=True), Flags(tmp_path), Caps(), lambda s: 1.0, live=True
        )
    with pytest.raises(ValueError):
        OrderGateway(
            j, FakeBroker(is_paper=False), Flags(tmp_path), Caps(), lambda s: 1.0, live=False
        )
    live_broker = FakeBroker(is_paper=False)
    gw = OrderGateway(j, live_broker, Flags(tmp_path / "f"), Caps(), lambda s: 50.0, live=True)
    h = j.append(
        "briefing",
        {
            "recommendation": "BUY",
            "cooling_off_hours": 0,
            "legs": [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 1}],
        },
    ).hash
    _, e = approve(
        j,
        h,
        [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=1)],
        REASON,
        100_000,
        dt.datetime.now(dt.UTC),
    )
    placed = gw.place(e.hash, 100_000)
    assert (
        placed[0].venue == "manual" and live_broker.submitted == []
    )  # IRA trades are placed by hand when live


def test_live_gate(tmp_path: Path) -> None:
    j = Journal(tmp_path / "j.sqlite")
    gate = tmp_path / "LIVE_GATE.signed.json"
    assert not live_allowed(False, gate, j)[0]
    assert "not found" in live_allowed(True, gate, j)[1]
    gate.write_text('{"gates": "all pass"}')
    assert "not signed" in live_allowed(True, gate, j)[1]
    j.append("live_gate", {"file_sha256": file_sha256(gate), "signed_by_human": True})
    assert live_allowed(True, gate, j)[0]
    gate.write_text('{"gates": "edited"}')
    assert not live_allowed(True, gate, j)[0]


@settings(max_examples=40, deadline=None)
@given(pct=st.floats(0.1, 10), value=st.floats(10_000, 5_000_000), close=st.floats(1, 2000))
def test_property_caps_never_exceeded(
    tmp_path_factory: pytest.TempPathFactory, pct: float, value: float, close: float
) -> None:
    d = tmp_path_factory.mktemp("p")
    j = Journal(d / "j.sqlite")
    gw = OrderGateway(j, FakeBroker(), Flags(d / "f"), Caps(), lambda s: close)
    h = j.append(
        "briefing",
        {
            "recommendation": "BUY",
            "cooling_off_hours": 0,
            "legs": [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": pct}],
        },
    ).hash
    _, e = approve(
        j,
        h,
        [ApprovedLeg(symbol="ABC", side="buy", account="ira", pct_total=pct)],
        REASON,
        value,
        dt.datetime.now(dt.UTC),
    )
    total = 0.0
    for _ in range(5):
        for p in gw.place(e.hash, value):
            assert p.notional_usd <= value * 0.02 + 1e-6
            total += p.notional_usd
    assert total <= value * 0.05 + 1e-6
    assert total <= value * pct / 100 + 1e-6


def test_alpaca_adapter_with_fake_client() -> None:
    order = SimpleNamespace(
        id="o1",
        client_order_id="cmt-1",
        symbol="ABC",
        side=SimpleNamespace(value="buy"),
        qty="10",
        limit_price="100.5",
        status=SimpleNamespace(value="filled"),
        filled_qty="10",
        filled_avg_price="100.4",
        filled_at=T0,
    )

    class Client:
        def __init__(self) -> None:
            self.sent: list[object] = []

        def submit_order(self, req: object) -> object:
            self.sent.append(req)
            return order

        def cancel_orders(self) -> list[object]:
            return [1, 2]

        def get_orders(self, req: object) -> list[object]:
            return [order]

        def get_all_positions(self) -> list[object]:
            return [SimpleNamespace(symbol="ABC", qty="10")]

    from committee.broker.models import OrderRequest

    b = AlpacaBroker("PKSECRET123", "s3cr3tvalue", paper=True, client=Client())
    bo = b.submit_limit_order(
        OrderRequest(
            client_order_id="cmt-1",
            symbol="ABC",
            side="buy",
            qty=10,
            limit_price=100.5,
            time_in_force="day",
            account="taxable",
        )
    )
    assert bo.filled_qty == 10 and bo.side == "buy" and b.is_paper
    assert b.cancel_all_orders() == 2
    assert b.list_orders()[0].status == "filled"
    assert b.positions() == {"ABC": 10.0}
    assert "PKSECRET123" not in repr(b) and "s3cr3tvalue" not in repr(b)
