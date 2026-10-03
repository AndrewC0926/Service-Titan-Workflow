"""Offline paper broker persisted to a JSON file (used when no Alpaca paper keys
are configured). Fills limit orders immediately at the limit price."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from committee.broker.fake import FakeBroker
from committee.broker.models import BrokerOrder, OrderRequest


@dataclass
class SimBroker:
    path: Path

    @property
    def is_paper(self) -> bool:
        return True

    def _load(self) -> FakeBroker:
        fb = FakeBroker()
        if self.path.exists():
            data = json.loads(self.path.read_text())
            for o in data.get("orders", []):
                bo = BrokerOrder.model_validate(o)
                fb.orders[bo.broker_order_id] = bo
            fb._pos = {k: float(v) for k, v in data.get("positions", {}).items()}
            fb._ids = __import__("itertools").count(len(fb.orders) + 1)
        return fb

    def _save(self, fb: FakeBroker) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(
            json.dumps(
                {
                    "orders": [o.model_dump(mode="json") for o in fb.orders.values()],
                    "positions": fb._pos,
                },
                indent=1,
            )
        )

    def submit_limit_order(self, req: OrderRequest) -> BrokerOrder:
        fb = self._load()
        o = fb.submit_limit_order(req)
        self._save(fb)
        return o

    def cancel_all_orders(self) -> int:
        fb = self._load()
        n = fb.cancel_all_orders()
        self._save(fb)
        return n

    def list_orders(self, status: str = "all") -> list[BrokerOrder]:
        return self._load().list_orders(status)

    def positions(self) -> dict[str, float]:
        return self._load().positions()
