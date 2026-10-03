"""Broker adapter protocol. Only modules in committee.broker may call a broker API."""

from __future__ import annotations

from typing import Protocol

from committee.broker.models import BrokerOrder, OrderRequest


class BrokerAdapter(Protocol):
    @property
    def is_paper(self) -> bool: ...

    def submit_limit_order(self, req: OrderRequest) -> BrokerOrder: ...

    def cancel_all_orders(self) -> int: ...

    def list_orders(self, status: str = "all") -> list[BrokerOrder]: ...

    def positions(self) -> dict[str, float]: ...
