"""Alpaca adapter (alpaca-py). Paper by default; live only through ``live_gate``."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from committee.broker.models import BrokerOrder, OrderRequest


def _side(v: Any) -> str:
    return str(getattr(v, "value", v)).lower()


@dataclass
class AlpacaBroker:
    api_key: str
    secret_key: str
    paper: bool = True
    client: Any = field(default=None, repr=False)

    def __post_init__(self) -> None:
        if self.client is None:
            from alpaca.trading.client import TradingClient

            self.client = TradingClient(self.api_key, self.secret_key, paper=self.paper)

    def __repr__(self) -> str:
        return f"AlpacaBroker(paper={self.paper})"

    @property
    def is_paper(self) -> bool:
        return self.paper

    @staticmethod
    def _convert(o: Any) -> BrokerOrder:
        return BrokerOrder(
            broker_order_id=str(o.id),
            client_order_id=str(o.client_order_id),
            symbol=str(o.symbol),
            side="buy" if _side(o.side) == "buy" else "sell",
            qty=float(o.qty or 0),
            limit_price=float(o.limit_price) if o.limit_price is not None else None,
            status=_side(o.status),
            filled_qty=float(o.filled_qty or 0),
            filled_avg_price=float(o.filled_avg_price) if o.filled_avg_price is not None else None,
            filled_at=o.filled_at,
        )

    def submit_limit_order(self, req: OrderRequest) -> BrokerOrder:
        from alpaca.trading.enums import OrderSide, TimeInForce
        from alpaca.trading.requests import LimitOrderRequest

        order = self.client.submit_order(
            LimitOrderRequest(
                symbol=req.symbol,
                qty=req.qty,
                side=OrderSide.BUY if req.side == "buy" else OrderSide.SELL,
                time_in_force=TimeInForce.DAY if req.time_in_force == "day" else TimeInForce.GTC,
                limit_price=round(req.limit_price, 2),
                client_order_id=req.client_order_id,
            )
        )
        return self._convert(order)

    def cancel_all_orders(self) -> int:
        return len(self.client.cancel_orders() or [])

    def list_orders(self, status: str = "all") -> list[BrokerOrder]:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        q = {
            "all": QueryOrderStatus.ALL,
            "open": QueryOrderStatus.OPEN,
            "closed": QueryOrderStatus.CLOSED,
        }[status]
        return [
            self._convert(o) for o in self.client.get_orders(GetOrdersRequest(status=q, limit=500))
        ]

    def positions(self) -> dict[str, float]:
        return {str(p.symbol): float(p.qty) for p in self.client.get_all_positions()}
