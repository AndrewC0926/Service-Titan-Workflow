"""In-memory broker for tests and offline paper simulation."""

from __future__ import annotations

import datetime as dt
import itertools
from dataclasses import dataclass, field

from committee.broker.models import BrokerOrder, OrderRequest


@dataclass
class FakeBroker:
    """Fills limit orders immediately at the limit price when ``auto_fill`` is set."""

    auto_fill: bool = True
    fail_submit: bool = False
    is_paper: bool = True
    orders: dict[str, BrokerOrder] = field(default_factory=dict)
    submitted: list[OrderRequest] = field(default_factory=list)
    _ids: itertools.count[int] = field(default_factory=lambda: itertools.count(1))
    _pos: dict[str, float] = field(default_factory=dict)

    def submit_limit_order(self, req: OrderRequest) -> BrokerOrder:
        if self.fail_submit:
            raise ConnectionError("broker unavailable")
        self.submitted.append(req)
        oid = f"FAKE-{next(self._ids)}"
        filled = self.auto_fill
        o = BrokerOrder(
            broker_order_id=oid,
            client_order_id=req.client_order_id,
            symbol=req.symbol,
            side=req.side,
            qty=req.qty,
            limit_price=req.limit_price,
            status="filled" if filled else "new",
            filled_qty=req.qty if filled else 0.0,
            filled_avg_price=req.limit_price if filled else None,
            filled_at=dt.datetime.now(dt.UTC) if filled else None,
        )
        self.orders[oid] = o
        if filled:
            sign = 1 if req.side == "buy" else -1
            self._pos[req.symbol] = self._pos.get(req.symbol, 0.0) + sign * req.qty
        return o

    def cancel_all_orders(self) -> int:
        n = 0
        for oid, o in list(self.orders.items()):
            if o.status in ("new", "accepted", "partially_filled"):
                self.orders[oid] = o.model_copy(update={"status": "canceled"})
                n += 1
        return n

    def list_orders(self, status: str = "all") -> list[BrokerOrder]:
        return list(self.orders.values())

    def positions(self) -> dict[str, float]:
        return dict(self._pos)
