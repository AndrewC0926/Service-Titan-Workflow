"""Order gateway: the ONLY path from an approval to a broker.

Checks, in order: kill switch / freeze flags, a journaled approval referencing a
journaled briefing, per-order and daily notional caps (independent of the risk
engine), and idempotency. Large approvals execute in tranches under the caps.
Orders are limit orders priced from the last close plus/minus a band.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import math
from collections.abc import Callable
from dataclasses import dataclass

from committee.broker.base import BrokerAdapter
from committee.broker.models import ApprovalRecord, Fill, OrderRequest
from committee.journal.store import Journal, JournalEntry
from committee.ops.flags import Flags

TRANCHE_WINDOW_DAYS = 30


class OrderBlocked(Exception):
    pass


@dataclass(frozen=True)
class Caps:
    per_order_pct: float = 2.0
    daily_pct: float = 5.0
    limit_band_pct: float = 0.5
    time_in_force: str = "day"


@dataclass(frozen=True)
class PlacedOrder:
    client_order_id: str
    symbol: str
    side: str
    qty: int
    limit_price: float
    notional_usd: float
    venue: str  # broker | manual
    journal_seq: int


def _cid(approval_hash: str, symbol: str, side: str, n: int) -> str:
    return "cmt-" + hashlib.sha256(f"{approval_hash}:{symbol}:{side}:{n}".encode()).hexdigest()[:20]


class OrderGateway:
    def __init__(
        self,
        journal: Journal,
        broker: BrokerAdapter,
        flags: Flags,
        caps: Caps,
        last_close: Callable[[str], float],
        clock: Callable[[], dt.datetime] = lambda: dt.datetime.now(dt.UTC),
        live: bool = False,
    ) -> None:
        self.journal = journal
        self.broker = broker
        self.flags = flags
        self.caps = caps
        self.last_close = last_close
        self.clock = clock
        self.live = live
        if live and broker.is_paper:
            raise ValueError("live gateway constructed with a paper broker")
        if not live and not broker.is_paper:
            raise ValueError("paper gateway constructed with a live broker")

    # ----------------------------------------------------------------- state
    def _orders_for(self, approval_hash: str) -> list[JournalEntry]:
        return [
            e
            for e in self.journal.entries("order")
            if e.payload.get("approval_hash") == approval_hash
        ]

    def notional_today(self, now: dt.datetime) -> float:
        day = now.date().isoformat()
        return sum(
            float(e.payload["notional_usd"])
            for e in self.journal.entries("order")
            if e.payload.get("status") != "rejected"
            and str(e.payload.get("placed_at_utc", ""))[:10] == day
        )

    # ------------------------------------------------------------------ place
    def place(self, approval_hash: str, account_value_usd: float) -> list[PlacedOrder]:
        now = self.clock()
        blocked = self.flags.orders_blocked()
        if blocked:
            raise OrderBlocked(f"order submission blocked ({blocked})")
        entry = self.journal.find_by_hash(approval_hash)
        if entry is None or entry.entry_type != "approval":
            raise OrderBlocked("no journaled approval with that hash")
        rec = ApprovalRecord.model_validate(entry.payload)
        briefing = self.journal.find_by_hash(rec.briefing_hash)
        if briefing is None or briefing.seq != rec.briefing_seq:
            raise OrderBlocked("approval does not reference a journaled briefing")
        if now - entry.created_at > dt.timedelta(days=TRANCHE_WINDOW_DAYS):
            raise OrderBlocked("approval is older than the tranche window; re-brief")
        prior = self._orders_for(approval_hash)
        per_order_cap = account_value_usd * self.caps.per_order_pct / 100
        daily_left = account_value_usd * self.caps.daily_pct / 100 - self.notional_today(now)
        placed: list[PlacedOrder] = []
        for leg in rec.legs:
            target = rec.account_value_usd * leg.pct_total / 100
            done = sum(
                float(e.payload["notional_usd"])
                for e in prior
                if e.payload["symbol"] == leg.symbol
                and e.payload["side"] == leg.side
                and e.payload.get("status") != "rejected"
            )
            remaining = target - done
            if remaining <= 1.0:
                continue
            close = self.last_close(leg.symbol)
            if close <= 0:
                raise OrderBlocked(f"{leg.symbol}: no valid last close")
            band = self.caps.limit_band_pct / 100
            limit = round(close * (1 + band) if leg.side == "buy" else close * (1 - band), 2)
            notional = min(remaining, per_order_cap, daily_left)
            qty = math.floor(notional / limit)
            if qty <= 0:
                continue
            notional = qty * limit
            n = len([e for e in prior if e.payload["symbol"] == leg.symbol]) + len(placed) + 1
            req = OrderRequest(
                client_order_id=_cid(approval_hash, leg.symbol, leg.side, n),
                symbol=leg.symbol,
                side=leg.side,
                qty=qty,
                limit_price=limit,
                time_in_force="gtc" if self.caps.time_in_force == "gtc" else "day",
                account=leg.account,
            )
            venue = "broker" if (not self.live or leg.account == "taxable") else "manual"
            base = {
                "approval_hash": approval_hash,
                "briefing_hash": rec.briefing_hash,
                "client_order_id": req.client_order_id,
                "symbol": req.symbol,
                "side": req.side,
                "qty": qty,
                "limit_price": limit,
                "notional_usd": notional,
                "account": leg.account,
                "venue": venue,
                "mode": "live" if self.live else "paper",
                "placed_at_utc": now.isoformat(),
            }
            if venue == "manual":
                e = self.journal.append(
                    "order", {**base, "status": "manual_ticket", "broker_order_id": None}
                )
            else:
                try:
                    bo = self.broker.submit_limit_order(req)
                except Exception as exc:
                    self.journal.append(
                        "order",
                        {
                            **base,
                            "status": "rejected",
                            "broker_order_id": None,
                            "error": str(exc)[:300],
                        },
                    )
                    raise OrderBlocked(f"broker error for {leg.symbol}: {exc}") from exc
                e = self.journal.append(
                    "order", {**base, "status": bo.status, "broker_order_id": bo.broker_order_id}
                )
            daily_left -= notional
            placed.append(
                PlacedOrder(
                    req.client_order_id, req.symbol, req.side, qty, limit, notional, venue, e.seq
                )
            )
            if daily_left <= 1.0:
                break
        return placed

    # -------------------------------------------------------------- reconcile
    def reconcile(self, on_fill: Callable[[Fill], None] | None = None) -> list[Fill]:
        """Journal new fills from the broker (deduped) and pass each to ``on_fill`` (tax lots)."""
        known = {e.payload["fill_id"] for e in self.journal.entries("fill")}
        ours = {e.payload["client_order_id"]: e.payload for e in self.journal.entries("order")}
        out: list[Fill] = []
        for o in self.broker.list_orders("all"):
            meta = ours.get(o.client_order_id)
            if meta is None or o.filled_qty <= 0 or o.filled_avg_price is None:
                continue
            fid = f"{o.broker_order_id}:{o.filled_qty:g}"
            if fid in known:
                continue
            prev = sum(
                float(e.payload["qty"])
                for e in self.journal.entries("fill")
                if e.payload["broker_order_id"] == o.broker_order_id
            )
            qty = o.filled_qty - prev
            if qty <= 0:
                continue
            f = Fill(
                fill_id=fid,
                broker_order_id=o.broker_order_id,
                client_order_id=o.client_order_id,
                symbol=o.symbol,
                side=o.side,
                qty=qty,
                price=o.filled_avg_price,
                filled_at=o.filled_at or self.clock(),
                account=meta["account"],
            )
            self.journal.append("fill", f.model_dump(mode="json"))
            if on_fill:
                on_fill(f)
            out.append(f)
        return out

    def unreconciled_orders(self) -> list[str]:
        """Broker orders that are filled but have no journaled fill (should be empty)."""
        filled = {e.payload["broker_order_id"] for e in self.journal.entries("fill")}
        return [
            o.client_order_id
            for o in self.broker.list_orders("all")
            if o.filled_qty > 0 and o.broker_order_id not in filled
        ]


def kill_switch(journal: Journal, broker: BrokerAdapter, flags: Flags, reason: str) -> int:
    """Cancel open orders, block submission, journal it. Returns orders cancelled."""
    flags.set("kill_switch", reason)
    try:
        n = broker.cancel_all_orders()
        err = None
    except Exception as exc:  # still keep the switch on
        n, err = 0, str(exc)[:300]
    journal.append(
        "kill_switch", {"action": "engaged", "reason": reason, "orders_cancelled": n, "error": err}
    )
    return n


def release_kill_switch(journal: Journal, flags: Flags, reason: str) -> None:
    flags.clear("kill_switch")
    journal.append("kill_switch", {"action": "released", "reason": reason})
