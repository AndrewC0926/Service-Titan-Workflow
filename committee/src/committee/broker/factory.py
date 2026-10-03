"""Choose the broker adapter: live Alpaca only through the live gate; otherwise
Alpaca paper when paper keys exist; otherwise the offline simulator."""

from __future__ import annotations

from committee.broker.alpaca import AlpacaBroker
from committee.broker.base import BrokerAdapter
from committee.broker.live_gate import live_allowed
from committee.broker.sim import SimBroker
from committee.context import AppContext
from committee.journal.store import Journal


def is_live(ctx: AppContext, journal: Journal) -> bool:
    ok, _ = live_allowed(
        ctx.secrets.live_trading_enabled(), ctx.path(ctx.config.app.broker.live_gate_file), journal
    )
    return ok


def make_broker(ctx: AppContext, journal: Journal) -> BrokerAdapter:
    if is_live(ctx, journal):
        return AlpacaBroker(
            ctx.secrets.require("ALPACA_LIVE_KEY"),
            ctx.secrets.require("ALPACA_LIVE_SECRET"),
            paper=False,
        )
    key, secret = ctx.secrets.get("ALPACA_PAPER_KEY"), ctx.secrets.get("ALPACA_PAPER_SECRET")
    if key and secret:
        return AlpacaBroker(key, secret, paper=True)
    return SimBroker(ctx.var_dir / "sim_broker.json")
