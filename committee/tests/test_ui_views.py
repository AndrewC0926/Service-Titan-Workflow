from __future__ import annotations

import datetime as dt
from pathlib import Path

from committee.broker.factory import is_live, make_broker
from committee.broker.models import OrderRequest
from committee.broker.sim import SimBroker
from committee.journal.store import Journal
from committee.ui.views import approval_queue, costs, search

T = dt.datetime(2026, 10, 5, 12, tzinfo=dt.UTC)


def test_queue_costs_search(tmp_path: Path) -> None:
    clock = [T]
    j = Journal(tmp_path / "j.sqlite", clock=lambda: clock[0])
    leg = [{"symbol": "ABC", "side": "buy", "account": "ira", "max_pct_total": 2}]
    j.append(
        "briefing", {"symbol": "ABC", "recommendation": "BUY", "legs": leg, "cooling_off_hours": 24}
    )
    j.append(
        "briefing", {"symbol": "OLD", "recommendation": "BUY", "legs": leg, "cooling_off_hours": 24}
    )
    j.append(
        "briefing",
        {"symbol": "NOPE", "recommendation": "PASS", "legs": [], "cooling_off_hours": 24},
    )
    j.append("agent_output", {"agent": "chair", "run_id": "r1", "cost_usd": 0.5})
    j.append("agent_output", {"agent": "bear", "run_id": "r1", "cost_usd": 0.25})
    clock[0] = T + dt.timedelta(days=1)
    q = approval_queue(j, clock[0])
    assert [i.label for i in q] == ["ABC", "OLD"] and q[0].ready
    q2 = approval_queue(j, T + dt.timedelta(days=8))
    assert q2 == []
    c = costs(j, clock[0])
    assert c["total_usd"] == 0.75 and c["by_agent"]["chair"] == 0.5 and c["per_review_usd"] == 0.75
    assert len(search(j, "OLD")) == 1 and len(search(j, entry_type="agent_output")) == 2


def test_sim_broker_persists(tmp_path: Path) -> None:
    b = SimBroker(tmp_path / "sim.json")
    o = b.submit_limit_order(
        OrderRequest(
            client_order_id="c1",
            symbol="ABC",
            side="buy",
            qty=3,
            limit_price=10,
            time_in_force="day",
            account="taxable",
        )
    )
    assert o.status == "filled"
    b2 = SimBroker(tmp_path / "sim.json")
    assert b2.positions() == {"ABC": 3.0} and len(b2.list_orders()) == 1
    o2 = b2.submit_limit_order(
        OrderRequest(
            client_order_id="c2",
            symbol="ABC",
            side="sell",
            qty=1,
            limit_price=11,
            time_in_force="day",
            account="taxable",
        )
    )
    assert o2.broker_order_id != o.broker_order_id
    assert b2.cancel_all_orders() == 0 and b2.is_paper


def test_factory_defaults_to_sim(ctx) -> None:  # type: ignore[no-untyped-def]
    with ctx.journal() as j:
        assert not is_live(ctx, j)
        assert isinstance(make_broker(ctx, j), SimBroker)
